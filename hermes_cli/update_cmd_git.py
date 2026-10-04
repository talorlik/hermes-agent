"""Git plumbing for ``hermes update``: fork/upstream sync, trampoline-git detection, lockfile/EOL churn cleanup, orphan rescue refs, parked-branch assessment, fetch-failure classification.

Split out of ``update_cmd.py``, which re-imports every name so ``hermes_cli.update_cmd.<name>``
still resolves/monkeypatches. Origin helpers are imported lazily per function (no cycle;
test patches on ``update_cmd`` stay effective).
"""

import logging
import re
import shutil
from contextlib import suppress
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from hermes_cli._subprocess_compat import windows_hide_flags

logger = logging.getLogger("hermes_cli.update_cmd")  # log-record parity with the origin module

_ORPHAN_RESCUE_REFS_TO_KEEP = 10
_ORPHAN_RESCUE_REF_MAX_AGE_DAYS = 30

# creationflags is folded in here so every ``**_GIT_TEXT_KW`` spawn (rev-parse label,
# fork-bomb probe, EOL churn normalization) hides its console under the console-less
# desktop backend (#117781).
_GIT_TEXT_KW = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                   creationflags=windows_hide_flags())
_BAR = "=" * 68
_UPSTREAM_ADD_CMD = "git remote add upstream https://github.com/NousResearch/hermes-agent.git"


def _git_ok(git_cmd, args, cwd, **kw) -> bool:
    """True when ``_git_run`` exits 0; any exception counts as failure."""
    return _git_stdout(git_cmd, args, cwd, **kw) is not None


def _git_run(git_cmd, args, cwd=None, *, check=False):
    """Run ``git_cmd + args`` and return the CompletedProcess.

    The updater's git runner: capture all output and decode as UTF-8 regardless of the
    Windows ANSI code page (#52649). ``check=True`` raises on non-zero exit. The spawn
    always hides its console window (#117781).
    """
    return subprocess.run(
        git_cmd + list(args),
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=check,
        creationflags=windows_hide_flags(),
    )


def _git_stdout(git_cmd, args, cwd, **kw) -> Optional[str]:
    """Stripped stdout of a successful ``_git_run``; ``None`` on non-zero exit or any exception."""
    from hermes_cli.update_cmd import _git_run
    with suppress(Exception):
        result = _git_run(git_cmd, args, cwd, **kw)
        if result.returncode == 0:
            return result.stdout.strip()
    return None


def _prune_orphan_rescue_refs(
    git_cmd, cwd, branch, keep=_ORPHAN_RESCUE_REFS_TO_KEEP, max_age_days=_ORPHAN_RESCUE_REF_MAX_AGE_DAYS
) -> None:
    """Expire old rescue refs (``refs/hermes-update-backups/<kind>-<branch>-<ts>-<sha>``).

    ``<kind>`` is ``orphan`` (no common ancestor), ``diverged`` (local commits on the target
    branch) or ``detached`` (commits made on a detached HEAD the update moved off). All are written
    before the update moves HEAD and all pin objects, so they expire on the same terms; each kind
    keeps its own ``keep`` newest.

    Each ref pins a possibly multi-GB snapshot against ``git gc``, so a repeatedly corrupted install would
    grow ``.git`` unbounded. Keep the ``keep`` newest AND drop any older than ``max_age_days`` by the
    ``YYYYMMDD-HHMMSS`` stamp (unparseable names left alone); names sort chronologically so
    ``for-each-ref`` order is creation order. Best-effort, never blocks.

    A rescue ref pins every object reachable from that commit against ``git gc`` — and in the incident shape
    those objects include a full working-tree snapshot (the autostash orphan commit), which can be multi-GB
    when the tree holds large stray files. See #87694.
    """
    from hermes_cli.update_cmd_git import _git_run
    with suppress(OSError):
        stale: set[str] = set()
        for kind in ("orphan", "diverged", "detached"):
            prefix = f"refs/hermes-update-backups/{kind}-{branch}-"
            list_result = _git_run(
                git_cmd, ["for-each-ref", "--format=%(refname)", "--sort=refname", f"{prefix}*"], cwd)
            if list_result.returncode != 0:
                continue
            refs = [line.strip() for line in list_result.stdout.splitlines() if line.strip()]
            stale |= set(refs[:-keep] if keep > 0 else refs)
            if max_age_days > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
                for ref in refs:
                    with suppress(ValueError):
                        stamp = datetime.strptime(ref[len(prefix):][:15], "%Y%m%d-%H%M%S")
                        if stamp.replace(tzinfo=timezone.utc) < cutoff:
                            stale.add(ref)
        for ref in sorted(stale):
            _git_run(git_cmd, ["update-ref", "-d", ref], cwd)


def _park_detached_head(git_cmd, cwd, branch) -> None:
    """Keep commits made on a detached HEAD reachable before the update moves HEAD off it.

    Such commits belong to no branch: once HEAD moves, only the expiring reflog still reaches them,
    and nothing in the output would name them. When HEAD is detached at a commit no ref contains
    (the autostash's ``refs/stash`` does not count: it is dropped after the update), write
    ``refs/hermes-update-backups/detached-<branch>-<ts>-<sha12>`` (the divergence rescue refs'
    scheme and expiry) and name it. When that write fails, refuse (``sys.exit(1)``) rather than
    orphan the work. Attached HEADs and already-reachable commits are left alone.
    """
    from hermes_cli.update_cmd import _git_run
    if _git_run(git_cmd, ["symbolic-ref", "-q", "HEAD"], cwd).returncode == 0:
        return  # on a branch
    head = _git_run(git_cmd, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd)
    sha = (head.stdout or "").strip()
    if head.returncode != 0 or not sha:
        return
    contains = _git_run(git_cmd, ["for-each-ref", "--contains", sha, "--format=%(refname)"], cwd)
    holders = [r for r in (contains.stdout or "").split() if r != "refs/stash"]
    if contains.returncode == 0 and holders:
        return  # already reachable from a branch, tag, remote or backup ref
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rescue_ref = f"refs/hermes-update-backups/detached-{branch}-{stamp}-{sha[:12]}"
    if _git_run(git_cmd, ["update-ref", rescue_ref, sha], cwd).returncode != 0:
        print(f"✗ HEAD is detached at {sha[:12]}, which no branch or tag contains, and backing it up "
              f"to {rescue_ref} failed.")
        print(f"  Update stopped so those commits are not orphaned. Keep them with: "
              f"git -C {cwd} branch <name> {sha[:12]}")
        sys.exit(1)
    count = (_git_run(git_cmd, ["rev-list", "--count", sha, "--not", "--branches", "--tags", "--remotes"],
                      cwd).stdout or "").strip()
    print(f"  ⚠ {count or 'Some'} commit(s) made on the detached HEAD are on no branch — backed up to "
          f"{rescue_ref} before moving HEAD. This backup expires after {_ORPHAN_RESCUE_REF_MAX_AGE_DAYS} days.")
    print(f"    List them with: git log {rescue_ref} --not --branches --tags --remotes")
    _prune_orphan_rescue_refs(git_cmd, cwd, branch)


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
        if branch.returncode != 0 or sha.returncode != 0 or not sha_text or not branch_name:
            return None
        return f"{'detached' if branch_name == 'HEAD' else branch_name} @ {sha_text}"
    except Exception:
        return None


def _branch_head_suffix(git_cmd=None, cwd=None) -> str:
    """`` [<branch> @ <sha>]`` suffix for summary lines ("" when unknown)."""
    label = _branch_head_label(git_cmd, cwd)
    return f" [{label}]" if label else ""


def _assess_parked_branch_switch(git_cmd: list[str], cwd: Path, current_branch: str, target_branch: str) -> tuple[bool, str]:
    """Decide whether a parked feature branch may be auto-switched back to the update target.

    - (True, "") — tree clean and every parked commit is in ``origin/<target>`` (no ``git cherry +``).
    - (True, "unmerged:<n>") — tree clean but commits not in target; switching is safe (checkout keeps
      committed work) but caller must print a LOUD notice. Non-interactive callers (desktop, gateway
      /update, cron) can't resolve a skip, so a clean checkout must reach target.
    - (False, "disabled"|"dirty"|"unverifiable") — caller must NOT touch the branch. Dirty is the
      genuinely unsafe case: uncommitted work riding an autostash across branches.
    A config read failure must not disable the safety checks: fall through with the default."""
    from hermes_cli.update_cmd_git import _git_run
    try:
        from hermes_cli.config import load_config
        _update_cfg = (load_config() or {}).get("updates", {})
        if isinstance(_update_cfg, dict) and not bool(_update_cfg.get("auto_switch_parked_branch", True)):
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


def _print_parked_branch_skip_warning(git_cmd: list[str], cwd: Path, current_branch: str, target_branch: str, reason: str) -> None:
    """LOUD block: why the update was skipped on a parked branch, behind-count, fix commands."""
    behind = None
    with suppress(Exception):
        behind_text = _git_stdout(git_cmd, ["rev-list", f"HEAD..origin/{target_branch}", "--count"], cwd)
        if behind_text:
            behind = int(behind_text)
    why = _PARKED_SKIP_WHY.get(reason, f"the branch state could not be verified against origin/{target_branch}")
    print(f"\n{_BAR}\n⚠ CODE UPDATE SKIPPED — checkout is parked on '{current_branch}'")
    print(f"  Not auto-switching to {target_branch}: {why}.")
    if behind is not None and behind > 0:
        print(f"  This checkout is {behind} commit(s) BEHIND origin/{target_branch} — the code you are running is stale.")
    print(
        f"\n  To resolve, inspect the branch and switch back yourself:\n"
        f"    git -C {cwd} status\n"
        f"    git -C {cwd} checkout {target_branch} && hermes update\n"
        f"  (commit or stash your work on the branch first if you want to keep it)\n{_BAR}"
    )


def _print_parked_branch_kept_notice(current_branch: str, target_branch: str, unmerged_count: str) -> None:
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
    return _git_ok(git_cmd, ["push", "origin", "main", "--force-with-lease"], cwd, network=True)


def _offer_upstream_remote(git_cmd: list[str], cwd: Path, *, assume_yes: bool, input_fn) -> bool:
    """Prompt to add ``upstream`` and add it; False when the user declined, the run is non-interactive, or add failed.

    ``--yes`` means "don't block", not "mutate my remotes", so a non-interactive skip is NOT persisted."""
    from hermes_cli.update_cmd import _add_upstream_remote, _mark_skip_upstream_prompt
    print(
        "\nℹ Your fork is not tracking the official Hermes repository.\n"
        "  This means you may miss updates from NousResearch/hermes-agent.\n"
    )
    if assume_yes or (input_fn is None and not (sys.stdin.isatty() and sys.stdout.isatty())):
        print(f"  Skipping upstream setup (non-interactive run).\n  Add it later with: {_UPSTREAM_ADD_CMD}")
        return False
    if input_fn is not None:
        response = input_fn("Add official repo as 'upstream' remote? [y/N]", "n").strip().lower()
    else:
        try:
            response = input("Add official repo as 'upstream' remote? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt, UnicodeDecodeError):
            print()
            response = "n"
    if response not in {"", "y", "yes"}:
        print(f"  Skipped. Run '{_UPSTREAM_ADD_CMD}' to add later.")
        _mark_skip_upstream_prompt()
        return False
    print("→ Adding upstream remote...")
    if not _add_upstream_remote(git_cmd, cwd):
        print("  ✗ Failed to add upstream remote. Skipping upstream sync.")
        return False
    print("  ✓ Added upstream: https://github.com/NousResearch/hermes-agent.git")
    return True


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

    @property
    def locally_integrated(self) -> bool:
        """Validated upstream code is in place; only the fork publication is unproven.

        The typed form of the Boolean wrapper's "True after a local sync whose push
        failed": the candidate passed validation before any push and the checkout is
        proven clean. HEAD need not have moved: a run that follows an unpushed sync
        re-validates the same commit and fails the same push. A fork the user cannot
        push to must not fail the update.
        """
        return (
            self.local_integration_completed
            and self.proof_complete
            and self.status in {"updated", "failed"}
        )

    @property
    def upstream_checked(self) -> bool:
        return self.status != "not_checked"

    @property
    def nothing_attempted(self) -> bool:
        """Declined, or no upstream remote: no mutating command ran.

        There is then no checkout change to prove, so this holds without
        ``proof_complete``. ``_finish_sync_outcome`` turns any contradicted
        not-checked result into ``failed``, which keeps this fail-closed. Only the
        observed sync may report ``not_checked``: it returns before its first mutating
        command. A Boolean answer cannot, because False is also what a rejected and
        rolled-back candidate returns.
        """
        return self.status == "not_checked" and not self.error

    @property
    def repair(self) -> dict[str, str | None]:
        """Structured next step for an outcome the updater refuses; empty when it proceeds.

        ``retry_update``: the candidate was rolled back and the checkout is proven at
        ``expected_head``. ``verify_checkout_then_retry``: the effect of the sync is
        unproven, so HEAD must be compared with ``recovery_ref`` before anything else.
        ``restore_recovery_ref``: the checkout is known not to be restored.
        """
        if self.safe_to_continue or self.nothing_attempted or self.locally_integrated:
            return {}
        if self.safe_to_restore:
            action = "retry_update"
        elif self.status == "outcome_unknown":
            action = "verify_checkout_then_retry"
        else:
            action = "restore_recovery_ref"
        return {
            "action": action,
            "recovery_ref": self.recovery_ref,
            "expected_head": self.pre_sha,
            "observed_head": self.post_sha,
        }

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

@dataclass(frozen=True)
class CheckoutIdentity:
    """The ref HEAD names (literal ``HEAD`` when detached) and the commit it resolves to."""

    ref: str
    head: str

    @property
    def label(self) -> str:
        return f"{self.ref} at {self.head[:10]}"

@dataclass(frozen=True)
class CandidateRollback:
    """What a rollback did: ``rolled_back``, ``reset_failed``, or ``refused`` (no command ran)."""

    state: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "rolled_back"

    @property
    def error(self) -> str:
        if self.state == "refused":
            return f"rollback refused: {self.detail}"
        return "" if self.ok else "rollback reset failed"

def _capture_checkout_identity(
    git_cmd: list[str], cwd: Path
) -> CheckoutIdentity | None:
    """Read the checked-out ref and commit; None when either cannot be proven."""
    from hermes_cli.update_cmd import _git_run

    try:
        ref = _git_run(git_cmd, ["rev-parse", "--symbolic-full-name", "HEAD"], cwd)
        head = _git_run(git_cmd, ["rev-parse", "--verify", "HEAD^{commit}"], cwd)
    except OSError:
        return None
    if ref.returncode != 0 or head.returncode != 0:
        return None
    ref_name, head_sha = ref.stdout.strip(), head.stdout.strip()
    if ref_name != "HEAD" and not ref_name.startswith("refs/heads/"):
        return None
    if _FULL_SHA_RE.fullmatch(head_sha) is None:
        return None
    return CheckoutIdentity(ref=ref_name, head=head_sha)

def _rollback_fork_sync_candidate(
    git_cmd: list[str],
    cwd: Path,
    rollback_ref: str,
    *,
    expected: CheckoutIdentity | None,
    abort_merge: bool = False,
) -> CandidateRollback:
    """Reset a failed upstream candidate, but only on the checkout the sync itself left.

    ``reset --hard`` and ``merge --abort`` act on whatever is checked out. *expected* is the
    identity the sync observed; it has no default, and an unknown or different checkout is
    refused before any command runs, so a branch switched in meanwhile (a hook, another
    process) is never reset. The check and the reset are separate Git calls: this narrows
    the window to one spawn, it cannot close it.
    """
    from hermes_cli.update_cmd import _no_prompt_git_kwargs

    observed = _capture_checkout_identity(git_cmd, cwd)
    if expected is None or observed != expected:
        detail = (
            f"expected {expected.label if expected else 'a checkout this sync could not identify'}, "
            f"observed {observed.label if observed else 'an unidentifiable checkout'}"
        )
        print(f"  ✗ Rollback not attempted: the checkout is not the one this sync left ({detail}).")
        print(f"    Nothing was reset. The pre-sync commit is {rollback_ref[:10]}.")
        return CandidateRollback("refused", detail)
    if abort_merge:
        with suppress(OSError):
            subprocess.run(
                git_cmd + ["merge", "--abort"],
                cwd=cwd,
                capture_output=True,
                check=False,
                **_no_prompt_git_kwargs(),
            )
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
        return CandidateRollback("reset_failed", str(exc))
    if rollback_result.returncode == 0:
        print(
            f"  ✓ Rolled back to {rollback_ref[:10]} - nothing was pushed to your fork."
        )
        return CandidateRollback("rolled_back")
    print("  ✗ Rollback failed. Recover manually with:")
    print(f"    cd {cwd} && git reset --hard {rollback_ref}")
    if rollback_result.stderr.strip():
        print(f"    ({rollback_result.stderr.strip().splitlines()[0]})")
    return CandidateRollback("reset_failed", rollback_result.stderr.strip())

def _validate_fork_sync_candidate(
    git_cmd: list[str],
    cwd: Path,
    rollback_ref: str,
    *,
    rollback_on_failure: bool = True,
    candidate_label: str = "Merged",
) -> bool:
    """Validate merged upstream code and roll it back on any owned failure.

    *candidate_label* names how the candidate arrived ("Merged" or "Pulled") so the
    report matches the operation the user saw.
    """
    from hermes_cli.update_cmd import _validate_critical_files_syntax

    # Pinned before validation runs anything: the audit and the tests execute candidate code
    # and hooks, and the rollback must not reset a checkout they switched in.
    expected = _capture_checkout_identity(git_cmd, cwd) if rollback_on_failure else None

    def fail() -> bool:
        if rollback_on_failure:
            _rollback_fork_sync_candidate(git_cmd, cwd, rollback_ref, expected=expected)
        return False

    syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(cwd)
    if not syntax_ok:
        print(f"\n  ✗ {candidate_label} code has a syntax error in a critical file:")
        print(f"    {failing_path}")
        if syntax_error:
            for line in str(syntax_error).splitlines()[:6]:
                print(f"      {line}")
        return fail()

    # Runs before the fork is pushed: merge damage outside the 9 critical files (scheduler,
    # worker tools) must be rejected here, not discovered after publication.
    from hermes_cli.update_cmd_integrity import failure_lines

    findings = failure_lines(cwd, strict=True)  # pre-publication: dependencies are installed
    if findings is not None:
        print(f"\n  ✗ {candidate_label} code failed the tree integrity audit (syntax error or broken import):")
        for line in findings[:12]:
            print(f"    {line}")
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

    sha_captured = isinstance(pre_sha, str) and _FULL_SHA_RE.fullmatch(pre_sha) is not None
    preflight_error = ""
    if not sha_captured:
        preflight_error = "missing or invalid pre-sync HEAD SHA"
    elif clean_before is not True or operation_before != "none":
        preflight_error = "pre-sync checkout is not clean and operation-free"

    def declined() -> UpstreamSyncOutcome:
        # No upstream remote and none wanted: no mutating command ran, so a checkout
        # that cannot be proven is not this sync's failure to report. The Boolean
        # wrapper has always answered "not checked" here without observing anything.
        if not preflight_error:
            return finish("not_checked")
        return UpstreamSyncOutcome(
            phase=phase,
            status="not_checked",
            pre_sha=pre_sha,
            post_sha=pre_sha,
            clean=clean_before,
            operation_state=operation_before,
            recovery_ref=_safe_recovery_ref(pre_sha),
            error="",
        )

    # A passing preflight keeps the historical command order (proof, then remote probe).
    if not _upstream_available:
        remote = _observe_upstream_remote(git_cmd, cwd)
        if remote.state == "failed":
            return finish("failed", f"upstream remote probe failed: {remote.detail}")
        if remote.state == "absent":
            if _should_skip_upstream_prompt():
                return declined()
            remote_outcome, remote_error = _offer_upstream_remote_observed(
                git_cmd, cwd, assume_yes=assume_yes, input_fn=input_fn
            )
            if remote_outcome == "failed":
                return finish(
                    "failed", f"upstream remote creation failed: {remote_error}"
                )
            if remote_outcome != "available":
                return declined()
    if preflight_error:
        if not sha_captured:
            print("  ✗ Could not capture the pre-sync HEAD. Upstream sync failed.")
        return finish("failed", preflight_error)

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

    # merge, pull and the rollback's reset all act on whatever is checked out. Pin the ref and
    # commit before the first mutation so a rollback can prove it is still this checkout.
    sync_identity = _capture_checkout_identity(git_cmd, cwd)
    if sync_identity is None or sync_identity.head != pre_sha:
        print("  ✗ Could not prove which checkout this sync owns. Upstream sync failed.")
        return finish(
            "failed",
            "checkout identity is unknown or moved before the sync changed anything",
        )

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
            rollback = _rollback_fork_sync_candidate(
                git_cmd, cwd, pre_sha, expected=sync_identity
            )
            status = "failed" if rollback.ok else "rollback_failed"
            error = f"upstream merge failed: {exc}"
            if not rollback.ok:
                error += f"; {rollback.error}"
            return finish(status, error, recovery_ref)
        if merge_result.returncode != 0:
            # A failed merge leaves HEAD where it was, so the pre-mutation identity still
            # names this checkout; the abort runs behind the same check as the reset.
            rollback = _rollback_fork_sync_candidate(
                git_cmd, cwd, pre_sha, expected=sync_identity, abort_merge=True
            )
            print(
                "  ✗ Could not merge upstream/main (conflict or dirty tree) - "
                + ("sync stopped, nothing was changed." if rollback.ok else "sync stopped.")
            )
            print(f"  Resolve manually: cd {cwd} && git merge upstream/main")
            if not rollback.ok:
                return finish(
                    "rollback_failed",
                    f"upstream merge failed; {rollback.error}",
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

    # The candidate has landed. A rollback is this sync's to run only on the ref it started
    # on, at the commit validation was handed; otherwise the identity stays unknown and the
    # rollback refuses.
    landed = _capture_checkout_identity(git_cmd, cwd)
    candidate_identity = (
        landed if landed is not None and landed.ref == sync_identity.ref else None
    )

    validation_error = ""
    try:
        candidate_valid = _validate_fork_sync_candidate(
            git_cmd,
            cwd,
            pre_sha,
            rollback_on_failure=False,
            candidate_label="Merged" if origin_ahead > 0 else "Pulled",
        )
    except Exception as exc:
        candidate_valid = False
        validation_error = f"candidate validation raised: {exc}"
    if not candidate_valid:
        rollback = _rollback_fork_sync_candidate(
            git_cmd, cwd, pre_sha, expected=candidate_identity
        )
        print("  Try the sync again once the candidate passes validation.")
        error = validation_error or "upstream candidate validation failed"
        if not rollback.ok:
            return finish(
                "rollback_failed", f"{error}; {rollback.error}", recovery_ref
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
        upstream_available = _offer_upstream_remote(
            git_cmd, cwd, assume_yes=assume_yes, input_fn=input_fn
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

def _observe_legacy_upstream_sync(
    legacy_sync,
    git_cmd: list[str],
    cwd: Path,
    *,
    phase: str,
    assume_yes: bool = False,
    input_fn=None,
) -> UpstreamSyncOutcome:
    """Typed verdict for a Boolean-only sync callable. The updater's call sites do not use it.

    The Boolean contract answers False for a declined sync, for a failed one, and for a
    candidate that failed validation and was rolled back. With HEAD where it was, those are
    one and the same observation, so False is never ``not_checked`` or ``noop`` here: it is
    ``outcome_unknown`` with an error, which is neither ``safe_to_continue`` nor
    ``nothing_attempted``. True is the callable's own attestation that upstream was checked;
    the checkout proof in ``_finish_sync_outcome`` still has to hold around it. Only
    ``_sync_with_upstream_observed`` can attest that no mutating command ran, and
    ``UpstreamSyncOutcome.repair`` names what a refused outcome needs next. Exceptions
    propagate: this adds evidence, it does not change how the callable aborts.
    """
    from hermes_cli.update_cmd import _capture_head_sha

    pre_sha = _capture_head_sha(git_cmd, cwd)
    checked = legacy_sync(git_cmd, cwd, assume_yes=assume_yes, input_fn=input_fn) is True
    moved = _capture_head_sha(git_cmd, cwd) != pre_sha
    if checked:
        status, error = ("updated" if moved else "noop"), ""
    else:
        status = "outcome_unknown"
        error = (
            "Boolean upstream sync answered False: declined, failed, and rolled back are "
            "indistinguishable without the typed outcome"
        )
        if moved:
            error += "; HEAD moved"
    return _finish_sync_outcome(
        git_cmd,
        cwd,
        phase=phase,
        status=status,
        pre_sha=pre_sha,
        recovery_ref=pre_sha,
        error=error,
        local_integration_completed=moved and checked,
    )


def _has_http_code(stderr: str, *codes: str) -> bool:
    return any(f"HTTP {code}" in stderr or f"returned error: {code}" in stderr for code in codes)


# Ordered (predicate, diagnosis): curl reports HTTP errors as ``unable to access '<url>': ... error: 429``,
# so rate-limit/outage checks must run BEFORE the generic "unable to access" check. An anonymous fetch
# answered with HTTP 401 ("could not read Username") is GitHub during an outage (or a renamed/private
# repo), not a user credentials problem.
_FETCH_FAILURE_RULES = (
    (lambda s: _has_http_code(s, "429") or "rate limit" in s.lower(),
     "✗ GitHub is rate limiting requests or having an outage (HTTP 429) — try again in 5 minutes."),
    (lambda s: _has_http_code(s, "500", "502", "503", "504"),
     "✗ GitHub appears to be having an outage — try again in a few minutes (https://www.githubstatus.com)."),
    (lambda s: "Could not resolve host" in s or "unable to access" in s,
     "✗ Network error — cannot reach the remote repository."),
    (lambda s: "could not read Username" in s or "terminal prompts disabled" in s,
     "✗ GitHub rejected the anonymous fetch (asked for a login) — this usually means a GitHub outage;"
     " try again in a few minutes (https://www.githubstatus.com). If it persists, check"
     " `git remote -v` points at a public repo."),
    (lambda s: "Authentication failed" in s,
     "✗ Authentication failed — check your git credentials or SSH key."),
    # SSH auth failures never say "Authentication failed" — OpenSSH prints its own
    # "Permission denied (publickey)"/"Host key verification failed" and git wraps
    # it as "Could not read from remote repository", which otherwise fell through
    # to the generic message below and left an SSH-remote user with no idea their
    # key (or lack of one) was the cause (#82169).
    (lambda s: "Permission denied (publickey)" in s or "Host key verification failed" in s,
     "✗ SSH authentication failed — check your SSH key is added to GitHub, or switch"
     " `origin` to HTTPS: `git remote set-url origin https://github.com/NousResearch/hermes-agent.git`."),
)


def _classify_fetch_failure(stderr: str) -> str:
    """Map git-fetch stderr to a one-line diagnosis (caller also prints the raw first line)."""
    return next((message for matches, message in _FETCH_FAILURE_RULES if matches(stderr)), "✗ Failed to fetch updates from origin.")


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
    from hermes_constants import get_default_hermes_root, get_hermes_home
    candidates = []
    with suppress(Exception):
        candidates += [root / "git" / "mingw64" / "libexec" / "git-core" / "git.exe" for root in (get_default_hermes_root(), Path(get_hermes_home()))]
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
        Path(base) / "Git" / arch / "libexec" / "git-core" / "git.exe"
        for base in (r"C:\Program Files", r"C:\Program Files (x86)")
        for arch in ("clangarm64", "mingw64", "mingw32")
    ] + _portable_git_candidates()
    return next((c for c in candidates if c.exists() and _probe_fork_bomb([str(c)]) is False), None)


def _ensure_non_trampoline_git(git_cmd: list) -> list:
    """Swap a broken Git-for-Windows trampoline for a real git binary so fetch/pull/checkout keep working;
    if none is found leave the command untouched (fetch-failure handler falls back to ZIP). No-op off
    Windows and when git is healthy."""
    from hermes_cli.update_cmd import _locate_real_git
    if sys.platform != "win32" or not _git_is_trampoline(git_cmd):
        return git_cmd
    real_git = _locate_real_git()
    if real_git is None:
        print("⚠ Detected a broken git trampoline and could not locate a real git binary — the update will fall back to the ZIP path.")
        return git_cmd
    print(f"⚠ Detected a broken git trampoline; switching to real git at {real_git}")
    return [str(real_git)] + list(git_cmd[1:])


def _npm_lockfile_owners(repo_root: Path) -> set[Path]:
    """Manifest directories whose specs the single root ``package-lock.json`` records: the root plus every
    workspace from the root ``workspaces`` globs (same model as ``update_cmd_deps._npm_manifest_paths``).
    A manifest outside that graph (``website/``, ``scripts/whatsapp-bridge/``) has its own lockfile."""
    owners = {Path(".")}
    try:
        import json
        package = json.loads((repo_root / "package.json").read_text(encoding="utf-8-sig"))
        workspaces = package.get("workspaces", [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages", [])
        if not isinstance(workspaces, list):
            return owners
        for pattern in workspaces:
            # One bad glob (absolute pattern -> NotImplementedError) degrades to "not an owner"
            # instead of aborting the whole churn cleanup through the caller's suppress(Exception).
            with suppress(Exception):
                for directory in repo_root.glob(str(pattern)):
                    if (directory / "package.json").is_file():
                        owners.add(directory.relative_to(repo_root))
    except (OSError, ValueError, TypeError):
        pass
    return owners


def _discard_lockfile_churn(git_cmd, repo_root):
    """Restore ``package-lock.json`` files npm rewrote non-deterministically, so the update sees a clean tree
    instead of autostashing every run. A lockfile is kept when a manifest it records is dirty: for the root
    lock that is the root or ANY workspace ``package.json`` (reverting it under a dirty ``apps/desktop``
    manifest desyncs spec and lock and every later ``npm ci`` fails, #112378); a nested lock is kept only
    with its sibling manifest. Best-effort."""
    from hermes_cli.update_cmd_git import _git_run
    with suppress(Exception):
        diff = _git_run(git_cmd, ["diff", "--name-only"], repo_root)
        if diff.returncode != 0:
            return
        changed = [line.strip() for line in diff.stdout.splitlines()]
        dirty_manifests = {Path(p).parent for p in changed if p.endswith("package.json")}
        root_owners = _npm_lockfile_owners(Path(repo_root))
        dirty = []
        for path in changed:
            if not path.endswith("package-lock.json"):
                continue
            lock_dir = Path(path).parent
            protected = (lock_dir == Path(".") and bool(dirty_manifests & root_owners)) or (
                lock_dir != Path(".") and lock_dir in dirty_manifests
            )
            if not protected:
                dirty.append(path)
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
    from hermes_cli.update_cmd_git import _git_run
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
        real_dirty = _probe_run("-c", "core.quotepath=false", "diff", "--numstat", "--ignore-cr-at-eol")
        if all_dirty.returncode != 0 or real_dirty.returncode != 0:
            return None
        return {p for p in all_dirty.stdout.split("\0") if p} - {
            parts[2]
            for parts in (line.split("\t", 2) for line in real_dirty.stdout.splitlines() if line.strip())
            if len(parts) == 3 and parts[2]
        }

    with suppress(Exception):
        if _git_run(git_cmd, ["config", "--get", "core.autocrlf"], repo_root).stdout.strip().lower() != "true":
            return
        eol_only = _eol_only()
        if eol_only is None:
            return
        if eol_only:
            # Pathspec via stdin: thousands of paths exceed the Windows argv limit.
            _probe_run("checkout", "--pathspec-from-file=-", "--pathspec-file-nul", "--",
                       input="\0".join(sorted(eol_only)), check=False)
            if _eol_only():  # still dirty: pinning would only surface churn we failed to clear
                return
            print(f"→ Normalized line-ending churn ({len(eol_only)} file(s))")
        subprocess.run(git_cmd + ["config", "core.autocrlf", "false"], cwd=repo_root, capture_output=True, check=False,
                       creationflags=windows_hide_flags())
