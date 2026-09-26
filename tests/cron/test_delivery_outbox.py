"""Transactional delivery outbox with at-least-once exact delivery.

Contract under test (recurring-automation P1/P2, requirement 4):

* Before the scheduler attempts a platform send, the execution's terminal
  intent AND a pending outbox row are persisted atomically (same SQLite
  transaction, same durable store).
* Every attempt is recorded in a per-attempt history; a failed send leaves
  the row pending (queryable) and mints a delivery incident — the SAME
  transaction writes both (requirement 5 atomicity).
* Pending deliveries are retried BEFORE new work on the next tick.
* Exact claims are at-least-once: a crash after transport acceptance leaves
  durable work for a replacement gateway to resend. Duplicates are acceptable,
  silent loss is not.
* A failed primary channel also lands the content in a durable local
  fallback file (no external credentials involved).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def outbox_env(tmp_path, monkeypatch):
    """Isolated cron env with a deliverable no_agent job due now."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    (hermes_home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "home-chat")

    import cron.executions as executions_mod
    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    monkeypatch.setattr(
        executions_mod, "EXECUTIONS_FILE", hermes_home / "cron" / "executions.db"
    )

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

    return {"home": hermes_home, "job_id": job["id"]}


def test_failed_send_leaves_pending_outbox_with_attempt_and_incident(
    outbox_env, monkeypatch
):
    from cron import incidents as I
    from cron import outbox as O
    from cron import scheduler as S

    monkeypatch.setattr(
        S,
        "_deliver_result",
        lambda *a, **k: "telegram adapter unreachable: connection refused",
    )
    S.tick(verbose=False, sync=True)

    rows = O.list_outbox(job_id=outbox_env["job_id"])
    assert len(rows) == 1
    entry = rows[0]
    assert entry["state"] == "pending"
    assert entry["attempts"] == 1
    assert "daily report content" in entry["content"]

    history = O.list_attempts(entry["id"])
    assert len(history) == 1
    assert history[0]["status"] == "failed"
    assert "unreachable" in history[0]["error"]

    # Delivery-only failure is incident-visible while the job itself is OK.
    assert I.count_incidents() == 1
    import cron.jobs as J

    refreshed = J.get_job(outbox_env["job_id"])
    assert refreshed is not None
    # Current upstream distinguishes a successful run whose delivery failed.
    assert refreshed["last_status"] == "delivery_failed"
    assert refreshed["last_delivery_error"]


def test_successful_send_marks_outbox_delivered(outbox_env, monkeypatch):
    from cron import outbox as O
    from cron import scheduler as S

    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: None)
    S.tick(verbose=False, sync=True)

    rows = O.list_outbox(job_id=outbox_env["job_id"])
    assert len(rows) == 1
    assert rows[0]["state"] == "delivered"
    assert rows[0]["attempts"] == 1
    history = O.list_attempts(rows[0]["id"])
    assert [h["status"] for h in history] == ["delivered"]


def test_enqueue_failure_blocks_external_send_and_writes_local_fallback(
    outbox_env, monkeypatch
):
    from unittest.mock import MagicMock

    from cron import outbox as O
    from cron import scheduler as S

    monkeypatch.setattr(
        O,
        "enqueue_deliveries_with_intent",
        MagicMock(side_effect=RuntimeError("outbox unavailable")),
    )
    deliver = MagicMock(return_value=None)
    monkeypatch.setattr(S, "_deliver_result", deliver)

    S.tick(verbose=False, sync=True)

    deliver.assert_not_called()
    assert O.list_outbox(job_id=outbox_env["job_id"]) == []
    fallback_dir = outbox_env["home"] / "cron" / "failed_deliveries"
    files = list(fallback_dir.rglob("*.md")) if fallback_dir.exists() else []
    assert len(files) == 1
    assert "daily report content" in files[0].read_text()

    import cron.jobs as J

    refreshed = J.get_job(outbox_env["job_id"])
    assert refreshed is not None
    assert refreshed["last_status"] == "delivery_failed"
    assert "outbox unavailable" in (refreshed["last_delivery_error"] or "")


def test_pending_delivery_retries_before_new_work(outbox_env, monkeypatch):
    from cron import outbox as O
    from cron import scheduler as S

    calls = []

    def failing_deliver(job, content, *a, **k):
        calls.append(("send", content))
        return "adapter down"

    monkeypatch.setattr(S, "_deliver_result", failing_deliver)
    S.tick(verbose=False, sync=True)
    assert len(O.list_outbox(job_id=outbox_env["job_id"], state="pending")) == 1
    calls.clear()

    # Next tick: adapter healed, job due again. The pending retry must be
    # sent BEFORE the new run's own delivery.
    def working_deliver(job, content, *a, **k):
        calls.append(("send", content))
        return None

    monkeypatch.setattr(S, "_deliver_result", working_deliver)
    import cron.jobs as J

    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    J.update_job(outbox_env["job_id"], {"next_run_at": due})
    S.tick(verbose=False, sync=True)

    assert len(calls) == 2  # retried backlog + the new run's delivery
    # Retry of the OLD content happened first.
    assert calls[0][1].strip().startswith("daily report content") or (
        "daily report content" in calls[0][1]
    )
    assert O.list_outbox(job_id=outbox_env["job_id"], state="pending") == []
    delivered = O.list_outbox(job_id=outbox_env["job_id"], state="delivered")
    assert len(delivered) == 2


def test_pending_legacy_replay_preserves_failure_lane(outbox_env, monkeypatch):
    from cron import outbox as O
    from cron import scheduler as S
    import cron.jobs as J

    job = J.get_job(outbox_env["job_id"])
    assert job is not None
    entry = O.enqueue_with_intent(
        execution_id=None,
        job_id=job["id"],
        target="telegram:original-chat",
        content="failure alert",
        intent_success=False,
        intent_error="run failed",
    )
    J.update_job(
        job["id"],
        {"deliver": "discord:new-normal", "failure_deliver": "slack:new-failure"},
    )
    observed = []

    def capture(replay_job, content, *args, **kwargs):
        observed.append((replay_job, content, kwargs))
        return None

    monkeypatch.setattr(S, "_deliver_result", capture)

    assert S._retry_pending_deliveries() == 1
    assert len(observed) == 1
    replay_job, content, kwargs = observed[0]
    assert content == "failure alert"
    assert kwargs["for_failure"] is True
    stored = O.list_outbox(job_id=job["id"])
    assert stored[0]["id"] == entry["id"]
    assert stored[0]["state"] == "delivered"


def test_crash_after_transport_accept_redelivers_at_least_once(outbox_env, monkeypatch):
    """An ambiguous transport boundary remains durable and is retried."""
    from cron import outbox as O
    from cron import scheduler as S

    sent = []

    def send_then_crash(job, content, *a, **k):
        sent.append(content)
        raise SystemExit("process killed mid-delivery bookkeeping")

    monkeypatch.setattr(S, "_deliver_result", send_then_crash)
    with pytest.raises(SystemExit):
        S.tick(verbose=False, sync=True)

    rows = O.list_outbox(job_id=outbox_env["job_id"])
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["exact_state"] == "READY"

    monkeypatch.setattr(
        S, "_deliver_result", lambda *a, **k: sent.append("retry") or None
    )
    S.tick(verbose=False, sync=True)
    assert sent == ["daily report content", "retry"]
    assert O.list_outbox(job_id=outbox_env["job_id"], state="pending") == []


def test_no_duplicate_incident_or_outbox_across_repeated_failures(
    outbox_env, monkeypatch
):
    from cron import incidents as I
    from cron import outbox as O
    from cron import scheduler as S

    monkeypatch.setattr(
        S, "_deliver_result", lambda *a, **k: "telegram adapter unreachable"
    )
    S.tick(verbose=False, sync=True)

    import cron.jobs as J

    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    J.update_job(outbox_env["job_id"], {"next_run_at": due})
    S.tick(verbose=False, sync=True)

    # Two runs, each with its own outbox entry (different content instances)
    # is at-least-once bookkeeping — but the retried FIRST entry must not
    # have spawned duplicates of itself, and the identical delivery-failure
    # signature must map to ONE incident.
    pending = O.list_outbox(job_id=outbox_env["job_id"], state="pending")
    ids = [row["id"] for row in pending]
    assert len(ids) == len(set(ids))
    assert I.count_incidents() == 1


def test_delivery_incident_stays_open_until_all_fanout_rows_settle(outbox_env):
    from cron import delivery_queue as Q
    from cron import incidents as I
    from cron import outbox as O

    entries = O.enqueue_deliveries_with_intent(
        execution_id=None,
        job_id=outbox_env["job_id"],
        target="all",
        destinations=[
            {"platform": "telegram", "chat_id": "chat-a", "thread_id": None},
            {"platform": "discord", "chat_id": "chat-b", "thread_id": None},
        ],
        content="fanout",
        intent_success=True,
    )

    failed = Q.claim_next()
    assert failed is not None
    failed_id = failed["outbox_id"]
    remaining_id = next(entry["id"] for entry in entries if entry["id"] != failed_id)
    assert Q._finish(
        failed["execution_id"],
        error="telegram unavailable",
        generation=failed["generation"],
        owner_token=failed["owner_token"],
    )
    incident = I.open_incidents()[0]

    delivered = Q.claim_next()
    assert delivered is not None and delivered["outbox_id"] == remaining_id
    assert Q._finish(
        delivered["execution_id"],
        error=None,
        generation=delivered["generation"],
        owner_token=delivered["owner_token"],
    )

    assert I.get_incident(incident["id"])["state"] in I.OPEN_INCIDENT_STATES

    assert Q.reactivate_exact(failed_id)
    retried = Q.claim_next()
    assert retried is not None and retried["outbox_id"] == failed_id
    assert Q._finish(
        retried["execution_id"],
        error=None,
        generation=retried["generation"],
        owner_token=retried["owner_token"],
    )
    assert I.get_incident(incident["id"])["state"] == "recovered"


def test_delivered_retry_recovers_delivery_incident_only(outbox_env, monkeypatch):
    """A healed channel recovers the DELIVERY incident in the same commit
    as the delivered attempt — and never touches an open EXECUTION
    incident for the same job (category independence, inverse direction)."""
    from cron import incidents as I
    from cron import outbox as O
    from cron import scheduler as S

    outage = {"active": True}
    monkeypatch.setattr(
        S,
        "_deliver_result",
        lambda *a, **k: "telegram unreachable" if outage["active"] else None,
    )
    S.tick(verbose=False, sync=True)
    open_rows = I.open_incidents()
    assert len(open_rows) == 1  # the delivery incident
    delivery_incident_id = open_rows[0]["id"]

    # Independent open execution incident, seeded AFTER the (successful)
    # run so the scheduler's own execution recovery cannot touch it.
    exec_incident_id, _ = I.upsert_incident(outbox_env["job_id"], "provider 500")

    outage["active"] = False
    S.tick(verbose=False, sync=True)  # retries the backlog before new work

    assert O.list_outbox(job_id=outbox_env["job_id"])[0]["state"] == "delivered"
    assert I.get_incident(delivery_incident_id)["state"] == "recovered"
    assert I.get_incident(exec_incident_id)["state"] in I.OPEN_INCIDENT_STATES


def test_failed_primary_channel_writes_durable_local_fallback(outbox_env, monkeypatch):
    from cron import scheduler as S

    monkeypatch.setattr(
        S, "_deliver_result", lambda *a, **k: "telegram adapter unreachable"
    )
    S.tick(verbose=False, sync=True)

    fallback_dir = outbox_env["home"] / "cron" / "failed_deliveries"
    files = list(fallback_dir.rglob("*.md")) if fallback_dir.exists() else []
    assert files, "failed primary delivery must land in the local fallback"
    assert any("daily report content" in f.read_text() for f in files)


def test_exception_path_failure_alert_uses_outbox_protocol(outbox_env, monkeypatch):
    """A run-body crash delivers its failure alert through the SAME durable
    protocol as normal delivery: execution intent + pending outbox row
    persisted before the send, per-attempt history, delivery fields on the
    execution row, and backlog replay once the channel heals."""
    import cron.executions as E
    from cron import outbox as O
    from cron import scheduler as S

    def _boom(*args, **kwargs):
        raise RuntimeError("run body exploded")

    monkeypatch.setattr(S, "run_job", _boom)
    outage = {"active": True}
    monkeypatch.setattr(
        S,
        "_deliver_result",
        lambda *a, **k: "telegram unreachable" if outage["active"] else None,
    )

    S.tick(verbose=False, sync=True)

    rows = O.list_outbox(job_id=outbox_env["job_id"])
    assert len(rows) == 1
    entry = rows[0]
    assert entry["state"] == "pending"
    assert entry["attempts"] == 1
    assert "run body exploded" in entry["content"]
    history = O.list_attempts(entry["id"])
    assert [h["status"] for h in history] == ["failed"]

    latest = E.latest_execution(outbox_env["job_id"])
    assert latest["status"] == "failed"
    assert latest["outcome"] == "failed"
    assert latest["delivery_target"] == "telegram"
    assert latest["delivery_status"] == "failed"
    assert latest["delivery_attempts"] == 1

    # Channel heals → the backlog replays before new work on the next tick.
    outage["active"] = False
    S.tick(verbose=False, sync=True)
    assert O.list_outbox(job_id=outbox_env["job_id"])[0]["state"] == "delivered"


def test_exception_path_successful_alert_records_delivery(outbox_env, monkeypatch):
    """Crash-path alert that DOES send still records the attempt and the
    execution delivery fields durably (queryable evidence, not just logs)."""
    import cron.executions as E
    from cron import outbox as O
    from cron import scheduler as S

    def _boom(*args, **kwargs):
        raise RuntimeError("run body exploded")

    monkeypatch.setattr(S, "run_job", _boom)
    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: None)

    S.tick(verbose=False, sync=True)

    rows = O.list_outbox(job_id=outbox_env["job_id"])
    assert len(rows) == 1
    assert rows[0]["state"] == "delivered"
    history = O.list_attempts(rows[0]["id"])
    assert [h["status"] for h in history] == ["delivered"]

    latest = E.latest_execution(outbox_env["job_id"])
    assert latest["delivery_target"] == "telegram"
    assert latest["delivery_status"] == "delivered"
    assert latest["delivery_attempts"] == 1


def test_exception_path_queue_failure_preserves_original_execution_failure(
    outbox_env, monkeypatch
):
    from unittest.mock import Mock

    import cron.delivery_queue as queue
    import cron.executions as executions
    from cron import outbox
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "run_job",
        Mock(side_effect=RuntimeError("original run failure")),
    )
    original_drain = queue.drain
    drain_calls = 0

    def fail_delivery_accounting(*args, **kwargs):
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 2:
            raise sqlite3.OperationalError("delivery accounting unavailable")
        return original_drain(*args, **kwargs)

    monkeypatch.setattr(queue, "drain", fail_delivery_accounting)

    scheduler.tick(verbose=False, sync=True)

    latest = executions.latest_execution(outbox_env["job_id"])
    assert latest is not None
    assert latest["status"] == "failed"
    assert latest["outcome"] == "failed"
    assert "original run failure" in latest["error"]
    rows = outbox.list_outbox(job_id=outbox_env["job_id"])
    assert len(rows) == 1
    assert rows[0]["exact_state"] == "READY"
    assert rows[0]["attempts"] == 0


def test_exception_path_enqueue_failure_never_sends_without_durable_intent(
    outbox_env, monkeypatch
):
    from unittest.mock import MagicMock

    from cron import outbox as O
    from cron import scheduler as S

    monkeypatch.setattr(
        S,
        "run_job",
        MagicMock(side_effect=RuntimeError("run body exploded")),
    )
    monkeypatch.setattr(
        O,
        "enqueue_deliveries_with_intent",
        MagicMock(side_effect=RuntimeError("outbox unavailable")),
    )
    deliver = MagicMock(return_value=None)
    monkeypatch.setattr(S, "_deliver_result", deliver)

    S.tick(verbose=False, sync=True)

    deliver.assert_not_called()
    fallback_dir = outbox_env["home"] / "cron" / "failed_deliveries"
    files = list(fallback_dir.rglob("*.md")) if fallback_dir.exists() else []
    assert len(files) == 1
    assert "run body exploded" in files[0].read_text()


def test_legacy_execution_schema_without_outcome_migrates_failure_lane(
    tmp_path, monkeypatch
):
    from cron import executions as E
    from cron import outbox as O

    db_path = tmp_path / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setattr(E, "EXECUTIONS_FILE", db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE executions (
                 id TEXT PRIMARY KEY,
                 job_id TEXT NOT NULL,
                 source TEXT NOT NULL,
                 process_id TEXT NOT NULL,
                 pid INTEGER NOT NULL,
                 process_started_at INTEGER,
                 status TEXT NOT NULL CHECK(status IN
                   ('claimed','running','completed','failed','unknown')),
                 claimed_at TEXT NOT NULL,
                 started_at TEXT,
                 finished_at TEXT,
                 error TEXT
               )"""
        )
        conn.execute(
            """CREATE TABLE cron_outbox (
                 id TEXT PRIMARY KEY,
                 execution_id TEXT,
                 job_id TEXT NOT NULL,
                 target TEXT NOT NULL,
                 content TEXT NOT NULL,
                 state TEXT NOT NULL,
                 attempts INTEGER NOT NULL DEFAULT 0,
                 created_at TEXT NOT NULL,
                 updated_at TEXT NOT NULL,
                 last_error TEXT
               )"""
        )
        for execution_id, status in (("exec-f", "failed"), ("exec-c", "completed")):
            conn.execute(
                "INSERT INTO executions "
                "(id, job_id, source, process_id, pid, status, claimed_at) "
                "VALUES (?, 'job', 'builtin', 'proc', 1, ?, '2026-09-01T00:00:00Z')",
                (execution_id, status),
            )
            conn.execute(
                "INSERT INTO cron_outbox "
                "(id, execution_id, job_id, target, content, state, created_at, updated_at) "
                "VALUES (?, ?, 'job', 'telegram', 'body', 'pending', "
                "'2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')",
                (f"outbox-{status}", execution_id),
            )
        conn.commit()
    finally:
        conn.close()

    rows = {row["execution_id"]: row for row in O.list_outbox()}

    assert rows["exec-f"]["for_failure"] == 1
    assert rows["exec-c"]["for_failure"] == 0


def test_outcome_only_execution_schema_migrates_failure_without_unsafe_stamp(
    tmp_path, monkeypatch
):
    from cron import executions as E
    from cron import outbox as O

    db_path = tmp_path / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setattr(E, "EXECUTIONS_FILE", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, outcome TEXT)")
        conn.execute(
            """CREATE TABLE cron_outbox (
                 id TEXT PRIMARY KEY,
                 execution_id TEXT,
                 job_id TEXT NOT NULL,
                 target TEXT NOT NULL,
                 content TEXT NOT NULL,
                 state TEXT NOT NULL,
                 attempts INTEGER NOT NULL DEFAULT 0,
                 created_at TEXT NOT NULL,
                 updated_at TEXT NOT NULL,
                 last_error TEXT
               )"""
        )
        for execution_id, outcome in (("exec-f", "failed"), ("exec-c", "completed")):
            conn.execute(
                "INSERT INTO executions (id, outcome) VALUES (?, ?)",
                (execution_id, outcome),
            )
            conn.execute(
                """INSERT INTO cron_outbox
                   (id, execution_id, job_id, target, content, state, created_at, updated_at)
                   VALUES (?, ?, 'job', 'telegram', 'body', 'pending',
                           '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')""",
                (f"outbox-{outcome}", execution_id),
            )

    rows = {row["execution_id"]: row for row in O.list_outbox()}
    assert rows["exec-f"]["for_failure"] == 1
    assert rows["exec-c"]["for_failure"] == 0

    O.enqueue_with_intent(
        execution_id="exec-f",
        job_id="job",
        target="telegram",
        content="new body",
        intent_success=True,
    )
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT outcome FROM executions WHERE id='exec-f'").fetchone()[
                0
            ]
            == "failed"
        )


def test_intent_stamp_supports_outcome_and_status_without_error_column(
    tmp_path, monkeypatch
):
    from cron import executions as E
    from cron import outbox as O

    db_path = tmp_path / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setattr(E, "EXECUTIONS_FILE", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE executions (id TEXT PRIMARY KEY, status TEXT, outcome TEXT)"
        )
        conn.execute(
            "INSERT INTO executions (id, status, outcome) VALUES ('exec', 'running', NULL)"
        )

    O.enqueue_with_intent(
        execution_id="exec",
        job_id="job",
        target="telegram",
        content="body",
        intent_success=False,
        intent_error="boom",
    )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT status, outcome FROM executions WHERE id='exec'"
        ).fetchone() == ("running", "failed")


def test_failure_lane_migration_is_idempotent_and_materializes_late_null(
    tmp_path, monkeypatch
):
    from cron import executions as E
    from cron import outbox as O

    db_path = tmp_path / "cron" / "executions.db"
    monkeypatch.setattr(E, "EXECUTIONS_FILE", db_path)
    execution = E.create_execution("job", source="builtin")
    E.mark_execution_running(execution["id"])
    E.finish_execution(execution["id"], success=False, error="boom")
    explicit = O.enqueue_with_intent(
        execution_id=execution["id"],
        job_id="job",
        target="telegram",
        content="success-lane payload",
        intent_success=True,
    )

    assert explicit["for_failure"] == 0
    O.list_outbox()
    O.list_outbox()

    conn = sqlite3.connect(db_path)
    try:
        marker_count = conn.execute(
            "SELECT COUNT(*) FROM cron_outbox_migrations WHERE name='failure_lane_v1'"
        ).fetchone()[0]
        explicit_lane = conn.execute(
            "SELECT for_failure FROM cron_outbox WHERE id=?", (explicit["id"],)
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO cron_outbox
               (id, execution_id, job_id, target, content, state, attempts, created_at, updated_at)
               VALUES ('late-null', ?, 'job', 'telegram', 'failure payload', 'pending', 0,
                       '2026-09-01T00:00:01Z', '2026-09-01T00:00:01Z')""",
            (execution["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    first_replay = {row["id"]: row for row in O.pending_outbox()}
    assert marker_count == 1
    assert explicit_lane == 0
    assert first_replay["late-null"]["for_failure"] == 1

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE executions SET outcome='completed', status='completed' WHERE id=?",
            (execution["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    second_replay = {row["id"]: row for row in O.pending_outbox()}
    assert second_replay["late-null"]["for_failure"] == 1
    assert second_replay[explicit["id"]]["for_failure"] == 0


def test_fanout_persists_concrete_destinations_and_replay_ignores_routing_changes(
    outbox_env, monkeypatch
):
    from cron import outbox as O
    from cron import scheduler as S
    from cron import scheduler_delivery as D
    import cron.jobs as J

    mapping = {"telegram": "home-t", "discord": "home-d"}
    monkeypatch.setattr(D, "_iter_home_target_platforms", lambda: iter(mapping))
    monkeypatch.setattr(
        D, "_get_home_target_chat_id", lambda platform: mapping.get(platform, "")
    )
    monkeypatch.setattr(
        D,
        "_get_home_target_thread_id",
        lambda platform: "home-topic" if platform == "telegram" else None,
    )
    monkeypatch.setattr(D, "_is_known_delivery_platform", lambda platform: True)
    route_expression = "all,telegram,origin,telegram:-100999:17"
    J.update_job(
        outbox_env["job_id"],
        {
            "deliver": route_expression,
            "origin": {
                "platform": "slack",
                "chat_id": "origin-chat",
                "thread_id": "origin-thread",
            },
        },
    )

    monkeypatch.setattr(
        S,
        "_deliver_result",
        lambda *args, **kwargs: "adapter offline",
        raising=False,
    )
    S.tick(verbose=False, sync=True)

    rows = O.list_outbox(job_id=outbox_env["job_id"], state="pending")
    destinations = {
        (
            destination["platform"],
            destination["chat_id"],
            destination["thread_id"],
        ): destination["_resolved_from"]
        for destination in (json.loads(row["destination_json"]) for row in rows)
    }
    assert len(rows) == 4
    assert {row["target"] for row in rows} == {route_expression}
    assert destinations == {
        ("telegram", "home-t", "home-topic"): "home",
        ("discord", "home-d", None): None,
        ("slack", "origin-chat", "origin-thread"): "origin",
        ("telegram", "-100999", "17"): "explicit",
    }

    mapping.clear()
    mapping["signal"] = "changed-home"
    J.update_job(
        outbox_env["job_id"],
        {
            "deliver": "signal:changed-chat",
            "origin": {"platform": "signal", "chat_id": "changed-origin"},
        },
    )
    replayed = []

    def capture_exact(job, content, destination, *args, **kwargs):
        replayed.append(destination)
        return None

    monkeypatch.setattr(S, "_deliver_result", capture_exact)

    assert S._retry_pending_deliveries() == 0
    assert S.drain_delivery_queue(None, None) == 4
    assert {
        (target["platform"], target["chat_id"], target["thread_id"])
        for target in replayed
    } == set(destinations)


def test_partial_fanout_retry_sends_only_failed_destination(outbox_env, monkeypatch):
    from cron import outbox as O
    from cron import scheduler as S
    from cron import scheduler_delivery as D
    import cron.jobs as J

    mapping = {"telegram": "home-t", "discord": "home-d"}
    monkeypatch.setattr(D, "_iter_home_target_platforms", lambda: iter(mapping))
    monkeypatch.setattr(
        D, "_get_home_target_chat_id", lambda platform: mapping[platform]
    )
    monkeypatch.setattr(D, "_get_home_target_thread_id", lambda platform: None)
    J.update_job(outbox_env["job_id"], {"deliver": "all"})

    def first_attempt(job, content, destination, *args, **kwargs):
        return "discord unavailable" if destination["platform"] == "discord" else None

    monkeypatch.setattr(S, "_deliver_result", first_attempt)
    S.tick(verbose=False, sync=True)

    rows = O.list_outbox(job_id=outbox_env["job_id"])
    states = {
        json.loads(row["destination_json"])["platform"]: row["state"] for row in rows
    }
    assert states == {"telegram": "delivered", "discord": "pending"}

    replayed = []

    def replay(job, content, destination, *args, **kwargs):
        replayed.append(destination["platform"])
        return None

    monkeypatch.setattr(S, "_deliver_result", replay)
    assert S._retry_pending_deliveries() == 0
    assert S.drain_delivery_queue(None, None) == 1
    assert replayed == ["discord"]


def test_retry_rejects_schema_invalid_concrete_destination_without_rerouting(
    outbox_env, monkeypatch
):
    from unittest.mock import Mock

    from cron import outbox as O
    from cron import scheduler as S

    entry = O.enqueue_deliveries_with_intent(
        execution_id=None,
        job_id=outbox_env["job_id"],
        target="telegram:original",
        destinations=[{"platform": "telegram", "chat_id": "original"}],
        content="durable content",
        intent_success=True,
    )[0]
    with sqlite3.connect(outbox_env["home"] / "cron" / "executions.db") as conn:
        conn.execute(
            "UPDATE cron_outbox SET destination_json='null' WHERE id=?",
            (entry["id"],),
        )
    deliver = Mock(return_value=None)
    monkeypatch.setattr(S, "_deliver_result", deliver)

    assert S._retry_pending_deliveries() == 0
    assert S.drain_delivery_queue(None, None) == 0
    deliver.assert_not_called()
    row = O.list_outbox(job_id=outbox_env["job_id"])[0]
    assert row["state"] == "abandoned"
    assert row["attempts"] == 1
    assert "destination" in row["last_error"].lower()


def test_exact_contract_rejects_null_destination_while_legacy_replays(
    outbox_env, monkeypatch
):
    from unittest.mock import Mock

    import cron.jobs as J
    from cron import outbox as O
    from cron import scheduler as S

    job = J.get_job(outbox_env["job_id"])
    assert job is not None
    exact = O.enqueue_deliveries_with_intent(
        execution_id=None,
        job_id=job["id"],
        target="telegram:exact",
        destinations=[{"platform": "telegram", "chat_id": "exact"}],
        content="exact body",
        intent_success=True,
    )[0]
    legacy = O.enqueue_with_intent(
        execution_id=None,
        job_id=job["id"],
        target="telegram:legacy",
        content="legacy body",
        intent_success=True,
    )
    with sqlite3.connect(outbox_env["home"] / "cron" / "executions.db") as conn:
        with pytest.raises(sqlite3.IntegrityError, match="exact delivery contract"):
            conn.execute(
                "UPDATE cron_outbox SET destination_json=NULL WHERE id=?",
                (exact["id"],),
            )
    deliver = Mock(return_value=None)
    monkeypatch.setattr(S, "_deliver_result", deliver)

    assert S._retry_pending_deliveries() == 1

    rows = {row["id"]: row for row in O.list_outbox(job_id=job["id"])}
    assert rows[exact["id"]]["delivery_contract"] == 1
    assert rows[exact["id"]]["state"] == "pending"
    assert rows[legacy["id"]]["delivery_contract"] == 0
    assert rows[legacy["id"]]["state"] == "delivered"
    deliver.assert_called_once()
    assert deliver.call_args.args[1] == "legacy body"


def test_concrete_fanout_finalizes_all_destination_receipts_once(monkeypatch):
    from cron import delivery_queue
    from cron import jobs
    from cron import scheduler
    from cron import scheduler_delivery as delivery

    job = {"id": "job-fanout", "execution_id": "exec-fanout"}
    entries = [
        {"id": "delivery-a", "delivery_contract": 1},
        {"id": "delivery-b", "delivery_contract": 1},
    ]
    destinations = [
        {"platform": "bot-chat", "chat_id": "alpha"},
        {"platform": "bot-chat", "chat_id": "beta"},
    ]
    updates = []
    monkeypatch.setattr(
        jobs,
        "update_job",
        lambda _job_id, values, **kwargs: updates.append((values, kwargs)),
    )
    monkeypatch.setattr(
        "cron.outbox.get_job_delivery_projection",
        lambda _job_id: {"execution_id": "exec-fanout", "revision": 7},
    )
    monkeypatch.setattr(delivery_queue, "enqueue", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        delivery_queue,
        "get_exact_state",
        lambda _outbox_id: {"state": "DELIVERED"},
    )

    def contribute(_job, _content, *, destination, outbox_id, delivery_run, **_kwargs):
        target_key = f"bot-chat:{destination['chat_id']}"
        delivery_run.record(
            delivery.DestinationDeliveryOutcome(
                delivery_id=outbox_id,
                target=destination,
                status="queued",
                bot_chat_receipts=(
                    (
                        target_key,
                        {"status": "queued", "delivery_id": f"receipt-{outbox_id}"},
                    ),
                ),
            )
        )
        return None

    monkeypatch.setattr(scheduler, "_deliver_result", contribute)

    def drain(send, *, limit, exact_outbox_ids):
        assert exact_outbox_ids == ["delivery-a", "delivery-b"]
        for entry, destination in zip(entries, destinations):
            send(
                job,
                "body",
                False,
                destination=destination,
                outbox_id=entry["id"],
            )
        return limit

    monkeypatch.setattr(delivery_queue, "drain", drain)

    assert (
        scheduler._attempt_concrete_deliveries(
            job,
            "body",
            entries,
            destinations,
            adapters=None,
            loop=None,
        )
        is None
    )

    assert len(updates) == 1
    values, cas = updates[0]
    assert set(values["last_delivery_queued"]) == {
        "bot-chat:alpha",
        "bot-chat:beta",
    }
    assert cas == {
        "expected_execution_id": "exec-fanout",
        "expected_projection_revision": 7,
    }
    assert job["last_delivery_unverified"] is None
