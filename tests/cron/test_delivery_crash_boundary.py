"""Adversarial contracts for the exact cron delivery state machine."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def exact_env(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox

    executions_db = tmp_path / "executions.db"
    legacy_db = tmp_path / "deliveries.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", executions_db)
    monkeypatch.setattr(outbox, "EXECUTIONS_FILE", executions_db)
    monkeypatch.setattr(queue, "DELIVERY_DB", legacy_db)
    queue._ACTIVE_DELIVERIES.clear()
    return executions_db, legacy_db, queue, outbox


def _enqueue_exact(exact_env, *, suffix="one", destinations=None):
    _, _, queue, outbox = exact_env
    destinations = destinations or [{"platform": "telegram", "chat_id": suffix}]
    entries = outbox.enqueue_deliveries_with_intent(
        execution_id=f"exec-{suffix}",
        job_id=f"job-{suffix}",
        target="telegram",
        destinations=destinations,
        content="body",
        intent_success=True,
    )
    for entry, destination in zip(entries, destinations):
        queue.enqueue(
            f"exec-{suffix}",
            {"id": f"job-{suffix}", "execution_id": f"exec-{suffix}"},
            "body",
            destination=destination,
            outbox_id=entry["id"],
        )
    return entries


def test_retryable_exact_row_survives_prune_and_reactivates(exact_env, monkeypatch):
    _, legacy_db, queue, _ = exact_env
    entry = _enqueue_exact(exact_env)[0]
    assert queue.drain(Mock(return_value="temporary outage")) == 1
    monkeypatch.setattr(queue, "MAX_TERMINAL_DELIVERIES", 0)
    queue.enqueue("legacy", {"id": "legacy"}, "legacy")
    assert queue.drain(Mock(return_value=None)) == 1

    retryable = queue.get_exact_state(entry["id"])
    assert retryable is not None
    assert retryable["state"] == "RETRYABLE_FAILED"
    assert queue.reactivate_exact(entry["id"])
    assert queue.get_exact_state(entry["id"])["state"] == "READY"
    with sqlite3.connect(legacy_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM delivery_tombstones WHERE execution_id=?",
                (entry["id"],),
            ).fetchone()[0]
            == 0
        )


def test_two_claimers_have_one_owner_and_terminal_state_cannot_reverse(exact_env):
    _, _, queue, outbox = exact_env
    entry = _enqueue_exact(exact_env)[0]
    first = queue.claim_next()
    assert first is not None
    assert queue.claim_next() is None
    assert queue._finish(
        first["execution_id"],
        generation=first["generation"],
        owner_token=first["owner_token"],
        error=None,
    )
    assert not queue._finish(
        first["execution_id"],
        generation=first["generation"],
        owner_token=first["owner_token"],
        error="late failure",
    )
    settled = outbox.record_attempt(entry["id"], status="failed", error="late failure")
    assert settled is not None
    assert settled["state"] == "delivered"
    assert settled["attempts"] == 1


def test_fanout_publication_atomically_creates_all_queue_owned_siblings(
    exact_env, monkeypatch
):
    from cron import scheduler
    from cron.scheduler_delivery import DestinationDeliveryOutcome

    _, _, queue, outbox = exact_env
    unrelated = _enqueue_exact(exact_env, suffix="older")[0]
    destinations = [
        {"platform": "telegram", "chat_id": "a"},
        {"platform": "telegram", "chat_id": "b"},
    ]
    job = {"id": "job-fanout", "execution_id": "exec-fanout"}
    entries = outbox.enqueue_deliveries_with_intent(
        execution_id=job["execution_id"],
        job_id=job["id"],
        target="all",
        destinations=destinations,
        content="body",
        intent_success=True,
        job=job,
    )
    replayed_publication = outbox.enqueue_deliveries_with_intent(
        execution_id=job["execution_id"],
        job_id=job["id"],
        target="all",
        destinations=destinations,
        content="body",
        intent_success=True,
        job=job,
    )
    assert [entry["id"] for entry in replayed_publication] == [
        entry["id"] for entry in entries
    ]
    sent: list[str] = []

    def send_exact(
        queued_job, _content, *, outbox_id, destination, delivery_run, **_kwargs
    ):
        assert queued_job["id"] == job["id"]
        sent.append(outbox_id)
        delivery_run.record(
            DestinationDeliveryOutcome(outbox_id, destination, "delivered")
        )
        return None

    monkeypatch.setattr(scheduler, "_deliver_result", send_exact)

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
    assert set(sent) == {entry["id"] for entry in entries}
    assert len(sent) == len(entries)
    assert queue.get_exact_state(unrelated["id"])["state"] == "READY"


def test_same_process_stale_claim_requeues_only_after_active_marker_clears(
    exact_env, monkeypatch
):
    executions_db, _, queue, _ = exact_env
    entry = _enqueue_exact(exact_env, suffix="same-process-stale")[0]
    claim = queue.claim_next()
    assert claim is not None
    with sqlite3.connect(executions_db) as conn:
        conn.execute(
            "UPDATE cron_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (entry["id"],),
        )
    monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: True)

    assert queue.recover_abandoned() == 0
    assert queue.get_exact_state(entry["id"])["state"] == "IN_FLIGHT"

    queue._ACTIVE_DELIVERIES.discard(entry["id"])
    assert queue.recover_abandoned() == 1
    assert queue.get_exact_state(entry["id"])["state"] == "READY"


def test_live_owner_lease_expiry_is_not_reclaimed_but_dead_owner_becomes_ready(
    exact_env, monkeypatch
):
    executions_db, _, queue, _ = exact_env
    entry = _enqueue_exact(exact_env)[0]
    claim = queue.claim_next()
    assert claim is not None
    queue._ACTIVE_DELIVERIES.discard(claim["execution_id"])
    with sqlite3.connect(executions_db) as conn:
        conn.execute(
            "UPDATE cron_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (entry["id"],),
        )
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement")
    monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: True)
    assert queue.recover_abandoned() == 0
    assert queue.get_exact_state(entry["id"])["state"] == "IN_FLIGHT"

    monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: False)
    assert queue.recover_abandoned() == 1
    assert queue.get_exact_state(entry["id"])["state"] == "READY"
    assert queue.claim_next() is not None


def test_continuation_thread_update_is_canonical_across_retry(exact_env, monkeypatch):
    from types import SimpleNamespace

    from cron import scheduler_delivery as delivery

    _, _, queue, outbox = exact_env
    entry = _enqueue_exact(exact_env)[0]
    adapter_sends: list[str] = []

    def prepare(_job, target, **_kwargs):
        opened = None if target.get("thread_id") else "opened-topic"
        return SimpleNamespace(
            opened_thread_id=opened,
            where="telegram:one",
            live_adapter_ready=True,
        )

    monkeypatch.setattr(delivery, "_prepare_target_delivery", prepare)
    monkeypatch.setattr(
        delivery,
        "_deliver_via_live_adapter",
        lambda *_args, **_kwargs: adapter_sends.append("sent") or True,
    )

    def send(job, content, _for_failure, **kwargs):
        return delivery._deliver_result(job, content, **kwargs)

    assert queue.drain(send, limit=1) == 1
    prepared = outbox.get_outbox(entry["id"])
    assert prepared is not None
    assert prepared["exact_state"] == "READY"
    assert json.loads(prepared["destination_json"])["thread_id"] == "opened-topic"
    assert adapter_sends == []

    assert queue.drain(send, limit=1) == 1
    assert queue.get_exact_state(entry["id"])["state"] == "DELIVERED"
    assert adapter_sends == ["sent"]


def test_sigkill_after_transport_accept_redelivers_at_least_once(exact_env, tmp_path):
    executions_db, legacy_db, queue, outbox = exact_env
    entry = _enqueue_exact(exact_env, suffix="crash")[0]
    accepted = tmp_path / "accepted.txt"
    code = r"""
import sys, time
from pathlib import Path
from cron import delivery_queue as q, executions, outbox
executions.EXECUTIONS_FILE = Path(sys.argv[1])
outbox.EXECUTIONS_FILE = Path(sys.argv[1])
q.DELIVERY_DB = Path(sys.argv[2])
accepted = Path(sys.argv[3])
def send(*args, **kwargs):
    with accepted.open("a", encoding="utf-8") as fh:
        fh.write("accepted\n")
        fh.flush()
    print("ACCEPTED", flush=True)
    time.sleep(60)
q.drain(send, limit=1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(executions_db), str(legacy_db), str(accepted)],
        cwd=Path(__file__).parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ACCEPTED"
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL

    assert queue.recover_abandoned() == 1
    assert queue.get_exact_state(entry["id"])["state"] == "READY"

    def resend(*_args, **_kwargs):
        with accepted.open("a", encoding="utf-8") as fh:
            fh.write("accepted\n")
        return None

    assert queue.drain(resend) == 1
    assert accepted.read_text(encoding="utf-8").splitlines() == [
        "accepted",
        "accepted",
    ]
    settled = outbox.get_outbox(entry["id"])
    assert settled is not None
    assert settled["state"] == "delivered"
    assert settled["attempts"] == 1


def test_late_success_corrects_delivery_projection_without_reversing_execution_failure(
    exact_env,
):
    from cron import executions

    _, _, queue, outbox = exact_env
    execution = executions.create_execution("job-late", source="builtin")
    executions.mark_execution_running(execution["id"])
    executions.finish_execution(
        execution["id"], success=False, error="run failed", delivery_outcome="failed"
    )
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id=execution["id"],
        job_id="job-late",
        target="telegram",
        destinations=[{"platform": "telegram", "chat_id": "late"}],
        content="failure alert",
        intent_success=False,
        intent_error="run failed",
        job={"id": "job-late", "execution_id": execution["id"]},
    )[0]
    claim = queue.claim_next()
    assert claim is not None
    assert queue._finish(
        claim["execution_id"],
        generation=claim["generation"],
        owner_token=claim["owner_token"],
        error=None,
    )

    refreshed = executions.get_execution(execution["id"])
    projection = outbox.get_job_delivery_projection("job-late")
    assert refreshed is not None
    assert projection is not None
    assert refreshed["status"] == "failed"
    assert refreshed["outcome"] == "failed"
    assert refreshed["delivery_outcome"] == "delivered"
    assert refreshed["delivery_status"] == "delivered"
    assert projection["execution_id"] == execution["id"]
    assert projection["last_status"] == "failed"
    assert projection["last_delivery_error"] is None


def test_non_null_missing_link_fails_closed_while_null_link_is_legacy(exact_env):
    _, _, queue, _ = exact_env
    with pytest.raises(ValueError, match="outbox"):
        queue.enqueue(
            "missing",
            {"id": "job"},
            "body",
            destination={"platform": "telegram", "chat_id": "chat"},
            outbox_id="missing",
        )

    queue.enqueue("legacy", {"id": "legacy"}, "body", outbox_id=None)
    send = Mock(return_value=None)
    assert queue.drain(send) == 1
    send.assert_called_once()


def test_current_profile_bot_chat_destination_round_trips_without_loosening_empty_ids(
    exact_env,
):
    _, _, _, outbox = exact_env
    current_profile = {
        "platform": "bot-chat",
        "chat_id": "",
        "thread_id": None,
    }

    encoded = outbox.canonical_destination_json(current_profile)

    assert outbox.decode_persisted_destination(encoded) == {
        **current_profile,
        "_resolved_from": None,
    }
    for invalid in (
        {"platform": "telegram", "chat_id": "", "thread_id": None},
        {"platform": "bot-chat", "chat_id": "", "thread_id": "thread"},
    ):
        with pytest.raises(ValueError, match="chat_id"):
            outbox.decode_persisted_destination(json.dumps(invalid))


@pytest.mark.parametrize(
    ("receipt_status", "receipt_reason", "expected_state", "expected_exact"),
    [
        ("failed", "provider_rate_limit", "pending", "RETRYABLE_FAILED"),
        ("failed", "missing_config", "abandoned", "DEAD"),
        ("cancelled", "cancelled", "abandoned", "DEAD"),
        ("ambiguous", "unknown", "abandoned", "UNKNOWN"),
    ],
)
def test_admitted_negative_receipts_reconcile_to_deterministic_outcomes(
    exact_env,
    monkeypatch,
    receipt_status,
    receipt_reason,
    expected_state,
    expected_exact,
):
    from tools import bot_live_delivery

    executions_db, _, queue, outbox = exact_env
    entry = _enqueue_exact(
        exact_env,
        suffix=f"receipt-{receipt_status}-{receipt_reason}",
        destinations=[{"platform": "bot-chat", "chat_id": "profile"}],
    )[0]
    claim = queue.claim_next()
    assert claim is not None
    assert queue._finish(
        claim["execution_id"],
        generation=claim["generation"],
        owner_token=claim["owner_token"],
        error=None,
        transport_status="queued",
        receipt_id="receipt-negative",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda _profile: executions_db.parent,
    )
    monkeypatch.setattr(
        bot_live_delivery,
        "read_delivery_result",
        lambda _home, _receipt_id: {
            "delivery_id": "receipt-negative",
            "status": receipt_status,
            "reason": receipt_reason,
            "error": "terminal receipt error",
        },
    )

    assert queue.reconcile_admitted() == 1

    reconciled = outbox.get_outbox(entry["id"])
    assert reconciled is not None
    assert reconciled["state"] == expected_state
    assert reconciled["exact_state"] == expected_exact
    attempt = outbox.list_attempts(entry["id"])[0]
    assert attempt["status"] == (
        "unknown" if receipt_status == "ambiguous" else "failed"
    )
    from cron import incidents

    assert incidents.count_incidents() == 1
    projection = outbox.get_job_delivery_projection(reconciled["job_id"])
    assert projection is not None
    assert bool(projection["finalized"]) is (expected_state == "abandoned")


def test_bot_admission_is_nonterminal_until_receipt_reconciliation(
    exact_env, monkeypatch
):
    from tools import bot_live_delivery

    executions_db, _, queue, outbox = exact_env
    entry = _enqueue_exact(
        exact_env,
        destinations=[{"platform": "bot-chat", "chat_id": "profile"}],
    )[0]
    claim = queue.claim_next()
    assert claim is not None
    assert queue._finish(
        claim["execution_id"],
        generation=claim["generation"],
        owner_token=claim["owner_token"],
        error=None,
        transport_status="queued",
        receipt_id="receipt-1",
    )
    admitted = outbox.get_outbox(entry["id"])
    assert admitted["state"] == "admitted"
    assert admitted["exact_state"] == "IN_FLIGHT"

    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda _profile: executions_db.parent,
    )
    monkeypatch.setattr(
        bot_live_delivery,
        "read_delivery_result",
        lambda _home, receipt_id: {
            "delivery_id": receipt_id,
            "status": "settled",
        },
    )
    send = Mock()
    assert queue.drain(send) == 0
    send.assert_not_called()
    assert outbox.get_outbox(entry["id"])["state"] == "delivered"


def test_receipt_dedup_retains_maximum_status_order(monkeypatch):
    from cron import scheduler_delivery as delivery

    updates = []
    monkeypatch.setattr(
        "cron.jobs.update_job",
        lambda _job_id, values, **_kwargs: updates.append(values),
    )
    job = {"id": "job", "execution_id": "exec"}
    run = delivery._begin_delivery_run(job)
    for delivery_id, status in (
        ("a", "claimed"),
        ("b", "settled"),
        ("c", "queued"),
    ):
        run.record(
            delivery.DestinationDeliveryOutcome(
                delivery_id=delivery_id,
                target={"platform": "bot-chat", "chat_id": "profile"},
                status=status,
                bot_chat_receipts=(
                    (
                        "bot-chat:profile",
                        {"delivery_id": "same-receipt", "status": status},
                    ),
                ),
            )
        )
    run.finalize()
    assert job["_bot_chat_delivery_receipts"]["bot-chat:profile"]["status"] == "settled"


def test_integer_and_string_thread_aliases_deduplicate(monkeypatch):
    from cron import scheduler_delivery as delivery
    from tools import send_message_tool

    monkeypatch.setattr(delivery, "_is_known_delivery_platform", lambda _platform: True)
    monkeypatch.setattr(
        send_message_tool, "prepare_send_message_platforms", lambda: None
    )
    monkeypatch.setattr(
        send_message_tool,
        "resolve_send_target",
        lambda _platform, _target, **_kwargs: ("chat", "17", None),
    )
    job = {
        "id": "job",
        "deliver": "origin,telegram:chat:17",
        "origin": {"platform": "telegram", "chat_id": "chat", "thread_id": 17},
    }
    targets = delivery._resolve_delivery_targets(job)
    assert len(targets) == 1
    assert targets[0]["thread_id"] == "17"


def test_attachment_snapshot_rejects_denied_source_before_read(
    exact_env, tmp_path, monkeypatch
):
    _, _, _, outbox = exact_env
    source = tmp_path / "denied" / "secret.txt"
    source.parent.mkdir()
    source.write_bytes(b"secret")
    monkeypatch.delenv("HERMES_MEDIA_DELIVERY_STRICT", raising=False)
    monkeypatch.delenv("HERMES_MEDIA_TRUST_RECENT_FILES", raising=False)
    monkeypatch.delenv("HERMES_MEDIA_ALLOW_DIRS", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "gateway": {
                "strict": True,
                "trust_recent_files": False,
                "media_delivery_allow_dirs": [str(tmp_path / "allowed")],
            }
        },
    )
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == source:
            raise AssertionError(
                "denied MEDIA source was read before policy validation"
            )
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-denied-media",
        job_id="job-denied-media",
        target="telegram",
        destinations=[{"platform": "telegram", "chat_id": "media"}],
        content=f"report\nMEDIA:{source}",
        intent_success=True,
        job={"id": "job-denied-media", "execution_id": "exec-denied-media"},
    )[0]

    with sqlite3.connect(exact_env[0]) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cron_outbox_attachments WHERE outbox_id=?",
                (entry["id"],),
            ).fetchone()[0]
            == 0
        )


def test_attachment_snapshot_survives_source_mutation_and_deleted_materialization_retries(
    exact_env, tmp_path, monkeypatch
):
    _, _, queue, outbox = exact_env
    source = tmp_path / "report.txt"
    source.write_bytes(b"enqueue-time-bytes")
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    content = f"report\nMEDIA:{source}"
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-media",
        job_id="job-media",
        target="telegram",
        destinations=[{"platform": "telegram", "chat_id": "media"}],
        content=content,
        intent_success=True,
        job={"id": "job-media", "execution_id": "exec-media"},
    )[0]
    source.write_bytes(b"mutated")
    source.unlink()

    materialized = outbox.materialize_exact_content(entry["id"], tmp_path / "retry")
    snapshot_path = Path(materialized.split("MEDIA:", 1)[1].strip())
    assert snapshot_path.read_bytes() == b"enqueue-time-bytes"

    send = Mock()
    monkeypatch.setattr(
        outbox,
        "materialize_exact_content",
        Mock(side_effect=OSError("snapshot volume unavailable")),
    )
    with pytest.raises(OSError, match="snapshot volume unavailable"):
        queue.drain(send, exact_outbox_ids=[entry["id"]])
    send.assert_not_called()
    assert queue.get_exact_state(entry["id"])["state"] == "RETRYABLE_FAILED"


def test_stale_delivery_without_projection_revision_cannot_write_job_fields(
    exact_env, monkeypatch
):
    from cron import scheduler_delivery as delivery

    _, _, _, outbox = exact_env
    outbox.begin_job_delivery_projection("job-cas", "exec-current")
    stale_job = {"id": "job-cas", "execution_id": "exec-stale"}
    writes = []
    monkeypatch.setattr(
        "cron.jobs.update_job",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    run = delivery._begin_delivery_run(stale_job)
    run.record(
        delivery.DestinationDeliveryOutcome(
            "delivery-stale",
            {"platform": "telegram", "chat_id": "chat"},
            "delivered",
            unverified_targets=("telegram:chat",),
        )
    )
    run.finalize()

    assert "_delivery_projection_revision" not in stale_job
    assert writes == []


def test_run_projection_finalizes_once_and_stale_execution_cas_fails(
    exact_env, monkeypatch
):
    _, _, _, outbox = exact_env
    first = outbox.begin_job_delivery_projection("job", "exec-old")
    newer = outbox.begin_job_delivery_projection("job", "exec-new")
    assert newer["revision"] > first["revision"]
    assert not outbox.finalize_job_delivery_projection(
        "job",
        execution_id="exec-old",
        expected_revision=first["revision"],
        last_status="completed",
        last_delivery_error=None,
    )

    from cron import scheduler_delivery as delivery

    calls = []
    monkeypatch.setattr(
        delivery,
        "_record_delivery_verification",
        lambda job, unverified, **kwargs: calls.append((job["id"], tuple(unverified))),
    )
    run = delivery._begin_delivery_run({"id": "job", "execution_id": "exec-new"})
    run.record(delivery.DestinationDeliveryOutcome("a", {}, "delivered"))
    run.record(delivery.DestinationDeliveryOutcome("b", {}, "delivered"))
    run.finalize()
    run.finalize()
    assert calls == [("job", ())]


def test_lookup_exception_after_successful_direct_path_cannot_duplicate(
    exact_env, monkeypatch
):
    from cron import scheduler

    _, _, queue, outbox = exact_env
    destination = {"platform": "telegram", "chat_id": "once"}
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-once",
        job_id="job-once",
        target="telegram",
        destinations=[destination],
        content="body",
        intent_success=True,
        job={"id": "job-once", "execution_id": "exec-once"},
    )[0]
    sends = []
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda *args, **kwargs: sends.append(kwargs.get("outbox_id")) or None,
    )
    lookup = Mock(side_effect=OSError("lookup failed"))
    monkeypatch.setattr(queue, "get_exact_state", lookup)
    scheduler._attempt_concrete_deliveries(
        {"id": "job-once", "execution_id": "exec-once"},
        "body",
        [entry],
        [destination],
        adapters=None,
        loop=None,
    )
    lookup.assert_called_once_with(entry["id"])
    assert outbox.get_outbox(entry["id"])["exact_state"] == "DELIVERED"
    monkeypatch.setattr("cron.jobs.get_job", lambda _job_id: {"id": "job-once"})
    scheduler._retry_pending_deliveries()
    assert sends == [entry["id"]]


def test_deleted_linked_row_cannot_starve_later_work(exact_env):
    executions_db, legacy_db, queue, outbox = exact_env
    entry = _enqueue_exact(exact_env, suffix="orphan")[0]
    queue.enqueue("later", {"id": "later"}, "later")
    with sqlite3.connect(executions_db) as conn:
        conn.execute("DELETE FROM cron_outbox WHERE id=?", (entry["id"],))
    send = Mock(return_value=None)

    assert queue.drain(send, limit=1) == 1
    send.assert_called_once_with({"id": "later"}, "later", False)
    assert queue.get_status("later")["status"] == "delivered"
    assert queue.get_exact_state(entry["id"]) is None
    assert outbox.get_outbox(entry["id"]) is None


def test_max_attempt_abandonment_cannot_reactivate(exact_env):
    executions_db, _, queue, outbox = exact_env
    entry = _enqueue_exact(exact_env, suffix="cap")[0]
    with sqlite3.connect(executions_db) as conn:
        conn.execute(
            "UPDATE cron_outbox SET attempts=? WHERE id=?",
            (outbox.MAX_OUTBOX_ATTEMPTS - 1, entry["id"]),
        )
    claim = queue.claim_next()
    assert claim is not None
    assert queue._finish(
        claim["execution_id"],
        generation=claim["generation"],
        owner_token=claim["owner_token"],
        error="last failure",
    )
    assert outbox.get_outbox(entry["id"])["state"] == "abandoned"
    assert queue.get_exact_state(entry["id"])["state"] == "DEAD"
    assert not queue.reactivate_exact(entry["id"])
    assert queue.claim_next() is None


def test_contract_downgrade_corruption_fails_closed(exact_env):
    executions_db, _, queue, outbox = exact_env
    entry = _enqueue_exact(exact_env, suffix="contract")[0]
    with sqlite3.connect(executions_db) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        with pytest.raises(sqlite3.IntegrityError, match="invalid exact"):
            conn.execute(
                "UPDATE cron_outbox SET delivery_contract=0 WHERE id=?", (entry["id"],)
            )
    send = Mock(return_value=None)

    assert queue.drain(send) == 1
    send.assert_called_once()
    row = outbox.get_outbox(entry["id"])
    assert row["delivery_contract"] == 1
    assert row["exact_state"] == "DELIVERED"


def test_accumulator_rejects_cross_job_and_post_finalize(monkeypatch):
    from cron import scheduler_delivery as delivery

    monkeypatch.setattr(
        delivery, "_record_delivery_verification", lambda *_a, **_k: None
    )
    run = delivery._begin_delivery_run({"id": "job-a", "execution_id": "exec-a"})
    with pytest.raises(ValueError, match="job"):
        delivery._deliver_result(
            {"id": "job-b", "execution_id": "exec-b"},
            "body",
            destination={"platform": "bot-chat", "chat_id": "profile"},
            outbox_id="delivery-b",
            delivery_run=run,
        )
    run.finalize()
    with pytest.raises(RuntimeError, match="final"):
        run.record(delivery.DestinationDeliveryOutcome("late", {}, "delivered"))
