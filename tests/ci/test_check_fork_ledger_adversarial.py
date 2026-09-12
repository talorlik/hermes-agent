"""Adversarial contracts for the fork-ledger verification boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from tests.ci.test_check_fork_ledger import (
    _GIT_ENV,
    _commit_existing,
    _commit_file,
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


def _valid_fixture_ledger(fx: dict[str, object]) -> str:
    return _entry(
        "G-FEATURE",
        "fork feature",
        commits="{}, {}".format(fx["fork_a"], fx["fork_b"]),
        owned_files=["fork_feature.py"],
    ) + _entry(
        "G-FORK-LEDGER",
        "ledger delivery",
        commits="self",
        owned_files=["docs/FORK_CHANGES.md"],
    )


def _annotated_tag_oid(repo: Path, target: object, name: str) -> str:
    _git(repo, "tag", "-a", "-m", f"tag {name}", name, str(target))
    return _git(repo, "rev-parse", f"{name}^{{tag}}")


def test_replacement_object_cannot_forge_pinned_ledger_tree(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(repo, _valid_fixture_ledger(fx))
    original_tip = _commit_existing(
        repo, "docs/FORK_CHANGES.md", subject="docs: add valid ledger"
    )
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 0, payload

    _write_ledger(repo, "## forged ledger\n")
    _git(repo, "add", "docs/FORK_CHANGES.md")
    forged_tree = _git(repo, "write-tree")
    forged_tip = subprocess.run(
        [
            "git",
            "commit-tree",
            forged_tree,
            "-p",
            _git(repo, "rev-parse", f"{original_tip}^"),
        ],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
        input="forged replacement ledger\n",
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(repo, "replace", original_tip, forged_tip)

    code, payload = _run_checker(repo, materialize_ledger=False)

    assert code == 0, payload
    assert payload["fork_oid"] == original_tip


def test_annotated_tag_object_oid_is_not_a_commit_claim(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    tag_oid = _annotated_tag_oid(repo, fx["fork_a"], "claim-tag")
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits="{}, {}".format(tag_oid, fx["fork_b"]),
            owned_files=["fork_feature.py"],
        ),
    )

    code, payload = _run_checker(repo)

    assert code == 1
    assert any(
        "canonical commit object" in problem
        for item in payload["invalid_entries"]
        for problem in item["problems"]
    )


def test_annotated_tag_object_oid_is_not_a_range_endpoint(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    tag_oid = _annotated_tag_oid(repo, fx["fork_a"], "range-tag")
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits="{}, {}..{}".format(fx["fork_a"], tag_oid, fx["fork_b"]),
            owned_files=["fork_feature.py"],
        ),
    )

    code, payload = _run_checker(repo)

    assert code == 1
    assert any(
        "canonical commit object" in problem
        for item in payload["invalid_entries"]
        for problem in item["problems"]
    )


def test_duplicate_canonical_commit_token_is_invalid(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits="{}, {}, {}".format(fx["fork_a"], fx["fork_a"], fx["fork_b"]),
            owned_files=["fork_feature.py"],
        ),
    )

    code, payload = _run_checker(repo)

    assert code == 1
    assert any(
        "duplicate commit declaration" in problem
        for item in payload["invalid_entries"]
        for problem in item["problems"]
    )


def _history_ledger_entry(history: str, revision: str | None = "1") -> str:
    entry = _entry(
        "G-FORK-LEDGER",
        "ledger delivery",
        commits="self",
        owned_files=["docs/FORK_CHANGES.md"],
    )
    revision_line = f"- Ledger-Revision: {revision}\n" if revision is not None else ""
    return entry.replace(
        "- Commits: self\n",
        f"- Commits: self\n{revision_line}- History-Reconciliations: {history}\n",
    )


def _make_reconciliation_repo(
    tmp_path: Path,
    *,
    tree_preserving: bool = True,
    unrelated: bool = False,
    side_branch: bool = False,
    initial_revision: str | None = "1",
) -> dict[str, object]:
    repo = tmp_path / "reconciliation-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    base = _commit_file(repo, "core.py", "BASE = 1\n", "upstream base")
    if unrelated:
        _git(repo, "checkout", "--orphan", "retired-main")
        _git(repo, "rm", "-rf", ".")
    else:
        _git(repo, "checkout", "-b", "retired-main")
    retired = _commit_file(repo, "retired.py", "RETIRED = 1\n", "retired work")
    _git(repo, "checkout", "upstream-main")
    _git(repo, "checkout", "-b", "fork-main")
    active = _commit_file(repo, "active.py", "ACTIVE = 1\n", "active work")
    if side_branch:
        _git(repo, "checkout", "-b", "reconciliation-side")
    merge = ["merge", "--no-ff", "-m", "history reconciliation"]
    if tree_preserving:
        merge.extend(["-s", "ours"])
    if unrelated:
        merge.append("--allow-unrelated-histories")
    merge.append("retired-main")
    _git(repo, *merge)
    reconciliation = _git(repo, "rev-parse", "HEAD")
    if side_branch:
        _git(repo, "checkout", "fork-main")
        _git(
            repo,
            "merge",
            "--no-ff",
            "-s",
            "ours",
            "-m",
            "merge reconciliation side branch",
            "reconciliation-side",
        )
    _write_ledger(
        repo,
        _entry(
            "G-ACTIVE",
            "active fork work",
            commits=active,
            owned_files=["active.py"],
        )
        + _history_ledger_entry(reconciliation, initial_revision),
    )
    authorization = _commit_existing(
        repo, "docs/FORK_CHANGES.md", subject="authorize reconciliation"
    )
    return {
        "repo": repo,
        "base": base,
        "active": active,
        "retired": retired,
        "reconciliation": reconciliation,
        "authorization": authorization,
    }


def _replace_commit_tree(repo: Path, commit: object, tree: str) -> str:
    parents = _git(repo, "show", "-s", "--format=%P", str(commit)).split()
    command = ["git", "commit-tree", tree]
    for parent in parents:
        command.extend(["-p", parent])
    replacement = subprocess.run(
        command,
        cwd=repo,
        env=_GIT_ENV,
        check=True,
        input="replacement commit\n",
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(repo, "replace", str(commit), replacement)
    return replacement


def _ledger_problems(payload: dict) -> list[str]:
    return next(
        item["problems"]
        for item in payload["invalid_entries"]
        if item["id"] == "G-FORK-LEDGER"
    )


def test_shared_root_reconciliation_has_exact_disjoint_partition(
    tmp_path: Path,
) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 0, payload
    assert payload["counts"]["fork_only_commits"] == 4
    assert payload["counts"]["work_commits"] == 2
    assert payload["counts"]["sync_merges"] == 0
    assert payload["counts"]["history_reconciliations"] == 1
    assert payload["counts"]["retired_history_commits"] == 1
    assert payload["partition"]["work"] == sorted([fx["active"], fx["authorization"]])
    assert payload["partition"]["sync"] == []
    assert payload["partition"]["history"] == sorted([
        fx["reconciliation"],
        fx["retired"],
    ])


def test_reconciliation_rejects_unrelated_retired_root(tmp_path: Path) -> None:
    fx = _make_reconciliation_repo(tmp_path, unrelated=True)
    repo = cast(Path, fx["repo"])
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any(
        "official upstream root" in problem for problem in _ledger_problems(payload)
    )


def test_reconciliation_must_be_on_fork_first_parent_chain(tmp_path: Path) -> None:
    fx = _make_reconciliation_repo(tmp_path, side_branch=True)
    repo = cast(Path, fx["repo"])
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any("first-parent chain" in problem for problem in _ledger_problems(payload))


def test_reconciliation_must_preserve_raw_first_parent_tree(tmp_path: Path) -> None:
    fx = _make_reconciliation_repo(tmp_path, tree_preserving=False)
    repo = cast(Path, fx["repo"])
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any(
        "raw first-parent tree" in problem for problem in _ledger_problems(payload)
    )


def test_annotated_tag_object_oid_is_not_a_reconciliation(tmp_path: Path) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    tag_oid = _annotated_tag_oid(repo, fx["reconciliation"], "history-tag")
    ledger = repo / "docs" / "FORK_CHANGES.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(str(fx["reconciliation"]), tag_oid),
        encoding="utf-8",
    )
    _git(repo, "commit", "-am", "use tag object as reconciliation")
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any(
        "canonical commit object" in problem for problem in _ledger_problems(payload)
    )


def test_duplicate_reconciliation_declaration_is_invalid(tmp_path: Path) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    ledger = repo / "docs" / "FORK_CHANGES.md"
    token = str(fx["reconciliation"])
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            f"- History-Reconciliations: {token}",
            f"- History-Reconciliations: {token}, {token}",
        ),
        encoding="utf-8",
    )
    _git(repo, "commit", "-am", "duplicate reconciliation")
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any(
        "duplicate History-Reconciliations" in problem
        for problem in _ledger_problems(payload)
    )


def test_replacement_cannot_forge_tree_preserving_reconciliation(
    tmp_path: Path,
) -> None:
    fx = _make_reconciliation_repo(tmp_path, tree_preserving=False)
    repo = cast(Path, fx["repo"])
    tree_spec = "{}^1^{{tree}}".format(fx["reconciliation"])
    first_parent_tree = _git(repo, "rev-parse", tree_spec)
    _replace_commit_tree(repo, fx["reconciliation"], first_parent_tree)
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any(
        "raw first-parent tree" in problem for problem in _ledger_problems(payload)
    )


def test_replacement_cannot_hide_valid_raw_tree_equality(tmp_path: Path) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    tree_spec = "{}^{{tree}}".format(fx["retired"])
    retired_tree = _git(repo, "rev-parse", tree_spec)
    _replace_commit_tree(repo, fx["reconciliation"], retired_tree)
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 0, payload


def _add_second_reconciliation(
    fx: dict[str, object],
    revision: str | None,
    *,
    mixed_path: bool = False,
) -> str:
    repo = cast(Path, fx["repo"])
    _git(repo, "checkout", "-b", "retired-two", str(fx["base"]))
    _commit_file(repo, "retired-two.py", "RETIRED_TWO = 1\n", "second retired work")
    _git(repo, "checkout", "fork-main")
    _git(
        repo,
        "merge",
        "--no-ff",
        "-s",
        "ours",
        "-m",
        "second reconciliation",
        "retired-two",
    )
    second = _git(repo, "rev-parse", "HEAD")
    ledger = repo / "docs" / "FORK_CHANGES.md"
    text = ledger.read_text(encoding="utf-8")
    text = text.replace(
        f"- History-Reconciliations: {fx['reconciliation']}",
        "- History-Reconciliations: {}, {}".format(fx["reconciliation"], second),
    )
    old_revision = next(
        line for line in text.splitlines() if line.startswith("- Ledger-Revision:")
    )
    replacement = f"- Ledger-Revision: {revision}" if revision is not None else ""
    text = text.replace(old_revision, replacement)
    ledger.write_text(text, encoding="utf-8")
    paths = ["docs/FORK_CHANGES.md"]
    if mixed_path:
        (repo / "unowned.py").write_text("UNOWNED = 1\n", encoding="utf-8")
        paths.append("unowned.py")
    _commit_existing(repo, *paths, subject="authorize second reconciliation")
    return second


@pytest.mark.parametrize(("changed_revision", "expected_code"), [("7", 1), ("8", 0)])
def test_initial_history_field_increments_existing_ledger_revision(
    tmp_path: Path, changed_revision: str, expected_code: int
) -> None:
    repo = tmp_path / "existing-ledger-revision"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    base = _commit_file(repo, "core.py", "BASE = 1\n", "upstream base")
    _git(repo, "checkout", "-b", "fork-main")
    _write_ledger(
        repo,
        _entry(
            "G-FORK-LEDGER",
            "ledger delivery",
            commits="self",
            owned_files=["docs/FORK_CHANGES.md"],
        ).replace("- Commits: self\n", "- Commits: self\n- Ledger-Revision: 7\n"),
    )
    _commit_existing(repo, "docs/FORK_CHANGES.md", subject="baseline ledger")
    _git(repo, "checkout", "-b", "retired-main", base)
    _commit_file(repo, "retired.py", "RETIRED = 1\n", "retired work")
    _git(repo, "checkout", "fork-main")
    _git(
        repo,
        "merge",
        "--no-ff",
        "-s",
        "ours",
        "-m",
        "history reconciliation",
        "retired-main",
    )
    reconciliation = _git(repo, "rev-parse", "HEAD")
    ledger = repo / "docs" / "FORK_CHANGES.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            "- Ledger-Revision: 7",
            f"- Ledger-Revision: {changed_revision}\n"
            f"- History-Reconciliations: {reconciliation}",
        ),
        encoding="utf-8",
    )
    _git(repo, "commit", "-am", "authorize retired history")

    code, payload = _run_checker(repo, materialize_ledger=False)

    assert code == expected_code, payload
    if expected_code:
        assert any(
            "activation must exceed the persistent Ledger-Revision floor" in problem
            for problem in _ledger_problems(payload)
        )


@pytest.mark.parametrize(
    "revision",
    [None, "0", "-1", "malformed"],
    ids=["missing", "zero", "negative", "malformed"],
)
def test_initial_reconciliation_revision_must_be_positive_decimal(
    tmp_path: Path, revision: str | None
) -> None:
    fx = _make_reconciliation_repo(tmp_path, initial_revision=revision)
    repo = cast(Path, fx["repo"])
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any(
        "positive decimal Ledger-Revision" in problem
        for problem in _ledger_problems(payload)
    )


def test_reconciliation_change_strictly_increments_revision(tmp_path: Path) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _add_second_reconciliation(fx, "2")
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 0, payload
    assert payload["counts"]["history_reconciliations"] == 2
    assert payload["counts"]["retired_history_commits"] == 2


@pytest.mark.parametrize(
    ("changed_revision", "has_problem"), [("1", True), ("2", False)]
)
def test_history_field_removal_requires_revision_increment(
    tmp_path: Path, changed_revision: str, has_problem: bool
) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    ledger = repo / "docs" / "FORK_CHANGES.md"
    lines = [
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if not line.startswith("- History-Reconciliations:")
    ]
    ledger.write_text(
        ("\n".join(lines) + "\n").replace(
            "- Ledger-Revision: 1", f"- Ledger-Revision: {changed_revision}"
        ),
        encoding="utf-8",
    )
    _git(repo, "commit", "-am", "remove retired history authorization")

    _code, payload = _run_checker(repo, materialize_ledger=False)
    problems = [
        problem
        for item in payload["invalid_entries"]
        if item["id"] == "G-FORK-LEDGER"
        for problem in item["problems"]
    ]
    assert (
        any(
            "strictly increase the persistent Ledger-Revision floor" in p
            for p in problems
        )
        is has_problem
    )


def test_entry_deletion_cannot_reset_history_revision_floor(tmp_path: Path) -> None:
    repo = tmp_path / "revision-laundering"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    base = _commit_file(repo, "core.py", "BASE = 1\n", "upstream base")
    _git(repo, "checkout", "-b", "fork-main")
    baseline = _entry(
        "G-FORK-LEDGER",
        "ledger",
        commits="self",
        owned_files=["docs/FORK_CHANGES.md"],
    ).replace("- Commits: self\n", "- Commits: self\n- Ledger-Revision: 7\n")
    _write_ledger(repo, baseline)
    _commit_existing(repo, "docs/FORK_CHANGES.md", subject="baseline ledger")

    _write_ledger(
        repo,
        _entry(
            "G-TEMP",
            "temporary owner",
            commits="none",
            owned_files=["docs/FORK_CHANGES.md"],
        ),
    )
    removal = _commit_existing(
        repo, "docs/FORK_CHANGES.md", subject="temporarily remove ledger entry"
    )
    _git(repo, "checkout", "-b", "retired-main", base)
    _commit_file(repo, "retired.py", "RETIRED = 1\n", "retired work")
    _git(repo, "checkout", "fork-main")
    _git(
        repo,
        "merge",
        "--no-ff",
        "-s",
        "ours",
        "-m",
        "history reconciliation",
        "retired-main",
    )
    reconciliation = _git(repo, "rev-parse", "HEAD")
    restored = _entry(
        "G-FORK-LEDGER",
        "ledger",
        commits=f"self, {removal}",
        owned_files=["docs/FORK_CHANGES.md"],
    ).replace(
        f"- Commits: self, {removal}\n",
        f"- Commits: self, {removal}\n"
        "- Ledger-Revision: 7\n"
        f"- History-Reconciliations: {reconciliation}\n",
    )
    _write_ledger(repo, restored)
    _commit_existing(repo, "docs/FORK_CHANGES.md", subject="restore ledger")

    code, payload = _run_checker(repo, materialize_ledger=False)

    assert code == 1
    problems = _ledger_problems(payload)
    assert any("Ledger-Revision floor" in problem for problem in problems)
    assert any("missing G-FORK-LEDGER" in problem for problem in problems)


def test_g_fork_ledger_cannot_disappear_after_history_activation(
    tmp_path: Path,
) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(
        repo,
        _entry(
            "G-TEMP",
            "temporary owner",
            commits="self",
            owned_files=["docs/FORK_CHANGES.md"],
        ),
    )
    _commit_existing(repo, "docs/FORK_CHANGES.md", subject="remove activated ledger")

    code, payload = _run_checker(repo, materialize_ledger=False)

    assert code == 1
    problems = [
        problem
        for item in payload["invalid_entries"]
        if item["id"] == "G-FORK-LEDGER"
        for problem in item["problems"]
    ]
    assert any("must not disappear after history activation" in p for p in problems)


def test_run_check_preserves_rev_list_order_for_legacy_output(tmp_path: Path) -> None:
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    mod = _load_checker_module()
    for index in range(20):
        _commit_file(
            repo, f"unmapped-{index}.py", f"VALUE = {index}\n", f"work {index}"
        )
        ordered, _sync = mod.classify_range(repo, "upstream-main", "fork-main")
        ordered_shas = [item["sha"] for item in ordered]
        if ordered_shas != sorted(ordered_shas):
            break
    else:
        raise AssertionError("could not construct a non-lexicographic rev-list order")
    _write_ledger(
        repo,
        _entry(
            "G-FORK-LEDGER",
            "ledger",
            commits="self",
            owned_files=["docs/FORK_CHANGES.md"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    legacy_work, legacy_sync = mod.classify_range(repo, "upstream-main", "fork-main")
    actual_unmapped = [item["sha"] for item in payload["unmapped_commits"]]
    unmapped_set = set(actual_unmapped)
    assert actual_unmapped == [
        item["sha"] for item in legacy_work if item["sha"] in unmapped_set
    ]
    assert payload["sync_merges"] == legacy_sync


@pytest.mark.parametrize(
    ("initial", "changed"),
    [("1", "1"), ("2", "1"), ("1", "bad"), ("1", None)],
    ids=["unchanged", "decreased", "malformed", "missing"],
)
def test_reconciliation_change_rejects_invalid_revision_transition(
    tmp_path: Path, initial: str, changed: str | None
) -> None:
    fx = _make_reconciliation_repo(tmp_path, initial_revision=initial)
    repo = cast(Path, fx["repo"])
    _add_second_reconciliation(fx, changed)
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any(
        "persistent Ledger-Revision floor" in problem
        or "positive decimal Ledger-Revision" in problem
        for problem in _ledger_problems(payload)
    )


def test_history_authorization_requires_effective_ledger_ownership(
    tmp_path: Path,
) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    ledger = repo / "docs" / "FORK_CHANGES.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + _entry(
            "G-OTHER",
            "competing ledger owner",
            commits="self",
            owned_files=["docs/FORK_CHANGES.md"],
        )
        + "## Path-Precedence\n\n"
        + "- `docs/FORK_CHANGES.md`: G-OTHER\n",
        encoding="utf-8",
    )
    _git(repo, "commit", "-am", "move effective ledger ownership")

    code, payload = _run_checker(repo, materialize_ledger=False)

    assert code == 1
    assert any(
        "effective G-FORK-LEDGER ownership" in problem
        for problem in _ledger_problems(payload)
    )


def test_reconciliation_change_must_be_self_owned(tmp_path: Path) -> None:
    fx = _make_reconciliation_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _add_second_reconciliation(fx, "2", mixed_path=True)
    code, payload = _run_checker(repo, materialize_ledger=False)
    assert code == 1
    assert any("self-owned commit" in problem for problem in _ledger_problems(payload))
