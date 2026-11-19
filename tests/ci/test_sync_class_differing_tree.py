"""Class rules on sync merges whose tree differs from git's own merge tree.

A merge whose tree equals ``git merge-tree --write-tree`` is a sync without
looking at any class rule, so each case here commits a merge that differs.
The fixtures live in ``test_sync_class_check.py`` and are loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HELPER_PATH = Path(__file__).with_name("test_sync_class_check.py")
_SPEC = importlib.util.spec_from_file_location("sync_class_fixture_differing", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_fx = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_fx)


def test_absent_declared_constant_is_not_exempt(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": _fx.DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [_fx.BUILDER_CONSTANT],
        }
    ]
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": _fx.DESKTOP_MANIFEST,
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
    manifest_after = (
        json.dumps({"devDependencies": {"electron-builder": "26.15.3"}}) + "\n"
    )
    # No tree carries the constant the contract declares. A tree-equal merge
    # is a sync whatever the contract says, so the published tip replays the
    # pinned manifest key: a structured-pin delta, the one the contract gates.
    repo = _fx._init_repo(tmp_path, "missing-constant")
    _fx._commit_file(repo, _fx.DESKTOP_MANIFEST, manifest_before, "base manifest")
    _fx._commit_spec(repo, _fx._class_spec(contracts=contract))
    _fx._git(repo, "checkout", "-b", "fork-main")
    pin_commit = _fx._commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, "note.txt", "upstream\n", "upstream note")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")
    sync_merge = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {_fx.DESKTOP_MANIFEST: manifest_after, "note.txt": "upstream\n"},
        "chore: replay the pin without its constant",
    )
    _fx._point_fork_at(repo, sync_merge)
    _fx._write_ledger(
        repo,
        _fx._entry("G-PIN", "desktop pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _fx._is_exempt(repo, sync_merge) is False


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
    repo = _fx._init_repo(tmp_path, "dynamic-manifest")
    _fx._commit_file(repo, manifest, '{"dependencies":{"left-pad":"1.0.0"}}\n', "base manifest")
    _fx._git(repo, "checkout", "-b", "fork-main")
    pin_commit = _fx._commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, manifest, '{"dependencies":{"left-pad":"9.0.0"}}\n', "upstream manifest")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")
    # Git's merge takes upstream's 9.0.0 and is a sync. The published tip
    # replays the exact pin over it instead. That delta is confined to the
    # pinned key of the manifest the pin names, and only a class spec can
    # permit it. This repo has none.
    sync_merge = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {manifest: '{"dependencies":{"left-pad":"1.3.0"}}\n'},
        "chore: replay the pin without a class spec",
    )
    _fx._point_fork_at(repo, sync_merge)
    _fx._write_ledger(
        repo,
        _fx._entry("G-PIN", "website pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _fx._is_exempt(repo, sync_merge) is False


def test_differing_merge_of_an_upstream_lock_below_the_pin_is_not_exempt(
    tmp_path: Path,
) -> None:
    repo = _fx._init_repo(tmp_path, "upstream-lock-below-pin")
    _fx._commit_file(
        repo,
        "package.json",
        json.dumps({"name": "root", "overrides": {"undici@^6": "6.28.0"}}) + "\n",
        "base manifest",
    )
    _fx._commit_file(repo, "package-lock.json", _fx._lock("6.28.0"), "base lock")
    _fx._commit_spec(repo, _fx._class_spec())
    _fx._git(repo, "checkout", "-b", "fork-main")
    pin_commit = _fx._commit_file(
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
    _fx._commit_file(
        repo,
        "package.json",
        json.dumps({"name": "root", "overrides": {"undici@^6": "6.29.1"}}) + "\n",
        "raise the override",
    )
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, "note.txt", "upstream\n", "upstream note")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")
    _fx._write_ledger(
        repo,
        _fx._entry("G-PIN", "undici floor", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    # Git's merge carries upstream's 6.28.0 lock and is a sync. A published
    # tip that rewrites the named lock answers to the floor. Both tips below
    # change only the pinned package's version in that lock, so the floor is
    # the one thing that separates them.
    short = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {"package-lock.json": _fx._lock("6.29.0"), "note.txt": "upstream\n"},
        "chore: replay the lock short of the floor",
    )
    _fx._point_fork_at(repo, short)
    assert _fx._is_exempt(repo, short) is False

    met = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {"package-lock.json": _fx._lock("6.29.1"), "note.txt": "upstream\n"},
        "chore: replay the lock to the floor",
    )
    _fx._point_fork_at(repo, met)
    assert _fx._is_exempt(repo, met) is True


def test_differing_merge_rejects_a_pin_without_a_bound(tmp_path: Path) -> None:
    repo = _fx._init_repo(tmp_path, "pin-without-bound")
    _fx._commit_file(repo, "package-lock.json", _fx._lock("6.29.0"), "base lock")
    _fx._commit_spec(repo, _fx._class_spec())
    _fx._git(repo, "checkout", "-b", "fork-main")
    pin_commit = _fx._commit_file(
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
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, "note.txt", "upstream\n", "upstream note")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")
    # The published tip moves the named lock to the version the pin resolves.
    # A pin with neither a floor nor an exact value bounds nothing, so it
    # cannot authorize that lock delta.
    sync_merge = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {"package-lock.json": _fx._lock("6.29.1"), "note.txt": "upstream\n"},
        "chore: replay a pin that has no bound",
    )
    _fx._point_fork_at(repo, sync_merge)
    _fx._write_ledger(
        repo,
        _fx._entry("G-PIN", "unbound pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _fx._is_exempt(repo, sync_merge) is False


def test_differing_merge_rejects_a_non_object_pin(tmp_path: Path) -> None:
    repo = _fx._init_repo(tmp_path, "non-object-pin")
    _fx._commit_file(repo, "package-lock.json", _fx._lock("6.29.0"), "base lock")
    _fx._commit_spec(repo, _fx._class_spec())
    _fx._git(repo, "checkout", "-b", "fork-main")
    pin_commit = _fx._commit_file(
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
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, "note.txt", "upstream\n", "upstream note")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")
    # The published tip moves the named lock to the exact pin, the delta a
    # well-formed pin list permits. A list holding a non-object is not a
    # readable spec, so nothing in it authorizes the replay.
    sync_merge = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {"package-lock.json": _fx._lock("6.29.1"), "note.txt": "upstream\n"},
        "chore: replay a malformed pin list",
    )
    _fx._point_fork_at(repo, sync_merge)
    _fx._write_ledger(
        repo,
        _fx._entry("G-PIN", "malformed pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _fx._is_exempt(repo, sync_merge) is False


def test_unlisted_dist_splice_is_not_exempt(tmp_path: Path) -> None:
    extra = "plugins/kanban/dashboard/dist/extra.js"
    repo = _fx._init_repo(tmp_path, "unlisted-dist")
    _fx._commit_file(repo, extra, "a\nb\nc\n", "base bundle")
    _fx._commit_spec(repo, _fx._class_spec(build=[_fx.BUILD_OUTPUT]))
    _fx._git(repo, "checkout", "-b", "fork-main")
    _fx._commit_file(repo, extra, "FORK\nb\nc\n", "fork bundle")
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, extra, "UPSTREAM\nb\nc\n", "upstream bundle")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")
    # Both sides rewrite the first line, so git cannot merge the bundle. The
    # published file keeps upstream's line and adds the fork's. For a source
    # file that resolution is the conflict fit and stays a sync. A path under
    # dist/ is build output whether the spec lists it or not, and a build
    # output delta is never exempt.
    sync_merge = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {extra: "UPSTREAM\nFORK\nb\nc\n"},
        "chore: splice the unlisted bundle",
    )
    _fx._point_fork_at(repo, sync_merge)
    _fx._write_ledger(
        repo,
        _fx._entry("G-NOTE", "unrelated", commits="none", owned_files=["note.txt"]),
    )
    assert _fx._is_exempt(repo, sync_merge) is False


def test_unlisted_workflow_taking_upstream_is_not_exempt(tmp_path: Path) -> None:
    extra = ".github/workflows/extra.yml"
    repo = _fx._init_repo(tmp_path, "unlisted-workflow")
    _fx._commit_file(repo, extra, "base-workflow\n", "base workflow")
    _fx._commit_spec(repo, _fx._class_spec(workflows=[_fx.WORKFLOW]))
    _fx._git(repo, "checkout", "-b", "fork-main")
    fork_edit = _fx._commit_file(repo, extra, "fork-workflow\n", "fork workflow")
    _fx._commit_ledger(
        repo,
        _fx._entry(
            "G-ORIGIN-WORKFLOWS",
            "origin workflows",
            commits=fork_edit,
            owned_files=[extra],
        ),
    )
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, extra, "upstream-workflow\n", "upstream workflow")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")

    # Both sides edit the workflow, so the published blob is a choice and not
    # git's merge. The spec lists only lint.yml. Keeping the fork blob is
    # exempt only under the hold class: a source conflict may not keep a
    # wholesale fork file. So the first tip shows the unlisted path is held.
    held = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {extra: "fork-workflow\n"},
        "chore: hold the unlisted workflow",
    )
    _fx._point_fork_at(repo, held)
    assert _fx._is_exempt(repo, held) is True

    taken = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {extra: "upstream-workflow\n"},
        "chore: take the upstream workflow",
    )
    _fx._point_fork_at(repo, taken)
    assert _fx._is_exempt(repo, taken) is False


def test_continued_builder_pin_is_not_exempt(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": _fx.DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [_fx.BUILDER_CONSTANT],
        }
    ]
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": _fx.DESKTOP_MANIFEST,
                        "field": "devDependencies",
                        "exact": "26.15.3",
                    }
                ],
            }
        )
        + "\n"
    )
    repo = _fx._init_repo(tmp_path, "continued-constant")
    _fx._commit_file(
        repo,
        _fx.DESKTOP_MANIFEST,
        json.dumps({"devDependencies": {"electron-builder": "26.15.2"}}) + "\n",
        "base manifest",
    )
    _fx._commit_file(
        repo,
        _fx.BUILDER_CONSTANT,
        "export const BUILDER_PIN = '26.15.3'\n=== '26.15.3';\n",
        "continued constant",
    )
    _fx._commit_spec(repo, _fx._class_spec(contracts=contract))
    _fx._git(repo, "checkout", "-b", "fork-main")
    pin_commit = _fx._commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, "note.txt", "upstream\n", "upstream note")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")
    # The published tip replays the pinned manifest key to the exact pin, a
    # structured-pin delta. The contract then reads the constant, and a
    # declaration continued onto the next line assigns a comparison, not the
    # pinned literal.
    sync_merge = _fx._publish_merge(
        repo,
        fork_tip,
        upstream_sha,
        {
            _fx.DESKTOP_MANIFEST: json.dumps(
                {"devDependencies": {"electron-builder": "26.15.3"}}
            )
            + "\n",
            "note.txt": "upstream\n",
        },
        "chore: replay the pin beside a continued constant",
    )
    _fx._point_fork_at(repo, sync_merge)
    _fx._write_ledger(
        repo,
        _fx._entry("G-PIN", "desktop pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )
    assert _fx._is_exempt(repo, sync_merge) is False


def test_differing_merge_without_a_lock_still_checks_the_manifest(tmp_path: Path) -> None:
    contract = [
        {
            "manifest": _fx.DESKTOP_MANIFEST,
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [_fx.BUILDER_CONSTANT],
        }
    ]
    pins = (
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {
                        "package": "electron-builder",
                        "manifest": _fx.DESKTOP_MANIFEST,
                        "field": "devDependencies",
                        "exact": "26.15.3",
                    }
                ],
            }
        )
        + "\n"
    )
    repo = _fx._init_repo(tmp_path, "manifest-without-lock")
    _fx._commit_file(
        repo,
        _fx.DESKTOP_MANIFEST,
        json.dumps({"devDependencies": {"electron-builder": "1.0.0"}}) + "\n",
        "base manifest",
    )
    _fx._commit_file(
        repo,
        _fx.BUILDER_CONSTANT,
        "export const BUILDER_PIN = '26.15.3'\n",
        "matching constant",
    )
    _fx._commit_spec(repo, _fx._class_spec(contracts=contract))
    _fx._git(repo, "checkout", "-b", "fork-main")
    pin_commit = _fx._commit_file(repo, "docs/fork-npm-pins.json", pins, "declare pin")
    fork_tip = _fx._git(repo, "rev-parse", "HEAD")
    _fx._git(repo, "checkout", "upstream-main")
    _fx._commit_file(repo, "note.txt", "upstream\n", "upstream note")
    upstream_sha = _fx._git(repo, "rev-parse", "HEAD")
    _fx._write_ledger(
        repo,
        _fx._entry("G-PIN", "desktop pin", commits=pin_commit, owned_files=["docs/fork-npm-pins.json"]),
    )

    def replay(version: str) -> str:
        """Publish git's merge with only the pinned manifest key moved."""
        merge = _fx._publish_merge(
            repo,
            fork_tip,
            upstream_sha,
            {
                _fx.DESKTOP_MANIFEST: json.dumps(
                    {"devDependencies": {"electron-builder": version}}
                )
                + "\n",
                "note.txt": "upstream\n",
            },
            f"chore: replay electron-builder {version}",
        )
        _fx._point_fork_at(repo, merge)
        return merge

    # No tree here has a lock, so no lock gate runs. The manifest alone
    # decides: both tips move only the pinned key, the constant matches the
    # pin, and the value that misses the exact pin is the one that stops.
    assert _fx._is_exempt(repo, replay("26.15.2")) is False
    assert _fx._is_exempt(repo, replay("26.15.3")) is True
