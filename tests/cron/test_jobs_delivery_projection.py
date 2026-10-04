"""Contracts between ``cron.jobs`` and the outbox delivery projection.

``cron/scheduler_delivery.py::_record_delivery_verification`` passes
``expected_execution_id`` / ``expected_projection_revision`` to ``update_job`` so a late
bookkeeping write from a superseded execution cannot clobber the current one; the kwargs
must therefore be accepted (no ``TypeError``) AND enforced (a stale CAS token writes
nothing). ``get_job`` overlays the FINALIZED projection so readers never see the stale
``jobs.json`` delivery status after the outbox has settled a run.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron store + executions DB with one recurring job."""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron" / "output").mkdir(parents=True)
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

    job = jobs_mod.create_job(prompt="report", schedule="every 10m")
    return job["id"]


def _stored(job_id: str) -> dict:
    """The raw ``jobs.json`` record, bypassing any read-side overlay."""
    from cron.jobs import load_jobs

    return next(j for j in load_jobs() if j["id"] == job_id)


class TestUpdateJobCompareAndSwap:
    def test_matching_execution_and_revision_apply_the_write(self, cron_env):
        from cron.jobs import update_job
        from cron.outbox import begin_job_delivery_projection

        projection = begin_job_delivery_projection(cron_env, "exec-1")
        result = update_job(
            cron_env,
            {"last_delivery_unverified": ["slack:C1"]},
            expected_execution_id="exec-1",
            expected_projection_revision=projection["revision"],
        )
        assert result is not None
        assert _stored(cron_env)["last_delivery_unverified"] == ["slack:C1"]

    def test_a_superseded_execution_id_writes_nothing(self, cron_env):
        from cron.jobs import update_job
        from cron.outbox import begin_job_delivery_projection

        begin_job_delivery_projection(cron_env, "exec-1")
        live = begin_job_delivery_projection(cron_env, "exec-2")
        result = update_job(
            cron_env,
            {"last_delivery_unverified": ["slack:C1"]},
            expected_execution_id="exec-1",
            expected_projection_revision=live["revision"],
        )
        assert result is None
        assert _stored(cron_env).get("last_delivery_unverified") is None

    def test_a_stale_projection_revision_writes_nothing(self, cron_env):
        from cron.jobs import update_job
        from cron.outbox import (
            begin_job_delivery_projection,
            finalize_job_delivery_projection,
        )

        started = begin_job_delivery_projection(cron_env, "exec-1")
        assert finalize_job_delivery_projection(
            cron_env,
            execution_id="exec-1",
            expected_revision=started["revision"],
            last_status="ok",
            last_delivery_error=None,
        )
        result = update_job(
            cron_env,
            {"last_delivery_unverified": ["slack:C1"]},
            expected_execution_id="exec-1",
            expected_projection_revision=started["revision"],
        )
        assert result is None
        assert _stored(cron_env).get("last_delivery_unverified") is None

    def test_cas_without_a_projection_writes_nothing_but_plain_updates_still_apply(
        self, cron_env
    ):
        from cron.jobs import update_job

        assert (
            update_job(
                cron_env,
                {"last_delivery_unverified": ["slack:C1"]},
                expected_execution_id="exec-1",
            )
            is None
        )
        assert _stored(cron_env).get("last_delivery_unverified") is None
        assert update_job(cron_env, {"last_delivery_unverified": ["slack:C1"]}) is not None
        assert _stored(cron_env)["last_delivery_unverified"] == ["slack:C1"]


class TestGetJobProjectionOverlay:
    def test_finalized_projection_overrides_the_stored_delivery_status(self, cron_env):
        from cron.jobs import get_job, mark_job_run
        from cron.outbox import (
            begin_job_delivery_projection,
            finalize_job_delivery_projection,
        )

        mark_job_run(cron_env, success=True)
        assert _stored(cron_env)["last_status"] == "ok"
        started = begin_job_delivery_projection(cron_env, "exec-1")
        assert finalize_job_delivery_projection(
            cron_env,
            execution_id="exec-1",
            expected_revision=started["revision"],
            last_status="delivery_failed",
            last_delivery_error="send failed: 502",
            last_delivery_unverified=["slack:C1"],
        )

        seen = get_job(cron_env)
        assert seen["last_status"] == "delivery_failed"
        assert seen["last_delivery_error"] == "send failed: 502"
        assert seen["last_delivery_unverified"] == ["slack:C1"]
        # The overlay is read-side only: the store itself is untouched.
        assert _stored(cron_env)["last_status"] == "ok"

    def test_an_unfinalized_projection_leaves_the_stored_status_visible(self, cron_env):
        from cron.jobs import get_job, mark_job_run
        from cron.outbox import begin_job_delivery_projection

        mark_job_run(cron_env, success=True)
        begin_job_delivery_projection(cron_env, "exec-1")
        assert get_job(cron_env)["last_status"] == "ok"
