"""Contract tests for scripts/ci/check_fork_ledger.py.

The checker is the deterministic post_verify gate behind the fork ledger
(ARD-010): every fork-only commit must map to a ledger entry, and every entry
must carry the full set of required fields. Tests build throwaway git repos in
tmp_path (no network, explicit refs) and drive the script as a subprocess so
the foreign-checkout contract is exercised for real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_fork_ledger.py"

# Deterministic, hermetic git: no user/system config (a global core.hooksPath
# breaks fixture repos), fixed identity and dates.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _commit_file(repo: Path, rel: str, content: str, subject: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _make_fork_repo(tmp_path: Path) -> dict[str, object]:
    """One repo, two branches: upstream-main and fork-main diverge, then the
    fork merges upstream back in (a sync merge, like fork_sync_strategy=merge).

    Returns the repo path plus the SHAs of the two fork-only work commits and
    the sync merge.
    """
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "core.py", "BASE = 1\n", "chore: upstream base")
    _git(repo, "checkout", "-b", "fork-main")
    fork_a = _commit_file(
        repo, "fork_feature.py", "FORK = 1\n", "feat(fork): add fork feature"
    )
    fork_b = _commit_file(
        repo, "fork_feature.py", "FORK = 2\n", "fix(fork): harden fork feature"
    )
    # Upstream advances independently...
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "core.py", "BASE = 2\n", "feat: upstream advance")
    # ...and the fork syncs it back in with a merge commit.
    _git(repo, "checkout", "fork-main")
    _git(
        repo,
        "merge",
        "--no-ff",
        "-m",
        "chore: merge upstream into fork",
        "upstream-main",
    )
    sync_merge = _git(repo, "rev-parse", "HEAD")
    return {
        "repo": repo,
        "fork_a": fork_a,
        "fork_b": fork_b,
        "sync_merge": sync_merge,
    }


def _entry(
    entry_id: str,
    title: str,
    *,
    commits: str,
    owned_files: list[str],
    intent: str = "Keep the fork feature working.",
    invariant: str = "Fork feature stays enabled.",
    tests: str = "tests/test_fork_feature.py",
    retirement: str = "Upstream ships the feature.",
    disposition: str = "active",
) -> str:
    lines = [f"## {entry_id}: {title}", f"- Commits: {commits}"]
    lines.append("- Owned-Files:")
    lines.extend(f"  - {f}" for f in owned_files)
    lines.extend([
        f"- Intent: {intent}",
        f"- Protected-Invariant: {invariant}",
        f"- Tests: {tests}",
        f"- Retirement-Condition: {retirement}",
        f"- Disposition: {disposition}",
        "",
    ])
    return "\n".join(lines)


def _write_ledger(repo: Path, body: str) -> None:
    if "docs/FORK_CHANGES.md" not in body:
        body = (
            body.rstrip()
            + "\n\n"
            + _entry(
                "G-TEST-LEDGER",
                "fixture ledger path owner",
                commits="self",
                owned_files=["docs/FORK_CHANGES.md"],
            )
        )
    path = repo / "docs" / "FORK_CHANGES.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Fork Change Ledger\n\n" + body, encoding="utf-8")


def _fork_ref_with_worktree_ledger(repo: Path) -> str:
    """Return an unreferenced replacement sync merge containing the test ledger."""
    ledger = repo / "docs" / "FORK_CHANGES.md"
    if not ledger.is_file():
        return "fork-main"
    indexed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "docs/FORK_CHANGES.md"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
    )
    if indexed.returncode not in {0, 1}:
        raise RuntimeError("could not inspect fixture ledger index state")
    if indexed.returncode == 0:
        tracked = subprocess.run(
            ["git", "diff", "--quiet", "fork-main", "--", "docs/FORK_CHANGES.md"],
            cwd=repo,
            env=_GIT_ENV,
        )
        if tracked.returncode == 0:
            return "fork-main"
        if tracked.returncode != 1:
            raise RuntimeError("could not compare fixture ledger with fork-main")
    index = repo / ".git" / "fork-ledger-test.index"
    env = {**_GIT_ENV, "GIT_INDEX_FILE": str(index)}
    try:
        subprocess.run(
            ["git", "read-tree", "fork-main"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "add", "--", "docs/FORK_CHANGES.md"],
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
        parents = [_git(repo, "rev-parse", "fork-main")]
        command = ["git", "commit-tree", tree]
        for parent in parents:
            command.extend(["-p", parent])
        return subprocess.run(
            command,
            cwd=repo,
            env=env,
            check=True,
            input="test: materialize fixture ledger\n",
            capture_output=True,
            text=True,
        ).stdout.strip()
    finally:
        index.unlink(missing_ok=True)


def _run_checker(
    repo: Path,
    *extra: str,
    cwd: Path | None = None,
    materialize_ledger: bool = True,
) -> tuple[int, dict]:
    fork_ref = (
        _fork_ref_with_worktree_ledger(repo) if materialize_ledger else "fork-main"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--upstream-ref",
            "upstream-main",
            "--fork-ref",
            fork_ref,
            *extra,
        ],
        cwd=cwd or repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc.returncode, payload


def test_unmapped_fork_commit_fails_and_is_listed(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=str(fx["fork_a"]),
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    unmapped = {c["sha"] for c in payload["unmapped_commits"]}
    assert fx["fork_b"] in unmapped
    assert payload["counts"]["unmapped"] == 1
    assert payload["ok"] is False


def test_fully_mapped_ledger_passes_with_zero_unmapped(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 0
    assert payload["ok"] is True
    assert payload["counts"]["unmapped"] == 0
    assert payload["counts"]["mapped"] == 3


def test_checker_reads_ledger_from_fork_ref_not_worktree(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    body = _entry(
        "G-FEATURE",
        "fork feature",
        commits=f"{fx['fork_a']}, {fx['fork_b']}",
        owned_files=["fork_feature.py"],
    ) + _entry(
        "G-LEDGER",
        "ledger",
        commits="self",
        owned_files=["docs/FORK_CHANGES.md"],
    )
    _write_ledger(repo, body)
    _git(repo, "add", "docs/FORK_CHANGES.md")
    _git(repo, "commit", "-m", "docs: add ledger")

    # The target ref is valid, but the mutable working copy is forged. A
    # ref-bound checker must ignore these bytes and still approve fork-main.
    _write_ledger(repo, "## forged working tree\n")
    code, payload = _run_checker(repo, materialize_ledger=False)

    assert code == 0
    assert payload["ok"] is True
    assert payload["counts"]["mapped"] == 3


def test_report_exposes_pinned_ref_oids(tmp_path):
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

    code, payload = _run_checker(repo)

    assert code == 0
    assert payload["upstream_oid"] == _git(repo, "rev-parse", "upstream-main")
    assert len(payload["fork_oid"]) == 40
    assert payload["fork_oid"] != "fork-main"


def test_external_ledger_path_is_rejected(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    body = _entry(
        "G-FEATURE",
        "fork feature",
        commits=f"{fx['fork_a']}, {fx['fork_b']}",
        owned_files=["fork_feature.py"],
    )
    _write_ledger(repo, body)
    external = tmp_path / "external-ledger.md"
    external.write_text("# Fork Change Ledger\n\n" + body, encoding="utf-8")

    code, payload = _run_checker(repo, "--ledger", str(external))

    assert code == 2
    assert payload["ok"] is False
    assert "inside repository" in payload["error"]


def test_explicit_claim_outside_entry_owned_files_fails(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    _write_ledger(
        repo,
        _entry(
            "G-CLAIM",
            "mis-scoped claim",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["core.py"],
        )
        + _entry(
            "G-OWNER",
            "actual path owner",
            commits="none",
            owned_files=["fork_feature.py"],
        ),
    )

    code, payload = _run_checker(repo)

    assert code == 1
    problems = next(
        item["problems"]
        for item in payload["invalid_entries"]
        if item["id"] == "G-CLAIM"
    )
    assert any("outside Owned-Files" in problem for problem in problems)


def test_explicit_claim_outside_evaluated_work_range_fails(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = cast(Path, fx["repo"])
    upstream_only = _git(repo, "rev-parse", "upstream-main")
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}, {upstream_only}",
            owned_files=["fork_feature.py", "core.py"],
        ),
    )

    code, payload = _run_checker(repo)

    assert code == 1
    problems = payload["invalid_entries"][0]["problems"]
    assert any("outside evaluated work range" in problem for problem in problems)


def test_missing_required_field_fails_with_named_field(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    entry = _entry(
        "G-FEATURE",
        "fork feature",
        commits=f"{fx['fork_a']}, {fx['fork_b']}",
        owned_files=["fork_feature.py"],
    )
    # Drop the Protected-Invariant line entirely.
    entry = "\n".join(
        line
        for line in entry.splitlines()
        if not line.startswith("- Protected-Invariant:")
    )
    _write_ledger(repo, entry)
    code, payload = _run_checker(repo)
    assert code == 1
    assert payload["ok"] is False
    invalid = payload["invalid_entries"]
    assert len(invalid) == 1
    assert invalid[0]["id"] == "G-FEATURE"
    assert any("Protected-Invariant" in p for p in invalid[0]["problems"])


def test_sync_merge_commits_are_exempt_from_mapping(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 0
    assert fx["sync_merge"] in payload["sync_merges"]
    assert payload["counts"]["sync_merges"] == 1


def _claimed_shas(payload: dict) -> set[str]:
    return {item["sha"] for item in payload.get("duplicate_claims", [])}


def test_precedence_loser_cannot_claim_shared_path_commit(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        )
        + _entry(
            "G-OTHER",
            "also claims first commit",
            commits=str(fx["fork_a"]),
            owned_files=["fork_feature.py"],
        )
        + _precedence_block({"fork_feature.py": "G-FEATURE"}),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert payload["duplicate_claims"] == []
    problems = next(
        item["problems"]
        for item in payload["invalid_entries"]
        if item["id"] == "G-OTHER"
    )
    assert any("loses effective path ownership" in problem for problem in problems)


def test_duplicate_entry_ids_claiming_same_commit_exit_1(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        )
        + _entry(
            "G-FEATURE",
            "same id claims first commit again",
            commits=str(fx["fork_a"]),
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert fx["fork_a"] in _claimed_shas(payload)
    matching = [
        item for item in payload["duplicate_claims"] if item["sha"] == fx["fork_a"]
    ]
    assert matching
    assert "G-FEATURE" in matching[0]["entries"]


def _precedence_block(mapping: dict[str, str]) -> str:
    lines = ["## Path-Precedence", ""]
    for path, entry_id in mapping.items():
        lines.append(f"- {path}: {entry_id}")
    lines.append("")
    return "\n".join(lines)


def _commit_existing(repo: Path, *rels: str, subject: str) -> str:
    _git(repo, "add", "--", *rels)
    _git(repo, "commit", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def test_self_maps_delivery_after_sha_rewrite_not_unrelated(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    unrelated = _commit_file(
        repo, "unrelated.py", "U = 1\n", "feat(fork): unrelated work"
    )
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        )
        + _entry(
            "G-OTHER",
            "owns unrelated path but claims no commit",
            commits="none",
            owned_files=["unrelated.py"],
        )
        + _entry(
            "G-FORK-LEDGER",
            "ledger delivery",
            commits="self",
            owned_files=["docs/FORK_CHANGES.md"],
        ),
    )
    old = _commit_existing(
        repo, "docs/FORK_CHANGES.md", subject="docs(fork): add ledger"
    )
    assert old not in (repo / "docs" / "FORK_CHANGES.md").read_text(encoding="utf-8")

    code, payload = _run_checker(repo)
    assert code == 1
    unmapped = {c["sha"] for c in payload["unmapped_commits"]}
    assert unmapped == {unrelated}
    assert old not in unmapped

    _git(repo, "commit", "--amend", "-m", "docs(fork): add ledger rewritten")
    new = _git(repo, "rev-parse", "HEAD")
    assert new != old
    assert new not in (repo / "docs" / "FORK_CHANGES.md").read_text(encoding="utf-8")

    code, payload = _run_checker(repo)
    assert code == 1
    unmapped = {c["sha"] for c in payload["unmapped_commits"]}
    assert unmapped == {unrelated}
    assert new not in unmapped
    assert payload["counts"]["unmapped"] == 1


def test_self_does_not_map_commit_that_also_changes_unowned_files(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
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
        ),
    )
    (repo / "unrelated.py").write_text("U = 1\n", encoding="utf-8")
    mixed = _commit_existing(
        repo,
        "docs/FORK_CHANGES.md",
        "unrelated.py",
        subject="docs(fork): ledger plus unrelated file",
    )
    code, payload = _run_checker(repo)
    assert code == 1
    unmapped = {c["sha"] for c in payload["unmapped_commits"]}
    assert mixed in unmapped


def test_missing_current_path_ownership_fails_closed(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _commit_file(repo, "extra.py", "E = 1\n", "feat(fork): extra path")
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits="none",
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert "extra.py" in payload.get("unowned_paths", [])


def test_ambiguous_current_path_ownership_fails_without_precedence(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        )
        + _entry(
            "G-OTHER",
            "second owner",
            commits="none",
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    ambiguous = {
        item["path"]: item["entries"] for item in payload.get("ambiguous_paths", [])
    }
    assert "fork_feature.py" in ambiguous
    assert set(ambiguous["fork_feature.py"]) == {"G-FEATURE", "G-OTHER"}


def test_explicit_path_precedence_resolves_one_owner(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        )
        + _entry(
            "G-OTHER",
            "second owner",
            commits="none",
            owned_files=["fork_feature.py"],
        )
        + _precedence_block({"fork_feature.py": "G-FEATURE"}),
    )
    code, payload = _run_checker(repo)
    assert code == 0, payload
    assert payload["ambiguous_paths"] == []
    assert payload["path_owners"]["fork_feature.py"] == "G-FEATURE"


def _precedence_rows(*rows: tuple[str, str]) -> str:
    lines = ["## Path-Precedence", ""]
    for path, entry_id in rows:
        lines.append(f"- {path}: {entry_id}")
    lines.append("")
    return "\n".join(lines)


def _multi_owned_feature_ledger(fx: dict[str, object], *rows: tuple[str, str]) -> str:
    return (
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        )
        + _entry(
            "G-OTHER",
            "second owner",
            commits="none",
            owned_files=["fork_feature.py"],
        )
        + _precedence_rows(*rows)
    )


def _precedence_problems(payload: dict, path: str) -> list[str]:
    return [
        item["problem"]
        for item in payload.get("invalid_precedence", [])
        if item.get("path") == path and item.get("problem")
    ]


def test_duplicate_same_precedence_rows_exit_1(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _multi_owned_feature_ledger(
            fx,
            ("fork_feature.py", "G-FEATURE"),
            ("fork_feature.py", "G-FEATURE"),
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert payload["ok"] is False
    problems = _precedence_problems(payload, "fork_feature.py")
    assert problems
    assert "duplicate" in problems[0].lower()
    assert "G-FEATURE" in problems[0]
    assert payload["path_owners"].get("fork_feature.py") != "G-FEATURE"


def test_duplicate_conflicting_precedence_rows_exit_1(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _multi_owned_feature_ledger(
            fx,
            ("fork_feature.py", "G-FEATURE"),
            ("fork_feature.py", "G-OTHER"),
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert payload["ok"] is False
    problems = _precedence_problems(payload, "fork_feature.py")
    assert problems
    assert "duplicate" in problems[0].lower()
    assert "G-FEATURE" in problems[0]
    assert "G-OTHER" in problems[0]
    assert payload["path_owners"].get("fork_feature.py") not in {
        "G-FEATURE",
        "G-OTHER",
    }


def test_deleted_quoted_path_must_be_owned_and_is_parsed_raw(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    quoted = (
        "apps/desktop/'/var/folders/mutex-test/home/.hermes-update-in-progress.mutex'"
    )
    _git(repo, "checkout", "upstream-main")
    junk = repo.joinpath(*quoted.split("/"))
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_text("mutex\n", encoding="utf-8")
    _git(repo, "add", "--", quoted)
    _git(repo, "commit", "-m", "chore: commit quoted mutex junk")
    _git(repo, "checkout", "fork-main")
    _git(
        repo,
        "merge",
        "--no-ff",
        "-m",
        "chore: merge upstream into fork",
        "upstream-main",
    )
    _git(repo, "rm", "--", quoted)
    _git(repo, "commit", "-m", "fix(fork): delete quoted mutex junk")
    delete_sha = _git(repo, "rev-parse", "HEAD")

    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1
    assert quoted in payload.get("unowned_paths", [])
    assert not any(path.startswith('"') for path in payload["unowned_paths"])

    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}, {delete_sha}",
            owned_files=["fork_feature.py", quoted],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 0, payload
    assert payload["unowned_paths"] == []
    assert quoted not in payload.get("unowned_paths", [])


def _run_checker_raw(
    repo: Path, *extra: str, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--upstream-ref",
            "upstream-main",
            "--fork-ref",
            "fork-main",
            *extra,
        ],
        cwd=repo,
        env=env or _GIT_ENV,
        capture_output=True,
        text=True,
    )


def test_invalid_utf8_ledger_exits_2_json_without_traceback(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    ledger = repo / "docs" / "FORK_CHANGES.md"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"# Fork\n\xff\xfe not utf-8\n")
    proc = _run_checker_raw(repo)
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]
    assert "Traceback" not in proc.stderr
    assert "Traceback" not in proc.stdout


def test_unreadable_ledger_exits_2_json_without_traceback(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    hidden = repo / "hidden"
    hidden.mkdir()
    ledger = hidden / "FORK_CHANGES.md"
    ledger.write_text("# Fork\n", encoding="utf-8")
    hidden.chmod(0o000)
    try:
        proc = _run_checker_raw(repo, "--ledger", str(ledger))
    finally:
        hidden.chmod(0o755)
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert "error" in payload
    assert "Traceback" not in proc.stderr
    assert "Traceback" not in proc.stdout


def test_git_oserror_exits_2_json_without_traceback(tmp_path):
    fx = _make_fork_repo(tmp_path)
    repo = fx["repo"]
    _write_ledger(
        repo,
        _entry(
            "G-FEATURE",
            "fork feature",
            commits=f"{fx['fork_a']}, {fx['fork_b']}",
            owned_files=["fork_feature.py"],
        ),
    )
    env = {**_GIT_ENV, "PATH": str(tmp_path / "missing-bin")}
    proc = _run_checker_raw(repo, env=env)
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]
    assert "Traceback" not in proc.stderr
    assert "Traceback" not in proc.stdout


# ---------------------------------------------------------------------------
# Real-repository ledger contract: the checked-in docs/FORK_CHANGES.md must
# exist, parse, and carry every required field in every entry. This runs from
# any checkout (no refs, no network); the full commit-mapping check against
# live upstream/main..main runs as the post_verify workflow gate instead,
# because a foreign clone may not have the upstream remote fetched.
# ---------------------------------------------------------------------------


def _load_checker_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_fork_ledger", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass processing resolves cls.__module__ through sys.modules, so the
    # module must be registered before exec.
    sys.modules["check_fork_ledger"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("check_fork_ledger", None)
        raise
    return module


def test_pre_resolved_conflict_sync_merge_is_exempt(tmp_path):
    repo = tmp_path / "pre-resolved-sync"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'base'\n", "base")
    _git(repo, "checkout", "-b", "fork-main")
    pre_resolved = _commit_file(
        repo, "shared.py", "VALUE = 'fork-plus-upstream'\n", "pre-resolve conflict"
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
    _git(repo, "checkout", "--ours", "--", "shared.py")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream with pre-resolved conflict")
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry(
            "G-SHARED",
            "pre-resolved shared path",
            commits=pre_resolved,
            owned_files=["shared.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1, payload
    assert sync_merge not in payload.get("sync_merges", [])


def test_fitted_conflict_sync_merge_is_exempt(tmp_path):
    repo = tmp_path / "fitted-sync"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'base'\n", "base")
    _git(repo, "checkout", "-b", "fork-main")
    pre_resolved = _commit_file(
        repo,
        "shared.py",
        "VALUE = 'fork'\nEXTRA = 'fork'\n",
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
    (repo / "shared.py").write_text("VALUE = 'upstream'\nEXTRA = 'fork'\n")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream with fitted conflict")
    sync_merge = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry(
            "G-SHARED",
            "fitted shared path",
            commits=pre_resolved,
            owned_files=["shared.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 0, payload
    assert sync_merge in payload["sync_merges"]


def test_pre_resolved_conflict_rejects_mode_change_in_merge(tmp_path):
    repo = tmp_path / "pre-resolved-mode-change"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'base'\n", "base")
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "shared.py", "VALUE = 'fork'\n", "pre-resolve conflict")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'upstream'\n", "upstream change")
    upstream = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 1
    _git(repo, "checkout", "--ours", "--", "shared.py")
    _git(repo, "add", "shared.py")
    _git(repo, "update-index", "--chmod=+x", "shared.py")
    _git(repo, "commit", "-m", "alter mode during conflict resolution")
    sync_merge = _git(repo, "rev-parse", "HEAD")
    parents = _git(repo, "show", "-s", "--format=%P", sync_merge).split()

    mod = _load_checker_module()
    assert not mod._is_clean_upstream_sync(repo, sync_merge, parents, upstream)


def test_pre_resolved_directory_rename_keeps_first_parent_path(tmp_path):
    repo = tmp_path / "pre-resolved-directory-rename"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "docs/base.md", "base\n", "base")
    _git(repo, "checkout", "-b", "fork-main")
    _commit_file(repo, "docs/fork-only.md", "fork\n", "add fork-only doc")
    _git(repo, "checkout", "upstream-main")
    (repo / "website" / "docs").mkdir(parents=True)
    _git(repo, "mv", "docs/base.md", "website/docs/base.md")
    _git(repo, "commit", "-m", "move upstream docs")
    upstream = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 1
    _git(repo, "rm", "--force", "website/docs/fork-only.md")
    _git(repo, "checkout", "HEAD", "--", "docs/fork-only.md")
    _git(repo, "add", "docs/fork-only.md")
    _git(repo, "commit", "-m", "retain canonical fork doc path")
    sync_merge = _git(repo, "rev-parse", "HEAD")
    parents = _git(repo, "show", "-s", "--format=%P", sync_merge).split()

    mod = _load_checker_module()
    assert mod._is_clean_upstream_sync(repo, sync_merge, parents, upstream)


def test_repo_ledger_exists_and_every_entry_is_well_formed():
    ledger = REPO_ROOT / "docs" / "FORK_CHANGES.md"
    assert ledger.is_file(), "docs/FORK_CHANGES.md is missing"
    mod = _load_checker_module()
    entries = mod.parse_ledger(ledger.read_text(encoding="utf-8"))
    assert entries, "ledger has no entries"
    for entry in entries:
        mod.validate_entry(entry)
    problems = {e.entry_id: e.problems for e in entries if e.problems}
    assert not problems, f"malformed ledger entries: {problems}"


def test_repo_ledger_entry_ids_are_unique():
    ledger = REPO_ROOT / "docs" / "FORK_CHANGES.md"
    mod = _load_checker_module()
    entries = mod.parse_ledger(ledger.read_text(encoding="utf-8"))
    ids = [e.entry_id for e in entries]
    assert len(ids) == len(set(ids)), f"duplicate entry ids: {ids}"


def test_cross_owner_commit_maps_only_when_no_single_owner_can_claim(tmp_path):
    """A published commit with two effective owners maps only through the explicit field."""
    repo = tmp_path / "cross-owner"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "base.py", "BASE = 1\n", "chore: upstream base")
    _git(repo, "checkout", "-b", "fork-main")
    mixed = _commit_file(repo, "a.py", "A = 1\n", "feat: touch a")
    # Second path in the same commit: amend is a new commit, so write both before commit.
    # The helper already committed a.py. Add b.py as its own commit, then we need one
    # commit that changes both. Rebuild that commit.
    _git(repo, "reset", "--soft", "HEAD~1")
    (repo / "b.py").write_text("B = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py", "b.py")
    _git(repo, "commit", "-m", "feat: touch two owners")
    mixed = _git(repo, "rev-parse", "HEAD")
    single = _commit_file(repo, "a.py", "A = 2\n", "fix: touch only a")
    body = (
        _entry("G-A", "owner a", commits="none", owned_files=["a.py"])
        + _entry("G-B", "owner b", commits="none", owned_files=["b.py"])
        + (
            "## G-FORK-LEDGER: fixture ledger\n"
            "- Commits: self\n"
            f"- Cross-Owner-Commits: {mixed}\n"
            "- Owned-Files:\n"
            "  - docs/FORK_CHANGES.md\n"
            "- Intent: Record the cross-owner commit.\n"
            "- Protected-Invariant: Single-owner commits stay on normal claims.\n"
            "- Tests: tests/ci/test_check_fork_ledger.py\n"
            "- Retirement-Condition: The fixture is gone.\n"
            "- Disposition: active\n"
        )
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    assert single in {item["sha"] for item in payload["unmapped_commits"]}
    assert mixed not in {item["sha"] for item in payload["unmapped_commits"]}
    refused = _entry("G-A", "owner a", commits=single, owned_files=["a.py"])
    body = body.replace(
        _entry("G-A", "owner a", commits="none", owned_files=["a.py"]),
        refused,
        1,
    )
    body = body.replace(
        f"- Cross-Owner-Commits: {mixed}\n",
        f"- Cross-Owner-Commits: {mixed}, {single}\n",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1
    problems = " ".join(
        problem
        for entry in payload["invalid_entries"]
        for problem in entry["problems"]
    )
    assert "single effective owner" in problems


def _conflict_drop_repo(tmp_path: Path, name: str) -> dict[str, str]:
    """Fork merge that keeps the fork file, then upstream moves on."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'base'\n", "base")
    _git(repo, "checkout", "-b", "fork-main")
    custom = _commit_file(
        repo,
        "shared.py",
        "VALUE = 'fork'\nEXTRA = 'fork'\n",
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
    _git(repo, "checkout", "--ours", "--", "shared.py")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream by keeping the fork file")
    bad = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'upstream-now'\n", "upstream moves on")
    _git(repo, "checkout", "fork-main")
    return {"repo": str(repo), "custom": custom, "bad": bad}


def test_repaired_conflict_merge_passes_only_when_tip_keeps_upstream_lines(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "repaired-conflict")
    repo = Path(fx["repo"])
    _commit_file(
        repo,
        "shared.py",
        "VALUE = 'upstream-now'\nEXTRA = 'fork'\n",
        "restore upstream lines",
    )
    restored = _git(repo, "rev-parse", "HEAD")
    body = (
        _entry(
            "G-SHARED",
            "shared path",
            commits=f"{fx['custom']}, {restored}",
            owned_files=["shared.py"],
        )
        + (
            "## G-FORK-LEDGER: fixture ledger\n"
            "- Commits: self\n"
            f"- Repaired-Conflict-Merges: {fx['bad']}\n"
            "- Owned-Files:\n"
            "  - docs/FORK_CHANGES.md\n"
            "- Intent: Record a conflict merge only after the tip keeps upstream lines.\n"
            "- Protected-Invariant: A listed merge stays a failure if the tip drops a line.\n"
            "- Tests: tests/ci/test_check_fork_ledger.py\n"
            "- Retirement-Condition: The fixture is gone.\n"
            "- Disposition: active\n"
        )
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 0, payload
    assert fx["bad"] in payload["repaired_conflict_merges"]
    assert fx["bad"] not in {item["sha"] for item in payload["unmapped_commits"]}
    assert fx["bad"] not in payload["sync_merges"]


def test_repaired_conflict_merge_stays_unmapped_without_the_lines(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "unrepaired-conflict")
    repo = Path(fx["repo"])
    body = (
        _entry(
            "G-SHARED",
            "shared path",
            commits=fx["custom"],
            owned_files=["shared.py"],
        )
        + (
            "## G-FORK-LEDGER: fixture ledger\n"
            "- Commits: self\n"
            f"- Repaired-Conflict-Merges: {fx['bad']}\n"
            "- Owned-Files:\n"
            "  - docs/FORK_CHANGES.md\n"
            "- Intent: Refuse a repair listing that did not restore upstream lines.\n"
            "- Protected-Invariant: Listing the SHA is not a waiver.\n"
            "- Tests: tests/ci/test_check_fork_ledger.py\n"
            "- Retirement-Condition: The fixture is gone.\n"
            "- Disposition: active\n"
        )
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    assert fx["bad"] in {item["sha"] for item in payload["unmapped_commits"]}
    problems = " ".join(
        problem
        for entry in payload["invalid_entries"]
        for problem in entry["problems"]
    )
    assert "shared.py" in problems
    assert "does not keep current upstream lines" in problems


def _repaired_ledger(owner_commits: str, bad: str) -> str:
    return _entry(
        "G-SHARED",
        "shared path",
        commits=owner_commits,
        owned_files=["shared.py"],
    ) + (
        "## G-FORK-LEDGER: fixture ledger\n"
        "- Commits: self\n"
        f"- Repaired-Conflict-Merges: {bad}\n"
        "- Owned-Files:\n"
        "  - docs/FORK_CHANGES.md\n"
        "- Intent: A later owned edit of a conflict path is the owner's audited work.\n"
        "- Protected-Invariant: A drop with no later owned edit stays a failure.\n"
        "- Tests: tests/ci/test_check_fork_ledger.py\n"
        "- Retirement-Condition: The fixture is gone.\n"
        "- Disposition: active\n"
    )


def _blob(repo: Path, rev: str, path: str) -> str:
    return _git(repo, "rev-parse", f"{rev}:{path}")


def test_upstream_line_deletion_waives_only_lines_in_the_declared_blobs(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "declared-deletion")
    repo = Path(fx["repo"])
    # The upstream version the merge resolved against. Its lines are the audited text.
    resolved = _blob(repo, "upstream-main~1", "shared.py")
    body = _repaired_ledger(fx["custom"], fx["bad"]).replace(
        "- Repaired-Conflict-Merges:",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs " + resolved + "\n"
        "- Repaired-Conflict-Merges:",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    # The declaration covers the resolved version, not the lines upstream added after it.
    assert code == 1, payload
    problems = " ".join(
        problem for entry in payload["invalid_entries"] for problem in entry["problems"]
    )
    assert "leaves" in problems and "unaudited" in problems


def test_upstream_line_deletion_covers_the_current_upstream_version(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "declared-current")
    repo = Path(fx["repo"])
    current = _blob(repo, "upstream-main", "shared.py")
    body = _repaired_ledger(fx["custom"], fx["bad"]).replace(
        "- Repaired-Conflict-Merges:",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs " + current + "\n"
        "- Repaired-Conflict-Merges:",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 0, payload
    assert fx["bad"] in payload["repaired_conflict_merges"]


def test_upstream_line_deletion_counts_repeated_lines(tmp_path):
    # The audited blob never contained the line. Upstream now has it twice and
    # the tip kept it once, so one occurrence is unaudited. Set membership
    # would see the line present at the tip and waive both; counting must not.
    repo = tmp_path / "repeated-line"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "shared.py", "OTHER = 1\n", "base")
    audited = _blob(repo, "HEAD", "shared.py")
    _git(repo, "checkout", "-b", "fork-main")
    custom = _commit_file(repo, "shared.py", "OTHER = 1\nEXTRA = 'fork'\n", "fork edit")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'x'\n", "upstream adds the line")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo, env=_GIT_ENV, capture_output=True, text=True,
    )
    assert merge.returncode == 1
    _git(repo, "checkout", "--ours", "--", "shared.py")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream by keeping the fork file")
    bad = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'x'\nVALUE = 'x'\n", "upstream repeats the line")
    _git(repo, "checkout", "fork-main")
    (repo / "shared.py").write_text("VALUE = 'x'\nEXTRA = 'fork'\n")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "keep one occurrence")
    kept = _git(repo, "rev-parse", "HEAD")
    body = _repaired_ledger(f"{custom}, {kept}", bad).replace(
        "- Repaired-Conflict-Merges:",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs " + audited + "\n"
        "- Repaired-Conflict-Merges:",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    problems = " ".join(
        problem for entry in payload["invalid_entries"] for problem in entry["problems"]
    )
    assert "unaudited" in problems


def _commit_bytes(repo: Path, rel: str, content: bytes, subject: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def test_upstream_line_deletion_counts_a_crlf_difference(tmp_path):
    # A CR-LF line and an LF line are different content. The audited blob is LF
    # and upstream is now CR-LF, so the difference is unaudited.
    repo = tmp_path / "crlf"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_bytes(repo, "shared.py", b"X\n", "base with LF")
    audited = _blob(repo, "HEAD", "shared.py")
    _git(repo, "checkout", "-b", "fork-main")
    custom = _commit_bytes(repo, "shared.py", b"OTHER\n", "fork drops the line")
    _git(repo, "checkout", "upstream-main")
    _commit_bytes(repo, "shared.py", b"X\r\n", "upstream switches to CR-LF")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo, env=_GIT_ENV, capture_output=True, text=True,
    )
    assert merge.returncode == 1
    _git(repo, "checkout", "--ours", "--", "shared.py")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream by keeping the fork file")
    bad = _git(repo, "rev-parse", "HEAD")
    body = _repaired_ledger(custom, bad).replace(
        "- Repaired-Conflict-Merges:",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs " + audited + "\n"
        "- Repaired-Conflict-Merges:",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    problems = " ".join(
        problem for entry in payload["invalid_entries"] for problem in entry["problems"]
    )
    assert "unaudited" in problems


def test_upstream_line_deletion_counts_a_missing_final_newline(tmp_path):
    # Git treats a missing final newline as distinct content. The audited blob
    # has the newline and upstream does not, so the difference is unaudited.
    # str.splitlines() would collapse the two and waive it.
    repo = tmp_path / "final-newline"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_bytes(repo, "shared.py", b"X\n", "base with a final newline")
    audited = _blob(repo, "HEAD", "shared.py")
    _git(repo, "checkout", "-b", "fork-main")
    custom = _commit_bytes(repo, "shared.py", b"OTHER\n", "fork drops the line")
    _git(repo, "checkout", "upstream-main")
    _commit_bytes(repo, "shared.py", b"X", "upstream drops the final newline")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo, env=_GIT_ENV, capture_output=True, text=True,
    )
    assert merge.returncode == 1
    _git(repo, "checkout", "--ours", "--", "shared.py")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream by keeping the fork file")
    bad = _git(repo, "rev-parse", "HEAD")
    body = _repaired_ledger(custom, bad).replace(
        "- Repaired-Conflict-Merges:",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs " + audited + "\n"
        "- Repaired-Conflict-Merges:",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    problems = " ".join(
        problem for entry in payload["invalid_entries"] for problem in entry["problems"]
    )
    assert "unaudited" in problems


def test_upstream_line_deletion_counts_blank_lines(tmp_path):
    # A trailing blank line is text. The audited blob has one blank line and
    # upstream now has two, so one is unaudited. Stripping newlines before
    # counting would hide it.
    repo = tmp_path / "blank-lines"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "shared.py", "\nX\n", "base with one blank line")
    audited = _blob(repo, "HEAD", "shared.py")
    _git(repo, "checkout", "-b", "fork-main")
    custom = _commit_file(repo, "shared.py", "X\n", "fork drops the blank line")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "shared.py", "\n\nX\n", "upstream adds a second blank line")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo, env=_GIT_ENV, capture_output=True, text=True,
    )
    assert merge.returncode == 1
    _git(repo, "checkout", "--ours", "--", "shared.py")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream by keeping the fork file")
    bad = _git(repo, "rev-parse", "HEAD")
    body = _repaired_ledger(custom, bad).replace(
        "- Repaired-Conflict-Merges:",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs " + audited + "\n"
        "- Repaired-Conflict-Merges:",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    problems = " ".join(
        problem for entry in payload["invalid_entries"] for problem in entry["problems"]
    )
    assert "unaudited" in problems


def test_upstream_line_deletion_rejects_a_repeated_blob_id(tmp_path):
    # Repeating a blob hash must not count its lines twice. One audited
    # occurrence cannot cover two upstream occurrences by being named twice.
    repo = tmp_path / "repeated-blob"
    repo.mkdir()
    _git(repo, "init", "-b", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'x'\n", "base")
    audited = _blob(repo, "HEAD", "shared.py")
    _git(repo, "checkout", "-b", "fork-main")
    custom = _commit_file(repo, "shared.py", "OTHER = 1\n", "fork drops the line")
    _git(repo, "checkout", "upstream-main")
    _commit_file(repo, "shared.py", "VALUE = 'x'\nVALUE = 'x'\n", "upstream repeats the line")
    _git(repo, "checkout", "fork-main")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "upstream-main", "-m", "sync upstream"],
        cwd=repo, env=_GIT_ENV, capture_output=True, text=True,
    )
    assert merge.returncode == 1
    _git(repo, "checkout", "--ours", "--", "shared.py")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "sync upstream by keeping the fork file")
    bad = _git(repo, "rev-parse", "HEAD")
    body = _repaired_ledger(custom, bad).replace(
        "- Repaired-Conflict-Merges:",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs "
        + audited + "+" + audited + "\n"
        "- Repaired-Conflict-Merges:",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    problems = " ".join(
        problem for entry in payload["invalid_entries"] for problem in entry["problems"]
    )
    assert "repeats a blob id" in problems


def test_upstream_line_deletion_rejects_a_blob_upstream_never_committed(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "foreign-blob")
    repo = Path(fx["repo"])
    _commit_file(repo, "other.py", "OTHER = 1\n", "unrelated")
    foreign = _blob(repo, "HEAD", "other.py")
    body = _repaired_ledger(fx["custom"], fx["bad"]).replace(
        "- Repaired-Conflict-Merges:",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs " + foreign + "\n"
        "- Repaired-Conflict-Merges:",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    problems = " ".join(
        problem for entry in payload["invalid_entries"] for problem in entry["problems"]
    )
    assert "not a blob upstream ever committed" in problems


def test_upstream_line_deletion_is_refused_from_any_other_entry(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "owner-declares")
    repo = Path(fx["repo"])
    current = _blob(repo, "upstream-main", "shared.py")
    body = _repaired_ledger(fx["custom"], fx["bad"]).replace(
        "- Owned-Files:\n  - shared.py\n",
        "- Upstream-Line-Deletions: shared.py removed-upstream-blobs " + current + "\n"
        "- Owned-Files:\n  - shared.py\n",
        1,
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    problems = " ".join(
        problem for entry in payload["invalid_entries"] for problem in entry["problems"]
    )
    assert "may be declared only by G-FORK-LEDGER" in problems


def test_repaired_conflict_merge_still_fails_when_the_later_edit_is_unmapped(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "unmapped-rewrite")
    repo = Path(fx["repo"])
    _commit_file(
        repo,
        "shared.py",
        "VALUE = 'fork-rewrite'\nEXTRA = 'fork'\n",
        "unmapped rewrite of the shared module",
    )
    unmapped = _git(repo, "rev-parse", "HEAD")
    _write_ledger(repo, _repaired_ledger(fx["custom"], fx["bad"]))
    code, payload = _run_checker(repo)
    assert code == 1, payload
    assert unmapped in {item["sha"] for item in payload["unmapped_commits"]}


def test_repaired_conflict_merge_ignores_a_later_edit_of_an_unowned_path(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "unowned-rewrite")
    repo = Path(fx["repo"])
    rewrite = _commit_file(
        repo,
        "shared.py",
        "VALUE = 'fork-rewrite'\nEXTRA = 'fork'\n",
        "rewrite with no owner for the path",
    )
    body = _repaired_ledger(f"{fx['custom']}, {rewrite}", fx["bad"]).replace(
        "  - shared.py\n", "  - other.py\n", 1
    )
    _write_ledger(repo, body)
    code, payload = _run_checker(repo)
    assert code == 1, payload
    assert fx["bad"] in {item["sha"] for item in payload["unmapped_commits"]}
    assert "shared.py" in payload["unowned_paths"]
    problems = " ".join(
        problem
        for entry in payload["invalid_entries"]
        for problem in entry["problems"]
    )
    assert "does not keep current upstream lines: shared.py" in problems


def test_restored_conflict_merge_stays_unmapped_when_unlisted(tmp_path):
    fx = _conflict_drop_repo(tmp_path, "unlisted-repair")
    repo = Path(fx["repo"])
    _commit_file(
        repo,
        "shared.py",
        "VALUE = 'upstream-now'\nEXTRA = 'fork'\n",
        "restore upstream lines",
    )
    restored = _git(repo, "rev-parse", "HEAD")
    _write_ledger(
        repo,
        _entry(
            "G-SHARED",
            "shared path",
            commits=f"{fx['custom']}, {restored}",
            owned_files=["shared.py"],
        ),
    )
    code, payload = _run_checker(repo)
    assert code == 1, payload
    assert fx["bad"] in {item["sha"] for item in payload["unmapped_commits"]}
