"""Regression for the merge-damage class behind the Conductor outage.

A conflict resolution spliced orphan fragments of fork lines into ``cron/jobs.py`` and
``tools/kanban_tools.py`` (``) -> List[str]:`` with no ``def``, a docstring without its
``def``). The 9-file critical-syntax guard never looked at those files, and the import
probe covered four modules, so a tree with 3 unparseable files was publishable.
``update_cmd_integrity`` audits every tracked Python file and imports the modules the
scheduler and worker tools load, in an isolated home, and fails closed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import update_cmd_integrity as integrity

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)


def _git_tree(root: Path, files: dict[str, str]) -> Path:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "T")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tree")
    return root


def test_orphan_merge_fragment_is_a_syntax_failure(tmp_path):
    """The exact shape of the cron/jobs.py damage: a signature tail with no ``def``."""
    root = _git_tree(tmp_path, {
        "ok.py": "X = 1\n",
        "cron/jobs.py": (
            "from typing import List, Optional\n\n\n"
            "def _normalize_skill_list(skill: Optional[str] = None) -> List[str]:\n"
            ") -> List[str]:\n"
            '    """doc"""\n'
            "    return []\n"
        ),
    })

    report = integrity.audit_tree(root, check_imports=False)

    assert report.ok is False
    assert [(f.path, f.line) for f in report.syntax_failures] == [("cron/jobs.py", 5)]


def test_conflict_marker_is_a_syntax_failure(tmp_path):
    root = _git_tree(tmp_path, {"mod.py": "<<<<<<< HEAD\nX = 1\n=======\nX = 2\n>>>>>>> other\n"})

    report = integrity.audit_tree(root, check_imports=False)

    assert [f.path for f in report.syntax_failures] == ["mod.py"]


def test_git_scan_ignores_untracked_and_ignored_files(tmp_path):
    """Venv caches and scratch files must never fail a candidate."""
    root = _git_tree(tmp_path, {".gitignore": "venv/\n", "ok.py": "X = 1\n"})
    (root / "venv" / "lib").mkdir(parents=True)
    (root / "venv" / "lib" / "broken.py").write_text("def (\n", encoding="utf-8")
    (root / "untracked_broken.py").write_text("def (\n", encoding="utf-8")

    report = integrity.audit_tree(root, check_imports=False)

    assert report.ok is True
    assert report.scan == "git"
    assert report.python_files == 1


def test_archive_tree_walk_skips_dependency_and_cache_dirs(tmp_path):
    """Non-git installs (ZIP/archive) still audit, without descending into venvs."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "ok.py").write_text("X = 1\n", encoding="utf-8")
    for skipped in ("venv", ".venv", "node_modules", "__pycache__", "site-packages"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "broken.py").write_text("def (\n", encoding="utf-8")
    (tmp_path / "pkg" / "bad.py").write_text("def (\n", encoding="utf-8")

    report = integrity.audit_tree(tmp_path, check_imports=False)

    assert report.scan == "walk"
    assert [f.path for f in report.syntax_failures] == ["pkg/bad.py"]


def test_syntactically_valid_import_failure_is_rejected(tmp_path, probe_root):
    """Both files parse; the import still fails. Only the import probe sees it."""
    (probe_root / "provider.py").write_text("OTHER = 1\n", encoding="utf-8")
    (probe_root / "consumer.py").write_text("from provider import SHARED_NAME\n", encoding="utf-8")

    report = integrity.audit_tree(probe_root, modules=("consumer",))

    assert report.syntax_failures == []
    assert report.ok is False
    assert [(f.module, f.kind) for f in report.import_failures] == [("consumer", "ImportError")]
    assert "SHARED_NAME" in report.import_failures[0].detail


def test_module_resolving_outside_the_candidate_is_rejected(probe_root):
    """An installed copy or shadow must not vouch for the candidate."""
    (probe_root / "consumer.py").write_text(
        "import sys, types\n"
        "stub = types.ModuleType('consumer')\n"
        "stub.__file__ = '/somewhere/else/consumer.py'\n"
        "sys.modules['consumer'] = stub\n",
        encoding="utf-8")

    report = integrity.audit_tree(probe_root, modules=("consumer",))

    assert [f.kind for f in report.import_failures] == ["ShadowedModule"]


def test_import_time_name_error_is_rejected(probe_root):
    """A leftover fragment can parse and still blow up at import (NameError)."""
    (probe_root / "consumer.py").write_text("VALUE = missing_helper()\n", encoding="utf-8")

    report = integrity.audit_tree(probe_root, modules=("consumer",))

    assert [(f.module, f.kind) for f in report.import_failures] == [("consumer", "NameError")]


def test_missing_third_party_dependency_fails_strict_and_is_incomplete_when_allowed(probe_root):
    """Strict (publish) mode must not certify importability it did not verify; the
    pre-dependency updater stage may defer, but the report says the check is incomplete."""
    (probe_root / "consumer.py").write_text("import totally_not_installed_pkg\n", encoding="utf-8")

    strict = integrity.audit_tree(probe_root, modules=("consumer",))
    relaxed = integrity.audit_tree(probe_root, modules=("consumer",), strict=False)

    assert strict.ok is False and strict.import_complete is False
    assert [f.module for f in strict.import_failures] == ["consumer"]
    assert relaxed.ok is True and relaxed.import_complete is False
    assert [f.module for f in relaxed.missing_dependencies] == ["consumer"]


def test_missing_first_party_import_is_a_failure_even_when_relaxed(probe_root):
    (probe_root / "hermes_helper_pkg").mkdir()
    (probe_root / "consumer.py").write_text("import hermes_gone_module\n", encoding="utf-8")

    report = integrity.audit_tree(probe_root, modules=("consumer",), strict=False)

    assert report.ok is False
    assert report.import_failures[0].kind == "ModuleNotFoundError"


@pytest.mark.parametrize("source", [
    "return 1\n",
    "await something()\n",
    "X = 1\nfrom __future__ import annotations\n",
])
def test_compiler_only_errors_are_syntax_failures(tmp_path, source):
    """These build an AST but cannot compile, so ``ast.parse`` alone certifies a dead file."""
    root = _git_tree(tmp_path, {"mod.py": source})

    report = integrity.audit_tree(root, check_imports=False)

    assert [f.path for f in report.syntax_failures] == ["mod.py"]


def test_git_enumeration_failure_is_not_a_silent_walk(tmp_path, monkeypatch):
    root = _git_tree(tmp_path, {"ok.py": "X = 1\n"})
    real_run = subprocess.run

    def broken_git(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "ls-files"]:
            raise subprocess.CalledProcessError(128, cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(integrity.subprocess, "run", broken_git)

    report = integrity.audit_tree(root, check_imports=False)

    assert report.ok is False
    assert "git ls-files failed" in report.enumeration_error


def test_tracked_file_missing_from_worktree_is_a_failure(tmp_path):
    root = _git_tree(tmp_path, {"ok.py": "X = 1\n", "gone.py": "Y = 1\n"})
    (root / "gone.py").unlink()

    report = integrity.audit_tree(root, check_imports=False)

    assert [(f.path, f.error) for f in report.syntax_failures] == [("gone.py", "tracked file is missing")]


def test_empty_or_nonexistent_root_is_not_green(tmp_path):
    assert integrity.audit_tree(tmp_path / "nope", check_imports=False).ok is False
    assert integrity.audit_tree(tmp_path, check_imports=False).ok is False


def _fake_probe(monkeypatch, *, stdout_for, returncode=0):
    """Replace the probe child; ``stdout_for(marker)`` builds what it prints."""
    import re

    def fake_run(cmd, *args, **kwargs):
        marker = re.search(r"__HERMES_INTEGRITY_\w+__", " ".join(cmd)).group(0)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout_for(marker), stderr="boom")

    monkeypatch.setattr(integrity.subprocess, "run", fake_run)


def test_probe_that_prints_marker_then_exits_nonzero_is_not_success(probe_root, monkeypatch):
    (probe_root / "consumer.py").write_text("X = 1\n", encoding="utf-8")
    _fake_probe(monkeypatch, stdout_for=lambda m: f"\n{m}[]", returncode=3)

    report = integrity.audit_tree(probe_root, modules=("consumer",))

    assert report.ok is False and "exited 3" in report.probe_error


@pytest.mark.parametrize("payload", [
    lambda m: "no marker at all",
    lambda m: f"\n{m}[] \n{m}[]",
    lambda m: f"\n{m}{{}}",
    lambda m: f"\n{m}[[\"consumer\", \"E\"]]",
    lambda m: f"\n{m}[[\"other\", \"E\", \"d\"]]",
    lambda m: f"\n{m}[[\"consumer\", 1, \"d\"]]",
    lambda m: f"\n{m}not json",
])
def test_probe_with_wrong_protocol_is_not_success(probe_root, monkeypatch, payload):
    (probe_root / "consumer.py").write_text("X = 1\n", encoding="utf-8")
    _fake_probe(monkeypatch, stdout_for=payload)

    report = integrity.audit_tree(probe_root, modules=("consumer",))

    assert report.ok is False and report.probe_error


def test_probe_timeout_and_spawn_failure_are_not_success(probe_root, monkeypatch):
    (probe_root / "consumer.py").write_text("X = 1\n", encoding="utf-8")
    for exc in (subprocess.TimeoutExpired("x", 1), OSError("no exec")):
        def boom(cmd, *a, _exc=exc, **k):
            raise _exc
        monkeypatch.setattr(integrity.subprocess, "run", boom)
        assert integrity.audit_tree(probe_root, modules=("consumer",)).ok is False


def test_probe_sees_no_live_profile_secret_or_home_state(probe_root, monkeypatch, tmp_path):
    """Hostile inherited overrides must not reach the candidate's import-time code."""
    live = tmp_path / "live"
    live.mkdir()
    seen = probe_root / "seen.json"
    for name, value in {
        "HERMES_HOME": str(live), "HERMES_PROFILE": "live-profile", "HERMES_ROOT_HOME": str(live),
        "HERMES_KANBAN_HOME": str(live), "HERMES_KANBAN_DB": str(live / "k.db"),
        "OPENAI_API_KEY": "sk-live", "XDG_CONFIG_HOME": str(live), "HOME": str(live),
        "PYTHONPATH": str(live), "HERMES_SESSION_ID": "live-session",
    }.items():
        monkeypatch.setenv(name, value)
    (probe_root / "consumer.py").write_text(
        "import json, os, pathlib\n"
        f"pathlib.Path({str(seen)!r}).write_text(json.dumps(dict(os.environ)))\n"
        "pathlib.Path(os.path.expanduser('~/probe-wrote-here')).write_text('x')\n",
        encoding="utf-8")

    report = integrity.audit_tree(probe_root, modules=("consumer",))

    env = json.loads(seen.read_text(encoding="utf-8"))
    assert report.ok is True
    assert not any(k.startswith("HERMES_") and k not in {"HERMES_HOME", "HERMES_DISABLE_LAZY_INSTALLS", "HERMES_GATEWAY_LOCK_DIR"} for k in env)
    assert "OPENAI_API_KEY" not in env and "PYTHONPATH" not in env
    for key in ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "TMPDIR"):
        assert not env[key].startswith(str(live)), key
    assert env["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
    assert list(live.iterdir()) == []


def test_audit_writes_no_bytecode_into_the_candidate(probe_root):
    (probe_root / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (probe_root / "consumer.py").write_text("import helper\n", encoding="utf-8")

    report = integrity.audit_tree(probe_root, modules=("consumer",))

    assert report.ok is True
    assert list(probe_root.rglob("*.pyc")) == [] and list(probe_root.rglob("__pycache__")) == []


def test_modules_absent_from_the_tree_fail_strict_and_are_visible_when_relaxed(probe_root):
    """Strict (publish) mode certifies a fully importable candidate; the pre-dependency
    updater may tolerate a refactor, but the skipped module stays in the report."""
    strict = integrity.audit_tree(probe_root, modules=("gone.entirely",))
    relaxed = integrity.audit_tree(probe_root, modules=("gone.entirely",), strict=False)

    assert strict.ok is False
    assert [(f.module, f.kind) for f in strict.import_failures] == [("gone.entirely", "MissingModule")]
    assert relaxed.ok is True and relaxed.import_complete is False
    assert relaxed.skipped_modules == ["gone.entirely"]


def test_one_missing_critical_module_among_present_ones_fails_strict(probe_root):
    (probe_root / "consumer.py").write_text("X = 1\n", encoding="utf-8")

    report = integrity.audit_tree(probe_root, modules=("consumer", "gone"))

    assert report.ok is False and report.import_complete is False
    assert [f.module for f in report.import_failures] == ["gone"]


def test_import_probe_runs_in_an_isolated_home_that_is_removed(probe_root, monkeypatch):
    live_home = probe_root / "live-home"
    live_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(live_home))
    seen = probe_root / "seen-home.txt"
    (probe_root / "consumer.py").write_text(
        "import os, pathlib\n"
        f"pathlib.Path({str(seen)!r}).write_text(os.environ['HERMES_HOME'])\n",
        encoding="utf-8",
    )

    report = integrity.audit_tree(probe_root, modules=("consumer",))

    probe_home = Path(seen.read_text(encoding="utf-8"))
    assert report.ok is True
    assert probe_home != live_home
    assert not probe_home.exists()
    assert list(live_home.iterdir()) == []


def test_cli_allow_missing_deps_and_checker_crash_exit_codes(probe_root, tmp_path):
    (probe_root / "consumer.py").write_text("import totally_not_installed_pkg\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    base = [sys.executable, "-m", "hermes_cli.update_cmd_integrity", "--root", str(probe_root), "--json"]

    strict = subprocess.run(base, capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert strict.returncode in (0, 1)  # consumer is not a default critical module: syntax only
    empty = subprocess.run([*base[:-3], "--root", str(tmp_path / "empty"), "--json"],
                           capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert empty.returncode == 2 and json.loads(empty.stdout)["enumeration_error"]


def test_cli_json_contract_and_exit_codes(tmp_path):
    root = _git_tree(tmp_path, {"bad.py": "def (\n"})
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}

    failed = subprocess.run(
        [sys.executable, "-m", "hermes_cli.update_cmd_integrity", "--root", str(root),
         "--no-imports", "--json"],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    payload = json.loads(failed.stdout)

    assert failed.returncode == 1
    assert payload["schema"] == 2 and payload["ok"] is False
    assert payload["syntax_failures"][0]["path"] == "bad.py"

    (root / "bad.py").write_text("X = 1\n", encoding="utf-8")
    clean = subprocess.run(
        [sys.executable, "-m", "hermes_cli.update_cmd_integrity", "--root", str(root),
         "--no-imports", "--json"],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert clean.returncode == 0 and json.loads(clean.stdout)["ok"] is True

    missing = subprocess.run(
        [sys.executable, "-m", "hermes_cli.update_cmd_integrity", "--root",
         str(tmp_path / "nope"), "--json"],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert missing.returncode == 2


def test_this_checkout_has_no_syntax_failures():
    """Whole-tree contract on the real candidate: every tracked .py parses."""
    report = integrity.audit_tree(REPO_ROOT, check_imports=False)

    assert report.scan == "git"
    assert report.syntax_failures == []
