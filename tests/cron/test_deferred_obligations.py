"""Durable deferred obligations for recurring cron jobs.

Contract under test (recurring-automation P1/P2, requirement 2):

* A TRANSIENT_DEFER pre-script outcome (exit 75) persists a durable retry
  obligation instead of consuming the logical occurrence: ``last_run_at``
  stays untouched, ``failure_streak`` does not increment, no incident is
  minted, nothing is delivered, and the retry fires after a bounded
  backoff — NOT at the next scheduled occurrence.
* Exactly one persisted retry per obligation: a second consecutive defer
  exhausts the budget and routes through the normal permanent-failure
  machinery (incident + failure delivery + schedule advance).
* The obligation and the deferred execution row survive a scheduler
  restart (they live on disk, not in module state).
* Obligations dedupe by job + logical occurrence — a retry fire attaches
  to the existing pending obligation, never a second row.

The end-to-end tests drive the REAL ``tick()`` against a throwaway
HERMES_HOME with a real bash script exiting 75 (the honest path through
``_run_job_script``'s exit-code surfacing).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
def defer_env(tmp_path, monkeypatch):
    """Isolated cron env + a recurring no_agent job whose script defers."""
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

    script = hermes_home / "scripts" / "gate.sh"
    script.write_text("#!/bin/bash\necho 'lock contention: another writer holds the lease'\nexit 75\n")

    def make_job(schedule: str = "every 10m"):
        job = jobs_mod.create_job(
            prompt="probe",
            schedule=schedule,
            no_agent=True,
            script="gate.sh",
        )
        due = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        jobs_mod.update_job(job["id"], {"next_run_at": due})
        return jobs_mod.get_job(job["id"])

    def make_script_succeed():
        script.write_text("#!/bin/bash\necho 'all clear'\nexit 0\n")

    return {
        "home": hermes_home,
        "make_job": make_job,
        "make_script_succeed": make_script_succeed,
    }


def _force_due(job_id):
    import cron.jobs as jobs_mod

    due = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    jobs_mod.update_job(job_id, {"next_run_at": due})


def test_defer_records_obligation_without_consuming_occurrence(
    defer_env, monkeypatch
):
    from cron import deferrals as D
    from cron import executions as E
    from cron import incidents as I
    from cron import jobs as J
    from cron import scheduler as S

    job = defer_env["make_job"]()
    due_iso = job["next_run_at"]

    delivered = []
    monkeypatch.setattr(
        S, "_deliver_result", lambda *a, **k: delivered.append(a) or None
    )

    before = datetime.now(timezone.utc)
    S.tick(verbose=False, sync=True)

    obligation = D.pending_deferral(job["id"])
    assert obligation is not None
    assert obligation["state"] == "pending"
    assert obligation["attempts"] == 0
    assert obligation["occurrence_key"] == f"{job['id']}:{due_iso}"
    assert "lock contention" in obligation["reason"]

    refreshed = J.get_job(job["id"])
    # The logical occurrence was NOT consumed and nothing counted against
    # the job's health.
    assert not refreshed.get("last_run_at")
    assert not refreshed.get("failure_streak")
    assert refreshed["last_status"] == "deferred"
    # Retry is a bounded backoff away, not a full schedule period.
    retry_at = datetime.fromisoformat(refreshed["next_run_at"])
    assert timedelta(seconds=30) < (retry_at - before) < timedelta(minutes=9)

    latest = E.latest_execution(job["id"])
    assert latest["status"] == "deferred"
    assert latest["outcome"] == "deferred"

    assert I.count_incidents() == 0
    assert delivered == []


def test_defer_then_later_completion_resolves_and_advances(defer_env):
    from cron import deferrals as D
    from cron import executions as E
    from cron import jobs as J
    from cron import scheduler as S

    job = defer_env["make_job"]()
    S.tick(verbose=False, sync=True)
    assert D.pending_deferral(job["id"]) is not None

    # Contention clears; the retry fire completes the occurrence.
    defer_env["make_script_succeed"]()
    _force_due(job["id"])
    S.tick(verbose=False, sync=True)

    assert D.pending_deferral(job["id"]) is None
    resolved = D.list_deferrals(job_id=job["id"])
    assert len(resolved) == 1
    assert resolved[0]["state"] == "completed"

    refreshed = J.get_job(job["id"])
    assert refreshed["last_status"] == "ok"
    assert refreshed["last_run_at"]
    assert not refreshed.get("failure_streak")
    # Terminal completion advances the NORMAL schedule (~10m out).
    next_run = datetime.fromisoformat(refreshed["next_run_at"])
    assert next_run > datetime.now(timezone.utc) + timedelta(minutes=5)

    assert E.latest_execution(job["id"])["status"] == "completed"


def test_second_consecutive_defer_exhausts_and_fails_permanently(defer_env):
    from cron import deferrals as D
    from cron import incidents as I
    from cron import jobs as J
    from cron import scheduler as S

    job = defer_env["make_job"]()
    S.tick(verbose=False, sync=True)
    assert D.pending_deferral(job["id"]) is not None

    # The retry ALSO defers — one persisted retry is the whole budget.
    _force_due(job["id"])
    S.tick(verbose=False, sync=True)

    assert D.pending_deferral(job["id"]) is None
    rows = D.list_deferrals(job_id=job["id"])
    assert len(rows) == 1
    assert rows[0]["state"] == "exhausted"
    assert rows[0]["attempts"] == 1

    refreshed = J.get_job(job["id"])
    # Exhaustion is a real failure: streak counts, schedule advances.
    assert refreshed["last_status"] == "error"
    assert refreshed.get("failure_streak") == 1
    next_run = datetime.fromisoformat(refreshed["next_run_at"])
    assert next_run > datetime.now(timezone.utc) + timedelta(minutes=5)

    assert I.count_incidents() == 1


def test_restart_during_defer_preserves_obligation(defer_env):
    import sqlite3

    from cron import executions as E
    from cron import jobs as J
    from cron import scheduler as S

    job = defer_env["make_job"]()
    S.tick(verbose=False, sync=True)

    # "Restart": read only what is durable on disk with a fresh connection.
    db = defer_env["home"] / "cron" / "executions.db"
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM cron_deferrals WHERE job_id=?", (job["id"],)
            )
        ]
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["retry_at"]

    # The restart recovery sweep must not rewrite a deferred attempt into
    # 'unknown' — deferred is a terminal, owned state.
    E.recover_interrupted_executions()
    assert E.latest_execution(job["id"])["status"] == "deferred"

    # The retry survives in the persisted job record too.
    refreshed = J.get_job(job["id"])
    assert refreshed["last_status"] == "deferred"
    assert refreshed["next_run_at"] == rows[0]["retry_at"]


def test_weekly_occurrence_retained_on_defer(defer_env):
    from cron import deferrals as D
    from cron import jobs as J
    from cron import scheduler as S

    job = defer_env["make_job"]("every 7d")
    due_iso = job["next_run_at"]
    before = datetime.now(timezone.utc)
    S.tick(verbose=False, sync=True)

    obligation = D.pending_deferral(job["id"])
    assert obligation is not None
    # The obligation is keyed to the ORIGINAL weekly occurrence.
    assert obligation["occurrence_key"] == f"{job['id']}:{due_iso}"

    refreshed = J.get_job(job["id"])
    retry_at = datetime.fromisoformat(refreshed["next_run_at"])
    # Contention must not consume the weekly occurrence: retry is minutes
    # away, not next week.
    assert (retry_at - before) < timedelta(hours=1)


def test_obligations_dedupe_by_job_and_occurrence(tmp_path, monkeypatch):
    import cron.executions as executions_mod
    from cron import deferrals as D

    monkeypatch.setattr(
        executions_mod, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )

    first = D.record_defer(
        "job-x", "job-x:2026-08-29T09:00:00+00:00",
        reason="busy", retry_after_seconds=120,
    )
    assert first["state"] == "pending"
    assert first["attempts"] == 0

    # The retry fire carries a DIFFERENT wall-clock occurrence, but must
    # attach to the existing pending obligation — never a second row.
    second = D.record_defer(
        "job-x", "job-x:2026-08-29T09:02:00+00:00",
        reason="still busy", retry_after_seconds=120,
    )
    rows = D.list_deferrals(job_id="job-x")
    assert len(rows) == 1
    assert second["attempts"] == 1
    assert second["occurrence_key"] == "job-x:2026-08-29T09:00:00+00:00"


def test_rollback_of_fresh_obligation_deletes_phantom_row(tmp_path, monkeypatch):
    """Ownership loss after record_defer must not leave a phantom pending
    obligation a replacement owner would then reuse and exhaust."""
    import cron.executions as executions_mod
    from cron import deferrals as D

    monkeypatch.setattr(
        executions_mod, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )

    obligation = D.record_defer(
        "job-r", "job-r:2026-08-29T09:00:00+00:00",
        reason="busy", retry_after_seconds=120,
    )
    assert D.rollback_defer(obligation) is True
    assert D.pending_deferral("job-r") is None
    assert D.list_deferrals(job_id="job-r") == []


def test_rollback_of_escalated_obligation_restores_prior_row(
    tmp_path, monkeypatch
):
    import cron.executions as executions_mod
    from cron import deferrals as D

    monkeypatch.setattr(
        executions_mod, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )

    first = D.record_defer(
        "job-e", "job-e:2026-08-29T09:00:00+00:00",
        reason="busy", retry_after_seconds=120,
    )
    # MAX_DEFER_RETRIES=1: the second defer escalates the row to exhausted.
    second = D.record_defer(
        "job-e", "job-e:2026-08-29T09:05:00+00:00",
        reason="still busy", retry_after_seconds=120,
    )
    assert second["state"] == "exhausted"

    assert D.rollback_defer(second) is True
    restored = D.pending_deferral("job-e")
    assert restored is not None
    assert restored["state"] == "pending"
    assert restored["attempts"] == first["attempts"]
    assert restored["reason"] == first["reason"]
    assert restored["retry_at"] == first["retry_at"]


def test_rollback_declines_when_replacement_moved_the_row(tmp_path, monkeypatch):
    """A stale owner's compensation must never clobber the replacement
    owner's newer obligation state."""
    import cron.executions as executions_mod
    from cron import deferrals as D

    monkeypatch.setattr(
        executions_mod, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )

    stale = D.record_defer(
        "job-c", "job-c:2026-08-29T09:00:00+00:00",
        reason="busy", retry_after_seconds=120,
    )
    replacement = D.record_defer(
        "job-c", "job-c:2026-08-29T09:05:00+00:00",
        reason="replacement defer", retry_after_seconds=120,
    )

    assert D.rollback_defer(stale) is False
    rows = D.list_deferrals(job_id="job-c")
    assert len(rows) == 1
    assert rows[0]["reason"] == replacement["reason"]
    assert rows[0]["attempts"] == replacement["attempts"]


def test_ownership_loss_leaves_no_phantom_deferral(defer_env, monkeypatch):
    """Scheduler wiring: when the fenced job-state handoff reports the fire
    claim was taken over, the just-persisted obligation is compensated away
    before returning — no pending row, execution finished as stale."""
    import cron.jobs as jobs_mod
    from cron import deferrals as D
    from cron import executions as E
    from cron import scheduler as S

    job = defer_env["make_job"]()

    monkeypatch.setattr(
        jobs_mod, "mark_job_deferred", lambda *a, **k: False
    )
    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: None)

    S.tick(verbose=False, sync=True)

    assert D.pending_deferral(job["id"]) is None
    latest = E.latest_execution(job["id"])
    assert latest["status"] == "failed"
    assert "stale defer" in (latest["error"] or "")


def _make_oneshot(defer_env):
    """A finite one-shot job due NOW whose script defers (exit 75)."""
    import cron.jobs as jobs_mod

    future = _iso(datetime.now(timezone.utc) + timedelta(minutes=5))
    job = jobs_mod.create_job(
        prompt="probe", schedule=future, no_agent=True, script="gate.sh"
    )
    due = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    stored = jobs_mod.get_job(job["id"])
    schedule = dict(stored.get("schedule") or {})
    schedule["run_at"] = due
    jobs_mod.update_job(job["id"], {"schedule": schedule, "next_run_at": due})
    return jobs_mod.get_job(job["id"])


def _force_due_oneshot(job_id):
    import cron.jobs as jobs_mod

    due = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    stored = jobs_mod.get_job(job_id)
    schedule = dict(stored.get("schedule") or {})
    schedule["run_at"] = due
    jobs_mod.update_job(job_id, {"schedule": schedule, "next_run_at": due})


def test_one_shot_defer_then_success_completes_without_duplicates(
    defer_env, monkeypatch
):
    """Full one-shot defer lifecycle: claims released, the consumed dispatch
    handed back (repeat restored), run_at rewritten to the bounded retry,
    obligation durable across restart, then ONE terminal completion — never
    a duplicate occurrence."""
    import sqlite3

    from cron import deferrals as D
    from cron import executions as E
    from cron import incidents as I
    from cron import jobs as J
    from cron import scheduler as S

    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: None)
    job = _make_oneshot(defer_env)
    before = datetime.now(timezone.utc)

    S.tick(verbose=False, sync=True)

    refreshed = J.get_job(job["id"])
    assert refreshed["last_status"] == "deferred"
    assert refreshed["state"] == "scheduled"          # still owed to the user
    assert refreshed.get("fire_claim") is None        # claim released
    assert refreshed.get("run_claim") is None
    repeat = refreshed.get("repeat") or {}
    assert int(repeat.get("completed") or 0) == 0     # dispatch handed back

    obligation = D.pending_deferral(job["id"])
    assert obligation is not None and obligation["state"] == "pending"
    # run_at rewrite: schedule + next_run_at both point at the retry time.
    assert refreshed["next_run_at"] == obligation["retry_at"]
    assert (refreshed.get("schedule") or {}).get("run_at") == obligation["retry_at"]
    # Bounded retry: a backoff away, not lost and not immediate.
    retry_at = datetime.fromisoformat(obligation["retry_at"])
    assert timedelta(seconds=59) <= (retry_at - before) <= timedelta(hours=6)

    # Restart durability: everything above is on disk, not module state.
    db = defer_env["home"] / "cron" / "executions.db"
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        disk_rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM cron_deferrals WHERE job_id=?", (job["id"],)
            )
        ]
    finally:
        conn.close()
    assert len(disk_rows) == 1 and disk_rows[0]["state"] == "pending"
    E.recover_interrupted_executions()
    assert E.latest_execution(job["id"])["status"] == "deferred"

    # Not due until the retry time: an interim tick must not fire it.
    S.tick(verbose=False, sync=True)
    assert len(E.list_executions(job_id=job["id"], limit=50)) == 1

    # Contention clears → the retry fire completes the SAME occurrence.
    defer_env["make_script_succeed"]()
    _force_due_oneshot(job["id"])
    S.tick(verbose=False, sync=True)

    final = J.get_job(job["id"])
    assert final["state"] == "completed"
    assert final["last_status"] == "ok"
    assert int((final.get("repeat") or {}).get("completed") or 0) == 1
    resolved = D.list_deferrals(job_id=job["id"])
    assert [r["state"] for r in resolved] == ["completed"]
    records = E.list_executions(job_id=job["id"], limit=50)
    assert sorted(r["status"] for r in records) == ["completed", "deferred"]
    assert I.count_incidents() == 0

    # Terminal: nothing left to fire, ever.
    S.tick(verbose=False, sync=True)
    assert len(E.list_executions(job_id=job["id"], limit=50)) == 2


def test_one_shot_defer_exhaustion_fails_permanently_without_duplicates(
    defer_env, monkeypatch
):
    """The bounded retry budget: a second consecutive defer exhausts the
    obligation and the one-shot terminates through the normal permanent
    failure machinery — incident minted, no further occurrences."""
    from cron import deferrals as D
    from cron import executions as E
    from cron import incidents as I
    from cron import jobs as J
    from cron import scheduler as S

    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: None)
    job = _make_oneshot(defer_env)

    S.tick(verbose=False, sync=True)
    assert (D.pending_deferral(job["id"]) or {}).get("state") == "pending"

    _force_due_oneshot(job["id"])
    S.tick(verbose=False, sync=True)  # still contended → budget exhausted

    rows = D.list_deferrals(job_id=job["id"])
    assert [r["state"] for r in rows] == ["exhausted"]
    final = J.get_job(job["id"])
    # Spent one-shots are retained as terminal 'completed' records (the
    # established inspectable-outcome shape); the FAILURE lives in
    # last_status/last_error, and the defer projection is gone.
    assert final["state"] == "completed"
    assert final["enabled"] is False
    assert final["last_status"] == "error"
    assert "exhausted" in (final.get("last_error") or "")
    assert final.get("last_defer") is None
    records = E.list_executions(job_id=job["id"], limit=50)
    assert sorted(r["status"] for r in records) == ["deferred", "failed"]
    failed = [r for r in records if r["status"] == "failed"][0]
    assert "exhausted" in (failed["error"] or "")
    assert I.count_incidents() == 1

    # Terminal: the spent one-shot never fires again.
    S.tick(verbose=False, sync=True)
    assert len(E.list_executions(job_id=job["id"], limit=50)) == 2
