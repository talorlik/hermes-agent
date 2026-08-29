"""Transactional delivery outbox: at-least-once cron delivery.

Contract under test (recurring-automation P1/P2, requirement 4):

* Before the scheduler attempts a platform send, the execution's terminal
  intent AND a pending outbox row are persisted atomically (same SQLite
  transaction, same durable store).
* Every attempt is recorded in a per-attempt history; a failed send leaves
  the row pending (queryable) and mints a delivery incident — the SAME
  transaction writes both (requirement 5 atomicity).
* Pending deliveries are retried BEFORE new work on the next tick.
* At-least-once: a crash after a successful send but before the delivered
  mark re-sends on retry; a crash between enqueue and send delivers on
  retry. Duplicates are acceptable, silent loss is not.
* A failed primary channel also lands the content in a durable local
  fallback file (no external credentials involved).
"""

from __future__ import annotations

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
    assert refreshed["last_status"] == "ok"
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


def test_crash_after_send_before_mark_redelivers_at_least_once(
    outbox_env, monkeypatch
):
    """Crash matrix: send succeeded, delivered-mark never happened. The row
    is still pending, so the next tick re-sends — duplicates over loss."""
    from cron import outbox as O
    from cron import scheduler as S

    sent = []

    def send_then_crash(job, content, *a, **k):
        sent.append(content)
        raise SystemExit("process killed mid-delivery bookkeeping")

    monkeypatch.setattr(S, "_deliver_result", send_then_crash)
    with pytest.raises(SystemExit):
        S.tick(verbose=False, sync=True)

    # The enqueue was durable and unmarked: pending with zero recorded
    # delivered attempts.
    rows = O.list_outbox(job_id=outbox_env["job_id"], state="pending")
    assert len(rows) == 1

    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: sent.append("retry") or None)
    S.tick(verbose=False, sync=True)
    assert "retry" in sent
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


def test_failed_primary_channel_writes_durable_local_fallback(
    outbox_env, monkeypatch
):
    from cron import scheduler as S

    monkeypatch.setattr(
        S, "_deliver_result", lambda *a, **k: "telegram adapter unreachable"
    )
    S.tick(verbose=False, sync=True)

    fallback_dir = outbox_env["home"] / "cron" / "failed_deliveries"
    files = list(fallback_dir.rglob("*.md")) if fallback_dir.exists() else []
    assert files, "failed primary delivery must land in the local fallback"
    assert any("daily report content" in f.read_text() for f in files)


def test_exception_path_failure_alert_uses_outbox_protocol(
    outbox_env, monkeypatch
):
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


def test_exception_path_successful_alert_records_delivery(
    outbox_env, monkeypatch
):
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
