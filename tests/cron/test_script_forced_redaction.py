"""Forced redaction of cron pre-run script results (``cron.script_redaction``, fork-only).

Every ``(success, output)`` the script runner yields is redacted with ``force=True`` and
``redact_url_credentials=True``: with ``security.redact_secrets`` switched off, on the early
path-resolution returns, on a launch exception, on a timeout, and when the redactor itself
fails. An unusable Hermes home is refused with a constant that names no path.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest


def test_script_runner_forces_strict_redaction_when_global_setting_is_disabled(
    tmp_path, monkeypatch
):
    from agent import redact
    from cron.scheduler_script import _run_job_script

    hermes_home = tmp_path / ".hermes"
    scripts_dir = hermes_home / "scripts"
    scripts_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    api_token = "sk-runnersecret0123456789"
    password = "runner-password-123"
    url_password = "runner-url-password"
    url_token = "runner-url-token"
    payload = (
        f"OPENAI_API_KEY={api_token}\n"
        f'{{"password": "{password}"}}\n'
        f"https://user:{url_password}@example.invalid/path?token={url_token}"
    )
    (scripts_dir / "leak.py").write_text(
        f"print({payload!r})\n", encoding="utf-8"
    )

    success, output = _run_job_script("leak.py")

    assert success is True
    for secret in (api_token, password, url_password, url_token):
        assert secret not in output
    assert "***" in output


@pytest.mark.parametrize(
    ("script_path", "secrets"),
    [
        pytest.param(
            "../https://operator:traversal-password@example.invalid/gate.py"
            "?token=traversal-query-token",
            ("traversal-password", "traversal-query-token"),
            id="traversal-url-credentials",
        ),
        pytest.param(
            "missing.py?token=missing-query-token",
            ("missing-query-token",),
            id="missing-query-credential",
        ),
    ],
)
def test_script_runner_redacts_credentials_from_early_path_failures(
    tmp_path, monkeypatch, script_path, secrets
):
    from agent import redact
    from cron.scheduler_script import _run_job_script

    hermes_home = tmp_path / ".hermes"
    (hermes_home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)

    success, output = _run_job_script(script_path)

    assert success is False
    for secret in secrets:
        assert secret not in output
    assert "***" in output


def test_script_runner_redacts_credentials_from_launch_exception(
    tmp_path, monkeypatch
):
    from agent import redact
    import cron.scheduler_script as scheduler

    hermes_home = tmp_path / ".hermes"
    scripts_dir = hermes_home / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "launch.py").write_text("print('unused')\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    url_password = "launch-url-password"
    query_token = "launch-query-token"
    launch_error = RuntimeError(
        f"transport https://user:{url_password}@example.invalid/?token={query_token}"
    )
    monkeypatch.setattr(
        scheduler.subprocess,
        "Popen",
        MagicMock(side_effect=launch_error),
    )

    success, output = scheduler._run_job_script("launch.py")

    assert success is False
    assert "Script execution failed" in output
    for secret in (url_password, query_token):
        assert secret not in output
    assert "***" in output


def test_script_runner_redacts_credentials_from_timeout_path(tmp_path, monkeypatch):
    from agent import redact
    import cron.scheduler_script as scheduler

    hermes_home = tmp_path / ".hermes"
    scripts_dir = hermes_home / "scripts"
    scripts_dir.mkdir(parents=True)
    query_token = "timeout-query-token"
    script_name = f"timeout.py?token={query_token}"
    (scripts_dir / script_name).write_text("print('unused')\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    monkeypatch.setattr(scheduler, "_get_script_timeout", lambda: 1)
    monkeypatch.setattr(
        scheduler.time,
        "monotonic",
        MagicMock(side_effect=[0.0, 0.0, 2.0]),
    )
    monkeypatch.setattr(scheduler, "_terminate_cron_script_tree", MagicMock())
    monkeypatch.setattr(scheduler, "_drain_script_pipes", MagicMock())

    proc = MagicMock()
    proc.communicate.side_effect = scheduler.subprocess.TimeoutExpired(
        ["python", script_name], 0.1
    )
    monkeypatch.setattr(scheduler.subprocess, "Popen", MagicMock(return_value=proc))

    success, output = scheduler._run_job_script(script_name)

    assert success is False
    assert "timed out after 1s" in output
    assert query_token not in output
    assert "token=***" in output


def test_script_runner_redaction_failure_emits_safe_constant(tmp_path, monkeypatch):
    from agent import redact
    import cron.scheduler_script as scheduler
    from cron import script_redaction

    hermes_home = tmp_path / ".hermes"
    scripts_dir = hermes_home / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "ok.py").write_text("print('ordinary output')\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    unavailable = MagicMock(side_effect=RuntimeError("redactor unavailable"))
    # The runner resolves the redactor from ``agent.redact`` per call; the wrapper binds it by name.
    monkeypatch.setattr(redact, "redact_sensitive_text", unavailable)
    monkeypatch.setattr(script_redaction, "redact_sensitive_text", unavailable)

    success, output = scheduler._run_job_script("ok.py")

    assert success is True
    assert output == script_redaction.REDACTION_FAILURE
    assert "ordinary output" not in output
    assert "redactor unavailable" not in output


def test_script_runner_redacts_credentials_when_path_resolution_fails(
    tmp_path, monkeypatch
):
    from cron.scheduler_script import _run_job_script

    hermes_home = tmp_path / ".hermes"
    (hermes_home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    secret = "path-query-secret-8675309"
    overlong = f"{'x' * 300}?token={secret}"

    success, output = _run_job_script(overlong)

    assert success is False
    assert secret not in output
    assert "token=***" in output


def test_script_runner_contains_credential_bearing_hermes_home_failure(
    tmp_path, monkeypatch
):
    import cron.scheduler as scheduler_core
    import cron.scheduler_script as scheduler

    secret = "home-query-secret-8675309"
    overlong_home = tmp_path / f"{'x' * 300}?token={secret}"
    monkeypatch.setattr(scheduler_core, "_hermes_home", None)
    monkeypatch.setenv("HERMES_HOME", str(overlong_home))

    success, output = scheduler._run_job_script("job.py")

    assert success is False
    assert secret not in output
    assert output == "Blocked: Hermes scripts directory is unavailable"


def test_no_agent_failure_surfaces_do_not_receive_path_credentials(
    tmp_path, monkeypatch
):
    from agent import redact
    from cron import scheduler

    hermes_home = tmp_path / ".hermes"
    (hermes_home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    monkeypatch.setattr("hermes_cli.env_loader.load_hermes_dotenv", MagicMock())
    url_password = "no-agent-url-password"
    query_token = "no-agent-query-token"
    script_path = (
        f"../https://operator:{url_password}@example.invalid/gate.py"
        f"?token={query_token}"
    )
    job = {
        "id": "script-redaction-job",
        "name": "script redaction job",
        "prompt": "Analyze the collected data.",
        "script": script_path,
        "no_agent": True,
    }

    success, doc, final_response, error = scheduler.run_job(job)

    assert success is False
    for surface in (doc, final_response, str(error)):
        assert url_password not in surface
        assert query_token not in surface
    assert "***" in doc


def test_a_raised_runner_is_scrubbed_like_any_output(tmp_path, monkeypatch):
    from agent import redact
    from cron.script_redaction import bind_forced_script_redaction

    hermes_home = tmp_path / ".hermes"
    (hermes_home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    query_token = "raised-query-token"

    def raising_runner(script_path: str) -> tuple[bool, str]:
        raise OSError(f"cannot reach https://example.invalid/?token={query_token}")

    success, output = bind_forced_script_redaction(raising_runner)("gate.py")

    assert success is False
    assert query_token not in output
    assert "token=***" in output


def _formatted(records) -> list[str]:
    """Each record as a handler would write it: message, then the traceback text if any."""
    return [logging.Formatter().format(record) for record in records]


def test_a_raised_runner_puts_no_credential_in_the_result_or_the_log(
    tmp_path, monkeypatch, caplog
):
    from agent import redact
    from cron.script_redaction import bind_forced_script_redaction

    hermes_home = tmp_path / ".hermes"
    (hermes_home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    url_password = "raised-log-url-password"
    query_token = "raised-log-query-token"

    def raising_runner(script_path: str) -> tuple[bool, str]:
        raise OSError(
            f"cannot reach https://user:{url_password}@example.invalid/?token={query_token}"
        )

    with caplog.at_level(logging.DEBUG, logger="cron.scheduler"):
        success, output = bind_forced_script_redaction(raising_runner)("gate.py")

    emitted = _formatted(caplog.records)
    assert success is False
    for secret in (url_password, query_token):
        assert secret not in output
        assert all(secret not in line for line in emitted), emitted
    assert any("OSError" in line and "token=***" in line for line in emitted), emitted


def test_a_failing_redactor_puts_no_exception_text_in_the_log(monkeypatch, caplog):
    from agent import redact
    from cron import script_redaction

    secret = "sk-redactorfailure0123456789"
    failing = MagicMock(side_effect=RuntimeError(f"redactor choked on {secret}"))
    monkeypatch.setattr(redact, "redact_sensitive_text", failing)
    monkeypatch.setattr(script_redaction, "redact_sensitive_text", failing)

    with caplog.at_level(logging.DEBUG, logger="cron.scheduler"):
        output = script_redaction.force_redact_script_text(f"OPENAI_API_KEY={secret}")

    emitted = _formatted(caplog.records)
    assert output == script_redaction.REDACTION_FAILURE
    assert all(secret not in line for line in emitted), emitted
    assert any("RuntimeError" in line for line in emitted), emitted


def test_a_missing_home_resolver_fails_the_job_and_names_it(monkeypatch):
    from cron import scheduler as scheduler_core
    from cron.script_redaction import bind_forced_script_redaction

    monkeypatch.delattr(scheduler_core, "_get_hermes_home")
    runner = MagicMock(return_value=(True, "ran"))

    success, output = bind_forced_script_redaction(runner)("gate.py")

    assert success is False
    assert "cron.scheduler._get_hermes_home" in output
    runner.assert_not_called()
