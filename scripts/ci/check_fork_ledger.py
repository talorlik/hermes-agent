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
tree equals the clean `git merge-tree --write-tree` result, or differs from that
result only by a file-class rule from `docs/fork-sync-classes.json` (version
1; a missing or wrong-version spec fails that check). Unspecified generated
files are taken from upstream. Pin-named locks stay on the existing
`docs/fork-npm-pins.json` replay: those locks and the manifests the spec names
may differ only when every floor and exact pin is met and no unpinned package
version moved. A lock no pin names is not a specified lock. Structured pins
replay declared keys only. Build output stops. Workflow files are held at the
fork blob under an active G-ORIGIN-WORKFLOWS entry. A dropped executable source
line stops. G-FORK-LEDGER may
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
`Cross-Owner-Commits` on G-FORK-LEDGER maps a published work commit whose paths
already have effective owners, but more than one of them. It cannot map an
unowned path, a single-owner commit, a ledger edit, or a commit another entry
already claims.
`Repaired-Conflict-Merges` on G-FORK-LEDGER maps a published conflict merge that
dropped upstream lines only when the checked tip still keeps every current
upstream line of each conflict path, in upstream order. Listing the SHA is not
enough. A tip that drops a line leaves the commit unmapped and names the path.
A later edit of the file is not a waiver: ownership attributes the edit, it does
not prove the upstream lines survived it. A drop that is intentional belongs in
`Upstream-Line-Deletions`, which names the path and the blob of the upstream
version whose lines were removed, so the deletion is audited text rather than an
inferred consequence of some other commit.
The SHA stays in `repaired_conflict_merges` so the old merge remains visible.

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
import difflib
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

_CI_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _CI_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_CI_DIR) not in sys.path:
    sys.path.insert(0, str(_CI_DIR))
from sync_class_gate import delta_allowed
from scripts.ci.sync_note_line import is_note_line

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



def _blob_text(repo: Path, entry: tuple[str, str, str] | None) -> str | None:
    if entry is None or entry[1] != "blob":
        return None
    return _git_bytes(repo, "cat-file", "-p", entry[2]).decode("utf-8", "surrogateescape")


def _line_records(text: str) -> list[str]:
    """Split only at LF and keep the terminator, matching git's line identity.

    str.splitlines() collapses a missing final newline, a CR-LF, and a unicode
    line separator into one line, so two blobs git treats as different compare
    equal. An unterminated final record stays unterminated.
    """
    records = text.split("\n")
    return [record + "\n" for record in records[:-1]] + (
        records[-1:] if records[-1] else []
    )


def _upstream_lines_kept(upstream: str, actual: str, fork: str) -> bool:
    """True when every upstream line survives and every extra line came from the fork."""
    if bytes((0,)) in upstream.encode("utf-8", "surrogateescape"):
        return actual == upstream
    if bytes((0,)) in actual.encode("utf-8", "surrogateescape"):
        return False
    required = _line_records(upstream)
    live = _line_records(actual)
    index = 0
    for line in required:
        while index < len(live) and live[index] != line:
            index += 1
        if index >= len(live):
            return False
        index += 1
    upstream_lines = set(required)
    fork_lines = set(_line_records(fork))
    return all(line in upstream_lines or line in fork_lines for line in live)


def _conflict_fit_allowed(
    repo: Path,
    first: tuple[str, str, str] | None,
    upstream: tuple[str, str, str] | None,
    actual: tuple[str, str, str] | None,
    *,
    reject_executable_drop: bool = False,
    path: str | None = None,
) -> bool:
    """Accept upstream text with fork insertions. Reject a wholesale fork file.

    When ``reject_executable_drop`` is set, a fork line may be absent only
    when ``is_note_line`` says it is a note. Every other dropped line stops
    the sync. ``path`` selects that rule's language.
    """
    if upstream is None:
        return actual == first
    if actual is None or actual[1] != "blob" or upstream[1] != "blob":
        return False
    if actual[0] != upstream[0]:
        return False
    upstream_text = _blob_text(repo, upstream) or ""
    actual_text = _blob_text(repo, actual) or ""
    fork_text = _blob_text(repo, first) or ""
    if not _upstream_lines_kept(upstream_text, actual_text, fork_text):
        return False
    if reject_executable_drop and _has_executable_drop(
        fork_text, actual_text, upstream_text, path
    ):
        return False
    return True


PIN_SPEC_PATH = "docs/fork-npm-pins.json"


def _is_generated_lock(path: str) -> bool:
    return path == "package-lock.json" or path.endswith("/package-lock.json")


def _tree_file_text(repo: Path, tree: str, path: str) -> str | None:
    proc = _run_git(repo, "show", f"{tree}:{path}")
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8")


def _parse_version(value: object) -> tuple[tuple[int, ...], str] | None:
    if not isinstance(value, str) or not value:
        return None
    main, sep, pre = value.partition("-")
    parts: list[int] = []
    for piece in main.split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    if not parts:
        return None
    return (tuple(parts), pre if sep else "")


def _version_at_least(value: object, floor: str) -> bool:
    parsed = _parse_version(value)
    required = _parse_version(floor)
    if parsed is None or required is None:
        return False
    length = max(len(parsed[0]), len(required[0]))
    left = parsed[0] + (0,) * (length - len(parsed[0]))
    right = required[0] + (0,) * (length - len(required[0]))
    if left != right:
        return left > right
    if parsed[1] == required[1] or parsed[1] == "":
        return True
    if required[1] == "":
        return False
    return _prerelease_key(parsed[1]) >= _prerelease_key(required[1])


def _prerelease_key(value: str) -> tuple[tuple[int, int | str], ...]:
    key: list[tuple[int, int | str]] = []
    for part in value.split("."):
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _lock_versions(lock: dict) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    packages = lock.get("packages")
    if isinstance(packages, dict):
        for path, meta in packages.items():
            if not isinstance(path, str) or not isinstance(meta, dict):
                continue
            name = meta.get("name")
            if not isinstance(name, str) or not name:
                if "node_modules/" not in path:
                    continue
                name = path.split("node_modules/")[-1]
            version = meta.get("version")
            if isinstance(version, str) and version:
                found.setdefault(name, set()).add(version)
    dependencies = lock.get("dependencies")
    if isinstance(dependencies, dict):
        _walk_dependency_versions(dependencies, found)
    return found


def _walk_dependency_versions(node: dict, found: dict[str, set[str]]) -> None:
    for name, meta in node.items():
        if not isinstance(name, str) or not isinstance(meta, dict):
            continue
        version = meta.get("version")
        if isinstance(version, str) and version:
            found.setdefault(name, set()).add(version)
        nested = meta.get("dependencies")
        if isinstance(nested, dict):
            _walk_dependency_versions(nested, found)


def _merged_lock_versions(repo: Path, tree: str) -> dict[str, set[str]] | None:
    found: dict[str, set[str]] = {}
    for path in _tree_entries(repo, tree):
        if not _is_generated_lock(path):
            continue
        text = _tree_file_text(repo, tree, path)
        if text is None:
            return None
        try:
            lock = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(lock, dict):
            return None
        for name, versions in _lock_versions(lock).items():
            found.setdefault(name, set()).update(versions)
    return found


def _manifests_meet_pins(repo: Path, tree: str, pins: list[dict]) -> bool:
    for pin in pins:
        target = pin.get("manifest") or "package.json"
        if not isinstance(target, str):
            return False
        if not (pin.get("override") or pin.get("field")):
            continue
        text = _tree_file_text(repo, tree, target)
        if text is None:
            return False
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(doc, dict):
            return False
        override = pin.get("override")
        if isinstance(override, str) and override:
            overrides = doc.get("overrides")
            current = overrides.get(override) if isinstance(overrides, dict) else None
            floor = pin.get("floor")
            resolved = pin.get("resolved")
            if isinstance(floor, str) and floor:
                if not _version_at_least(current, floor) or (
                    isinstance(resolved, str) and resolved and not _version_at_least(resolved, floor)
                ):
                    return False
            elif current != resolved:
                return False
        field = pin.get("field")
        if isinstance(field, str) and field:
            block = doc.get(field)
            current = block.get(pin.get("package")) if isinstance(block, dict) else None
            exact = pin.get("exact")
            if isinstance(exact, str) and exact:
                if current != exact:
                    return False
            else:
                floor = pin.get("floor")
                resolved = pin.get("resolved")
                if isinstance(floor, str) and floor:
                    if not _version_at_least(current, floor) or (
                        isinstance(resolved, str)
                        and resolved
                        and not _version_at_least(resolved, floor)
                    ):
                        return False
                elif current != resolved:
                    return False
    return True


def _locks_meet_pins(resolved: dict[str, set[str]], pins: list[dict]) -> bool:
    for pin in pins:
        package = pin.get("package")
        if not isinstance(package, str) or not package:
            return False
        versions = resolved.get(package) or set()
        exact = pin.get("exact")
        if isinstance(exact, str) and exact and versions != {exact}:
            return False
        floor = pin.get("floor")
        if isinstance(floor, str) and floor:
            if not versions or any(not _version_at_least(item, floor) for item in versions):
                return False
    return True


def _pin_has_bound(pin: dict) -> bool:
    floor = pin.get("floor")
    exact = pin.get("exact")
    return (isinstance(floor, str) and bool(floor)) or (isinstance(exact, str) and bool(exact))


def _package_name(path: str, meta: dict) -> str | None:
    name = meta.get("name")
    if isinstance(name, str) and name:
        return name
    marker = "node_modules/"
    if marker in path:
        tail = path.rsplit(marker, 1)[1]
        return tail or None
    return None


def _lock_installs(lock: dict) -> dict[str, tuple[str, str]]:
    """Map each install identity to its package name and version."""
    found: dict[str, tuple[str, str]] = {}
    packages = lock.get("packages")
    if isinstance(packages, dict):
        for path, meta in packages.items():
            if not isinstance(meta, dict):
                continue
            version = meta.get("version")
            if not isinstance(version, str):
                continue
            name = _package_name(str(path), meta)
            if name:
                found[f"packages:{path}"] = (name, version)
    def walk(node: object, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for name, meta in node.items():
            if not isinstance(meta, dict):
                continue
            version = meta.get("version")
            ident = f"{prefix}:{name}"
            if isinstance(version, str):
                found[ident] = (str(name), version)
            walk(meta.get("dependencies"), ident)
    walk(lock.get("dependencies"), "dependencies")
    return found


def _merged_lock_installs(repo: Path, tree: str) -> dict[str, tuple[str, str]] | None:
    found: dict[str, tuple[str, str]] = {}
    for path in _tree_entries(repo, tree):
        if not _is_generated_lock(path):
            continue
        text = _tree_file_text(repo, tree, path)
        if text is None:
            return None
        try:
            lock = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(lock, dict):
            return None
        for ident, pair in _lock_installs(lock).items():
            found[f"{path}:{ident}"] = pair
    return found


def _lowered_cleared_install(
    expected: dict[str, tuple[str, str]],
    actual: dict[str, tuple[str, str]],
    pins: list[dict],
) -> bool:
    """True when one cleared install is rewritten below its own pre-replay version."""
    floors = {
        pin["package"]: pin["floor"]
        for pin in pins
        if isinstance(pin.get("package"), str)
        and pin.get("package")
        and isinstance(pin.get("floor"), str)
        and pin.get("floor")
    }
    for ident, (name, before_version) in expected.items():
        floor = floors.get(name)
        if floor is None or not _version_at_least(before_version, floor):
            continue
        after = actual.get(ident)
        if after is None or after[0] != name or not _version_at_least(after[1], before_version):
            return True
    return False


def _json_object(repo: Path, tree: str, path: str) -> dict | None:
    text = _tree_file_text(repo, tree, path)
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def _manifest_delta_is_pinned(expected: dict, actual: dict, pins: list[dict]) -> bool:
    """True when a named manifest changes only the keys its pins name."""
    allowed_overrides = {
        pin["override"]
        for pin in pins
        if isinstance(pin.get("override"), str) and pin.get("override")
    }
    allowed_fields: dict[str, set[str]] = {}
    for pin in pins:
        field = pin.get("field")
        package = pin.get("package")
        if isinstance(field, str) and field and isinstance(package, str) and package:
            allowed_fields.setdefault(field, set()).add(package)
    if set(expected) != set(actual):
        return False
    for key, before in expected.items():
        after = actual[key]
        if before == after:
            continue
        if key == "overrides" and isinstance(before, dict) and isinstance(after, dict):
            changed = {
                name
                for name in set(before) | set(after)
                if (name in before) != (name in after) or before.get(name) != after.get(name)
            }
            if not changed <= allowed_overrides:
                return False
            continue
        if key in allowed_fields and isinstance(before, dict) and isinstance(after, dict):
            changed = {
                name
                for name in set(before) | set(after)
                if (name in before) != (name in after) or before.get(name) != after.get(name)
            }
            if not changed <= allowed_fields[key]:
                return False
            continue
        return False
    return True


def _changed_manifests_are_pin_only(
    repo: Path,
    expected_tree: str,
    actual_tree: str,
    pins: list[dict],
    changed: set[str],
) -> bool:
    by_manifest: dict[str, list[dict]] = {}
    for pin in pins:
        target = pin.get("manifest") or "package.json"
        if isinstance(target, str):
            by_manifest.setdefault(target, []).append(pin)
    for path, manifest_pins in by_manifest.items():
        if path not in changed:
            continue
        expected = _json_object(repo, expected_tree, path)
        actual = _json_object(repo, actual_tree, path)
        if expected is None or actual is None:
            return False
        if not _manifest_delta_is_pinned(expected, actual, manifest_pins):
            return False
    return True


def _unpinned_install_moved(
    expected: dict[str, tuple[str, str]],
    actual: dict[str, tuple[str, str]],
    pinned_names: set[str],
) -> bool:
    for ident in set(expected) | set(actual):
        before = expected.get(ident)
        after = actual.get(ident)
        if before == after:
            continue
        names = [pair[0] for pair in (before, after) if pair is not None]
        if any(name not in pinned_names for name in names):
            return True
    return False


def _pin_replay_delta_allowed(
    repo: Path,
    expected_tree: str,
    actual_tree: str,
    first_parent: str,
    already_allowed: set[str] | None = None,
) -> bool:
    """A sync may rewrite pin-named locks and pin manifests, and nothing else."""
    spec_text = _tree_file_text(repo, first_parent, PIN_SPEC_PATH)
    if not spec_text:
        return False
    try:
        spec = json.loads(spec_text)
    except json.JSONDecodeError:
        return False
    pins = spec.get("pins") if isinstance(spec, dict) else None
    if not isinstance(pins, list) or not pins or any(not isinstance(pin, dict) for pin in pins):
        return False
    if any(not _pin_has_bound(pin) for pin in pins):
        return False
    expected_entries = _tree_entries(repo, expected_tree)
    actual_entries = _tree_entries(repo, actual_tree)
    changed = {
        path
        for path in expected_entries.keys() | actual_entries.keys()
        if expected_entries.get(path) != actual_entries.get(path)
    }
    remaining = changed - (already_allowed or set())
    if not remaining or PIN_SPEC_PATH in changed:
        return False
    manifest_paths = {
        pin.get("manifest") or "package.json"
        for pin in pins
        if pin.get("override") or pin.get("field")
    }
    if any(not isinstance(path, str) for path in manifest_paths):
        return False
    named_locks = _named_lock_paths(pins)
    allowed_extra = {
        path
        for path in remaining
        if path in named_locks and _is_generated_lock(path)
    } | (manifest_paths & remaining)
    if remaining != allowed_extra:
        return False
    if not _manifests_meet_pins(repo, actual_tree, pins):
        return False
    expected_versions = _merged_lock_versions(repo, expected_tree)
    actual_versions = _merged_lock_versions(repo, actual_tree)
    if expected_versions is None or actual_versions is None:
        return False
    if not _locks_meet_pins(actual_versions, pins):
        return False
    if not _changed_manifests_are_pin_only(
        repo, expected_tree, actual_tree, pins, remaining
    ):
        return False
    expected_installs = _merged_lock_installs(repo, expected_tree)
    actual_installs = _merged_lock_installs(repo, actual_tree)
    if expected_installs is None or actual_installs is None:
        return False
    pinned_names = {
        pin["package"]
        for pin in pins
        if isinstance(pin.get("package"), str) and pin.get("package")
    }
    if _lowered_cleared_install(expected_installs, actual_installs, pins):
        return False
    if _unpinned_install_moved(expected_installs, actual_installs, pinned_names):
        return False
    names = set(expected_versions) | set(actual_versions)
    moved = {
        name
        for name in names
        if expected_versions.get(name, set()) != actual_versions.get(name, set())
    }
    return moved <= pinned_names


SYNC_CLASS_SPEC = "docs/fork-sync-classes.json"
_BUILDER_PIN_RE = re.compile(
    r"""^[ \t]*export[ \t]+const[ \t]+BUILDER_PIN[ \t]*=[ \t]*['"]([^'"]+)['"][ \t]*$"""
)
_BUILDER_PIN_DECLARATION = re.compile(
    r"^[ \t]*export[ \t]+const[ \t]+BUILDER_PIN\b"
)
# str.splitlines breaks on these. Inside a JS string they are not statement
# boundaries, so the scanner must not emit them as line breaks.
_STRING_LINE_SEPARATORS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_REGEX_KEYWORDS = frozenset(
    {
        "await",
        "case",
        "delete",
        "do",
        "else",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }
)
_OBJECT_BRACE_KEYWORDS = frozenset(
    {
        "await",
        "case",
        "delete",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }
)


def _js_executable_text(text: str, *, keep_templates: bool = False) -> str:
    """Drop comments outside strings so a commented pin cannot pass.

    A comment marker inside a quote stays in the literal. Template literals
    are dropped so their text cannot satisfy the contract.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in {"'", '"'}:
            out.append(char)
            index += 1
            while index < length:
                current = text[index]
                index += 1
                if current == "\\":
                    if index < length and text[index] in {"\n", "\r"}:
                        if (
                            text[index] == "\r"
                            and index + 1 < length
                            and text[index + 1] == "\n"
                        ):
                            index += 2
                        else:
                            index += 1
                        continue
                    out.append(current)
                    if index < length:
                        escaped = text[index]
                        out.append(
                            " " if escaped in _STRING_LINE_SEPARATORS else escaped
                        )
                        index += 1
                    continue
                out.append(" " if current in _STRING_LINE_SEPARATORS else current)
                if current == char:
                    break
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            out.append(_comment_separator(text[index:end]))
            index = end
            continue
        if text.startswith("//", index):
            index = _line_terminator_index(text, index + 2)
            continue
        if char == "/" and _slash_starts_regex("".join(out)):
            out.append("0")
            index = _skip_regex(text, index)
            continue
        if char == "`":
            if keep_templates:
                held: list[str] = []
                index = _skip_template(text, index + 1, held, keep_interpolation=True)
                out.append("`")
                out.append("`")
                if held:
                    out.append("(")
                    out.extend(held)
                    out.append(")")
            else:
                out.append("`")
                index = _skip_template(text, index + 1, out)
                out.append("`")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _line_terminator_index(text: str, index: int) -> int:
    """Index of the next JS line terminator, or the end of the text."""
    length = len(text)
    while index < length:
        if text[index] in "\n\r\u2028\u2029":
            return index
        index += 1
    return length


def _comment_separator(comment: str) -> str:
    """A block comment with a JS line terminator is a statement boundary."""
    if any(term in comment for term in "\n\r\u2028\u2029"):
        return "\n"
    return " "


def _trailing_token_is_member(text: str) -> bool:
    """True when the trailing identifier is a property name, not a keyword."""
    index = len(text) - 1
    while index >= 0 and (
        text[index].isalnum()
        or text[index] in "_$"
        or text[index] in _OTHER_ID_START
        or text[index] in _OTHER_ID_CONTINUE
        or unicodedata.category(text[index]) == "Pc"
    ):
        index -= 1
    while index >= 0 and _is_js_whitespace(text[index]):
        index -= 1
    return index >= 0 and text[index] in ".#"


def _trailing_identifier_start(text: str) -> int | None:
    """Start of the identifier ending at text, including a unicode escape."""
    index = len(text) - 1
    if index < 0:
        return None
    start: int | None = None
    while index >= 0:
        char = text[index]
        if (
            char.isalnum()
            or char in "_$"
            or char in _OTHER_ID_START
            or char in _OTHER_ID_CONTINUE
            or unicodedata.category(char) == "Pc"
            or _identifier_continuation_char(char)
        ):
            start = index
            index -= 1
            continue
        if char == "}" and _braced_unicode_escape_before(text[: index + 1]):
            marker = text.rfind("\\u{", 0, index + 1)
            if marker < 0:
                break
            start = marker
            index = marker - 1
            continue
        if (
            char == "\\"
            and text.startswith("\\u", index)
            and _identifier_escape_end(text, index) is not None
        ):
            start = index
            index -= 1
            continue
        break
    return start


def _trailing_identifier_is_member(text: str, start: int) -> bool:
    """True when the identifier at start is a property, not a keyword."""
    index = start - 1
    while index >= 0 and _is_js_whitespace(text[index]):
        index -= 1
    return index >= 0 and text[index] in ".#"


def _contextual_keyword(text: str, start: int, keywords: frozenset[str]) -> bool:
    """True when the identifier at start is a keyword in this position.

    of is contextual. It is a keyword only as the for-of operator. A binding
    or a value named of is an identifier.
    """
    decoded = _decoded_identifier(text, start)
    if decoded == "of":
        return _of_is_for_operator(text, start)
    if decoded == "await":
        return decoded in keywords and _await_is_reserved(text, start)
    if decoded == "yield":
        return decoded in keywords and _yield_is_reserved(text, start)
    return decoded in keywords



def _await_is_reserved(text: str, start: int) -> bool:
    """True when await is reserved, not an ordinary identifier.

    An ordinary function may bind await. An async function reserves it.
    """
    return _enclosing_function_kind(text, start) in {"async", "async-generator"}


def _yield_is_reserved(text: str, start: int) -> bool:
    """True when yield is reserved, not an ordinary identifier.

    An ordinary function may bind yield. A generator reserves it.
    """
    return _enclosing_function_kind(text, start) in {"generator", "async-generator"}


def _enclosing_function_kind(text: str, start: int) -> str:
    depth = 0
    index = start - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"', "`"}:
            index = _skip_quoted_backward(text, index)
            continue
        if char == "}":
            depth += 1
        elif char == "{":
            if depth == 0:
                kind = _function_kind_before(text, index)
                if kind != "block":
                    return kind
            else:
                depth -= 1
        index -= 1
    return "top"


def _function_kind_before(text: str, brace: int) -> str:
    index = brace - 1
    while index >= 0 and _is_js_whitespace(text[index]):
        index -= 1
    if index >= 1 and text[index - 1 : index + 1] == "=>":
        cursor = index - 2
        while cursor >= 0 and _is_js_whitespace(text[cursor]):
            cursor -= 1
        if cursor >= 0 and text[cursor] == ")":
            before_paren = _skip_closer_backward(text, cursor, "(", ")")
            if before_paren is not None:
                return _kind_before_params(text, before_paren + 1)
        return _kind_before_arrow_name(text, cursor)
    annotated = _rstrip_js(_without_return_annotation(text[: index + 1]))
    if annotated.endswith(")"):
        index = len(annotated) - 1
    if index < 0 or text[index] != ")":
        return "block"
    before_paren = _skip_closer_backward(text, index, "(", ")")
    if before_paren is None:
        return "block"
    return _kind_before_params(text, before_paren + 1)


def _kind_before_arrow_name(text: str, index: int) -> str:
    """Kind of an identifier arrow, such as async x =>."""
    if index < 0:
        return "block"
    found = _identifier_ending_at(text, index)
    if found is None:
        return "block"
    earlier = found[1]
    while earlier >= 0 and (
        _is_js_whitespace(text[earlier]) or text[earlier] == "*"
    ):
        earlier -= 1
    previous = _identifier_ending_at(text, earlier) if earlier >= 0 else None
    if previous is not None and previous[0] == "async":
        return "async"
    return "function"


def _arrow_follows_paren(text: str, paren: int) -> bool:
    """True when => follows the parameter list that opens at paren."""
    depth = 0
    index = paren
    length = len(text)
    while index < length:
        char = text[index]
        if char in {"'", '"', "`"}:
            index = _skip_quoted(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                nxt = index + 1
                while nxt < length and _is_js_whitespace(text[nxt]):
                    nxt += 1
                return text.startswith("=>", nxt)
        index += 1
    return False


def _kind_before_params(text: str, paren: int) -> str:
    words: list[str] = []
    cursor = paren - 1
    saw_star = False
    while len(words) < 3 and cursor >= 0:
        while cursor >= 0 and _is_js_whitespace(text[cursor]):
            cursor -= 1
        if cursor >= 0 and text[cursor] == "*" and not saw_star and not words:
            saw_star = True
            cursor -= 1
            continue
        found = _identifier_ending_at(text, cursor)
        if found is None:
            break
        words.append(found[0])
        cursor = found[1]
        while cursor >= 0 and _is_js_whitespace(text[cursor]):
            cursor -= 1
        if cursor >= 0 and text[cursor] == "*":
            saw_star = True
            cursor -= 1
    name = words[0] if words else ""
    if "function" in words:
        async_fn = "async" in words and name != "async"
    elif name == "async":
        async_fn = _arrow_follows_paren(text, paren)
    else:
        async_fn = "async" in words[1:]
    is_function = "function" in words
    if not is_function and not saw_star and not async_fn:
        if name == "async":
            return "function"
        return "block"
    if async_fn and saw_star:
        return "async-generator"
    if async_fn:
        return "async"
    if saw_star:
        return "generator"
    return "function"


def _const_is_type_parameter(text: str, index: int) -> bool:
    """True when const at index modifies a type parameter, not a binding.

    function f<const T extends string> does not bind a runtime value.
    """
    cursor = index - 1
    while cursor >= 0 and _is_js_whitespace(text[cursor]):
        cursor -= 1
    if cursor < 0 or text[cursor] not in "<,":
        return False
    opener = _type_parameter_opener(text, index)
    if opener is None:
        return False
    before = opener - 1
    while before >= 0 and _is_js_whitespace(text[before]):
        before -= 1
    found = _identifier_ending_at(text, before) if before >= 0 else None
    if found is None:
        if before >= 0 and text[before] == "*":
            earlier = before - 1
            while earlier >= 0 and _is_js_whitespace(text[earlier]):
                earlier -= 1
            previous = _identifier_ending_at(text, earlier) if earlier >= 0 else None
            return previous is not None and previous[0] == "function"
        return False
    earlier = before
    earlier = found[1]
    while earlier >= 0 and (
        _is_js_whitespace(text[earlier]) or text[earlier] == "*"
    ):
        earlier -= 1
    previous = _identifier_ending_at(text, earlier) if earlier >= 0 else None
    previous_name = previous[0] if previous else ""
    return found[0] in {"function", "class"} or previous_name in {"function", "class"}


def _type_parameter_opener(text: str, index: int) -> int | None:
    depth = 0
    cursor = index - 1
    while cursor >= 0:
        char = text[cursor]
        if char in {"'", '"', "`"}:
            cursor = _skip_quoted_backward(text, cursor)
            continue
        if char == ">" and cursor > 0 and text[cursor - 1] == "=":
            cursor -= 2
            continue
        if char == ">":
            depth += 1
        elif char == "<":
            if depth == 0:
                return cursor
            depth -= 1
        cursor -= 1
    return None

def _of_is_for_operator(text: str, start: int) -> bool:
    """True when the identifier at start is the for-of operator, not a value."""
    paren = _enclosing_for_paren(text, start)
    if paren is None:
        return False
    return _lhs_done_before_of(text, paren + 1, start)


def _enclosing_for_paren(text: str, start: int) -> int | None:
    """Index of the for ( that contains start, or None."""
    depth = 0
    index = start - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"', "`"}:
            index = _skip_quoted_backward(text, index)
            continue
        if char == ")":
            depth += 1
        elif char == "(":
            if depth == 0:
                return index if _paren_belongs_to_for(text, index) else None
            depth -= 1
        index -= 1
    return None


def _paren_belongs_to_for(text: str, paren: int) -> bool:
    index = paren - 1
    while index >= 0 and _is_js_whitespace(text[index]):
        index -= 1
    if index < 0:
        return False
    found = _identifier_ending_at(text, index)
    if found is None:
        return False
    start = found[1] + 1
    if _trailing_identifier_is_member(text[: index + 1], start):
        return False
    word = _decoded_identifier(text, start)
    if word == "for":
        return True
    if word != "await":
        return False
    before = found[1]
    while before >= 0 and _is_js_whitespace(text[before]):
        before -= 1
    if before < 0:
        return False
    earlier = _identifier_ending_at(text, before)
    if earlier is None:
        return False
    earlier_start = earlier[1] + 1
    if _trailing_identifier_is_member(text[: before + 1], earlier_start):
        return False
    return _decoded_identifier(text, earlier_start) == "for"


def _lhs_done_before_of(text: str, index: int, of_start: int) -> bool:
    """True when of_start is the operator after a completed for-of binding."""
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    lhs_complete = False
    expect_binding = False
    operator_seen = False
    binding_pattern = False
    paren_binding = False
    while index < of_start:
        if text.startswith("//", index):
            index = _line_terminator_index(text, index)
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = of_start if end < 0 else end + 2
            continue
        char = text[index]
        if char in {"'", '"', "`"}:
            index = _skip_quoted(text, index)
            continue
        if char == "(":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                paren_binding = True
            depth_paren += 1
        elif char == ")":
            if depth_paren:
                depth_paren -= 1
            if (
                paren_binding
                and depth_paren == 0
                and depth_bracket == 0
                and depth_brace == 0
            ):
                lhs_complete = True
                expect_binding = False
                paren_binding = False
        elif char in "{[":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                binding_pattern = True
            if char == "{":
                depth_brace += 1
            else:
                depth_bracket += 1
        elif char == "}":
            if depth_brace:
                depth_brace -= 1
            if (
                binding_pattern
                and depth_paren == 0
                and depth_bracket == 0
                and depth_brace == 0
            ):
                lhs_complete = True
                expect_binding = False
                binding_pattern = False
        elif char == "]":
            if depth_bracket:
                depth_bracket -= 1
            if (
                binding_pattern
                and depth_paren == 0
                and depth_bracket == 0
                and depth_brace == 0
            ):
                lhs_complete = True
                expect_binding = False
                binding_pattern = False
        elif char == ";" and depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
            return False
        elif (
            char == "="
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
            and not text.startswith(("==", "=>"), index)
            and (index == 0 or text[index - 1] not in "!<>=")
        ):
            return False
        elif (
            depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
            and _identifier_start(text, index)
        ):
            word = _decoded_identifier(text, index)
            end = _skip_identifier(text, index)
            if _trailing_identifier_is_member(text[:end], index):
                index = end
                continue
            if word in {"const", "let", "var"}:
                expect_binding = True
            elif expect_binding or not lhs_complete:
                lhs_complete = True
                expect_binding = False
            elif word == "of" and not operator_seen:
                operator_seen = True
            index = end
            continue
        index += 1
    return lhs_complete and not operator_seen


def _trailing_identifier_is_regex_keyword(text: str) -> bool:
    """True when the decoded identifier ending at text is a regex keyword.

    A raw suffix such as return is not a keyword when a unicode escape makes
    the identifier longer. The decoded form is the keyword only when the whole
    identifier decodes to it.
    """
    start = _trailing_identifier_start(text)
    if start is None or _trailing_identifier_is_member(text, start):
        return False
    return _contextual_keyword(text, start, _REGEX_KEYWORDS)


def _slash_starts_regex(emitted: str) -> bool:
    """True when a slash starts a regex, not a division."""
    if _asi_ends_before_slash(emitted):
        return True
    stripped = _rstrip_js(emitted)
    if not stripped:
        return True
    if stripped.endswith(("++", "--")):
        return False
    last = stripped[-1]
    if last == ")":
        return _paren_before_slash_is_control(stripped)
    if last == "}":
        if _braced_unicode_escape_before(stripped):
            return _trailing_identifier_is_regex_keyword(stripped)
        return not _trailing_brace_completes_expression(stripped)
    if last in "]\"'`":
        return False
    if (
        last.isalnum()
        or last in "_$"
        or last in _OTHER_ID_START
        or last in _OTHER_ID_CONTINUE
        or unicodedata.category(last) in {"Nl", "Pc"}
        or _identifier_continuation_char(last)
    ):
        return _trailing_identifier_is_regex_keyword(stripped)
    if last == "!":
        return not _postfix_non_null_before(stripped)
    if last == "." and _trailing_dot_completes_number(stripped):
        return False
    if last == ">" and _gt_closes_type_arguments(stripped):
        return False
    return True


def _trailing_dot_completes_number(text: str) -> bool:
    """True when a final dot ends a decimal integer literal, as in 1."""
    if len(text) < 2 or text[-1] != ".":
        return False
    index = len(text) - 2
    if not text[index].isdigit():
        return False
    while index >= 0 and text[index].isdigit():
        index -= 1
    return not (index >= 0 and text[index] == ".")


def _identifier_continuation_char(char: str) -> bool:
    """True for a combining mark, ZWNJ, or ZWJ."""
    return char in "\u200c\u200d" or unicodedata.category(char) in {"Mn", "Mc", "Me"}


def _braced_unicode_escape_before(text: str) -> bool:
    """True when text ends with a \\u{hex} identifier escape, not a block."""
    if not text.endswith("}"):
        return False
    marker = text.rfind("\\u{")
    if marker < 0:
        return False
    body = text[marker + 3 : -1]
    return bool(body) and all(char in "0123456789abcdefABCDEF" for char in body)


def _postfix_non_null_before(stripped: str) -> bool:
    """True when stripped ends with one or more postfix non-null assertions."""
    before = stripped
    while before.endswith("!"):
        candidate = before[:-1]
        while candidate and _is_js_whitespace(candidate[-1]):
            if candidate[-1] in "\n\r\u2028\u2029":
                return False
            candidate = candidate[:-1]
        if not candidate:
            return False
        previous = candidate[-1]
        if previous == "!":
            before = candidate
            continue
        if previous == ")":
            return not _paren_before_slash_is_control(candidate)
        if previous in "\"'`]":
            return True
        if previous == "}":
            if _braced_unicode_escape_before(candidate):
                return not _trailing_identifier_is_regex_keyword(candidate)
            return _trailing_brace_completes_expression(candidate)
        if (
            previous.isalnum()
            or previous in "_$"
            or previous in _OTHER_ID_START
            or previous in _OTHER_ID_CONTINUE
            or unicodedata.category(previous) == "Pc"
            or _identifier_continuation_char(previous)
        ):
            return not _trailing_identifier_is_regex_keyword(candidate)
        return False
    return False


_ASI_RESTRICTED_BEFORE_BRACE = frozenset(
    {"return", "throw", "break", "continue", "yield"}
)
_ASI_ENDS_STATEMENT = _ASI_RESTRICTED_BEFORE_BRACE | frozenset({"debugger"})


def _asi_ends_before_slash(emitted: str) -> bool:
    """True when a line terminator ends a statement before the slash."""
    index = len(emitted) - 1
    saw_terminator = False
    while index >= 0 and _is_js_whitespace(emitted[index]):
        if emitted[index] in "\n\r\u2028\u2029":
            saw_terminator = True
        index -= 1
    if not saw_terminator:
        return False
    prefix = emitted[: index + 1]
    if prefix.endswith(("++", "--")) or _postfix_non_null_before(prefix):
        return True
    start = _trailing_identifier_start(prefix)
    if start is None or _trailing_identifier_is_member(prefix, start):
        return False
    token = _decoded_identifier(prefix, start)
    if token == "yield":
        return _yield_is_reserved(prefix, start)
    if token in _ASI_ENDS_STATEMENT:
        return True
    return _keyword_before_label(prefix, start) is not None


def _keyword_before_label(prefix: str, label_start: int) -> str | None:
    """Return break or continue when the identifier is that statement's label.

    A line terminator between the keyword and the identifier means the
    identifier is a new statement, not a label.
    """
    cursor = label_start - 1
    while cursor >= 0 and _is_js_whitespace(prefix[cursor]):
        if prefix[cursor] in "\n\r\u2028\u2029":
            return None
        cursor -= 1
    if cursor < 0:
        return None
    start = _trailing_identifier_start(prefix[: cursor + 1])
    if start is None or _trailing_identifier_is_member(prefix, start):
        return None
    token = _decoded_identifier(prefix, start)
    if token in {"break", "continue"}:
        return token
    return None


def _postfix_before_terminator(prefix: str) -> bool:
    """True when ++, --, or a postfix ! is separated from the next token by a line terminator."""
    index = len(prefix) - 1
    saw_terminator = False
    while index >= 0 and _is_js_whitespace(prefix[index]):
        if prefix[index] in "\n\r\u2028\u2029":
            saw_terminator = True
        index -= 1
    if not saw_terminator or index < 0:
        return False
    head = prefix[: index + 1]
    if head.endswith(("++", "--")):
        return True
    return head.endswith("!") and _postfix_non_null_before(head)


def _restricted_keyword_across_terminator(prefix: str) -> bool:
    """True when ASI ends return, throw, break, continue, or yield before {."""
    index = len(prefix) - 1
    saw_terminator = False
    while index >= 0 and _is_js_whitespace(prefix[index]):
        if prefix[index] in "\n\r\u2028\u2029":
            saw_terminator = True
        index -= 1
    if not saw_terminator:
        return False
    start = _trailing_identifier_start(prefix[: index + 1])
    if start is None or _trailing_identifier_is_member(prefix, start):
        return False
    return _decoded_identifier(prefix, start) in _ASI_RESTRICTED_BEFORE_BRACE


_TYPE_POSITION_KEYWORDS = frozenset(
    {"as", "class", "extends", "implements", "interface", "function", "new", "satisfies", "type"}
)


def _skip_quoted_backward(text: str, index: int) -> int:
    quote = text[index]
    cursor = index - 1
    while cursor >= 0:
        if text[cursor] == quote:
            slashes = 0
            look = cursor - 1
            while look >= 0 and text[look] == "\\":
                slashes += 1
                look -= 1
            if slashes % 2 == 0:
                return cursor - 1
        cursor -= 1
    return -1


def _identifier_ending_at(text: str, index: int) -> tuple[str, int] | None:
    if index < 0 or index >= len(text):
        return None
    prefix = text[: index + 1]
    start = _trailing_identifier_start(prefix)
    if start is None:
        return None
    return prefix[start:], start - 1


def _skip_closer_backward(text: str, index: int, open_char: str, close_char: str) -> int | None:
    """Index before the opener matching close_char at index."""
    depth = 0
    while index >= 0:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted_backward(text, index)
            continue
        if char == close_char:
            depth += 1
        elif char == open_char:
            depth -= 1
            if depth == 0:
                return index - 1
        index -= 1
    return None


def _name_before(text: str, index: int) -> int | None:
    """Index before the type name ending at index, including a qualified name."""
    while index >= 0 and _is_js_whitespace(text[index]):
        index -= 1
    if index < 0:
        return None
    if text[index] == "." or text[index] == "#":
        cursor = index - 1
        if cursor >= 0 and text[cursor] == "?":
            cursor -= 1
        return _name_before(text, cursor)
    if text[index] == "]":
        before = _skip_closer_backward(text, index, "[", "]")
        if before is None:
            return None
        return _name_before(text, before)
    if text[index] == ")":
        return _skip_closer_backward(text, index, "(", ")")
    found = _identifier_ending_at(text, index)
    if found is None:
        return None
    before = found[1]
    while before >= 0 and _is_js_whitespace(text[before]):
        before -= 1
    if before >= 0 and text[before] in ".#":
        return _name_before(text, before)
    return before


def _angle_span_has_assignment(text: str, lt_index: int) -> bool:
    """True when a bare = appears before the > matching lt_index.

    => is an arrow, not that assignment. A bare = cannot appear in type
    arguments, so the < is a comparison.
    """
    depth = 0
    index = lt_index
    length = len(text)
    while index < length:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted(text, index)
            continue
        if char == "`":
            end = text.find("`", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "=":
            if index + 1 < length and text[index + 1] == ">":
                index += 2
                continue
            return True
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth == 0:
                return False
        index += 1
    return False


def _opens_declaration_type_params(text: str, lt_index: int) -> bool:
    """True when < opens a function or class type-parameter list."""
    before = lt_index - 1
    while before >= 0 and _is_js_whitespace(text[before]):
        before -= 1
    found = _identifier_ending_at(text, before) if before >= 0 else None
    if found is None:
        return False
    earlier = found[1]
    while earlier >= 0 and (
        _is_js_whitespace(text[earlier]) or text[earlier] == "*"
    ):
        earlier -= 1
    previous = _identifier_ending_at(text, earlier) if earlier >= 0 else None
    previous_name = previous[0] if previous else ""
    return found[0] in {"function", "class"} or previous_name in {"function", "class"}


def _lt_in_type_position(text: str, lt_index: int) -> bool:
    """True when < at lt_index opens a type argument list, not a comparison.

    An identifier immediately before < is a generic instantiation or a type
    reference. A space before < keeps a comparison such as a < b. A bare =
    before the matching > is a comparison, not a type argument.
    """
    if _angle_span_has_assignment(text, lt_index) and not _opens_declaration_type_params(text, lt_index):
        return False
    if (
        lt_index > 0
        and not _is_js_whitespace(text[lt_index - 1])
        and _name_before(text, lt_index - 1) is not None
    ):
        return True
    cursor = _name_before(text, lt_index - 1)
    if cursor is None:
        return False
    return _type_position_at(text, cursor)


def _expression_colon(text: str, colon_index: int) -> bool:
    """True when : separates an expression or statement, not a type.

    A value-object property, a ternary else, a statement label, and a
    switch case do not put a later < in type position. A colon after a
    binding, parameter, or return type still does.
    """
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    pending = 1
    index = colon_index - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"', "`"}:
            index = _skip_quoted_backward(text, index)
            continue
        if char == ")":
            depth_paren += 1
        elif char == "(":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                return False
            depth_paren -= 1
        elif char == "]":
            depth_bracket += 1
        elif char == "[":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                return False
            depth_bracket -= 1
        elif char == "}":
            depth_brace += 1
        elif char == "{":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                if _brace_opens_object(text, index):
                    return True
                if _type_body_brace(text, index):
                    return False
                return _statement_colon(text, colon_index, index)
            depth_brace -= 1
        elif (
            char == ":"
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
        ):
            pending += 1
        elif (
            char == "?"
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
            and not _question_is_operator(text, index)
        ):
            if text[index + 1 : colon_index].strip() == "":
                index -= 1
                continue
            pending -= 1
            if pending == 0:
                return True
        elif (
            depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
            and (
                char == ";"
                or (
                    char in "\n\r\u2028\u2029"
                    and not _rhs_open(text, index)
                    and not _inside_unclosed_group(text, index)
                )
            )
        ):
            return _statement_colon(text, colon_index, index)
        index -= 1
    return _statement_colon(text, colon_index, -1)


def _type_body_brace(text: str, brace_index: int) -> bool:
    """True when { opens a type body, not a statement block."""
    index = brace_index - 1
    while index >= 0 and _is_js_whitespace(text[index]):
        index -= 1
    if index < 0:
        return False
    if text[index] == ":":
        return True
    found = _identifier_ending_at(text, index)
    return found is not None and found[0] in {"interface", "type", "extends", "implements"}


def _statement_colon(text: str, colon_index: int, boundary: int) -> bool:
    """True when : is a label or case colon after a statement boundary."""
    start = boundary + 1
    while start < colon_index and _is_js_whitespace(text[start]):
        start += 1
    end = start
    while end < colon_index and (text[end].isalnum() or text[end] in "_$"):
        end += 1
    if text[start:end] == "case":
        return True
    index = colon_index - 1
    while index > boundary and _is_js_whitespace(text[index]):
        index -= 1
    found = _identifier_ending_at(text, index) if index > boundary else None
    if found is None:
        return False
    before = found[1]
    while before > boundary and _is_js_whitespace(text[before]):
        before -= 1
    return before == boundary


def _type_position_at(text: str, cursor: int) -> bool:
    """True when cursor sits before a name that is in type position."""
    while cursor >= 0 and _is_js_whitespace(text[cursor]):
        cursor -= 1
    if cursor < 0:
        return False
    if text[cursor] == ":":
        return not _expression_colon(text, cursor)
    if text[cursor] == ",":
        return _type_position_at(text, _skip_type_backward(text, cursor - 1))
    keyword = _identifier_ending_at(text, cursor)
    return keyword is not None and keyword[0] in _TYPE_POSITION_KEYWORDS

def _skip_type_backward(text: str, index: int) -> int:
    """Index before the type ending at index, for an implements or extends list."""
    while index >= 0 and _is_js_whitespace(text[index]):
        index -= 1
    if index < 0:
        return -1
    if text[index] == ">":
        if index > 0 and text[index - 1] == "=":
            return index
        matched = _matching_lt_backward(text, index)
        if matched < 0:
            return -1
        index = matched - 1
    if index < 0:
        return -1
    before = _name_before(text, index)
    return -1 if before is None else before

def _matching_lt_backward(text: str, index: int) -> int:
    """Index of the < matching the > at index. The > in => is not a closer."""
    depth = 0
    while index >= 0:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted_backward(text, index)
            continue
        if char == ">":
            if index > 0 and text[index - 1] == "=":
                index -= 2
                continue
            depth += 1
        elif char == "<":
            depth -= 1
            if depth == 0:
                return index
        index -= 1
    return -1

def _gt_closes_type_arguments(prefix: str) -> bool:
    """True when prefix ends with a > that closes a type argument list."""
    if not prefix or prefix[-1] != ">":
        return False
    index = len(prefix) - 1
    depth = 0
    while index >= 0:
        char = prefix[index]
        if char in "'\"":
            index = _skip_quoted_backward(prefix, index)
            continue
        if char == "`":
            previous = prefix.rfind("`", 0, index)
            index = -1 if previous < 0 else previous - 1
            continue
        if char == ">":
            # The > in => is an arrow, not a type-argument closer.
            if index > 0 and prefix[index - 1] == "=":
                index -= 2
                continue
            depth += 1
        elif char == "<":
            depth -= 1
            if depth == 0:
                return _lt_in_type_position(prefix, index)
        index -= 1
    return False


def _brace_opens_object(text: str, brace_index: int) -> bool:
    """True when { at brace_index opens an object, not a block."""
    raw = text[:brace_index]
    if _restricted_keyword_across_terminator(raw):
        return False
    if _postfix_before_terminator(raw):
        return False
    prefix = _rstrip_js(raw)
    if not prefix or prefix.endswith("=>"):
        return False
    if prefix[-1] == ":":
        return _colon_opens_object(text, len(prefix) - 1)
    annotated = _without_return_annotation(prefix)
    if annotated != prefix and _rstrip_js(annotated).endswith(")"):
        return False
    if prefix[-1] == ">":
        return not _gt_closes_type_arguments(prefix)
    if prefix[-1] in "=([{,?!~+-*/%&|^<":
        return True
    start = _trailing_identifier_start(prefix)
    if start is None or _trailing_identifier_is_member(prefix, start):
        return False
    return _contextual_keyword(prefix, start, _OBJECT_BRACE_KEYWORDS)


def _trailing_brace_is_object(emitted: str) -> bool:
    """True when a trailing } closed an object literal."""
    text = _rstrip_js(emitted)
    if not text or text[-1] != "}":
        return False
    depth = 0
    index = len(text) - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"'}:
            quote = char
            index -= 1
            while index >= 0:
                if text[index] == quote and (index == 0 or text[index - 1] != "\\"):
                    break
                index -= 1
            index -= 1
            continue
        if char == "}":
            depth += 1
        elif char == "{":
            depth -= 1
            if depth == 0:
                return _brace_opens_object(text, index)
        index -= 1
    return False


_EXPRESSION_KEYWORDS = frozenset(
    {
        "await",
        "case",
        "delete",
        "do",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }
)


def _open_brace_matching_end(text: str) -> int | None:
    """Index of the { that matches a trailing }, or None."""
    if not text or text[-1] != "}":
        return None
    depth = 0
    index = len(text) - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"', "`"}:
            quote = char
            index -= 1
            while index >= 0:
                if text[index] == quote and (index == 0 or text[index - 1] != "\\"):
                    break
                index -= 1
            index -= 1
            continue
        if char == "}":
            depth += 1
        elif char == "{":
            depth -= 1
            if depth == 0:
                return index
        index -= 1
    return None


def _question_is_operator(text: str, index: int) -> bool:
    """True when ? is optional chaining or nullish coalescing, not a ternary."""
    if index + 1 < len(text) and text[index + 1] in ".?":
        return True
    return index > 0 and text[index - 1] == "?"


def _colon_opens_object(text: str, colon_index: int) -> bool:
    """True when : introduces an object, not a labeled statement.

    An unmatched ? owns this colon across a newline. A ? that already has
    its : does not, so a later label stays a label.
    """
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    pending = 1
    index = colon_index - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"', "`"}:
            quote = char
            index -= 1
            while index >= 0:
                if text[index] == quote and (index == 0 or text[index - 1] != "\\"):
                    break
                index -= 1
            index -= 1
            continue
        if char == ")":
            depth_paren += 1
        elif char == "(":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                return True
            depth_paren -= 1
        elif char == "]":
            depth_bracket += 1
        elif char == "[":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                return True
            depth_bracket -= 1
        elif char == "}":
            depth_brace += 1
        elif char == "{":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                return _brace_opens_object(text, index)
            depth_brace -= 1
        elif (
            char == ":"
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
        ):
            pending += 1
        elif (
            char == "?"
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
            and not _question_is_operator(text, index)
        ):
            pending -= 1
            if pending == 0:
                return True
        elif char == ";" and depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
            return False
        index -= 1
    return False


def _matching_open_paren(text: str) -> int | None:
    """Index of the ( that matches a trailing ), or None."""
    if not text or text[-1] != ")":
        return None
    depth = 0
    index = len(text) - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"', "`"}:
            quote = char
            index -= 1
            while index >= 0:
                if text[index] == quote and (index == 0 or text[index - 1] != "\\"):
                    break
                index -= 1
            index -= 1
            continue
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                return index
        index -= 1
    return None


def _closes_parameter_list(before: str) -> bool:
    """True when a trailing ) closes a function parameter list, not a type."""
    open_index = _matching_open_paren(before)
    if open_index is None:
        return False
    if _token_before(before, open_index) in {
        "asserts",
        "infer",
        "keyof",
        "readonly",
        "typeof",
        "unique",
    }:
        return False
    index = open_index - 1
    while index >= 0 and _is_js_whitespace(before[index]):
        index -= 1
    if index < 0:
        return False
    return before[index].isalnum() or before[index] in "_$>*" or _identifier_continuation_char(before[index]) or _braced_unicode_escape_before(before[: index + 1])


def _skip_matched_closer(cursor: str) -> str | None:
    """Drop a trailing [], (), or <> group, including one nested in the heritage."""
    if not cursor or cursor[-1] not in "])>":
        return None
    pairs = {"]": "[", ")": "(", ">": "<"}
    close = cursor[-1]
    open_char = pairs[close]
    depth = 0
    index = len(cursor) - 1
    while index >= 0:
        char = cursor[index]
        if char in {"'", '"', "`"}:
            quote = char
            index -= 1
            while index >= 0:
                if cursor[index] == quote and (index == 0 or cursor[index - 1] != "\\"):
                    break
                index -= 1
            index -= 1
            continue
        if char == ">" and close == ">" and index > 0 and cursor[index - 1] == "=":
            index -= 2
            continue
        if char == close:
            depth += 1
        elif char == open_char:
            depth -= 1
            if depth == 0:
                return _rstrip_js(cursor[:index])
        index -= 1
    return None


def _keyword_ends(text: str, keyword: str) -> bool:
    """True when text ends with keyword and not a longer identifier."""
    if not text.endswith(keyword):
        return False
    start = len(text) - len(keyword)
    return start == 0 or not (
        text[start - 1].isalnum()
        or text[start - 1] in "_$"
        or unicodedata.category(text[start - 1]) == "Pc"
    )


def _opening_template_before(text: str) -> int | None:
    """Index of the opening backtick of a template that ends this text.

    A tagged template in class heritage is part of the heritage, not the end
    of the class. An interpolation brace does not hide the opening backtick.
    """
    if not text.endswith("`"):
        return None
    index = len(text) - 2
    depth = 0
    while index >= 0:
        char = text[index]
        if char == "`" and depth == 0:
            slashes = 0
            look = index - 1
            while look >= 0 and text[look] == "\\":
                slashes += 1
                look -= 1
            if slashes % 2 == 0:
                return index
        if char == "}" and depth == 0:
            depth += 1
        elif char == "{" and depth and index > 0 and text[index - 1] == "$":
            depth -= 1
            index -= 1
        index -= 1
    return None


def _without_class_heritage(prefix: str) -> str:
    """Drop ``extends Type`` so the class keyword still owns the body.

    The type may be a member expression: dotted, computed, generic, or
    parenthesized. A following slash is still division.
    """
    stripped = _rstrip_js(prefix)
    cursor = stripped
    while cursor:
        skipped = _skip_matched_closer(cursor)
        if skipped is not None:
            cursor = skipped
            continue
        if cursor.endswith("}") and not _braced_unicode_escape_before(cursor):
            brace = _open_brace_matching_end(cursor)
            if brace is None:
                return cursor
            cursor = _rstrip_js(cursor[:brace])
            continue
        if cursor.endswith((".", ",", "#")):
            cursor = _rstrip_js(cursor[:-1])
            if cursor.endswith("?"):
                cursor = _rstrip_js(cursor[:-1])
            continue
        if cursor.endswith("`"):
            opened = _opening_template_before(cursor)
            if opened is None:
                return cursor
            cursor = _rstrip_js(cursor[:opened])
            continue
        token = _token_before(cursor, len(cursor))
        if not token:
            return cursor
        before_token = _rstrip_js(cursor[: len(cursor) - len(token)])
        if before_token.endswith((".", "?", "[", "<", "(", ",", "#")):
            if token in {"function", "class"} and before_token.endswith(("[", "(", ",")):
                return cursor
            cursor = before_token
            continue
        if token in {"extends", "implements"}:
            cursor = before_token
            continue
        if _keyword_ends(before_token, "class"):
            cursor = before_token
            continue
        if token == "new" or _keyword_ends(before_token, "new"):
            cursor = before_token
            continue
        for keyword in ("implements", "extends"):
            if before_token.endswith(keyword):
                cursor = _rstrip_js(before_token[: -len(keyword)])
                break
        else:
            return cursor
        continue
    return stripped


def _without_return_annotation(prefix: str) -> str:
    """Drop a trailing ``: Type`` that follows a parameter list.

    An arrow function keeps its annotation. Only a suffix after ``)`` is a
    return type, so the function keyword stays at the same index.
    """
    cursor = _rstrip_js(prefix)
    if cursor.endswith("=>"):
        return prefix
    depth_angle = 0
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    index = len(cursor) - 1
    while index >= 0:
        if index > 0 and cursor[index - 1 : index + 1] == "=>":
            index -= 2
            continue
        char = cursor[index]
        if char in {"'", '"', "`"}:
            quote = char
            index -= 1
            while index >= 0 and not (
                cursor[index] == quote and (index == 0 or cursor[index - 1] != "\\")
            ):
                index -= 1
            index -= 1
            continue
        if char == ">":
            depth_angle += 1
        elif char == "<":
            if depth_angle == 0:
                return prefix
            depth_angle -= 1
        elif char == ")":
            depth_paren += 1
        elif char == "(":
            if depth_paren == 0:
                return prefix
            depth_paren -= 1
        elif char == "]":
            depth_bracket += 1
        elif char == "[":
            if depth_bracket == 0:
                return prefix
            depth_bracket -= 1
        elif char == "}":
            if (
                index != len(cursor) - 1
                and depth_angle == depth_paren == depth_bracket == depth_brace == 0
            ):
                return prefix
            depth_brace += 1
        elif char == "{":
            if depth_brace == 0:
                return prefix
            depth_brace -= 1
        elif (
            char == ";"
            and depth_angle == depth_paren == depth_bracket == depth_brace == 0
        ):
            return prefix
        elif char == ":" and depth_angle == depth_paren == depth_bracket == depth_brace == 0:
            before = _rstrip_js(cursor[:index])
            if before.endswith(")") and _closes_parameter_list(before):
                return before
        index -= 1
    return prefix


def _function_or_class_at(prefix: str) -> int | None:
    """Index of the function or class keyword that owns the following body."""
    stripped = _without_return_annotation(_without_class_heritage(_rstrip_js(prefix)))
    while True:
        skipped = _skip_matched_closer(stripped)
        if skipped is None:
            break
        stripped = skipped
    if stripped.endswith("*"):
        stripped = _rstrip_js(stripped[:-1])
    token = _token_before(stripped, len(stripped))
    head = stripped
    if token and token not in {"function", "class"}:
        head = _rstrip_js(stripped[: len(stripped) - len(token)])
        if head.endswith("*"):
            head = _rstrip_js(head[:-1])
    if head.endswith("function"):
        return len(head) - len("function")
    if head.endswith("class"):
        return len(head) - len("class")
    return None


def _colon_introduces_expression(text: str, colon_index: int) -> bool:
    """True when : introduces a value, not a statement label.

    A ternary else and an object property, including a computed or quoted
    property, put the following function or class in expression position.
    A case, default, or identifier label does not. A type annotation does not.
    """
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    pending = 1
    index = colon_index - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"', "`"}:
            index = _skip_quoted_backward(text, index)
            continue
        if char == ")":
            depth_paren += 1
        elif char == "(":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                return False
            depth_paren -= 1
        elif char == "]":
            depth_bracket += 1
        elif char == "[":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                cursor = index - 1
                while cursor >= 0 and _is_js_whitespace(text[cursor]):
                    cursor -= 1
                return cursor >= 0 and text[cursor] in "{,"
            depth_bracket -= 1
        elif char == "}":
            depth_brace += 1
        elif char == "{":
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                return _brace_opens_object(text, index)
            depth_brace -= 1
        elif (
            char == ":"
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
        ):
            pending += 1
        elif (
            char == "?"
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
            and not _question_is_operator(text, index)
        ):
            if text[index + 1 : colon_index].strip() == "":
                index -= 1
                continue
            pending -= 1
            if pending == 0:
                return True
        elif (
            char == ";"
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
        ):
            return False
        index -= 1
    return False


def _keyword_in_expression_position(text: str, keyword_index: int) -> bool:
    """True when function or class at keyword_index is an expression."""
    raw = text[:keyword_index]
    if _restricted_keyword_across_terminator(raw):
        return False
    if _postfix_before_terminator(raw):
        return False
    prefix = _rstrip_js(raw)
    if not prefix:
        return False
    if prefix[-1] in ";{}":
        return False
    if prefix.endswith("=>"):
        return True
    if prefix[-1] == ":":
        return _colon_introduces_expression(text, len(prefix) - 1)
    if prefix[-1] in "=([{,?!~+-*/%&|^<>":
        return True
    start = _trailing_identifier_start(prefix)
    if start is None:
        return False
    return _contextual_keyword(prefix, start, _EXPRESSION_KEYWORDS)


def _function_or_class_expression(text: str, brace_index: int) -> bool:
    """True when { at brace_index is a function or class expression body."""
    prefix = _rstrip_js(text[:brace_index])
    if prefix.endswith(")"):
        open_paren = _matching_open_paren(prefix)
        if open_paren is None:
            return False
        prefix = _rstrip_js(prefix[:open_paren])
    keyword = _function_or_class_at(prefix)
    if keyword is None:
        return False
    before = _rstrip_inline_js(text[:keyword])
    if before.endswith("async"):
        keyword = len(before) - len("async")
    return _keyword_in_expression_position(text, keyword)


def _trailing_brace_completes_expression(emitted: str) -> bool:
    """True when a trailing } finished a value, so the next / is division."""
    text = _rstrip_js(emitted)
    brace = _open_brace_matching_end(text)
    if brace is None:
        return False
    if _brace_opens_object(text, brace):
        return True
    return _function_or_class_expression(text, brace)


def _token_before(text: str, index: int) -> str:
    """Raw identifier immediately before index, including escapes and continuations."""
    prefix = _rstrip_js(text[:index])
    start = _trailing_identifier_start(prefix)
    if start is None:
        return ""
    return prefix[start:]


def _decoded_token_before(text: str, index: int) -> str:
    """Decoded identifier immediately before index."""
    prefix = _rstrip_js(text[:index])
    start = _trailing_identifier_start(prefix)
    if start is None:
        return ""
    return _decoded_identifier(prefix, start)


def _paren_before_slash_is_control(emitted: str) -> bool:
    """True when a trailing ) closed if, while, for, or with, not a call."""
    text = _rstrip_js(emitted)
    if not text or text[-1] != ")":
        return False
    depth = 0
    index = len(text) - 1
    while index >= 0:
        char = text[index]
        if char in {"'", '"'}:
            quote = char
            index -= 1
            while index >= 0:
                if text[index] == quote and (index == 0 or text[index - 1] != "\\"):
                    break
                index -= 1
            index -= 1
            continue
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                raw = _token_before(text, index)
                token = _decoded_token_before(text, index)
                if token == "await":
                    before_await = _rstrip_js(text[:index])
                    if raw:
                        before_await = _rstrip_js(before_await[: -len(raw)])
                    if _decoded_token_before(before_await, len(before_await)) != "for":
                        return False
                    head = before_await
                    for_raw = _token_before(before_await, len(before_await))
                    if for_raw:
                        head = _rstrip_js(head[: -len(for_raw)])
                    return not head.endswith((".", "#"))
                if token not in {"if", "while", "for", "with"}:
                    return False
                before = _rstrip_js(text[:index])
                if raw:
                    before = _rstrip_js(before[: -len(raw)])
                return not before.endswith((".", "#"))
        index -= 1
    return False


def _skip_regex(text: str, index: int) -> int:
    """Skip a regex literal, including a backtick in its body."""
    length = len(text)
    index += 1
    in_class = False
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2 if index + 1 < length else 1
            continue
        if char in "\n\r\u2028\u2029":
            return index
        if char == "[" and not in_class:
            in_class = True
            index += 1
            continue
        if char == "]" and in_class:
            in_class = False
            index += 1
            continue
        if char == "/" and not in_class:
            index += 1
            while index < length and (text[index].isalnum() or text[index] in "_$"):
                index += 1
            return index
        index += 1
    return index


def _skip_template(
    text: str, index: int, out: list[str], *, keep_interpolation: bool = False
) -> int:
    """Skip a template body, including a nested template inside ${}.

    Newlines are kept so a later statement stays on its own line. Literal
    template text is not emitted. Interpolation is emitted only when
    keep_interpolation is set, because that code can bind BUILDER_PIN.
    """
    length = len(text)
    while index < length:
        if text.startswith("\\", index) and index + 1 < length:
            index += 2
            continue
        if text.startswith("${", index):
            index = _skip_interpolation(
                text, index + 2, out, keep_interpolation=keep_interpolation
            )
            continue
        char = text[index]
        if char == "`":
            return index + 1
        if char == "\n":
            out.append("\n")
        index += 1
    return index

def _skip_interpolation(
    text: str, index: int, out: list[str], *, keep_interpolation: bool = False
) -> int:
    """Skip a ${} body. A brace inside the body is not the interpolation end.

    Nested templates and strings are skipped so their braces and backticks
    cannot close the interpolation. Newlines are kept. When
    keep_interpolation is set, the body is emitted as code so a nested
    binding is checked. Literal template text, comments, and regex text
    are not emitted.
    """
    length = len(text)
    brace = 0
    # Interpolation is an expression. "=" puts a leading object, function,
    # or class in expression position without emitting a fake assignment.
    seen: list[str] = ["="]
    while index < length:
        if text.startswith("\\", index) and index + 1 < length:
            escape = text[index : index + 2]
            seen.append(escape)
            if keep_interpolation:
                out.append(escape)
            index += 2
            continue
        if text.startswith("${", index):
            index = _skip_interpolation(
                text, index + 2, out, keep_interpolation=keep_interpolation
            )
            seen.append(" ")
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                separator = _comment_separator(text[index:])
                if separator == "\n" or keep_interpolation:
                    out.append(separator)
                return length
            separator = _comment_separator(text[index:end])
            if separator == "\n" or keep_interpolation:
                out.append(separator)
            index = end + 2
            seen.append(separator)
            continue
        if text.startswith("//", index):
            index = _line_terminator_index(text, index + 2)
            continue
        char = text[index]
        if char == "/" and _slash_starts_regex("".join(seen)):
            index = _skip_regex(text, index)
            seen.append("0")
            if keep_interpolation:
                out.append("0")
            continue
        if char == "{":
            brace += 1
            seen.append(char)
            if keep_interpolation:
                out.append(char)
            index += 1
            continue
        if char == "}":
            if brace:
                brace -= 1
                seen.append(char)
                if keep_interpolation:
                    out.append(char)
                index += 1
                continue
            return index + 1
        if char == "`":
            if keep_interpolation:
                held: list[str] = []
                index = _skip_template(text, index + 1, held, keep_interpolation=True)
                out.append("`")
                out.append("`")
                if held:
                    out.append("(")
                    out.extend(held)
                    out.append(")")
            else:
                index = _skip_template(text, index + 1, out, keep_interpolation=False)
            seen.append("``")
            continue
        if char in {"'", '"'}:
            start = index
            index += 1
            while index < length:
                current = text[index]
                index += 1
                if current == "\\":
                    if index < length:
                        index += 1
                    continue
                if current == char:
                    break
            if keep_interpolation:
                out.append(text[start:index])
            seen.append(char + char)
            continue
        if char == "\n":
            out.append("\n")
        elif keep_interpolation:
            out.append(char)
        seen.append(char)
        index += 1
    return index


def _generic_call_lt(text: str, index: int) -> bool:
    """True when < opens a call's type arguments, not a comparison."""
    before = index - 1
    while before >= 0 and _is_js_whitespace(text[before]):
        before -= 1
    if before < 0:
        return False
    previous = text[before]
    if previous == "}" and _braced_unicode_escape_before(text[: before + 1]):
        return _gt_followed_by_call(text, index)
    if not (
        previous.isalnum()
        or previous in "_$])."
        or previous in _OTHER_ID_START
        or previous in _OTHER_ID_CONTINUE
        or unicodedata.category(previous) == "Pc"
        or _identifier_continuation_char(previous)
    ):
        return False
    return _gt_followed_by_call(text, index)

def _gt_followed_by_call(text: str, lt_index: int) -> bool:
    """True when the > matching lt_index is followed by a call parenthesis.

    A bare = cannot appear in type arguments, so that < is a comparison.
    => is an arrow, not that assignment.
    """
    depth = 0
    index = lt_index
    length = len(text)
    while index < length:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted(text, index)
            continue
        if char == "`":
            end = text.find("`", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "=":
            if index + 1 < length and text[index + 1] == ">":
                index += 2
                continue
            return False
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth == 0:
                cursor = _skip_ws(text, index + 1)
                return cursor < length and text[cursor] == "("
        index += 1
    return False


def _line_continues_expression(line: str) -> bool:
    """True when the next line can grammatically continue an assignment.

    A statement keyword or identifier starts a new statement. Automatic
    semicolon insertion completes the assignment in that case. An operator,
    a call, member access, or an infix keyword does not.
    """
    stripped = _lstrip_js(line)
    if not stripped:
        return False
    if stripped.startswith(("++", "--")):
        return False
    if stripped[0] in set("+-*/%.?&|^<>=,[(:") or stripped[0] == "`":
        return True
    if stripped.startswith(("!=", "!==")):
        return True
    if stripped[0] == "!":
        return _line_continues_expression(stripped[1:])
    end = _skip_identifier(stripped, 0)
    if end == 0:
        return False
    return _decoded_identifier(stripped, 0) in {"as", "in", "instanceof", "satisfies"}


def _pin_statements(text: str) -> list[tuple[str, bool]]:
    """Split executable text into statements.

    A newline after ``=`` stays in the same statement. A semicolon terminates
    the statement, so a later line cannot continue it. Splits happen outside
    quotes, so a declaration inside a string is not its own statement.
    """
    statements: list[tuple[str, bool]] = []
    current: list[str] = []
    index = 0
    length = len(text)

    def finish(terminated: bool) -> None:
        statement = "".join(current).strip()
        current.clear()
        if statement:
            statements.append((statement, terminated))
        elif terminated and statements and not statements[-1][1]:
            statements[-1] = (statements[-1][0], True)

    while index < length:
        char = text[index]
        if char in {"'", '"'}:
            start = index
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2 if index + 1 < length else 1
                    continue
                if text[index] == char:
                    index += 1
                    break
                index += 1
            current.append(text[start:index])
            continue
        if char == ";":
            finish(True)
            index += 1
            continue
        if char in "\r\n\u2028\u2029":
            if char == "\r" and index + 1 < length and text[index + 1] == "\n":
                index += 1
            if _rstrip_js("".join(current)).endswith("="):
                current.append(" ")
            else:
                finish(False)
            index += 1
            continue
        current.append(char)
        index += 1
    finish(False)
    return statements


def _template_touches_assignment(line: str) -> bool:
    """True when a backtick is part of the pin assignment, not a later statement."""
    if _BUILDER_PIN_RE.fullmatch(line):
        return False
    if re.match(r"^[ \t]*export[ \t]+const[ \t]+BUILDER_PIN\b", line) is None:
        return False
    return "`" in line


def _is_js_whitespace(char: str) -> bool:
    """True for ECMAScript WhiteSpace and line terminators."""
    if char in " \t\n\r\u000b\u000c\u2028\u2029\ufeff":
        return True
    return unicodedata.category(char) == "Zs"


def _rstrip_js(text: str) -> str:
    """Drop trailing ECMAScript whitespace, including BOM."""
    index = len(text)
    while index > 0 and _is_js_whitespace(text[index - 1]):
        index -= 1
    return text[:index]


def _rstrip_inline_js(text: str) -> str:
    """Drop trailing ECMAScript whitespace but keep a line terminator."""
    index = len(text)
    while (
        index > 0
        and _is_js_whitespace(text[index - 1])
        and text[index - 1] not in "\n\r\u2028\u2029"
    ):
        index -= 1
    return text[:index]


def _lstrip_js(text: str) -> str:
    """Drop leading ECMAScript whitespace, including BOM."""
    index = 0
    length = len(text)
    while index < length and _is_js_whitespace(text[index]):
        index += 1
    return text[index:]


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and _is_js_whitespace(text[index]):
        index += 1
    return index


def _braced_escape_before(text: str, index: int) -> bool:
    """True when index follows a \\u{hex} escape that continues an identifier."""
    if index <= 0 or text[index - 1] != "}":
        return False
    marker = text.rfind("\\u{", 0, index)
    if marker < 0:
        return False
    return _identifier_escape_end(text, marker) == index


def _whole_token(text: str, index: int, token: str) -> bool:
    end = index + len(token)
    if not text.startswith(token, index):
        return False
    if index > 0 and (
        _identifier_continue(text, index - 1) or _braced_escape_before(text, index)
    ):
        return False
    return end >= len(text) or not _identifier_continue(text, end)


def _declaration_after_export(text: str, index: int) -> int | None:
    """Index after BUILDER_PIN when export starts that declaration."""
    if not _whole_token(text, index, "export"):
        return None
    cursor = _skip_ws(text, index + len("export"))
    if not _whole_token(text, cursor, "const"):
        return None
    cursor = _skip_ws(text, cursor + len("const"))
    if not _whole_token(text, cursor, "BUILDER_PIN"):
        return None
    return cursor + len("BUILDER_PIN")


def _skip_quoted(text: str, index: int) -> int:
    quote = text[index]
    cursor = index + 1
    length = len(text)
    while cursor < length:
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == quote:
            return cursor + 1
        cursor += 1
    return length


def _simple_string_at(text: str, index: int) -> tuple[str, int] | None:
    """A quote-delimited literal with no escapes, or None."""
    if index >= len(text) or text[index] not in "'\"":
        return None
    quote = text[index]
    cursor = index + 1
    chars: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            return None
        if char == quote:
            return "".join(chars), cursor + 1
        chars.append(char)
        cursor += 1
    return None


_BINDING_KEYWORDS = ("const", "let", "var")


def _binding_keyword_at(text: str, index: int) -> str | None:
    for keyword in _BINDING_KEYWORDS:
        if not _whole_token(text, index, keyword):
            continue
        cursor = index - 1
        while cursor >= 0 and _is_js_whitespace(text[cursor]):
            cursor -= 1
        if cursor >= 0 and text[cursor] == ".":
            return None
        if keyword == "const" and _const_is_type_parameter(text, index):
            return None
        return keyword
    return None


_RHS_KEYWORDS = frozenset(
    {
        "as",
        "await",
        "delete",
        "in",
        "instanceof",
        "new",
        "satisfies",
        "typeof",
        "void",
        "yield",
    }
)


_PREFIX_UPDATE_KEYWORDS = frozenset({"await", "delete", "typeof", "void", "yield"})


def _keyword_operator_before(text: str, before: int) -> bool:
    """True when the identifier ending at before is a unary keyword operator.

    ++ or -- after that operator is prefix. A member such as obj.typeof is not.
    """
    found = _identifier_ending_at(text, before)
    if found is None:
        return False
    start = found[1] + 1
    if _trailing_identifier_is_member(text[: before + 1], start):
        return False
    return _decoded_identifier(text, start) in _PREFIX_UPDATE_KEYWORDS


def _postfix_update_at(text: str, cursor: int) -> bool:
    """True when cursor is the last character of postfix ++ or --."""
    if cursor < 1 or text[cursor] not in "+-" or text[cursor - 1] != text[cursor]:
        return False
    before = cursor - 2
    while before >= 0 and _is_js_whitespace(text[before]):
        before -= 1
    if before < 0:
        return False
    if _keyword_operator_before(text, before):
        return False
    previous = text[before]
    if (
        previous in "\"'`) ]"
        or previous.isalnum()
        or previous in "_$"
        or previous in _OTHER_ID_START
        or previous in _OTHER_ID_CONTINUE
        or unicodedata.category(previous) == "Pc"
    ):
        return True
    if _identifier_continuation_char(previous):
        return True
    if previous == "}":
        head = text[: before + 1]
        if _braced_unicode_escape_before(head):
            return True
        return _trailing_brace_completes_expression(head)
    return False


def _inside_unclosed_group(text: str, index: int) -> bool:
    """True when index sits inside an unclosed ( or [.

    ASI does not end a statement inside a parenthesized or bracketed group.
    """
    depth_paren = 0
    depth_bracket = 0
    cursor = index
    while cursor >= 0:
        char = text[cursor]
        if char in {"'", '"', "`"}:
            cursor = _skip_quoted_backward(text, cursor)
            continue
        if char == ")":
            depth_paren += 1
        elif char == "(":
            if depth_paren == 0:
                return True
            depth_paren -= 1
        elif char == "]":
            depth_bracket += 1
        elif char == "[":
            if depth_bracket == 0:
                return True
            depth_bracket -= 1
        cursor -= 1
    return False


def _rhs_open(text: str, index: int) -> bool:
    cursor = index - 1
    while cursor >= 0 and _is_js_whitespace(text[cursor]):
        cursor -= 1
    if cursor < 0:
        return False
    if _postfix_update_at(text, cursor) or (
        text[cursor] == "!" and _postfix_non_null_before(text[: cursor + 1])
    ):
        return False
    if text[cursor] in "+-*/%=<>&|^?:,.([{~!":
        return True
    start = _trailing_identifier_start(text[: cursor + 1])
    if start is None:
        return False
    word = _decoded_identifier(text, start)
    if word == "await" and not _await_is_reserved(text, start):
        return False
    if word == "yield" and not _yield_is_reserved(text, start):
        return False
    if word not in _RHS_KEYWORDS:
        return False
    cursor = start - 1
    while cursor >= 0 and _is_js_whitespace(text[cursor]):
        cursor -= 1
    return not (cursor >= 0 and text[cursor] == ".")


def _skip_group(text: str, index: int) -> int:
    depth = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted(text, index)
            continue
        if char == "`":
            end = text.find("`", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return length


def _postfix_newline_boundary(text: str, index: int) -> bool:
    """True when a line terminator closes a postfix expression.

    ++, --, or postfix ! before the terminator is a boundary even when the
    next line starts with an index. A terminator before postfix ! is also a
    boundary. Same-line indexing after those operators still continues.
    """
    cursor = index - 1
    while (
        cursor >= 0
        and text[cursor] not in "\n\r\u2028\u2029"
        and _is_js_whitespace(text[cursor])
    ):
        cursor -= 1
    if cursor >= 0 and (
        _postfix_update_at(text, cursor)
        or (text[cursor] == "!" and _postfix_non_null_before(text[: cursor + 1]))
    ):
        return True
    nxt = _skip_ws(text, index + 1)
    return nxt < len(text) and text[nxt] == "!" and not text.startswith(("!=", "!=="), nxt)


def _skip_initializer(text: str, index: int) -> int:
    length = len(text)
    depth_paren = depth_brace = depth_bracket = depth_angle = 0
    while index < length:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted(text, index)
            continue
        if char == "`":
            end = text.find("`", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "<" and (depth_angle or _generic_call_lt(text, index) or _lt_in_type_position(text, index)):
            depth_angle += 1
        elif char == ">" and depth_angle:
            if index > 0 and text[index - 1] == "=":
                index += 1
                continue
            depth_angle -= 1
        elif char == "(":
            depth_paren += 1
        elif char == ")":
            if depth_paren == 0:
                return index
            depth_paren -= 1
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            if depth_brace == 0:
                return index
            depth_brace -= 1
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            if depth_bracket == 0:
                return index
            depth_bracket -= 1
        elif (
            char in ",;"
            and depth_paren == depth_brace == depth_bracket == depth_angle == 0
        ):
            return index
        elif (
            char in "\n\r\u2028\u2029"
            and depth_paren == depth_brace == depth_bracket == depth_angle == 0
        ):
            if _postfix_newline_boundary(text, index):
                return index
            if not _rhs_open(text, index) and not _line_continues_expression(
                text[index + 1 :]
            ):
                return index
        index += 1
    return index

def _initializer_continues(text: str, index: int) -> bool:
    """True when text after a pin literal changes that literal's value."""
    cursor = index
    while True:
        nxt = _skip_ws(text, cursor)
        if nxt >= len(text):
            return False
        crossed = any(text[pos] in "\n\r\u2028\u2029" for pos in range(cursor, nxt))
        if text[nxt] == "!" and not text.startswith(("!=", "!=="), nxt):
            if crossed:
                return False
            cursor = nxt + 1
            continue
        if crossed and cursor != index:
            return False
        cursor = nxt
        break
    if text[cursor] in ",;}":
        return False
    return _line_continues_expression(text[cursor:])


def _skip_type_annotation(text: str, index: int) -> int:
    """Index of the token that ends a type annotation."""
    length = len(text)
    depth_angle = depth_paren = depth_brace = depth_bracket = 0
    while index < length:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted(text, index)
            continue
        if char == "`":
            end = text.find("`", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "<":
            depth_angle += 1
        elif char == ">" and depth_angle:
            if index > 0 and text[index - 1] == "=":
                index += 1
                continue
            depth_angle -= 1
        elif char == "(":
            depth_paren += 1
        elif char == ")":
            if depth_paren == 0:
                return index
            depth_paren -= 1
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            if depth_brace == 0:
                return index
            depth_brace -= 1
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            if depth_bracket == 0:
                return index
            depth_bracket -= 1
        elif (
            char in "=,;"
            and depth_angle == depth_paren == depth_brace == depth_bracket == 0
        ):
            if char == "=" and index + 1 < length and text[index + 1] == ">":
                index += 2
                continue
            return index
        index += 1
    return index

def _skip_until_pattern_separator(text: str, index: int, closer: str) -> int:
    length = len(text)
    depth_paren = depth_brace = depth_bracket = depth_angle = 0
    while index < length:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted(text, index)
            continue
        if char == "`":
            end = text.find("`", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "<" and (depth_angle or _generic_call_lt(text, index) or _lt_in_type_position(text, index)):
            depth_angle += 1
        elif char == ">" and depth_angle:
            if index > 0 and text[index - 1] == "=":
                index += 1
                continue
            depth_angle -= 1
        elif char == "(":
            depth_paren += 1
        elif char == ")":
            if depth_paren == 0 and closer == ")":
                return index
            if depth_paren:
                depth_paren -= 1
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            if depth_brace == 0 and closer == "}":
                return index
            if depth_brace:
                depth_brace -= 1
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            if depth_bracket == 0 and closer == "]":
                return index
            if depth_bracket:
                depth_bracket -= 1
        elif (
            char in ",;"
            and depth_paren == depth_brace == depth_bracket == depth_angle == 0
        ):
            return index
        index += 1
    return index


def _skip_pattern_default(text: str, index: int, closer: str) -> int:
    index = _skip_ws(text, index)
    if index >= len(text) or text[index] != "=" or text.startswith("=>", index):
        return index
    return _skip_until_pattern_separator(text, index + 1, closer)


def _binding_target(text: str, index: int) -> tuple[bool, int]:
    index = _skip_ws(text, index)
    if index >= len(text):
        return False, index
    if text[index] in "{[":
        return _pattern_binds_pin(text, index)
    if _pin_binding_at(text, index):
        return True, _skip_identifier(text, index)
    if _identifier_start(text, index):
        return False, _skip_identifier(text, index)
    return False, index


def _pattern_elements(
    text: str, index: int, closer: str, is_object: bool
) -> tuple[bool, int]:
    length = len(text)
    binds = False
    while index < length:
        index = _skip_ws(text, index)
        if index >= length:
            return binds, index
        if text[index] == closer:
            return binds, index + 1
        if text.startswith("...", index):
            nested, index = _binding_target(text, index + 3)
            binds = binds or nested
        elif is_object and text[index] == "[":
            index = _skip_group(text, index)
            index = _skip_ws(text, index)
            if index < length and text[index] == ":":
                nested, index = _binding_target(text, index + 1)
                binds = binds or nested
        elif is_object and text[index] in "'\"":
            index = _skip_quoted(text, index)
            index = _skip_ws(text, index)
            if index < length and text[index] == ":":
                nested, index = _binding_target(text, index + 1)
                binds = binds or nested
        elif text[index] in "{[":
            nested, index = _pattern_binds_pin(text, index)
            binds = binds or nested
        elif _identifier_start(text, index):
            name_is_pin = _pin_binding_at(text, index)
            index = _skip_identifier(text, index)
            looked = _skip_ws(text, index)
            if is_object and looked < length and text[looked] == ":":
                nested, index = _binding_target(text, looked + 1)
                binds = binds or nested
            else:
                if name_is_pin:
                    binds = True
                index = looked
        else:
            index += 1
            continue
        index = _skip_pattern_default(text, index, closer)
        index = _skip_ws(text, index)
        if index < length and text[index] == ",":
            index += 1
    return binds, index


def _pattern_binds_pin(text: str, index: int) -> tuple[bool, int]:
    """True when the object or array pattern at index binds BUILDER_PIN."""
    closer = "}" if text[index] == "{" else "]"
    return _pattern_elements(text, index + 1, closer, closer == "}")


def _after_name_assertion(text: str, index: int) -> int:
    index = _skip_ws(text, index)
    if index < len(text) and text[index] == "!" and not text.startswith(("!=", "!=="), index):
        index = _skip_ws(text, index + 1)
    if index < len(text) and text[index] == ":":
        index = _skip_ws(text, _skip_type_annotation(text, index + 1))
    return index


def _identifier_escape_end(text: str, index: int) -> int | None:
    """Index after a ``\\uXXXX`` or ``\\u{...}`` escape, or None."""
    if not text.startswith("\\u", index):
        return None
    length = len(text)
    if index + 2 < length and text[index + 2] == "{":
        end = text.find("}", index + 3)
        if end < 0:
            return None
        digits = text[index + 3 : end]
        if not digits or any(char not in "0123456789abcdefABCDEF" for char in digits):
            return None
        return end + 1
    digits = text[index + 2 : index + 6]
    if len(digits) < 4 or any(char not in "0123456789abcdefABCDEF" for char in digits):
        return None
    return index + 6


_OTHER_ID_START = frozenset("\u2118\u212e\u309b\u309c")
_OTHER_ID_CONTINUE = frozenset(
    "\u00b7\u0387\u1369\u136a\u136b\u136c\u136d\u136e\u136f\u1370\u1371\u19da"
)


def _identifier_continue(text: str, index: int) -> bool:
    if index >= len(text):
        return False
    if text.startswith("\\u", index):
        return _identifier_escape_end(text, index) is not None
    char = text[index]
    return (
        char.isalnum()
        or char in "_$"
        or char in "\u200c\u200d"
        or char in _OTHER_ID_START
        or char in _OTHER_ID_CONTINUE
        or unicodedata.category(char) in {"Mn", "Mc", "Me", "Nl", "Pc"}
    )


def _identifier_start(text: str, index: int) -> bool:
    if text.startswith("\\u", index):
        return _identifier_escape_end(text, index) is not None
    return index < len(text) and (
        text[index].isalpha()
        or text[index] in "_$"
        or text[index] in _OTHER_ID_START
        or unicodedata.category(text[index]) == "Nl"
    )


def _skip_identifier(text: str, index: int) -> int:
    """Index after the binding name at index, including continuations."""
    length = len(text)
    if text.startswith("\\u", index):
        end = _identifier_escape_end(text, index)
        if end is None:
            return index
        index = end
    elif index < length and _identifier_start(text, index):
        index += 1
    else:
        return index
    while index < length and _identifier_continue(text, index):
        if text.startswith("\\u", index):
            end = _identifier_escape_end(text, index)
            if end is None:
                break
            index = end
            continue
        index += 1
    return index


def _decoded_identifier(text: str, index: int) -> str:
    """Decode the identifier at index, including unicode escapes."""
    end = _skip_identifier(text, index)
    parts: list[str] = []
    cursor = index
    while cursor < end:
        if text.startswith("\\u", cursor):
            escape_end = _identifier_escape_end(text, cursor)
            if escape_end is None or escape_end > end:
                break
            hex_digits = (
                text[cursor + 3 : escape_end - 1]
                if text.startswith("\\u{", cursor)
                else text[cursor + 2 : escape_end]
            )
            try:
                code = int(hex_digits, 16)
            except ValueError:
                return ""
            if code > 0x10FFFF:
                return ""
            parts.append(chr(code))
            cursor = escape_end
            continue
        parts.append(text[cursor])
        cursor += 1
    return "".join(parts)


def _pin_binding_at(text: str, index: int) -> bool:
    """True when the identifier at index decodes to BUILDER_PIN."""
    if _whole_token(text, index, "BUILDER_PIN"):
        return True
    if not _identifier_start(text, index):
        return False
    return _decoded_identifier(text, index) == "BUILDER_PIN"


def _consume_declaration(text: str, index: int, resolved: str) -> tuple[bool, bool, int]:
    """Parse one const, let, or var list starting after the keyword.

    Returns whether it bound BUILDER_PIN, whether every such binding is the
    resolved literal, and the index after the list. A comma starts another
    binding. A type annotation does not end the list. A destructuring binding
    of BUILDER_PIN is not a literal assignment. Export is not required.
    """
    length = len(text)
    has_pin = False
    while index < length:
        index = _skip_ws(text, index)
        if index >= length:
            break
        if _pin_binding_at(text, index):
            has_pin = True
            cursor = _after_name_assertion(text, _skip_identifier(text, index))
            if cursor >= length or text[cursor] != "=":
                return True, False, cursor
            literal = _simple_string_at(text, _skip_ws(text, cursor + 1))
            if literal is None or literal[0] != resolved:
                return True, False, cursor
            if _initializer_continues(text, literal[1]):
                return True, False, literal[1]
            index = literal[1]
            while True:
                index = _skip_ws(text, index)
                if (
                    index < length
                    and text[index] == "!"
                    and not text.startswith(("!=", "!=="), index)
                ):
                    index += 1
                    continue
                break
        elif text[index] in "{[":
            binds, index = _pattern_binds_pin(text, index)
            if binds:
                return True, False, index
            index = _after_name_assertion(text, index)
            if index < length and text[index] == "=":
                index = _skip_initializer(text, index + 1)
        elif _identifier_start(text, index):
            index = _skip_identifier(text, index)
            index = _after_name_assertion(text, index)
            if index < length and text[index] == "=":
                index = _skip_initializer(text, index + 1)
        else:
            break
        index = _skip_ws(text, index)
        if index < length and text[index] == ",":
            index += 1
            continue
        break
    return has_pin, True, index


def _declarations_match(text: str, resolved: str) -> bool:
    """True when every const, let, or var binding of BUILDER_PIN assigns the literal.

    Export is optional. A comma starts another binding in the same declaration.
    A binding nested in another initializer still counts. The tokens may be
    split by whitespace. A string or template does not hide another declaration
    and cannot satisfy the contract.
    """
    index = 0
    length = len(text)
    matched = False
    while index < length:
        char = text[index]
        if char in "'\"":
            index = _skip_quoted(text, index)
            continue
        if char == "`":
            end = text.find("`", index + 1)
            index = length if end < 0 else end + 1
            continue
        keyword = _binding_keyword_at(text, index)
        if keyword is None:
            index += 1
            continue
        has_pin, pin_ok, _end = _consume_declaration(text, index + len(keyword), resolved)
        if has_pin and not pin_ok:
            return False
        if has_pin:
            matched = True
        # Resume inside the declaration so a nested binding is not skipped.
        index += len(keyword)
    return matched


def _constant_assigns(text: str, resolved: str) -> bool:
    """True only when every BUILDER_PIN declaration assigns the resolved literal."""
    return _declarations_match(
        _js_executable_text(text, keep_templates=True),
        resolved,
    )


def _named_lock_paths(pins: list[dict[str, object]]) -> set[str]:
    """Locks a pin names. A pin with no lock field names package-lock.json only."""
    named: set[str] = set()
    for pin in pins:
        lock = pin.get("lock")
        if isinstance(lock, str) and lock:
            named.add(lock)
        else:
            named.add("package-lock.json")
    return named


def _pin_classified_paths(pins: list[dict[str, object]]) -> set[str]:
    """Manifests and locks a pin names. An empty pin list names nothing."""
    if not pins:
        return set()
    paths = set(_named_lock_paths(pins))
    for pin in pins:
        if not (pin.get("field") or pin.get("override")):
            continue
        manifest = pin.get("manifest")
        paths.add(manifest if isinstance(manifest, str) and manifest else "package.json")
    return paths


def _pins_from_tree(repo: Path, tree: str) -> list[dict[str, object]] | None:
    text = _tree_file_text(repo, tree, PIN_SPEC_PATH)
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, dict):
        return []
    pins = loaded.get("pins")
    if not isinstance(pins, list):
        return []
    if any(not isinstance(pin, dict) for pin in pins):
        return None
    return pins


def _load_sync_classes(repo: Path, tree: str) -> dict[str, object] | None:
    text = _tree_file_text(repo, tree, SYNC_CLASS_SPEC)
    if text is None:
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict) or loaded.get("version") != 1:
        return None
    return loaded


def _ledger_entries_from_tree(repo: Path, tree: str) -> list[LedgerEntry]:
    text = _tree_file_text(repo, tree, "docs/FORK_CHANGES.md")
    if not text:
        return []
    return parse_ledger(text)


def _entry_is_active(entries: list[LedgerEntry], entry_id: str) -> bool:
    return bool(entry_id) and any(
        entry.entry_id == entry_id and entry.fields.get("Disposition") == "active"
        for entry in entries
    )


def _path_owned_by_active(path: str, entries: list[LedgerEntry]) -> bool:
    return any(
        path in entry.owned_files and entry.fields.get("Disposition") == "active"
        for entry in entries
    )


def _classify_sync_path(
    path: str,
    spec: dict[str, object],
    named_locks: set[str],
    entries: list[LedgerEntry],
) -> str:
    """First match: specified lock, structured pin, build, hold, unspecified, owned."""
    if path in named_locks:
        return "generated-specified"
    contracts = spec.get("structured_pin_contract")
    if isinstance(contracts, list):
        for contract in contracts:
            if isinstance(contract, dict) and contract.get("manifest") == path:
                return "structured-pin"
    for kind, key in (
        ("build-output", "build_output"),
        ("auth-hold", "auth_hold"),
        ("generated-unspecified", "generated_unspecified"),
    ):
        block = spec.get(key)
        if not isinstance(block, dict):
            continue
        paths = block.get("paths")
        if isinstance(paths, list) and path in paths:
            return kind
    if path.startswith("plugins/kanban/dashboard/dist/"):
        return "build-output"
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        return "auth-hold"
    if path.rsplit("/", 1)[-1] == "package-lock.json":
        return "generated-unspecified"
    if _path_owned_by_active(path, entries):
        return "owned-source"
    return "unowned"


def _is_rust_attribute(stripped: str) -> bool:
    """``#[`` and ``#![`` are attributes. Comments and whitespace may sit between.

    Rust treats ``#!`` as a shebang only when no ``[`` follows it after
    comments and whitespace. ``#![no_std]`` and ``#! [no_std]`` are inner
    attributes that change compilation, so a dropped one executes.
    """
    rest = _js_executable_text(stripped[1:]).lstrip()
    if rest.startswith("!"):
        rest = _js_executable_text(rest[1:]).lstrip()
    return rest.startswith("[")


def _hash_identifier(rest: str) -> tuple[str, str | None] | None:
    """Split a hash token into its identifier and the next non-space character.

    A private name uses the same identifier rules as a declaration, including
    Other_ID_Start, Other_ID_Continue, and unicode escapes.
    """
    if not _identifier_start(rest, 0):
        return None
    index = _skip_identifier(rest, 0)
    if index == 0:
        return None
    nxt_index = index
    while nxt_index < len(rest) and _is_js_whitespace(rest[nxt_index]):
        nxt_index += 1
    return rest[:index], rest[nxt_index] if nxt_index < len(rest) else None


def _hash_line_executes(stripped: str) -> bool:
    """True when a hash line is code, not prose.

    A private field, a preprocessor directive, and a Rust attribute still
    execute. A shebang and a spaced prose comment do not. C strips comments
    before it reads a directive, so ``#/**/include`` still executes.
    """
    if _is_rust_attribute(stripped):
        return True
    if stripped.startswith("#!"):
        return False
    stripped = _js_executable_text(stripped)
    rest = stripped[1:]
    if not rest:
        return False
    if not rest[0].isspace():
        match = _hash_identifier(rest)
        if match is None:
            return False
        head, nxt = match
        if head in {
            "include",
            "define",
            "undef",
            "if",
            "ifdef",
            "ifndef",
            "elif",
            "elifdef",
            "elifndef",
            "else",
            "endif",
            "pragma",
            "error",
            "warning",
            "line",
            "embed",
        }:
            return True
        # A private name is code when an operator follows it. A bare #comment
        # is still a comment, so the next token has to be an operator.
        return nxt in {"=", "(", ".", ";", "[", "+", "-", "*", "/", "%", "&", "|", "^", "<", ">", "?", "!", ",", ":", "`"}
    spaced = rest.lstrip()
    match = re.match(r"([A-Za-z_]\w*)\s*(\S)?", spaced)
    if match is None:
        return False
    head, nxt = match.group(1), match.group(2)
    directives = {
        "include", "define", "undef", "if", "ifdef", "ifndef", "elif",
        "elifdef", "elifndef", "else", "endif", "pragma", "error",
        "warning", "line", "embed",
    }
    return head in directives or nxt in {"(", "<", '"', "'"}


def _non_executable_indexes(lines: list[str], path: str | None = None) -> set[int]:
    """Indexes whose absence is a note, not a line that stops the sync.

    A line is non-executable only when ``is_note_line`` says so. A path
    ending in ``.py`` uses Python and the joined lines as source. A missing
    path uses ``other``.
    """
    language = "python" if path is not None and path.endswith(".py") else "other"
    source = "".join(lines) if language == "python" else None
    return {
        index
        for index, line in enumerate(lines)
        if is_note_line(
            line,
            language=language,
            source=source,
            line_number=index + 1,
        )
    }


def _call_callee(line: str) -> str:
    """The dotted name before a call. A leading await or yield is not the callee."""
    stripped = line.strip()
    while True:
        match = re.match(r"(?:await|yield)\s+", stripped)
        if match is None:
            break
        stripped = stripped[match.end() :]
    match = re.match(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(", stripped)
    return match.group(1) if match else ""


def _is_rewrite(fork_line: str, upstream_line: str) -> bool:
    """A rewrite shares the statement's first token. Shared indentation is not enough.

    An assignment rewrite may replace the whole literal. A call is not a rewrite
    of a different call, even when the two lines sit beside each other.
    """
    if fork_line == upstream_line:
        return True
    fork_call = _call_callee(fork_line)
    upstream_call = _call_callee(upstream_line)
    if (fork_call or upstream_call) and fork_call != upstream_call:
        return False
    fork_token = _statement_token(fork_line)
    upstream_token = _statement_token(upstream_line)
    if not fork_token or fork_token != upstream_token:
        return False
    if _is_assignment(fork_line) and _is_assignment(upstream_line):
        return True
    return (
        difflib.SequenceMatcher(
            a=fork_line.strip(), b=upstream_line.strip(), autojunk=False
        ).ratio()
        >= 0.5
    )


def _is_assignment(line: str) -> bool:
    return bool(re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", line.strip()))


def _statement_token(line: str) -> str:
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", line.strip())
    return match.group(0) if match else ""


def _has_executable_drop(
    fork_text: str,
    published_text: str,
    upstream_text: str = "",
    path: str | None = None,
) -> bool:
    """True when a fork-only executable insertion is absent and an upstream line remains.

    Alignment is positional. Every upstream occurrence of a duplicate line is
    reserved. A published copy reserved for an upstream occurrence cannot cover
    an insertion. A preserved extra copy is not a drop. A second copy elsewhere
    does not cover a dropped copy. A fork line that rewrites a distinct upstream
    line is not an insertion.
    """
    fork_lines = _line_records(fork_text)
    published_lines = _line_records(published_text)
    upstream_line_list = _line_records(upstream_text)
    if not any(line in set(published_lines) for line in upstream_line_list):
        return False
    insertions, _correspondence, shared = _insertion_indexes(
        fork_lines, upstream_line_list, path
    )
    if not insertions:
        return False
    preserved = {
        index
        for index in insertions
        if index < len(published_lines) and published_lines[index] == fork_lines[index]
    }
    reserved = _reserved_published(published_lines, upstream_line_list, preserved)
    return _published_drops_insertions(
        fork_lines, published_lines, insertions, shared, reserved
    )


def _insertion_indexes(
    fork_lines: list[str],
    upstream_lines: list[str],
    path: str | None = None,
) -> tuple[set[int], dict[int, str], dict[int, int]]:
    """Fork lines with no positional upstream rewrite counterpart.

    The second result maps a rewritten fork line to the upstream line it
    rewrites. The third maps a shared fork line to its upstream index. That
    upstream line remains a boundary after its text changes.
    """
    matcher = difflib.SequenceMatcher(a=fork_lines, b=upstream_lines, autojunk=False)
    insertions: set[int] = set()
    correspondence: dict[int, str] = {}
    shared: dict[int, int] = {}
    non_executable = _non_executable_indexes(fork_lines, path)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset, index in enumerate(range(i1, i2)):
                shared[index] = j1 + offset
            continue
        if tag not in {"delete", "replace"}:
            continue
        counterparts = list(range(j1, j2)) if tag == "replace" else []
        for index in range(i1, i2):
            line = fork_lines[index]
            if index in non_executable:
                continue
            match_at = next(
                (
                    offset
                    for offset, upstream_index in enumerate(counterparts)
                    if _is_rewrite(line, upstream_lines[upstream_index])
                ),
                None,
            )
            if match_at is not None:
                upstream_index = counterparts.pop(match_at)
                correspondence[index] = upstream_lines[upstream_index]
                shared[index] = upstream_index
                continue
            insertions.add(index)
    return insertions, correspondence, shared


def _reserved_published(
    published_lines: list[str],
    upstream_lines: list[str],
    preserved: set[int] | None = None,
) -> dict[int, int]:
    """Published index reserved for each upstream line.

    Every upstream occurrence is reserved before a published copy can cover
    an insertion. An extra published copy, beyond those reservations, can.
    """
    matcher = difflib.SequenceMatcher(
        a=published_lines, b=upstream_lines, autojunk=False
    )
    reserved: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset, upstream_index in enumerate(range(j1, j2)):
                reserved[upstream_index] = i1 + offset
            continue
        if tag != "replace":
            continue
        counterparts = list(range(i1, i2))
        for upstream_index in range(j1, j2):
            match_at = next(
                (
                    offset
                    for offset, published_index in enumerate(counterparts)
                    if _is_rewrite(
                        published_lines[published_index],
                        upstream_lines[upstream_index],
                    )
                ),
                None,
            )
            if match_at is None:
                continue
            reserved[upstream_index] = counterparts.pop(match_at)
    used = set(reserved.values())
    blocked = preserved or set()
    for upstream_index, line in enumerate(upstream_lines):
        if upstream_index in reserved:
            continue
        for published_index, published_line in enumerate(published_lines):
            if published_index in used or published_index in blocked:
                continue
            if published_line == line or _is_rewrite(published_line, line):
                reserved[upstream_index] = published_index
                used.add(published_index)
                break
    return reserved


def _next_reserved_boundary(
    fork_lines: list[str],
    index: int,
    insertions: set[int],
    shared: dict[int, int],
    reserved: dict[int, int],
) -> int | None:
    for later in range(index + 1, len(fork_lines)):
        if later in insertions:
            continue
        boundary = reserved.get(shared.get(later, -1))
        if boundary is not None:
            return boundary
    return None


def _find_unreserved(
    lines: list[str],
    needle: str,
    start: int,
    end: int | None,
    reserved_indexes: set[int],
) -> int | None:
    limit = len(lines) if end is None else end
    for index in range(start, limit):
        if index in reserved_indexes:
            continue
        if lines[index] == needle:
            return index
    return None


def _published_drops_insertions(
    fork_lines: list[str],
    published_lines: list[str],
    insertions: set[int],
    shared: dict[int, int],
    reserved: dict[int, int],
) -> bool:
    """True when an insertion is absent before the next reserved upstream line.

    Every upstream occurrence is reserved. A published copy reserved for an
    upstream occurrence cannot cover an insertion. A preserved extra copy,
    beyond those reserved occurrences, is not a drop. A later copy does not
    cover a line removed from its position.
    """
    published_index = 0
    reserved_indexes = set(reserved.values())
    for index, line in enumerate(fork_lines):
        if index not in insertions:
            boundary = reserved.get(shared.get(index, -1))
            if boundary is not None and boundary + 1 > published_index:
                published_index = boundary + 1
            continue
        limit = _next_reserved_boundary(
            fork_lines, index, insertions, shared, reserved
        )
        found = _find_unreserved(
            published_lines, line, published_index, limit, reserved_indexes
        )
        if found is None:
            return True
        published_index = found + 1
    return False


def _entry_equal(
    left: dict[str, tuple[str, str, str]],
    right: dict[str, tuple[str, str, str]],
    path: str,
) -> bool:
    return path in left and left.get(path) == right.get(path)


def _text_at(repo: Path, entries: dict[str, tuple[str, str, str]], path: str) -> str:
    return _blob_text(repo, entries.get(path)) or ""


def _pins_for_manifest(
    pins: list[dict[str, object]], manifest: str
) -> list[dict[str, object]]:
    """Pins that authorize keys in this manifest, not in some other package.json."""
    matched: list[dict[str, object]] = []
    for pin in pins:
        pin_manifest = pin.get("manifest")
        if isinstance(pin_manifest, str) and pin_manifest:
            if pin_manifest == manifest:
                matched.append(pin)
            continue
        if manifest == "package.json":
            matched.append(pin)
    return matched


def _pin_manifest_paths(pins: list[dict[str, object]]) -> set[str]:
    found: set[str] = set()
    for pin in pins:
        if not (pin.get("override") or pin.get("field")):
            continue
        manifest = pin.get("manifest") or "package.json"
        if isinstance(manifest, str) and manifest:
            found.add(manifest)
    return found


def _resolved_pin_value(
    pins: list[dict[str, object]], package: str, manifest: str
) -> str | None:
    for pin in pins:
        if pin.get("package") != package:
            continue
        pin_manifest = pin.get("manifest")
        if isinstance(pin_manifest, str) and pin_manifest != manifest:
            continue
        if not isinstance(pin_manifest, str) and manifest != "package.json":
            continue
        for key in ("resolved", "exact"):
            value = pin.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _published_contract_holds(
    repo: Path,
    tree: str,
    contract: dict[str, object],
    pins: list[dict[str, object]],
) -> bool:
    """Each declared constant must assign the resolved pin. Absence does not pass."""
    constants = contract.get("constants")
    if not isinstance(constants, list):
        return True
    return _contract_assigns_builder_pin(repo, tree, contract, pins)


def _contract_assigns_builder_pin(
    repo: Path,
    tree: str,
    contract: dict[str, object],
    pins: list[dict[str, object]],
) -> bool:
    package = contract.get("package")
    manifest = contract.get("manifest")
    constants = contract.get("constants")
    if not isinstance(package, str) or not isinstance(manifest, str):
        return False
    if not isinstance(constants, list) or not constants:
        return False
    resolved = _resolved_pin_value(pins, package, manifest)
    if not resolved:
        return False
    for constant in constants:
        if not isinstance(constant, str) or not constant:
            return False
        text = _tree_file_text(repo, tree, constant)
        if text is None:
            return False
        if not _constant_assigns(text, resolved):
            return False
    return True


def _structured_pin_flags(
    repo: Path,
    expected_tree: str,
    actual_tree: str,
    path: str,
    spec: dict[str, object],
    pins: list[dict[str, object]],
) -> tuple[bool, bool]:
    contract: dict[str, object] | None = None
    contracts = spec.get("structured_pin_contract")
    if isinstance(contracts, list):
        for item in contracts:
            if isinstance(item, dict) and item.get("manifest") == path:
                contract = item
                break
    if contract is None:
        return False, False
    expected_doc = _json_object(repo, expected_tree, path)
    actual_doc = _json_object(repo, actual_tree, path)
    pin_keys_only = (
        expected_doc is not None
        and actual_doc is not None
        and _manifest_delta_is_pinned(
            expected_doc, actual_doc, _pins_for_manifest(pins, path)
        )
    )
    return pin_keys_only, _contract_assigns_builder_pin(repo, actual_tree, contract, pins)


def _class_gate(
    path: str,
    kind: str,
    *,
    equals_upstream: bool = False,
    equals_fork: bool = False,
    pin_keys_only: bool = False,
    contract_ok: bool = False,
    executable_drop: bool = False,
) -> bool:
    return delta_allowed(
        path,
        kind,
        equals_upstream=equals_upstream,
        equals_fork=equals_fork,
        pin_keys_only=pin_keys_only,
        contract_ok=contract_ok,
        executable_drop=executable_drop,
    )


def _sync_delta_permitted(
    repo: Path,
    expected_tree: str,
    actual_tree: str,
    first_parent: str,
    upstream_parent: str,
    *,
    fork_tree: str,
    ledger_entries: list[LedgerEntry] | None,
    conflict_paths: set[str],
    rename_paths: set[str],
) -> bool:
    """Allow a non-clean sync only when each changed path's class permits it.

    Pin-named locks and pin manifests stay on the existing pin replay. A lock
    no pin names is unspecified and must equal the upstream blob. Unowned paths
    stay on the existing conflict fit. A missing or wrong-version class spec
    fails the check.
    """
    spec = _load_sync_classes(repo, fork_tree)
    if spec is None:
        return False
    if not _clean_class_holds(
        repo,
        actual_tree,
        first_parent,
        upstream_parent,
        fork_tree=fork_tree,
        ledger_entries=ledger_entries,
    ):
        return False
    entries = (
        ledger_entries
        if ledger_entries is not None
        else _ledger_entries_from_tree(repo, fork_tree)
    )
    pins = _pins_from_tree(repo, first_parent)
    if pins is None:
        return False
    named = _named_lock_paths(pins)
    manifests = _pin_manifest_paths(pins)
    expected_entries = _tree_entries(repo, expected_tree)
    actual_entries = _tree_entries(repo, actual_tree)
    first_entries = _tree_entries(repo, first_parent)
    upstream_entries = _tree_entries(repo, upstream_parent)
    changed = {
        path
        for path in expected_entries.keys() | actual_entries.keys()
        if expected_entries.get(path) != actual_entries.get(path)
    }
    if not changed:
        return True

    pre_allowed: set[str] = set()
    pin_paths: set[str] = set()
    for path in sorted(changed):
        kind = _classify_sync_path(path, spec, named, entries)
        equals_upstream = _entry_equal(actual_entries, upstream_entries, path)
        equals_fork = _entry_equal(actual_entries, first_entries, path)
        executable_drop = _has_executable_drop(
            _text_at(repo, first_entries, path),
            _text_at(repo, actual_entries, path),
            _text_at(repo, upstream_entries, path),
        )
        if kind == "generated-specified" or (
            path in manifests and kind in {"unowned", "owned-source"}
        ):
            # Pin manifests are not source fits. The pin gates already bound
            # that delta; the owned-source drop rule must not replace them.
            pin_paths.add(path)
            continue
        if kind == "unowned":
            _class_gate(path, kind)
            continue
        if kind == "owned-source":
            if not _class_gate(path, kind, executable_drop=executable_drop):
                return False
            continue
        if kind == "auth-hold":
            auth = spec.get("auth_hold")
            entry_id = ""
            if isinstance(auth, dict) and isinstance(auth.get("ledger_entry"), str):
                entry_id = auth["ledger_entry"]
            held = equals_fork and _entry_is_active(entries, entry_id)
            if not _class_gate(path, kind, equals_fork=held):
                return False
            pre_allowed.add(path)
            continue
        if kind == "structured-pin":
            pin_keys_only, contract_ok = _structured_pin_flags(
                repo, expected_tree, actual_tree, path, spec, pins
            )
            manifest_ok = _manifests_meet_pins(
                repo, actual_tree, _pins_for_manifest(pins, path)
            )
            if not manifest_ok or not _class_gate(
                path,
                kind,
                pin_keys_only=pin_keys_only,
                contract_ok=contract_ok,
            ):
                return False
            pre_allowed.add(path)
            continue
        if kind == "generated-unspecified":
            if not _class_gate(path, kind, equals_upstream=equals_upstream):
                return False
            pre_allowed.add(path)
            continue
        if kind == "build-output":
            _class_gate(
                path,
                kind,
                equals_upstream=equals_upstream,
                equals_fork=equals_fork,
            )
            return False
        return False

    for path in (conflict_paths - rename_paths) - pre_allowed - pin_paths:
        kind = _classify_sync_path(path, spec, named, entries)
        if not _conflict_fit_allowed(
            repo,
            first_entries.get(path),
            upstream_entries.get(path),
            actual_entries.get(path),
            reject_executable_drop=kind in {"owned-source", "unowned"},
            path=path,
        ):
            return False
        if path in changed:
            pre_allowed.add(path)

    for path in rename_paths & changed:
        if path in pre_allowed or path in pin_paths:
            continue
        kind = _classify_sync_path(path, spec, named, entries)
        if kind in {
            "build-output",
            "generated-unspecified",
            "auth-hold",
            "structured-pin",
            "generated-specified",
        }:
            return False
        pre_allowed.add(path)

    if changed - pre_allowed - pin_paths:
        return False
    if not pin_paths:
        return True
    pin_ok = _pin_replay_delta_allowed(
        repo,
        expected_tree,
        actual_tree,
        first_parent,
        already_allowed=pre_allowed,
    )
    for path in pin_paths:
        if _classify_sync_path(path, spec, named, entries) != "generated-specified":
            continue
        if not _class_gate(path, "generated-specified", pin_keys_only=pin_ok):
            return False
    return pin_ok


def _classified_without_spec(path: str) -> bool:
    """Path shapes the class file would name. A missing spec still fails these."""
    if path in {
        "uv.lock",
        "pm/uv.lock",
        "pm/lock.json",
        "flake.lock",
        "nix/pythonLock.nix",
        "nix/npm-pinned.nix",
    }:
        return True
    if path.rsplit("/", 1)[-1] == "package-lock.json":
        return True
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        return True
    if path in {
        "apps/desktop/package.json",
        "apps/desktop/scripts/builder-arch-targets.mjs",
        "apps/desktop/scripts/builder-config-26.mjs",
    }:
        return True
    return path.startswith("plugins/kanban/dashboard/dist/")


def _same_or_both_absent(
    left: dict[str, tuple[str, str, str]],
    right: dict[str, tuple[str, str, str]],
    path: str,
) -> bool:
    if path not in left and path not in right:
        return True
    return left.get(path) == right.get(path)


def _spec_path_list(spec: dict[str, object], key: str) -> list[str]:
    block = spec.get(key)
    if not isinstance(block, dict):
        return []
    paths = block.get("paths")
    if not isinstance(paths, list):
        return []
    return [path for path in paths if isinstance(path, str) and path]


def _clean_class_holds(
    repo: Path,
    tree: str,
    first_parent: str,
    upstream_parent: str,
    *,
    fork_tree: str,
    ledger_entries: list[LedgerEntry] | None,
) -> bool:
    """A clean merge is exempt only when each class action holds.

    A missing or unreadable spec fails closed for a path the class file would
    name. A present spec rejects a splice of an unspecified generated file, a
    workflow blob that is not the fork blob, and build output that equals
    neither parent.
    """
    spec = _load_sync_classes(repo, fork_tree)
    published = _tree_entries(repo, tree)
    fork_entries = _tree_entries(repo, first_parent)
    upstream_entries = _tree_entries(repo, upstream_parent)
    if spec is None:
        pins = _pins_from_tree(repo, first_parent)
        if pins is None:
            return False
        entries = ledger_entries if ledger_entries is not None else _ledger_entries_from_tree(repo, fork_tree)
        dynamic = _pin_classified_paths(pins)
        touched = set(published) | set(fork_entries) | set(upstream_entries)
        for path in touched:
            owned = _path_owned_by_active(path, entries)
            if path not in dynamic and not _classified_without_spec(path) and not owned:
                continue
            if owned:
                if not _same_or_both_absent(published, fork_entries, path):
                    return False
                continue
            if not _same_or_both_absent(published, fork_entries, path):
                return False
            if not _same_or_both_absent(published, upstream_entries, path):
                return False
        return True
    entries = ledger_entries if ledger_entries is not None else _ledger_entries_from_tree(repo, fork_tree)
    pins = _pins_from_tree(repo, first_parent)
    if pins is None:
        return False
    named = _named_lock_paths(pins)
    published = _tree_entries(repo, tree)
    fork_entries = _tree_entries(repo, first_parent)
    upstream_entries = _tree_entries(repo, upstream_parent)
    unspecified = set(_spec_path_list(spec, "generated_unspecified")) - named
    for path in set(published) | set(fork_entries) | set(upstream_entries):
        if path.rsplit("/", 1)[-1] == "package-lock.json" and path not in named:
            unspecified.add(path)
    for path in unspecified:
        if not _same_or_both_absent(published, upstream_entries, path):
            return False
    named_present = [
        path
        for path in named
        if path in published or path in upstream_entries or path in fork_entries
    ]
    if not _manifests_meet_pins(repo, tree, pins):
        return False
    if named_present:
        if not pins or any(not _pin_has_bound(pin) for pin in pins):
            return False
        versions = _merged_lock_versions(repo, tree)
        if versions is None or not _locks_meet_pins(versions, pins):
            return False
    hold = spec.get("auth_hold")
    entry_id = ""
    if isinstance(hold, dict) and isinstance(hold.get("ledger_entry"), str):
        entry_id = hold["ledger_entry"]
    touched = set(published) | set(fork_entries) | set(upstream_entries)
    workflow_paths = set(_spec_path_list(spec, "auth_hold"))
    build_paths = set(_spec_path_list(spec, "build_output"))
    for path in touched:
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            workflow_paths.add(path)
        if path.startswith("plugins/kanban/dashboard/dist/"):
            build_paths.add(path)
    for path in workflow_paths:
        if not _same_or_both_absent(published, fork_entries, path):
            return False
        if not _same_or_both_absent(published, upstream_entries, path):
            if not _entry_is_active(entries, entry_id):
                return False
    for path in build_paths:
        equals_fork = _same_or_both_absent(published, fork_entries, path)
        equals_upstream = _same_or_both_absent(published, upstream_entries, path)
        if not equals_fork and not equals_upstream:
            return False
    contracts = spec.get("structured_pin_contract")
    if isinstance(contracts, list):
        for contract in contracts:
            if not isinstance(contract, dict):
                return False
            if not _published_contract_holds(repo, tree, contract, pins):
                return False
    return True


def _is_clean_upstream_sync(
    repo: Path,
    sha: str,
    parents: list[str],
    upstream_oid: str,
    *,
    fork_tree: str | None = None,
    ledger_entries: list[LedgerEntry] | None = None,
) -> bool:
    if len(parents) != 2:
        return False
    first_parent, upstream_parent = parents
    if not _is_ancestor(repo, upstream_parent, upstream_oid):
        return False
    if _is_ancestor(repo, first_parent, upstream_oid):
        return False
    proc = _run_git(
        repo,
        "merge-tree",
        "--write-tree",
        "--messages",
        "--name-only",
        "-z",
        first_parent,
        upstream_parent,
    )
    if proc.returncode not in {0, 1}:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CheckerError(f"git merge-tree failed (exit {proc.returncode}): {detail}")
    tree_section, separator, message_section = proc.stdout.partition(b"\0\0")
    tree_parts = tree_section.split(b"\0")
    expected_tree_raw = tree_parts[0] if tree_parts else b""
    if not re.fullmatch(rb"[0-9a-f]{40,64}", expected_tree_raw):
        return False
    expected_tree = expected_tree_raw.decode("ascii")
    actual_tree = _git(repo, "rev-parse", f"{sha}^{{tree}}")
    spec_tree = fork_tree or sha
    if proc.returncode == 0:
        if expected_tree == actual_tree:
            return _clean_class_holds(
                repo,
                actual_tree,
                first_parent,
                upstream_parent,
                fork_tree=spec_tree,
                ledger_entries=ledger_entries,
            )
        return _sync_delta_permitted(
            repo,
            expected_tree,
            actual_tree,
            first_parent,
            upstream_parent,
            fork_tree=spec_tree,
            ledger_entries=ledger_entries,
            conflict_paths=set(),
            rename_paths=set(),
        )
    if not separator:
        return False

    # A conflicted sync is exempt only when content conflicts keep upstream lines
    # and extras come from the fork. A wholesale fork file is not exempt.
    # Directory-rename suggestions name both the proposed destination and the
    # original source, so retaining the original ledger path is also auditable.
    conflict_paths = {
        raw.decode("utf-8", errors="surrogateescape")
        for raw in tree_parts[1:]
        if raw
    }
    if not conflict_paths:
        return False

    relocation_sources: dict[str, str] = {}
    message_parts = message_section.split(b"\0")
    index = 0
    while index < len(message_parts) and message_parts[index]:
        try:
            path_count = int(message_parts[index])
        except ValueError:
            return False
        group_end = index + path_count + 3
        if path_count < 1 or group_end > len(message_parts):
            return False
        paths = [
            raw.decode("utf-8", errors="surrogateescape")
            for raw in message_parts[index + 1 : index + 1 + path_count]
        ]
        kind = message_parts[index + 1 + path_count]
        if kind == b"CONFLICT (directory rename suggested)":
            if path_count != 2:
                return False
            destination, source = paths
            if destination in relocation_sources.values() or source in relocation_sources:
                return False
            relocation_sources[source] = destination
        index = group_end

    def tree_entries(treeish: str) -> dict[str, tuple[str, str, str]]:
        entries: dict[str, tuple[str, str, str]] = {}
        raw = _git_bytes(repo, "ls-tree", "-r", "-z", treeish)
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                metadata, path = record.split(b"\t", 1)
                mode, kind, oid = metadata.split(b" ", 2)
            except ValueError as exc:
                raise CheckerError("malformed git ls-tree output") from exc
            entries[path.decode("utf-8", errors="surrogateescape")] = (
                mode.decode("ascii"), kind.decode("ascii"), oid.decode("ascii")
            )
        return entries

    first_entries = tree_entries(first_parent)
    expected_entries = tree_entries(expected_tree)
    actual_entries = tree_entries(actual_tree)

    # Rename suggestions keep the original fork path. Class rules decide the
    # remaining conflict paths; unowned content conflicts still use the fit.
    rename_paths = set(relocation_sources) | set(relocation_sources.values())
    if any(
        actual_entries.get(path) != first_entries.get(path)
        for path in conflict_paths & rename_paths
    ):
        return False
    for source, destination in relocation_sources.items():
        if destination not in conflict_paths:
            return False
        source_entry = first_entries.get(source)
        if (
            source_entry is None
            or actual_entries.get(source) != source_entry
            or expected_entries.get(source) == source_entry
            or expected_entries.get(destination) != source_entry
        ):
            return False

    return _sync_delta_permitted(
        repo,
        expected_tree,
        actual_tree,
        first_parent,
        upstream_parent,
        fork_tree=spec_tree,
        ledger_entries=ledger_entries,
        conflict_paths=conflict_paths,
        rename_paths=rename_paths,
    )


def _tree_entries(repo: Path, treeish: str) -> dict[str, tuple[str, str, str]]:
    entries: dict[str, tuple[str, str, str]] = {}
    raw = _git_bytes(repo, "ls-tree", "-r", "-z", treeish)
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ", 2)
        except ValueError as exc:
            raise CheckerError("malformed git ls-tree output") from exc
        entries[path.decode("utf-8", errors="surrogateescape")] = (
            mode.decode("ascii"),
            kind.decode("ascii"),
            oid.decode("ascii"),
        )
    return entries


def _merge_conflict_paths(repo: Path, first: str, upstream_parent: str) -> set[str]:
    proc = _run_git(
        repo,
        "merge-tree",
        "--write-tree",
        "--messages",
        "--name-only",
        "-z",
        first,
        upstream_parent,
    )
    if proc.returncode not in {0, 1}:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CheckerError(f"git merge-tree failed (exit {proc.returncode}): {detail}")
    tree_section, _separator, _message = proc.stdout.partition(b"\0\0")
    if proc.returncode == 0:
        return set()
    return {
        raw.decode("utf-8", errors="surrogateescape")
        for raw in tree_section.split(b"\0")[1:]
        if raw
    }


def _tip_missing_upstream_lines(
    repo: Path,
    conflict_paths: set[str],
    upstream_oid: str,
    fork_oid: str,
) -> list[str]:
    """Paths whose current upstream lines are absent from the checked tip."""
    upstream_entries = _tree_entries(repo, upstream_oid)
    tip_entries = _tree_entries(repo, fork_oid)
    missing: list[str] = []
    for path in sorted(conflict_paths):
        upstream = upstream_entries.get(path)
        actual = tip_entries.get(path)
        if upstream is None:
            if actual is not None:
                missing.append(path)
            continue
        # Pass the tip as the fork text so later fork insertions are allowed.
        # Upstream lines must still occur in order. A missing upstream blob
        # that the tip still carries is not a repair.
        if not _conflict_fit_allowed(repo, actual, upstream, actual, path=path):
            missing.append(path)
    return missing


_DELETION_ITEM = re.compile(
    r"^(?P<path>\S+) removed-upstream-blobs (?P<blobs>[0-9a-f]{40}(?:\+[0-9a-f]{40})*)$"
)


def _classify_upstream_line_deletions(
    repo: Path,
    entries: list[LedgerEntry],
    upstream_oid: str,
    fork_oid: str,
) -> dict[str, str]:
    """Audit an intentional removal of upstream lines, and return the waivers.

    ``Upstream-Line-Deletions`` names the path and the blob of the upstream
    version whose lines the fork removed. The blob is the audited text: the
    declaration only waives lines that are absent at the tip when that exact
    blob is what upstream currently has. Upstream moving on invalidates it, so
    a deletion cannot silently cover lines nobody reviewed. The field may be
    declared only by G-FORK-LEDGER, which owns no product path, so the owner of
    the path cannot waive its own deletion.
    """
    declarers = [e for e in entries if "Upstream-Line-Deletions" in e.fields]
    for entry in declarers:
        if entry.entry_id != "G-FORK-LEDGER":
            entry.problems.append(
                "Upstream-Line-Deletions may be declared only by G-FORK-LEDGER"
            )
    ledger_entries = [e for e in declarers if e.entry_id == "G-FORK-LEDGER"]
    if len(ledger_entries) > 1:
        for entry in ledger_entries:
            entry.problems.append(
                "Upstream-Line-Deletions requires one unique G-FORK-LEDGER entry"
            )
        return {}
    if not ledger_entries:
        return {}
    entry = ledger_entries[0]
    raw = entry.fields["Upstream-Line-Deletions"].strip()
    if not raw:
        entry.problems.append("Upstream-Line-Deletions cannot be empty")
        return {}
    waivers: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        matched = _DELETION_ITEM.fullmatch(item)
        if not matched:
            entry.problems.append(
                "Upstream-Line-Deletions item must be "
                "'<path> removed-upstream-blobs <40-hex>[+<40-hex>...]': " + item
            )
            continue
        path = matched.group("path")
        blobs = matched.group("blobs").split("+")
        if len(blobs) != len(set(blobs)):
            entry.problems.append(
                f"Upstream-Line-Deletions for {path} repeats a blob id; "
                "repeating a hash does not audit more text"
            )
            continue
        if path in waivers:
            entry.problems.append(
                f"duplicate Upstream-Line-Deletions declaration: {path}"
            )
            continue
        # Every declared blob must be an ancestor version of the path, so the
        # declaration cannot name an arbitrary blob to launder a deletion.
        for blob in blobs:
            owners = _git(
                repo, "log", "--format=%H", upstream_oid, "--find-object", blob
            ).split()
            if not owners:
                entry.problems.append(
                    f"Upstream-Line-Deletions blob {blob[:12]} for {path} "
                    "is not a blob upstream ever committed"
                )
                break
        else:
            # Count occurrences, not set membership. A line upstream now has
            # twice, of which the tip kept one, is a new line the declaration
            # never audited; treating lines as a set would waive it.
            from collections import Counter

            def _records(raw: bytes) -> list[str]:
                # Same contract as _line_records, applied to raw blob bytes so
                # no decode-and-strip step can hide a line ending.
                return _line_records(raw.decode("utf-8", errors="surrogateescape"))

            tip_counts = Counter(_records(_git_bytes(repo, "show", f"{fork_oid}:{path}")))
            upstream_counts = Counter(
                _records(_git_bytes(repo, "show", f"{upstream_oid}:{path}"))
            )
            audited_counts: Counter[str] = Counter()
            for blob in blobs:
                audited_counts.update(_records(_git_bytes(repo, "cat-file", "-p", blob)))
            uncovered = sum(
                max(0, upstream_counts[line] - tip_counts[line] - audited_counts[line])
                for line in upstream_counts
            )
            if uncovered:
                entry.problems.append(
                    f"Upstream-Line-Deletions for {path} leaves "
                    f"{uncovered} current upstream line(s) unaudited"
                )
                continue
            waivers[path] = "+".join(blobs)
    return waivers


def _classify_repaired_conflict_merges(
    repo: Path,
    entries: list[LedgerEntry],
    upstream_oid: str,
    fork_oid: str,
    work_shas: set[str],
    sync_set: set[str],
    deleted_upstream_blobs: dict[str, str],
) -> list[str]:
    """Map a conflict merge only when the tip still keeps upstream lines.

    The listing does not waive the commit. Verification uses the checked tip
    and the current upstream ref, so a later drop of those lines fails closed.
    A later owned edit of the file is not a waiver: ownership attributes the
    edit, it does not prove the upstream lines survived it.
    """
    declarers = [
        entry for entry in entries if "Repaired-Conflict-Merges" in entry.fields
    ]
    for entry in declarers:
        if entry.entry_id != "G-FORK-LEDGER":
            entry.problems.append(
                "Repaired-Conflict-Merges may be declared only by G-FORK-LEDGER"
            )
    ledger_entries = [
        entry for entry in declarers if entry.entry_id == "G-FORK-LEDGER"
    ]
    if len(ledger_entries) != 1:
        if len(ledger_entries) > 1:
            for entry in ledger_entries:
                entry.problems.append(
                    "Repaired-Conflict-Merges requires one unique G-FORK-LEDGER entry"
                )
        return []
    entry = ledger_entries[0]
    tokens = [
        token
        for token in _SPEC_SPLIT.split(entry.fields["Repaired-Conflict-Merges"])
        if token
    ]
    if not tokens:
        entry.problems.append("Repaired-Conflict-Merges cannot be empty")
        return []
    repaired: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            entry.problems.append(
                f"duplicate Repaired-Conflict-Merges declaration: {token}"
            )
            continue
        seen.add(token)
        if not _FULL_SHA.fullmatch(token):
            entry.problems.append(
                "Repaired-Conflict-Merges requires full 40-character object IDs: "
                f"{token}"
            )
            continue
        if token in sync_set:
            entry.problems.append(
                "Repaired-Conflict-Merges commit is already a clean upstream sync: "
                f"{token}"
            )
            continue
        if token not in work_shas:
            entry.problems.append(
                "Repaired-Conflict-Merges commit is outside evaluated work range: "
                f"{token}"
            )
            continue
        parents = _git(repo, "show", "-s", "--format=%P", token).split()
        if len(parents) != 2:
            entry.problems.append(
                "Repaired-Conflict-Merges commit must have exactly two parents: "
                f"{token}"
            )
            continue
        first_parent, upstream_parent = parents
        if not _is_ancestor(repo, upstream_parent, upstream_oid):
            entry.problems.append(
                "Repaired-Conflict-Merges upstream parent is not an ancestor of "
                f"upstream: {token}"
            )
            continue
        if _is_ancestor(repo, first_parent, upstream_oid):
            entry.problems.append(
                "Repaired-Conflict-Merges first parent must not be upstream: "
                f"{token}"
            )
            continue
        conflict_paths = _merge_conflict_paths(repo, first_parent, upstream_parent)
        if not conflict_paths:
            entry.problems.append(
                "Repaired-Conflict-Merges commit is not a conflicted upstream merge: "
                f"{token}"
            )
            continue
        missing = [
            path
            for path in _tip_missing_upstream_lines(
                repo, conflict_paths, upstream_oid, fork_oid
            )
            if path not in deleted_upstream_blobs
        ]
        if missing:
            entry.problems.append(
                f"Repaired-Conflict-Merges commit {token} does not keep current "
                "upstream lines: " + ", ".join(missing)
            )
            continue
        repaired.append(token)
    return repaired



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
        and _is_clean_upstream_sync(
            repo,
            sha,
            parents,
            upstream_oid,
            fork_tree=fork_oid,
            ledger_entries=entries,
        )
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
    fork_oid = _git(repo, "rev-parse", "--verify", f"{fork_ref}^{{commit}}")
    work: list[dict[str, str]] = []
    sync_merges: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, parents_raw, subject = line.split("\x00", 2)
        parents = parents_raw.split()
        if _is_clean_upstream_sync(
            repo, sha, parents, upstream_ref, fork_tree=fork_oid
        ):
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



def _apply_cross_owner_commits(
    repo: Path,
    entries: list[LedgerEntry],
    work_shas: set[str],
    path_cache: dict[str, set[str]],
    effective_owners: dict[str, str],
    claims: dict[str, list[str]],
    ledger_rel: str,
) -> None:
    """Map a published commit that already has owners, but more than one.

    This does not hide first-parent work and does not invent an owner. A
    single-owner commit must use a normal claim. An unowned path stays a
    failure.
    """
    others = [
        entry for entry in entries
        if entry.entry_id != "G-FORK-LEDGER" and "Cross-Owner-Commits" in entry.fields
    ]
    for entry in others:
        entry.problems.append(
            "Cross-Owner-Commits may be declared only by G-FORK-LEDGER"
        )
    ledger_entries = [entry for entry in entries if entry.entry_id == "G-FORK-LEDGER"]
    if len(ledger_entries) != 1:
        return
    entry = ledger_entries[0]
    raw = entry.fields.get("Cross-Owner-Commits", "")
    if not raw:
        return
    seen: set[str] = set()
    for token in (part for part in _SPEC_SPLIT.split(raw) if part):
        if token in seen:
            entry.problems.append(
                f"duplicate Cross-Owner-Commits declaration: {token}"
            )
            continue
        seen.add(token)
        if not _FULL_SHA.fullmatch(token):
            entry.problems.append(
                "Cross-Owner-Commits requires full 40-character object IDs: "
                f"{token}"
            )
            continue
        if token not in work_shas:
            entry.problems.append(
                "Cross-Owner-Commits commit is outside evaluated work range: "
                f"{token}"
            )
            continue
        if token not in path_cache:
            path_cache[token] = commit_changed_paths(repo, token)
        paths = path_cache[token]
        if ledger_rel in paths:
            entry.problems.append(
                f"Cross-Owner-Commits commit changes the ledger: {token}"
            )
            continue
        missing = sorted(path for path in paths if path not in effective_owners)
        if missing:
            entry.problems.append(
                f"Cross-Owner-Commits commit {token} has unowned or ambiguous "
                "paths: " + ", ".join(missing)
            )
            continue
        owners = {effective_owners[path] for path in paths}
        if len(owners) < 2:
            entry.problems.append(
                "Cross-Owner-Commits commit has a single effective owner: "
                f"{token}"
            )
            continue
        if token in claims:
            entry.problems.append(
                f"Cross-Owner-Commits commit is already claimed: {token}"
            )
            continue
        claims.setdefault(token, []).append(entry.entry_id)


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
    deleted_upstream_blobs = _classify_upstream_line_deletions(
        repo, entries, upstream_oid, fork_oid
    )
    repaired = _classify_repaired_conflict_merges(
        repo, entries, upstream_oid, fork_oid, work_shas, sync_set,
        deleted_upstream_blobs,
    )
    repaired_set = set(repaired)
    work = [commit for commit in work if commit["sha"] not in repaired_set]
    work_shas -= repaired_set
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

    _apply_cross_owner_commits(
        repo,
        entries,
        work_shas,
        path_cache,
        effective_owners,
        claims,
        ledger_rel,
    )
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
            "fork_only_commits": len(work_shas | sync_set | history_set | repaired_set),
            "work_commits": len(work),
            "sync_merges": len(sync_set),
            "repaired_conflict_merges": len(repaired),
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
        "repaired_conflict_merges": repaired,
        "history_reconciliations": history_reconciliations,
        "partition": {
            "work": sorted(work_shas),
            "sync": sorted(sync_set),
            "history": sorted(history_set),
            "repaired": sorted(repaired_set),
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
