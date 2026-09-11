"""Typed outcomes / delivery / detached state through the list serializers.

Contract under test (recurring-automation P1/P2, requirement 8): the
EXISTING surfaces — the ``cronjob`` tool's job serializer, ``hermes cron
list`` and ``hermes cron runs`` — expose the new durable facts (typed
outcome, retry occurrence, delivery history, detached state) without
leaking secrets that delivery errors may carry.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron").mkdir(parents=True)
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
    return hermes_home


def test_format_job_exposes_defer_retry_occurrence(cron_env):
    from cron.jobs import create_job, get_job, mark_job_deferred
    from tools.cronjob_tools import _format_job

    job = create_job(prompt="probe", schedule="every 1h")
    mark_job_deferred(
        job["id"],
        "2026-08-29T09:05:00+00:00",
        reason="lock contention",
        occurrence_key=f"{job['id']}:2026-08-29T09:00:00+00:00",
        attempts=0,
    )

    formatted = _format_job(get_job(job["id"]))
    assert formatted["last_status"] == "deferred"
    defer = formatted["last_defer"]
    assert defer["occurrence_key"].endswith("2026-08-29T09:00:00+00:00")
    assert defer["retry_at"] == "2026-08-29T09:05:00+00:00"
    assert defer["attempts"] == 0


def test_cron_runs_prints_outcome_delivery_and_detached(cron_env, capsys):
    from cron import executions as E
    from hermes_cli.cron import cron_runs

    deferred = E.create_execution("job-serial", source="builtin")
    E.defer_execution(
        deferred["id"],
        reason="upstream busy",
        occurrence_key="job-serial:2026-08-29T09:00:00+00:00",
        retry_at="2026-08-29T09:05:00+00:00",
    )

    delivered = E.create_execution("job-serial", source="builtin")
    E.finish_execution(delivered["id"], success=True)
    E.record_delivery(
        delivered["id"],
        target="telegram",
        status="failed",
        error="401 with token=sk-abc123def456ghi789jkl012mno345pqr678",
    )

    detached = E.create_execution("job-serial", source="builtin")
    E.register_detached_run(detached["id"], run_id="run-42", lease_seconds=3600)

    cron_runs("job-serial", limit=10)
    out = capsys.readouterr().out

    assert "deferred" in out
    assert "job-serial:2026-08-29T09:00:00+00:00" in out
    assert "telegram" in out
    assert "attempt" in out.lower()
    assert "detached" in out.lower()
    assert "run-42" in out
    # No raw secrets through the serializer.
    assert "sk-abc123def456ghi789jkl012mno345pqr678" not in out


def test_cron_list_shows_deferred_retry(cron_env, capsys):
    from cron.jobs import create_job, mark_job_deferred
    from hermes_cli.cron import cron_list

    job = create_job(prompt="probe", schedule="every 1h", name="defer-viz")
    mark_job_deferred(
        job["id"],
        "2026-08-29T09:05:00+00:00",
        reason="lock contention",
        occurrence_key=f"{job['id']}:2026-08-29T09:00:00+00:00",
        attempts=0,
    )

    cron_list(show_all=True)
    out = capsys.readouterr().out
    assert "defer-viz" in out
    assert "deferred" in out
    assert "09:05" in out or "retry" in out.lower()


_EXPECTED_EXECUTION_KEYS = {
    "id", "status", "outcome", "source",
    "claimed_at", "started_at", "finished_at", "error",
    "occurrence_key", "retry_at",
    "delivery_target", "delivery_status", "delivery_attempts",
    "delivery_error",
    "detached_run_id", "detached_status", "detached_worker",
    "lease_expires_at",
}


def test_format_job_exposes_latest_execution_exact_schema(cron_env):
    """The cronjob tool serializer carries the job's latest durable
    execution with EXACTLY the public outcome/occurrence/delivery/detached
    fields — no internal process columns, nothing missing."""
    import json

    from cron import executions as E
    from cron.jobs import create_job, get_job
    from tools.cronjob_tools import _format_job

    job = create_job(prompt="probe", schedule="every 1h")
    row = E.create_execution(job["id"], source="builtin")
    E.mark_execution_running(row["id"])
    E.register_detached_run(
        row["id"],
        run_id="run-9",
        lease_seconds=600,
        worker="update-runner@host:7",
        occurrence_key=f"{job['id']}:2026-08-29T09:00:00+00:00",
    )
    E.record_delivery(
        row["id"],
        target="telegram",
        status="failed",
        error="401 with token=sk-abc123def456ghi789jkl012mno345pqr678",
    )

    latest = _format_job(get_job(job["id"]))["latest_execution"]
    assert set(latest) == _EXPECTED_EXECUTION_KEYS
    assert latest["status"] == "running"
    assert latest["occurrence_key"].endswith("2026-08-29T09:00:00+00:00")
    assert latest["delivery_target"] == "telegram"
    assert latest["delivery_status"] == "failed"
    assert latest["delivery_attempts"] == 1
    assert latest["detached_run_id"] == "run-9"
    assert latest["detached_status"] == "started"
    assert latest["detached_worker"] == "update-runner@host:7"
    assert latest["lease_expires_at"]
    assert "sk-abc123def456ghi789jkl012mno345pqr678" not in json.dumps(latest)


def test_format_job_latest_execution_redacts_run_error(cron_env):
    """The run error is redacted AT SERIALIZATION — the ledger stores it
    raw for local forensics, but the tool surface must never leak it."""
    from cron import executions as E
    from cron.jobs import create_job, get_job
    from tools.cronjob_tools import _format_job

    job = create_job(prompt="probe", schedule="every 1h")
    row = E.create_execution(job["id"], source="builtin")
    E.finish_execution(
        row["id"],
        success=False,
        error="deploy failed: api_key=sk-abc123def456ghi789jkl012 rejected",
    )

    latest = _format_job(get_job(job["id"]))["latest_execution"]
    assert latest["status"] == "failed"
    assert "sk-abc123def456ghi789jkl012" not in (latest["error"] or "")
    assert latest["error"]  # evidence survives, redacted


def test_format_job_without_history_has_null_latest_execution(cron_env):
    from cron.jobs import create_job, get_job
    from tools.cronjob_tools import _format_job

    job = create_job(prompt="probe", schedule="every 1h")
    assert _format_job(get_job(job["id"]))["latest_execution"] is None
