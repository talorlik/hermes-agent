"""Durable at-most-once delivery handoff for restart-safe cron workers."""

from __future__ import annotations

import json
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
