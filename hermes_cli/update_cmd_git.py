"""Git plumbing for ``hermes update``: fork/upstream sync, trampoline-git detection, lockfile/EOL churn cleanup, orphan rescue refs, parked-branch assessment, fetch-failure classification.

Split out of ``update_cmd.py``, which re-imports every name so ``hermes_cli.update_cmd.<name>``
still resolves/monkeypatches. Origin helpers are imported lazily per function (no cycle;
test patches on ``update_cmd`` stay effective).
"""

import logging
import re
from contextlib import suppress
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(
    "hermes_cli.update_cmd"
)  # log-record parity with the origin module

_ORPHAN_RESCUE_REFS_TO_KEEP = 10
_ORPHAN_RESCUE_REF_MAX_AGE_DAYS = 30

_GIT_TEXT_KW = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")
_BAR = "=" * 68
_UPSTREAM_ADD_CMD = (
    "git remote add upstream https://github.com/NousResearch/hermes-agent.git"
)


_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_RECOVERY_REF_RE = re.compile(r"^refs/tags/pre-upstream-sync-[0-9]{8}-[0-9]{6}$")
_SYNC_OPERATION_MARKERS = {
    "merge": "MERGE_HEAD",
    "revert": "REVERT_HEAD",
    "cherry-pick": "CHERRY_PICK_HEAD",
    "rebase-head": "REBASE_HEAD",
    "rebase": "rebase-merge",
    "rebase-apply": "rebase-apply",
    "sequencer": "sequencer",
    "bisect": "BISECT_LOG",
    "bisect-start": "BISECT_START",
}


@dataclass(frozen=True)
class GitCommandObservation:
    """Exact result of one Git boundary, including spawn failures."""

    state: str
    returncode: int | None
    stdout: str
    stderr: str
    exception: str

    @property
    def detail(self) -> str:
        fields = [f"state={self.state}", f"returncode={self.returncode!r}"]
        if self.stdout:
            fields.append(f"stdout={self.stdout}")
        if self.stderr:
            fields.append(f"stderr={self.stderr}")
        if self.exception:
            fields.append(f"exception={self.exception}")
        return "; ".join(fields)


@dataclass(frozen=True)
class UpstreamSyncOutcome:
    """Immutable evidence for one fork-upstream synchronization attempt."""

    phase: str
    status: str
    pre_sha: str | None
    post_sha: str | None
    clean: bool | None
    operation_state: str
    recovery_ref: str | None
    error: str
    local_integration_completed: bool = False

    @property
    def proof_complete(self) -> bool:
        return (
            self.pre_sha is not None
            and self.post_sha is not None
            and isinstance(self.pre_sha, str)
            and _FULL_SHA_RE.fullmatch(self.pre_sha) is not None
            and isinstance(self.post_sha, str)
            and _FULL_SHA_RE.fullmatch(self.post_sha) is not None
            and self.clean is True
            and self.operation_state == "none"
        )

    @property
    def safe_to_continue(self) -> bool:
        if not self.proof_complete or self.error:
            return False
        if self.status in {"noop", "not_checked"}:
            return self.pre_sha == self.post_sha
        return self.status == "updated" and self.pre_sha != self.post_sha

    @property
    def safe_to_restore(self) -> bool:
        return (
            self.status == "failed"
            and self.proof_complete
            and self.pre_sha == self.post_sha
        )

    @property
    def changed(self) -> bool:
        return self.proof_complete and self.pre_sha != self.post_sha


def _capture_checkout_proof(
    git_cmd: list[str], cwd: Path
) -> tuple[bool | None, str, str]:
    """Prove a clean index/worktree and the absence of an active Git operation."""
    from hermes_cli.update_cmd import _git_run

    try:
        status = _git_run(
            git_cmd, ["status", "--porcelain=v1", "--untracked-files=all"], cwd
        )
        if status.returncode != 0:
            return None, "unknown", (status.stderr or "git status failed").strip()
        git_dir_result = _git_run(git_cmd, ["rev-parse", "--absolute-git-dir"], cwd)
        git_dir_text = git_dir_result.stdout.strip()
        if git_dir_result.returncode != 0 or not git_dir_text:
            return (
                not bool(status.stdout.strip()),
                "unknown",
                (
                    git_dir_result.stderr
                    or "could not prove the absolute Git operation directory"
                ).strip(),
            )
        git_dir = Path(git_dir_text)
        active = [
            name
            for name, marker in _SYNC_OPERATION_MARKERS.items()
            if (git_dir / marker).exists()
        ]
        return (
            not bool(status.stdout.strip()),
            ",".join(active) if active else "none",
            "",
        )
    except Exception as exc:
        return None, "unknown", str(exc)


def _safe_recovery_ref(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if _FULL_SHA_RE.fullmatch(value) or _SAFE_RECOVERY_REF_RE.fullmatch(value):
        return value
    return None


def _finish_sync_outcome(
    git_cmd: list[str],
    cwd: Path,
    *,
    phase: str,
    status: str,
    pre_sha: str | None,
    recovery_ref: str | None,
    error: str = "",
    local_integration_completed: bool = False,
) -> UpstreamSyncOutcome:
    """Capture post-state and make incomplete or contradictory evidence fail closed."""
    from hermes_cli.update_cmd import _capture_checkout_proof, _capture_head_sha

    post_sha = _capture_head_sha(git_cmd, cwd)
    clean, operation_state, proof_error = _capture_checkout_proof(git_cmd, cwd)
    evidence_errors = [
        message
        for message in (error, proof_error)
        if isinstance(message, str) and message
    ]
    if proof_error and not isinstance(proof_error, str):
        evidence_errors.append("invalid checkout-proof error detail")
    if not isinstance(pre_sha, str) or _FULL_SHA_RE.fullmatch(pre_sha) is None:
        evidence_errors.append("missing or invalid pre-sync HEAD SHA")
    if not isinstance(post_sha, str) or _FULL_SHA_RE.fullmatch(post_sha) is None:
        evidence_errors.append("missing or invalid post-sync HEAD SHA")
    if clean is not True:
        evidence_errors.append(
            "worktree or index is dirty or could not be proven clean"
        )
    if operation_state != "none":
        evidence_errors.append(f"Git operation state is {operation_state}")
    if status in {"noop", "not_checked"} and pre_sha != post_sha:
        evidence_errors.append("a no-op synchronization changed HEAD")
    if status == "updated" and pre_sha == post_sha:
        evidence_errors.append("an updated synchronization did not change HEAD")
    if evidence_errors and status not in {
        "failed",
        "rollback_failed",
        "outcome_unknown",
    }:
        status = "failed"
    return UpstreamSyncOutcome(
        phase=phase,
        status=status,
        pre_sha=pre_sha,
        post_sha=post_sha,
        clean=clean,
        operation_state=operation_state,
        recovery_ref=_safe_recovery_ref(recovery_ref),
        error="; ".join(dict.fromkeys(evidence_errors)),
        local_integration_completed=local_integration_completed,
    )


def _git_ok(git_cmd, args, cwd, **kw) -> bool:
    """True when ``_git_run`` exits 0; any exception counts as failure."""
    return _git_stdout(git_cmd, args, cwd, **kw) is not None


def _git_stdout(git_cmd, args, cwd, **kw) -> Optional[str]:
    """Stripped stdout of a successful ``_git_run``; ``None`` on non-zero exit or any exception."""
    from hermes_cli.update_cmd import _git_run

    with suppress(Exception):
        result = _git_run(git_cmd, args, cwd, **kw)
        if result.returncode == 0:
            return result.stdout.strip()
    return None


def _prune_orphan_rescue_refs(
    git_cmd,
    cwd,
    branch,
    keep=_ORPHAN_RESCUE_REFS_TO_KEEP,
    max_age_days=_ORPHAN_RESCUE_REF_MAX_AGE_DAYS,
) -> None:
    """Expire old orphan rescue refs (``refs/hermes-update-backups/orphan-<branch>-<ts>-<sha>``).

    Each ref pins a possibly multi-GB snapshot against ``git gc``, so a repeatedly corrupted install would
    grow ``.git`` unbounded. Keep the ``keep`` newest AND drop any older than ``max_age_days`` by the
    ``YYYYMMDD-HHMMSS`` stamp (unparseable names left alone); names sort chronologically so
    ``for-each-ref`` order is creation order. Best-effort, never blocks.

    A rescue ref pins every object reachable from that commit against ``git gc`` — and in the incident shape
    those objects include a full working-tree snapshot (the autostash orphan commit), which can be multi-GB
    when the tree holds large stray files. See #87694.
    """
    from hermes_cli.update_cmd import _git_run

    with suppress(OSError):
        prefix = f"refs/hermes-update-backups/orphan-{branch}-"
        list_result = _git_run(
            git_cmd,
            ["for-each-ref", "--format=%(refname)", "--sort=refname", f"{prefix}*"],
            cwd,
        )
        if list_result.returncode != 0:
            return
        refs = [
            line.strip() for line in list_result.stdout.splitlines() if line.strip()
        ]
        stale = set(refs[:-keep] if keep > 0 else refs)
        if max_age_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            for ref in refs:
                with suppress(ValueError):
                    if (
                        datetime.strptime(
                            ref[len(prefix) :][:15], "%Y%m%d-%H%M%S"
                        ).replace(tzinfo=timezone.utc)
                        < cutoff
                    ):
                        stale.add(ref)
        for ref in sorted(stale):
            _git_run(git_cmd, ["update-ref", "-d", ref], cwd)


def _branch_head_label(git_cmd=None, cwd=None) -> str | None:
    """``"<branch> @ <short-sha>"`` for the checkout (``detached`` when not on a branch), or None. Never raises.

    Appended to summary lines so a checkout parked on a stale branch is visible."""
    from hermes_cli.update_cmd import _m

    try:
        cmd = list(git_cmd) if git_cmd else ["git"]
        root = cwd if cwd is not None else _m().PROJECT_ROOT

        def _rev_parse(*args):
            return subprocess.run(cmd + ["rev-parse", *args], cwd=root, **_GIT_TEXT_KW)

        branch, sha = _rev_parse("--abbrev-ref", "HEAD"), _rev_parse("--short", "HEAD")
        branch_name, sha_text = branch.stdout.strip(), sha.stdout.strip()
        if (
            branch.returncode != 0
            or sha.returncode != 0
            or not sha_text
            or not branch_name
        ):
            return None
        return f"{'detached' if branch_name == 'HEAD' else branch_name} @ {sha_text}"
    except Exception:
        return None


def _branch_head_suffix(git_cmd=None, cwd=None) -> str:
    """`` [<branch> @ <sha>]`` suffix for summary lines ("" when unknown)."""
    label = _branch_head_label(git_cmd, cwd)
    return f" [{label}]" if label else ""


def _assess_parked_branch_switch(
    git_cmd: list[str], cwd: Path, current_branch: str, target_branch: str
) -> tuple[bool, str]:
    """Decide whether a parked feature branch may be auto-switched back to the update target.

    - (True, "") — tree clean and every parked commit is in ``origin/<target>`` (no ``git cherry +``).
    - (True, "unmerged:<n>") — tree clean but commits not in target; switching is safe (checkout keeps
      committed work) but caller must print a LOUD notice. Non-interactive callers (desktop, gateway
      /update, cron) can't resolve a skip, so a clean checkout must reach target.
    - (False, "disabled"|"dirty"|"unverifiable") — caller must NOT touch the branch. Dirty is the
      genuinely unsafe case: uncommitted work riding an autostash across branches.
    A config read failure must not disable the safety checks: fall through with the default."""
    from hermes_cli.update_cmd import _git_run

    try:
        from hermes_cli.config import load_config

        _update_cfg = (load_config() or {}).get("updates", {})
        if isinstance(_update_cfg, dict) and not bool(
            _update_cfg.get("auto_switch_parked_branch", True)
        ):
            return False, "disabled"
    except Exception as exc:
        logger.debug("Could not read updates.auto_switch_parked_branch: %s", exc)
    status = _git_run(git_cmd, ["status", "--porcelain"], cwd)
    if status.returncode != 0:
        return False, "unverifiable"
    if status.stdout.strip():
        return False, "dirty"
    cherry = _git_run(git_cmd, ["cherry", f"origin/{target_branch}"], cwd)
    if cherry.returncode != 0:
        return False, "unverifiable"
    unmerged = [line for line in cherry.stdout.splitlines() if line.startswith("+")]
    return True, f"unmerged:{len(unmerged)}" if unmerged else ""


_PARKED_SKIP_WHY = {
    "dirty": "the working tree has uncommitted changes",
    "disabled": "updates.auto_switch_parked_branch is set to false in config.yaml",
}


def _print_parked_branch_skip_warning(
    git_cmd: list[str], cwd: Path, current_branch: str, target_branch: str, reason: str
) -> None:
    """LOUD block: why the update was skipped on a parked branch, behind-count, fix commands."""
    behind = None
    with suppress(Exception):
        behind_text = _git_stdout(
            git_cmd, ["rev-list", f"HEAD..origin/{target_branch}", "--count"], cwd
        )
        if behind_text:
            behind = int(behind_text)
    why = _PARKED_SKIP_WHY.get(
        reason, f"the branch state could not be verified against origin/{target_branch}"
    )
    print(f"\n{_BAR}\n⚠ CODE UPDATE SKIPPED — checkout is parked on '{current_branch}'")
    print(f"  Not auto-switching to {target_branch}: {why}.")
    if behind is not None and behind > 0:
        print(
            f"  This checkout is {behind} commit(s) BEHIND origin/{target_branch} — the code you are running is stale."
        )
    print(
        f"\n  To resolve, inspect the branch and switch back yourself:\n"
        f"    git -C {cwd} status\n"
        f"    git -C {cwd} checkout {target_branch} && hermes update\n"
        f"  (commit or stash your work on the branch first if you want to keep it)\n{_BAR}"
    )


def _print_parked_branch_kept_notice(
    current_branch: str, target_branch: str, unmerged_count: str
) -> None:
    """LOUD notice when a clean parked branch with unmerged commits is auto-switched.

    Non-interactive callers can't resolve a skip, so we proceed — but the unmerged work
    (still safe on its branch) must be impossible to miss."""
    print(
        f"\n{_BAR}\n"
        f"⚠ Checkout was parked on '{current_branch}' with "
        f"{unmerged_count} commit(s) not merged into origin/{target_branch}.\n"
        f"  Switching to {target_branch} so the update can proceed — your "
        f"commit(s) are safe on '{current_branch}'.\n\n"
        f"  To pick the work back up later:\n    git checkout {current_branch}\n{_BAR}"
    )


OFFICIAL_REPO_URLS = {
    "https://github.com/NousResearch/hermes-agent.git",
    "git@github.com:NousResearch/hermes-agent.git",
    "https://github.com/NousResearch/hermes-agent",
    "git@github.com:NousResearch/hermes-agent",
}
OFFICIAL_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"
SKIP_UPSTREAM_PROMPT_FILE = ".skip_upstream_prompt"


def _get_origin_url(git_cmd: list[str], cwd: Path) -> Optional[str]:
    """Get the URL of the origin remote, or None if not set."""
    return _git_stdout(git_cmd, ["remote", "get-url", "origin"], cwd)


def _is_fork(origin_url: Optional[str]) -> bool:
    """Check if the origin remote points to a fork (not the official repo)."""
    if not origin_url:
        return False

    def _norm(url: str) -> str:
        url = url.rstrip("/")
        return url[:-4] if url.endswith(".git") else url

    return _norm(origin_url) not in {_norm(official) for official in OFFICIAL_REPO_URLS}


def _observe_git_command(
    git_cmd: list[str],
    args: list[str],
    cwd: Path,
    *,
    absent_returncodes: set[int] | None = None,
    network: bool = False,
) -> GitCommandObservation:
    """Capture a Git command without collapsing non-zero and spawn failure evidence."""
    from hermes_cli.update_cmd import _git_run

    try:
        result = _git_run(git_cmd, args, cwd, network=network)
    except Exception as exc:
        return GitCommandObservation(
            state="failed",
            returncode=None,
            stdout="",
            stderr="",
            exception=f"{type(exc).__name__}: {exc}",
        )
    returncode = int(result.returncode)
    state = (
        "succeeded"
        if returncode == 0
        else "absent"
        if returncode in (absent_returncodes or set())
        else "failed"
    )
    return GitCommandObservation(
        state=state,
        returncode=returncode,
        stdout=str(result.stdout or ""),
        stderr=str(result.stderr or ""),
        exception="",
    )


def _observe_upstream_remote(git_cmd: list[str], cwd: Path) -> GitCommandObservation:
    """Observe upstream presence: success, confirmed absence, or probe failure."""
    return _observe_git_command(
        git_cmd, ["remote", "get-url", "upstream"], cwd, absent_returncodes={2}
    )


def _add_upstream_remote_observed(
    git_cmd: list[str], cwd: Path
) -> GitCommandObservation:
    return _observe_git_command(
        git_cmd, ["remote", "add", "upstream", OFFICIAL_REPO_URL], cwd
    )


def _push_fork_with_upstream_observed(
    git_cmd: list[str], cwd: Path
) -> GitCommandObservation:
    return _observe_git_command(
        git_cmd,
        ["push", "origin", "main"],
        cwd,
        network=True,
    )


def _verify_origin_main_postcondition(
    git_cmd: list[str], cwd: Path, expected_sha: str | None
) -> GitCommandObservation:
    """Prove that the pushed ``origin/main`` resolves to the integrated local HEAD."""
    if (
        not isinstance(expected_sha, str)
        or _FULL_SHA_RE.fullmatch(expected_sha) is None
    ):
        return GitCommandObservation(
            state="failed",
            returncode=None,
            stdout="",
            stderr="",
            exception="missing or invalid integrated local HEAD SHA",
        )
    observation = _observe_git_command(
        git_cmd,
        ["ls-remote", "--exit-code", "origin", "refs/heads/main"],
        cwd,
        network=True,
    )
    if observation.state != "succeeded":
        return observation
    fields = observation.stdout.strip().split()
    actual_sha = fields[0] if len(fields) == 2 else None
    actual_ref = fields[1] if len(fields) == 2 else None
    if (
        actual_sha is None
        or _FULL_SHA_RE.fullmatch(actual_sha) is None
        or actual_ref != "refs/heads/main"
        or actual_sha.lower() != expected_sha.lower()
    ):
        return GitCommandObservation(
            state="failed",
            returncode=observation.returncode,
            stdout=observation.stdout,
            stderr=observation.stderr,
            exception=(
                "origin/main postcondition mismatch: "
                f"expected={expected_sha}; observed={actual_sha or '<unresolved>'}"
            ),
        )
    return observation


def _has_upstream_remote(git_cmd: list[str], cwd: Path) -> bool:
    """Check if an 'upstream' remote already exists."""
    return _git_ok(git_cmd, ["remote", "get-url", "upstream"], cwd)


def _add_upstream_remote(git_cmd: list[str], cwd: Path) -> bool:
    """Add the official repo as the 'upstream' remote. Returns True on success."""
    return _git_ok(git_cmd, ["remote", "add", "upstream", OFFICIAL_REPO_URL], cwd)


def _count_commits_between(git_cmd: list[str], cwd: Path, base: str, head: str) -> int:
    """Count commits on `head` that are not on `base`. Returns -1 on error."""
    with suppress(Exception):
        count = _git_stdout(git_cmd, ["rev-list", "--count", f"{base}..{head}"], cwd)
        if count is not None:
            return int(count)
    return -1


def _should_skip_upstream_prompt() -> bool:
    """Check if user previously declined to add upstream."""
    from hermes_constants import get_hermes_home

    return (get_hermes_home() / SKIP_UPSTREAM_PROMPT_FILE).exists()


def _mark_skip_upstream_prompt():
    """Create marker file to skip future upstream prompts."""
    with suppress(Exception):
        from hermes_constants import get_hermes_home

        (get_hermes_home() / SKIP_UPSTREAM_PROMPT_FILE).touch()


def _sync_fork_with_upstream(git_cmd: list[str], cwd: Path) -> bool:
    """Push updated main to origin (sync fork); True on success."""
    return _git_ok(git_cmd, ["push", "origin", "main"], cwd, network=True)


_FORK_SYNC_TEST_PATHS = (
    "tests/hermes_cli/test_fork_sync_strategy.py",
    "tests/hermes_cli/test_update_post_pull_syntax_guard.py",
)


def _subprocess_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Return up to eight lines from each output stream, preserving both."""
    sections: list[str] = []
    for label, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
        lines = stream.strip().splitlines()
        if lines:
            sections.append(f"{label}:\n" + "\n".join(lines[-8:]))
    return "\n".join(sections)


def _run_fork_sync_tests(cwd: Path) -> tuple[bool, str]:
    """Bootstrap and run credential-free updater tests before a fork push."""
    python = sys.executable
    try:
        probe = subprocess.run(
            [python, "-c", "import pytest, pytest_asyncio"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if probe.returncode != 0:
            from hermes_cli.managed_uv import ensure_uv

            uv_bin = ensure_uv()
            if not uv_bin:
                return (
                    False,
                    "uv is unavailable; cannot install updater test dependencies",
                )
            install = subprocess.run(
                [
                    str(uv_bin),
                    "pip",
                    "install",
                    "--python",
                    python,
                    "pytest==9.1.1",
                    "pytest-asyncio==1.3.0",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5 * 60,
            )
            if install.returncode != 0:
                return False, _subprocess_detail(install)

        result = subprocess.run(
            [python, "-m", "pytest", *_FORK_SYNC_TEST_PATHS, "-q"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15 * 60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    return False, _subprocess_detail(result)


def _rollback_fork_sync_candidate(
    git_cmd: list[str], cwd: Path, rollback_ref: str
) -> bool:
    """Reset a failed upstream candidate and report whether rollback succeeded."""
    from hermes_cli.update_cmd import _no_prompt_git_kwargs

    try:
        rollback_result = subprocess.run(
            git_cmd + ["reset", "--hard", rollback_ref],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_no_prompt_git_kwargs(),
        )
    except OSError as exc:
        print(f"  ✗ Rollback could not start: {exc}")
        return False
    if rollback_result.returncode == 0:
        print(
            f"  ✓ Rolled back to {rollback_ref[:10]} - nothing was pushed to your fork."
        )
        return True
    print("  ✗ Rollback failed. Recover manually with:")
    print(f"    cd {cwd} && git reset --hard {rollback_ref}")
    if rollback_result.stderr.strip():
        print(f"    ({rollback_result.stderr.strip().splitlines()[0]})")
    return False


def _validate_fork_sync_candidate(
    git_cmd: list[str],
    cwd: Path,
    rollback_ref: str,
    *,
    rollback_on_failure: bool = True,
) -> bool:
    """Validate merged upstream code and roll it back on any owned failure."""
    from hermes_cli.update_cmd import _validate_critical_files_syntax

    def fail() -> bool:
        if rollback_on_failure:
            _rollback_fork_sync_candidate(git_cmd, cwd, rollback_ref)
        return False

    syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(cwd)
    if not syntax_ok:
        print("\n  ✗ Merged code has a syntax error in a critical file:")
        print(f"    {failing_path}")
        if syntax_error:
            for line in str(syntax_error).splitlines()[:6]:
                print(f"      {line}")
        return fail()

    print("→ Running targeted updater tests before syncing the fork...")
    tests_ok, test_detail = _run_fork_sync_tests(cwd)
    if not tests_ok:
        print("  ✗ Fork sync targeted updater tests failed:")
        if test_detail:
            for line in test_detail.splitlines():
                print(f"    {line}")
        return fail()
    print("  ✓ Targeted updater tests passed")
    return True


def _offer_upstream_remote(
    git_cmd: list[str], cwd: Path, *, assume_yes: bool, input_fn
) -> str:
    """Offer upstream setup and distinguish availability, decline, and mutation failure."""
    from hermes_cli.update_cmd import _add_upstream_remote, _mark_skip_upstream_prompt

    print(
        "\nℹ Your fork is not tracking the official Hermes repository.\n"
        "  This means you may miss updates from NousResearch/hermes-agent.\n"
    )
    if assume_yes or (
        input_fn is None and not (sys.stdin.isatty() and sys.stdout.isatty())
    ):
        print(
            "  Skipping upstream setup (non-interactive run).\n"
            f"  Add it later with: {_UPSTREAM_ADD_CMD}"
        )
        return "declined"
    if input_fn is not None:
        response = (
            input_fn("Add official repo as 'upstream' remote? [y/N]", "n")
            .strip()
            .lower()
        )
    else:
        try:
            response = (
                input("Add official repo as 'upstream' remote? [Y/n]: ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt, UnicodeDecodeError):
            print()
            response = "n"
    if response not in {"", "y", "yes"}:
        print(f"  Skipped. Run '{_UPSTREAM_ADD_CMD}' to add later.")
        _mark_skip_upstream_prompt()
        return "declined"
    print("→ Adding upstream remote...")
    if not _add_upstream_remote(git_cmd, cwd):
        print("  ✗ Failed to add upstream remote. Upstream sync failed.")
        return "failed"
    print("  ✓ Added upstream: https://github.com/NousResearch/hermes-agent.git")
    return "available"


def _offer_upstream_remote_observed(
    git_cmd: list[str], cwd: Path, *, assume_yes: bool, input_fn
) -> tuple[str, str]:
    """Offer setup and retain exact add-command evidence on failure."""
    from hermes_cli.update_cmd import (
        _add_upstream_remote_observed,
        _mark_skip_upstream_prompt,
    )

    print(
        "\nℹ Your fork is not tracking the official Hermes repository.\n"
        "  This means you may miss updates from NousResearch/hermes-agent.\n"
    )
    if assume_yes or (
        input_fn is None and not (sys.stdin.isatty() and sys.stdout.isatty())
    ):
        print(
            "  Skipping upstream setup (non-interactive run).\n"
            f"  Add it later with: {_UPSTREAM_ADD_CMD}"
        )
        return "declined", ""
    if input_fn is not None:
        response = (
            input_fn("Add official repo as 'upstream' remote? [y/N]", "n")
            .strip()
            .lower()
        )
    else:
        try:
            response = (
                input("Add official repo as 'upstream' remote? [Y/n]: ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt, UnicodeDecodeError):
            print()
            response = "n"
    if response not in {"", "y", "yes"}:
        print(f"  Skipped. Run '{_UPSTREAM_ADD_CMD}' to add later.")
        _mark_skip_upstream_prompt()
        return "declined", ""
    print("→ Adding upstream remote...")
    result = _add_upstream_remote_observed(git_cmd, cwd)
    if result.state != "succeeded":
        print("  ✗ Failed to add upstream remote. Upstream sync failed.")
        return "failed", result.detail
    print("  ✓ Added upstream: https://github.com/NousResearch/hermes-agent.git")
    return "available", ""


def _fork_sync_strategy() -> str:
    """Return the validated fork synchronization policy."""
    try:
        from hermes_cli.config import load_config

        updates = (load_config() or {}).get("updates", {})
        if isinstance(updates, dict):
            value = str(updates.get("fork_sync_strategy", "ff_only")).strip().lower()
            if value in {"ff_only", "merge"}:
                return value
    except Exception as exc:
        logger.debug("Could not read updates.fork_sync_strategy: %s", exc)
    return "ff_only"


def _sync_with_upstream_observed(
    git_cmd: list[str],
    cwd: Path,
    *,
    phase: str,
    assume_yes: bool = False,
    input_fn=None,
    _upstream_available: bool = False,
) -> UpstreamSyncOutcome:
    """Run the complete mutating sync behind a BaseException recovery boundary."""
    from hermes_cli.update_cmd import _capture_head_sha

    pre_sha: str | None = None
    try:
        pre_sha = _capture_head_sha(git_cmd, cwd)
        return _sync_with_upstream_observed_impl(
            git_cmd,
            cwd,
            phase=phase,
            pre_sha=pre_sha,
            assume_yes=assume_yes,
            input_fn=input_fn,
            _upstream_available=_upstream_available,
        )
    except BaseException as exc:
        return _finish_sync_outcome(
            git_cmd,
            cwd,
            phase=phase,
            status="outcome_unknown",
            pre_sha=pre_sha,
            recovery_ref=pre_sha,
            error=f"{type(exc).__name__}: {exc}",
        )


def _sync_with_upstream_observed_impl(
    git_cmd: list[str],
    cwd: Path,
    *,
    phase: str,
    pre_sha: str | None,
    assume_yes: bool = False,
    input_fn=None,
    _upstream_available: bool = False,
) -> UpstreamSyncOutcome:
    """Attempt one fork sync and return complete, immutable checkout evidence."""
    from hermes_cli.update_cmd import (
        _capture_checkout_proof,
        _count_commits_between,
        _no_prompt_git_kwargs,
        _observe_upstream_remote,
        _offer_upstream_remote_observed,
        _push_fork_with_upstream_observed,
        _should_skip_upstream_prompt,
    )

    clean_before, operation_before, proof_error = _capture_checkout_proof(git_cmd, cwd)

    def finish(
        status: str,
        error: str = "",
        recovery_ref: str | None = None,
        *,
        local_integration_completed: bool = False,
    ) -> UpstreamSyncOutcome:
        detail = "; ".join(
            message
            for message in (error, proof_error)
            if isinstance(message, str) and message
        )
        if proof_error and not isinstance(proof_error, str):
            detail = "; ".join(
                message
                for message in (detail, "invalid checkout-proof error detail")
                if message
            )
        return _finish_sync_outcome(
            git_cmd,
            cwd,
            phase=phase,
            status=status,
            pre_sha=pre_sha,
            recovery_ref=recovery_ref or pre_sha,
            error=detail,
            local_integration_completed=local_integration_completed,
        )

    if not isinstance(pre_sha, str) or _FULL_SHA_RE.fullmatch(pre_sha) is None:
        print("  ✗ Could not capture the pre-sync HEAD. Upstream sync failed.")
        return finish("failed", "missing or invalid pre-sync HEAD SHA")
    if clean_before is not True or operation_before != "none":
        return finish("failed", "pre-sync checkout is not clean and operation-free")
    if not _upstream_available:
        remote = _observe_upstream_remote(git_cmd, cwd)
        if remote.state == "failed":
            return finish("failed", f"upstream remote probe failed: {remote.detail}")
        if remote.state == "absent":
            if _should_skip_upstream_prompt():
                return finish("not_checked")
            remote_outcome, remote_error = _offer_upstream_remote_observed(
                git_cmd, cwd, assume_yes=assume_yes, input_fn=input_fn
            )
            if remote_outcome == "failed":
                return finish(
                    "failed", f"upstream remote creation failed: {remote_error}"
                )
            if remote_outcome != "available":
                return finish("not_checked")

    print("\n→ Fetching upstream...")
    try:
        subprocess.run(
            git_cmd + ["fetch", "upstream", "main", "--quiet"],
            cwd=cwd,
            capture_output=True,
            check=True,
            **_no_prompt_git_kwargs(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print("  ✗ Failed to fetch upstream. Upstream sync failed.")
        return finish("failed", f"upstream fetch failed: {exc}")

    origin_ahead = _count_commits_between(git_cmd, cwd, "upstream/main", "origin/main")
    upstream_ahead = _count_commits_between(
        git_cmd, cwd, "origin/main", "upstream/main"
    )
    if origin_ahead < 0 or upstream_ahead < 0:
        print("  ✗ Could not compare branches. Upstream sync failed.")
        return finish("failed", "could not compare origin/main with upstream/main")

    strategy = _fork_sync_strategy()
    if origin_ahead > 0 and strategy != "merge":
        print(
            f"\nℹ Your fork has {origin_ahead} commit(s) not on upstream.\n"
            "  Skipping upstream sync to preserve your changes.\n"
            "  If you want to merge upstream changes, run:\n    git pull upstream main\n"
            "  (set updates.fork_sync_strategy: merge in config.yaml to do this automatically)"
        )
        return finish("noop")
    if upstream_ahead == 0:
        print("  ✓ Fork is up to date with upstream")
        return finish("noop")

    sync_tag = f"pre-upstream-sync-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    recovery_ref = f"refs/tags/{sync_tag}"
    try:
        tag_result = subprocess.run(
            git_cmd + ["tag", sync_tag],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_no_prompt_git_kwargs(),
        )
    except OSError as exc:
        print(
            "  ✗ Could not create the pre-upstream-sync recovery tag. Upstream sync failed."
        )
        return finish("failed", f"recovery tag creation failed: {exc}")
    if tag_result.returncode != 0:
        detail = (tag_result.stderr or tag_result.stdout or "unknown Git error").strip()
        print(
            "  ✗ Could not create the pre-upstream-sync recovery tag. Upstream sync failed."
        )
        print(f"  Git reported: {detail}")
        return finish("failed", f"recovery tag creation failed: {detail}")

    if origin_ahead > 0:
        print(
            f"\n→ Fork has {origin_ahead} commit(s) of its own and is "
            f"{upstream_ahead} commit(s) behind upstream"
        )
        print("→ Merging upstream/main (updates.fork_sync_strategy: merge)...")
        try:
            merge_result = subprocess.run(
                git_cmd + ["merge", "--no-edit", "upstream/main"],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_no_prompt_git_kwargs(),
            )
        except OSError as exc:
            rollback_ok = _rollback_fork_sync_candidate(git_cmd, cwd, pre_sha)
            status = "failed" if rollback_ok else "rollback_failed"
            error = f"upstream merge failed: {exc}"
            if not rollback_ok:
                error += "; rollback reset failed"
            return finish(status, error, recovery_ref)
        if merge_result.returncode != 0:
            with suppress(OSError):
                subprocess.run(
                    git_cmd + ["merge", "--abort"],
                    cwd=cwd,
                    capture_output=True,
                    check=False,
                    **_no_prompt_git_kwargs(),
                )
            rollback_ok = _rollback_fork_sync_candidate(git_cmd, cwd, pre_sha)
            print(
                "  ✗ Could not merge upstream/main (conflict or dirty tree) - "
                "sync stopped, nothing was changed."
            )
            print(f"  Resolve manually: cd {cwd} && git merge upstream/main")
            if not rollback_ok:
                return finish(
                    "rollback_failed",
                    "upstream merge failed; rollback reset failed",
                    recovery_ref,
                )
            return finish("failed", "upstream merge failed", recovery_ref)
    else:
        print(
            f"\n→ Fork is {upstream_ahead} commit(s) behind upstream\n→ Pulling from upstream..."
        )
        try:
            subprocess.run(
                git_cmd + ["pull", "--ff-only", "upstream", "main"],
                cwd=cwd,
                check=True,
                **_no_prompt_git_kwargs(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print("  ✗ Failed to pull from upstream. Upstream sync failed.")
            return finish("failed", f"upstream pull failed: {exc}", recovery_ref)

    validation_error = ""
    try:
        candidate_valid = _validate_fork_sync_candidate(
            git_cmd, cwd, pre_sha, rollback_on_failure=False
        )
    except Exception as exc:
        candidate_valid = False
        validation_error = f"candidate validation raised: {exc}"
    if not candidate_valid:
        rollback_ok = _rollback_fork_sync_candidate(git_cmd, cwd, pre_sha)
        print("  Try the sync again once the candidate passes validation.")
        error = validation_error or "upstream candidate validation failed"
        if not rollback_ok:
            return finish(
                "rollback_failed", f"{error}; rollback reset failed", recovery_ref
            )
        return finish("failed", error, recovery_ref)

    action = (
        "Merged upstream/main (your commits preserved)"
        if origin_ahead > 0
        else "Updated from upstream"
    )
    print(f"  ✓ {action}\n→ Syncing fork...")
    push_error = ""
    push = _push_fork_with_upstream_observed(git_cmd, cwd)
    if push.state == "succeeded":
        print("  ✓ Fork synced with upstream")
    else:
        push_error = (
            f"fork push failed; local upstream update is retained; {push.detail}"
        )
        print(
            "  ℹ Updated locally but couldn't push to fork (no write access?)\n"
            "    Your local repo is updated, but your fork on GitHub may be behind."
        )
    outcome = finish(
        "updated",
        push_error,
        recovery_ref,
        local_integration_completed=True,
    )
    if push.state != "succeeded" or not outcome.safe_to_continue:
        return outcome
    remote_proof = _verify_origin_main_postcondition(git_cmd, cwd, outcome.post_sha)
    if remote_proof.state == "succeeded":
        return outcome
    return UpstreamSyncOutcome(
        phase=outcome.phase,
        status="failed",
        pre_sha=outcome.pre_sha,
        post_sha=outcome.post_sha,
        clean=outcome.clean,
        operation_state=outcome.operation_state,
        recovery_ref=outcome.recovery_ref,
        error=f"origin/main postcondition was not proven; {remote_proof.detail}",
        local_integration_completed=True,
    )


def _sync_with_upstream_if_needed(
    git_cmd: list[str],
    cwd: Path,
    *,
    assume_yes: bool = False,
    input_fn=None,
) -> bool:
    """Preserve the legacy checked/not-checked contract for external callers."""
    from hermes_cli.update_cmd import _has_upstream_remote, _should_skip_upstream_prompt

    upstream_available = _has_upstream_remote(git_cmd, cwd)
    if not upstream_available:
        if _should_skip_upstream_prompt():
            return False
        upstream_available = (
            _offer_upstream_remote(
                git_cmd, cwd, assume_yes=assume_yes, input_fn=input_fn
            )
            == "available"
        )
        if not upstream_available:
            return False
    outcome = _sync_with_upstream_observed(
        git_cmd,
        cwd,
        phase="legacy",
        assume_yes=assume_yes,
        input_fn=input_fn,
        _upstream_available=True,
    )
    return outcome.status == "noop" or outcome.local_integration_completed


def _has_http_code(stderr: str, *codes: str) -> bool:
    return any(
        f"HTTP {code}" in stderr or f"returned error: {code}" in stderr
        for code in codes
    )


# Ordered (predicate, diagnosis): curl reports HTTP errors as ``unable to access '<url>': ... error: 429``,
# so rate-limit/outage checks must run BEFORE the generic "unable to access" check. An anonymous fetch
# answered with HTTP 401 ("could not read Username") is GitHub during an outage (or a renamed/private
# repo), not a user credentials problem.
_FETCH_FAILURE_RULES = (
    (
        lambda s: _has_http_code(s, "429") or "rate limit" in s.lower(),
        "✗ GitHub is rate limiting requests or having an outage (HTTP 429) — try again in 5 minutes.",
    ),
    (
        lambda s: _has_http_code(s, "500", "502", "503", "504"),
        "✗ GitHub appears to be having an outage — try again in a few minutes (https://www.githubstatus.com).",
    ),
    (
        lambda s: "Could not resolve host" in s or "unable to access" in s,
        "✗ Network error — cannot reach the remote repository.",
    ),
    (
        lambda s: "could not read Username" in s or "terminal prompts disabled" in s,
        "✗ GitHub rejected the anonymous fetch (asked for a login) — this usually means a GitHub outage;"
        " try again in a few minutes (https://www.githubstatus.com). If it persists, check"
        " `git remote -v` points at a public repo.",
    ),
    (
        lambda s: "Authentication failed" in s,
        "✗ Authentication failed — check your git credentials or SSH key.",
    ),
)


def _classify_fetch_failure(stderr: str) -> str:
    """Map git-fetch stderr to a one-line diagnosis (caller also prints the raw first line)."""
    return next(
        (message for matches, message in _FETCH_FAILURE_RULES if matches(stderr)),
        "✗ Failed to fetch updates from origin.",
    )


def _print_fetch_failure(stderr: str) -> None:
    """Print the classified diagnosis plus the first raw stderr line."""
    stderr = (stderr or "").strip()
    print(_classify_fetch_failure(stderr))
    if stderr:
        print(f"  {stderr.splitlines()[0]}")


def _probe_fork_bomb(argv: list) -> Optional[bool]:
    """Run ``<argv> --version``; True/False = guard message seen/absent, None = probe itself failed."""
    try:
        result = subprocess.run(argv + ["--version"], timeout=15, **_GIT_TEXT_KW)
    except Exception:
        return None
    return "fork bomb" in ((result.stdout or "") + (result.stderr or "")).lower()


def _git_is_trampoline(git_cmd: list) -> bool:
    """Whether *git_cmd* is a broken Git-for-Windows trampoline shim.

    The ~46KB ``bin\\git.exe``/``cmd\\git.exe`` shims re-exec git-core; when they can't find it every call
    dies with the launcher's guard message (a PATH problem, not network). Never raises; unknown states
    report False so a probe failure can't block an update.

    Git for Windows ships two ~46KB shims (``bin\\git.exe``, ``cmd\\git.exe``) that re-exec the real
    ``mingw64\\libexec\\git-core\\git.exe``. See #87876.
    """
    return _probe_fork_bomb(git_cmd) is True


def _portable_git_candidates() -> list:
    """PortableGit candidates: shared root first (where the managed tree actually lives, not the
    profile-scoped HERMES_HOME), then profile home as a fallback for custom layouts.

    The Hermes-managed PortableGit tree lives under the SHARED root (``<root>/git/...``), not the
    profile-scoped HERMES_HOME (``<root>/profiles/<name>``), so a profile-scoped ``hermes update`` must look
    there (monerostar review, #87876).
    """
    from hermes_cli.update_cmd import get_default_hermes_root, get_hermes_home

    candidates = []
    with suppress(Exception):
        candidates += [
            root / "git" / "mingw64" / "libexec" / "git-core" / "git.exe"
            for root in (get_default_hermes_root(), Path(get_hermes_home()))
        ]
    return candidates


def _locate_real_git() -> Optional[Path]:
    """Find a real Git-for-Windows ``git-core/git.exe`` (standard locations + managed PortableGit) that runs
    without the trampoline guard. None when nothing suits — callers keep the broken command and let the
    fetch-failure ZIP fallback handle it. A failed probe (None) disqualifies a candidate like a guard hit.

    The trampoline symptom is PATH-level: ``bin\\git.exe`` / ``cmd\\git.exe`` (both ~46KB shims) fail to
    re-exec git-core, while the real binary at ``mingw64\\libexec\\git-core\\git.exe`` (≈4.4MB) works when
    invoked directly (#87876).
    """
    candidates = [
        Path(r"C:\Program Files\Git\mingw64\libexec\git-core\git.exe"),
        Path(r"C:\Program Files (x86)\Git\mingw64\libexec\git-core\git.exe"),
    ] + _portable_git_candidates()
    return next(
        (c for c in candidates if c.exists() and _probe_fork_bomb([str(c)]) is False),
        None,
    )


def _ensure_non_trampoline_git(git_cmd: list) -> list:
    """Swap a broken Git-for-Windows trampoline for a real git binary so fetch/pull/checkout keep working;
    if none is found leave the command untouched (fetch-failure handler falls back to ZIP). No-op off
    Windows and when git is healthy."""
    from hermes_cli.update_cmd import _locate_real_git

    if sys.platform != "win32" or not _git_is_trampoline(git_cmd):
        return git_cmd
    real_git = _locate_real_git()
    if real_git is None:
        print(
            "⚠ Detected a broken git trampoline and could not locate a real git binary — the update will fall back to the ZIP path."
        )
        return git_cmd
    print(f"⚠ Detected a broken git trampoline; switching to real git at {real_git}")
    return [str(real_git)] + list(git_cmd[1:])


def _discard_lockfile_churn(git_cmd, repo_root):
    """Restore ``package-lock.json`` files npm rewrote non-deterministically, so the update sees a clean tree
    instead of autostashing every run. Only touches lockfiles whose package.json is NOT also dirty. Best-effort."""
    from hermes_cli.update_cmd import _git_run

    with suppress(Exception):
        diff = _git_run(git_cmd, ["diff", "--name-only"], repo_root)
        if diff.returncode != 0:
            return
        changed = [line.strip() for line in diff.stdout.splitlines()]
        dirty_package_dirs = {
            Path(p).parent for p in changed if p.endswith("package.json")
        }
        dirty = [
            p
            for p in changed
            if p.endswith("package-lock.json")
            and Path(p).parent not in dirty_package_dirs
        ]
        if not dirty:
            return
        _git_run(git_cmd, ["checkout", "--", *dirty], repo_root)
        print(f"→ Discarded npm lockfile churn ({len(dirty)} file(s))")


def _normalize_managed_eol(git_cmd, repo_root):
    """Take a managed checkout off ``core.autocrlf=true`` without leaving it dirty.

    Git for Windows sets ``autocrlf=true`` system-wide, turning LF files CRLF and breaking ``git checkout``
    on update; install.ps1 pins ``false`` but older checkouts never got it and only ``hermes update`` can
    fix them. Pin and cleanup are one operation: under ``autocrlf=true`` a CRLF tree reads clean, so pinning
    alone would expose every file as modified (whole-tree autostash). Pin only after the tree verifies clean
    under it; a checkout we can't fully normalize is left as-is. Only ``true`` rewrites LF->CRLF
    (unset/false/input leave the tree alone). Best-effort.

    Checkouts created before that landed never got the pin and cannot receive it — the bootstrap installer
    reuses its build-pinned ``install.ps1`` forever — so ``hermes update``, which ships with the checkout
    itself, is the only path left that can fix them. See #67730.
    """
    from hermes_cli.update_cmd import _git_run

    # -c, not config: evaluate the tree as it WOULD look pinned, persisting nothing.
    probe = git_cmd + ["-c", "core.autocrlf=false"]

    def _probe_run(*args, **kw):
        return subprocess.run(probe + list(args), cwd=repo_root, **_GIT_TEXT_KW, **kw)

    def _eol_only():
        """Dirty paths whose ONLY change is CRLF; None when either probe fails."""
        all_dirty = _probe_run("diff", "-z", "--name-only")
        # Files with a *content* change ignoring CRLF. ``--name-only --ignore-cr-at-eol`` still LISTS
        # CR-only files; ``--numstat`` honors the filter (no record for them). Records are
        # "<added>\\t<deleted>\\t<path>"; rename detection is off, so exactly one path.
        real_dirty = _probe_run(
            "-c", "core.quotepath=false", "diff", "--numstat", "--ignore-cr-at-eol"
        )
        if all_dirty.returncode != 0 or real_dirty.returncode != 0:
            return None
        return {p for p in all_dirty.stdout.split("\0") if p} - {
            parts[2]
            for parts in (
                line.split("\t", 2)
                for line in real_dirty.stdout.splitlines()
                if line.strip()
            )
            if len(parts) == 3 and parts[2]
        }

    with suppress(Exception):
        if (
            _git_run(git_cmd, ["config", "--get", "core.autocrlf"], repo_root)
            .stdout.strip()
            .lower()
            != "true"
        ):
            return
        eol_only = _eol_only()
        if eol_only is None:
            return
        if eol_only:
            # Pathspec via stdin: thousands of paths exceed the Windows argv limit.
            _probe_run(
                "checkout",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                "--",
                input="\0".join(sorted(eol_only)),
                check=False,
            )
            if (
                _eol_only()
            ):  # still dirty: pinning would only surface churn we failed to clear
                return
            print(f"→ Normalized line-ending churn ({len(eol_only)} file(s))")
        subprocess.run(
            git_cmd + ["config", "core.autocrlf", "false"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
