"""Typed pre-script outcomes: SUCCESS / TRANSIENT_DEFER / PERMANENT_FAIL / SKIP.

Contract under test (recurring-automation P1):

* ``cron.outcomes.classify_script_result`` turns a raw script result into a
  typed outcome. Exit code 75 (BSD ``EX_TEMPFAIL``) maps to TRANSIENT_DEFER
  for backward compatibility with pre-existing defer-aware scripts.
* A defer is neither success nor failure: ``run_job`` must surface it as a
  typed ``CronPreScriptDefer`` marker instead of routing it through the
  script-failure policy (``fail_closed`` alerting / ``continue`` prompt
  injection).
* ``_run_job_script`` surfaces the subprocess exit code so classification
  does not depend on parsing error prose.
"""

from __future__ import annotations

import json
from unittest.mock import patch


def _agent_job(policy: str = "fail_closed") -> dict:
    return {
        "id": "typed-outcome-job",
        "name": "typed outcome job",
        "prompt": "Analyze the collected data.",
        "script": "gate.py",
        "script_failure_policy": policy,
        "next_run_at": "2026-08-29T09:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Classification layer (cron/outcomes.py)
# ---------------------------------------------------------------------------


def test_exit_75_classifies_as_transient_defer():
    from cron.outcomes import TRANSIENT_DEFER, classify_script_result

    outcome = classify_script_result(
        False,
        "Script exited with code 75\nstderr:\nlock held by another process",
        returncode=75,
        occurrence_key="job:2026-08-29T09:00:00+00:00",
    )
    assert outcome.kind == TRANSIENT_DEFER
    assert outcome.occurrence_key == "job:2026-08-29T09:00:00+00:00"
    assert "lock held" in outcome.reason
    assert outcome.retry_after_seconds > 0


def test_other_nonzero_exit_classifies_as_permanent_fail():
    from cron.outcomes import PERMANENT_FAIL, classify_script_result

    outcome = classify_script_result(
        False, "Script exited with code 3", returncode=3
    )
    assert outcome.kind == PERMANENT_FAIL
    assert "code 3" in outcome.reason


def test_clean_exit_classifies_as_success():
    from cron.outcomes import SUCCESS, classify_script_result

    outcome = classify_script_result(True, "collected 5 rows", returncode=0)
    assert outcome.kind == SUCCESS


def test_json_directive_transient_defer_with_bounded_retry_after():
    from cron.outcomes import (
        MAX_RETRY_AFTER_SECONDS,
        MIN_RETRY_AFTER_SECONDS,
        TRANSIENT_DEFER,
        classify_script_result,
    )

    directive = json.dumps(
        {"outcome": "transient_defer", "retry_after": 600, "reason": "API 429"}
    )
    outcome = classify_script_result(True, f"partial data\n{directive}", returncode=0)
    assert outcome.kind == TRANSIENT_DEFER
    assert outcome.retry_after_seconds == 600
    assert outcome.reason == "API 429"

    # retry_after is clamped, never trusted verbatim.
    low = json.dumps({"outcome": "transient_defer", "retry_after": 1})
    high = json.dumps({"outcome": "transient_defer", "retry_after": 10**9})
    assert (
        classify_script_result(True, low, returncode=0).retry_after_seconds
        == MIN_RETRY_AFTER_SECONDS
    )
    assert (
        classify_script_result(True, high, returncode=0).retry_after_seconds
        == MAX_RETRY_AFTER_SECONDS
    )


def test_json_directive_permanent_fail_and_skip():
    from cron.outcomes import PERMANENT_FAIL, SKIP, classify_script_result

    fail = json.dumps({"outcome": "permanent_fail", "reason": "bad credentials"})
    outcome = classify_script_result(True, fail, returncode=0)
    assert outcome.kind == PERMANENT_FAIL
    assert outcome.reason == "bad credentials"

    skip = json.dumps({"outcome": "skip"})
    assert classify_script_result(True, skip, returncode=0).kind == SKIP
    # Legacy wake gate is honoured as SKIP too.
    legacy = json.dumps({"wakeAgent": False})
    assert classify_script_result(True, legacy, returncode=0).kind == SKIP


def test_plain_tuple_without_returncode_keeps_legacy_semantics():
    """Patched/mocked 2-tuples (no returncode) must never classify as defer."""
    from cron.outcomes import PERMANENT_FAIL, SUCCESS, classify_script_result

    assert classify_script_result(True, "ok").kind == SUCCESS
    assert (
        classify_script_result(False, "Script exited with code 75").kind
        == PERMANENT_FAIL
    )


# ---------------------------------------------------------------------------
# Exit-code surfacing from the real script runner
# ---------------------------------------------------------------------------


def test_run_job_script_surfaces_exit_code(tmp_path, monkeypatch):
    import cron.scheduler as scheduler

    home = tmp_path / "hermes-home"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    script = scripts / "defer.sh"
    script.write_text("#!/bin/bash\necho 'busy: lock held'\nexit 75\n")

    result = scheduler._run_job_script(str(script))
    ok, output = result  # 2-tuple unpack contract must survive
    assert ok is False
    assert getattr(result, "returncode", None) == 75


# ---------------------------------------------------------------------------
# run_job plumbing: defer is neither success nor failure
# ---------------------------------------------------------------------------


def _defer_script_result():
    from cron.scheduler import ScriptResult

    return ScriptResult(
        False,
        "Script exited with code 75\nstderr:\ntransient: upstream lock",
        returncode=75,
    )


def test_run_job_surfaces_typed_defer_instead_of_fail_closed():
    from cron.outcomes import CronPreScriptDefer
    from cron.scheduler import run_job

    with patch(
        "cron.scheduler._run_job_script_with_claim_heartbeat",
        return_value=_defer_script_result(),
    ):
        success, _doc, final_response, error = run_job(_agent_job("fail_closed"))

    assert success is False
    assert isinstance(error, CronPreScriptDefer)
    assert error.retry_after_seconds > 0
    assert error.occurrence_key.endswith("2026-08-29T09:00:00+00:00")
    # The fail-closed alert path must NOT have been taken.
    assert "fail-closed" not in (final_response or "").lower()


def test_run_job_defer_wins_over_continue_policy():
    """Legacy ``continue`` policy injects failures into the prompt; a defer
    must never reach the agent at all."""
    from cron.outcomes import CronPreScriptDefer
    from cron.scheduler import run_job

    with patch(
        "cron.scheduler._run_job_script_with_claim_heartbeat",
        return_value=_defer_script_result(),
    ), patch("run_agent.AIAgent") as agent_cls:
        success, _doc, _resp, error = run_job(_agent_job("continue"))

    assert success is False
    assert isinstance(error, CronPreScriptDefer)
    agent_cls.assert_not_called()


def test_run_job_no_agent_defer_is_typed_not_watchdog_alert():
    from cron.outcomes import CronPreScriptDefer
    from cron.scheduler import run_job

    job = {
        "id": "typed-outcome-noagent",
        "name": "watchdog",
        "script": "watch.sh",
        "no_agent": True,
        "next_run_at": "2026-08-29T09:00:00+00:00",
    }
    with patch(
        "cron.scheduler._run_job_script_with_claim_heartbeat",
        return_value=_defer_script_result(),
    ):
        success, _doc, final_response, error = run_job(job)

    assert success is False
    assert isinstance(error, CronPreScriptDefer)
    assert "watchdog" not in (final_response or "").lower() or not final_response


def test_json_directive_detached_carries_validated_metadata():
    """DETACHED (RUN_STARTED): the script launched a detached worker and
    hands the scheduler a correlation id, worker metadata, and a bounded
    reconciliation deadline; the logical occurrence travels with it."""
    from cron.outcomes import (
        DETACHED,
        MAX_DETACHED_LEASE_SECONDS,
        classify_script_result,
    )

    directive = json.dumps({
        "outcome": "detached",
        "run_id": "auto-update-2026-08-29",
        "lease_seconds": 999_999,  # absurd → clamped to the bound
        "worker": "update-runner@host:4242",
    })
    outcome = classify_script_result(
        True,
        f"worker launched\n{directive}",
        returncode=0,
        occurrence_key="job:2026-08-29T09:00:00+00:00",
    )
    assert outcome.kind == DETACHED
    assert outcome.run_id == "auto-update-2026-08-29"
    assert outcome.worker == "update-runner@host:4242"
    assert outcome.lease_seconds == MAX_DETACHED_LEASE_SECONDS
    assert outcome.occurrence_key == "job:2026-08-29T09:00:00+00:00"


def test_detached_directive_without_run_id_fails_closed():
    """A detached worker nobody can correlate is an unreconcilable run —
    fail closed with a visible reason instead of leaking a ghost worker."""
    from cron.outcomes import PERMANENT_FAIL, classify_script_result

    directive = json.dumps({"outcome": "detached", "worker": "w@h:1"})
    outcome = classify_script_result(True, directive, returncode=0)
    assert outcome.kind == PERMANENT_FAIL
    assert "run_id" in outcome.reason


def test_detached_directive_with_invalid_run_id_fails_closed():
    from cron.outcomes import PERMANENT_FAIL, classify_script_result

    directive = json.dumps(
        {"outcome": "detached", "run_id": "bad id with spaces\n"}
    )
    outcome = classify_script_result(True, directive, returncode=0)
    assert outcome.kind == PERMANENT_FAIL
    assert "run_id" in outcome.reason


def test_detached_lease_floor_and_default():
    from cron.outcomes import (
        DEFAULT_DETACHED_LEASE_SECONDS,
        DETACHED,
        MIN_DETACHED_LEASE_SECONDS,
        classify_script_result,
    )

    floor = classify_script_result(
        True,
        json.dumps({"outcome": "detached", "run_id": "r1", "lease_seconds": 1}),
        returncode=0,
    )
    assert floor.kind == DETACHED
    assert floor.lease_seconds == MIN_DETACHED_LEASE_SECONDS

    default = classify_script_result(
        True,
        json.dumps({"outcome": "detached", "run_id": "r2"}),
        returncode=0,
    )
    assert default.kind == DETACHED
    assert default.lease_seconds == DEFAULT_DETACHED_LEASE_SECONDS
