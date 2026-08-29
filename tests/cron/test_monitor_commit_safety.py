"""Monitor dedup-hash commit safety: no commit before preflight succeeds.

Contract under test (recurring-automation P1/P2, requirement 7):

* The monitor's change-dedup hash must NOT be committed at detection time.
  It commits only after the provider/model/tool preflight succeeds and the
  durable work intent exists — i.e. immediately before the agent actually
  runs.
* A failed preflight (or a provider-resolution crash) therefore leaves the
  detected change fully retryable: the next tick re-detects and re-alerts,
  with the LATEST output.
* The original boundary survives: once committed (pre-agent), a failed
  agent run does not re-alert forever on the same content.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib

    import hermes_constants

    importlib.reload(hermes_constants)
    import cron.jobs

    importlib.reload(cron.jobs)
    import cron.monitor

    importlib.reload(cron.monitor)
    import cron.scheduler

    importlib.reload(cron.scheduler)
    return home


def _write_script(home, name: str, body: str) -> str:
    path = home / "scripts" / name
    path.write_text(body, encoding="utf-8")
    return name


def _make_monitor_job(home, script_body: str):
    from cron.jobs import create_job

    _write_script(home, "mon.sh", script_body)
    return create_job(
        prompt="Summarize what changed",
        schedule="every 5m",
        monitor_script="mon.sh",
        deliver="local",
    )


def _install_agent_stubs(monkeypatch, observed: dict, *, agent_raises=False):
    import cron.scheduler as sched

    observed.setdefault("prompts", [])
    observed.setdefault("agent_runs", 0)

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, prompt, *_a, **_kw):
            observed["agent_runs"] += 1
            observed["prompts"].append(prompt)
            if agent_raises:
                raise RuntimeError("agent exploded mid-run")
            return {"final_response": "agent done", "messages": []}

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0}

    fake_mod = type(sys)("run_agent")
    fake_mod.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

    from hermes_cli import runtime_provider as _rtp

    monkeypatch.setattr(
        _rtp,
        "resolve_runtime_provider",
        lambda **_kw: {
            "provider": "test",
            "api_key": "k",
            "base_url": "http://test.local",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(sched, "_resolve_origin", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
    monkeypatch.setattr(
        sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None
    )
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: True)


def _stored_hash(job_id):
    from cron.jobs import get_job

    state = (get_job(job_id) or {}).get("monitor_state") or {}
    return state.get("last_output_hash")


def test_failed_preflight_leaves_hash_uncommitted_and_retryable(
    hermes_env, monkeypatch
):
    import cron.scheduler as sched
    from cron.jobs import get_job

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)
    monkeypatch.setattr(sched, "_cron_preflight_enabled", lambda cfg: True)
    monkeypatch.setattr(
        sched, "_preflight_job_config", lambda job, cfg: "provider key missing"
    )

    success, _doc, _final, error = sched.run_job(job)
    assert success is False
    assert "provider key missing" in str(error)
    assert observed["agent_runs"] == 0
    # THE contract: the change was detected but must NOT be committed.
    assert _stored_hash(job["id"]) is None

    # Preflight heals → the same change re-detects and the agent runs.
    monkeypatch.setattr(sched, "_preflight_job_config", lambda job, cfg: None)
    job = get_job(job["id"])
    success, _doc, _final, error = sched.run_job(job)
    assert success is True
    assert observed["agent_runs"] == 1
    assert "state A" in observed["prompts"][0]
    assert _stored_hash(job["id"])


def test_provider_resolution_crash_leaves_hash_uncommitted(
    hermes_env, monkeypatch
):
    import cron.scheduler as sched
    from cron.jobs import get_job

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    from hermes_cli import runtime_provider as _rtp

    def _boom(**_kw):
        raise RuntimeError("provider registry unavailable")

    monkeypatch.setattr(_rtp, "resolve_runtime_provider", _boom)

    success, _doc, _final, _error = sched.run_job(job)
    assert success is False
    assert observed["agent_runs"] == 0
    assert _stored_hash(job["id"]) is None

    # Recovery re-detects the pending change.
    _install_agent_stubs(monkeypatch, observed)
    job = get_job(job["id"])
    success, _doc, _final, _error = sched.run_job(job)
    assert success is True
    assert observed["agent_runs"] == 1
    assert _stored_hash(job["id"])


def test_drift_between_failed_attempts_alerts_with_latest_output(
    hermes_env, monkeypatch
):
    """Monitor drift across a failed preflight: the retried alert must carry
    the LATEST output, and the committed hash must match it."""
    import cron.monitor as monitor
    import cron.scheduler as sched
    from cron.jobs import get_job

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)
    monkeypatch.setattr(sched, "_cron_preflight_enabled", lambda cfg: True)
    monkeypatch.setattr(
        sched, "_preflight_job_config", lambda job, cfg: "config broken"
    )
    sched.run_job(job)
    assert _stored_hash(job["id"]) is None

    # The source drifts further while the config is broken.
    _write_script(hermes_env, "mon.sh", "echo 'state B'\n")
    monkeypatch.setattr(sched, "_preflight_job_config", lambda job, cfg: None)
    job = get_job(job["id"])
    success, _doc, _final, _error = sched.run_job(job)
    assert success is True
    assert observed["agent_runs"] == 1
    assert "state B" in observed["prompts"][0]
    assert _stored_hash(job["id"]) == monitor.hash_monitor_output("state B")


def test_committed_hash_still_prevents_realert_after_agent_crash(
    hermes_env, monkeypatch
):
    """The pre-agent commit boundary survives: an agent crash AFTER commit
    must not re-alert forever on the same content."""
    import cron.scheduler as sched
    from cron.jobs import get_job
    from cron.scheduler import SILENT_MARKER

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, agent_raises=True)

    success, _doc, _final, _error = sched.run_job(job)
    assert success is False
    assert observed["agent_runs"] == 1
    assert _stored_hash(job["id"])  # committed pre-agent

    # Same content on the next tick → suppressed, no re-alert loop.
    _install_agent_stubs(monkeypatch, observed)
    job = get_job(job["id"])
    success, _doc, final, _error = sched.run_job(job)
    assert success is True
    assert final == SILENT_MARKER
    assert observed["agent_runs"] == 1


def test_hash_update_failure_fails_the_run_visibly(hermes_env, monkeypatch):
    """A monitor commit that cannot write the jobs-store hash must FAIL the
    run (no swallow, no agent) and leave the change fully retryable."""
    import cron.jobs
    import cron.scheduler as sched

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    real_update = cron.jobs.update_job

    def flaky_update(job_id, fields, *args, **kwargs):
        if "monitor_state" in fields:
            raise RuntimeError("jobs store write failed")
        return real_update(job_id, fields, *args, **kwargs)

    monkeypatch.setattr(cron.jobs, "update_job", flaky_update)

    success, _doc, _final, error = sched.run_job(job)
    assert success is False  # visible failure, not a swallowed warning
    assert "jobs store write failed" in str(error)
    assert observed["agent_runs"] == 0  # the run did not proceed

    # Store heals → the same change re-detects and commits.
    monkeypatch.setattr(cron.jobs, "update_job", real_update)
    from cron.jobs import get_job

    job = get_job(job["id"])
    success, _doc, _final, _error = sched.run_job(job)
    assert success is True
    assert observed["agent_runs"] == 1
    assert _stored_hash(job["id"])


def test_snapshot_write_failure_fails_the_run_and_stays_retryable(
    hermes_env, monkeypatch
):
    """The snapshot file is the commit authority: if it cannot be written,
    the commit did not happen — the run fails visibly and the change
    re-detects next tick even though the jobs-store mirror already moved."""
    import cron.monitor as monitor
    import cron.scheduler as sched

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    real_write = monitor._write_last_output
    outage = {"active": True}

    def broken_write(job_id, output):
        if outage["active"]:
            raise OSError("disk full")
        return real_write(job_id, output)

    monkeypatch.setattr(monitor, "_write_last_output", broken_write)
    success, _doc, _final, error = sched.run_job(job)
    assert success is False
    assert "disk full" in str(error)
    assert observed["agent_runs"] == 0

    # Snapshot store heals → authority still says "never seen" → re-alert.
    outage["active"] = False
    from cron.jobs import get_job

    job = get_job(job["id"])
    success, _doc, _final, _error = sched.run_job(job)
    assert success is True
    assert observed["agent_runs"] == 1
    assert "state A" in observed["prompts"][0]


def test_torn_two_resource_state_converges_on_snapshot_authority(
    hermes_env, monkeypatch
):
    """Crash-recovery convergence at both torn boundaries: whatever the
    jobs-store mirror claims, the SNAPSHOT decides.

    * mirror=new / snapshot=old (crash between journal and commit) →
      still a change: the alert was never committed, so it re-fires;
    * mirror=old / snapshot=new (mirror write lost after commit) →
      no change: the commit happened, no re-alert loop.
    """
    import cron.monitor as monitor
    from cron.jobs import update_job

    job = _make_monitor_job(hermes_env, "echo 'state B'\n")
    job_id = job["id"]

    # Boundary 1: mirror already says B, snapshot still holds A.
    monitor._write_last_output(job_id, "state A")
    update_job(job_id, {"monitor_state": {
        "last_output_hash": monitor.hash_monitor_output("state B"),
        "last_changed_at": "2026-08-29T00:00:00+00:00",
    }})
    from cron.jobs import get_job

    outcome = monitor.check_monitor(get_job(job_id))
    assert outcome.ok
    assert outcome.changed is True

    # Boundary 2: snapshot committed B, mirror still says A.
    monitor._write_last_output(job_id, "state B")
    update_job(job_id, {"monitor_state": {
        "last_output_hash": monitor.hash_monitor_output("state A"),
        "last_changed_at": "2026-08-29T00:00:00+00:00",
    }})
    outcome = monitor.check_monitor(get_job(job_id))
    assert outcome.ok
    assert outcome.changed is False
