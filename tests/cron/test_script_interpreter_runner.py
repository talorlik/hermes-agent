"""The job-level ``interpreter`` on the cron script runner (``cron.scheduler_script``).

An absolute or ``~``-prefixed Python runs the script; without one the default runner is
unchanged. A relative, missing, non-executable or non-Python interpreter is refused before
anything is spawned.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _interpreter_fixture(tmp_path, monkeypatch):
    import stat as _stat
    import sys

    hermes_home = tmp_path / ".hermes"
    (hermes_home / "scripts").mkdir(parents=True)
    (hermes_home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    wrapper = hermes_home / "venv" / "bin" / "python3"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "env = os.environ.copy()\n"
        'env["CRON_WRAPPER_USED"] = "1"\n'
        "os.execve(sys.executable, [sys.executable, *sys.argv[1:]], env)\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | _stat.S_IXUSR)
    (hermes_home / "scripts" / "marker.py").write_text(
        'import os\nprint(os.environ.get("CRON_WRAPPER_USED", "0"))\n'
    )
    return hermes_home, wrapper


def test_script_runner_honours_absolute_and_tilde_python_interpreter(tmp_path, monkeypatch):
    import cron.scheduler_script as scheduler

    _home, wrapper = _interpreter_fixture(tmp_path, monkeypatch)

    ok, output = scheduler._run_job_script("marker.py", interpreter=str(wrapper))
    assert ok is True and output.strip() == "1"

    tilde = "~/.hermes/venv/bin/python3"
    ok, output = scheduler._run_job_script("marker.py", interpreter=tilde)
    assert ok is True and output.strip() == "1"

    # Default runner is unchanged when no interpreter is configured.
    ok, output = scheduler._run_job_script("marker.py")
    assert ok is True and output.strip() == "0"


@pytest.mark.parametrize(
    "make_interpreter, needle",
    [
        (lambda home: "venv/bin/python3", "absolute"),
        (lambda home: str(home / "venv" / "bin" / "python3.99"), "not found"),
        (lambda home: str(home / "venv" / "bin" / "python-noexec"), "executable"),
        (lambda home: str(home / "venv" / "bin" / "notpython"), "python"),
        (lambda home: f"{home}/venv/bin/python3; echo pwned", "not found"),
    ],
)
def test_script_runner_rejects_invalid_interpreters_fail_closed(
    tmp_path, monkeypatch, make_interpreter, needle
):
    import stat as _stat

    import cron.scheduler_script as scheduler

    home, wrapper = _interpreter_fixture(tmp_path, monkeypatch)
    noexec = home / "venv" / "bin" / "python-noexec"
    noexec.write_text(wrapper.read_text())
    noexec.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    notpython = home / "venv" / "bin" / "notpython"
    notpython.write_text(wrapper.read_text())
    notpython.chmod(notpython.stat().st_mode | _stat.S_IXUSR)
    popen = MagicMock(side_effect=AssertionError("nothing may be executed for a rejected interpreter"))
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)

    ok, output = scheduler._run_job_script("marker.py", interpreter=make_interpreter(home))

    assert ok is False
    assert "interpreter" in output.lower()
    assert needle in output.lower()
    popen.assert_not_called()
