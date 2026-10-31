"""Sync-class exemptions for scripts/ci/check_fork_ledger.py.

A sync delta is exempt only when its file class allows it. These tests use
the same throwaway-repo style as test_check_fork_ledger.py.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

_HELPER_PATH = Path(__file__).with_name("test_check_fork_ledger.py")
_SPEC = importlib.util.spec_from_file_location("fork_ledger_fixture", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_fixture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_fixture)

_commit_file = _fixture._commit_file
_entry = _fixture._entry
_git = _fixture._git
_run_checker = _fixture._run_checker
_write_ledger = _fixture._write_ledger
_GIT_ENV = _fixture._GIT_ENV

WORKFLOW = ".github/workflows/lint.yml"
BUILD_OUTPUT = "plugins/kanban/dashboard/dist/index.js"
DESKTOP_MANIFEST = "apps/desktop/package.json"
BUILDER_CONSTANT = "apps/desktop/scripts/builder-config-26.mjs"


def _class_spec(
    *,
    generated: list[str] | None = None,
    build: list[str] | None = None,
    workflows: list[str] | None = None,
    contracts: list[dict[str, object]] | None = None,
    version: int = 1,
) -> str:
    return json.dumps(
        {
            "version": version,
            "generated_unspecified": {
                "action": "take-upstream",
                "paths": generated or [],
            },
            "build_output": {
                "action": "regenerate-or-stop",
                "generator": None,
                "paths": build or [],
            },
            "auth_hold": {
                "action": "hold-previous-blob",
                "ledger_entry": "G-ORIGIN-WORKFLOWS",
                "reason": "token cannot update workflow files",
                "paths": workflows or [],
            },
            "structured_pin_contract": contracts or [],
        }
    ) + "\n"


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    return repo


def _commit_spec(repo: Path, text: str) -> None:
    _commit_file(repo, "docs/fork-sync-classes.json", text, "docs: declare sync classes")


def _publish_merge(
    repo: Path,
    first: str,
    second: str,
    updates: dict[str, str],
    message: str,
) -> str:
    """Commit a two-parent merge whose tree is the fork tree plus updates."""
    index = repo / ".git" / "sync-class-test.index"
    env = {**_GIT_ENV, "GIT_INDEX_FILE": str(index)}
    try:
        subprocess.run(
            ["git", "read-tree", first],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        for rel, content in updates.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            subprocess.run(
                ["git", "add", "--", rel],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
        tree = subprocess.run(
            ["git", "write-tree"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    finally:
        index.unlink(missing_ok=True)
    return _git(
        repo,
        "commit-tree",
        tree,
        "-p",
        first,
        "-p",
        second,
        "-m",
        message,
    )


def _point_fork_at(repo: Path, sha: str) -> None:
    _git(repo, "update-ref", "refs/heads/fork-main", sha)
    _git(repo, "checkout", "-f", "fork-main")


def _is_exempt(repo: Path, sha: str) -> bool:
    _code, payload = _run_checker(repo)
    return sha in payload.get("sync_merges", [])


def test_source_fit_rejects_an_absent_executable_fork_line(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "dropped-executable")
    _commit_file(repo, "shared.py", "VALUE = 'base'\n", "base")
    _commit_spec(repo, _class_spec())
    _git(repo, "checkout", "-b", "fork-main")
    fork_sha = _commit_file(
        repo,
        "shared.py",
        "VALUE = 'fork'\ndef run():\n    return helper()\n",
        "fork customization",
    )
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'upstream'\n", "upstream change")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 1
    (repo / "shared.py").write_text("VALUE = 'upstream'\ndef run():\n", encoding="utf-8")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream with fitted conflict")
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry(
            "G-SHARED",
            "fitted shared path",
            commits=fork_sha,
            owned_files=["shared.py"],
        ),
    )

    assert _is_exempt(repo, sync_merge) is False


def test_comment_only_absence_still_fits_the_upstream_line_rule(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "comment-absence")
    _commit_file(repo, "shared.py", "VALUE = 'base'\n", "base")
    _commit_spec(repo, _class_spec())
    _git(repo, "checkout", "-b", "fork-main")
    fork_sha = _commit_file(
        repo,
        "shared.py",
        "VALUE = 'fork'\n# fork note\nEXTRA = 'fork'\n",
        "fork customization",
    )
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'upstream'\n", "upstream change")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 1
    (repo / "shared.py").write_text(
        "VALUE = 'upstream'\nEXTRA = 'fork'\n",
        encoding="utf-8",
    )
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream with fitted conflict")
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry(
            "G-SHARED",
            "fitted shared path",
            commits=fork_sha,
            owned_files=["shared.py"],
        ),
    )

    assert _is_exempt(repo, sync_merge) is True


def test_generated_unspecified_is_accepted_only_from_upstream(tmp_path: Path) -> None:
    taken = _init_repo(tmp_path, "take-upstream")
    _commit_file(taken, "uv.lock", "base-lock\n", "base lock")
    _commit_file(taken, "marker.py", "base\n", "base marker")
    _commit_spec(taken, _class_spec(generated=["uv.lock"]))
    _git(taken, "checkout", "-b", "fork-main")
    fork_lock = _commit_file(taken, "uv.lock", "fork-lock\n", "fork lock")
    fork_tip = _git(taken, "rev-parse", "HEAD")
    _git(taken, "checkout", "upstream-main")
    _commit_file(taken, "marker.py", "upstream\n", "upstream marker")
    upstream_sha = _git(taken, "rev-parse", "HEAD")
    taken_merge = _publish_merge(
        taken,
        fork_tip,
        upstream_sha,
        {"uv.lock": "base-lock\n", "marker.py": "upstream\n"},
        "chore: take upstream lock",
    )
    _point_fork_at(taken, taken_merge)
    _write_ledger(
        taken,
        _entry("G-LOCK", "fork lock edit", commits=fork_lock, owned_files=["uv.lock"]),
    )
    assert _is_exempt(taken, taken_merge) is True

    spliced = _init_repo(tmp_path, "spliced-lock")
    _commit_file(spliced, "uv.lock", "base-lock\n", "base lock")
    _commit_spec(spliced, _class_spec(generated=["uv.lock"]))
    _git(spliced, "checkout", "-b", "fork-main")
    fork_edit = _commit_file(
        spliced,
        "uv.lock",
        "upstream line\nfork extra\n",
        "fork lock",
    )
    fork_tip = _git(spliced, "rev-parse", "HEAD")
    _git(spliced, "checkout", "upstream-main")
    _commit_file(spliced, "uv.lock", "upstream line\n", "upstream lock")
    upstream_sha = _git(spliced, "rev-parse", "HEAD")
    splice_merge = _publish_merge(
        spliced,
        fork_tip,
        upstream_sha,
        {"uv.lock": "upstream line\nfork extra\n"},
        "chore: splice generated lock",
    )
    _point_fork_at(spliced, splice_merge)
    _write_ledger(
        spliced,
        _entry("G-LOCK", "fork lock edit", commits=fork_edit, owned_files=["uv.lock"]),
    )
    assert _is_exempt(spliced, splice_merge) is False


def test_pin_named_lock_stays_on_the_existing_pin_gates(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "named-lock")
    _commit_file(
        repo,
        "package.json",
        json.dumps({"name": "root", "overrides": {"undici@^6": "6.29.0"}}) + "\n",
        "base manifest",
    )
    _commit_file(
        repo,
        "package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/undici": {"version": "6.29.0"},
                    "node_modules/lodash": {"version": "4.18.0"},
                },
            }
        )
        + "\n",
        "base lock",
    )
    _commit_spec(repo, _class_spec())
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(
        repo,
        "docs/fork-npm-pins.json",
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "undici",
                        "override": "undici@^6",
                        "floor": "6.29.1",
                        "resolved": "6.29.1",
                    }
                ],
            }
        )
        + "\n",
        "feat(fork): declare undici floor",
    )
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(
        repo,
        "package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/undici": {"version": "6.29.0"},
                    "node_modules/lodash": {"version": "4.18.1"},
                },
            }
        )
        + "\n",
        "upstream lock",
    )
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    replay = _publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {
            "package.json": json.dumps(
                {"name": "root", "overrides": {"undici@^6": "6.29.1"}}
            )
            + "\n",
            "package-lock.json": json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "node_modules/undici": {"version": "6.29.1"},
                        "node_modules/lodash": {"version": "4.18.1"},
                    },
                }
            )
            + "\n",
        },
        "chore: replay npm pins",
    )
    _point_fork_at(repo, replay)
    _write_ledger(
        repo,
        _entry(
            "G-NPM-PIN-REPLAY",
            "fork npm pin spec",
            commits=pin_commit,
            owned_files=["docs/fork-npm-pins.json"],
        )
        + _entry(
            "G-LOCK",
            "lock path owner",
            commits="none",
            owned_files=["package.json", "package-lock.json"],
        ),
    )
    assert _is_exempt(repo, replay) is True

    unnamed = _init_repo(tmp_path, "unnamed-lock")
    _commit_file(
        unnamed,
        "package.json",
        json.dumps({"name": "root", "overrides": {"undici@^6": "6.29.1"}}) + "\n",
        "base manifest",
    )
    _commit_file(
        unnamed,
        "package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/undici": {"version": "6.29.1"}},
            }
        )
        + "\n",
        "base root lock",
    )
    _commit_file(
        unnamed,
        "web/package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/undici": {"version": "6.29.1"}},
            }
        )
        + "\n",
        "base nested lock",
    )
    _commit_spec(unnamed, _class_spec())
    _git(unnamed, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(
        unnamed,
        "docs/fork-npm-pins.json",
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "undici",
                        "override": "undici@^6",
                        "floor": "6.29.1",
                        "resolved": "6.29.1",
                    }
                ],
            }
        )
        + "\n",
        "feat(fork): declare undici floor",
    )
    fork_tip = _git(unnamed, "rev-parse", "HEAD")
    _git(unnamed, "checkout", "upstream-main")
    upstream_sha = _git(unnamed, "rev-parse", "HEAD")
    nested = _publish_merge(
        unnamed,
        fork_tip,
        upstream_sha,
        {
            "web/package-lock.json": json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "node_modules/undici": {"version": "6.29.1", "license": "MIT"},
                    },
                }
            )
            + "\n"
        },
        "chore: splice an unnamed lock",
    )
    _point_fork_at(unnamed, nested)
    _write_ledger(
        unnamed,
        _entry(
            "G-NPM-PIN-REPLAY",
            "fork npm pin spec",
            commits=pin_commit,
            owned_files=["docs/fork-npm-pins.json"],
        ),
    )
    assert _is_exempt(unnamed, nested) is False


def test_workflow_hold_requires_the_fork_blob_and_active_entry(tmp_path: Path) -> None:
    spec = _class_spec(workflows=[WORKFLOW])

    held = _init_repo(tmp_path, "hold-fork")
    _commit_file(held, WORKFLOW, "base-workflow\n", "base workflow")
    _commit_spec(held, spec)
    _git(held, "checkout", "-b", "fork-main")
    fork_edit = _commit_file(held, WORKFLOW, "fork-workflow\n", "fork workflow")
    fork_tip = _git(held, "rev-parse", "HEAD")
    _git(held, "checkout", "upstream-main")
    _commit_file(held, WORKFLOW, "upstream-workflow\n", "upstream workflow")
    upstream_sha = _git(held, "rev-parse", "HEAD")
    held_merge = _publish_merge(
        held,
        fork_tip,
        upstream_sha,
        {WORKFLOW: "fork-workflow\n"},
        "chore: hold workflow",
    )
    _point_fork_at(held, held_merge)
    _write_ledger(
        held,
        _entry(
            "G-ORIGIN-WORKFLOWS",
            "origin workflows",
            commits=fork_edit,
            owned_files=[WORKFLOW],
        ),
    )
    assert _is_exempt(held, held_merge) is True

    taken = _init_repo(tmp_path, "hold-upstream")
    _commit_file(taken, WORKFLOW, "base-workflow\n", "base workflow")
    _commit_spec(taken, spec)
    _git(taken, "checkout", "-b", "fork-main")
    fork_edit = _commit_file(taken, WORKFLOW, "fork-workflow\n", "fork workflow")
    fork_tip = _git(taken, "rev-parse", "HEAD")
    _git(taken, "checkout", "upstream-main")
    _commit_file(taken, WORKFLOW, "upstream-workflow\n", "upstream workflow")
    upstream_sha = _git(taken, "rev-parse", "HEAD")
    taken_merge = _publish_merge(
        taken,
        fork_tip,
        upstream_sha,
        {WORKFLOW: "upstream-workflow\n"},
        "chore: take upstream workflow",
    )
    _point_fork_at(taken, taken_merge)
    _write_ledger(
        taken,
        _entry(
            "G-ORIGIN-WORKFLOWS",
            "origin workflows",
            commits=fork_edit,
            owned_files=[WORKFLOW],
        ),
    )
    assert _is_exempt(taken, taken_merge) is False

    missing = _init_repo(tmp_path, "hold-missing-entry")
    _commit_file(missing, WORKFLOW, "base-workflow\n", "base workflow")
    _commit_spec(missing, spec)
    _git(missing, "checkout", "-b", "fork-main")
    fork_edit = _commit_file(missing, WORKFLOW, "fork-workflow\n", "fork workflow")
    fork_tip = _git(missing, "rev-parse", "HEAD")
    _git(missing, "checkout", "upstream-main")
    _commit_file(missing, WORKFLOW, "upstream-workflow\n", "upstream workflow")
    upstream_sha = _git(missing, "rev-parse", "HEAD")
    missing_merge = _publish_merge(
        missing,
        fork_tip,
        upstream_sha,
        {WORKFLOW: "fork-workflow\n"},
        "chore: hold workflow without the entry",
    )
    _point_fork_at(missing, missing_merge)
    _write_ledger(
        missing,
        _entry(
            "G-OTHER",
            "not the workflow hold",
            commits=fork_edit,
            owned_files=[WORKFLOW],
        ),
    )
    assert _is_exempt(missing, missing_merge) is False


def test_missing_sync_class_spec_rejects_a_generated_delta(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "missing-spec")
    _commit_file(repo, "uv.lock", "base-lock\n", "base lock")
    _commit_file(repo, "marker.py", "base\n", "base marker")
    _git(repo, "checkout", "-b", "fork-main")
    fork_lock = _commit_file(repo, "uv.lock", "fork-lock\n", "fork lock")
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "marker.py", "upstream\n", "upstream marker")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    sync_merge = _publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {"uv.lock": "base-lock\n", "marker.py": "upstream\n"},
        "chore: take upstream lock",
    )
    _point_fork_at(repo, sync_merge)
    _write_ledger(
        repo,
        _entry("G-LOCK", "fork lock edit", commits=fork_lock, owned_files=["uv.lock"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_build_output_is_never_an_exempt_sync_delta(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "build-output")
    _commit_file(repo, BUILD_OUTPUT, "base-build\n", "base build")
    _commit_spec(repo, _class_spec(build=[BUILD_OUTPUT]))
    _git(repo, "checkout", "-b", "fork-main")
    fork_edit = _commit_file(repo, BUILD_OUTPUT, "fork-build\n", "fork build")
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    sync_merge = _publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {BUILD_OUTPUT: "base-build\n"},
        "chore: take upstream build output",
    )
    _point_fork_at(repo, sync_merge)
    _write_ledger(
        repo,
        _entry("G-BUILD", "build output", commits=fork_edit, owned_files=[BUILD_OUTPUT]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_structured_pin_requires_pin_keys_and_builder_pin(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [BUILDER_CONSTANT],
        }
    ]
    pins = json.dumps(
        {
            "version": 1,
            "pins": [
                {
                    "package": "electron-builder",
                    "manifest": DESKTOP_MANIFEST,
                    "field": "devDependencies",
                    "exact": "26.15.3",
                }
            ],
        }
    ) + "\n"
    manifest_before = (
        json.dumps({"devDependencies": {"electron-builder": "26.15.2"}}) + "\n"
    )
    manifest_after = (
        json.dumps({"devDependencies": {"electron-builder": "26.15.3"}}) + "\n"
    )

    matched = _init_repo(tmp_path, "structured-match")
    _commit_file(matched, DESKTOP_MANIFEST, manifest_before, "base manifest")
    _commit_file(
        matched,
        BUILDER_CONSTANT,
        "export const BUILDER_PIN = '26.15.3'\n",
        "base constant",
    )
    _commit_spec(matched, _class_spec(contracts=contract))
    _git(matched, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(matched, "docs/fork-npm-pins.json", pins, "declare pin")
    fork_tip = _git(matched, "rev-parse", "HEAD")
    _git(matched, "checkout", "upstream-main")
    upstream_sha = _git(matched, "rev-parse", "HEAD")
    matched_merge = _publish_merge(
        matched,
        fork_tip,
        upstream_sha,
        {DESKTOP_MANIFEST: manifest_after},
        "chore: replay structured pin",
    )
    _point_fork_at(matched, matched_merge)
    _write_ledger(
        matched,
        _entry(
            "G-PIN",
            "desktop pin",
            commits=pin_commit,
            owned_files=["docs/fork-npm-pins.json"],
        ),
    )
    assert _is_exempt(matched, matched_merge) is True

    mismatched = _init_repo(tmp_path, "structured-mismatch")
    _commit_file(mismatched, DESKTOP_MANIFEST, manifest_before, "base manifest")
    _commit_file(
        mismatched,
        BUILDER_CONSTANT,
        "export const BUILDER_PIN = '26.0.0'\n",
        "stale constant",
    )
    _commit_spec(mismatched, _class_spec(contracts=contract))
    _git(mismatched, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(
        mismatched, "docs/fork-npm-pins.json", pins, "declare pin"
    )
    fork_tip = _git(mismatched, "rev-parse", "HEAD")
    _git(mismatched, "checkout", "upstream-main")
    upstream_sha = _git(mismatched, "rev-parse", "HEAD")
    mismatch_merge = _publish_merge(
        mismatched,
        fork_tip,
        upstream_sha,
        {DESKTOP_MANIFEST: manifest_after},
        "chore: replay structured pin",
    )
    _point_fork_at(mismatched, mismatch_merge)
    _write_ledger(
        mismatched,
        _entry(
            "G-PIN",
            "desktop pin",
            commits=pin_commit,
            owned_files=["docs/fork-npm-pins.json"],
        ),
    )
    assert _is_exempt(mismatched, mismatch_merge) is False


def test_clean_splice_of_unspecified_lock_is_not_exempt(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "clean-splice")
    _commit_file(repo, "uv.lock", "a\nb\nc\n", "base lock")
    _commit_spec(repo, _class_spec(generated=["uv.lock"]))
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "uv.lock", "FORK\nb\nc\n", "fork lock")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "uv.lock", "a\nb\nUPSTREAM\n", "upstream lock")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    published = (repo / "uv.lock").read_text(encoding="utf-8")
    assert "FORK" in published and "UPSTREAM" in published
    _write_ledger(
        repo,
        _entry("G-LOCK", "fork lock", commits="none", owned_files=["uv.lock"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_clean_splice_without_spec_is_not_exempt(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "clean-splice-no-spec")
    _commit_file(repo, "uv.lock", "a\nb\nc\n", "base lock")
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "uv.lock", "FORK\nb\nc\n", "fork lock")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "uv.lock", "a\nb\nUPSTREAM\n", "upstream lock")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-LOCK", "fork lock", commits="none", owned_files=["uv.lock"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_duplicate_guard_does_not_hide_a_dropped_insertion() -> None:
    checker = _fixture._load_checker_module()
    fork = "def one():\n    guard()\n    new()\ndef two():\n    guard()\n"
    published = "def one():\n    new()\ndef two():\n    guard()\n"
    upstream = "def one():\n    old()\ndef two():\n    guard()\n"
    assert checker._has_executable_drop(fork, published, upstream) is True


def test_relocated_guard_is_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "a()\nguard()\nb()\n"
    published = "a()\nb()\nguard()\n"
    upstream = "a()\nb()\n"
    assert checker._has_executable_drop(fork, published, upstream) is True


def test_guard_kept_between_the_same_lines_is_not_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "a()\nguard()\nb()\n"
    upstream = "a()\nb()\n"
    assert checker._has_executable_drop(fork, fork, upstream) is False


def test_rewritten_neighbor_does_not_hide_a_moved_guard() -> None:
    checker = _fixture._load_checker_module()
    fork = "head();\nguard();\nVALUE = 1;\ntail();\n"
    upstream = "head();\nVALUE = 2;\ntail();\n"
    published = "head();\nVALUE = 2;\nguard();\ntail();\n"
    assert checker._has_executable_drop(fork, published, upstream) is True


def test_guard_kept_before_a_rewritten_assignment_is_not_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "head();\nguard();\nVALUE = 1;\ntail();\n"
    upstream = "head();\nVALUE = 2;\ntail();\n"
    published = "head();\nguard();\nVALUE = 2;\ntail();\n"
    assert checker._has_executable_drop(fork, published, upstream) is False


def test_different_method_calls_are_not_a_rewrite() -> None:
    checker = _fixture._load_checker_module()
    fork = "self.guard();\nself.new();\n"
    published = "self.old();\nself.new();\n"
    upstream = "self.old();\n"
    assert checker._has_executable_drop(fork, published, upstream) is True


def test_builder_pin_allows_optional_whitespace_around_equals() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns("export const BUILDER_PIN='26.15.3';", "26.15.3") is True


def test_nested_template_cannot_satisfy_the_constant() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = `start ${\n"
        "`inner\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "`} end`\n"
        "export const BUILDER_PIN = 'wrong' + 'value';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_prefix_increment_does_not_continue_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26.15.3'\n++counter;\n"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_newline_inside_a_block_comment_separates_statements() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26.15.3' /* comment\n */ export const OTHER = 1;\n"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_escaped_backtick_does_not_end_the_template() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const text = `outer \\` still\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "`;\n"
        "export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_object_brace_does_not_end_the_interpolation() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const text = `outer ${({}) && `inner\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "`} end`;\n"
        "export const BUILDER_PIN = 'wrong' + 'value';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_semicolon_ends_the_assignment_before_a_same_line_statement() -> None:
    checker = _fixture._load_checker_module()
    source = 'export const BUILDER_PIN = "26.15.3"; export const OTHER = 1;\n'
    assert checker._constant_assigns(source, "26.15.3") is True


def test_escaped_newline_in_a_string_is_not_a_declaration() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const hidden = "prefix\\\n'
        "export const BUILDER_PIN = '26.15.3';\";\n"
        "export const BUILDER_PIN = 'wrong' + 'value';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_semicolon_ends_the_assignment_before_a_continued_call() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26.15.3'; foo()\n[0];\n"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_template_after_the_semicolon_does_not_reject_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26.15.3'; const label = `builder`;\n"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_comment_brace_does_not_end_the_interpolation() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const text = `outer ${ /* } */ `inner\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "` } end`;\n"
        "export const BUILDER_PIN = 'wrong' + 'value';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_string_brace_does_not_end_the_interpolation() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const text = `outer ${ "}" && `inner\n'
        "export const BUILDER_PIN = '26.15.3';\n"
        "` } end`;\n"
        "export const BUILDER_PIN = 'stringvalue';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_line_separator_in_a_string_is_not_a_declaration() -> None:
    checker = _fixture._load_checker_module()
    for separator in ("\u2028", "\u2029", "\v"):
        source = (
            f'const hidden = "prefix{separator}'
            "export const BUILDER_PIN = '26.0.0';"
            f'{separator}";\n'
            'export const BUILDER_PIN = "wrong" + "";\n'
        )
        assert checker._constant_assigns(source, "26.0.0") is False


def test_comment_between_export_and_const_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "export/* why */const BUILDER_PIN = '26.0.0';\n"
    assert checker._constant_assigns(source, "26.0.0") is True


def test_backtick_in_a_regex_is_not_a_template() -> None:
    checker = _fixture._load_checker_module()
    source = "const r = /`/;\nexport const BUILDER_PIN = '26.0.0';\n"
    assert checker._constant_assigns(source, "26.0.0") is True


def test_division_after_an_object_does_not_hide_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_regex_after_a_block_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "if (ok) {}\n"
        "/export const BUILDER_PIN = '26.15.3'/;\n"
        "export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_division_after_a_function_expression_does_not_hide_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = function() {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_division_after_a_class_expression_does_not_hide_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_regex_after_a_labeled_block_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "label: {}\n/`/;\nexport const BUILDER_PIN = '26.15.3';\n"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_division_after_a_class_extends_expression_does_not_hide_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const ratio = class extends Base {} / 2; "
        "export const BUILDER_PIN = '26.0.12';\n"
    )
    assert checker._constant_assigns(source, "26.0.12") is True


def test_division_after_a_method_named_if_does_not_hide_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = "obj.if(true) / 2; export const BUILDER_PIN = '26.0.12';\n"
    assert checker._constant_assigns(source, "26.0.12") is True


def test_division_after_qualified_class_heritage_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class extends ns.Base {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26.0.0';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.0.0") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_a_spaced_method_named_if_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "obj. if(true) / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26.0.0';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.0.0") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_a_regex_literal_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "const x = /x/ / 2; export const BUILDER_PIN = '26.0.0';\n"
    assert checker._constant_assigns(source, "26.0.0") is True


def test_division_after_named_parenthesized_heritage_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class Named extends (Base) {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_computed_class_heritage_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class extends ns[base] {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26.0.0';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.0.0") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_block_comment_line_terminator_ends_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    for terminator in ("\r", "\u2028", "\u2029"):
        source = (
            f'export const BUILDER_PIN = "26" /*{terminator}*/ '
            "export const OTHER = 1;"
        )
        assert checker._constant_assigns(source, "26") is True


def test_assignment_split_after_equals_accepts_crlf() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN =\r\n'26.0.0';\r\n"
    assert checker._constant_assigns(source, "26.0.0") is True

def test_crlf_in_an_earlier_initializer_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN=\"26\"; function f(){ const a =\r\n0, BUILDER_PIN=\"bad\"; }\n"
    assert checker._constant_assigns(source, "26") is False

def test_blank_line_in_an_earlier_initializer_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN=\"26\"; function f(){ const a =\n\n0, BUILDER_PIN=\"bad\"; }\n"
    assert checker._constant_assigns(source, "26") is False


def test_later_template_mention_does_not_reject_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = 'export const BUILDER_PIN = "26.0.0";\nconsole.log(BUILDER_PIN, `ready`);\n'
    assert checker._constant_assigns(source, "26.0.0") is True


def test_following_template_is_a_continuation() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26.15.3'\n`wrong`\n"
    assert checker._constant_assigns(source, "26.15.3") is False

def test_same_line_template_is_not_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26.15.3'`suffix`;\n"
    assert checker._constant_assigns(source, "26.15.3") is False

def test_interpolated_template_continuation_is_not_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26.15.3'`${value}`;\n"
    assert checker._constant_assigns(source, "26.15.3") is False

def test_interpolated_template_on_the_next_line_is_not_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26.15.3'\n`${value}`\n"
    assert checker._constant_assigns(source, "26.15.3") is False

def test_nested_template_continuation_is_not_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "const t = `${(() => { const BUILDER_PIN = '1'`tag`; })()}`;\n"
    assert checker._constant_assigns(source, "1") is False

def test_nested_template_on_the_next_line_is_not_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "const t = `${(() => { const BUILDER_PIN = '1'\n`tag`; })()}`;\n"
    assert checker._constant_assigns(source, "1") is False

def test_multiline_interpolation_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; function f(){ const a = `x${\n x\n}`, BUILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "ok") is False


def test_keyword_operator_newline_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; function f(){ const a = typeof\n value, BUILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "ok") is False


def test_generic_default_comma_is_not_a_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; const {a = make<A, BUILDER_PIN>()} = obj;\n"
    assert checker._constant_assigns(source, "ok") is True


def test_array_generic_default_comma_is_not_a_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; const [a = make<A, BUILDER_PIN>()] = obj;\n"
    assert checker._constant_assigns(source, "ok") is True


def test_property_keyword_newline_is_not_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; function f(){ const a = obj.satisfies\n"
        " value, BUILDER_PIN = 'bad'; }\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_spaced_property_keyword_newline_is_not_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; function f(){ const a = obj. satisfies\n"
        " value, BUILDER_PIN = 'bad'; }\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_optional_property_keyword_newline_is_not_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; function f(){ const a = obj?.satisfies\n"
        " value, BUILDER_PIN = 'bad'; }\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_member_newline_keyword_is_not_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; function f(){ const a = obj.\n"
        " satisfies\n value, BUILDER_PIN = 'bad'; }\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_comparison_before_greater_call_is_a_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; const {a = foo < bar, BUILDER_PIN = 'bad' > (0)} = obj;\n"
    assert checker._constant_assigns(source, "ok") is False


def test_comparison_comma_is_a_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; const {a = 1 < 2, BUILDER_PIN = 'bad'} = obj;\n"
    assert checker._constant_assigns(source, "ok") is False


def test_instanceof_newline_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; function f(){ const a = value instanceof\n"
        " Object, BUILDER_PIN = 'bad'; }\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_crlf_interpolation_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; function f(){ const a = `x${\r\n x\r\n}`, BUILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "ok") is False


def test_interpolation_declaration_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; '
        'const x = `${(() => { const BUILDER_PIN = "wrong"; })()}`;\n'
    )
    assert checker._constant_assigns(source, "26") is False

def test_comment_in_interpolation_keeps_the_declaration() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; '
        'const x = `${(() => { const/**/BUILDER_PIN = "wrong"; })()}`;\n'
    )
    assert checker._constant_assigns(source, "26") is False

def test_template_literal_text_still_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "export const BUILDER_PIN = '26';\n"
        "const x = `export const BUILDER_PIN = 'wrong';`;\n"
    )
    assert checker._constant_assigns(source, "26") is True

def test_arrow_in_type_annotation_does_not_hide_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; '
        "function f() { const x: Foo<() => void, number> = y, "
        'BUILDER_PIN = "wrong"; }\n'
    )
    assert checker._constant_assigns(source, "26") is False

def test_arrow_type_annotation_keeps_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = 'export const BUILDER_PIN: Select<() => void, string> = "26";\n'
    assert checker._constant_assigns(source, "26") is True

def test_top_level_arrow_annotation_keeps_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = 'const BUILDER_PIN: () => string = "26";\n'
    assert checker._constant_assigns(source, "26") is True

def test_regex_after_generic_implements_list_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = 'class C implements A<T>, B<U> {} /const BUILDER_PIN = "26";/;\n'
    assert checker._constant_assigns(source, "26") is False

def test_void_return_type_keeps_a_declaration_slash_as_regex() -> None:
    checker = _fixture._load_checker_module()
    source = "function f(): void {} /const BUILDER_PIN = 'wrong';/;\nconst BUILDER_PIN = '1';\n"
    assert checker._constant_assigns(source, "1") is True

def test_void_operator_object_still_exposes_a_declaration() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '1'; void {} /const BUILDER_PIN = 'wrong';/;\n"
    assert checker._constant_assigns(source, "1") is False

def test_generic_call_comma_does_not_hide_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; '
        'function f() { const OTHER = foo<A, B>(), BUILDER_PIN = "wrong"; }\n'
    )
    assert checker._constant_assigns(source, "26") is False

def test_combining_mark_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN=\"26\"; function f(){ const a\u0301=0, BUILDER_PIN=\"bad\"; }\n"
    assert checker._constant_assigns(source, "26") is False

def test_escaped_identifier_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN=\"26\"; function f(){ const a\\u0301=0, BUILDER_PIN=\"bad\"; }\n"
    assert checker._constant_assigns(source, "26") is False

def test_escaped_identifier_start_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN=\"26\"; function f(){ const \\u0061=0, BUILDER_PIN=\"bad\"; }\n"
    assert checker._constant_assigns(source, "26") is False

def test_zwnj_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN=\"26\"; function f(){ const a\u200c=0, BUILDER_PIN=\"bad\"; }\n"
    assert checker._constant_assigns(source, "26") is False

def test_combining_mark_suffix_is_not_the_pin() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'; const BUILDER_PIN\u0301 = 'other';\n"
    assert checker._constant_assigns(source, "26") is True

def test_zwnj_suffix_is_not_the_pin() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'; const BUILDER_PIN\u200c = 'other';\n"
    assert checker._constant_assigns(source, "26") is True

def test_zwj_suffix_is_not_the_pin() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'; const BUILDER_PIN\u200d = 'other';\n"
    assert checker._constant_assigns(source, "26") is True

def test_escaped_suffix_is_not_the_pin() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'; const BUILDER_PIN\\u0301 = 'other';\n"
    assert checker._constant_assigns(source, "26") is True

def test_brace_escaped_pin_spelling_is_still_the_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'; function f(){ const \\u{42}UILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "26") is False

def test_mid_escaped_pin_spelling_is_still_the_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'; function f(){ const BUILD\\u0045R_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "26") is False

def test_escaped_pin_spelling_is_still_the_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'; function f(){ const \\u0042UILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "26") is False

def test_comparison_comma_still_separates_bindings() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; '
        'function f() { const OTHER = a < b, BUILDER_PIN = "wrong"; }\n'
    )
    assert checker._constant_assigns(source, "26") is False

def test_parenthesized_generic_call_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; '
        'function f() { const a = (factory)<A,B>(), BUILDER_PIN = "bad"; }\n'
    )
    assert checker._constant_assigns(source, "26") is False

def test_optional_generic_call_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; '
        'function f() { const a = factory?.<A,B>(), BUILDER_PIN = "bad"; }\n'
    )
    assert checker._constant_assigns(source, "26") is False

def test_regex_brace_in_interpolation_is_not_a_declaration() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const bait = `${ /}/.test('x') ? `\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "` : '' }`;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_regex_after_for_await_does_not_declare_the_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "async function scan(xs) { "
        "for await (const x of xs) /;export const BUILDER_PIN = '26.15.3';/.test(x); "
        "}"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_semicolon_on_the_next_line_terminates_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26.15.3'\n;\n[0]"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_regex_after_a_control_condition_does_not_hide_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = "if (true) /`/.test('');\nexport const BUILDER_PIN = '26.15.3';\n"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_division_after_a_call_does_not_hide_the_assignment() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = foo() / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26.15.3';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_assignment_split_after_equals_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN =\n '26.15.3';\n"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_assignment_after_a_same_line_statement_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "const unrelated = 1; export const BUILDER_PIN = '26.15.3';\n"
    assert checker._constant_assigns(source, "26.15.3") is True


def test_line_comment_ends_at_every_line_terminator() -> None:
    checker = _fixture._load_checker_module()
    for terminator in ("\r", "\u2028", "\u2029"):
        source = (
            "export const BUILDER_PIN = '26.15.3'\n"
            f"// comment{terminator}[0];\n"
        )
        assert checker._constant_assigns(source, "26.15.3") is False


def test_await_does_not_hide_a_different_call() -> None:
    checker = _fixture._load_checker_module()
    fork = "await guard();\nawait new();\n"
    published = "await old();\nawait new();\n"
    upstream = "await old();\n"
    assert checker._has_executable_drop(fork, published, upstream) is True


def test_private_field_hash_line_is_a_note() -> None:
    checker = _fixture._load_checker_module()
    fork = "class Box {\n    #guard = authorize();\n    run() {}\n}\n"
    published = "class Box {\n    run() {}\n}\n"
    upstream = "class Box {\n    run() {}\n}\n"
    assert checker._has_executable_drop(fork, published, upstream) is False


def test_bom_after_private_field_name_is_an_executable_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "class Box {\n    #x" + chr(0xFEFF) + "= 1;\n    run() {}\n}\n"
    published = "class Box {\n    run() {}\n}\n"
    assert checker._has_executable_drop(fork, published, published) is False


def test_unicode_private_field_is_an_executable_drop() -> None:
    checker = _fixture._load_checker_module()
    published = "class Box {\n    run() {}\n}\n"
    for line in (
        "    #\u2118 = authorize();",
        "    #a\u00b7b = authorize();",
        "    #a\\u{61} = authorize();",
    ):
        fork = "class Box {\n" + line + "\n    run() {}\n}\n"
        assert checker._has_executable_drop(fork, published, published) is False, line


def test_multiline_template_slash_star_does_not_hide_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "const message = `\n/*\n`;\nguard();\nanchor();\n"
    published = "const message = `\n/*\n`;\nanchor();\n"
    assert checker._has_executable_drop(fork, published, published) is True


def test_template_interpolation_drop_is_executable() -> None:
    checker = _fixture._load_checker_module()
    fork = "const message = `\n${guard()};\n`;\nanchor();\n"
    published = "const message = `\n`;\nanchor();\n"
    assert checker._has_executable_drop(fork, published, published) is True

def test_escaped_docstring_delimiter_does_not_hide_trailing_code() -> None:
    checker = _fixture._load_checker_module()
    quote = '"' * 3
    line = quote + "a \\" + quote + " # text " + quote + "; run()\n"
    assert checker._has_executable_drop(line + "base()\n", "base()\n", "base()\n") is True


def test_identifier_continuation_closes_a_parameter_list() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "function f\u0301(): void {} /const BUILDER_PIN='v';/;",
        "function f\u200c(): void {} /const BUILDER_PIN='v';/;",
        "function f\u200d(): void {} /const BUILDER_PIN='v';/;",
        "function f\\u{61}(): void {} /const BUILDER_PIN='v';/;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "v") is False, source


def test_optional_chain_heritage_keeps_the_class_body() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "class C extends ns?.Base<T> {} /const BUILDER_PIN='v';/;",
        "class C extends ns?.[base]<T> {} /const BUILDER_PIN='v';/;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "v") is False, source


def test_postfix_boundary_beats_a_following_index() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN='v'; const x=a++\n[1], BUILDER_PIN;",
        "const BUILDER_PIN='v'; const x=a!\n[1], BUILDER_PIN;",
        "const BUILDER_PIN='v'; const x=a\n![1], BUILDER_PIN;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "v") is True, source


def test_interpolation_regex_keeps_a_balanced_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN='v'; const t = `${(()=>{ const x=/r/\nx, BUILDER_PIN; })()}`;"
    assert checker._constant_assigns(source, "v") is True


def test_slash_star_inside_docstring_does_not_hide_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = '"""doc\n/* text\n"""\nguard()\nbase()\n'
    assert checker._has_executable_drop(fork, "base()\n", "base()\n") is True


def test_regex_brace_inside_interpolation_does_not_hide_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "const message = `${ /}/.test(x) && (\nguard(),\ntrue)\n}`\nanchor()\n"
    published = "const message = `${ /}/.test(x) && (\ntrue)\n}`\nanchor()\n"
    assert checker._has_executable_drop(fork, published, published) is True
    klass = "const message = `${ /[}]/.test(x) && (\nguard(),\ntrue)\n}`\nanchor()\n"
    kept = "const message = `${ /[}]/.test(x) && (\ntrue)\n}`\nanchor()\n"
    assert checker._has_executable_drop(klass, kept, kept) is True
    division = "const message = `${1 / 2}`;\nguard()\nanchor()\n"
    divided = "const message = `${1 / 2}`;\nanchor()\n"
    assert checker._has_executable_drop(division, divided, divided) is True
    quoted = "const message = `${'a' / 'b'}`;\nguard()\nanchor()\n"
    quoted_kept = "const message = `${'a' / 'b'}`;\nanchor()\n"
    assert checker._has_executable_drop(quoted, quoted_kept, quoted_kept) is True


def test_hash_comment_backtick_does_not_hide_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "# documentation `\nguard()\nupstream()\n"
    assert checker._has_executable_drop(fork, "upstream()\n", "upstream()\n") is True
    trailing = "keep(); # note `\nguard()\nupstream()\n"
    assert checker._has_executable_drop(trailing, "keep();\nupstream()\n", "upstream()\n") is True


def test_unspaced_python_comment_is_not_an_executable_drop() -> None:
    checker = _fixture._load_checker_module()
    assert checker._has_executable_drop("#comment\nshared()\n", "shared()\n", "shared()\n") is False
    assert checker._has_executable_drop("/* hidden */\nshared()\n", "shared()\n", "shared()\n") is True





def test_code_after_docstring_closer_is_an_executable_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = '"""doc\n"""; guard()\nkeep()\n'
    assert checker._has_executable_drop(fork, "keep()\n", "keep()\n") is True


def test_code_after_single_quoted_docstring_closer_is_an_executable_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "'''doc\n'''; guard()\nkeep()\n"
    assert checker._has_executable_drop(fork, "keep()\n", "keep()\n") is True



def test_prefixed_docstring_does_not_hide_a_following_drop() -> None:
    checker = _fixture._load_checker_module()
    heads = (
        'r"""docs\ntext\n"""\n',
        'u"""docs\ntext\n"""\n',
        "rf'''docs\ntext\n'''\n",
    )
    for head in heads:
        fork = f"{head}guard()\nkeep()\n"
        published = f"{head}keep()\n"
        assert checker._has_executable_drop(fork, published, "keep()\n") is True, head




def test_dollar_and_unicode_private_fields_are_executable() -> None:
    checker = _fixture._load_checker_module()
    assert checker._has_executable_drop("#$guard = 1;\nshared()\n", "shared()\n", "shared()\n") is False
    assert checker._has_executable_drop("#é = 1;\nshared()\n", "shared()\n", "shared()\n") is False


def test_zwnj_private_field_drop_is_executable() -> None:
    checker = _fixture._load_checker_module()
    fork = "class C {\n    #x\u200c = guard();\n    run() {}\n}\n"
    published = "class C {\n    run() {}\n}\n"
    upstream = "class C {\n    run() {}\n}\n"
    assert checker._has_executable_drop(fork, published, upstream) is False


def test_zwj_private_field_drop_is_executable() -> None:
    checker = _fixture._load_checker_module()
    fork = "class C {\n    #x\u200d = guard();\n    run() {}\n}\n"
    published = "class C {\n    run() {}\n}\n"
    upstream = "class C {\n    run() {}\n}\n"
    assert checker._has_executable_drop(fork, published, upstream) is False


def test_rust_inner_attribute_executes() -> None:
    checker = _fixture._load_checker_module()
    assert checker._hash_line_executes("#![no_std]") is True
    assert checker._hash_line_executes("#! [no_std]") is True
    assert checker._hash_line_executes("#!/*crate*/[no_std]") is True
    assert checker._hash_line_executes("# ! [no_std]") is True
    assert checker._hash_line_executes("#!/usr/bin/env python") is False
    assert checker._hash_line_executes("# kept comment") is False


def test_comment_between_hash_and_directive_executes() -> None:
    checker = _fixture._load_checker_module()
    assert checker._hash_line_executes("#/**/include <stdio.h>") is True
    assert checker._hash_line_executes("# /* enabled */ if(FORK)") is True
    assert checker._hash_line_executes("# /* prose */ kept comment") is False


def test_adjacent_assignment_rewrites_are_not_drops() -> None:
    checker = _fixture._load_checker_module()
    long_a = "a" * 40
    long_b = "b" * 40
    long_x = "x" * 40
    long_y = "y" * 40
    upstream = f"A = '{long_a}'\nB = '{long_b}'\n"
    fork = f"A = '{long_x}'\nB = '{long_y}'\n"
    assert checker._has_executable_drop(fork, fork, upstream) is False


def test_structured_pin_rejects_a_value_below_the_exact_pin(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [BUILDER_CONSTANT],
        }
    ]
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": DESKTOP_MANIFEST,
                        "field": "devDependencies",
                        "exact": "26.15.3",
                    }
                ],
            }
        )
        + "\n"
    )
    manifest_before = (
        json.dumps({"devDependencies": {"electron-builder": "26.15.2"}}) + "\n"
    )
    manifest_wrong = (
        json.dumps({"devDependencies": {"electron-builder": "1.0.0"}}) + "\n"
    )
    repo = _init_repo(tmp_path, "structured-below-exact")
    _commit_file(repo, DESKTOP_MANIFEST, manifest_before, "base manifest")
    _commit_file(
        repo,
        BUILDER_CONSTANT,
        "export const BUILDER_PIN = '26.15.3'\n",
        "base constant",
    )
    _commit_spec(repo, _class_spec(contracts=contract))
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    sync_merge = _publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {DESKTOP_MANIFEST: manifest_wrong},
        "chore: replay a value below the exact pin",
    )
    _point_fork_at(repo, sync_merge)
    _write_ledger(
        repo,
        _entry(
            "G-PIN",
            "desktop pin",
            commits=pin_commit,
            owned_files=["docs/fork-npm-pins.json"],
        ),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_absent_declared_constant_is_not_exempt(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [BUILDER_CONSTANT],
        }
    ]
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": DESKTOP_MANIFEST,
                        "field": "devDependencies",
                        "exact": "26.15.3",
                    }
                ],
            }
        )
        + "\n"
    )
    manifest = json.dumps({"devDependencies": {"electron-builder": "26.15.3"}}) + "\n"
    repo = _init_repo(tmp_path, "missing-constant")
    _commit_file(repo, DESKTOP_MANIFEST, manifest, "base manifest")
    _commit_file(repo, BUILDER_CONSTANT, "export const BUILDER_PIN = '26.15.3'\n", "base constant")
    _commit_spec(repo, _class_spec(contracts=contract))
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    _git(repo, "checkout", "upstream-main")
    _git(repo, "rm", BUILDER_CONSTANT)
    _git(repo, "commit", "-m", "upstream deletes the constant")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-PIN", "desktop pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_pin_named_manifest_without_spec_is_not_exempt(tmp_path: Path) -> None:
    manifest = "website/package.json"
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "left-pad",
                        "manifest": manifest,
                        "field": "dependencies",
                        "exact": "1.3.0",
                    }
                ],
            }
        )
        + "\n"
    )
    repo = _init_repo(tmp_path, "dynamic-manifest")
    _commit_file(repo, manifest, '{"dependencies":{"left-pad":"1.0.0"}}\n', "base manifest")
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, manifest, '{"dependencies":{"left-pad":"9.0.0"}}\n', "upstream manifest")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-PIN", "website pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def _lock(version: str) -> str:
    return (
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/undici": {"version": version}},
            }
        )
        + "\n"
    )


def test_clean_merge_of_an_upstream_lock_below_the_pin_is_not_exempt(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path, "upstream-lock-below-pin")
    _commit_file(
        repo,
        "package.json",
        json.dumps({"name": "root", "overrides": {"undici@^6": "6.29.0"}}) + "\n",
        "base manifest",
    )
    _commit_file(repo, "package-lock.json", _lock("6.29.0"), "base lock")
    _commit_spec(repo, _class_spec())
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(
        repo,
        "docs/fork-npm-pins.json",
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "undici",
                        "override": "undici@^6",
                        "floor": "6.29.1",
                        "resolved": "6.29.1",
                    }
                ],
            }
        )
        + "\n",
        "declare floor",
    )
    _commit_file(
        repo,
        "package.json",
        json.dumps({"name": "root", "overrides": {"undici@^6": "6.29.1"}}) + "\n",
        "raise the override",
    )
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "note.txt", "upstream\n", "upstream note")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-PIN", "undici floor", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_clean_merge_rejects_a_pin_without_a_bound(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "pin-without-bound")
    _commit_file(repo, "package-lock.json", _lock("6.29.0"), "base lock")
    _commit_spec(repo, _class_spec())
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(
        repo,
        "docs/fork-npm-pins.json",
        json.dumps(
            {
                "version": 1,
                "pins": [{"package": "undici", "resolved": "6.29.1"}],
            }
        )
        + "\n",
        "declare an unbound pin",
    )
    _commit_file(repo, "package-lock.json", _lock("1.0.0"), "fork lock")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "note.txt", "upstream\n", "upstream note")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-PIN", "unbound pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_clean_merge_rejects_a_non_object_pin(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "non-object-pin")
    _commit_file(repo, "package-lock.json", _lock("6.29.1"), "base lock")
    _commit_spec(repo, _class_spec())
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(
        repo,
        "docs/fork-npm-pins.json",
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {"package": "undici", "exact": "6.29.1"},
                    42,
                ],
            }
        )
        + "\n",
        "declare a malformed pin list",
    )
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "note.txt", "upstream\n", "upstream note")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-PIN", "malformed pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_unlisted_dist_splice_is_not_exempt(tmp_path: Path) -> None:
    extra = "plugins/kanban/dashboard/dist/extra.js"
    repo = _init_repo(tmp_path, "unlisted-dist")
    _commit_file(repo, extra, "a\nb\nc\n", "base bundle")
    _commit_spec(repo, _class_spec(build=[BUILD_OUTPUT]))
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, extra, "FORK\nb\nc\n", "fork bundle")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, extra, "a\nb\nUPSTREAM\n", "upstream bundle")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-NOTE", "unrelated", commits="none", owned_files=["note.txt"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_unlisted_workflow_taking_upstream_is_not_exempt(tmp_path: Path) -> None:
    extra = ".github/workflows/extra.yml"
    repo = _init_repo(tmp_path, "unlisted-workflow")
    _commit_file(repo, extra, "fork-workflow\n", "base workflow")
    _commit_spec(repo, _class_spec(workflows=[WORKFLOW]))
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "note.txt", "fork\n", "fork note")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, extra, "upstream-workflow\n", "upstream workflow")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-NOTE", "unrelated", commits="none", owned_files=["note.txt"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_continued_builder_pin_is_not_exempt(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [BUILDER_CONSTANT],
        }
    ]
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": DESKTOP_MANIFEST,
                        "field": "devDependencies",
                        "exact": "26.15.3",
                    }
                ],
            }
        )
        + "\n"
    )
    repo = _init_repo(tmp_path, "continued-constant")
    _commit_file(
        repo,
        DESKTOP_MANIFEST,
        json.dumps({"devDependencies": {"electron-builder": "26.15.3"}}) + "\n",
        "base manifest",
    )
    _commit_file(
        repo,
        BUILDER_CONSTANT,
        "export const BUILDER_PIN = '26.15.3'\n=== '26.15.3';\n",
        "continued constant",
    )
    _commit_spec(repo, _class_spec(contracts=contract))
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "note.txt", "upstream\n", "upstream note")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-PIN", "desktop pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_shipped_builder_pin_followed_by_a_function_is_exempt(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [BUILDER_CONSTANT],
        }
    ]
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": DESKTOP_MANIFEST,
                        "field": "devDependencies",
                        "exact": "26.15.3",
                    }
                ],
            }
        )
        + "\n"
    )
    source = "export const BUILDER_PIN = '26.15.3'\n\nexport function adapt() {\n  return BUILDER_PIN\n}\n"
    repo = _init_repo(tmp_path, "shipped-constant")
    _commit_file(
        repo,
        DESKTOP_MANIFEST,
        json.dumps({"devDependencies": {"electron-builder": "26.15.3"}}) + "\n",
        "base manifest",
    )
    _commit_file(repo, BUILDER_CONSTANT, source, "shipped constant")
    _commit_spec(repo, _class_spec(contracts=contract))
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "note.txt", "upstream\n", "upstream note")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-PIN", "desktop pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _is_exempt(repo, sync_merge) is True


def test_clean_merge_without_a_lock_still_checks_the_manifest(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [BUILDER_CONSTANT],
        }
    ]
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": DESKTOP_MANIFEST,
                        "field": "devDependencies",
                        "exact": "26.15.3",
                    }
                ],
            }
        )
        + "\n"
    )
    repo = _init_repo(tmp_path, "manifest-without-lock")
    _commit_file(
        repo,
        DESKTOP_MANIFEST,
        json.dumps({"devDependencies": {"electron-builder": "1.0.0"}}) + "\n",
        "base manifest",
    )
    _commit_file(
        repo,
        BUILDER_CONSTANT,
        "export const BUILDER_PIN = '26.15.3'\n",
        "matching constant",
    )
    _commit_spec(repo, _class_spec(contracts=contract))
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "note.txt", "upstream\n", "upstream note")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-PIN", "desktop pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_unlisted_dist_and_workflow_use_their_class() -> None:
    checker = _fixture._load_checker_module()
    spec = json.loads(_class_spec(build=[BUILD_OUTPUT], workflows=[WORKFLOW]))
    assert (
        checker._classify_sync_path(
            "plugins/kanban/dashboard/dist/extra.js", spec, set(), []
        )
        == "build-output"
    )
    assert (
        checker._classify_sync_path(".github/workflows/extra.yml", spec, set(), [])
        == "auth-hold"
    )


def test_clean_owned_source_without_spec_is_not_exempt(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "owned-no-spec")
    _commit_file(
        repo,
        "owned.py",
        "VALUE = 'base'\nUNCHANGED = 1\nKEEP = 1\n",
        "base",
    )
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(
        repo,
        "owned.py",
        "VALUE = 'fork'\nUNCHANGED = 1\nKEEP = 1\n",
        "fork customization",
    )
    _git(repo, "checkout", "upstream-main")
    _commit_file(
        repo,
        "owned.py",
        "VALUE = 'base'\nUNCHANGED = 1\nKEEP = 2\n",
        "upstream addition",
    )
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-OWNED", "owned source", commits="none", owned_files=["owned.py"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_regex_after_generic_class_declaration_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = "class C extends Base<T> {} /;export const BUILDER_PIN = '26';/.test(s);\n"
    assert checker._constant_assigns(source, "26") is False


def test_regex_after_return_newline_block_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = "function f() { return\n{} /;export const BUILDER_PIN = '26';/.test(s);\n}\n"
    assert checker._constant_assigns(source, "26") is False


def test_combining_mark_private_field_is_executable() -> None:
    checker = _fixture._load_checker_module()
    assert checker._hash_line_executes("#e\u0301 = 1;") is True
    assert checker._has_executable_drop("#e\u0301 = 1;\nshared()\n", "shared()\n", "shared()\n") is False


def test_regex_after_labeled_block_following_a_ternary_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = true ? 1 : 2\n"
        "label: {}\n"
        "/;export const BUILDER_PIN = '26';/.test('');\n"
        "export const BUILDER_PIN = 'wrong' + '';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is False


def test_clean_owned_addition_without_spec_is_not_exempt(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "owned-addition")
    _commit_file(repo, "core.py", "BASE = 1\n", "base")
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "fork_only.py", "FORK = 1\n", "fork file")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "owned.py", "VALUE = 1\n", "upstream addition")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry("G-OWNED", "owned source", commits="none", owned_files=["owned.py"]),
    )
    assert _is_exempt(repo, sync_merge) is False


def test_division_after_generic_class_expression_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class Named<T> extends Base {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_generic_function_expression_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = function foo<T>() {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_template_block_comment_keeps_the_real_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const value = `${function(){return/*\n"
        "*/{} /}/.test('');}}`;\n"
        "export const BUILDER_PIN = '26';\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_division_after_typed_function_expression_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = function foo<T>(): T {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_implements_class_expression_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class Named<T> implements Base {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_regex_after_asi_return_class_declaration_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "function f() { return\n"
        "class C {} /;export const BUILDER_PIN = '26';/.test(s);\n"
        "}\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_division_after_extends_and_implements_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class extends Base implements I {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_comma_separated_implements_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class implements I, J {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_arrow_inside_a_type_argument_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class extends Base<() => void> {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_typescript_assertion_continuation_rejects_a_resolved_pin() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'\nas string;\n"
    assert checker._constant_assigns(source, "26") is False
    source = "export const BUILDER_PIN = '26'\nsatisfies string;\n"
    assert checker._constant_assigns(source, "26") is False


def test_division_after_a_multiline_object_property_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = {\n"
        " key: {} / 2 }; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_string_inside_template_control_keeps_the_real_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const text = `${function(){if ('x') /}/.test('');}}`;\n"
        "export const BUILDER_PIN = '26';\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_division_after_conditional_return_type_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = function foo<T>(): T extends U ? V : W {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_object_property_following_a_ternary_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = { key: true ? 1 : 2,\n"
        " next: {} / 2 }; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_parenthesized_conditional_branch_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = function foo<T>(): T extends U ? (V) : W {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_a_multiline_return_type_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = function foo<T>():\n"
        "T {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_regex_after_async_separated_by_a_newline_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = "const x = async\nfunction f() {} /;export const BUILDER_PIN = '26';/.test(s);\n"
    assert checker._constant_assigns(source, "26") is False


def test_division_after_negative_return_type_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = function(): -1 {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_class_heritage_expression_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class extends class {} {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_keyof_parenthesized_type_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = function foo<T>(): T extends U ? keyof (V) : W {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_named_heritage_class_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class extends class Inner {} {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_named_outer_heritage_class_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = class Named extends class Inner {} {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_division_after_keyword_property_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = obj.return / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong' + '';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_expression_declaration_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "namespace A {\n"
        "export const BUILDER_PIN = '26.15.3'\n"
        "}\n"
        "namespace B {\n"
        "export const BUILDER_PIN = 'wrong' + ''\n"
        "}\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_division_after_spaced_keyword_property_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = obj. return / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_division_after_keyword_property_across_a_newline_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = obj.\n"
        "return / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_prefixed_expression_declaration_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "namespace A {\n"
        'export const BUILDER_PIN = "26";\n'
        "}\n"
        'namespace B { export const BUILDER_PIN = "wrong" + ""; }\n'
    )
    assert checker._constant_assigns(source, "26") is False


def test_split_expression_declaration_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "export const BUILDER_PIN = '26.15.3';\n"
        "export const\n"
        "BUILDER_PIN = compute();\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_split_literal_declaration_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "export\nconst\nBUILDER_PIN = '26';\n"
    assert checker._constant_assigns(source, "26") is True


def test_division_after_unfinished_ternary_object_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = flag ? 1\n"
        ": {} / 2; export const BUILDER_PIN = '26.15.3';\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is True


def test_newline_in_unfinished_ternary_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok';\n"
        "const x = flag ? 1\n"
        ": function() {} / (() => { const BUILDER_PIN = 'bad'; return 1; })() / 2;"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_regex_after_label_following_optional_chain_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = obj?.prop\n"
        "label: {}\n"
        "/;export const BUILDER_PIN = 'wrong';/.test('');\n"
        "export const BUILDER_PIN = '26';\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_regex_after_label_following_nullish_coalescing_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = a ?? b\n"
        "label: {}\n"
        "/;export const BUILDER_PIN = 'wrong';/.test('');\n"
        "export const BUILDER_PIN = '26';\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_comma_binding_expression_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'export const BUILDER_PIN = "26";\n'
        "namespace N { export const other = 0, BUILDER_PIN = compute(); }\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_comma_binding_literal_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "export const other = 0, BUILDER_PIN = '26';\n"
    assert checker._constant_assigns(source, "26") is True


def test_non_exported_expression_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "export const BUILDER_PIN = '26';\n"
        "{ const BUILDER_PIN = compute(); }\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_let_binding_expression_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "export const BUILDER_PIN = '26';\n"
        "{ let BUILDER_PIN = compute(); }\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_non_null_assertion_plus_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = 'export const BUILDER_PIN = "26"! + "wrong";\n'
    assert checker._constant_assigns(source, "26") is False


def test_non_null_assertion_index_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'![0];\n"
    assert checker._constant_assigns(source, "26") is False


def test_bare_non_null_assertion_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = 'export const BUILDER_PIN = "26"!;\n'
    assert checker._constant_assigns(source, "26") is True

def test_division_after_non_null_assertion_is_not_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'! / 2;\n"
    assert checker._constant_assigns(source, "26") is False

def test_division_after_non_null_assertion_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = '26'; function f(){ const a = x! / 2, BUILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "26") is False


def test_chained_non_null_assertion_division_is_not_a_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'!! / 2;\n"
    assert checker._constant_assigns(source, "ok") is False


def test_combining_mark_before_slash_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok';\n"
        "const n = obj.e\u0301 / (() => { const BUILDER_PIN = 'bad'; return 2; })() / 1;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_zwnj_before_slash_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok';\n"
        "const n = obj.e\u200c / (() => { const BUILDER_PIN = 'bad'; return 2; })() / 1;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_zwj_before_slash_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok';\n"
        "const n = obj.e\u200d / (() => { const BUILDER_PIN = 'bad'; return 2; })() / 1;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_braced_unicode_escape_before_slash_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26.15.3'; const x=foo\\u{61} / "
        "(()=>{const BUILDER_PIN='wrong'; return 2;})() / 1;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_braced_unicode_escape_assertion_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26.15.3'; const x=foo\\u{61}! / "
        "(()=>{const BUILDER_PIN='wrong'; return 2;})() / 1;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_keyword_property_assertion_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26.15.3'; const x=obj.return! / "
        "(()=>{const BUILDER_PIN='wrong'; return 2;})() / 1;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_spaced_keyword_property_assertion_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26.15.3'; const x=obj. return! / "
        "(()=>{const BUILDER_PIN='wrong'; return 2;})() / 1;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_chained_keyword_property_assertion_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26.15.3'; const x=obj.return!! / "
        "(()=>{const BUILDER_PIN='wrong'; return 2;})() / 1;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_object_expression_assertion_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26.15.3'; const x={a:1}! / "
        "(()=>{const BUILDER_PIN='wrong'; return 2;})() / 1;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_class_expression_assertion_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26.15.3'; const x=class{}! / "
        "(()=>{const BUILDER_PIN='wrong'; return 2;})() / 1;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_function_expression_assertion_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26.15.3'; const x=function(){}! / "
        "(()=>{const BUILDER_PIN='wrong'; return 2;})() / 1;\n"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_combining_mark_generic_call_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const other=f\u0301<A, BUILDER_PIN>(), BUILDER_PIN='26';\n"
    assert checker._constant_assigns(source, "26") is True


def test_zwnj_generic_call_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const other=f\u200c<A, BUILDER_PIN>(), BUILDER_PIN='26';\n"
    assert checker._constant_assigns(source, "26") is True


def test_zwj_generic_call_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const other=f\u200d<A, BUILDER_PIN>(), BUILDER_PIN='26';\n"
    assert checker._constant_assigns(source, "26") is True


def test_braced_unicode_escape_generic_call_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const other=foo\\u{61}<A, BUILDER_PIN>(), BUILDER_PIN='26';\n"
    assert checker._constant_assigns(source, "26") is True


def test_interpolation_object_division_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x = `${{} / "
        "(() => { const BUILDER_PIN='bad'; return 1; })() / 2 }`;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_interpolation_function_division_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x = `${ function(){} / "
        "(() => { const BUILDER_PIN='bad'; return 1; })() / 2 }`;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_interpolation_class_division_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x = `${ class{} / "
        "(() => { const BUILDER_PIN='bad'; return 1; })() / 2 }`;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_interpolation_async_function_division_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x = `${ async function(){} / "
        "(() => { const BUILDER_PIN='bad'; return 1; })() / 2 }`;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_interpolation_escape_division_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const aa=4; const x = `${ a\\u{61} / "
        "(() => { const BUILDER_PIN='bad'; return 1; })() / 2 }`;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_interpolation_escape_assertion_keeps_a_nested_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x = `${ a\\u{61}! / "
        "(() => { const BUILDER_PIN='bad'; return 1; })() / 2 }`;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_division_after_comparison_object_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = 1 > {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`;\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_division_after_shift_object_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = 1 >> {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`;\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_division_after_comparison_chain_object_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = 1 < 2 > {} / 3; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`;\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_property_colon_comparison_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = { v: 1 < 2 > {} / 2 }; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_computed_property_colon_comparison_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = { [v]: 1 < 2 > {} / 2 }; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_ternary_colon_comparison_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = cond ? a : 1 < 2 > {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_label_colon_comparison_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "label: 1 < 2 > {} / 2; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_quoted_property_colon_comparison_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x = { 'v': 1 < 2 > {} / 2 }; const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_case_colon_comparison_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "switch (n) { case 1: 1 < 2 > {} / 2; } const bait = `/\n"
        "export const BUILDER_PIN = '26';\n"
        "`; export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_escape_keyword_suffix_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; const n = a\\u{61}return / "
        "(() => { const BUILDER_PIN = 'bad'; return 2; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_escape_keyword_suffix_assertion_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; const n = a\\u{61}return! / "
        "(() => { const BUILDER_PIN = 'bad'; return 2; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_escape_keyword_suffix_in_interpolation_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; const n = `${a\\u{61}return / "
        "(() => { const BUILDER_PIN = 'bad'; return 2; })() / 2}`;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_escape_prefix_call_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x=\\u{61}if() / "
        "(()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_combining_mark_call_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x=a"
        + "\u0301"
        + "if() / (()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_zwnj_call_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x=a"
        + "\u200c"
        + "if() / (()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_zwj_call_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x=a"
        + "\u200d"
        + "if() / (()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_js_whitespace_after_typeof_keeps_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    for char in ("\u00a0", "\u000b", "\u000c", "\ufeff", "\u2003"):
        source = (
            "const BUILDER_PIN='26'; function f(){const x=typeof"
            + char
            + "\nvalue, BUILDER_PIN='bad';}\n"
        )
        assert checker._constant_assigns(source, "26") is False


def test_nbsp_between_assertions_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x={}!"
        + "\u00a0"
        + "! / (()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_nbsp_between_assertions_after_function_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x=function(){}!"
        + "\u00a0"
        + "! / (()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_nbsp_between_assertions_after_class_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x=class{}!"
        + "\u00a0"
        + "! / (()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_arrow_class_expression_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x=()=>class {} / "
        "(()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_arrow_function_expression_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='26'; const x=()=>function(){} / "
        "(()=>{const BUILDER_PIN='bad';})();\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_trailing_dot_number_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x=1. / "
        "(()=>{ const BUILDER_PIN='bad'; return 2; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_nbsp_before_assertion_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x=foo"
        + "\u00a0"
        + "! / (()=>{ const BUILDER_PIN='bad'; return 2; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_bom_before_assertion_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x=foo"
        + "\ufeff"
        + "! / (()=>{ const BUILDER_PIN='bad'; return 2; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_bom_before_slash_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN="26"; const x=foo'
        + "\ufeff"
        + '/ (()=>{const BUILDER_PIN="bad"; return 2})() / 2;\n'
    )
    assert checker._constant_assigns(source, "26") is False


def test_bom_before_member_keyword_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN="26"; const x=obj.'
        + "\ufeff"
        + 'if() / (()=>{const BUILDER_PIN="bad"; return 2})() / 2;\n'
    )
    assert checker._constant_assigns(source, "26") is False


def test_nbsp_before_generic_call_is_not_a_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN="26"; const x=fn'
        + "\u00a0"
        + "<A, BUILDER_PIN>();\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_bom_before_generic_call_is_not_a_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN="26"; const x=fn'
        + "\ufeff"
        + "<A, BUILDER_PIN>();\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_bom_before_continued_call_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = '1'; { const x = obj\n"
        + "\ufeff"
        + "(1), BUILDER_PIN = 'bad'; }\n"
    )
    assert checker._constant_assigns(source, "1") is False


def test_bom_before_continued_index_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = '1'; { const x = obj\n"
        + "\ufeff"
        + "[0], BUILDER_PIN = 'bad'; }\n"
    )
    assert checker._constant_assigns(source, "1") is False


def test_nbsp_after_return_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = '1'; function test(){ return\n"
        + "\u00a0"
        + "{} /const BUILDER_PIN = 'bad'/; }\n"
    )
    assert checker._constant_assigns(source, "1") is True


def test_nbsp_after_return_keeps_a_function_regex() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = '1'; function test(){ return\n"
        + "\u00a0"
        + "function(){} /const BUILDER_PIN = 'bad'/; }\n"
    )
    assert checker._constant_assigns(source, "1") is True


def test_nbsp_after_return_keeps_a_class_regex() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = '1'; function test(){ return\n"
        + "\u00a0"
        + "class C {} /const BUILDER_PIN = 'bad'/; }\n"
    )
    assert checker._constant_assigns(source, "1") is True




def test_bom_before_object_hides_a_template_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const x ="
        + "\ufeff"
        + "{} / 2; const bait = `/\nexport const BUILDER_PIN = '26';\n`; "
        + "export const BUILDER_PIN = 'wrong';\n"
    )
    assert checker._constant_assigns(source, "26") is False
    assert checker._constant_assigns(source, "wrong") is True


def test_nbsp_in_return_annotation_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "function f():"
        + "\u00a0"
        + "void {} /const BUILDER_PIN = 'wrong'/; export const BUILDER_PIN = '26';\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_vertical_tab_in_return_annotation_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "function f():"
        + "\u000b"
        + "void {} /const BUILDER_PIN = 'wrong'/; export const BUILDER_PIN = '26';\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_bom_in_return_annotation_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "function f():"
        + "\ufeff"
        + "void {} /const BUILDER_PIN = 'wrong'/; export const BUILDER_PIN = '26';\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_unary_plus_in_return_annotation_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; const f = function f(): +1 {}'
        " / (() => { const BUILDER_PIN = 'bad'; }) / 2;\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_unary_bang_in_return_annotation_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; const f = function f(): !1 {}'
        " / (() => { const BUILDER_PIN = 'bad'; }) / 2;\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_unary_tilde_in_return_annotation_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; const f = function f(): ~1 {}'
        " / (() => { const BUILDER_PIN = 'bad'; }) / 2;\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_star_in_return_annotation_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; const f = function f(): *1 {}'
        " / (() => { const BUILDER_PIN = 'bad'; }) / 2;\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_template_type_in_return_annotation_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        'const BUILDER_PIN = "26"; const f = function f(): `T` {}'
        " / (() => { const BUILDER_PIN = 'bad'; }) / 2;\n"
    )
    assert checker._constant_assigns(source, "26") is False




def test_string_slash_star_does_not_open_a_block_comment() -> None:
    checker = _fixture._load_checker_module()
    fork = '/* first\n*/ run("/*");\ncontinuation\nkeep();\n'
    published = '/* first\n*/ run("/*");\nkeep();\n'
    assert checker._has_executable_drop(fork, published, "keep();\n") is True


def test_template_slash_star_does_not_open_a_block_comment() -> None:
    checker = _fixture._load_checker_module()
    shared = "const text = `/*`;\nkeep();\n"
    fork = "const text = `/*`;\nguard();\nkeep();\n"
    assert checker._has_executable_drop(fork, shared, shared) is True


def test_plus_between_functions_does_not_hide_a_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = '26'; function f(): void {} + function g() {"
        " const BUILDER_PIN = 'bad'; }\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_postfix_increment_before_newline_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN='ok'; let x=1; x++\n{} /const BUILDER_PIN='bad'/;\n"
    assert checker._constant_assigns(source, "ok") is True


def test_postfix_decrement_before_newline_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN='ok'; let x=1; x--\n{} /const BUILDER_PIN='bad'/;\n"
    assert checker._constant_assigns(source, "ok") is True


def test_postfix_non_null_before_newline_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN='ok'; let x=1; x!\n{} /const BUILDER_PIN='bad'/;\n"
    assert checker._constant_assigns(source, "ok") is True


def test_unary_not_after_newline_keeps_a_regex() -> None:
    checker = _fixture._load_checker_module()
    for gap in ("\n", "\u2028"):
        source = (
            "const BUILDER_PIN = 'ok'; let x = 1; x"
            + gap
            + "! /const BUILDER_PIN = 'bad'/;"
        )
        assert checker._constant_assigns(source, "ok") is True, repr(gap)


def test_postfix_increment_before_function_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; let x=1; x++\n"
        "function f() {} /const BUILDER_PIN='bad'/;\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_postfix_increment_before_class_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; let x=1; x++\n"
        "class C {} /const BUILDER_PIN='bad'/;\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_combining_mark_heritage_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "class C extends B\u0301<T> {} /const BUILDER_PIN='bad'/; "
        "const BUILDER_PIN='ok';\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_combining_mark_implements_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "class C implements I\u0301<T> {} /const BUILDER_PIN='bad'/; "
        "const BUILDER_PIN='ok';\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_zwnj_heritage_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "class C extends B\u200c<T> {} /const BUILDER_PIN='bad'/; "
        "const BUILDER_PIN='ok';\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_zwj_heritage_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "class C extends B\u200d<T> {} /const BUILDER_PIN='bad'/; "
        "const BUILDER_PIN='ok';\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_braced_escape_heritage_keeps_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "class C extends B\\u{301}<T> {} /const BUILDER_PIN='bad'/; "
        "const BUILDER_PIN='ok';\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_braced_escape_heritage_does_not_hide_a_division() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN='ok'; const C=class extends B\\u{61} {} / (() => {const BUILDER_PIN='bad'; return 1;})() / 2;",
        "const BUILDER_PIN='ok'; const C=class extends B\\u{61}<T> {} / (() => {const BUILDER_PIN='bad'; return 1;})() / 2;",
        "const BUILDER_PIN='ok'; const C=class implements B\\u{61} {} / (() => {const BUILDER_PIN='bad'; return 1;})() / 2;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "ok") is False, source


def test_identifier_continuation_is_not_an_infix_keyword() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN='ok'\nas\u0301();",
        "const BUILDER_PIN='ok'\nas$();",
        "const BUILDER_PIN='ok'\nas\\u{301}();",
        "const BUILDER_PIN='ok'\nin\u200c();",
    )
    for source in sources:
        assert checker._constant_assigns(source, "ok") is True, source


def test_generic_return_annotation_does_not_assign_regex_text() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "function f(): string | Promise<string> { return ''; }\n/const BUILDER_PIN = 'v';/;",
        "function f(): T extends U ? number : Promise<string> { return ''; }\n/const BUILDER_PIN = 'v';/;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "v") is False, source


def test_postfix_increment_closes_the_initializer() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN = 'v';\nlet a = 0;\nconst x = a++\nx, BUILDER_PIN;",
        "const BUILDER_PIN = 'v';\nlet a = 0;\nconst x = a--\nx, BUILDER_PIN;",
        "const BUILDER_PIN = 'v';\nlet a = 0;\nconst x = a!\nx, BUILDER_PIN;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "v") is True, source


def test_postfix_assertion_before_newline_closes_the_initializer() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN = '26.0.0'!\n[1].forEach(f);",
        "const BUILDER_PIN = '26.0.0'++\n[1].forEach(f);",
        "const BUILDER_PIN = '26.0.0'--\n[1].forEach(f);",
        "const BUILDER_PIN = '26.0.0'\n![1].forEach(f);",
    )
    for source in sources:
        assert checker._constant_assigns(source, "26.0.0") is True, source
    assert checker._constant_assigns("const BUILDER_PIN = '26.0.0'![0];", "26.0.0") is False
    assert checker._constant_assigns("const BUILDER_PIN = '26.0.0'\n[1].forEach(f);", "26.0.0") is False


def test_expression_keyword_after_opener_is_not_heritage() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN='ok'; const x = [function() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2];",
        "const BUILDER_PIN='ok'; const x = (function() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2);",
        "const BUILDER_PIN='ok'; foo(function() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2);",
        "const BUILDER_PIN='ok'; foo(1, function() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2);",
        "const BUILDER_PIN='ok'; const x = [class {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2];",
        "const BUILDER_PIN='ok'; const x = (class {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2);",
        "const BUILDER_PIN='ok'; foo(class {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2);",
    )
    for source in sources:
        assert checker._constant_assigns(source, "ok") is False, source


def test_escaped_identifier_suffix_is_not_a_keyword_operator() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN = 'v';\nconst a\\u{61}typeof = 0;\nconst x = a\\u{61}typeof\nx, BUILDER_PIN;",
        "const BUILDER_PIN = 'v';\nconst a" + chr(0x0301) + "typeof = 0;\nconst x = a" + chr(0x0301) + "typeof\nx, BUILDER_PIN;",
        "const BUILDER_PIN = 'v';\nconst a" + chr(0x200C) + "typeof = 0;\nconst x = a" + chr(0x200C) + "typeof\nx, BUILDER_PIN;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "v") is True, source


def test_nbsp_before_label_colon_does_not_hide_a_binding() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN='ok'; label" + chr(0x00A0) + ": a < b > {} / (()=>{const BUILDER_PIN='bad';return 2;})() / 2;",
        "const BUILDER_PIN='ok'; x\nlabel: a < b > {} / (()=>{const BUILDER_PIN='bad';return 2;})() / 2;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "ok") is False, source


def test_decoded_return_keyword_hides_a_regex_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN = 'ok'; const n = \\u{72}eturn / "
        "(() => { const BUILDER_PIN = 'bad'; return 2; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is True


def test_nbsp_separated_binding_rejects_a_mismatch() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; function f(){ const\u00a0BUILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "ok") is False


def test_vertical_tab_separated_binding_rejects_a_mismatch() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; function f(){ const\u000bBUILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "ok") is False


def test_unicode_space_separated_binding_rejects_a_mismatch() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN = 'ok'; function f(){ const\u2003BUILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "ok") is False


def test_regex_after_nested_generic_class_does_not_assign() -> None:
    checker = _fixture._load_checker_module()
    source = "class C extends Base<Map<T>> {} /;export const BUILDER_PIN = '26';/.test(s);\n"
    assert checker._constant_assigns(source, "26") is False


def test_arrow_in_generic_heritage_does_not_assign_regex_text() -> None:
    checker = _fixture._load_checker_module()
    source = "class C extends Base<() => void> {} /;export const BUILDER_PIN = '26';/.test(s);\n"
    assert checker._constant_assigns(source, "26") is False


def test_arrow_in_class_type_parameters_keeps_a_later_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "class C<T extends () => void> {}\n"
        "/export const BUILDER_PIN = 'bad'/;\n"
        "export const BUILDER_PIN = '26';\n"
    )
    assert checker._constant_assigns(source, "26") is True


def test_dotted_generic_heritage_does_not_assign_regex_text() -> None:
    checker = _fixture._load_checker_module()
    source = "class C extends ns.Base<T> {} /;export const BUILDER_PIN = '26';/.test(s);\n"
    assert checker._constant_assigns(source, "26") is False


def test_computed_generic_heritage_does_not_assign_regex_text() -> None:
    checker = _fixture._load_checker_module()
    source = "class C extends ns[base]<T> {} /;export const BUILDER_PIN = '26';/.test(s);\n"
    assert checker._constant_assigns(source, "26") is False


def test_nested_initializer_declaration_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "export const BUILDER_PIN = '26';\n"
        "const f = () => { const BUILDER_PIN = compute(); };\n"
    )
    assert checker._constant_assigns(source, "26") is False


def test_destructured_binding_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'; {const {BUILDER_PIN} = source;}\n"
    assert checker._constant_assigns(source, "26") is False


def test_array_destructured_binding_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'; {const [BUILDER_PIN] = source;}\n"
    assert checker._constant_assigns(source, "26") is False


def test_renamed_destructured_binding_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'; {const {other: BUILDER_PIN} = source;}\n"
    assert checker._constant_assigns(source, "26") is False


def test_nested_destructured_binding_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'; {const {a: {BUILDER_PIN}} = source;}\n"
    assert checker._constant_assigns(source, "26") is False


def test_rest_destructured_binding_rejects_a_matching_literal() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'; {const {...BUILDER_PIN} = source;}\n"
    assert checker._constant_assigns(source, "26") is False


def test_renamed_away_destructure_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'; {const {BUILDER_PIN: alias} = source;}\n"
    assert checker._constant_assigns(source, "26") is True


def test_type_annotation_does_not_hide_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN = '26'; { const a: string = '', BUILDER_PIN = 'bad'; }\n"
    assert checker._constant_assigns(source, "26") is False


def test_typed_literal_still_assigns() -> None:
    checker = _fixture._load_checker_module()
    source = "export const BUILDER_PIN: string = '26';\n"
    assert checker._constant_assigns(source, "26") is True


def test_non_null_assertion_does_not_hide_a_later_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "var BUILDER_PIN = '26'!, BUILDER_PIN = 'bad';\n"
    assert checker._constant_assigns(source, "26") is False


def test_bom_before_object_expression_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x="
        + "\ufeff"
        + "{} / (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_bom_before_function_expression_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x="
        + "\ufeff"
        + "function(){return 1} / (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_bom_before_class_expression_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x="
        + "\ufeff"
        + "class{m(){return 1}} / (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_bom_before_async_function_expression_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x="
        + "\ufeff"
        + "async function(){return 1} / (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_bom_between_params_and_body_rejects_a_hidden_binding() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const BUILDER_PIN='ok'; const x=function()"
        + "\ufeff"
        + "{return 1} / (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;\n"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_escaped_opener_delimiter_does_not_hide_a_drop() -> None:
    checker = _fixture._load_checker_module()
    for quote in ('"""', "'''"):
        fork = quote + "doc \\" + quote + "\ncontinuation\n" + quote + "\nguard()\nkeep()\n"
        published = quote + "doc \\" + quote + "\ncontinuation\n" + quote + "\nkeep()\n"
        assert checker._has_executable_drop(fork, published, "keep()\n") is True, quote




def test_statement_label_keeps_a_function_declaration() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN='1'; switch (0) { case 0: function f() {} /const BUILDER_PIN=\"bad\"/; }",
        "const BUILDER_PIN='1'; switch (0) { default: function f() {} /const BUILDER_PIN=\"bad\"/; }",
        "const BUILDER_PIN='1'; label: function f() {} /const BUILDER_PIN=\"bad\"/;",
        "const BUILDER_PIN='1'; switch (0) { case 0: class C {} /const BUILDER_PIN=\"bad\"/; }",
        "const BUILDER_PIN='1'; switch (0) { case 0: async function f() {} /const BUILDER_PIN=\"bad\"/; }",
    )
    for source in sources:
        assert checker._constant_assigns(source, "1") is True, source
    expressions = (
        "const BUILDER_PIN='ok'; const x = { key: function() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2 };",
        "const BUILDER_PIN='ok'; const x = { 'key': function() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2 };",
        "const BUILDER_PIN='ok'; const x = { [key]: function() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2 };",
        "const BUILDER_PIN='ok'; const x = cond ? 0 : function() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2;",
        "const BUILDER_PIN='ok'; const x = cond ? 0 : class {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2;",
    )
    for source in expressions:
        assert checker._constant_assigns(source, "ok") is False, source


def test_backtick_inside_docstring_does_not_hide_a_drop() -> None:
    checker = _fixture._load_checker_module()
    for quote, inner in (('"""', '"'), ("'''", "'")):
        fork = quote + "doc\n" + inner + " ` example\n" + quote + "\nguard()\nkeep()\n"
        published = quote + "doc\n" + inner + " ` example\n" + quote + "\nkeep()\n"
        assert checker._has_executable_drop(fork, published, "keep()\n") is True, quote


def test_escaped_docstring_delimiter_does_not_open_a_block_comment() -> None:
    checker = _fixture._load_checker_module()
    for quote in ('"""', "'''"):
        fork = quote + "doc \\" + quote + " /*\ntext\n" + quote + "\nguard()\nkeep()\n"
        published = quote + "doc \\" + quote + " /*\ntext\n" + quote + "\nkeep()\n"
        assert checker._has_executable_drop(fork, published, "keep()\n") is True, quote


def test_prefix_update_after_keyword_keeps_the_initializer() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN='ok'; { const other=typeof ++\n x, BUILDER_PIN='bad'; }",
        "const BUILDER_PIN='ok'; { const other=void ++\n x, BUILDER_PIN='bad'; }",
        "const BUILDER_PIN='ok'; { const other=typeof --\n x, BUILDER_PIN='bad'; }",
        "const BUILDER_PIN='ok'; { const other=type\\u{6f}f ++\n x, BUILDER_PIN='bad'; }",
    )
    for source in sources:
        assert checker._constant_assigns(source, "ok") is False, source
    assert checker._constant_assigns(
        "const BUILDER_PIN='ok'; { const other=a++\n x, BUILDER_PIN='bad'; }",
        "ok",
    ) is True
    assert checker._constant_assigns(
        "const BUILDER_PIN='ok'; { const other=obj.typeof++\n x, BUILDER_PIN='bad'; }",
        "ok",
    ) is True


def test_identifier_suffix_does_not_open_an_object() -> None:
    checker = _fixture._load_checker_module()
    sources = (
        "const BUILDER_PIN='ok';\na\\u{61}return\n{} /const BUILDER_PIN = 'bad'/;",
        "const BUILDER_PIN='ok';\na\\u{61}typeof\n{} /const BUILDER_PIN = 'bad'/;",
        "const BUILDER_PIN='ok';\na" + chr(0x0301) + "return\n{} /const BUILDER_PIN = 'bad'/;",
        "const BUILDER_PIN='ok';\na" + chr(0x200C) + "return\n{} /const BUILDER_PIN = 'bad'/;",
        "const BUILDER_PIN='ok';\na" + chr(0x200D) + "return\n{} /const BUILDER_PIN = 'bad'/;",
    )
    for source in sources:
        assert checker._constant_assigns(source, "ok") is True, source
    assert checker._constant_assigns(
        "const BUILDER_PIN='ok'; return {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2;",
        "ok",
    ) is False


def test_hash_comment_does_not_open_a_block_comment() -> None:
    checker = _fixture._load_checker_module()
    assert checker._has_executable_drop(
        "x = 1 # /*\nguard()\nkeep()\n",
        "x = 1 # /*\nkeep()\n",
        "keep()\n",
    ) is True


def test_identifier_of_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        "const of = 8; const x = of / (() => { const BUILDER_PIN = 'bad'; return 2; })() / 2; const BUILDER_PIN = 'ok';",
        "ok",
    ) is False
    assert checker._constant_assigns(
        "for (const x of /const BUILDER_PIN = 'bad'/ ) {} const BUILDER_PIN = 'ok';",
        "ok",
    ) is True
    assert checker._constant_assigns(
        "const of = 8; for (const x of of / (() => { const BUILDER_PIN = 'bad'; return 2; })() / 2) {} const BUILDER_PIN = 'ok';",
        "ok",
    ) is False


def test_function_after_else_keeps_the_following_regex() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        "if (false) {} else function f() {} /const BUILDER_PIN = 'bad'/; const BUILDER_PIN = 'ok';",
        "ok",
    ) is True
    assert checker._constant_assigns(
        "if (false) {} else /const BUILDER_PIN = 'bad'/; const BUILDER_PIN = 'ok';",
        "ok",
    ) is True
    assert checker._constant_assigns(
        "if (false) {} else (function(){ return 1 }) / (() => { const BUILDER_PIN = 'bad'; return 2 })(); const BUILDER_PIN = 'ok';",
        "ok",
    ) is False


def test_destructuring_for_of_keeps_a_later_value_of() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        "for (const {x} of of / (() => { const BUILDER_PIN = 'bad'; return 1; })() / 2) {}\nconst BUILDER_PIN = 'ok';",
        "ok",
    ) is False
    assert checker._constant_assigns(
        "for (const [x] of of / (() => { const BUILDER_PIN = 'bad'; return 1; })() / 2) {}\nconst BUILDER_PIN = 'ok';",
        "ok",
    ) is False
    assert checker._constant_assigns(
        "for (const {x} of /const BUILDER_PIN = 'bad'/ ) {} const BUILDER_PIN = 'ok';",
        "ok",
    ) is True
    assert checker._constant_assigns(
        "for (const [x] of /const BUILDER_PIN = 'bad'/ ) {} const BUILDER_PIN = 'ok';",
        "ok",
    ) is True


def test_regex_character_class_does_not_open_a_block_comment() -> None:
    checker = _fixture._load_checker_module()
    assert checker._has_executable_drop(
        "const pattern = /[/*]/;\nguard();\nkeep();\n",
        "const pattern = /[/*]/;\nkeep();\n",
        "keep();\n",
    ) is True
    assert checker._has_executable_drop(
        "const pattern = `${\n/[/*]/\n}`;\nguard();\nkeep();\n",
        "const pattern = `${\n/[/*]/\n}`;\nkeep();\n",
        "keep();\n",
    ) is True
    assert checker._has_executable_drop(
        "const pattern = /[/*]/;\nkeep();\n",
        "const pattern = /[/*]/;\nkeep();\n",
        "keep();\n",
    ) is False



def test_backtick_inside_regex_does_not_mask_following_code() -> None:
    checker = _fixture._load_checker_module()
    assert checker._has_executable_drop(
        "const re = /`/;\nguard();\nkeep();\n",
        "const re = /`/;\nkeep();\n",
        "keep();\n",
    ) is True
    assert checker._has_executable_drop(
        "const re = /`/;\nkeep();\n",
        "const re = /`/;\nkeep();\n",
        "keep();\n",
    ) is False


def test_member_for_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        'obj.for(x, of / (() => {const BUILDER_PIN = "bad"; return 1;})() / 2); const BUILDER_PIN = "26";',
        "26",
    ) is False
    assert checker._constant_assigns(
        'obj.for await (x, of / (() => {const BUILDER_PIN = "bad"; return 1;})() / 2); const BUILDER_PIN = "26";',
        "26",
    ) is False


def test_member_of_does_not_consume_the_for_of_operator() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        'for (obj.of of /const BUILDER_PIN = "bad"/g) {} const BUILDER_PIN = "26";',
        "26",
    ) is True
    assert checker._constant_assigns(
        'for (obj.of of of / (() => {const BUILDER_PIN = "bad"; return 1;})() / 2) {} const BUILDER_PIN = "26";',
        "26",
    ) is False


def test_initializer_of_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        'const BUILDER_PIN = "26"; for (let i = of / (() => {const BUILDER_PIN = "bad"; return 1;})() / 2; i < 1; i++) {}',
        "26",
    ) is False
    assert checker._constant_assigns(
        'for (const {x = 1} of /const BUILDER_PIN = "bad"/) {} const BUILDER_PIN = "26";',
        "26",
    ) is True


def test_slash_after_comment_or_regex_does_not_hide_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "const ratio = 1 /* comment */ / `foo\n/*\n`;\nguard()\nanchor()\n"
    upstream = "const ratio = 1 /* comment */ / `foo\n/*\n`;\nanchor()\n"
    assert checker._has_executable_drop(fork, upstream, "anchor()\n") is True
    fork_regex = "const ratio = /a/ / `foo\n/*\n`;\nguard()\nanchor()\n"
    upstream_regex = "const ratio = /a/ / `foo\n/*\n`;\nanchor()\n"
    assert checker._has_executable_drop(fork_regex, upstream_regex, "anchor()\n") is True
    assert checker._has_executable_drop(upstream, upstream, "anchor()\n") is False


def test_class_extends_new_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        "const BUILDER_PIN='ok'; const C = class extends new Foo() {} / (()=>{const BUILDER_PIN='bad'; return 1;})() / 2;",
        "ok",
    ) is False
    assert checker._constant_assigns(
        "const BUILDER_PIN='ok'; class C extends new Foo() {} /const BUILDER_PIN = 'bad'/;",
        "ok",
    ) is True


def test_braced_escape_does_not_split_a_binding_keyword() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        "const BUILDER_PIN = '1'; let aaconst = 0; a\\u{61}const\nBUILDER_PIN;",
        "1",
    ) is True
    assert checker._constant_assigns(
        "const BUILDER_PIN = '1'; a\\u{61}let\nBUILDER_PIN;",
        "1",
    ) is True
    assert checker._constant_assigns(
        "const BUILDER_PIN = '1'; a\\u{61}var\nBUILDER_PIN;",
        "1",
    ) is True
    assert checker._constant_assigns(
        "const BUILDER_PIN = '1'; \\u{61}const\nBUILDER_PIN;",
        "1",
    ) is True
    assert checker._constant_assigns(
        "const BUILDER_PIN = '1'; }const BUILDER_PIN = 'bad';",
        "1",
    ) is False


def test_comment_newline_in_interpolation_does_not_hide_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "const t = `${\n(()=> { function f() { return/*\n*/ {} /}}}/; }\nguard();\n})()\n}`;\nanchor();\n"
    upstream = "const t = `${\n(()=> { function f() { return/*\n*/ {} /}}}/; }\n})()\n}`;\nanchor();\n"
    assert checker._has_executable_drop(fork, upstream, "anchor();\n") is True
    assert checker._has_executable_drop(upstream, upstream, "anchor();\n") is False


def test_parenthesized_for_of_keeps_a_regex() -> None:
    checker = _fixture._load_checker_module()
    assert checker._constant_assigns(
        "let x; for ((x) of /const BUILDER_PIN = 'bad'/.source) {} const BUILDER_PIN='ok';",
        "ok",
    ) is True
    assert checker._constant_assigns(
        "let obj; for ((obj.x) of /const BUILDER_PIN = 'bad'/.source) {} const BUILDER_PIN='ok';",
        "ok",
    ) is True
    assert checker._constant_assigns(
        "let x; for ((x) of of / (() => {const BUILDER_PIN = 'bad'; return 1;})() / 2) {} const BUILDER_PIN='ok';",
        "ok",
    ) is False


def test_private_keyword_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    hidden = (
        "class C { #return; m(){ const x = this.#return / (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;"
        " const BUILDER_PIN='ok'; } }"
    )
    assert checker._constant_assigns(hidden, "ok") is False
    member = (
        "class C { #break; m(){ const x = this.#break\n/ (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;"
        " const BUILDER_PIN='ok'; } }"
    )
    assert checker._constant_assigns(member, "ok") is False


def test_private_control_call_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    for name in ("if", "while", "for", "with"):
        source = (
            "class C { #" + name + "() {} f() { const x = this.#" + name
            + "() / (() => { const BUILDER_PIN = 'wrong'; }) / 2; } } "
            "const BUILDER_PIN = '26.15.3';"
        )
        assert checker._constant_assigns(source, "26.15.3") is False, name


def test_other_id_characters_keep_a_declaration_list() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        "function f(){ const \u2118=1, BUILDER_PIN='wrong'; } const BUILDER_PIN='ok';",
        "function f(){ const \u212e=1, BUILDER_PIN='wrong'; } const BUILDER_PIN='ok';",
        "function f(){ const a\u00b7b=1, BUILDER_PIN='wrong'; } const BUILDER_PIN='ok';",
        "function f(){ const a\u0387b=1, BUILDER_PIN='wrong'; } const BUILDER_PIN='ok';",
    ):
        assert checker._constant_assigns(source, "ok") is False


def test_asi_terminated_statement_keeps_a_regex() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        "const BUILDER_PIN='ok'; while(true){ break\n/const BUILDER_PIN='wrong'/; }",
        "const BUILDER_PIN='ok'; while(true){ continue\n/const BUILDER_PIN='wrong'/; }",
        "const BUILDER_PIN='ok'; debugger\n/const BUILDER_PIN='wrong'/;",
    ):
        assert checker._constant_assigns(source, "ok") is True


def test_labeled_break_keeps_a_regex() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        "label: while (true) { break label\n/const BUILDER_PIN = 'ok';/.test(''); }",
        "label: while (true) { continue label\n/const BUILDER_PIN = 'ok';/.test(''); }",
    ):
        assert checker._constant_assigns(source, "ok") is False, source


def test_comment_closer_keeps_a_regex_before_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "/*\n*/ /[/*]/;\nguard();\nkeep();\n"
    published = "/*\n*/ /[/*]/;\nkeep();\n"
    assert checker._has_executable_drop(fork, published, published) is True


def test_block_comment_newline_keeps_a_regex_before_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "let n=0;\nn++ /*\n*/ {} /[`]/ .test('x');\nguard();\nkeep();\n"
    published = "let n=0;\nn++ /*\n*/ {} /[`]/ .test('x');\nkeep();\n"
    assert checker._has_executable_drop(fork, published, "keep();\n") is True



def test_other_id_operand_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        "const \u2118=1; const x=\u2118 / (()=>{const BUILDER_PIN='bad';return 1})() / 2; const BUILDER_PIN='26.15.3';",
        "const a\u00b7=1; const x=a\u00b7 / (()=>{const BUILDER_PIN='bad';return 1})() / 2; const BUILDER_PIN='26.15.3';",
    ):
        assert checker._constant_assigns(source, "26.15.3") is False, source


def test_private_heritage_keeps_a_division() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "class C { #Foo; m(){ const x=class extends this.#Foo {} / "
        "(()=>{const BUILDER_PIN='bad';return 1})() / 2; } } "
        "const BUILDER_PIN='26.15.3';"
    )
    assert checker._constant_assigns(source, "26.15.3") is False


def test_private_field_reference_keeps_a_template_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "const x = this.#value + `\n/* text\n`;\nguard();\nkeep();\n"
    published = "const x = this.#value + `\n/* text\n`;\nkeep();\n"
    assert checker._has_executable_drop(fork, published, published) is True


def test_letter_number_and_connector_keep_the_declaration_list() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        "const BUILDER_PIN='ok'; { const a\u203f=1, BUILDER_PIN=1; }",
        "const BUILDER_PIN='ok'; { const \u2163=1, BUILDER_PIN=1; }",
        "const \u2163=1; const x=\u2163 / (()=>{const BUILDER_PIN='bad';return 1})() / 2; const BUILDER_PIN='ok';",
        "const a\u203f=1; const x=a\u203f / (()=>{const BUILDER_PIN='bad';return 1})() / 2; const BUILDER_PIN='ok';",
    ):
        assert checker._constant_assigns(source, "ok") is False, source


def test_division_after_generic_assertion_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        "const x = value as Foo<number> / (() => { const BUILDER_PIN = 'bad'; return 1; })() / 2; const BUILDER_PIN = 'ok';",
        "const x = value satisfies Foo<number> / (() => { const BUILDER_PIN = 'bad'; return 1; })() / 2; const BUILDER_PIN = 'ok';",
        "const x = Foo<number> / (() => { const BUILDER_PIN = 'bad'; return 1; })() / 2; const BUILDER_PIN = 'ok';",
    ):
        assert checker._constant_assigns(source, "ok") is False, source


def test_tagged_template_heritage_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    source = (
        "const value = class extends Foo`x` {} / (() => { const BUILDER_PIN = 'bad'; return 1; })() / 2; "
        "const BUILDER_PIN = 'ok';"
    )
    assert checker._constant_assigns(source, "ok") is False


def test_assertion_type_arguments_are_not_bindings() -> None:
    checker = _fixture._load_checker_module()
    asserted = (
        "type BUILDER_PIN = number; type Foo<A, B> = number; const value = 1; "
        "const x = value as Foo<number, BUILDER_PIN>; const BUILDER_PIN = 'ok';"
    )
    satisfied = "const x = value satisfies Foo<number, BUILDER_PIN>; const BUILDER_PIN = 'ok';"
    assert checker._constant_assigns(asserted, "ok") is True
    assert checker._constant_assigns(satisfied, "ok") is True


def test_negation_after_a_control_condition_hides_a_regex() -> None:
    checker = _fixture._load_checker_module()
    for keyword in ("if", "while", "for", "with"):
        source = f"const BUILDER_PIN = 'ok'; {keyword} (flag)! /const BUILDER_PIN = 'bad'/;"
        assert checker._constant_assigns(source, "ok") is True, keyword
    call = (
        "const x = foo()! / (() => { const BUILDER_PIN = 'bad'; return 1; })() / 2; "
        "const BUILDER_PIN = 'ok';"
    )
    assert checker._constant_assigns(call, "ok") is False


def test_other_id_postfix_assertion_rejects_a_bait_pin() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        'const BUILDER_PIN="ok"; const x=℘! / (()=>{const BUILDER_PIN="bad";return 1})() / 2;',
        'const BUILDER_PIN="ok"; const x=a·! / (()=>{const BUILDER_PIN="bad";return 1})() / 2;',
    ):
        assert checker._constant_assigns(source, "ok") is False, source


def test_other_id_postfix_update_ends_the_initializer() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        'const BUILDER_PIN="26.15.3"; { const x=℘++\nvalue, BUILDER_PIN="bad"; }',
        'const BUILDER_PIN="26.15.3"; { const x=a·++\nvalue, BUILDER_PIN="bad"; }',
    ):
        assert checker._constant_assigns(source, "26.15.3") is True, source


def test_connector_punctuation_keeps_the_identifier() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        "const BUILDER_PIN='26.15.3'; a‿! / (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;",
        "const BUILDER_PIN='26.15.3'; a‿return / (() => { const BUILDER_PIN='wrong'; return 1; })() / 2;",
    ):
        assert checker._constant_assigns(source, "26.15.3") is False, source


def test_comparison_equals_is_not_a_type_argument() -> None:
    checker = _fixture._load_checker_module()
    source = 'const BUILDER_PIN = "26.15.3"; function f(){ const x = foo<0, BUILDER_PIN=1>(0); }'
    assert checker._constant_assigns(source, "26.15.3") is False


def test_ordinary_await_and_yield_do_not_hide_a_pin() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        'function f(){var await=8;const x=await / (()=>{const BUILDER_PIN="bad";return 1})() / 2;} const BUILDER_PIN="26.15.3";',
        'function f(){var yield=8;const x=yield / (()=>{const BUILDER_PIN="bad";return 1})() / 2;} const BUILDER_PIN="26.15.3";',
        'function async(){var await=8;const x=await / (()=>{const BUILDER_PIN="bad";return 1})() / 2;} const BUILDER_PIN="26.15.3";',
        'class C { async(){ var await=8; await / (()=>{const BUILDER_PIN="bad";return 1})() / 2; } } const BUILDER_PIN="26.15.3";',
    ):
        assert checker._constant_assigns(source, "26.15.3") is False, source
    for source in (
        'async function f(){const x=await / (()=>{const BUILDER_PIN="bad";return 1})() / 2;} const BUILDER_PIN="26.15.3";',
        'async function f(){if(x){await /const BUILDER_PIN=1/;}} const BUILDER_PIN="26.15.3";',
        'const f = async () => { await /const BUILDER_PIN=1/; }; const BUILDER_PIN="26.15.3";',
        'function* f(){if(x){yield /const BUILDER_PIN=1/;}} const BUILDER_PIN="26.15.3";',
    ):
        assert checker._constant_assigns(source, "26.15.3") is True, source


def test_earlier_return_annotation_does_not_hide_a_later_object() -> None:
    checker = _fixture._load_checker_module()
    source = 'const BUILDER_PIN="ok"; function f(): void {}; const x = {} / (() => { const BUILDER_PIN="bad"; return 2; })() / 2;'
    assert checker._constant_assigns(source, "ok") is False


def test_const_type_parameter_is_not_a_binding() -> None:
    checker = _fixture._load_checker_module()
    source = 'function f<const BUILDER_PIN extends string>(x: BUILDER_PIN){return x;} const BUILDER_PIN="26.15.3";'
    assert checker._constant_assigns(source, "26.15.3") is True


def test_null_manifest_key_is_a_change() -> None:
    checker = _fixture._load_checker_module()
    expected = {"devDependencies": {"electron-builder": "26.15.2"}}
    actual = {"devDependencies": {"electron-builder": "26.15.3", "unrelated": None}}
    pins = [{"package": "electron-builder", "field": "devDependencies"}]
    assert checker._manifest_delta_is_pinned(expected, actual, pins) is False


def test_unmatched_upstream_copy_stays_reserved() -> None:
    checker = _fixture._load_checker_module()
    upstream = "guard();\nguard();\nkeep();\n"
    fork = upstream + "guard();\n"
    published = "guard();\nkeep();\nguard();\nkeep();\n"
    assert checker._has_executable_drop(fork, published, upstream) is True


def test_line_terminator_after_postfix_starts_a_regex() -> None:
    checker = _fixture._load_checker_module()
    for prefix in ("x++\n", "x--\n", "x!\n"):
        source = 'const BUILDER_PIN = "26.15.3";\n' + prefix + "/const BUILDER_PIN = 1;/;"
        assert checker._constant_assigns(source, "26.15.3") is True, prefix


def test_masked_template_stays_an_operand() -> None:
    checker = _fixture._load_checker_module()
    fork = "const x = `t` / /[/*]/.source.length;\nguard();\nkeep();\n"
    published = "const x = `t` / /[/*]/.source.length;\nkeep();\n"
    assert checker._has_executable_drop(fork, published, "keep();\n") is True


def test_quote_in_a_comment_closer_does_not_hide_the_guard() -> None:
    checker = _fixture._load_checker_module()
    fork = "/*\n\" */ const s = '\\\nhello'; guard();\nkeep();\n"
    published = "/*\n\" */ const s = '\\\nhello';\nkeep();\n"
    assert checker._has_executable_drop(fork, published, "keep();\n") is True


def test_second_comment_does_not_hide_a_private_field() -> None:
    checker = _fixture._load_checker_module()
    fork = "class C {\n#field\n/* one */ /* two */\n;\n}\nkeep();\n"
    published = "class C {\n/* one */ /* two */\n;\n}\nkeep();\n"
    upstream = "class C {\n;\n}\nkeep();\n"
    assert checker._has_executable_drop(fork, published, upstream) is False


def test_comment_after_a_private_name_does_not_hide_the_field() -> None:
    checker = _fixture._load_checker_module()
    for fork, published in (
        (
            "class C {\n#field /* note */\n;\n}\nkeep();\n",
            "class C {\n/* note */\n;\n}\nkeep();\n",
        ),
        (
            "class C {\n#field // note\n;\n}\nkeep();\n",
            "class C {\n// note\n;\n}\nkeep();\n",
        ),
    ):
        assert checker._has_executable_drop(fork, published, "keep();\n") is False, fork


def test_preserved_duplicate_file_is_not_a_drop() -> None:
    checker = _fixture._load_checker_module()
    upstream = "guard();\nguard();\nkeep();\n"
    same = "guard();\nkeep();\nguard();\nkeep();\n"
    assert checker._has_executable_drop(same, same, upstream) is False


def test_annotated_async_function_keeps_the_regex() -> None:
    checker = _fixture._load_checker_module()
    source = 'const BUILDER_PIN="26.15.3"; async function f(): void { await /const BUILDER_PIN=1;/; }'
    assert checker._constant_assigns(source, "26.15.3") is True


def test_bom_before_a_field_semicolon_is_whitespace() -> None:
    checker = _fixture._load_checker_module()
    fork = "class C {\n#field\n\ufeff;\n}\nkeep();\n"
    published = "class C {\n\ufeff;\n}\nkeep();\n"
    upstream = "class C {\n;\n}\nkeep();\n"
    assert checker._has_executable_drop(fork, published, upstream) is False



def test_continued_string_after_a_comment_closer_keeps_the_guard() -> None:
    checker = _fixture._load_checker_module()
    fork = '/* comment */ const s = "text\\\n/*\\\nend";\nguard();\nkeep();\n'
    published = '/* comment */ const s = "text\\\n/*\\\nend";\nkeep();\n'
    assert checker._has_executable_drop(fork, published, "keep();\n") is True


def test_ordinary_yield_is_not_an_asi_keyword() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN='ok'; function f() { var yield=1; yield\n / (()=>{const BUILDER_PIN=1;})() / 2; }"
    assert checker._constant_assigns(source, "ok") is False


def test_arrow_in_type_params_keeps_const_modifier() -> None:
    checker = _fixture._load_checker_module()
    source = 'const BUILDER_PIN="26.15.3"; function f<T extends () => void, const BUILDER_PIN extends string>() {}'
    assert checker._constant_assigns(source, "26.15.3") is True
    generated = 'const BUILDER_PIN="26.15.3"; function* f<const BUILDER_PIN extends string>() {}'
    assert checker._constant_assigns(generated, "26.15.3") is True
    anonymous = 'const BUILDER_PIN="26.15.3"; function*<const BUILDER_PIN>() {}'
    assert checker._constant_assigns(anonymous, "26.15.3") is True


def test_unicode_generic_callee_is_not_a_binding() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        'const BUILDER_PIN="26.15.3"; const a = ℘ <T, BUILDER_PIN>();',
        'const BUILDER_PIN="26.15.3"; const a = a· <T, BUILDER_PIN>();',
        'const BUILDER_PIN="26.15.3"; const a = a‿ <T, BUILDER_PIN>();',
    ):
        assert checker._constant_assigns(source, "26.15.3") is True, source


def test_ordinary_await_does_not_continue_an_initializer() -> None:
    checker = _fixture._load_checker_module()
    for source in (
        'const BUILDER_PIN="26.15.3"; function f(){ const x = await\nfoo, BUILDER_PIN = 1; }',
        'const BUILDER_PIN="26.15.3"; function f(){ const x = yield\nfoo, BUILDER_PIN = 1; }',
    ):
        assert checker._constant_assigns(source, "26.15.3") is True, source


def test_class_type_default_does_not_make_the_body_an_object() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN='ok'; class C<T = string> {} /const BUILDER_PIN=1/;"
    assert checker._constant_assigns(source, "ok") is True


def test_private_heritage_member_keeps_the_class_body() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN='ok'; class Outer { #Foo; method() { class C extends this.#Foo <T> {} /const BUILDER_PIN=1/; } }"
    assert checker._constant_assigns(source, "ok") is True


def test_ternary_colon_on_the_next_line_keeps_the_binding() -> None:
    checker = _fixture._load_checker_module()
    source = "const BUILDER_PIN='26.15.3'; function f() { const other = flag ? 1\n: 2, BUILDER_PIN = 1; }"
    assert checker._constant_assigns(source, "26.15.3") is False


def test_hash_comment_backtick_does_not_hide_a_guard() -> None:
    checker = _fixture._load_checker_module()
    fork = "#comment mentions `\nguard();\nkeep();\n"
    published = "#comment mentions `\nkeep();\n"
    assert checker._has_executable_drop(fork, published, "keep();\n") is True


def test_hash_comment_quote_does_not_hide_a_guard() -> None:
    checker = _fixture._load_checker_module()
    fork = 'x = 1 # "\\\nguard()\nkeep()\n'
    published = 'x = 1 # "\\\nkeep()\n'
    assert checker._has_executable_drop(fork, published, "keep()\n") is True



def test_escaped_newline_in_a_string_keeps_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = 'const s = "start\\\n/*";\nguard();\nkeep();\n'
    published = 'const s = "start\\\n/*";\nkeep();\n'
    assert checker._has_executable_drop(fork, published, published) is True


def test_preserved_duplicate_line_is_not_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "guard()\nguard()\nkeep()\n"
    upstream = "guard()\nkeep()\n"
    assert checker._has_executable_drop(fork, fork, upstream) is False


def test_reserved_upstream_copies_do_not_cover_a_dropped_duplicate() -> None:
    checker = _fixture._load_checker_module()
    upstream = "guard();\nguard();\nkeep();\n"
    dropped = "guard();\nguard();\nguard();\nkeep();\n"
    assert checker._has_executable_drop(dropped, upstream, upstream) is True
    preserved = "guard();\nguard();\nguard();\nkeep();\n"
    assert checker._has_executable_drop(preserved, preserved, upstream) is False


def test_preserved_duplicate_pair_is_not_a_drop() -> None:
    checker = _fixture._load_checker_module()
    fork = "a();\nb();\na();\nb();\nc();\n"
    upstream = "a();\nb();\nc();\n"
    assert checker._has_executable_drop(fork, fork, upstream) is False


def test_private_field_continued_on_the_next_line_is_a_drop() -> None:
    checker = _fixture._load_checker_module()
    upstream = "class C {\n}\nkeep();\n"
    for fork, published in (
        ("class C {\n#field\n;\n}\nkeep();\n", "class C {\n;\n}\nkeep();\n"),
        ("class C {\n#field\n= 1;\n}\nkeep();\n", "class C {\n= 1;\n}\nkeep();\n"),
        ("class C {\n#method\n() {}\n}\nkeep();\n", "class C {\n() {}\n}\nkeep();\n"),
        ("class C {\n#field\n\n;\n}\nkeep();\n", "class C {\n\n;\n}\nkeep();\n"),
    ):
        assert checker._has_executable_drop(fork, published, upstream) is False, fork


def test_block_comment_does_not_hide_a_continued_private_field() -> None:
    checker = _fixture._load_checker_module()
    fork = "class C {\n#field\n/* comment\nplain continuation\n*/\n;\n}\nkeep();\n"
    published = "class C {\n/* comment\nplain continuation\n*/\n;\n}\nkeep();\n"
    assert checker._has_executable_drop(fork, published, "keep();\n") is False


def test_quote_in_a_line_comment_does_not_continue_a_string() -> None:
    checker = _fixture._load_checker_module()
    fork = 'work(); // "\\\nguard();\nkeep();\n'
    published = 'work(); // "\\\nkeep();\n'
    assert checker._has_executable_drop(fork, published, "keep();\n") is True


def test_crlf_backslash_continues_a_string() -> None:
    checker = _fixture._load_checker_module()
    fork = 'const s = "x\\\r\n/* text";\nguard();\nkeep();\n'
    published = 'const s = "x\\\r\n/* text";\nkeep();\n'
    assert checker._has_executable_drop(fork, published, "keep();\n") is True


