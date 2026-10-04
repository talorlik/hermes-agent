"""The updater rejects a candidate the 9-file critical-syntax guard cannot see.

Merge damage in ``cron/jobs.py`` (not a critical file) must stop the fork sync BEFORE push,
roll the checkout back after a pull, and refuse a downloaded ZIP before anything is swapped.
Rejection is a rollback of the candidate, never a quarantine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from hermes_cli import main, update_cmd, update_cmd_git, update_cmd_zip

BROKEN = ") -> List[str]:\n"


def _git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "hermes_constants.py").write_text("X = 1\n", encoding="utf-8")
    # The launcher imports the checkout's own bootstrap; without it the installed one runs.
    (tmp_path / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "cron" / "jobs.py").write_text("Y = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "good")
    return tmp_path


def test_fork_sync_candidate_with_noncritical_damage_is_rejected_before_push(repo, monkeypatch, capsys):
    good = _git(repo, "rev-parse", "HEAD")
    (repo / "cron" / "jobs.py").write_text(BROKEN, encoding="utf-8")
    _git(repo, "commit", "-qam", "damaged merge")
    tests_ran = []
    monkeypatch.setattr(update_cmd_git, "_run_fork_sync_tests",
                        lambda cwd: tests_ran.append(cwd) or (True, ""))

    ok = update_cmd_git._validate_fork_sync_candidate(["git"], repo, good)

    assert ok is False
    assert "cron/jobs.py" in capsys.readouterr().out
    assert tests_ran == []
    assert _git(repo, "rev-parse", "HEAD") == good  # rolled back, not quarantined


def test_fork_sync_candidate_that_compiles_still_reaches_the_targeted_tests(repo, monkeypatch):
    from hermes_cli import update_cmd_integrity

    good = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(update_cmd_integrity, "INTEGRITY_IMPORT_MODULES", ("hermes_constants", "cron.jobs"))
    monkeypatch.setattr(update_cmd_git, "_run_fork_sync_tests", lambda cwd: (True, ""))

    assert update_cmd_git._validate_fork_sync_candidate(["git"], repo, good) is True


def test_pre_push_gate_is_strict_about_missing_critical_modules(repo, monkeypatch, capsys):
    """Publication must not certify a tree whose critical modules are absent or whose
    third-party imports fail; only the pre-dependency audit may tolerate those."""
    from hermes_cli import update_cmd_integrity

    good = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(update_cmd_integrity, "INTEGRITY_IMPORT_MODULES",
                        ("hermes_constants", "tools.kanban_tools"))
    (repo / "hermes_constants.py").write_text("import totally_not_installed_pkg\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "needs a dependency")
    tests_ran = []
    monkeypatch.setattr(update_cmd_git, "_run_fork_sync_tests",
                        lambda cwd: tests_ran.append(cwd) or (True, ""))

    assert update_cmd_git._validate_fork_sync_candidate(["git"], repo, good) is False
    out = capsys.readouterr().out
    assert "tools.kanban_tools" in out and "hermes_constants" in out
    assert tests_ran == [] and _git(repo, "rev-parse", "HEAD") == good


def test_pull_rolls_back_noncritical_damage_and_accepts_the_corrected_retry(repo, monkeypatch, capsys):
    previous = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(main, "PROJECT_ROOT", repo)
    (repo / "cron" / "jobs.py").write_text(BROKEN, encoding="utf-8")
    _git(repo, "commit", "-qam", "damaged upstream")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "reset", "--hard", previous)

    def pull():
        return update_cmd._pull_updates(
            ["git"], "main", None, prompt_for_restore=False, gw_input_fn=None,
            discard_local_changes=False, keep_stash=False)

    with pytest.raises(SystemExit) as failure:
        pull()

    assert failure.value.code == 1
    assert "integrity audit" in capsys.readouterr().out
    assert _git(repo, "rev-parse", "HEAD") == previous

    (repo / "cron" / "jobs.py").write_text("Y = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "fixed upstream")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "reset", "--hard", previous)
    pull()
    assert (repo / "cron" / "jobs.py").read_text(encoding="utf-8") == "Y = 2\n"


def _zip_with(tmp_path: Path, jobs_source: str, branch: str = "main") -> str:
    archive = tmp_path / "src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"hermes-agent-{branch}/hermes_constants.py", "X = 1\n")
        zf.writestr(f"hermes-agent-{branch}/cron/jobs.py", jobs_source)
    return archive.as_uri()


@pytest.mark.parametrize("jobs_source, swapped", [(BROKEN, False), ("Y = 2\n", True)])
def test_zip_update_audits_the_download_before_the_swap(tmp_path, monkeypatch, jobs_source, swapped, capsys):
    project = tmp_path / "install"
    (project / "cron").mkdir(parents=True)
    (project / "cron" / "jobs.py").write_text("Y = 1\n", encoding="utf-8")
    (project / "hermes_constants.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(main, "PROJECT_ROOT", project)
    monkeypatch.setattr(update_cmd_zip, "_zip_overlay_block_reason", lambda *a, **k: None)
    url = _zip_with(tmp_path, jobs_source)

    if swapped:
        update_cmd_zip._download_and_swap_zip("main", url)
    else:
        with pytest.raises(SystemExit):
            update_cmd_zip._download_and_swap_zip("main", url)

    expected = jobs_source if swapped else "Y = 1\n"
    assert (project / "cron" / "jobs.py").read_text(encoding="utf-8") == expected
    if not swapped:
        assert "integrity audit" in capsys.readouterr().out
