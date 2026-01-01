#!/usr/bin/env python3
"""Verify docs/FORK_CHANGES.md maps every fork-only commit to a ledger entry.

This is the deterministic post_verify gate behind the fork change ledger
(ARD-010 in the Central Command orchestration plan): the autonomous upstream
conflict resolver may only act on fork changes whose ownership is documented,
so an unmapped fork-only commit or a ledger entry missing a required field is
a hard failure.

Mapping methodology
-------------------
Both refs resolve once to immutable OIDs. Fork-only commits are
`git rev-list <upstream-oid>..<fork-oid>`. A merge is exempt only when it has
exactly two parents in fork/upstream order and its committed tree equals the
clean `git merge-tree --write-tree` result; every other commit is mapped work.

A Commits field may contain full SHAs, non-empty `<sha>..<sha>` ranges, the token
`none` (path ownership only, no commit claim), and the token `self`. `self`
is component-scoped self-mapping, not a singular delivery-commit special
case. It maps any future work commit that changes the ledger and only files
owned by that entry. It stays narrow because the commit must touch the
ledger and every changed path must already be listed in Owned-Files. A
commit that also changes an unrelated path remains unmapped. Do not record
a branch-only SHA for those commits; that SHA changes on merge or squash.

Explicit claims must be in the pinned work range and the claimant must be the
effective owner of every first-parent changed path under all ledger
declarations, including paths absent from the current three-dot delta. `self`
also requires that its specific entry changed. Current-path ownership reporting
is limited to the pinned three-dot changed path set, including both sides of
renames and reversibly decoded non-UTF-8 names. Every current path must have
exactly one owner, or an explicit Path-Precedence row naming one of its owners.
Missing and ambiguous current paths fail closed. Precedence never invents an owner.
A path may be declared once; a repeated row fails closed whether the winner
is the same or conflicting, and is reported in invalid_precedence. The
winner is the token after the last `: <ID>`, so colon and quoted paths stay
intact.

The checker is standalone: stdlib only, no network, works from any checkout
via --repo, rejects ledger paths outside that repository, and reads ledger
bytes from the pinned fork object. Output is a single
JSON document on stdout; exit 0 means the ledger is complete and well formed,
exit 1 means violations were found, exit 2 means the checker itself could not
run (missing or unreadable ledger, invalid UTF-8, unresolvable ref, git
execution failure). Environment failures emit JSON and no traceback.

Usage:
    python scripts/ci/check_fork_ledger.py \\
        [--repo PATH] [--ledger PATH] \\
        [--upstream-ref upstream/main] [--fork-ref main]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_FIELDS = (
    "Commits",
    "Owned-Files",
    "Intent",
    "Protected-Invariant",
    "Tests",
    "Retirement-Condition",
    "Disposition",
)

# Entry headers look like: ## G-KANBAN-LIFECYCLE: durable Kanban lifecycle
_ENTRY_HEADER = re.compile(r"^##\s+(?P<id>[A-Z0-9][A-Z0-9-]*):\s+(?P<title>.+?)\s*$")
_FIELD_LINE = re.compile(r"^-\s+(?P<key>[A-Za-z-]+):\s*(?P<value>.*)$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_FULL_SHA_RANGE = re.compile(r"^(?P<a>[0-9a-f]{40})\.\.(?P<b>[0-9a-f]{40})$")
_ABBREVIATED_SHA = re.compile(r"^[0-9a-f]{7,39}(?:\.\.[0-9a-f]{7,39})?$")


class CheckerError(RuntimeError):
    """Environment/setup failure: the check could not be evaluated at all."""


def _git_bytes(repo: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
        )
    except OSError as exc:
        raise CheckerError(
            f"git {' '.join(args)} could not be executed: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise CheckerError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return proc.stdout


def _git(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8", errors="surrogateescape").strip("\n")


@dataclass
class LedgerEntry:
    entry_id: str
    title: str
    line: int
    fields: dict[str, str] = field(default_factory=dict)
    owned_files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _decode_owned_path(raw: str) -> str:
    """Decode one Owned-Files item using plain or JSON-string syntax."""
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON-escaped owned path: {exc}") from exc
        if not isinstance(value, str):
            raise ValueError("JSON-escaped owned path must decode to a string")
        if "\0" in value:
            raise ValueError("owned path cannot contain NUL")
        return value
    if raw != raw.strip() or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError(
            "owned path with edge whitespace/control bytes must use JSON-string syntax"
        )
    return raw


def parse_ledger(text: str) -> list[LedgerEntry]:
    """Parse ledger entries: `## ID: title` headers followed by `- Key: value`
    lines; `- Owned-Files:` introduces an indented `  - path` list."""
    entries: list[LedgerEntry] = []
    current: LedgerEntry | None = None
    in_owned_files = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        header = _ENTRY_HEADER.match(raw)
        if header:
            current = LedgerEntry(
                entry_id=header.group("id"), title=header.group("title"), line=lineno
            )
            entries.append(current)
            in_owned_files = False
            continue
        if current is None:
            continue
        if in_owned_files:
            item = re.match(r"^  - (?P<path>.+)$", raw)
            if item:
                try:
                    current.owned_files.append(_decode_owned_path(item.group("path")))
                except ValueError as exc:
                    current.problems.append(str(exc))
                continue
            in_owned_files = False  # fall through: this line may be a field
        fld = _FIELD_LINE.match(raw)
        if fld:
            key, value = fld.group("key"), fld.group("value").strip()
            if key in current.fields:
                current.problems.append(f"duplicate field: {key}")
                continue
            current.fields[key] = value
            if key == "Owned-Files" and not value:
                in_owned_files = True
    return entries


def validate_entry(entry: LedgerEntry) -> None:
    for key in REQUIRED_FIELDS:
        if key not in entry.fields:
            entry.problems.append(f"missing required field: {key}")
            continue
        if key == "Owned-Files":
            if entry.fields[key]:
                entry.problems.append("Owned-Files must use list form")
            if not entry.owned_files:
                entry.problems.append("Owned-Files list is empty")
        elif not entry.fields[key]:
            entry.problems.append(f"required field is empty: {key}")


_PRECEDENCE_HEADER = re.compile(r"^##\s+Path-Precedence\s*$")
_PRECEDENCE_LINE = re.compile(r"^- (?P<path>.+): (?P<id>[A-Z0-9][A-Z0-9-]*)$")
_SPEC_SPLIT = re.compile(r"[\s,]+")


def parse_path_precedence(
    text: str,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Parse one exact precedence section and report malformed control rows."""
    precedence: dict[str, str] = {}
    first_winner: dict[str, str] = {}
    problems: list[dict[str, str]] = []
    in_section = False
    section_count = 0
    for raw in text.splitlines():
        if _PRECEDENCE_HEADER.match(raw):
            section_count += 1
            in_section = True
            if section_count > 1:
                problems.append({
                    "path": "Path-Precedence",
                    "problem": "duplicate Path-Precedence section",
                })
            continue
        if raw.startswith("## "):
            in_section = False
            continue
        if not in_section or not raw.strip():
            continue
        if not raw.startswith("-"):
            continue
        match = _PRECEDENCE_LINE.match(raw)
        if not match:
            problems.append({
                "path": raw,
                "problem": f"malformed Path-Precedence row: {raw}",
            })
            continue
        raw_path = match.group("path")
        winner = match.group("id")
        try:
            path = _decode_owned_path(raw_path)
        except ValueError as exc:
            problems.append({
                "path": raw_path,
                "problem": f"invalid Path-Precedence path: {exc}",
            })
            continue
        if path in first_winner:
            problems.append({
                "path": path,
                "problem": (
                    "duplicate Path-Precedence declaration for "
                    f"{path}: already {first_winner[path]}, "
                    f"repeated as {winner}; declare each path once"
                ),
            })
            precedence.pop(path, None)
            continue
        first_winner[path] = winner
        precedence[path] = winner
    return precedence, problems


def audit_commit_tokens(entry: LedgerEntry) -> tuple[bool, bool]:
    """Allow only SHAs, ranges, `self`, and sole token `none`."""
    spec = entry.fields.get("Commits", "")
    tokens = [token for token in _SPEC_SPLIT.split(spec) if token]
    has_self = False
    has_none = False
    has_sha = False
    for token in tokens:
        if token == "self":
            has_self = True
        elif token == "none":
            has_none = True
        elif _FULL_SHA.fullmatch(token) or _FULL_SHA_RANGE.fullmatch(token):
            has_sha = True
        elif _ABBREVIATED_SHA.fullmatch(token):
            entry.problems.append(
                f"commit claims require full 40-character object IDs: {token}"
            )
        else:
            entry.problems.append(f"unknown commit token: {token}")
    if has_none and (has_self or has_sha):
        entry.problems.append(
            "commit token none cannot be combined with other commit claims"
        )
    return has_self, has_none


def _read_ledger_from_ref(repo: Path, fork_oid: str, ledger_rel: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{fork_oid}:{ledger_rel}"],
            capture_output=True,
        )
    except OSError as exc:
        raise CheckerError(f"git show could not be executed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CheckerError(
            f"ledger not found in pinned fork ref: {ledger_rel}: {detail}"
        )
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckerError(
            f"ledger is not valid UTF-8 in pinned fork ref: {ledger_rel}: {exc}"
        ) from exc


def _ledger_repo_rel(repo: Path, ledger_path: Path) -> str:
    try:
        return ledger_path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError as exc:
        raise CheckerError(
            f"ledger path must be inside repository: {ledger_path}"
        ) from exc


def _parse_name_status_z(raw: bytes) -> list[str]:
    """Decode NUL records reversibly and retain both sides of renames/copies."""
    parts = raw.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(parts):
        if parts[index] == b"":
            index += 1
            continue
        status = parts[index].decode("ascii", errors="strict")
        if status[:1] in {"R", "C"}:
            if index + 2 >= len(parts):
                break
            paths.extend(
                part.decode("utf-8", errors="surrogateescape")
                for part in (parts[index + 1], parts[index + 2])
            )
            index += 3
            continue
        if index + 1 >= len(parts):
            break
        paths.append(parts[index + 1].decode("utf-8", errors="surrogateescape"))
        index += 2
    return paths


def fork_changed_paths(repo: Path, upstream_ref: str, fork_ref: str) -> list[str]:
    raw = _git_bytes(
        repo, "diff", "--name-status", "-z", f"{upstream_ref}...{fork_ref}"
    )
    seen: set[str] = set()
    ordered: list[str] = []
    for path in _parse_name_status_z(raw):
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def commit_changed_paths(repo: Path, sha: str) -> set[str]:
    parents = _git(repo, "show", "-s", "--format=%P", sha).split()
    if parents:
        raw = _git_bytes(repo, "diff", "--name-status", "-z", parents[0], sha)
    else:
        raw = _git_bytes(
            repo,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            sha,
        )
    return set(_parse_name_status_z(raw))


def _run_claim_git(repo: Path, *args: str) -> tuple[int, str]:
    """Run claim-resolution Git; preserve invalid-object status for the ledger."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
        )
    except OSError as exc:
        raise CheckerError(
            f"git {' '.join(args)} could not be executed: {exc}"
        ) from exc
    return proc.returncode, proc.stdout.decode(
        "utf-8", errors="surrogateescape"
    ).strip()


def resolve_entry_commits(repo: Path, entry: LedgerEntry) -> set[str]:
    """Expand validated full-SHA claims; invalid or empty claims fail closed."""
    spec = entry.fields.get("Commits", "")
    shas: set[str] = set()
    for token in (part for part in _SPEC_SPLIT.split(spec) if part):
        range_match = _FULL_SHA_RANGE.fullmatch(token)
        if range_match:
            a, b = range_match.group("a"), range_match.group("b")
            returncode, output = _run_claim_git(repo, "rev-list", f"{a}..{b}")
            if returncode != 0:
                entry.problems.append(f"unresolvable commit range: {token}")
                continue
            expanded = output.split()
            if not expanded:
                entry.problems.append(f"empty commit range: {token}")
                continue
            shas.update(expanded)
            continue
        if not _FULL_SHA.fullmatch(token):
            continue
        returncode, output = _run_claim_git(repo, "rev-parse", f"{token}^{{commit}}")
        if returncode != 0:
            entry.problems.append(f"unresolvable commit: {token}")
            continue
        shas.add(output)
    return shas


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            capture_output=True,
        )
    except OSError as exc:
        raise CheckerError(f"git merge-base could not be executed: {exc}") from exc
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise CheckerError(
        "git merge-base --is-ancestor failed "
        f"(exit {proc.returncode}): "
        f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
    )


def _is_clean_upstream_sync(
    repo: Path, sha: str, parents: list[str], upstream_oid: str
) -> bool:
    if len(parents) != 2:
        return False
    first_parent, upstream_parent = parents
    if not _is_ancestor(repo, upstream_parent, upstream_oid):
        return False
    if _is_ancestor(repo, first_parent, upstream_oid):
        return False
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-tree",
                "--write-tree",
                first_parent,
                upstream_parent,
            ],
            capture_output=True,
        )
    except OSError as exc:
        raise CheckerError(f"git merge-tree could not be executed: {exc}") from exc
    if proc.returncode == 1:
        return False
    if proc.returncode != 0:
        raise CheckerError(
            f"git merge-tree failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    expected_tree = proc.stdout.decode("ascii", errors="strict").splitlines()[0]
    actual_tree = _git(repo, "rev-parse", f"{sha}^{{tree}}")
    return expected_tree == actual_tree


def classify_range(
    repo: Path, upstream_ref: str, fork_ref: str
) -> tuple[list[dict[str, str]], list[str]]:
    """Split pinned range into mapped work and strictly proven clean sync merges."""
    out = _git(
        repo,
        "rev-list",
        "--format=%H%x00%P%x00%s",
        "--no-commit-header",
        f"{upstream_ref}..{fork_ref}",
    )
    work: list[dict[str, str]] = []
    sync_merges: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, parents_raw, subject = line.split("\x00", 2)
        parents = parents_raw.split()
        if _is_clean_upstream_sync(repo, sha, parents, upstream_ref):
            sync_merges.append(sha)
            continue
        work.append({"sha": sha, "subject": subject})
    return work, sync_merges


def _self_matches(
    repo: Path,
    entry: LedgerEntry,
    work: list[dict[str, str]],
    ledger_rel: str,
    path_cache: dict[str, set[str]],
    effective_owners: dict[str, str],
) -> set[str]:
    """Map commits that change this exact entry and only its effective paths."""
    owned = set(entry.owned_files)
    matched: set[str] = set()
    for commit in work:
        sha = commit["sha"]
        if sha not in path_cache:
            path_cache[sha] = commit_changed_paths(repo, sha)
        paths = path_cache[sha]
        lacks_effective_ownership = any(
            effective_owners.get(path) != entry.entry_id for path in paths
        )
        if (
            ledger_rel in paths
            and paths <= owned
            and not lacks_effective_ownership
            and _commit_changes_entry(repo, sha, ledger_rel, entry.entry_id)
        ):
            matched.add(sha)
    return matched


def _entry_fingerprint_at(
    repo: Path, oid: str, ledger_rel: str, entry_id: str
) -> tuple[str, tuple[tuple[str, str], ...], tuple[str, ...]] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{oid}:{ledger_rel}"],
            capture_output=True,
        )
    except OSError as exc:
        raise CheckerError(f"git show could not be executed: {exc}") from exc
    if proc.returncode != 0:
        return None
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckerError(
            f"ledger is not valid UTF-8 at {oid}:{ledger_rel}: {exc}"
        ) from exc
    candidates = [item for item in parse_ledger(text) if item.entry_id == entry_id]
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    return (
        candidate.title,
        tuple(sorted(candidate.fields.items())),
        tuple(candidate.owned_files),
    )


def _commit_changes_entry(repo: Path, sha: str, ledger_rel: str, entry_id: str) -> bool:
    parents = _git(repo, "show", "-s", "--format=%P", sha).split()
    before = (
        _entry_fingerprint_at(repo, parents[0], ledger_rel, entry_id)
        if parents
        else None
    )
    after = _entry_fingerprint_at(repo, sha, ledger_rel, entry_id)
    return after is not None and after != before


def _declared_path_owners(entries: list[LedgerEntry]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    for entry in entries:
        for path in entry.owned_files:
            ids = owners.setdefault(path, [])
            if entry.entry_id not in ids:
                ids.append(entry.entry_id)
    return owners


def _resolve_all_declared_paths(
    owners: dict[str, list[str]],
    precedence: dict[str, str],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Resolve effective ownership for every path declared by any entry."""
    effective_owners = {path: ids[0] for path, ids in owners.items() if len(ids) == 1}
    invalid_precedence: list[dict[str, str]] = []

    for path, winner in precedence.items():
        listed = owners.get(path, [])
        if winner not in listed or len(listed) < 2:
            invalid_precedence.append({
                "path": path,
                "problem": (
                    "precedence must name one of at least two owners that "
                    f"list the path; got {winner} for {listed}"
                ),
            })
            continue
        effective_owners[path] = winner

    return effective_owners, invalid_precedence


def _resolve_current_paths(
    changed: list[str],
    owners: dict[str, list[str]],
    effective_owners: dict[str, str],
) -> tuple[dict[str, str], list[str], list[dict[str, object]]]:
    """Limit current-path ownership reporting to the three-dot changed paths."""
    path_owners: dict[str, str] = {}
    unowned: list[str] = []
    ambiguous: list[dict[str, object]] = []

    for path in changed:
        listed = owners.get(path, [])
        if not listed:
            unowned.append(path)
            continue
        effective_owner = effective_owners.get(path)
        if effective_owner is not None:
            path_owners[path] = effective_owner
            continue
        ambiguous.append({"path": path, "entries": listed})

    return path_owners, unowned, ambiguous


def run_check(repo: Path, ledger_path: Path, upstream_ref: str, fork_ref: str) -> dict:
    repo = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    upstream_oid = _git(repo, "rev-parse", "--verify", f"{upstream_ref}^{{commit}}")
    fork_oid = _git(repo, "rev-parse", "--verify", f"{fork_ref}^{{commit}}")
    ledger_rel = _ledger_repo_rel(repo, ledger_path)
    ledger_text = _read_ledger_from_ref(repo, fork_oid, ledger_rel)
    entries = parse_ledger(ledger_text)
    seen_ids: set[str] = set()
    wants_self: list[LedgerEntry] = []
    for entry in entries:
        validate_entry(entry)
        if entry.entry_id in seen_ids:
            entry.problems.append(f"duplicate entry id: {entry.entry_id}")
        seen_ids.add(entry.entry_id)
        has_self, _has_none = audit_commit_tokens(entry)
        if has_self:
            wants_self.append(entry)

    explicit_claims = [resolve_entry_commits(repo, entry) for entry in entries]
    work, sync_merges = classify_range(repo, upstream_oid, fork_oid)
    work_shas = {commit["sha"] for commit in work}
    changed = fork_changed_paths(repo, upstream_oid, fork_oid)
    precedence, precedence_problems = parse_path_precedence(ledger_text)
    declared_owners = _declared_path_owners(entries)
    effective_owners, invalid_precedence = _resolve_all_declared_paths(
        declared_owners, precedence
    )
    path_owners, unowned, ambiguous = _resolve_current_paths(
        changed, declared_owners, effective_owners
    )
    invalid_precedence = precedence_problems + invalid_precedence
    path_cache: dict[str, set[str]] = {}
    claims: dict[str, list[str]] = {}
    for entry, claimed in zip(entries, explicit_claims, strict=True):
        valid_claimed: set[str] = set()
        for sha in set(claimed):
            if sha not in work_shas:
                entry.problems.append(
                    f"explicit commit claim is outside evaluated work range: {sha}"
                )
                continue
            if sha not in path_cache:
                path_cache[sha] = commit_changed_paths(repo, sha)
            outside = sorted(path_cache[sha] - set(entry.owned_files))
            if outside:
                entry.problems.append(
                    f"explicit commit {sha} changes paths outside Owned-Files: "
                    + ", ".join(outside)
                )
                continue
            losing = sorted(
                path
                for path in path_cache[sha]
                if effective_owners.get(path) != entry.entry_id
            )
            if losing:
                entry.problems.append(
                    f"explicit commit {sha} loses effective path ownership: "
                    + ", ".join(losing)
                )
                continue
            valid_claimed.add(sha)
        if entry in wants_self:
            matched = _self_matches(
                repo, entry, work, ledger_rel, path_cache, effective_owners
            )
            if not matched:
                entry.problems.append(
                    "self matched no commit that changes the ledger and only "
                    "paths owned by this entry"
                )
            valid_claimed.update(matched)
        for sha in valid_claimed:
            claims.setdefault(sha, []).append(entry.entry_id)

    duplicate_claims = [
        {"sha": sha, "entries": ",".join(ids)}
        for sha, ids in claims.items()
        if len(ids) > 1
    ]
    mapped = {sha: ids[0] for sha, ids in claims.items()}
    unmapped = [c for c in work if c["sha"] not in mapped]
    invalid = [e for e in entries if e.problems]

    return {
        "ok": (
            not unmapped
            and not invalid
            and not duplicate_claims
            and not unowned
            and not ambiguous
            and not invalid_precedence
        ),
        "repo": str(repo),
        "ledger": str(ledger_path),
        "upstream_ref": upstream_ref,
        "fork_ref": fork_ref,
        "upstream_oid": upstream_oid,
        "fork_oid": fork_oid,
        "counts": {
            "fork_only_commits": len(work) + len(sync_merges),
            "work_commits": len(work),
            "sync_merges": len(sync_merges),
            "mapped": sum(1 for c in work if c["sha"] in mapped),
            "unmapped": len(unmapped),
            "entries": len(entries),
            "invalid_entries": len(invalid),
            "unowned_paths": len(unowned),
            "ambiguous_paths": len(ambiguous),
        },
        "unmapped_commits": unmapped,
        "invalid_entries": [
            {"id": e.entry_id, "line": e.line, "problems": e.problems} for e in invalid
        ],
        "duplicate_claims": duplicate_claims,
        "unowned_paths": unowned,
        "ambiguous_paths": ambiguous,
        "invalid_precedence": invalid_precedence,
        "path_owners": path_owners,
        "sync_merges": sync_merges,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="repository to inspect (default: this script's repo)",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="ledger path (default: <repo>/docs/FORK_CHANGES.md)",
    )
    parser.add_argument("--upstream-ref", default="upstream/main")
    parser.add_argument("--fork-ref", default="main")
    args = parser.parse_args(argv)

    repo = args.repo or Path(__file__).resolve().parents[2]
    if args.ledger is None:
        ledger = repo / "docs" / "FORK_CHANGES.md"
    else:
        ledger = args.ledger if args.ledger.is_absolute() else repo / args.ledger
    try:
        report = run_check(repo, ledger, args.upstream_ref, args.fork_ref)
    except (CheckerError, OSError, UnicodeError) as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, indent=2)
        print()
        return 2
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
