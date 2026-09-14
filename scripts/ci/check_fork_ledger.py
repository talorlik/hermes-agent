#!/usr/bin/env python3
"""Verify the fork change ledger maps every fork-only commit to an entry.

This is the deterministic post_verify gate behind the fork change ledger
(ARD-010 in the Central Command orchestration plan): the autonomous upstream
conflict resolver may only act on fork changes whose ownership is documented,
so an unmapped fork-only commit or a ledger entry missing a required field is
a hard failure.

Mapping methodology
-------------------
Both refs resolve once to immutable OIDs. Fork-only commits are
`git rev-list <upstream-oid>..<fork-oid>`. A merge is exempt as upstream sync
only when it has exactly two parents in fork/upstream order and its committed
tree equals the clean `git merge-tree --write-tree` result. G-FORK-LEDGER may
also authorize zero-tree history-reconciliation merges and their retired
second-parent-only ancestry; the remaining commits are mapped work. These
work, sync, and retired-history sets must form an exact disjoint partition.

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
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
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
_MAX_REVISION_DIGITS = 64


class CheckerError(RuntimeError):
    """Environment/setup failure: the check could not be evaluated at all."""


class RevisionKind(Enum):
    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class LedgerRevision:
    kind: RevisionKind
    value: int | None = None


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    joined = " ".join(args)
    try:
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), *args],
            capture_output=True,
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
        )
    except OSError as exc:
        raise CheckerError(f"git {joined} could not be executed: {exc}") from exc


def _git_bytes(repo: Path, *args: str) -> bytes:
    proc = _run_git(repo, *args)
    if proc.returncode != 0:
        joined = " ".join(args)
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CheckerError(f"git {joined} failed (exit {proc.returncode}): {detail}")
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
    """Allow only unique canonical SHA syntax, ranges, self, or sole none."""
    spec = entry.fields.get("Commits", "")
    tokens = [token for token in _SPEC_SPLIT.split(spec) if token]
    has_self = False
    has_none = False
    has_sha = False
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            entry.problems.append(f"duplicate commit declaration: {token}")
        seen.add(token)
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
    proc = _run_git(repo, "show", f"{fork_oid}:{ledger_rel}")
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
    repo = Path(os.path.abspath(repo))
    ledger_path = Path(os.path.abspath(ledger_path))
    try:
        ledger_rel = ledger_path.relative_to(repo)
    except ValueError as exc:
        raise CheckerError(
            f"ledger path must be inside repository: {ledger_path}"
        ) from exc
    candidate = repo
    for component in ledger_rel.parts:
        candidate /= component
        if candidate.is_symlink():
            raise CheckerError(
                "ledger path must be inside repository and cannot contain a "
                f"symlink component: {candidate}"
            )
    return ledger_rel.as_posix()


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
    proc = _run_git(repo, *args)
    return proc.returncode, proc.stdout.decode(
        "utf-8", errors="surrogateescape"
    ).strip()


def _is_canonical_commit_object(repo: Path, oid: str) -> bool:
    returncode, object_type = _run_claim_git(repo, "cat-file", "-t", oid)
    if returncode != 0 or object_type != "commit":
        return False
    returncode, resolved = _run_claim_git(
        repo, "rev-parse", "--verify", "--end-of-options", f"{oid}^{{commit}}"
    )
    return returncode == 0 and resolved == oid


def resolve_entry_commits(repo: Path, entry: LedgerEntry) -> set[str]:
    """Expand canonical commit-object claims and reject declaration overlap."""
    spec = entry.fields.get("Commits", "")
    shas: set[str] = set()
    for token in (part for part in _SPEC_SPLIT.split(spec) if part):
        range_match = _FULL_SHA_RANGE.fullmatch(token)
        if range_match:
            a, b = range_match.group("a"), range_match.group("b")
            invalid = [
                oid for oid in (a, b) if not _is_canonical_commit_object(repo, oid)
            ]
            if invalid:
                for oid in invalid:
                    entry.problems.append(
                        f"range endpoint is not a canonical commit object: {oid}"
                    )
                continue
            returncode, output = _run_claim_git(repo, "rev-list", f"{a}..{b}")
            if returncode != 0:
                entry.problems.append(f"unresolvable commit range: {token}")
                continue
            expanded = set(output.split())
            if not expanded:
                entry.problems.append(f"empty commit range: {token}")
                continue
            for sha in sorted(shas & expanded):
                entry.problems.append(
                    f"duplicate canonical commit declaration after expansion: {sha}"
                )
            shas.update(expanded)
            continue
        if not _FULL_SHA.fullmatch(token):
            continue
        if not _is_canonical_commit_object(repo, token):
            entry.problems.append(f"not a canonical commit object: {token}")
            continue
        if token in shas:
            entry.problems.append(f"duplicate canonical commit declaration: {token}")
        shas.add(token)
    return shas


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    proc = _run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    detail = proc.stderr.decode("utf-8", errors="replace").strip()
    raise CheckerError(
        f"git merge-base --is-ancestor failed (exit {proc.returncode}): {detail}"
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
    proc = _run_git(repo, "merge-tree", "--write-tree", first_parent, upstream_parent)
    if proc.returncode not in {0, 1}:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CheckerError(f"git merge-tree failed (exit {proc.returncode}): {detail}")
    lines = proc.stdout.decode("utf-8", errors="surrogateescape").splitlines()
    if not lines or not re.fullmatch(r"[0-9a-f]{40,64}", lines[0]):
        return False
    expected_tree = lines[0]
    actual_tree = _git(repo, "rev-parse", f"{sha}^{{tree}}")
    if proc.returncode == 0:
        return expected_tree == actual_tree

    # Conflict merges are byte-clean upstream syncs only when every
    # conflict was resolved in the first parent and the merge keeps those
    # exact pre-resolved blobs. No new resolution bytes can enter via the
    # otherwise exempt merge commit.
    stages: dict[str, dict[int, str]] = {}
    for line in lines[1:]:
        if not line:
            break
        match = re.fullmatch(
            r"[0-7]{6} ([0-9a-f]{40,64}) ([123])\t(.*)", line
        )
        if match is None:
            return False
        blob, stage, path = match.groups()
        stages.setdefault(path, {})[int(stage)] = blob
    if not stages or any(2 not in values for values in stages.values()):
        return False
    conflict_paths = set(stages)
    changed = {
        path
        for path in _git_bytes(
            repo, "diff", "--name-only", "-z", expected_tree, actual_tree
        ).decode("utf-8", errors="surrogateescape").split("\0")
        if path
    }
    if changed != conflict_paths:
        return False
    return all(
        _git(repo, "rev-parse", f"{sha}:{path}") == values[2]
        for path, values in stages.items()
    )


def _history_reconciliation_partition(
    repo: Path,
    entries: list[LedgerEntry],
    upstream_oid: str,
    fork_oid: str,
    ledger_rel: str,
    range_shas: set[str],
) -> tuple[set[str], list[dict[str, object]]]:
    declarers = [
        entry for entry in entries if "History-Reconciliations" in entry.fields
    ]
    for entry in declarers:
        if entry.entry_id != "G-FORK-LEDGER":
            entry.problems.append(
                "History-Reconciliations may be declared only by G-FORK-LEDGER"
            )
    ledger_declarers = [
        entry for entry in declarers if entry.entry_id == "G-FORK-LEDGER"
    ]
    if not ledger_declarers:
        return set(), []
    if len(ledger_declarers) != 1:
        for entry in ledger_declarers:
            entry.problems.append(
                "History-Reconciliations requires one unique G-FORK-LEDGER entry"
            )
        return set(), []

    entry = ledger_declarers[0]
    tokens = [
        token
        for token in _SPEC_SPLIT.split(entry.fields["History-Reconciliations"])
        if token
    ]
    if not tokens:
        entry.problems.append("History-Reconciliations cannot be empty")
        return set(), []
    if "none" in tokens:
        if len(tokens) != 1:
            entry.problems.append(
                "History-Reconciliations token none cannot be combined with object IDs"
            )
        return set(), []

    first_parent_range = set(
        _git(
            repo,
            "rev-list",
            "--first-parent",
            f"{upstream_oid}..{fork_oid}",
        ).splitlines()
    )
    first_parent_boundary = set(
        _git(repo, "rev-list", "--first-parent", upstream_oid).splitlines()
    )
    boundary_fingerprint = _entry_fingerprint_at(
        repo, upstream_oid, ledger_rel=ledger_rel, entry_id="G-FORK-LEDGER"
    )
    boundary_history = (
        dict(boundary_fingerprint[1]).get("History-Reconciliations", "")
        if boundary_fingerprint is not None
        else ""
    )
    inherited_tokens = {
        token for token in _SPEC_SPLIT.split(boundary_history) if token != "none"
    }
    upstream_roots = set(
        _git(repo, "rev-list", "--max-parents=0", upstream_oid).splitlines()
    )
    history: set[str] = set()
    declared_history: set[str] = set()
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            entry.problems.append(
                f"duplicate History-Reconciliations declaration: {token}"
            )
            continue
        seen.add(token)
        if not _FULL_SHA.fullmatch(token):
            entry.problems.append(
                "History-Reconciliations requires full 40-character object IDs: "
                f"{token}"
            )
            continue
        if not _is_canonical_commit_object(repo, token):
            entry.problems.append(
                f"History-Reconciliations is not a canonical commit object: {token}"
            )
            continue
        inherited = token in inherited_tokens
        if not inherited and token not in range_shas:
            entry.problems.append(
                f"History-Reconciliations commit is outside evaluated range: {token}"
            )
            continue
        required_first_parent = (
            first_parent_boundary if inherited else first_parent_range
        )
        if token not in required_first_parent:
            entry.problems.append(
                "History-Reconciliations commit must be on the fork first-parent chain: "
                f"{token}"
            )
            continue
        parents = _git(repo, "show", "-s", "--format=%P", token).split()
        if len(parents) != 2:
            entry.problems.append(
                f"History-Reconciliations commit must have exactly two parents: {token}"
            )
            continue
        first_parent, retired_tip = parents
        actual_tree = _git(repo, "show", "-s", "--format=%T", token)
        first_parent_tree = _git(repo, "show", "-s", "--format=%T", first_parent)
        if actual_tree != first_parent_tree:
            entry.problems.append(
                "History-Reconciliations commit must equal its raw first-parent tree: "
                f"{token}"
            )
            continue
        retired_roots = set(
            _git(repo, "rev-list", "--max-parents=0", retired_tip).splitlines()
        )
        if not retired_roots or not retired_roots <= upstream_roots:
            entry.problems.append(
                "History-Reconciliations retired parent must descend only from the "
                f"official upstream root: {token}"
            )
            continue
        retired_args = ["rev-list", retired_tip, f"^{first_parent}"]
        if not inherited:
            retired_args.append(f"^{upstream_oid}")
        retired = set(_git(repo, *retired_args).splitlines())
        if not retired:
            entry.problems.append(
                "History-Reconciliations second parent has no exclusive retired "
                f"commits: {token}"
            )
            continue
        candidate = {token, *retired}
        if not inherited:
            outside = candidate - range_shas
            if outside:
                entry.problems.append(
                    "History-Reconciliations history is outside evaluated range: "
                    + ", ".join(sorted(outside))
                )
                continue
        overlap = candidate & declared_history
        if overlap:
            entry.problems.append(
                "History-Reconciliations retired sets overlap: "
                + ", ".join(sorted(overlap))
            )
            continue
        declared_history.update(candidate)
        if not inherited:
            history.update(candidate)
        records.append({
            "sha": token,
            "retired_tip": retired_tip,
            "retired_commits": sorted(retired),
        })
    return history, records


def _partition_range(
    repo: Path,
    entries: list[LedgerEntry],
    upstream_oid: str,
    fork_oid: str,
    ledger_rel: str,
) -> tuple[list[dict[str, str]], list[str], set[str], list[dict[str, object]]]:
    out = _git(
        repo,
        "rev-list",
        "--format=%H%x00%P%x00%s",
        "--no-commit-header",
        f"{upstream_oid}..{fork_oid}",
    )
    rows: dict[str, tuple[list[str], str]] = {}
    for line in out.splitlines():
        if line.strip():
            sha, parents_raw, subject = line.split("\x00", 2)
            rows[sha] = (parents_raw.split(), subject)
    range_shas = set(rows)
    history, records = _history_reconciliation_partition(
        repo, entries, upstream_oid, fork_oid, ledger_rel, range_shas
    )
    sync_order = [
        sha
        for sha, (parents, _subject) in rows.items()
        if sha not in history
        and _is_clean_upstream_sync(repo, sha, parents, upstream_oid)
    ]
    sync = set(sync_order)
    work_shas = range_shas - sync - history
    if (
        sync & history
        or sync & work_shas
        or history & work_shas
        or sync | history | work_shas != range_shas
    ):
        raise CheckerError(
            "fork range classification is not an exact disjoint partition"
        )
    work = [
        {"sha": sha, "subject": subject}
        for sha, (_parents, subject) in rows.items()
        if sha in work_shas
    ]
    return work, sync_order, history, records


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


def _ledger_entry_at(
    repo: Path, oid: str, ledger_rel: str, entry_id: str
) -> LedgerEntry | None:
    proc = _run_git(repo, "show", f"{oid}:{ledger_rel}")
    if proc.returncode != 0:
        return None
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckerError(
            f"ledger is not valid UTF-8 at {oid}:{ledger_rel}: {exc}"
        ) from exc
    candidates = [item for item in parse_ledger(text) if item.entry_id == entry_id]
    return candidates[0] if len(candidates) == 1 else None


def _entry_fingerprint_at(
    repo: Path, oid: str, ledger_rel: str, entry_id: str
) -> tuple[str, tuple[tuple[str, str], ...], tuple[str, ...]] | None:
    candidate = _ledger_entry_at(repo, oid, ledger_rel, entry_id)
    if candidate is None:
        return None
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


def _ledger_revision(fields: dict[str, str]) -> LedgerRevision:
    if "Ledger-Revision" not in fields:
        return LedgerRevision(RevisionKind.ABSENT)
    raw = fields["Ledger-Revision"]
    if len(raw) > _MAX_REVISION_DIGITS or not re.fullmatch(r"[1-9][0-9]*", raw):
        return LedgerRevision(RevisionKind.INVALID)
    return LedgerRevision(RevisionKind.VALID, int(raw))


def _history_tokens(value: str) -> frozenset[str]:
    """Return the authorization set represented by one history field."""
    return frozenset(
        token for token in _SPEC_SPLIT.split(value) if token and token != "none"
    )


def _reachable_revision_high_waters(
    repo: Path, upstream_oid: str, fork_oid: str, ledger_rel: str
) -> dict[str, int | None]:
    boundary_fingerprint = _entry_fingerprint_at(
        repo, upstream_oid, ledger_rel, "G-FORK-LEDGER"
    )
    boundary_revision = (
        _ledger_revision(dict(boundary_fingerprint[1])).value
        if boundary_fingerprint is not None
        else None
    )
    rows = _git(
        repo,
        "rev-list",
        "--reverse",
        "--topo-order",
        "--parents",
        f"{upstream_oid}..{fork_oid}",
    ).splitlines()
    inclusive: dict[str, int | None] = {}
    prior: dict[str, int | None] = {}
    for row in rows:
        sha, *parents = row.split()
        prior_revision = boundary_revision
        for parent in parents:
            parent_revision = inclusive.get(parent)
            if parent_revision is not None:
                prior_revision = (
                    parent_revision
                    if prior_revision is None
                    else max(prior_revision, parent_revision)
                )
        prior[sha] = prior_revision
        fingerprint = _entry_fingerprint_at(repo, sha, ledger_rel, "G-FORK-LEDGER")
        current_revision = (
            _ledger_revision(dict(fingerprint[1])).value
            if fingerprint is not None
            else None
        )
        inclusive[sha] = (
            prior_revision
            if current_revision is None
            else (
                current_revision
                if prior_revision is None
                else max(prior_revision, current_revision)
            )
        )
    return prior


def _effective_owners_at(
    repo: Path, sha: str, ledger_rel: str
) -> tuple[dict[str, str], list[str]]:
    proc = _run_git(repo, "show", f"{sha}:{ledger_rel}")
    if proc.returncode != 0:
        return {}, [f"cannot read historical ledger at {sha}:{ledger_rel}"]
    try:
        ledger_text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckerError(
            f"ledger is not valid UTF-8 at {sha}:{ledger_rel}: {exc}"
        ) from exc
    historical_entries = parse_ledger(ledger_text)
    historical_precedence, precedence_problems = parse_path_precedence(ledger_text)
    historical_owners = _declared_path_owners(historical_entries)
    effective, invalid_owners = _resolve_all_declared_paths(
        historical_owners, historical_precedence
    )
    problems = [
        f"historical Path-Precedence in commit {sha}: {problem}"
        for problem in precedence_problems
    ]
    problems.extend(
        f"historical ownership resolution in commit {sha}: {problem}"
        for problem in invalid_owners
    )
    return effective, problems


def _validate_history_revision_transitions(
    repo: Path,
    entries: list[LedgerEntry],
    upstream_oid: str,
    fork_oid: str,
    ledger_rel: str,
) -> bool:
    """Validate sticky history authorization against persistent first-parent state."""
    current_entries = [entry for entry in entries if entry.entry_id == "G-FORK-LEDGER"]

    def add_problem(problem: str) -> None:
        nonlocal current_entries
        if not current_entries:
            synthetic = LedgerEntry(
                entry_id="G-FORK-LEDGER",
                title="missing activated ledger entry",
                line=0,
                problems=[],
            )
            entries.append(synthetic)
            current_entries = [synthetic]
        for target in current_entries:
            if problem not in target.problems:
                target.problems.append(problem)

    first_parent = _git(
        repo,
        "rev-list",
        "--reverse",
        "--first-parent",
        f"{upstream_oid}..{fork_oid}",
    ).splitlines()
    reachable_floors = _reachable_revision_high_waters(
        repo, upstream_oid, fork_oid, ledger_rel
    )
    boundary_fingerprint = _entry_fingerprint_at(
        repo, upstream_oid, ledger_rel, "G-FORK-LEDGER"
    )
    boundary_fields = (
        dict(boundary_fingerprint[1]) if boundary_fingerprint is not None else {}
    )
    boundary_revision_state = _ledger_revision(boundary_fields)
    boundary_revision = boundary_revision_state.value
    boundary_history = boundary_fields.get("History-Reconciliations")
    activated = boundary_history is not None
    seen_entry = boundary_fingerprint is not None
    previous_present = boundary_fingerprint is not None
    revision_floor = boundary_revision
    last_history = boundary_history

    if boundary_revision_state.kind is RevisionKind.INVALID:
        add_problem(
            "invalid Ledger-Revision at upstream boundary "
            f"{upstream_oid}: expected a positive decimal of at most "
            f"{_MAX_REVISION_DIGITS} digits"
        )

    for sha in first_parent:
        reachable_floor = reachable_floors.get(sha)
        if reachable_floor is not None:
            revision_floor = (
                reachable_floor
                if revision_floor is None
                else max(revision_floor, reachable_floor)
            )
        fingerprint = _entry_fingerprint_at(repo, sha, ledger_rel, "G-FORK-LEDGER")
        if fingerprint is None:
            if activated:
                add_problem(
                    f"G-FORK-LEDGER must not disappear after history activation: {sha}"
                )
            previous_present = False
            continue

        fields = dict(fingerprint[1])
        history = fields.get("History-Reconciliations")
        revision_state = _ledger_revision(fields)
        revision = revision_state.value

        if not activated and history is None:
            seen_entry = True
            previous_present = True
            if revision is not None:
                revision_floor = (
                    revision
                    if revision_floor is None
                    else max(revision_floor, revision)
                )
            continue

        if revision_state.kind is not RevisionKind.VALID:
            add_problem(
                "History-Reconciliations requires a positive decimal "
                f"Ledger-Revision of at most {_MAX_REVISION_DIGITS} digits in commit {sha}"
            )

        history_changed = not activated or history != last_history
        if not activated:
            if revision_floor is not None and (
                revision is None or revision <= revision_floor
            ):
                add_problem(
                    "History-Reconciliations activation must exceed the persistent "
                    f"Ledger-Revision floor {revision_floor} in commit {sha}"
                )
            activated = True
        else:
            if not previous_present:
                add_problem(
                    "G-FORK-LEDGER reappeared after a forbidden post-activation "
                    f"absence in commit {sha}"
                )
            if history is None:
                add_problem(
                    "History-Reconciliations must not be removed after activation: "
                    f"{sha}"
                )
            if revision_floor is not None and revision is not None:
                if revision < revision_floor:
                    add_problem(
                        "History-Reconciliations revision regressed below the "
                        f"persistent revision floor {revision_floor} in commit {sha}"
                    )
                if history_changed and revision <= revision_floor:
                    add_problem(
                        "History-Reconciliations change must strictly increase the "
                        f"persistent Ledger-Revision floor in commit {sha}"
                    )

        if history_changed:
            changed = commit_changed_paths(repo, sha)
            after_owned = set(fingerprint[2])
            if ledger_rel not in changed or not changed <= after_owned:
                add_problem(
                    "History-Reconciliations change requires a G-FORK-LEDGER "
                    f"self-owned commit: {sha}"
                )
            historical_effective_owners, ownership_problems = _effective_owners_at(
                repo, sha, ledger_rel
            )
            for problem in ownership_problems:
                add_problem(problem)
            if any(
                historical_effective_owners.get(path) != "G-FORK-LEDGER"
                for path in changed
            ):
                add_problem(
                    "History-Reconciliations change requires effective "
                    f"G-FORK-LEDGER ownership for every changed path: {sha}"
                )

        seen_entry = True
        previous_present = True
        last_history = history
        if revision is not None:
            revision_floor = (
                revision if revision_floor is None else max(revision_floor, revision)
            )

    first_parent_set = set(first_parent)
    reachable_rows = _git(
        repo,
        "rev-list",
        "--reverse",
        "--topo-order",
        "--parents",
        f"{upstream_oid}..{fork_oid}",
    ).splitlines()
    for row in reachable_rows:
        sha, *parents = row.split()
        historical_entry = _ledger_entry_at(repo, sha, ledger_rel, "G-FORK-LEDGER")
        if historical_entry is not None:
            for problem in historical_entry.problems:
                add_problem(f"malformed G-FORK-LEDGER in commit {sha}: {problem}")
            if _ledger_revision(historical_entry.fields).kind is RevisionKind.INVALID:
                add_problem(
                    f"invalid Ledger-Revision in commit {sha}: expected a positive "
                    f"decimal of at most {_MAX_REVISION_DIGITS} digits"
                )
        fingerprint = _entry_fingerprint_at(repo, sha, ledger_rel, "G-FORK-LEDGER")
        history = (
            dict(fingerprint[1]).get("History-Reconciliations")
            if fingerprint is not None
            else None
        )
        fields = dict(fingerprint[1]) if fingerprint is not None else {}
        revision_state = _ledger_revision(fields)
        revision = revision_state.value
        active_parent_tokens: list[frozenset[str]] = []
        for parent in parents:
            parent_fingerprint = _entry_fingerprint_at(
                repo, parent, ledger_rel, "G-FORK-LEDGER"
            )
            parent_history = (
                dict(parent_fingerprint[1]).get("History-Reconciliations")
                if parent_fingerprint is not None
                else None
            )
            if parent_history is None:
                continue
            active_parent_tokens.append(_history_tokens(parent_history))
            if fingerprint is None:
                add_problem(
                    f"G-FORK-LEDGER must not disappear after history activation: {sha}"
                )
            elif history is None:
                add_problem(
                    f"History-Reconciliations must not be removed after activation: {sha}"
                )
        divergent_parent_tokens = (
            len(parents) > 1
            and len(active_parent_tokens) > 1
            and len(set(active_parent_tokens)) > 1
        )
        if divergent_parent_tokens and fingerprint is not None and history is not None:
            parent_union = frozenset().union(*active_parent_tokens)
            if _history_tokens(history) != parent_union:
                add_problem(
                    "merge result must include every active parent token and no others: "
                    f"{sha}"
                )
            reachable_floor = reachable_floors.get(sha)
            if reachable_floor is not None and (
                revision is None or revision <= reachable_floor
            ):
                add_problem(
                    "merge reconciliation must strictly increase the full parent "
                    f"Ledger-Revision high-water {reachable_floor} in commit {sha}"
                )
            changed = commit_changed_paths(repo, sha)
            after_owned = set(fingerprint[2])
            if ledger_rel not in changed or not changed <= after_owned:
                add_problem(
                    "merge reconciliation requires a G-FORK-LEDGER self-owned "
                    f"commit: {sha}"
                )
            historical_effective_owners, ownership_problems = _effective_owners_at(
                repo, sha, ledger_rel
            )
            for problem in ownership_problems:
                add_problem(problem)
            if any(
                historical_effective_owners.get(path) != "G-FORK-LEDGER"
                for path in changed
            ):
                add_problem(
                    "merge reconciliation requires effective G-FORK-LEDGER "
                    f"ownership for every changed path: {sha}"
                )
        if sha in first_parent_set:
            continue
        first_parent_fingerprint = (
            _entry_fingerprint_at(repo, parents[0], ledger_rel, "G-FORK-LEDGER")
            if parents
            else boundary_fingerprint
        )
        before_history = (
            dict(first_parent_fingerprint[1]).get("History-Reconciliations")
            if first_parent_fingerprint is not None
            else None
        )
        history_was_active = before_history is not None
        if fingerprint is None:
            if history_was_active:
                add_problem(
                    f"G-FORK-LEDGER must not disappear after history activation: {sha}"
                )
            continue

        if history_was_active and history is None:
            add_problem(
                f"History-Reconciliations must not be removed after activation: {sha}"
            )
        history_changed = history != before_history and history is not None
        if history is not None and revision_state.kind is not RevisionKind.VALID:
            add_problem(
                "History-Reconciliations requires a positive decimal "
                f"Ledger-Revision of at most {_MAX_REVISION_DIGITS} digits in commit {sha}"
            )
        reachable_floor = reachable_floors.get(sha)
        if (
            history is not None
            and reachable_floor is not None
            and revision is not None
            and revision < reachable_floor
        ):
            add_problem(
                "History-Reconciliations revision regressed below the "
                f"persistent revision floor {reachable_floor} in commit {sha}"
            )
        if (
            history_changed
            and reachable_floor is not None
            and (revision is None or revision <= reachable_floor)
        ):
            add_problem(
                "History-Reconciliations change must strictly increase the "
                f"persistent Ledger-Revision floor in commit {sha}"
            )
        if not history_changed:
            continue
        changed = commit_changed_paths(repo, sha)
        after_owned = set(fingerprint[2])
        if ledger_rel not in changed or not changed <= after_owned:
            add_problem(
                "History-Reconciliations change requires a G-FORK-LEDGER "
                f"self-owned commit: {sha}"
            )
        historical_effective_owners, ownership_problems = _effective_owners_at(
            repo, sha, ledger_rel
        )
        for problem in ownership_problems:
            add_problem(problem)
        if any(
            historical_effective_owners.get(path) != "G-FORK-LEDGER" for path in changed
        ):
            add_problem(
                "History-Reconciliations change requires effective "
                f"G-FORK-LEDGER ownership for every changed path: {sha}"
            )

    return activated


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
    ledger_rel = _ledger_repo_rel(repo, ledger_path)
    repo = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    upstream_oid = _git(repo, "rev-parse", "--verify", f"{upstream_ref}^{{commit}}")
    fork_oid = _git(repo, "rev-parse", "--verify", f"{fork_ref}^{{commit}}")
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

    precedence, precedence_problems = parse_path_precedence(ledger_text)
    declared_owners = _declared_path_owners(entries)
    effective_owners, invalid_precedence = _resolve_all_declared_paths(
        declared_owners, precedence
    )
    history_activated = _validate_history_revision_transitions(
        repo, entries, upstream_oid, fork_oid, ledger_rel
    )

    explicit_claims = [resolve_entry_commits(repo, entry) for entry in entries]
    work, sync_merges, history_set, history_reconciliations = _partition_range(
        repo, entries, upstream_oid, fork_oid, ledger_rel
    )
    sync_set = set(sync_merges)
    work_shas = {commit["sha"] for commit in work}
    changed = fork_changed_paths(repo, upstream_oid, fork_oid)
    path_owners, unowned, ambiguous = _resolve_current_paths(
        changed, declared_owners, effective_owners
    )
    invalid_precedence = precedence_problems + invalid_precedence
    path_cache: dict[str, set[str]] = {}
    claims: dict[str, list[str]] = {}
    for entry, claimed in zip(entries, explicit_claims, strict=True):
        valid_claimed: set[str] = set()
        for sha in set(claimed):
            if history_activated and entry.entry_id == "G-FORK-LEDGER":
                parents = _git(repo, "show", "-s", "--format=%P", sha).split()
                before = (
                    _entry_fingerprint_at(repo, parents[0], ledger_rel, "G-FORK-LEDGER")
                    if parents
                    else None
                )
                after = _entry_fingerprint_at(repo, sha, ledger_rel, "G-FORK-LEDGER")
                if before is None or after is None:
                    entry.problems.append(
                        "explicit G-FORK-LEDGER claim crosses a missing "
                        f"G-FORK-LEDGER transition: {sha}"
                    )
                    continue
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
            boundary_fingerprint = (
                _entry_fingerprint_at(repo, upstream_oid, ledger_rel, "G-FORK-LEDGER")
                if entry.entry_id == "G-FORK-LEDGER"
                else None
            )
            current_fingerprint = (
                entry.title,
                tuple(sorted(entry.fields.items())),
                tuple(entry.owned_files),
            )
            inherited_unchanged = (
                boundary_fingerprint is not None
                and boundary_fingerprint == current_fingerprint
                and "History-Reconciliations" in entry.fields
            )
            if not matched and not inherited_unchanged:
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
            "fork_only_commits": len(work_shas | sync_set | history_set),
            "work_commits": len(work),
            "sync_merges": len(sync_set),
            "history_reconciliations": len(history_reconciliations),
            "retired_history_commits": len(history_set)
            - sum(
                1 for record in history_reconciliations if record["sha"] in history_set
            ),
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
        "history_reconciliations": history_reconciliations,
        "partition": {
            "work": sorted(work_shas),
            "sync": sorted(sync_set),
            "history": sorted(history_set),
        },
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
        ledger = repo / "website" / "docs" / "developer-guide" / "FORK_CHANGES.md"
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
