"""Typed-defer production contract through the REAL ``tick()`` path.

Producer: ``cron.scheduler_script._run_job_script`` surfaces the exit code as ``ScriptResult``;
``cron.outcomes.classify_script_result`` maps exit 75 to TRANSIENT_DEFER BEFORE the ordinary
``fail_closed`` / ``continue`` / ``no_agent`` handling, so a deferred occurrence never reaches the
agent, the failure policy, or the delivery producer.

Consumer: ``cron.deferrals.record_defer`` persists the obligation, ``cron.jobs.mark_job_deferred``
hands the job state over under the fire-claim fence, ``cron.executions.defer_execution`` records the
attempt as DEFERRED; a lost fence compensates the obligation (``rollback_defer``) and finishes the
attempt as a stale failure. Exhaustion is an ORDINARY failure routed through the durable delivery
producer (intent + outbox row + incident), and the logical occurrence key survives the retry.

Complements ``tests/cron/test_deferred_obligations.py`` (mandatory no_agent lifecycle coverage).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest


@pytest.fixture
def defer_env(tmp_path, monkeypatch):
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

    script = hermes_home / "scripts" / "gate.sh"
    script.write_text(
        "#!/bin/bash\necho 'lock contention: another writer holds the lease' >&2\nexit 75\n"
    )

    def force_due(job_id):
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        jobs_mod.update_job(job_id, {"next_run_at": due})
        return jobs_mod.get_job(job_id)

    def make_job(**overrides):
        kwargs = dict(
            prompt="probe",
            schedule="every 10m",
            no_agent=True,
            script="gate.sh",
            deliver="telegram",
        )
        kwargs.update(overrides)
        job = jobs_mod.create_job(**kwargs)
        return force_due(job["id"])

    return {
        "home": hermes_home,
        "make_job": make_job,
        "force_due": force_due,
        "script": script,
    }


def test_agent_lane_defer_is_classified_before_fail_closed_in_a_real_tick(
    defer_env, monkeypatch
):
    import cron.jobs as J
    from cron import deferrals as D
    from cron import executions as E
    from cron import incidents as I
    from cron import outbox as O
    from cron import scheduler as S

    job = defer_env["make_job"](
        no_agent=False,
        prompt="Analyze the collected data.",
        script_failure_policy="fail_closed",
    )
    due_iso = job["next_run_at"]
    monkeypatch.setattr(
        "run_agent.AIAgent",
        Mock(
            side_effect=AssertionError(
                "agent must not be constructed for a deferred occurrence"
            )
        ),
    )
    sent: list = []
    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: sent.append(a) or None)

    S.tick(verbose=False, sync=True)

    obligation = D.pending_deferral(job["id"])
    assert obligation is not None and obligation["state"] == "pending"
    assert obligation["occurrence_key"] == f"{job['id']}:{due_iso}"
    assert "lock contention" in obligation["reason"]
    # Neither the fail-closed alert nor any other delivery intent was produced.
    assert sent == []
    assert O.list_outbox(job_id=job["id"]) == []
    assert I.count_incidents() == 0
    latest = E.latest_execution(job["id"])
    assert latest["status"] == "deferred" and latest["outcome"] == "deferred"
    assert latest["occurrence_key"] == obligation["occurrence_key"]
    # The run's projection generation was admitted for THIS execution but never finalized.
    projection = O.get_job_delivery_projection(job["id"])
    assert projection["execution_id"] == latest["id"]
    assert projection["finalized"] == 0
    refreshed = J.get_job(job["id"])
    assert refreshed["last_status"] == "deferred"
    assert not refreshed.get("last_run_at")
    assert not refreshed.get("failure_streak")


def test_exhausted_defer_fails_through_the_durable_delivery_producer(
    defer_env, monkeypatch
):
    import cron.jobs as J
    from cron import deferrals as D
    from cron import executions as E
    from cron import incidents as I
    from cron import outbox as O
    from cron import scheduler as S

    job = defer_env["make_job"]()
    due_iso = job["next_run_at"]
    sent: list = []
    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: sent.append(a) or None)

    S.tick(verbose=False, sync=True)
    assert D.pending_deferral(job["id"]) is not None
    assert sent == []
    defer_env["force_due"](job["id"])
    S.tick(verbose=False, sync=True)

    rows = D.list_deferrals(job_id=job["id"])
    assert [row["state"] for row in rows] == ["exhausted"]
    # The retry attached to the ORIGINAL logical occurrence, not the retry's wall clock.
    assert rows[0]["occurrence_key"] == f"{job['id']}:{due_iso}"
    assert rows[0]["attempts"] == 1
    # Exhaustion is an ordinary failure: durable intent + outbox row + incident, one send.
    outbox_rows = O.list_outbox(job_id=job["id"])
    assert len(outbox_rows) == 1
    assert outbox_rows[0]["for_failure"] == 1
    assert "exhausted" in outbox_rows[0]["content"].lower()
    assert outbox_rows[0]["state"] == "delivered"
    assert len(sent) == 1
    assert I.count_incidents() == 1
    latest = E.latest_execution(job["id"])
    assert latest["status"] == "failed"
    assert "exhausted" in (latest["error"] or "").lower()
    refreshed = J.get_job(job["id"])
    assert refreshed["last_status"] == "error"
    assert refreshed.get("failure_streak") == 1
    assert refreshed.get("last_defer") is None


def test_stale_owner_defer_compensates_against_a_real_replacement_claim(
    defer_env, monkeypatch
):
    import cron.jobs as J
    from cron import deferrals as D
    from cron import executions as E
    from cron import incidents as I
    from cron import outbox as O
    from cron import scheduler as S

    job = defer_env["make_job"]()
    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: None)
    real_handoff = J.mark_job_deferred
    handoffs: list = []

    def takeover_then_handoff(job_id, retry_at, **kwargs):
        # A replacement owner took the fire claim while this run was busy: the REAL fenced
        # handoff must refuse, and the consumer must compensate the obligation it just wrote.
        J.update_job(
            job_id,
            {
                "fire_claim": {
                    "by": "replacement-owner",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        handoffs.append(kwargs.get("expected_fire_owner"))
        return real_handoff(job_id, retry_at, **kwargs)

    monkeypatch.setattr(J, "mark_job_deferred", takeover_then_handoff)

    S.tick(verbose=False, sync=True)

    assert handoffs and handoffs[0] not in (None, "replacement-owner")
    assert D.pending_deferral(job["id"]) is None
    assert D.list_deferrals(job_id=job["id"]) == []
    latest = E.latest_execution(job["id"])
    assert latest["status"] == "failed"
    assert "stale defer" in (latest["error"] or "")
    assert I.count_incidents() == 0
    assert O.list_outbox(job_id=job["id"]) == []
    assert J.get_job(job["id"])["fire_claim"]["by"] == "replacement-owner"


def test_stale_owner_cannot_exhaust_a_pending_obligation_when_the_claim_moves_after_the_pre_check(
    defer_env, monkeypatch
):
    """Fault interleaving: the fire claim moves to a replacement AFTER any ownership pre-check and
    BEFORE the obligation write. The real pending debt belongs to the replacement; the stale owner's
    second defer must neither exhaust it nor hand the job over, and must compensate what it wrote."""
    import cron.jobs as J
    from cron import deferrals as D
    from cron import executions as E
    from cron import incidents as I
    from cron import outbox as O
    from cron import scheduler as S

    job = defer_env["make_job"]()
    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: None)
    S.tick(verbose=False, sync=True)
    pending = D.pending_deferral(job["id"])
    assert pending is not None and pending["attempts"] == 0

    defer_env["force_due"](job["id"])
    real_record = D.record_defer
    takeovers: list = []

    def takeover_then_record(job_id, occurrence_key, **kwargs):
        # Ownership changes between the consumer's pre-check and its durable obligation write.
        J.update_job(
            job_id,
            {
                "fire_claim": {
                    "by": "replacement-owner",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        takeovers.append(occurrence_key)
        return real_record(job_id, occurrence_key, **kwargs)

    monkeypatch.setattr(D, "record_defer", takeover_then_record)

    S.tick(verbose=False, sync=True)

    assert takeovers, "the retry fire never reached the obligation write"
    after = D.pending_deferral(job["id"])
    assert after is not None, (
        "stale owner exhausted the replacement's pending obligation"
    )
    assert after["id"] == pending["id"]
    assert after["attempts"] == pending["attempts"] == 0
    assert after["state"] == "pending"
    assert [row["state"] for row in D.list_deferrals(job_id=job["id"])] == ["pending"]
    latest = E.latest_execution(job["id"])
    assert latest["status"] == "failed"
    assert "stale defer" in (latest["error"] or "")
    # No exhaustion side effects: no incident, no failure intent, no handoff by the stale owner.
    assert I.count_incidents() == 0
    assert O.list_outbox(job_id=job["id"]) == []
    refreshed = J.get_job(job["id"])
    assert refreshed["fire_claim"]["by"] == "replacement-owner"
    assert not refreshed.get("failure_streak")


def test_exhaustion_is_compensated_when_the_fenced_terminal_write_loses_the_claim(
    defer_env, monkeypatch
):
    """Ownership moves AFTER the exhausted branch's own fence passed: the authoritative owner-fenced
    terminal write refuses, and the exhausted debt must be restored to the replacement's pending row."""
    import cron.jobs as J
    from cron import deferrals as D
    from cron import executions as E
    from cron import scheduler as S

    job = defer_env["make_job"]()
    monkeypatch.setattr(S, "_deliver_result", lambda *a, **k: None)
    S.tick(verbose=False, sync=True)
    pending = D.pending_deferral(job["id"])
    assert pending is not None and pending["attempts"] == 0

    defer_env["force_due"](job["id"])
    # The scheduler binds ``mark_job_run`` by name at import; the fenced write is intercepted at
    # the scheduler seam and still performed by the REAL owner-fenced function.
    real_mark = S.mark_job_run
    marks: list = []

    def takeover_then_mark(job_id, success, error=None, **kwargs):
        J.update_job(
            job_id,
            {
                "fire_claim": {
                    "by": "replacement-owner",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        marks.append(kwargs.get("expected_fire_owner"))
        return real_mark(job_id, success, error, **kwargs)

    monkeypatch.setattr(S, "mark_job_run", takeover_then_mark)
    S.tick(verbose=False, sync=True)

    assert marks and marks[0] not in (None, "replacement-owner")
    after = D.pending_deferral(job["id"])
    assert after is not None, (
        "exhaustion survived a terminal write the stale owner lost"
    )
    assert after["id"] == pending["id"] and after["attempts"] == 0
    latest = E.latest_execution(job["id"])
    assert latest["status"] == "failed"
    assert "ownership lost" in (latest["error"] or "").lower()
    assert J.get_job(job["id"])["fire_claim"]["by"] == "replacement-owner"


def test_delayed_old_completion_resolves_only_its_own_occurrence(defer_env):
    """Terminal resolution must be keyed on the completing run's logical occurrence: a delayed old
    completion cannot settle an obligation a NEWER admitted occurrence owns, while the retry fire of
    the deferred occurrence (which carries the obligation key on its dispatched record) does."""
    from cron import deferrals as D
    from cron.scheduler_outcomes import resolve_deferral_at_terminal

    job = defer_env["make_job"]()
    newer = D.record_defer(
        job["id"],
        f"{job['id']}:2026-10-03T12:00:00+00:00",
        reason="transient",
        retry_after_seconds=5,
    )
    assert newer["state"] == "pending"

    old_run = dict(job, _scheduled_instant="2026-10-03T11:50:00+00:00")
    old_run.pop("last_defer", None)
    resolve_deferral_at_terminal(old_run, success=True)
    survivor = D.pending_deferral(job["id"])
    assert survivor is not None, (
        "a delayed old completion resolved a newer occurrence's debt"
    )
    assert survivor["id"] == newer["id"]

    retry_run = dict(
        job,
        _scheduled_instant="2026-10-03T12:05:00+00:00",
        last_defer={"occurrence_key": newer["occurrence_key"]},
    )
    resolve_deferral_at_terminal(retry_run, success=True)
    assert D.pending_deferral(job["id"]) is None
    assert [row["state"] for row in D.list_deferrals(job_id=job["id"])] == ["completed"]
