"""Adversarial contracts for the fork-ledger verification boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from tests.ci.test_check_fork_ledger import (
    _GIT_ENV,
    _commit_existing,
    _entry,
    _git,
    _load_checker_module,
    _make_fork_repo,
    _multi_owned_feature_ledger,
    _run_checker,
    _write_ledger,
)


def test_duplicate_entry_field_is_invalid(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    entry = _entry(
        "G-FEATURE",
        "fork feature",
        commits=f"{fx['fork_a']}, {fx['fork_b']}",
        owned_files=["fork_feature.py"],
    ).replace(
        "- Intent: Keep the fork feature working.",
        "- Intent: first\n- Intent: second",
    )
    _write_ledger(repo, entry)
    code, payload = _run_checker(repo)
    assert code == 1
    assert any(
        "duplicate field: Intent" in problem
        for item in payload["invalid_entries"]
        for problem in item["problems"]
    )


def test_inline_owned_files_is_invalid(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    entry = _entry(
        "G-FEATURE",
        "fork feature",
        commits=f"{fx['fork_a']}, {fx['fork_b']}",
        owned_files=["fork_feature.py"],
    ).replace("- Owned-Files:\n  - fork_feature.py", "- Owned-Files: fork_feature.py")
    _write_ledger(repo, entry)
    code, payload = _run_checker(repo)
    assert code == 1
    assert any(
        "Owned-Files must use list form" in problem
        for item in payload["invalid_entries"]
        for problem in item["problems"]
    )


def test_duplicate_precedence_sections_are_invalid(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    body = _multi_owned_feature_ledger(fx, ("fork_feature.py", "G-FEATURE"))
    body += "\n## Path-Precedence\n- fork_feature.py: G-FEATURE\n"
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1
    assert any(
        "duplicate Path-Precedence section" in item["problem"]
        for item in payload["invalid_precedence"]
    )


def test_malformed_precedence_bullet_is_invalid(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    body = _multi_owned_feature_ledger(fx)
    body += "- fork_feature.py G-FEATURE\n"
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1
    assert any(
        "malformed Path-Precedence row" in item["problem"]
        for item in payload["invalid_precedence"]
    )


def test_relative_ledger_path_is_resolved_from_repo(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(
        repo,
        "--ledger",
        "docs/FORK_CHANGES.md",
        cwd=tmp_path,
    )
    assert code == 0, payload


def test_symlink_ledger_escape_is_rejected(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        ),
    )
    external = tmp_path / "outside.md"
    external.write_text("# external\n", encoding="utf-8")
    link = repo / "docs" / "linked-ledger.md"
    link.symlink_to(external)

    code, payload = _run_checker(repo, "--ledger", "docs/linked-ledger.md")

    assert code == 2
    assert "inside repository" in payload["error"]


def test_abbreviated_commit_claim_is_invalid(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{str(fx['fork_a'])[:7]}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert any(
        "full 40-character" in problem
        for item in payload["invalid_entries"]
        for problem in item["problems"]
    )


def test_empty_commit_range_is_invalid(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=(f"{fx['fork_a']}, {fx['fork_b']}, {fx['fork_a']}..{fx['fork_a']}"),
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert any(
        "empty commit range" in problem
        for item in payload["invalid_entries"]
        for problem in item["problems"]
    )


def test_crafted_merge_with_upstream_parent_is_not_sync_exempt(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        ),
    )
    (repo / "fork_feature.py").write_text("FORK = 999\n", encoding="utf-8")
    _git(repo, "add", "fork_feature.py")
    tree = _git(repo, "write-tree")
    crafted = subprocess.run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            _git(repo, "rev-parse", "fork-main"),
            "-p",
            _git(repo, "rev-parse", "upstream-main"),
        ],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
        input="merge: crafted sync\n",
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(repo, "update-ref", "refs/heads/fork-main", crafted)
    code, payload = _run_checker(repo)
    assert code == 1
    assert crafted in {item["sha"] for item in payload["unmapped_commits"]}


def test_self_does_not_map_edit_to_different_ledger_entry(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    body = _entry(
        "G-FEATURE",
        "fork feature",
        commits=f"{fx['fork_a']}, {fx['fork_b']}",
        owned_files=["fork_feature.py"],
    ) + _entry(
        "G-FORK-LEDGER",
        "ledger delivery",
        commits="self",
        owned_files=["docs/FORK_CHANGES.md"],
    )
    _write_ledger(repo, body)
    _git(repo, "add", "docs/FORK_CHANGES.md")
    _git(repo, "commit", "-m", "docs: add ledger")
    ledger = repo / "docs" / "FORK_CHANGES.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            "- Intent: Keep the fork feature working.",
            "- Intent: Change a different entry only.",
            1,
        ),
        encoding="utf-8",
    )
    unrelated_edit = _commit_existing(
        repo, "docs/FORK_CHANGES.md", subject="docs: edit feature entry"
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert unrelated_edit in {item["sha"] for item in payload["unmapped_commits"]}


def test_self_cannot_claim_precedence_losing_ledger_path(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    body = (
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        )
        + _entry(
            "G-FORK-LEDGER",
            "ledger delivery",
            commits="self",
            owned_files=["docs/FORK_CHANGES.md"],
        )
        + _entry(
            "G-OTHER",
            "effective ledger owner",
            commits="none",
            owned_files=["docs/FORK_CHANGES.md"],
        )
    )
    body += "\n## Path-Precedence\n- docs/FORK_CHANGES.md: G-OTHER\n"
    _write_ledger(repo, body)
    self_commit = _commit_existing(
        repo, "docs/FORK_CHANGES.md", subject="docs: add losing self entry"
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert self_commit in {item["sha"] for item in payload["unmapped_commits"]}


def test_historical_shared_path_claim_obeys_declared_precedence(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    (repo / "fork_feature.py").unlink()
    deletion = _commit_existing(
        repo, "fork_feature.py", subject="fix(fork): delete fork feature"
    )
    body = _entry(
        "G-FEATURE",
        "original fork feature",
        commits=f"{fx['fork_a']}, {fx['fork_b']}",
        owned_files=["fork_feature.py"],
    ) + _entry(
        "G-OTHER",
        "feature removal owner",
        commits=deletion,
        owned_files=["fork_feature.py"],
    )
    body += "\n## Path-Precedence\n- fork_feature.py: G-OTHER\n"
    _write_ledger(repo, body)

    code, payload = _run_checker(repo)

    assert code == 1
    assert "fork_feature.py" not in payload["path_owners"]
    problems = next(
        item["problems"]
        for item in payload["invalid_entries"]
        if item["id"] == "G-FEATURE"
    )
    assert any("loses effective path ownership" in problem for problem in problems)


def test_rename_path_parser_keeps_source_and_destination(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _git(repo, "mv", "core.py", "renamed_core.py")
    _git(repo, "commit", "-m", "refactor: rename upstream path")
    mod = _load_checker_module()
    paths = mod.fork_changed_paths(repo, "upstream-main", "fork-main")
    assert "core.py" in paths
    assert "renamed_core.py" in paths


def test_name_status_parser_uses_reversible_byte_decoding() -> None:
    mod = _load_checker_module()
    raw = b"M\0bad-\xff-name\0"
    assert mod._parse_name_status_z(raw) == ["bad-\udcff-name"]


def test_owned_path_json_escape_round_trips_surrogate_bytes() -> None:
    mod = _load_checker_module()
    assert mod._decode_owned_path('"bad-\\udcff-name"') == "bad-\udcff-name"


def test_precedence_path_json_escape_round_trips_surrogate_bytes() -> None:
    mod = _load_checker_module()
    precedence, problems = mod.parse_path_precedence(
        '## Path-Precedence\n- "bad-\\udcff-name": G-FEATURE\n'
    )
    assert problems == []
    assert precedence == {"bad-\udcff-name": "G-FEATURE"}


@pytest.mark.parametrize(
    "leading",
    [chr(value) for value in range(32)] + ["\x7f", " "],
    ids=[f"ascii-{value}" for value in range(32)] + ["ascii-127", "space"],
)
def test_precedence_plain_path_rejects_extra_leading_control_or_space(
    leading: str,
) -> None:
    mod = _load_checker_module()
    precedence, problems = mod.parse_path_precedence(
        f"## Path-Precedence\n- {leading}victim.py: G-FEATURE\n"
    )

    assert precedence == {}
    assert problems


def test_owned_path_edge_whitespace_requires_json_encoding() -> None:
    mod = _load_checker_module()
    with pytest.raises(ValueError, match="JSON-string syntax"):
        mod._decode_owned_path(" leading-space")


@pytest.mark.parametrize("path", ["bad\tname", "bad\x01name"])
def test_owned_path_plain_control_character_requires_json_encoding(path: str) -> None:
    mod = _load_checker_module()
    with pytest.raises(ValueError, match="JSON-string syntax"):
        mod._decode_owned_path(path)


def test_fatal_merge_base_error_is_checker_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    mod = _load_checker_module()
    real_run = mod.subprocess.run

    def _run(command, *args, **kwargs):
        if "merge-base" in command:
            return subprocess.CompletedProcess(command, 128, b"", b"fatal")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", _run)
    with pytest.raises(mod.CheckerError):
        mod.classify_range(repo, "upstream-main", "fork-main")
