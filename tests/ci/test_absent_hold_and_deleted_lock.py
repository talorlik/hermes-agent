"""Regressions for the 2026-11-17 update stop.

An upstream-only workflow kept absent is an auth hold, including when neither
tree has the file. A named lock that upstream deleted and the published tip
kept absent is not a pin file once the sync leaves the clean merge-tree path.
A lock the published tip still has must meet its floor.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

_HELPER_PATH = Path(__file__).with_name("test_sync_class_check.py")
_SPEC = importlib.util.spec_from_file_location("sync_class_fixture", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_fx = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_fx)

_GIT_ENV = _fx._GIT_ENV
_class_spec = _fx._class_spec
_commit_file = _fx._commit_file
_entry = _fx._entry
_git = _fx._git
_init_repo = _fx._init_repo
_is_exempt = _fx._is_exempt
_point_fork_at = _fx._point_fork_at
_publish_merge = _fx._publish_merge
_write_ledger = _fx._write_ledger

_NEW_WORKFLOW = ".github/workflows/catalog-dead-repos.yml"


def _commit_ledger(repo: Path, body: str, subject: str) -> str:
    _write_ledger(repo, body)
    _git(repo, "add", "docs/FORK_CHANGES.md")
    _git(repo, "commit", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _remove_file(repo: Path, rel: str, subject: str) -> str:
    _git(repo, "rm", rel)
    _git(repo, "commit", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _publish_without(
    repo: Path,
    first: str,
    second: str,
    removals: list[str],
    updates: dict[str, str],
    message: str,
) -> str:
    index = repo / ".git" / "absent-hold-test.index"
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
        for rel in removals:
            subprocess.run(
                ["git", "rm", "--cached", "-f", "--", rel],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
        for rel, text in updates.items():
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=repo,
                env=env,
                input=text,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}"],
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
    return _git(repo, "commit-tree", tree, "-p", first, "-p", second, "-m", message)


def _pin_text() -> str:
    return (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "undici",
                        "floor": "6.29.1",
                        "resolved": "6.29.1",
                    }
                ],
            }
        )
        + "\n"
    )


def _lock_text(version: str) -> str:
    return (
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/undici": {"version": version}},
            }
        )
        + "\n"
    )


def test_absent_upstream_workflow_is_an_auth_hold(tmp_path: Path) -> None:
    """A modify/delete workflow kept absent equals the fork parent."""
    repo = _init_repo(tmp_path, "absent-workflow")
    _commit_file(repo, _NEW_WORKFLOW, "base-workflow\n", "base workflow")
    _commit_file(
        repo,
        "docs/fork-sync-classes.json",
        _class_spec(workflows=[".github/workflows/lint.yml"]),
        "docs: classes",
    )
    _git(repo, "checkout", "-b", "fork-main")
    deleted = _remove_file(repo, _NEW_WORKFLOW, "chore: keep the new workflow absent")
    _commit_ledger(
        repo,
        _entry(
            "G-ORIGIN-WORKFLOWS",
            "origin workflows",
            commits=deleted,
            owned_files=[_NEW_WORKFLOW],
        ),
        "docs: claim the absent workflow",
    )
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, _NEW_WORKFLOW, "upstream-workflow\n", "feat: add catalog workflow")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    merge = _publish_merge(repo, fork_tip, upstream_sha, {}, "sync(upstream): hold absent workflow")
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is True


def _lock_conflict_repo(tmp_path: Path, name: str) -> tuple[Path, str, str, str]:
    """Fork edits the lock. Upstream deletes it, so the sync is not merge-tree-clean."""
    repo = _init_repo(tmp_path, name)
    _commit_file(repo, "package-lock.json", _lock_text("6.29.0"), "base lock")
    _commit_file(repo, "docs/fork-sync-classes.json", _class_spec(), "docs: classes")
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(
        repo, "docs/fork-npm-pins.json", _pin_text(), "feat(fork): declare undici floor"
    )
    _commit_ledger(
        repo,
        _entry(
            "G-NPM-PIN-REPLAY",
            "npm pins",
            commits=pin_commit,
            owned_files=["docs/fork-npm-pins.json"],
        ),
        "docs: claim the pin spec",
    )
    _commit_file(
        repo,
        "package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/undici": {"version": "6.29.0"},
                    "node_modules/left-pad": {"version": "1.0.0"},
                },
            }
        )
        + "\n",
        "chore: fork lock edit",
    )
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _remove_file(repo, "package-lock.json", "chore: drop npm lock")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    return repo, pin_commit, fork_tip, upstream_sha


def test_deleted_upstream_lock_is_not_a_pin_file(tmp_path: Path) -> None:
    """Absence on the published tip and on upstream is the take, not a pin miss."""
    repo, _pin_commit, fork_tip, upstream_sha = _lock_conflict_repo(tmp_path, "deleted-lock")
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        ["package-lock.json"],
        {},
        "sync(upstream): take the deleted lock",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is True


def test_kept_lock_below_floor_is_still_rejected(tmp_path: Path) -> None:
    """A lock the published tip still has must meet the pin floor."""
    repo, _pin_commit, fork_tip, upstream_sha = _lock_conflict_repo(tmp_path, "kept-lock")
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        [],
        {},
        "sync(upstream): keep the old lock",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is False


_DESKTOP = "apps/desktop/package.json"
_BUILDER = "apps/desktop/scripts/builder-config-26.mjs"


def _desktop_manifest(builder: str, *, name: str | None = None) -> str:
    body: dict[str, object] = {"devDependencies": {"electron-builder": builder}}
    if name is not None:
        body["name"] = name
    return json.dumps(body, separators=(",", ":")) + "\n"


def _builder_pin_spec() -> str:
    return (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": _DESKTOP,
                        "field": "devDependencies",
                        "exact": "26.15.3",
                    }
                ],
            }
        )
        + "\n"
    )


def _builder_contract() -> list[dict[str, object]]:
    return [
        {
            "package": "electron-builder",
            "manifest": _DESKTOP,
            "constants": [_BUILDER],
        }
    ]


def _conflicted_pin_repo(tmp_path: Path, name: str) -> tuple[Path, str, str]:
    """Both sides edit the same manifest line, so merge-tree leaves markers."""
    repo = _init_repo(tmp_path, name)
    _commit_file(repo, _DESKTOP, _desktop_manifest("26.15.2"), "base desktop manifest")
    _commit_file(
        repo,
        "docs/fork-sync-classes.json",
        _class_spec(contracts=_builder_contract()),
        "docs: classes",
    )
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "docs/fork-npm-pins.json", _builder_pin_spec(), "feat: pin builder")
    _commit_file(repo, _BUILDER, "export const BUILDER_PIN = '26.15.3'\n", "feat: builder pin")
    _commit_file(repo, _DESKTOP, _desktop_manifest("26.15.3"), "feat: pin builder manifest")
    pin_tip = _git(repo, "rev-parse", "HEAD")
    _commit_ledger(
        repo,
        _entry(
            "G-NPM-PIN-REPLAY",
            "npm pins",
            commits=pin_tip,
            owned_files=[
                "docs/fork-npm-pins.json",
                _DESKTOP,
                _BUILDER,
            ],
        ),
        "docs: claim the builder pin",
    )
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(
        repo,
        _DESKTOP,
        _desktop_manifest("27.0.0-alpha.6", name="upstream-desktop"),
        "feat: upstream desktop manifest",
    )
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    return repo, fork_tip, upstream_sha


def test_conflicted_manifest_uses_upstream_as_the_pin_baseline(tmp_path: Path) -> None:
    """Conflict markers are not a baseline. Only the pinned key may differ from upstream."""
    repo, fork_tip, upstream_sha = _conflicted_pin_repo(tmp_path, "conflicted-pin")
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        [],
        {_DESKTOP: _desktop_manifest("26.15.3", name="upstream-desktop")},
        "sync(upstream): replay the builder pin",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is True


def test_conflicted_manifest_rejects_an_unpinned_key(tmp_path: Path) -> None:
    """A conflicted baseline must not waive a key the pin spec does not name."""
    repo, fork_tip, upstream_sha = _conflicted_pin_repo(tmp_path, "conflicted-extra")
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        [],
        {_DESKTOP: _desktop_manifest("26.15.3", name="fork-desktop")},
        "sync(upstream): move an unpinned key",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is False



_TUI = "ui-tui/package.json"


def _tui_manifest(undici: str, *, name: str | None = None) -> str:
    body: dict[str, object] = {"dependencies": {"undici": undici}}
    if name is not None:
        body["name"] = name
    return json.dumps(body, separators=(",", ":")) + "\n"


def _tui_pin_spec() -> str:
    return (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "undici",
                        "manifest": _TUI,
                        "field": "dependencies",
                        "floor": "6.29.1",
                        "resolved": "6.29.1",
                    }
                ],
            }
        )
        + "\n"
    )


def _conflicted_field_pin_repo(
    tmp_path: Path, name: str, *, fork_manifest: str | None = None
) -> tuple[Path, str, str]:
    """A field pin with no contract, on a manifest both sides edit. No npm lock exists."""
    repo = _init_repo(tmp_path, name)
    _commit_file(repo, _TUI, _tui_manifest("6.29.0"), "base tui manifest")
    _commit_file(repo, "docs/fork-sync-classes.json", _class_spec(), "docs: classes")
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "docs/fork-npm-pins.json", _tui_pin_spec(), "feat: pin undici")
    _commit_file(
        repo, _TUI, fork_manifest or _tui_manifest("6.29.1"), "feat: pin undici manifest"
    )
    pin_tip = _git(repo, "rev-parse", "HEAD")
    _commit_ledger(
        repo,
        _entry(
            "G-NPM-PIN-REPLAY",
            "npm pins",
            commits=pin_tip,
            owned_files=["docs/fork-npm-pins.json", _TUI],
        ),
        "docs: claim the undici pin",
    )
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(
        repo,
        _TUI,
        _tui_manifest("6.28.0", name="upstream-tui"),
        "feat: upstream tui manifest",
    )
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    return repo, fork_tip, upstream_sha


def test_conflicted_field_pin_manifest_uses_upstream_as_the_baseline(tmp_path: Path) -> None:
    """With the named lock absent, a conflicted manifest is read against upstream."""
    repo, fork_tip, upstream_sha = _conflicted_field_pin_repo(tmp_path, "conflicted-field")
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        [],
        {_TUI: _tui_manifest("6.29.1", name="upstream-tui")},
        "sync(upstream): replay the undici pin",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is True


def test_conflicted_field_pin_manifest_rejects_an_unpinned_key(tmp_path: Path) -> None:
    """The upstream baseline waives the pinned key only."""
    repo, fork_tip, upstream_sha = _conflicted_field_pin_repo(tmp_path, "conflicted-field-extra")
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        [],
        {_TUI: _tui_manifest("6.29.1", name="fork-tui")},
        "sync(upstream): move an unpinned key",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is False


def test_conflicted_manifest_does_not_drop_a_fork_key(tmp_path: Path) -> None:
    """The upstream baseline is not a licence to drop fork content the pins do not name."""
    fork_manifest = (
        json.dumps(
            {"dependencies": {"undici": "6.29.1"}, "scripts": {"postinstall": "node fork.mjs"}},
            separators=(",", ":"),
        )
        + "\n"
    )
    repo, fork_tip, upstream_sha = _conflicted_field_pin_repo(
        tmp_path, "conflicted-fork-key", fork_manifest=fork_manifest
    )
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        [],
        {_TUI: _tui_manifest("6.29.1", name="upstream-tui")},
        "sync(upstream): drop the fork postinstall",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is False


def test_unparseable_fork_manifest_without_a_conflict_has_no_baseline(tmp_path: Path) -> None:
    """Only conflict markers earn the upstream baseline. A broken fork manifest does not."""
    repo = _init_repo(tmp_path, "broken-manifest")
    _commit_file(repo, _TUI, _tui_manifest("6.29.0"), "base tui manifest")
    _commit_file(repo, "docs/fork-sync-classes.json", _class_spec(), "docs: classes")
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "docs/fork-npm-pins.json", _tui_pin_spec(), "feat: pin undici")
    _commit_file(repo, _TUI, '{"dependencies": {"undici": "6.29.1"\n', "feat: break the manifest")
    pin_tip = _git(repo, "rev-parse", "HEAD")
    _commit_ledger(
        repo,
        _entry(
            "G-NPM-PIN-REPLAY",
            "npm pins",
            commits=pin_tip,
            owned_files=["docs/fork-npm-pins.json", _TUI],
        ),
        "docs: claim the undici pin",
    )
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "note.txt", "upstream\n", "docs: upstream note")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        [],
        {_TUI: _tui_manifest("6.29.1"), "note.txt": "upstream\n"},
        "sync(upstream): rewrite the broken manifest",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is False


def test_fork_only_named_lock_cannot_be_dropped(tmp_path: Path) -> None:
    """Upstream never had this lock, so its absence is a drop and not a take."""
    repo = _init_repo(tmp_path, "fork-only-lock")
    _commit_file(repo, "docs/fork-sync-classes.json", _class_spec(), "docs: classes")
    _git(repo, "checkout", "-b", "fork-main")
    pin_commit = _commit_file(
        repo, "docs/fork-npm-pins.json", _pin_text(), "feat(fork): declare undici floor"
    )
    _commit_ledger(
        repo,
        _entry(
            "G-NPM-PIN-REPLAY",
            "npm pins",
            commits=pin_commit,
            owned_files=["docs/fork-npm-pins.json"],
        ),
        "docs: claim the pin spec",
    )
    _commit_file(repo, "package-lock.json", _lock_text("6.29.0"), "chore: fork-only lock")
    fork_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "note.txt", "upstream\n", "docs: upstream note")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    merge = _publish_without(
        repo,
        fork_tip,
        upstream_sha,
        ["package-lock.json"],
        {"note.txt": "upstream\n"},
        "sync(upstream): drop the fork lock",
    )
    _point_fork_at(repo, merge)
    assert _is_exempt(repo, merge) is False
