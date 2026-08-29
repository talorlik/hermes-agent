"""Incident lifecycle extension: open → recovered → acknowledged.

Contract under test (recurring-automation P1/P2, requirement 5):

* Product cron incidents are the single writer; the lifecycle grows a
  ``recovered`` state — the underlying failure healed on its own (a later
  run succeeded) while the operator never acked.
* ``record_recovery`` is idempotent, per-job, and never touches
  ``closed`` (acknowledged) incidents.
* A recovered signature that fails AGAIN re-opens the SAME incident
  (idempotent dedup — no duplicate row); an acknowledged signature stays
  closed forever (existing contract, re-asserted).
* End-to-end: the scheduler records recovery automatically when a
  previously-failing job completes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def incident_store(tmp_path, monkeypatch):
    import cron.executions as executions_mod

    monkeypatch.setattr(
        executions_mod, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    import cron.incidents as incidents

    return incidents


def test_open_incident_transitions_to_recovered(incident_store):
    I = incident_store
    incident_id, is_new = I.upsert_incident("job-r", "provider 500")
    assert is_new

    changed = I.record_recovery("job-r")
    assert changed == 1
    row = I.get_incident(incident_id)
    assert row["state"] == "recovered"
    assert row["recovered_at"]

    # Idempotent: nothing left to recover.
    assert I.record_recovery("job-r") == 0


def test_recovery_never_touches_acknowledged_incidents(incident_store):
    I = incident_store
    incident_id, _ = I.upsert_incident("job-a", "provider 500")
    assert I.ack_incident(incident_id)

    assert I.record_recovery("job-a") == 0
    assert I.get_incident(incident_id)["state"] == "closed"


def test_failure_after_recovery_reopens_same_incident(incident_store):
    I = incident_store
    incident_id, _ = I.upsert_incident("job-o", "provider 500")
    I.record_recovery("job-o")
    assert I.get_incident(incident_id)["state"] == "recovered"

    reopened_id, is_new = I.upsert_incident("job-o", "provider 500")
    assert reopened_id == incident_id
    assert is_new is False
    assert I.get_incident(incident_id)["state"] == "detected"
    assert I.count_incidents() == 1


def test_failure_after_ack_stays_closed(incident_store):
    I = incident_store
    incident_id, _ = I.upsert_incident("job-c", "provider 500")
    I.ack_incident(incident_id)

    again_id, is_new = I.upsert_incident("job-c", "provider 500")
    assert again_id == incident_id
    assert is_new is False
    assert I.get_incident(incident_id)["state"] == "closed"


def test_open_incidents_view_excludes_recovered_and_closed(incident_store):
    I = incident_store
    a, _ = I.upsert_incident("job-1", "boom")
    b, _ = I.upsert_incident("job-2", "boom")
    c, _ = I.upsert_incident("job-3", "boom")
    I.record_recovery("job-2")
    I.ack_incident(c)

    open_ids = {row["id"] for row in I.open_incidents()}
    assert open_ids == {a}


def test_success_recovery_leaves_delivery_incident_open(incident_store):
    """Execution recovery and delivery recovery are independent categories:
    a successful job run proves the EXECUTION healed, not the channel — a
    still-open delivery incident must survive it untouched."""
    I = incident_store
    exec_id, _ = I.upsert_incident("job-ind", "provider 500")
    deliv_id, _ = I.upsert_incident(
        "job-ind",
        "delivery failed to telegram: 502",
        failure_type="delivery",
    )

    changed = I.record_recovery("job-ind")
    assert changed == 1
    assert I.get_incident(exec_id)["state"] == "recovered"
    assert I.get_incident(deliv_id)["state"] in I.OPEN_INCIDENT_STATES
    assert {row["id"] for row in I.open_incidents()} == {deliv_id}


def test_delivery_scope_recovers_only_delivery_incidents(incident_store):
    """The inverse independence: a healed channel recovers the delivery
    incident while an open execution incident stays open."""
    I = incident_store
    exec_id, _ = I.upsert_incident("job-inv", "provider 500")
    deliv_id, _ = I.upsert_incident(
        "job-inv",
        "delivery failed to telegram: 502",
        failure_type="delivery",
    )

    changed = I.record_recovery("job-inv", category="delivery")
    assert changed == 1
    assert I.get_incident(deliv_id)["state"] == "recovered"
    assert I.get_incident(exec_id)["state"] in I.OPEN_INCIDENT_STATES
    assert {row["id"] for row in I.open_incidents()} == {exec_id}


def _isolated_cron_env(tmp_path, monkeypatch):
    """Point the jobs store + executions ledger at an isolated temp home."""
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
    return hermes_home, jobs_mod


def test_scheduler_records_recovery_on_successful_run(tmp_path, monkeypatch):
    hermes_home, jobs_mod = _isolated_cron_env(tmp_path, monkeypatch)

    script = hermes_home / "scripts" / "flaky.sh"
    script.write_text("#!/bin/bash\necho 'kaput' >&2\nexit 1\n")
    job = jobs_mod.create_job(
        prompt="probe", schedule="every 10m", no_agent=True, script="flaky.sh"
    )

    def force_due():
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        jobs_mod.update_job(job["id"], {"next_run_at": due})

    from cron import incidents as I
    from cron import scheduler as S

    force_due()
    S.tick(verbose=False, sync=True)
    open_rows = I.open_incidents()
    assert len(open_rows) == 1

    script.write_text("#!/bin/bash\necho 'healed'\nexit 0\n")
    force_due()
    S.tick(verbose=False, sync=True)

    assert I.open_incidents() == []
    assert I.get_incident(open_rows[0]["id"])["state"] == "recovered"


def test_not_configured_delivery_does_not_mark_incident_alerted(
    tmp_path, monkeypatch
):
    """'alerted' means the operator was actually pinged. An unresolvable
    origin target composes an alert that never leaves the process — the
    incident must stay in its truthful undelivered state ('detected')."""
    hermes_home, jobs_mod = _isolated_cron_env(tmp_path, monkeypatch)

    script = hermes_home / "scripts" / "broken.sh"
    script.write_text("#!/bin/bash\necho 'kaput' >&2\nexit 1\n")
    job = jobs_mod.create_job(
        prompt="probe",
        schedule="every 10m",
        no_agent=True,
        script="broken.sh",
        deliver="origin",  # no origin recorded → unresolvable target
    )
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    jobs_mod.update_job(job["id"], {"next_run_at": due})

    from cron import incidents as I
    from cron import scheduler as S

    S.tick(verbose=False, sync=True)

    rows = I.open_incidents()
    assert len(rows) == 1
    assert rows[0]["state"] == "detected"

    # The execution ledger tells the same truth: the delivery was
    # not_configured, not "delivered".
    import cron.executions as executions_mod

    latest = executions_mod.latest_execution(job["id"])
    assert latest["delivery_status"] == "not_configured"


def test_not_configured_on_exception_path_does_not_mark_alerted(
    tmp_path, monkeypatch
):
    """Same truthful-state contract on the run-body exception path."""
    hermes_home, jobs_mod = _isolated_cron_env(tmp_path, monkeypatch)

    job = jobs_mod.create_job(
        prompt="probe", schedule="every 10m", deliver="origin"
    )
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    jobs_mod.update_job(job["id"], {"next_run_at": due})

    from cron import incidents as I
    from cron import scheduler as S

    def _boom(*args, **kwargs):
        raise RuntimeError("run body exploded")

    monkeypatch.setattr(S, "run_job", _boom)
    S.tick(verbose=False, sync=True)

    rows = I.open_incidents()
    assert len(rows) == 1
    assert rows[0]["state"] == "detected"
