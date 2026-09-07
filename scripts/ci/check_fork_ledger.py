#!/usr/bin/env python3
"""Verify docs/FORK_CHANGES.md maps every fork-only commit to a ledger entry.

This is the deterministic post_verify gate behind the fork change ledger
(ARD-010 in the Central Command orchestration plan): the autonomous upstream
conflict resolver may only act on fork changes whose ownership is documented,
so an unmapped fork-only commit or a ledger entry missing a required field is
a hard failure.

Mapping methodology
-------------------
Fork-only commits are `git rev-list <upstream-ref>..<fork-ref>` evaluated in
the target repository. Merge commits with at least one parent reachable from
<upstream-ref> are classified as upstream sync merges (fork_sync_strategy=
merge keeps them intact by design) and are exempt from mapping; every other
commit in the range must be claimed by exactly one ledger entry.

A Commits field may contain explicit SHAs, `<sha>..<sha>` ranges, the token
`none` (path ownership only, no commit claim), and the token `self`. `self`
is component-scoped self-mapping, not a singular delivery-commit special
case. It maps any future work commit that changes the ledger and only files
owned by that entry. It stays narrow because the commit must touch the
ledger and every changed path must already be listed in Owned-Files. A
commit that also changes an unrelated path remains unmapped. Do not record
a branch-only SHA for those commits; that SHA changes on merge or squash.

Current-path ownership is the three-dot `upstream-ref...fork-ref` changed
path set (deletions included; parsed with `git diff -z` so quoted or unusual
paths are not guessed). Every current path must have exactly one owner, or
an explicit Path-Precedence row naming one of its owners. Missing and
ambiguous current paths fail closed. Precedence never invents an owner.
A path may be declared once; a repeated row fails closed whether the winner
is the same or conflicting, and is reported in invalid_precedence. The
winner is the token after the last `: <ID>`, so colon and quoted paths stay
intact.

The checker is standalone: stdlib only, no network, works from any checkout
via --repo, and pins the comparison to explicit refs so a foreign worktree or
CI clone gets the same answer as the primary checkout. Output is a single
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
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_SHA_RANGE = re.compile(r"\b(?P<a>[0-9a-f]{7,40})\.\.(?P<b>[0-9a-f]{7,40})\b")


class CheckerError(RuntimeError):
    """Environment/setup failure: the check could not be evaluated at all."""


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise CheckerError(
            f"git {' '.join(args)} could not be executed: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise CheckerError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip("\n")


@dataclass
class LedgerEntry:
    entry_id: str
    title: str
    line: int
    fields: dict[str, str] = field(default_factory=dict)
    owned_files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


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
            item = re.match(r"^\s+-\s+(?P<path>\S.*?)\s*$", raw)
            if item:
                current.owned_files.append(item.group("path"))
                continue
            in_owned_files = False  # fall through: this line may be a field
        fld = _FIELD_LINE.match(raw)
        if fld:
            key, value = fld.group("key"), fld.group("value").strip()
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
            if not entry.owned_files and not entry.fields[key]:
                entry.problems.append("Owned-Files list is empty")
        elif not entry.fields[key]:
            entry.problems.append(f"required field is empty: {key}")


_PRECEDENCE_HEADER = re.compile(r"^##\s+Path-Precedence\s*$")
_PRECEDENCE_LINE = re.compile(
    r"^-\s+(?P<path>.+):\s+(?P<id>[A-Z0-9][A-Z0-9-]*)\s*$"
)
_SPEC_SPLIT = re.compile(r"[\s,]+")


def parse_path_precedence(
    text: str,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Read `## Path-Precedence` rows of `- <path>: <ENTRY-ID>`.

    Split on the last `: <ID>` so a colon or quoted path is not truncated.
    The section is not an entry header; it only names the owner that wins a
    multi-owned path. A second declaration of the same parsed path fails
    closed, same winner or conflicting, and is not kept as a winner.
    """
    precedence: dict[str, str] = {}
    first_winner: dict[str, str] = {}
    duplicates: list[dict[str, str]] = []
    in_section = False
    for raw in text.splitlines():
        if _PRECEDENCE_HEADER.match(raw):
            in_section = True
            continue
        if in_section and raw.startswith("## "):
            break
        if not in_section:
            continue
        if not raw.strip():
            continue
        match = _PRECEDENCE_LINE.match(raw)
        if not match:
            continue
        path = match.group("path")
        winner = match.group("id")
        if path in first_winner:
            duplicates.append(
                {
                    "path": path,
                    "problem": (
                        "duplicate Path-Precedence declaration for "
                        f"{path}: already {first_winner[path]}, "
                        f"repeated as {winner}; declare each path once"
                    ),
                }
            )
            precedence.pop(path, None)
            continue
        first_winner[path] = winner
        precedence[path] = winner
    return precedence, duplicates


def audit_commit_tokens(entry: LedgerEntry) -> tuple[bool, bool]:
    """Allow only SHAs, ranges, `self`, and sole token `none`."""
    spec = entry.fields.get("Commits", "")
    residual = _SHA_RANGE.sub(" ", spec)
    residual = _SHA.sub(" ", residual)
    tokens = [token for token in _SPEC_SPLIT.split(residual) if token]
    has_self = False
    has_none = False
    for token in tokens:
        if token == "self":
            has_self = True
        elif token == "none":
            has_none = True
        else:
            entry.problems.append(f"unknown commit token: {token}")
    has_sha = bool(_SHA.search(spec) or _SHA_RANGE.search(spec))
    if has_none and (has_self or has_sha):
        entry.problems.append(
            "commit token none cannot be combined with other commit claims"
        )
    return has_self, has_none


def _read_ledger(ledger_path: Path) -> str:
    try:
        return ledger_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CheckerError(f"unreadable ledger: {ledger_path}: {exc}") from exc


def _ledger_repo_rel(repo: Path, ledger_path: Path) -> str:
    try:
        return ledger_path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return "docs/FORK_CHANGES.md"


def _parse_name_status_z(raw: str) -> list[str]:
    """Current paths from `git diff --name-status -z`. Renames use the new path."""
    parts = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(parts):
        if parts[index] == "":
            index += 1
            continue
        status = parts[index]
        if status[:1] in {"R", "C"}:
            if index + 2 >= len(parts):
                break
            paths.append(parts[index + 2])
            index += 3
            continue
        if index + 1 >= len(parts):
            break
        paths.append(parts[index + 1])
        index += 2
    return paths


def fork_changed_paths(repo: Path, upstream_ref: str, fork_ref: str) -> list[str]:
    raw = _git(repo, "diff", "--name-status", "-z", f"{upstream_ref}...{fork_ref}")
    seen: set[str] = set()
    ordered: list[str] = []
    for path in _parse_name_status_z(raw):
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def commit_changed_paths(repo: Path, sha: str) -> set[str]:
    raw = _git(
        repo,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        "--root",
        sha,
    )
    return {path for path in raw.split("\0") if path}


def resolve_entry_commits(repo: Path, entry: LedgerEntry) -> set[str]:
    """Expand the entry's Commits field into a set of full SHAs. Ranges
    (a..b) expand via rev-list; single SHAs resolve via rev-parse. An
    unresolvable reference is recorded as an entry problem, not a crash."""
    spec = entry.fields.get("Commits", "")
    shas: set[str] = set()
    consumed_ranges: list[tuple[str, str]] = []
    for m in _SHA_RANGE.finditer(spec):
        a, b = m.group("a"), m.group("b")
        consumed_ranges.append((a, b))
        try:
            out = _git(repo, "rev-list", f"{a}..{b}")
        except CheckerError:
            entry.problems.append(f"unresolvable commit range: {a}..{b}")
            continue
        shas.update(out.split())
    stripped = _SHA_RANGE.sub(" ", spec)
    for m in _SHA.finditer(stripped):
        try:
            shas.add(_git(repo, "rev-parse", f"{m.group(0)}^{{commit}}"))
        except CheckerError:
            entry.problems.append(f"unresolvable commit: {m.group(0)}")
    return shas


def classify_range(
    repo: Path, upstream_ref: str, fork_ref: str
) -> tuple[list[dict[str, str]], list[str]]:
    """Split upstream..fork into work commits (must be mapped) and upstream
    sync merges (exempt: a merge with a parent reachable from upstream)."""
    out = _git(repo, "rev-list", "--format=%H%x00%P%x00%s", "--no-commit-header",
               f"{upstream_ref}..{fork_ref}")
    work: list[dict[str, str]] = []
    sync_merges: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, parents_raw, subject = line.split("\x00", 2)
        parents = parents_raw.split()
        if len(parents) > 1:
            is_sync = False
            for parent in parents:
                try:
                    probe = subprocess.run(
                        ["git", "-C", str(repo), "merge-base", "--is-ancestor",
                         parent, upstream_ref],
                        capture_output=True,
                    )
                except OSError as exc:
                    raise CheckerError(
                        f"git merge-base --is-ancestor could not be executed: {exc}"
                    ) from exc
                if probe.returncode == 0:
                    is_sync = True
                    break
            if is_sync:
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
) -> set[str]:
    """Map any work commit that changes the ledger and only this entry's
    Owned-Files. Later patches on those same paths still match. A commit
    that also changes an unrelated path does not.
    """
    owned = set(entry.owned_files)
    matched: set[str] = set()
    for commit in work:
        sha = commit["sha"]
        if sha not in path_cache:
            path_cache[sha] = commit_changed_paths(repo, sha)
        paths = path_cache[sha]
        if ledger_rel in paths and paths <= owned:
            matched.add(sha)
    return matched


def _resolve_current_paths(
    changed: list[str],
    entries: list[LedgerEntry],
    precedence: dict[str, str],
) -> tuple[dict[str, str], list[str], list[dict[str, object]], list[dict[str, str]]]:
    owners: dict[str, list[str]] = {}
    for entry in entries:
        for path in entry.owned_files:
            ids = owners.setdefault(path, [])
            if entry.entry_id not in ids:
                ids.append(entry.entry_id)

    known_ids = {entry.entry_id for entry in entries}
    path_owners: dict[str, str] = {}
    unowned: list[str] = []
    ambiguous: list[dict[str, object]] = []
    invalid_precedence: list[dict[str, str]] = []

    for path, winner in precedence.items():
        listed = owners.get(path, [])
        if winner not in known_ids or winner not in listed or len(listed) < 2:
            invalid_precedence.append(
                {
                    "path": path,
                    "problem": (
                        "precedence must name one of at least two owners that "
                        f"list the path; got {winner} for {listed}"
                    ),
                }
            )

    for path in changed:
        listed = owners.get(path, [])
        if not listed:
            unowned.append(path)
            continue
        if len(listed) == 1:
            path_owners[path] = listed[0]
            continue
        winner = precedence.get(path)
        if winner in listed:
            path_owners[path] = winner
            continue
        ambiguous.append({"path": path, "entries": listed})

    return path_owners, unowned, ambiguous, invalid_precedence


def run_check(repo: Path, ledger_path: Path, upstream_ref: str, fork_ref: str) -> dict:
    if not ledger_path.is_file():
        raise CheckerError(f"ledger not found: {ledger_path}")
    for ref in (upstream_ref, fork_ref):
        _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")

    ledger_text = _read_ledger(ledger_path)
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
    work, sync_merges = classify_range(repo, upstream_ref, fork_ref)
    ledger_rel = _ledger_repo_rel(repo, ledger_path)
    path_cache: dict[str, set[str]] = {}
    claims: dict[str, list[str]] = {}
    for entry, claimed in zip(entries, explicit_claims, strict=True):
        claimed = set(claimed)
        if entry in wants_self:
            matched = _self_matches(repo, entry, work, ledger_rel, path_cache)
            if not matched:
                entry.problems.append(
                    "self matched no commit that changes the ledger and only "
                    "paths owned by this entry"
                )
            claimed.update(matched)
        for sha in claimed:
            claims.setdefault(sha, []).append(entry.entry_id)

    duplicate_claims = [
        {"sha": sha, "entries": ",".join(ids)}
        for sha, ids in claims.items()
        if len(ids) > 1
    ]
    mapped = {sha: ids[0] for sha, ids in claims.items()}
    unmapped = [c for c in work if c["sha"] not in mapped]
    invalid = [e for e in entries if e.problems]

    changed = fork_changed_paths(repo, upstream_ref, fork_ref)
    precedence, duplicate_precedence = parse_path_precedence(ledger_text)
    path_owners, unowned, ambiguous, invalid_precedence = _resolve_current_paths(
        changed, entries, precedence
    )
    invalid_precedence = duplicate_precedence + invalid_precedence

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
    parser.add_argument("--repo", type=Path, default=None,
                        help="repository to inspect (default: this script's repo)")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="ledger path (default: <repo>/docs/FORK_CHANGES.md)")
    parser.add_argument("--upstream-ref", default="upstream/main")
    parser.add_argument("--fork-ref", default="main")
    args = parser.parse_args(argv)

    repo = args.repo or Path(__file__).resolve().parents[2]
    ledger = args.ledger or repo / "docs" / "FORK_CHANGES.md"
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
