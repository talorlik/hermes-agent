"""Producer-first delivery generation contract.

scheduler tick -> fanout admission -> durable outbox/queue intents -> transport ->
owned-generation callback (CAS) -> one aggregate finalization.

Real SQLite stores (``executions.db`` outbox/projection, ``deliveries.db`` queue) and the real
``jobs.json`` only; the transport is the single seam that is stubbed. Contract:

* every concrete intent is persisted (and queue-claimed) BEFORE the first send;
* the invocation carries the immutable ``(job, execution, projection_revision)`` it admitted and the
  durable queue snapshot carries the same token, so late callbacks never borrow the newest row;
* settling the fanout must not rotate the owner's generation out from under its own publication;
* the run aggregate publishes exactly once under the owned token, and the finalized projection
  overlay must not hide that publication;
* stale and partial tokens are rejected byte-for-byte; a competing process publication blocks on
  the projection reservation; a publication fault rolls rows and generation back together.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD_PUBLISH = """
import json, sys
from pathlib import Path
from cron import executions, outbox
executions.EXECUTIONS_FILE = Path(sys.argv[1])
outbox.EXECUTIONS_FILE = Path(sys.argv[1])
sys.stdout.write("ready\\n"); sys.stdout.flush()
# Explicit start handshake: nothing touches SQLite until the parent, already holding the
# reservation, releases this process. "ready" alone does not order the two publications.
assert sys.stdin.readline().strip() == "go"
rows = outbox.enqueue_deliveries_with_intent(
    execution_id=sys.argv[3], job_id=sys.argv[2], target="telegram",
    destinations=[{"platform": "telegram", "chat_id": "child-chat"}],
    content="child body", intent_success=True,
    job={"id": sys.argv[2], "execution_id": sys.argv[3]},
)
sys.stdout.write(json.dumps({"revision": rows[0]["projection_revision"]}) + "\\n")
"""


@pytest.fixture
def producer_env(tmp_path, monkeypatch):
    """Isolated cron home with a deliverable no_agent job due now (real stores)."""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron" / "output").mkdir(parents=True)
    (hermes_home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "home-chat")
    monkeypatch.delenv("_HERMES_CRON_EXTERNAL_WORKER", raising=False)

    import cron.delivery_queue as queue_mod
    import cron.executions as executions_mod
    import cron.jobs as jobs_mod
    import cron.outbox as outbox_mod

    db = hermes_home / "cron" / "executions.db"
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    monkeypatch.setattr(executions_mod, "EXECUTIONS_FILE", db)
    monkeypatch.setattr(outbox_mod, "EXECUTIONS_FILE", db)
    monkeypatch.setattr(
        queue_mod, "DELIVERY_DB", hermes_home / "cron" / "deliveries.db"
    )
    queue_mod._ACTIVE_DELIVERIES.clear()

    (hermes_home / "scripts" / "report.sh").write_text(
        "#!/bin/bash\necho 'daily report content'\nexit 0\n"
    )
    job = jobs_mod.create_job(
        prompt="report",
        schedule="every 10m",
        no_agent=True,
        script="report.sh",
        deliver="telegram",
    )
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    jobs_mod.update_job(job["id"], {"next_run_at": due})
    return {"home": hermes_home, "job_id": job["id"], "db": db}


def _stored(home: Path, job_id: str) -> dict:
    data = json.loads((home / "cron" / "jobs.json").read_text())
    return next(j for j in data["jobs"] if j["id"] == job_id)


def test_tick_persists_every_intent_before_the_first_send_and_binds_the_owned_generation(
    producer_env, monkeypatch
):
    import cron.executions as E
    import cron.jobs as J
    from cron import outbox as O
    from cron import scheduler as S

    job_id = producer_env["job_id"]
    seen: list[dict] = []

    def capture(
        job, content, *args, destination=None, outbox_id=None, delivery_run=None, **kw
    ):
        row = O.get_outbox(outbox_id) if outbox_id else None
        seen.append({
            "token": job.get("_delivery_projection_revision"),
            "execution_id": job.get("execution_id"),
            "outbox_id": outbox_id,
            "row_state": row["exact_state"] if row else None,
            "row_revision": row["projection_revision"] if row else None,
            "destination": destination,
            "run_job": getattr(delivery_run, "job_id", None),
            "content": content,
        })
        return None

    monkeypatch.setattr(S, "_deliver_result", capture)
    S.tick(verbose=False, sync=True)

    assert len(seen) == 1
    sent = seen[0]
    # The intent row existed and was queue-claimed before the transport ran.
    assert sent["row_state"] == "IN_FLIGHT"
    assert sent["token"] is not None
    assert sent["token"] == sent["row_revision"]
    assert sent["run_job"] == job_id
    assert "daily report content" in sent["content"]
    assert sent["destination"] == {
        "platform": "telegram",
        "chat_id": "home-chat",
        "thread_id": None,
        "_resolved_from": "home",
    }

    projection = O.get_job_delivery_projection(job_id)
    assert projection["execution_id"] == sent["execution_id"]
    # Settlement finalizes the owner's generation without rotating it away from the owner.
    assert projection["revision"] == sent["token"]
    assert projection["finalized"] == 1
    # The overlay shows the status the run recorded in the job store, not the
    # outbox settlement word. The ledger row below stays "completed".
    assert projection["last_status"] == "ok"

    rows = O.list_outbox(job_id=job_id)
    assert [row["state"] for row in rows] == ["delivered"]
    assert rows[0]["projection_revision"] == sent["token"]
    # The durable queue snapshot carries the owned token for any late replay/callback.
    assert (
        json.loads(rows[0]["job_json"])["_delivery_projection_revision"]
        == sent["token"]
    )

    latest = E.latest_execution(job_id)
    assert latest["status"] == "completed"
    assert latest["delivery_status"] == "delivered"
    assert latest["delivery_attempts"] == 1
    assert J.get_job(job_id)["last_status"] == "ok"


def test_fanout_aggregate_publishes_once_under_the_owned_token_after_settlement(
    producer_env, monkeypatch
):
    import cron.jobs as J
    from cron import outbox as O
    from cron import scheduler as S
    from cron import scheduler_delivery as D

    job_id = producer_env["job_id"]
    mapping = {"telegram": "home-t", "discord": "home-d"}
    monkeypatch.setattr(D, "_iter_home_target_platforms", lambda: iter(mapping))
    monkeypatch.setattr(
        D, "_get_home_target_chat_id", lambda platform: mapping[platform]
    )
    monkeypatch.setattr(D, "_get_home_target_thread_id", lambda platform: None)
    J.update_job(job_id, {"deliver": "all"})

    def contribute(job, content, *args, destination, outbox_id, delivery_run, **kwargs):
        delivery_run.record(
            D.DestinationDeliveryOutcome(
                outbox_id,
                destination,
                "delivered",
                unverified_targets=(
                    f"{destination['platform']}:{destination['chat_id']}",
                ),
            )
        )
        return None

    monkeypatch.setattr(S, "_deliver_result", contribute)
    cas_writes: list[dict] = []
    real_update = J.update_job

    def spy(job_key, values, **cas):
        if cas:
            cas_writes.append({"values": dict(values), "cas": dict(cas)})
        return real_update(job_key, values, **cas)

    monkeypatch.setattr(J, "update_job", spy)

    S.tick(verbose=False, sync=True)

    projection = O.get_job_delivery_projection(job_id)
    assert projection["finalized"] == 1
    assert len(cas_writes) == 1, cas_writes
    assert cas_writes[0]["cas"] == {
        "expected_execution_id": projection["execution_id"],
        "expected_projection_revision": projection["revision"],
    }
    expected_unverified = ["telegram:home-t", "discord:home-d"]
    assert sorted(cas_writes[0]["values"]["last_delivery_unverified"]) == sorted(
        expected_unverified
    )
    assert sorted(
        _stored(producer_env["home"], job_id)["last_delivery_unverified"]
    ) == sorted(expected_unverified)
    # The finalized-projection overlay must surface, not hide, the owned publication.
    refreshed = J.get_job(job_id)
    assert sorted(refreshed["last_delivery_unverified"]) == sorted(expected_unverified)
    assert refreshed["last_status"] == "ok"
    assert {row["state"] for row in O.list_outbox(job_id=job_id)} == {"delivered"}


def test_stale_and_partial_tokens_cannot_overwrite_a_modern_generation(producer_env):
    from cron import outbox as O
    from cron import scheduler_delivery as D

    home, job_id = producer_env["home"], producer_env["job_id"]
    destination = {"platform": "telegram", "chat_id": "home-chat"}
    first = O.enqueue_deliveries_with_intent(
        execution_id="exec-a",
        job_id=job_id,
        target="telegram",
        destinations=[destination],
        content="a",
        intent_success=True,
        job={"id": job_id, "execution_id": "exec-a"},
    )[0]
    stale = json.loads(first["job_json"])
    assert stale["_delivery_projection_revision"] == first["projection_revision"]

    second = O.enqueue_deliveries_with_intent(
        execution_id="exec-b",
        job_id=job_id,
        target="telegram",
        destinations=[destination],
        content="b",
        intent_success=True,
        job={"id": job_id, "execution_id": "exec-b"},
    )[0]
    modern = json.loads(second["job_json"])
    assert modern["_delivery_projection_revision"] == first["projection_revision"] + 1

    before = (home / "cron" / "jobs.json").read_bytes()
    # Stale COMPLETE token (the retired generation's execution + revision) racing a modern
    # generation: byte-identical jobs.json.
    D._record_delivery_verification(dict(stale), ["stale-target"])
    assert (home / "cron" / "jobs.json").read_bytes() == before
    # Partial token (execution only): refused rather than borrowing the current revision.
    D._record_delivery_verification(
        {"id": job_id, "execution_id": "exec-b"}, ["partial"]
    )
    assert (home / "cron" / "jobs.json").read_bytes() == before
    # Actual token-free asynchronous callback (no execution id at all) against an existing
    # modern projection: refused byte-for-byte, never the legacy unguarded write.
    D._record_delivery_verification({"id": job_id}, ["token-free"])
    assert (home / "cron" / "jobs.json").read_bytes() == before
    assert (
        O.get_job_delivery_projection(job_id)["revision"]
        == modern["_delivery_projection_revision"]
    )
    # The owner of the current generation publishes.
    D._record_delivery_verification(dict(modern), ["modern-target"])
    assert _stored(home, job_id)["last_delivery_unverified"] == ["modern-target"]


def test_competing_process_publication_blocks_on_the_reservation_and_rotates_once(
    producer_env,
):
    from cron import outbox as O

    db, job_id = producer_env["db"], producer_env["job_id"]
    mine = O.enqueue_deliveries_with_intent(
        execution_id="exec-mine",
        job_id=job_id,
        target="telegram",
        destinations=[{"platform": "telegram", "chat_id": "mine"}],
        content="mine",
        intent_success=True,
        job={"id": job_id, "execution_id": "exec-mine"},
    )[0]
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "HERMES_HOME": str(db.parents[1]),
    }
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", _CHILD_PUBLISH, str(db), job_id, "exec-child"],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdin is not None
        # "ready" = imports done, SQLite untouched. The child is released into its publication
        # only AFTER the parent holds the reservation, so the ordering is forced, not scheduled.
        assert child.stdout.readline().strip() == "ready"
        with O.reserve_job_delivery_projection(job_id) as reserved:
            assert reserved["revision"] == mine["projection_revision"]
            child.stdin.write("go\n")
            child.stdin.flush()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                assert child.poll() is None, (
                    "independent process published a fanout while the reservation was held"
                )
                time.sleep(0.05)
        out, err = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 0, err
    rotated = json.loads(out.strip().splitlines()[-1])
    assert rotated["revision"] == mine["projection_revision"] + 1
    projection = O.get_job_delivery_projection(job_id)
    assert projection["execution_id"] == "exec-child"
    assert projection["revision"] == rotated["revision"]
    # Each generation's rows keep the token they were admitted under.
    by_execution = {row["execution_id"]: row for row in O.list_outbox(job_id=job_id)}
    assert (
        by_execution["exec-mine"]["projection_revision"] == mine["projection_revision"]
    )
    assert by_execution["exec-child"]["projection_revision"] == rotated["revision"]


def test_publication_fault_rolls_back_rows_and_generation_together(
    producer_env, monkeypatch
):
    from cron import outbox as O

    job_id = producer_env["job_id"]
    previous = O.begin_job_delivery_projection(job_id, "exec-prev")

    def fault(*_args, **_kwargs):
        raise RuntimeError("ledger fault inside publication")

    monkeypatch.setattr(O, "_stamp_execution_intent", fault)
    with pytest.raises(RuntimeError, match="ledger fault"):
        O.enqueue_deliveries_with_intent(
            execution_id="exec-a",
            job_id=job_id,
            target="all",
            destinations=[
                {"platform": "telegram", "chat_id": "one"},
                {"platform": "discord", "chat_id": "two"},
            ],
            content="body",
            intent_success=True,
        )
    assert O.list_outbox(job_id=job_id) == []
    projection = O.get_job_delivery_projection(job_id)
    assert projection["execution_id"] == "exec-prev"
    assert projection["revision"] == previous["revision"]
    assert projection["finalized"] == 0


def test_settled_generation_accepts_its_owner_once_and_rejects_after_rotation(
    producer_env,
):
    """Settlement finalizes the owner's generation WITHOUT rotating it: the owning invocation's
    late publication (same token) still lands, exactly once per distinct value; only a new
    ``begin`` (next execution, or a same-execution re-begin) retires the token, after which late
    receipts carrying it are rejected byte-for-byte."""
    import cron.jobs as J
    from cron import outbox as O
    from cron import scheduler_delivery as D

    home, job_id = producer_env["home"], producer_env["job_id"]
    destination = {"platform": "telegram", "chat_id": "home-chat"}
    rows = O.enqueue_deliveries_with_intent(
        execution_id="exec-settled",
        job_id=job_id,
        target="telegram",
        destinations=[destination],
        content="body",
        intent_success=True,
        job={"id": job_id, "execution_id": "exec-settled"},
    )
    owned = json.loads(rows[0]["job_json"])
    token = owned["_delivery_projection_revision"]
    # The queue owner settles the single row: projection finalized, revision unchanged.
    claimed = O.claim_exact(
        owner_process_id="p",
        owner_pid=1,
        owner_started_at="s",
        outbox_ids=[rows[0]["id"]],
    )
    assert claimed is not None
    assert O.finish_exact(
        rows[0]["id"],
        generation=int(claimed["generation"]),
        owner_token=str(claimed["owner_token"]),
        error=None,
    )
    projection = O.get_job_delivery_projection(job_id)
    assert projection["finalized"] == 1 and projection["revision"] == token

    # Late owner callback after settlement publishes under the original token ...
    writes: list = []
    real_update = J.update_job

    def spy(job_key, values, **cas):
        result = real_update(job_key, values, **cas)
        writes.append((dict(values), dict(cas), result is not None))
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(J, "update_job", spy)
        D._record_delivery_verification(dict(owned), ["late-target"])
        # ... and an identical late receipt is idempotent: no second write at all.
        D._record_delivery_verification(
            dict(owned, last_delivery_unverified=["late-target"]), ["late-target"]
        )
    assert writes == [
        (
            {"last_delivery_unverified": ["late-target"]},
            {
                "expected_execution_id": "exec-settled",
                "expected_projection_revision": token,
            },
            True,
        )
    ]
    assert _stored(home, job_id)["last_delivery_unverified"] == ["late-target"]

    # A same-execution re-begin is a stale rotation for the old token; a new execution likewise.
    before = (home / "cron" / "jobs.json").read_bytes()
    rebegun = O.begin_job_delivery_projection(job_id, "exec-settled")
    assert rebegun["revision"] == token + 1 and rebegun["finalized"] == 0
    D._record_delivery_verification(dict(owned), ["after-rebegin"])
    assert (home / "cron" / "jobs.json").read_bytes() == before
    O.begin_job_delivery_projection(job_id, "exec-next")
    D._record_delivery_verification(dict(owned), ["after-next-execution"])
    assert (home / "cron" / "jobs.json").read_bytes() == before
    assert _stored(home, job_id)["last_delivery_unverified"] == ["late-target"]
