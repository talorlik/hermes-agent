"""Durable at-most-once delivery handoff for restart-safe cron workers."""

from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import Mock

import pytest


def test_pending_delivery_is_claimed_and_sent_once(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    send = Mock(return_value=None)

    assert queue.drain(send) == 1
    assert queue.drain(send) == 0
    send.assert_called_once_with({"id": "job-1"}, "brief", False)
    status = queue.get_status("exec-1")
    assert status["status"] == "delivered"
    assert status["job_json"] == "{}"
    assert status["content"] == ""


def test_exact_target_payload_survives_queue_and_gateway_drain(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox
    from cron import scheduler

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    destination = {
        "platform": "telegram",
        "chat_id": "resolved-chat",
        "thread_id": "resolved-thread",
        "_resolved_from": "explicit",
    }
    first = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-1",
        job_id="job-1",
        target="telegram:resolved-chat:resolved-thread",
        destinations=[destination],
        content="brief",
        intent_success=True,
    )[0]
    queue.enqueue(
        "exec-1",
        {"id": "job-1", "execution_id": "exec-1"},
        "brief",
        destination=destination,
        outbox_id=first["id"],
    )
    observed = []

    def deliver(*args, **kwargs):
        observed.append((args, kwargs))
        return None

    monkeypatch.setattr(scheduler, "_deliver_result", deliver)
    adapters = object()
    loop = object()
    configured = queue.get_exact_state(first["id"])
    assert configured is not None
    expected_attempt_id = f"{first['id']}:{int(configured['generation']) + 1}"

    assert scheduler.drain_delivery_queue(adapters, loop) == 1
    assert observed == [
        (
            (
                {
                    "id": "job-1",
                    "execution_id": "exec-1",
                    "_exact_delivery_attempt_id": expected_attempt_id,
                },
                "brief",
            ),
            {
                "adapters": adapters,
                "loop": loop,
                "for_failure": False,
                "destination": destination,
                "outbox_id": first["id"],
            },
        )
    ]
    first_status = queue.get_status("exec-1", outbox_id=first["id"])
    assert first_status is not None and first_status["status"] == "delivered"
    assert first["id"] not in queue._ACTIVE_DELIVERIES

    second = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-2",
        job_id="job-1",
        target="telegram:resolved-chat:resolved-thread",
        destinations=[destination],
        content="second fanout brief",
        intent_success=True,
    )[0]
    pending_error = queue.enqueue_and_wait(
        "exec-2",
        {"id": "job-1", "execution_id": "exec-2"},
        "second fanout brief",
        timeout=0,
        destination=destination,
        outbox_id=second["id"],
    )
    assert pending_error is not None
    assert "queued" in pending_error
    second_status = queue.get_status("exec-2", outbox_id=second["id"])
    assert second_status is not None and second_status["status"] == "pending"


def test_external_worker_initial_handoff_leaves_retry_owned_by_queue(monkeypatch):
    from cron import delivery_queue as queue
    from cron import outbox
    from cron import scheduler
    from cron import scheduler_delivery

    job = {"id": "job-1", "execution_id": "exec-1", "deliver": "changed-route"}
    destination = {
        "platform": "telegram",
        "chat_id": "resolved-chat",
        "thread_id": "resolved-thread",
        "_resolved_from": "explicit",
    }
    monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", "exec-1")
    monkeypatch.setattr(
        scheduler_delivery,
        "_resolve_delivery_targets",
        lambda *args, **kwargs: pytest.fail("exact delivery re-resolved its route"),
    )
    handoffs = []

    def enqueue_and_wait(execution_id, queued_job, content, **kwargs):
        handoffs.append((execution_id, queued_job, content, kwargs))
        return None

    monkeypatch.setattr(queue, "enqueue_and_wait", enqueue_and_wait)

    assert (
        scheduler_delivery._deliver_result(
            job,
            "initial",
            destination=destination,
            outbox_id="outbox-initial",
        )
        is None
    )

    monkeypatch.setattr(
        outbox,
        "pending_outbox",
        lambda limit: [
            {
                "id": "outbox-retry",
                "job_id": "job-1",
                "content": "retry",
                "for_failure": 0,
                "delivery_contract": 1,
                "destination_json": json.dumps(destination),
            }
        ],
    )
    attempts = []
    monkeypatch.setattr(
        outbox,
        "record_attempt",
        lambda *args, **kwargs: attempts.append((args, kwargs)),
    )
    reactivated = []
    monkeypatch.setattr(
        queue,
        "get_exact_state",
        lambda _outbox_id: {"state": "RETRYABLE_FAILED"},
    )
    monkeypatch.setattr(
        queue,
        "reactivate_exact",
        lambda outbox_id: reactivated.append(outbox_id) or True,
    )

    assert scheduler._retry_pending_deliveries() == 0
    assert [handoff[0] for handoff in handoffs] == ["exec-1"]
    assert [handoff[2] for handoff in handoffs] == ["initial"]
    assert [handoff[3]["destination"] for handoff in handoffs] == [destination]
    assert [handoff[3]["outbox_id"] for handoff in handoffs] == ["outbox-initial"]
    assert reactivated == ["outbox-retry"]
    assert attempts == []


def test_exact_timeout_late_success_is_accounted_once_without_resend(
    tmp_path, monkeypatch
):
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox
    from cron import scheduler
    from cron import scheduler_delivery

    executions_db = tmp_path / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", executions_db)
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    destination = {"platform": "telegram", "chat_id": "exact-chat"}
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-late",
        job_id="job-late",
        target="telegram:exact-chat",
        destinations=[destination],
        content="body",
        intent_success=True,
    )[0]
    job = {"id": "job-late", "execution_id": "exec-late"}
    monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", "exec-late")
    original_wait = queue.enqueue_and_wait
    monkeypatch.setattr(
        queue,
        "enqueue_and_wait",
        lambda *args, **kwargs: original_wait(*args, timeout=0, **kwargs),
    )

    timeout_error = scheduler_delivery._deliver_result(
        job,
        "body",
        destination=destination,
        outbox_id=entry["id"],
    )

    assert timeout_error is not None
    assert outbox.list_outbox()[0]["attempts"] == 0
    send = Mock(return_value=None)
    assert queue.drain(send) == 1
    settled = outbox.list_outbox()[0]
    assert settled["state"] == "delivered"
    assert settled["attempts"] == 1
    assert settled["accounted_queue_generation"] == settled["generation"]
    assert queue.drain(send) == 0
    assert scheduler._retry_pending_deliveries() == 0
    send.assert_called_once()


def test_exact_queue_hash_corruption_dead_accounts_once_without_sender(
    tmp_path, monkeypatch
):
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-corrupt",
        job_id="job-corrupt",
        target="telegram:original",
        destinations=[{"platform": "telegram", "chat_id": "original"}],
        content="body",
        intent_success=True,
    )[0]
    queue.enqueue(
        "exec-corrupt",
        {"id": "job-corrupt", "execution_id": "exec-corrupt"},
        "body",
        destination={"platform": "telegram", "chat_id": "original"},
        outbox_id=entry["id"],
    )
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE cron_outbox SET destination_json=? WHERE id=?",
            (
                json.dumps({"platform": "telegram", "chat_id": "redirected"}),
                entry["id"],
            ),
        )
    send = Mock(return_value=None)

    assert queue.drain(send) == 0

    send.assert_not_called()
    queued = queue.get_status("exec-corrupt", outbox_id=entry["id"])
    assert queued is not None
    assert queued["state"] == "DEAD"
    settled = outbox.get_outbox(entry["id"])
    assert settled is not None
    assert settled["state"] == "abandoned"
    assert settled["attempts"] == 1
    assert len(outbox.list_attempts(entry["id"])) == 1


def test_non_null_missing_link_fails_closed_and_omitted_link_stays_legacy(
    tmp_path, monkeypatch
):
    from cron import delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    with pytest.raises(ValueError, match="outbox"):
        queue.enqueue(
            "missing-exact",
            {"id": "legacy-job"},
            "body",
            destination={"platform": "telegram", "chat_id": "legacy-chat"},
            outbox_id="missing-legacy-outbox",
        )

    queue.enqueue("exec-legacy", {"id": "legacy-job"}, "body")
    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed["delivery_contract"] == 0
    queue._ACTIVE_DELIVERIES.discard(claimed["execution_id"])
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-owner")
    monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: False)

    assert queue.recover_abandoned() == 1
    recovered = queue.get_status("exec-legacy")
    assert recovered is not None
    assert recovered["status"] == "unknown"
    assert recovered["state"] is None


def test_dead_exact_owner_requeues_and_rejects_stale_completion(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-reclaim",
        job_id="job-reclaim",
        target="telegram:chat",
        destinations=[{"platform": "telegram", "chat_id": "chat"}],
        content="body",
        intent_success=True,
    )[0]
    queue.enqueue(
        "exec-reclaim",
        {"id": "job-reclaim"},
        "body",
        destination={"platform": "telegram", "chat_id": "chat"},
        outbox_id=entry["id"],
    )
    first = queue.claim_next()
    assert first is not None
    queue._ACTIVE_DELIVERIES.discard(first["execution_id"])
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-owner")
    monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: False)

    assert queue.recover_abandoned() == 1
    replacement = queue.claim_next()
    assert replacement is not None
    assert replacement["generation"] > first["generation"]
    assert not queue._finish(
        first["execution_id"],
        generation=first["generation"],
        owner_token=first["owner_token"],
        error=None,
    )
    assert queue._finish(
        replacement["execution_id"],
        generation=replacement["generation"],
        owner_token=replacement["owner_token"],
        error=None,
    )
    settled = outbox.get_outbox(entry["id"])
    assert settled is not None
    assert settled["state"] == "delivered"
    assert settled["exact_state"] == "DELIVERED"
    assert settled["attempts"] == 1


def test_retryable_exact_failure_reuses_single_executions_row(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox

    executions_db = tmp_path / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", executions_db)
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    destination = {"platform": "telegram", "chat_id": "chat"}
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-retry",
        job_id="job-retry",
        target="telegram:chat",
        destinations=[destination],
        content="body",
        intent_success=True,
    )[0]
    queue.enqueue(
        "exec-retry",
        {"id": "job-retry"},
        "body",
        destination=destination,
        outbox_id=entry["id"],
    )

    assert queue.drain(Mock(return_value="adapter unavailable")) == 1
    assert [row["id"] for row in outbox.pending_outbox()] == [entry["id"]]
    assert queue.reactivate_exact(entry["id"])
    assert queue.drain(Mock(return_value=None)) == 1
    with sqlite3.connect(executions_db) as conn:
        queued = conn.execute(
            "SELECT COUNT(*), MAX(exact_state) FROM cron_outbox WHERE id=?",
            (entry["id"],),
        ).fetchone()
    assert queued == (1, "DELIVERED")
    settled = outbox.get_outbox(entry["id"])
    assert settled is not None
    assert settled["state"] == "delivered"
    assert settled["attempts"] == 2


def test_terminal_exact_row_is_not_replayed_by_scheduler_retry(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox
    from cron import scheduler

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    destination = {"platform": "telegram", "chat_id": "chat"}
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="exec-reconcile",
        job_id="job-reconcile",
        target="telegram:chat",
        destinations=[destination],
        content="body",
        intent_success=True,
    )[0]
    queue.enqueue(
        "exec-reconcile",
        {"id": "job-reconcile", "execution_id": "exec-reconcile"},
        "body",
        destination=destination,
        outbox_id=entry["id"],
    )
    claimed = queue.claim_next()
    assert claimed is not None
    assert queue._finish(
        claimed["execution_id"],
        generation=claimed["generation"],
        owner_token=claimed["owner_token"],
        error=None,
    )
    deliver = Mock(side_effect=AssertionError("terminal exact row was resent"))
    monkeypatch.setattr(scheduler, "_deliver_result", deliver)

    assert scheduler._retry_pending_deliveries() == 0

    deliver.assert_not_called()
    settled = outbox.get_outbox(entry["id"])
    assert settled is not None
    assert settled["state"] == "delivered"
    assert settled["attempts"] == 1
    assert settled["accounted_queue_generation"] == claimed["generation"]


def test_terminal_delivery_retention_is_bounded(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(queue, "MAX_TERMINAL_DELIVERIES", 2, raising=False)
    for index in range(4):
        execution_id = f"exec-{index}"
        queue.enqueue(execution_id, {"id": f"job-{index}"}, f"brief-{index}")
        assert queue.claim_next()["execution_id"] == execution_id
        assert queue._finish(execution_id, error=None)

    # Pruning may discard verbose outcome rows, but never the durable
    # idempotency tombstone for an execution that could be replayed later.
    pruned = queue.get_status("exec-0")
    assert pruned is not None
    assert pruned["status"] == "delivered"
    assert queue.get_status("exec-1")["status"] == "delivered"
    assert queue.get_status("exec-2")["status"] == "delivered"
    assert queue.get_status("exec-3")["status"] == "delivered"

    # Pruning may discard verbose outcome rows, but never the durable
    # idempotency tombstone for an execution that could be replayed later.
    queue.enqueue("exec-0", {"id": "job-replayed"}, "duplicate brief")
    send = Mock(return_value=None)
    assert queue.drain(send) == 0
    send.assert_not_called()


@pytest.mark.parametrize("corrupt_column", ["job_json", "destination_json"])
def test_malformed_payload_is_failed_without_blocking_later_delivery(
    tmp_path, monkeypatch, corrupt_column
):
    import cron.delivery_queue as queue
    from cron import executions, outbox

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    malformed_id = f"malformed-{corrupt_column}"
    destination = {"platform": "telegram", "chat_id": "resolved-chat"}
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id=malformed_id,
        job_id="malformed-job",
        target="telegram:resolved-chat",
        destinations=[destination],
        content="sensitive content",
        intent_success=True,
    )[0]
    queue.enqueue(
        malformed_id,
        {"id": "malformed-job"},
        "sensitive content",
        destination=destination,
        outbox_id=entry["id"],
    )
    corrupt_queries = {
        "job_json": "UPDATE cron_outbox SET job_json=? WHERE id=?",
        "destination_json": "UPDATE cron_outbox SET destination_json=? WHERE id=?",
    }
    with sqlite3.connect(tmp_path / "executions.db") as conn:
        conn.execute(
            corrupt_queries[corrupt_column],
            ('{"token":"DO_NOT_RETAIN"', entry["id"]),
        )
    queue.enqueue("valid-after-malformed", {"id": "valid-job"}, "valid content")
    send = Mock(return_value=None)

    assert queue.drain(send) == 1
    send.assert_called_once_with({"id": "valid-job"}, "valid content", False)
    malformed = queue.get_status(malformed_id, outbox_id=entry["id"])
    assert malformed is not None
    assert malformed["status"] == "failed"
    assert "JSONDecodeError" in malformed["error"]
    assert "DO_NOT_RETAIN" not in malformed["error"]
    assert malformed["state"] == "DEAD"
    assert malformed_id not in queue._ACTIVE_DELIVERIES
    assert queue.get_status("valid-after-malformed")["status"] == "delivered"


def test_only_malformed_rows_are_cleaned_without_consuming_drain_limit(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    active_before = set(queue._ACTIVE_DELIVERIES)
    for index in range(3):
        execution_id = f"malformed-{index}"
        queue.enqueue(execution_id, {"id": execution_id}, "sensitive content")
        with sqlite3.connect(queue.DELIVERY_DB) as conn:
            conn.execute(
                "UPDATE deliveries SET job_json=? WHERE execution_id=?",
                ('{"secret":"DO_NOT_RETAIN"', execution_id),
            )
    send = Mock(return_value=None)

    assert queue.drain(send, limit=1) == 0
    send.assert_not_called()
    assert queue._ACTIVE_DELIVERIES == active_before
    assert all(
        queue.get_status(f"malformed-{index}")["status"] == "failed"
        for index in range(3)
    )


def test_malformed_cleanup_terminalizes_without_owner_leak(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("malformed-finish", {"id": "job"}, "content")
    with sqlite3.connect(queue.DELIVERY_DB) as conn:
        conn.execute(
            "UPDATE deliveries SET job_json=? WHERE execution_id='malformed-finish'",
            ('{"broken"',),
        )

    assert queue.drain(Mock()) == 0
    assert "malformed-finish" not in queue._ACTIVE_DELIVERIES
    status = queue.get_status("malformed-finish")
    assert status is not None
    assert status["status"] == "failed"


def test_unexpected_hydration_failure_releases_owner_and_propagates(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("unexpected-hydration", {"id": "job"}, "content")
    monkeypatch.setattr(
        queue.json, "loads", Mock(side_effect=RuntimeError("decoder defect"))
    )
    send = Mock()

    with pytest.raises(RuntimeError, match="decoder defect"):
        queue.drain(send)

    send.assert_not_called()
    assert "unexpected-hydration" not in queue._ACTIVE_DELIVERIES


@pytest.mark.parametrize("payload", ["null", "[]", "{}"])
def test_exact_queue_rejects_schema_invalid_destination(tmp_path, monkeypatch, payload):
    import cron.delivery_queue as queue
    from cron import executions, outbox

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id="schema-invalid",
        job_id="job",
        target="telegram:original",
        destinations=[{"platform": "telegram", "chat_id": "original"}],
        content="content",
        intent_success=True,
    )[0]
    queue.enqueue(
        "schema-invalid",
        {"id": "job"},
        "content",
        destination={"platform": "telegram", "chat_id": "original"},
        outbox_id=entry["id"],
    )
    with sqlite3.connect(tmp_path / "executions.db") as conn:
        conn.execute(
            "UPDATE cron_outbox SET destination_json=? WHERE id=?",
            (payload, entry["id"]),
        )
    send = Mock(return_value=None)

    assert queue.drain(send) == 0
    send.assert_not_called()
    row = queue.get_status("schema-invalid", outbox_id=entry["id"])
    assert row is not None
    assert row["status"] == "failed"
    assert "destination" in row["error"].lower()
    assert entry["id"] not in queue._ACTIVE_DELIVERIES


def test_failure_delivery_lane_survives_durable_handoff(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue(
        "exec-failure",
        {"id": "job-failure", "failure_deliver": "local"},
        "failed",
        for_failure=True,
    )
    send = Mock(return_value=None)

    assert queue.drain(send) == 1
    send.assert_called_once_with(
        {"id": "job-failure", "failure_deliver": "local"},
        "failed",
        True,
    )


def test_legacy_queue_schema_adds_failure_lane_before_enqueue(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    db = tmp_path / "deliveries.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE deliveries (
                 execution_id TEXT PRIMARY KEY,
                 job_json TEXT NOT NULL,
                 content TEXT NOT NULL,
                 status TEXT NOT NULL,
                 owner_process_id TEXT,
                 owner_pid INTEGER,
                 owner_started_at INTEGER,
                 created_at TEXT NOT NULL,
                 finished_at TEXT,
                 error TEXT
               )"""
        )
    monkeypatch.setattr(queue, "DELIVERY_DB", db)

    queue.enqueue(
        "exec-migrated",
        {"id": "job-migrated"},
        "failed",
        for_failure=True,
    )

    assert queue.get_status("exec-migrated")["for_failure"] == 1


def test_wait_timeout_marks_inflight_delivery_unknown_without_retry(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-inflight", {"id": "job-inflight"}, "result")
    assert queue.claim_next() is not None

    error = queue.enqueue_and_wait(
        "exec-inflight", {"id": "job-inflight"}, "result", timeout=0
    )

    assert error is not None
    assert "outcome is unknown" in error
    status = queue.get_status("exec-inflight")
    assert status is not None
    assert status["status"] == "unknown"
    send = Mock(return_value=None)
    assert queue.drain(send) == 0
    send.assert_not_called()


def test_dead_delivery_owner_becomes_unknown_and_is_not_retried(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    assert queue.claim_next() is not None
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: False)

    assert queue.recover_abandoned() == 1
    send = Mock()
    assert queue.drain(send) == 0
    send.assert_not_called()
    assert queue.get_status("exec-1")["status"] == "unknown"


def test_delivery_failure_is_terminal_not_retried_and_redacted(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    send = Mock(return_value="request failed: https://example.test/?token=TOKEN123")

    assert queue.drain(send) == 1
    assert queue.drain(send) == 0
    assert send.call_count == 1
    status = queue.get_status("exec-1")
    assert status is not None
    assert status["status"] == "failed"
    assert "TOKEN123" not in status["error"]
    assert "token=***" in status["error"]


def test_wait_timeout_leaves_unclaimed_delivery_queued_for_next_gateway(
    tmp_path, monkeypatch
):
    """A row nobody claimed was never attempted: it is not uncertain, so a
    gateway outage longer than the worker's wait budget must not lose it."""
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    job = {"id": "job-3", "deliver": "origin"}

    error = queue.enqueue_and_wait("exec-3", job, "result", timeout=0)

    # Deferred, not failed: the worker must not record delivery_failed.
    assert error is None
    status = queue.get_status("exec-3")
    assert status is not None
    assert status["status"] == "pending"
    send = Mock(return_value=None)
    assert queue.drain(send) == 1
    send.assert_called_once_with(job, "result", False)
    assert queue.get_status("exec-3")["status"] == "delivered"


def test_same_gateway_recovers_terminalization_failure_without_resending(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-4", {"id": "job-4"}, "result")
    send = Mock(return_value=None)
    original_finish = queue._finish
    monkeypatch.setattr(
        queue,
        "_finish",
        Mock(side_effect=OSError("database temporarily unavailable")),
    )

    with pytest.raises(OSError, match="temporarily unavailable"):
        queue.drain(send)

    monkeypatch.setattr(queue, "_finish", original_finish)
    assert queue.drain(send) == 0
    send.assert_called_once()
    status = queue.get_status("exec-4")
    assert status["status"] == "unknown"
    assert "not retried" in status["error"]


def _settle_exact_row_suppressed(tmp_path, monkeypatch):
    """Persist a genuine exact failure-notice row and settle it ``suppressed``.

    The row is produced by the real outbox intent API, linked to a real execution
    ledger row and job delivery projection, claimed by the real queue and settled
    through the production ``drain`` -> ``_finish`` -> ``finish_exact`` path. The
    sender honours the documented warning-policy contract: it withholds every
    target, flags ``_notification_all_targets_suppressed`` and never reaches a
    platform adapter. No SQL writes or forged rows are involved.
    """
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox

    executions_db = tmp_path / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", executions_db)
    monkeypatch.setattr(outbox, "EXECUTIONS_FILE", executions_db)
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    adapter = Mock(side_effect=AssertionError("platform adapter must not send"))
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", adapter)

    job_id = "job-suppressed"
    execution = executions.create_execution(job_id, source="test")
    outbox.begin_job_delivery_projection(job_id, execution["id"])
    destination = {"platform": "telegram", "chat_id": "muted-chat"}
    job = {"id": job_id, "execution_id": execution["id"]}
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id=execution["id"],
        job_id=job_id,
        target="telegram:muted-chat",
        destinations=[destination],
        content="failure notice",
        intent_success=False,
        intent_error="script exited 1",
        job=job,
    )[0]
    assert entry["exact_state"] == "READY"
    queued = queue.enqueue(
        execution["id"],
        job,
        "failure notice",
        for_failure=True,
        destination=destination,
        outbox_id=entry["id"],
    )
    assert queued["status"] == "pending"

    def withholding_sender(job_arg, content, for_failure, **kwargs):
        assert for_failure is True
        assert kwargs["outbox_id"] == entry["id"]
        job_arg["_notification_all_targets_suppressed"] = True
        return None

    assert queue.drain(withholding_sender) == 1
    return {
        "queue": queue,
        "executions": executions,
        "outbox": outbox,
        "executions_db": executions_db,
        "adapter": adapter,
        "job_id": job_id,
        "execution_id": execution["id"],
        "job": job,
        "destination": destination,
        "outbox_id": entry["id"],
    }


def test_exact_suppressed_settlement_round_trips_restart_and_drain_without_send(
    tmp_path, monkeypatch
):
    from cron import scheduler

    ctx = _settle_exact_row_suppressed(tmp_path, monkeypatch)
    queue, outbox = ctx["queue"], ctx["outbox"]
    outbox_id, execution_id = ctx["outbox_id"], ctx["execution_id"]

    # Queue projection: settled without a send, never presented as delivered.
    exact = queue.get_exact_state(outbox_id)
    assert exact is not None
    assert exact["status"] == "suppressed"
    assert exact["state"] == "DELIVERED"
    assert exact["transport_status"] == "suppressed"
    assert exact["attempts"] == 1
    status = queue.get_status(execution_id, outbox_id=outbox_id)
    assert status is not None and status["status"] == "suppressed"
    settled = outbox.get_outbox(outbox_id)
    assert settled is not None
    assert settled["exact_state"] == "DELIVERED"
    assert settled["transport_status"] == "suppressed"
    assert settled["attempts"] == 1
    assert outbox.pending_outbox() == []
    assert outbox_id not in queue._ACTIVE_DELIVERIES
    projection = outbox.get_job_delivery_projection(ctx["job_id"])
    assert projection is not None
    assert projection["finalized"] == 1
    assert projection["last_status"] == "failed"
    assert projection["last_delivery_error"] is None

    # Restart: in-memory ownership is gone, only the durable rows survive.
    queue._ACTIVE_DELIVERIES.clear()
    assert queue.recover_abandoned() == 0
    with sqlite3.connect(ctx["executions_db"]) as conn:
        durable = conn.execute(
            "SELECT exact_state, transport_status FROM cron_outbox WHERE id=?",
            (outbox_id,),
        ).fetchone()
        incidents = conn.execute(
            "SELECT state FROM cron_incidents WHERE job_id=?", (ctx["job_id"],)
        ).fetchall()
    assert durable == ("DELIVERED", "suppressed")
    assert incidents == []
    assert queue.get_exact_state(outbox_id)["status"] == "suppressed"

    # Replay and drain after restart: nothing is resent or resurrected.
    resend = Mock(side_effect=AssertionError("suppressed exact row was resent"))
    monkeypatch.setattr(scheduler, "_deliver_result", resend)
    assert scheduler._retry_pending_deliveries() == 0
    assert scheduler.drain_delivery_queue(object(), object()) == 0
    assert queue.reactivate_exact(outbox_id) is False
    replayed = queue.enqueue(
        execution_id,
        dict(ctx["job"]),
        "failure notice",
        for_failure=True,
        destination=ctx["destination"],
        outbox_id=outbox_id,
    )
    assert replayed["status"] == "suppressed"
    assert (
        queue.enqueue_and_wait(
            execution_id,
            dict(ctx["job"]),
            "failure notice",
            for_failure=True,
            timeout=0,
            destination=ctx["destination"],
            outbox_id=outbox_id,
        )
        is None
    )
    assert queue.drain(resend) == 0
    resend.assert_not_called()
    ctx["adapter"].assert_not_called()
    final = queue.get_exact_state(outbox_id)
    assert final["status"] == "suppressed"
    assert final["transport_status"] == "suppressed"
    assert final["attempts"] == 1
    assert outbox.get_outbox(outbox_id)["attempts"] == 1
    assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 1


@pytest.mark.parametrize(
    "receipt", ["attempt_history", "ledger_status", "ledger_outcome"]
)
def test_exact_suppressed_settlement_leaves_no_confirmed_delivery_receipt(
    tmp_path, monkeypatch, receipt
):
    ctx = _settle_exact_row_suppressed(tmp_path, monkeypatch)
    queue, outbox, executions = ctx["queue"], ctx["outbox"], ctx["executions"]
    outbox_id = ctx["outbox_id"]

    assert queue.get_exact_state(outbox_id)["status"] == "suppressed"
    ctx["adapter"].assert_not_called()
    attempts = outbox.list_attempts(outbox_id)
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger is not None
    assert ledger["delivery_attempts"] == 1
    assert ledger["delivery_error"] is None
    # A settled non-send must leave no confirmed-delivery receipt anywhere the
    # real accounting writes: the attempt row and the execution ledger must say
    # "suppressed", never "delivered".
    if receipt == "attempt_history":
        assert [a["status"] for a in attempts] == ["suppressed"], attempts
    elif receipt == "ledger_status":
        assert ledger["delivery_status"] == "suppressed", ledger
    else:
        assert ledger["delivery_outcome"] == "suppressed", ledger


def _persist_exact_fanout(tmp_path, monkeypatch, destinations):
    """Persist one genuine exact failure-notice fanout: one READY row per destination.

    Same isolation and real APIs as ``_settle_exact_row_suppressed``; every row is
    linked to one execution ledger row and one job delivery projection, and each is
    queued through the real ``enqueue``. Nothing is settled here, so a test controls
    the actual settlement order by draining one admitted outbox id at a time.
    """
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox

    executions_db = tmp_path / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", executions_db)
    monkeypatch.setattr(outbox, "EXECUTIONS_FILE", executions_db)
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    adapter = Mock(side_effect=AssertionError("platform adapter must not send"))
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", adapter)

    job_id = "job-fanout"
    execution = executions.create_execution(job_id, source="test")
    outbox.begin_job_delivery_projection(job_id, execution["id"])
    job = {"id": job_id, "execution_id": execution["id"]}
    entries = outbox.enqueue_deliveries_with_intent(
        execution_id=execution["id"],
        job_id=job_id,
        target="telegram:fanout",
        destinations=list(destinations),
        content="failure notice",
        intent_success=False,
        intent_error="script exited 1",
        job=job,
    )
    assert [entry["exact_state"] for entry in entries] == ["READY"] * len(destinations)
    ids = {}
    for destination, entry in zip(destinations, entries):
        queued = queue.enqueue(
            execution["id"],
            job,
            "failure notice",
            for_failure=True,
            destination=destination,
            outbox_id=entry["id"],
        )
        assert queued["status"] == "pending"
        ids[destination["chat_id"]] = entry["id"]
    return {
        "queue": queue,
        "executions": executions,
        "outbox": outbox,
        "executions_db": executions_db,
        "adapter": adapter,
        "job_id": job_id,
        "execution_id": execution["id"],
        "ids": ids,
        "calls": [],
    }


def _fanout_sender(ctx, *, withhold=(), fail=None):
    """Sender honouring the documented contract per admitted outbox id.

    Withheld ids flag ``_notification_all_targets_suppressed`` and return None;
    ids in ``fail`` return their transport error; every other id is a genuine send.
    """

    def send(job_arg, content, for_failure, **kwargs):
        assert for_failure is True
        outbox_id = kwargs["outbox_id"]
        ctx["calls"].append(outbox_id)
        if outbox_id in withhold:
            job_arg["_notification_all_targets_suppressed"] = True
            return None
        if fail and outbox_id in fail:
            return fail[outbox_id]
        return None

    return send


def _attempt_statuses(ctx, outbox_id):
    return [a["status"] for a in ctx["outbox"].list_attempts(outbox_id)]


def _delivery_incident_states(ctx):
    with sqlite3.connect(ctx["executions_db"]) as conn:
        rows = conn.execute(
            """SELECT state FROM cron_incidents
               WHERE job_id=? AND failure_type='delivery' ORDER BY id""",
            (ctx["job_id"],),
        ).fetchall()
    return [row[0] for row in rows]


@pytest.mark.parametrize("order", ["a_then_b", "b_then_a"])
def test_exact_all_suppressed_fanout_settles_suppressed_in_either_order(
    tmp_path, monkeypatch, order
):
    from cron import scheduler

    ctx = _persist_exact_fanout(
        tmp_path,
        monkeypatch,
        [
            {"platform": "telegram", "chat_id": "muted-a"},
            {"platform": "telegram", "chat_id": "muted-b"},
        ],
    )
    queue, outbox, executions = ctx["queue"], ctx["outbox"], ctx["executions"]
    a, b = ctx["ids"]["muted-a"], ctx["ids"]["muted-b"]
    send = _fanout_sender(ctx, withhold={a, b})
    settle_order = (a, b) if order == "a_then_b" else (b, a)
    for outbox_id in settle_order:
        assert queue.drain(send, exact_outbox_ids=[outbox_id]) == 1
    assert ctx["calls"] == list(settle_order)
    ctx["adapter"].assert_not_called()

    for outbox_id in (a, b):
        exact = queue.get_exact_state(outbox_id)
        assert exact["status"] == "suppressed"
        assert exact["state"] == "DELIVERED"
        assert exact["transport_status"] == "suppressed"
        assert exact["attempts"] == 1
        assert _attempt_statuses(ctx, outbox_id) == ["suppressed"]
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "suppressed", ledger
    assert ledger["delivery_outcome"] == "suppressed", ledger
    assert ledger["delivery_attempts"] == 2
    assert ledger["delivery_error"] is None
    projection = outbox.get_job_delivery_projection(ctx["job_id"])
    assert projection["finalized"] == 1
    assert projection["last_status"] == "failed"
    assert projection["last_delivery_error"] is None
    assert _delivery_incident_states(ctx) == []

    # Restart and replay: the aggregate stays suppressed and nothing is resent.
    queue._ACTIVE_DELIVERIES.clear()
    assert queue.recover_abandoned() == 0
    resend = Mock(side_effect=AssertionError("suppressed exact row was resent"))
    monkeypatch.setattr(scheduler, "_deliver_result", resend)
    assert scheduler._retry_pending_deliveries() == 0
    assert queue.drain(resend) == 0
    resend.assert_not_called()
    ctx["adapter"].assert_not_called()
    for outbox_id in (a, b):
        assert queue.reactivate_exact(outbox_id) is False
        assert _attempt_statuses(ctx, outbox_id) == ["suppressed"]
        assert outbox.get_outbox(outbox_id)["attempts"] == 1
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "suppressed"
    assert ledger["delivery_outcome"] == "suppressed"
    assert ledger["delivery_attempts"] == 2


@pytest.mark.parametrize("order", ["sent_then_muted", "muted_then_sent"])
def test_exact_mixed_sent_and_suppressed_fanout_is_delivered_in_either_order(
    tmp_path, monkeypatch, order
):
    ctx = _persist_exact_fanout(
        tmp_path,
        monkeypatch,
        [
            {"platform": "telegram", "chat_id": "open-chat"},
            {"platform": "telegram", "chat_id": "muted-chat"},
        ],
    )
    queue, outbox, executions = ctx["queue"], ctx["outbox"], ctx["executions"]
    sent, muted = ctx["ids"]["open-chat"], ctx["ids"]["muted-chat"]
    send = _fanout_sender(ctx, withhold={muted})
    settle_order = (sent, muted) if order == "sent_then_muted" else (muted, sent)
    for outbox_id in settle_order:
        assert queue.drain(send, exact_outbox_ids=[outbox_id]) == 1
    assert ctx["calls"] == list(settle_order)

    # The genuine send is unchanged; only the withheld row carries the marker.
    assert queue.get_exact_state(sent)["status"] == "delivered"
    assert queue.get_exact_state(sent)["transport_status"] != "suppressed"
    assert _attempt_statuses(ctx, sent) == ["delivered"]
    assert queue.get_exact_state(muted)["status"] == "suppressed"
    assert _attempt_statuses(ctx, muted) == ["suppressed"]
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "delivered", ledger
    assert ledger["delivery_outcome"] == "delivered", ledger
    assert ledger["delivery_attempts"] == 2
    assert ledger["delivery_error"] is None
    assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 1
    assert _delivery_incident_states(ctx) == []

    # Replay: exactly one send ever happens.
    assert queue.drain(send) == 0
    assert ctx["calls"] == list(settle_order)
    for outbox_id in (sent, muted):
        assert outbox.get_outbox(outbox_id)["attempts"] == 1


@pytest.mark.parametrize("order", ["failed_then_muted", "muted_then_failed"])
def test_exact_failure_dominates_suppression_in_either_order(
    tmp_path, monkeypatch, order
):
    ctx = _persist_exact_fanout(
        tmp_path,
        monkeypatch,
        [
            {"platform": "telegram", "chat_id": "broken-chat"},
            {"platform": "telegram", "chat_id": "muted-chat"},
        ],
    )
    queue, outbox, executions = ctx["queue"], ctx["outbox"], ctx["executions"]
    broken, muted = ctx["ids"]["broken-chat"], ctx["ids"]["muted-chat"]
    send = _fanout_sender(
        ctx, withhold={muted}, fail={broken: "transport rejected: 503"}
    )
    settle_order = (broken, muted) if order == "failed_then_muted" else (muted, broken)
    for outbox_id in settle_order:
        assert queue.drain(send, exact_outbox_ids=[outbox_id]) == 1
    assert ctx["calls"] == list(settle_order)

    # Intermediate state: the retryable failure owns the ledger whichever row
    # settled last; the suppressed sibling cannot hide it.
    assert outbox.get_outbox(broken)["exact_state"] == "RETRYABLE_FAILED"
    assert queue.get_exact_state(broken)["status"] == "failed"
    assert _attempt_statuses(ctx, broken) == ["failed"]
    assert queue.get_exact_state(muted)["status"] == "suppressed"
    assert _attempt_statuses(ctx, muted) == ["suppressed"]
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "failed", ledger
    assert "transport rejected" in (ledger["delivery_error"] or ""), ledger
    assert ledger["delivery_outcome"] in (None, "failed"), ledger
    assert ledger["delivery_attempts"] == 2
    assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 0
    assert _delivery_incident_states(ctx) == ["detected"]

    # Terminal state: retrying the broken target to its cap makes it DEAD; the
    # aggregate is failed, the error survives, the muted row never re-sends.
    for _ in range(outbox.MAX_OUTBOX_ATTEMPTS):
        if outbox.get_outbox(broken)["exact_state"] != "RETRYABLE_FAILED":
            break
        assert queue.reactivate_exact(broken) is True
        assert queue.drain(send, exact_outbox_ids=[broken]) == 1
    assert outbox.get_outbox(broken)["exact_state"] == "DEAD"
    assert outbox.get_outbox(broken)["attempts"] == outbox.MAX_OUTBOX_ATTEMPTS
    assert ctx["calls"].count(broken) == outbox.MAX_OUTBOX_ATTEMPTS
    assert ctx["calls"].count(muted) == 1
    assert outbox.get_outbox(muted)["attempts"] == 1
    assert _attempt_statuses(ctx, muted) == ["suppressed"]
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "failed", ledger
    assert ledger["delivery_outcome"] == "failed", ledger
    assert "transport rejected" in (ledger["delivery_error"] or ""), ledger
    projection = outbox.get_job_delivery_projection(ctx["job_id"])
    assert projection["finalized"] == 1
    assert projection["last_status"] == "delivery_failed"
    assert _delivery_incident_states(ctx) == ["detected"]
    ctx["adapter"].assert_not_called()


def test_exact_suppressed_retry_keeps_existing_delivery_incident_detected(
    tmp_path, monkeypatch
):
    ctx = _persist_exact_fanout(
        tmp_path, monkeypatch, [{"platform": "telegram", "chat_id": "muted-chat"}]
    )
    queue, outbox, executions = ctx["queue"], ctx["outbox"], ctx["executions"]
    row_id = ctx["ids"]["muted-chat"]

    # A real failed transport seeds the delivery incident through the accounting API.
    failing = _fanout_sender(ctx, fail={row_id: "transport rejected: 503"})
    assert queue.drain(failing, exact_outbox_ids=[row_id]) == 1
    assert outbox.get_outbox(row_id)["exact_state"] == "RETRYABLE_FAILED"
    assert _attempt_statuses(ctx, row_id) == ["failed"]
    assert _delivery_incident_states(ctx) == ["detected"]
    assert executions.get_execution(ctx["execution_id"])["delivery_status"] == "failed"

    # The policy now withholds the target: the retry settles without a send and
    # proves nothing about transport, so the incident is neither recovered nor alerted.
    assert queue.reactivate_exact(row_id) is True
    withholding = _fanout_sender(ctx, withhold={row_id})
    assert queue.drain(withholding, exact_outbox_ids=[row_id]) == 1
    assert ctx["calls"] == [row_id, row_id]
    ctx["adapter"].assert_not_called()
    assert queue.get_exact_state(row_id)["status"] == "suppressed"
    assert _attempt_statuses(ctx, row_id) == ["failed", "suppressed"]
    assert _delivery_incident_states(ctx) == ["detected"]
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "suppressed", ledger
    assert ledger["delivery_outcome"] == "suppressed", ledger
    assert ledger["delivery_attempts"] == 2
    assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 1

    # Replay after the suppressed retry: no resend, no incident transition.
    assert queue.reactivate_exact(row_id) is False
    assert queue.drain(withholding) == 0
    assert ctx["calls"] == [row_id, row_id]
    assert _delivery_incident_states(ctx) == ["detected"]


def test_exact_finish_with_stale_owner_rolls_back_without_accounting(
    tmp_path, monkeypatch
):
    ctx = _persist_exact_fanout(
        tmp_path, monkeypatch, [{"platform": "telegram", "chat_id": "muted-chat"}]
    )
    outbox, executions = ctx["outbox"], ctx["executions"]
    row_id = ctx["ids"]["muted-chat"]
    claimed = outbox.claim_exact(
        owner_process_id="r9-stale-owner-test",
        owner_pid=os.getpid(),
        owner_started_at=None,
        outbox_ids=[row_id],
    )
    assert claimed is not None and claimed["id"] == row_id
    assert claimed["exact_state"] == "IN_FLIGHT"
    generation, owner = int(claimed["generation"]), str(claimed["owner_token"])
    ledger_before = executions.get_execution(ctx["execution_id"])

    # A stale owner or generation loses the CAS before any accounting is written.
    assert (
        outbox.finish_exact(
            row_id,
            generation=generation,
            owner_token=owner + "-stale",
            error=None,
            transport_status="suppressed",
        )
        is False
    )
    assert (
        outbox.finish_exact(
            row_id,
            generation=generation + 1,
            owner_token=owner,
            error=None,
            transport_status="suppressed",
        )
        is False
    )
    assert (
        outbox.finish_exact(
            row_id,
            generation=generation,
            owner_token=owner + "-stale",
            error="transport rejected: 503",
        )
        is False
    )
    row = outbox.get_outbox(row_id)
    assert row["exact_state"] == "IN_FLIGHT"
    assert row["owner_token"] == owner
    assert row["generation"] == generation
    assert row["attempts"] == 0
    assert row["transport_status"] == claimed["transport_status"]
    assert outbox.list_attempts(row_id) == []
    assert executions.get_execution(ctx["execution_id"]) == ledger_before
    assert _delivery_incident_states(ctx) == []
    assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 0

    # The genuine owner settles exactly once; a repeat by the retired owner is a no-op.
    assert (
        outbox.finish_exact(
            row_id,
            generation=generation,
            owner_token=owner,
            error=None,
            transport_status="suppressed",
        )
        is True
    )
    assert _attempt_statuses(ctx, row_id) == ["suppressed"]
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "suppressed", ledger
    assert ledger["delivery_outcome"] == "suppressed", ledger
    assert ledger["delivery_attempts"] == 1
    assert (
        outbox.finish_exact(
            row_id,
            generation=generation,
            owner_token=owner,
            error=None,
            transport_status="suppressed",
        )
        is False
    )
    assert _attempt_statuses(ctx, row_id) == ["suppressed"]
    assert outbox.get_outbox(row_id)["attempts"] == 1
    assert _delivery_incident_states(ctx) == []
    ctx["adapter"].assert_not_called()


def _settle_admitted_row_ambiguous(ctx, outbox_id):
    """Drive one exact row through the real admitted-receipt path to UNKNOWN.

    ``claim_exact`` -> ``finish_exact(transport_status="queued")`` is the production
    Bot Chat admission; ``settle_admitted_delivery(receipt_status="ambiguous")`` is
    the production reconciliation of a receipt whose outcome cannot be known.
    """
    outbox = ctx["outbox"]
    claimed = outbox.claim_exact(
        owner_process_id="r9-admitting-owner",
        owner_pid=os.getpid(),
        owner_started_at=None,
        outbox_ids=[outbox_id],
    )
    assert claimed is not None and claimed["id"] == outbox_id
    receipt_id = f"receipt-{outbox_id}"
    assert (
        outbox.finish_exact(
            outbox_id,
            generation=int(claimed["generation"]),
            owner_token=str(claimed["owner_token"]),
            error=None,
            transport_status="queued",
            receipt_id=receipt_id,
        )
        is True
    )
    assert _attempt_statuses(ctx, outbox_id) == ["admitted"]
    assert (
        outbox.settle_admitted_delivery(
            outbox_id,
            receipt_id=receipt_id,
            receipt_status="ambiguous",
            receipt_reason="Bot Chat delivery outcome is ambiguous",
        )
        is True
    )
    assert outbox.get_outbox(outbox_id)["exact_state"] == "UNKNOWN"


@pytest.mark.parametrize("order", ["unknown_then_muted", "muted_then_unknown"])
def test_exact_unknown_dominates_suppression_in_either_order(
    tmp_path, monkeypatch, order
):
    ctx = _persist_exact_fanout(
        tmp_path,
        monkeypatch,
        [
            {"platform": "telegram", "chat_id": "ambiguous-chat"},
            {"platform": "telegram", "chat_id": "muted-chat"},
        ],
    )
    queue, outbox, executions = ctx["queue"], ctx["outbox"], ctx["executions"]
    ambiguous, muted = ctx["ids"]["ambiguous-chat"], ctx["ids"]["muted-chat"]
    send = _fanout_sender(ctx, withhold={muted})

    if order == "unknown_then_muted":
        _settle_admitted_row_ambiguous(ctx, ambiguous)
        ledger = executions.get_execution(ctx["execution_id"])
        assert ledger["delivery_status"] == "unknown", ledger
        assert queue.drain(send, exact_outbox_ids=[muted]) == 1
    else:
        assert queue.drain(send, exact_outbox_ids=[muted]) == 1
        ledger = executions.get_execution(ctx["execution_id"])
        assert ledger["delivery_status"] == "suppressed", ledger
        assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 0
        _settle_admitted_row_ambiguous(ctx, ambiguous)
    assert ctx["calls"] == [muted]
    ctx["adapter"].assert_not_called()

    # The unknown outcome owns the aggregate whichever row settled last; the
    # suppressed sibling can neither hide it nor turn it into a delivery.
    assert outbox.get_outbox(ambiguous)["exact_state"] == "UNKNOWN"
    assert _attempt_statuses(ctx, ambiguous) == ["unknown"]
    assert queue.get_exact_state(muted)["status"] == "suppressed"
    assert _attempt_statuses(ctx, muted) == ["suppressed"]
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "failed", ledger
    assert ledger["delivery_outcome"] == "failed", ledger
    assert "ambiguous" in (ledger["delivery_error"] or ""), ledger
    assert ledger["delivery_attempts"] == 2
    projection = outbox.get_job_delivery_projection(ctx["job_id"])
    assert projection["finalized"] == 1
    assert projection["last_status"] == "delivery_failed"
    assert _delivery_incident_states(ctx) == ["detected"]

    # Replay: neither row is resent or reactivated; the aggregate is unchanged.
    assert queue.drain(send) == 0
    assert ctx["calls"] == [muted]
    for outbox_id in (ambiguous, muted):
        assert queue.reactivate_exact(outbox_id) is False
        assert outbox.get_outbox(outbox_id)["attempts"] == 1
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "failed"
    assert ledger["delivery_outcome"] == "failed"
    assert _delivery_incident_states(ctx) == ["detected"]
    ctx["adapter"].assert_not_called()


@pytest.mark.parametrize("settlement", ["suppressed", "retryable_failure"])
def test_exact_finish_rolls_back_every_write_when_accounting_fails_mid_transaction(
    tmp_path, monkeypatch, settlement
):
    ctx = _persist_exact_fanout(
        tmp_path, monkeypatch, [{"platform": "telegram", "chat_id": "muted-chat"}]
    )
    outbox, executions = ctx["outbox"], ctx["executions"]
    row_id = ctx["ids"]["muted-chat"]
    claimed = outbox.claim_exact(
        owner_process_id="r9-rollback-owner",
        owner_pid=os.getpid(),
        owner_started_at=None,
        outbox_ids=[row_id],
    )
    assert claimed is not None and claimed["exact_state"] == "IN_FLIGHT"
    generation, owner = int(claimed["generation"]), str(claimed["owner_token"])
    ledger_before = executions.get_execution(ctx["execution_id"])
    finish_kwargs = (
        {"error": None, "transport_status": "suppressed"}
        if settlement == "suppressed"
        else {"error": "transport rejected: 503"}
    )

    # The accounting step runs for real (attempt row, ledger, incident) and the
    # transaction then fails before commit: nothing it wrote may survive.
    real_account = outbox.record_exact_queue_outcome_in
    inject = {"active": True}

    def failing_account(conn, **kwargs):
        assert real_account(conn, **kwargs) is True
        if inject["active"]:
            raise sqlite3.OperationalError("injected failure after accounting")
        return True

    monkeypatch.setattr(outbox, "record_exact_queue_outcome_in", failing_account)
    with pytest.raises(sqlite3.OperationalError, match="injected failure"):
        outbox.finish_exact(
            row_id, generation=generation, owner_token=owner, **finish_kwargs
        )
    row = outbox.get_outbox(row_id)
    assert row["exact_state"] == "IN_FLIGHT"
    assert row["state"] == "pending"
    assert row["owner_token"] == owner
    assert row["generation"] == generation
    assert row["attempts"] == 0
    assert row["outcome_error"] is None
    assert row["transport_status"] == claimed["transport_status"]
    assert outbox.list_attempts(row_id) == []
    assert executions.get_execution(ctx["execution_id"]) == ledger_before
    assert _delivery_incident_states(ctx) == []
    assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 0

    # The same owner settles exactly once after the fault clears.
    inject["active"] = False
    assert (
        outbox.finish_exact(
            row_id, generation=generation, owner_token=owner, **finish_kwargs
        )
        is True
    )
    row = outbox.get_outbox(row_id)
    assert row["attempts"] == 1
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_attempts"] == 1
    if settlement == "suppressed":
        assert row["exact_state"] == "DELIVERED"
        assert row["transport_status"] == "suppressed"
        assert _attempt_statuses(ctx, row_id) == ["suppressed"]
        assert ledger["delivery_status"] == "suppressed", ledger
        assert ledger["delivery_outcome"] == "suppressed", ledger
        assert _delivery_incident_states(ctx) == []
        assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 1
    else:
        assert row["exact_state"] == "RETRYABLE_FAILED"
        assert _attempt_statuses(ctx, row_id) == ["failed"]
        assert ledger["delivery_status"] == "failed", ledger
        assert "transport rejected" in (ledger["delivery_error"] or ""), ledger
        assert _delivery_incident_states(ctx) == ["detected"]
        assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 0
    assert (
        outbox.finish_exact(
            row_id, generation=generation, owner_token=owner, **finish_kwargs
        )
        is False
    )
    assert outbox.get_outbox(row_id)["attempts"] == 1
    assert len(outbox.list_attempts(row_id)) == 1
    ctx["adapter"].assert_not_called()


@pytest.mark.parametrize("order", ["a_then_b", "b_then_a"])
def test_exact_seeded_incident_survives_all_suppressed_fanout_in_either_order(
    tmp_path, monkeypatch, order
):
    ctx = _persist_exact_fanout(
        tmp_path,
        monkeypatch,
        [
            {"platform": "telegram", "chat_id": "muted-a"},
            {"platform": "telegram", "chat_id": "muted-b"},
        ],
    )
    queue, outbox, executions = ctx["queue"], ctx["outbox"], ctx["executions"]
    a, b = ctx["ids"]["muted-a"], ctx["ids"]["muted-b"]

    # Real failed transports on both rows seed the delivery incident.
    failing = _fanout_sender(
        ctx, fail={a: "transport rejected: 503", b: "transport rejected: 503"}
    )
    for outbox_id in (a, b):
        assert queue.drain(failing, exact_outbox_ids=[outbox_id]) == 1
        assert outbox.get_outbox(outbox_id)["exact_state"] == "RETRYABLE_FAILED"
        assert _attempt_statuses(ctx, outbox_id) == ["failed"]
    assert _delivery_incident_states(ctx) == ["detected"]
    assert executions.get_execution(ctx["execution_id"])["delivery_status"] == "failed"

    # Policy now withholds every target: both retries settle without a send.
    withholding = _fanout_sender(ctx, withhold={a, b})
    first, second = (a, b) if order == "a_then_b" else (b, a)
    assert queue.reactivate_exact(first) is True
    assert queue.drain(withholding, exact_outbox_ids=[first]) == 1
    # Intermediate: the sibling is still RETRYABLE_FAILED, so it owns the ledger
    # and the incident stays exactly where it was.
    assert outbox.get_outbox(second)["exact_state"] == "RETRYABLE_FAILED"
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "failed", ledger
    assert "transport rejected" in (ledger["delivery_error"] or ""), ledger
    assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 0
    assert _delivery_incident_states(ctx) == ["detected"]
    assert queue.reactivate_exact(second) is True
    assert queue.drain(withholding, exact_outbox_ids=[second]) == 1
    assert ctx["calls"] == [a, b, first, second]
    ctx["adapter"].assert_not_called()

    # Final: all suppressed, nothing sent, so the incident is neither recovered
    # nor alerted and no projection claims a delivery.
    for outbox_id in (a, b):
        assert queue.get_exact_state(outbox_id)["status"] == "suppressed"
        assert _attempt_statuses(ctx, outbox_id) == ["failed", "suppressed"]
        assert outbox.get_outbox(outbox_id)["attempts"] == 2
    ledger = executions.get_execution(ctx["execution_id"])
    assert ledger["delivery_status"] == "suppressed", ledger
    assert ledger["delivery_outcome"] == "suppressed", ledger
    assert ledger["delivery_attempts"] == 4
    projection = outbox.get_job_delivery_projection(ctx["job_id"])
    assert projection["finalized"] == 1
    assert projection["last_status"] == "failed"
    assert _delivery_incident_states(ctx) == ["detected"]

    # Replay after restart: no resend, no reactivation, no incident transition.
    queue._ACTIVE_DELIVERIES.clear()
    assert queue.recover_abandoned() == 0
    assert queue.drain(withholding) == 0
    for outbox_id in (a, b):
        assert queue.reactivate_exact(outbox_id) is False
        assert outbox.get_outbox(outbox_id)["attempts"] == 2
    assert ctx["calls"] == [a, b, first, second]
    assert _delivery_incident_states(ctx) == ["detected"]
    ctx["adapter"].assert_not_called()


@pytest.mark.parametrize("first_chat", ["a", "b"])
@pytest.mark.parametrize("retry_state", ["READY", "IN_FLIGHT"])
def test_suppression_preserves_failure_while_sibling_retry_pending(
    tmp_path, monkeypatch, first_chat, retry_state
):
    """A pending retry must not lose its failure to a suppressed sibling.

    The production backlog pass (``scheduler._retry_pending_deliveries``) moves
    EVERY due ``RETRYABLE_FAILED`` row back to ``READY`` before the gateway drains
    one, and a drain may claim the sibling ``IN_FLIGHT`` first. While that sibling
    still carries its unresolved failed attempt, a non-send settlement on the other
    row must leave the ledger failure in place; only the sibling's own terminal
    settlement supersedes it.
    """
    from cron import scheduler

    ctx = _persist_exact_fanout(
        tmp_path,
        monkeypatch,
        [
            {"platform": "telegram", "chat_id": "a"},
            {"platform": "telegram", "chat_id": "b"},
        ],
    )
    queue, outbox, executions = ctx["queue"], ctx["outbox"], ctx["executions"]
    a, b = ctx["ids"]["a"], ctx["ids"]["b"]
    failing = _fanout_sender(
        ctx, fail={a: "transport rejected: 503", b: "transport rejected: 503"}
    )
    for row_id in (a, b):
        assert queue.drain(failing, exact_outbox_ids=[row_id]) == 1
        assert outbox.get_outbox(row_id)["exact_state"] == "RETRYABLE_FAILED"
    assert executions.get_execution(ctx["execution_id"])["delivery_status"] == "failed"
    assert _delivery_incident_states(ctx) == ["detected"]

    # This is the actual backlog activation pass, before the gateway drains.
    assert scheduler._retry_pending_deliveries() == 0
    assert all(outbox.get_outbox(row_id)["exact_state"] == "READY" for row_id in (a, b))
    first = ctx["ids"][first_chat]
    second = b if first == a else a
    claim = None
    if retry_state == "IN_FLIGHT":
        claim = outbox.claim_exact(
            owner_process_id="review-adversarial",
            owner_pid=os.getpid(),
            owner_started_at=None,
            outbox_ids=[second],
        )
        assert claim is not None
    withholding = _fanout_sender(ctx, withhold={a, b})
    assert queue.drain(withholding, exact_outbox_ids=[first]) == 1
    sibling = outbox.get_outbox(second)
    assert sibling["exact_state"] == retry_state
    assert sibling["attempts"] == 1
    assert sibling["outcome_error"] == "transport rejected: 503"
    ledger = executions.get_execution(ctx["execution_id"])
    intermediate = {
        key: ledger[key]
        for key in (
            "delivery_status",
            "delivery_error",
            "delivery_outcome",
            "delivery_attempts",
        )
    }
    assert outbox.get_job_delivery_projection(ctx["job_id"])["finalized"] == 0
    assert _attempt_statuses(ctx, first) == ["failed", "suppressed"]
    assert _attempt_statuses(ctx, second) == ["failed"]
    assert _delivery_incident_states(ctx) == ["detected"]
    ctx["adapter"].assert_not_called()

    # Complete the retry and check terminal non-send accounting and no replay.
    if claim is None:
        assert queue.drain(withholding, exact_outbox_ids=[second]) == 1
    else:
        assert outbox.finish_exact(
            second,
            generation=claim["generation"],
            owner_token=claim["owner_token"],
            error=None,
            transport_status="suppressed",
        )
    final = executions.get_execution(ctx["execution_id"])
    assert final["delivery_status"] == final["delivery_outcome"] == "suppressed"
    assert final["delivery_attempts"] == 4
    assert _delivery_incident_states(ctx) == ["detected"]
    assert queue.drain(withholding) == 0
    assert all(queue.reactivate_exact(row_id) is False for row_id in (a, b))
    ctx["adapter"].assert_not_called()

    assert intermediate["delivery_status"] == "failed", intermediate
    assert "transport rejected" in (intermediate["delivery_error"] or ""), intermediate


def test_opened_thread_is_durable_before_a_competing_drainer_can_claim(tmp_path, monkeypatch):
    """A continuation thread must be committed with the claim release.

    Releasing first leaves a READY row whose destination still has no thread.
    A concurrent drainer can claim that row, the later thread update then
    rejects IN_FLIGHT, and the helper used to report success anyway.
    """
    from cron import delivery_queue as queue
    from cron import executions
    from cron import outbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr(outbox, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    execution = executions.create_execution("thread-job", source="test")
    outbox.begin_job_delivery_projection("thread-job", execution["id"])
    destination = {"platform": "discord", "chat_id": "review-channel"}
    entry = outbox.enqueue_deliveries_with_intent(
        execution_id=execution["id"],
        job_id="thread-job",
        target="discord:review-channel",
        destinations=[destination],
        content="review-content",
        intent_success=True,
        job={"id": "thread-job", "execution_id": execution["id"]},
    )[0]
    row = queue.claim_next(exact_outbox_ids=[entry["id"]])
    assert row is not None
    row["job"]["_exact_thread_updates"] = {entry["id"]: "opened-thread-1"}
    original_release = outbox.release_exact_claim
    observed = {}

    def release_then_compete(outbox_id, *, generation, owner_token):
        released = original_release(
            outbox_id, generation=generation, owner_token=owner_token
        )
        if released:
            competitor = queue.claim_next(exact_outbox_ids=[outbox_id])
            observed["competitor"] = competitor
        return released

    monkeypatch.setattr(outbox, "release_exact_claim", release_then_compete)
    applied = queue._apply_exact_thread_update(row)
    final = outbox.get_outbox(entry["id"])
    final_destination = outbox.decode_persisted_destination(final["destination_json"])

    assert applied is True
    assert final["exact_state"] == "READY"
    assert final_destination["thread_id"] == "opened-thread-1"
    competitor = observed.get("competitor")
    if competitor is not None:
        assert competitor["destination"]["thread_id"] == "opened-thread-1"
