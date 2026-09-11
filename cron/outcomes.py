"""Typed pre-script outcomes for cron jobs.

A cron pre-script can now express four outcomes instead of the binary
success/failure the scheduler used to infer from its exit code:

* ``SUCCESS``          — proceed normally (agent wake / stdout delivery).
* ``TRANSIENT_DEFER``  — the work could not run *right now* (lock held,
  upstream 429, contention). The scheduler must NOT consume the logical
  occurrence, NOT count a failure, and must retry once after a bounded
  backoff. Expressed either by exit code 75 (BSD ``EX_TEMPFAIL``,
  backward-compatible with defer-aware scripts) or by a last-line JSON
  directive ``{"outcome": "transient_defer", "retry_after": <seconds>,
  "reason": "...", "occurrence": "..."}``.
* ``PERMANENT_FAIL``   — the run is definitively broken; retrying the same
  occurrence cannot help. Any non-75 non-zero exit, or the
  ``{"outcome": "permanent_fail"}`` directive.
* ``SKIP``             — nothing to do this occurrence. The
  ``{"outcome": "skip"}`` directive, or the legacy
  ``{"wakeAgent": false}`` gate.

Classification is pure input→output; the scheduler owns what each kind
does to schedule state. A mocked/legacy 2-tuple result (no exit code)
never classifies as a defer — only a surfaced exit code or an explicit
directive can, so patched test doubles keep legacy semantics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import re

SUCCESS = "success"
TRANSIENT_DEFER = "transient_defer"
PERMANENT_FAIL = "permanent_fail"
SKIP = "skip"
# RUN_STARTED: the script launched a DETACHED worker that owns finishing
# this occurrence. The directive must carry a correlation ``run_id`` the
# worker will later finalize under; ``lease_seconds`` bounds how long the
# scheduler waits for that report before declaring the run lost.
DETACHED = "detached"

# BSD sysexits.h EX_TEMPFAIL — the long-standing "temporary failure, try
# again later" convention (sendmail, postfix). Mapping it keeps existing
# defer-aware scripts working with no rewrite.
EX_TEMPFAIL = 75

# retry_after bounds: scripts request, the scheduler clamps. The floor stops
# a hot-loop retry storm; the ceiling keeps a deferred daily occurrence from
# silently skipping a day.
MIN_RETRY_AFTER_SECONDS = 60
MAX_RETRY_AFTER_SECONDS = 6 * 3600
DEFAULT_RETRY_AFTER_SECONDS = 300

_MAX_REASON_CHARS = 500

# Detached-run reconciliation deadline bounds: the floor keeps a worker from
# being declared lost before it can even start; the ceiling guarantees a
# crashed worker becomes a VISIBLE failure within a day, never a silent hang.
MIN_DETACHED_LEASE_SECONDS = 60
MAX_DETACHED_LEASE_SECONDS = 24 * 3600
DEFAULT_DETACHED_LEASE_SECONDS = 3600

# Correlation ids travel through CLI args, filenames, and SQL — restrict to
# a safe token so a hostile/buggy script cannot smuggle structure.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_WORKER_CHARS = 200


class ScriptResult(tuple):
    """(ok, output) 2-tuple with the subprocess exit code attached.

    Every existing call site unpacks ``ok, output = result``; subclassing
    tuple keeps that contract byte-for-byte while letting the classifier
    read ``result.returncode``. Plain tuples (mocks, legacy paths) simply
    have no returncode.
    """

    returncode: Optional[int]

    def __new__(
        cls, ok: bool, output: str, returncode: Optional[int] = None
    ) -> "ScriptResult":
        self = super().__new__(cls, (bool(ok), output))
        self.returncode = returncode
        return self


class CronPreScriptDefer(str):
    """Scheduler-owned typed marker for a transient pre-script defer.

    ``str`` subclass (mirrors ``cron.scheduler._CronScriptFailure``) so any
    legacy code that logs or ``str()``s the error keeps working; the extra
    attributes carry the durable-obligation metadata the scheduler persists.
    """

    retry_after_seconds: int
    occurrence_key: str
    reason: str

    def __new__(
        cls, reason: str, *, retry_after_seconds: int, occurrence_key: str
    ) -> "CronPreScriptDefer":
        clean = str(reason or "").strip()
        text = (
            f"Deferred (transient): {clean}" if clean else "Deferred (transient)."
        )
        self = super().__new__(cls, text)
        self.retry_after_seconds = int(retry_after_seconds)
        self.occurrence_key = str(occurrence_key)
        self.reason = clean
        return self


class CronDetachedStart(str):
    """Scheduler-owned typed marker for a DETACHED (RUN_STARTED) outcome.

    ``str`` subclass (mirrors :class:`CronPreScriptDefer`) so legacy code
    that logs or ``str()``s it keeps working; the attributes carry the
    lease the scheduler registers on the execution row.
    """

    run_id: str
    worker: str
    lease_seconds: int
    occurrence_key: str
    reason: str

    def __new__(
        cls, reason: str, *, run_id: str, worker: str,
        lease_seconds: int, occurrence_key: str,
    ) -> "CronDetachedStart":
        clean = str(reason or "").strip()
        text = f"Detached run started: {run_id}" + (
            f" — {clean}" if clean else ""
        )
        self = super().__new__(cls, text)
        self.run_id = str(run_id)
        self.worker = str(worker or "")
        self.lease_seconds = int(lease_seconds)
        self.occurrence_key = str(occurrence_key or "")
        self.reason = clean
        return self


@dataclass(frozen=True)
class PreScriptOutcome:
    """One classified pre-script result."""

    kind: str
    reason: str = ""
    retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS
    occurrence_key: str = ""
    # DETACHED-only metadata (empty/default for every other kind).
    run_id: str = ""
    worker: str = ""
    lease_seconds: int = DEFAULT_DETACHED_LEASE_SECONDS


def job_occurrence_key(job: dict) -> str:
    """Logical-occurrence idempotency key for the fire being processed.

    Keyed on the SCHEDULED time (``next_run_at`` is still the due time while
    the run executes — it only advances at terminal completion), so a retry
    of a deferred daily 09:00 occurrence dedupes against the original fire
    even when the wall clock has moved on.
    """
    job_id = str(job.get("id") or "")
    scheduled = str(
        job.get("due_occurrence_at")
        or job.get("next_run_at")
        or job.get("manual_run_at")
        or "manual"
    )
    return f"{job_id}:{scheduled}"


def _clamp_retry_after(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    return max(MIN_RETRY_AFTER_SECONDS, min(MAX_RETRY_AFTER_SECONDS, seconds))


def _clamp_lease_seconds(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DETACHED_LEASE_SECONDS
    return max(
        MIN_DETACHED_LEASE_SECONDS, min(MAX_DETACHED_LEASE_SECONDS, seconds)
    )


def _last_line_directive(output: str) -> Optional[dict]:
    """Parse the last non-empty stdout line as a JSON outcome directive.

    Same line convention as the legacy ``wakeAgent`` gate
    (``cron.scheduler._parse_wake_gate``) so scripts keep one protocol.
    """
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        parsed = json.loads(lines[-1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _tempfail_reason(output: str) -> str:
    """Human reason from an exit-75 result, minus the mechanical framing.

    The script runner folds diagnostics into ``Script exited with code 75``
    plus ``stderr:`` / ``stdout:`` label lines; strip those so the persisted
    defer reason is the script's own message.
    """
    kept = [
        line
        for line in str(output or "").splitlines()
        if line.strip()
        and not line.startswith("Script exited with code")
        and line.strip() not in ("stderr:", "stdout:")
    ]
    return "\n".join(kept).strip()[:_MAX_REASON_CHARS]


def classify_script_result(
    ok: bool,
    output: str,
    returncode: Optional[int] = None,
    *,
    occurrence_key: str = "",
) -> PreScriptOutcome:
    """Classify one raw script result into a typed outcome.

    ``returncode`` is only available from the real runner (``ScriptResult``);
    callers holding a plain 2-tuple pass ``None`` and can never observe a
    defer from an exit code alone.
    """
    text = str(output or "")
    if returncode == EX_TEMPFAIL:
        return PreScriptOutcome(
            TRANSIENT_DEFER,
            reason=_tempfail_reason(text),
            retry_after_seconds=DEFAULT_RETRY_AFTER_SECONDS,
            occurrence_key=occurrence_key,
        )
    if not ok:
        return PreScriptOutcome(
            PERMANENT_FAIL,
            reason=text[:_MAX_REASON_CHARS],
            occurrence_key=occurrence_key,
        )

    directive = _last_line_directive(text)
    if directive is not None:
        kind = str(directive.get("outcome") or "").strip().lower()
        if kind == TRANSIENT_DEFER:
            return PreScriptOutcome(
                TRANSIENT_DEFER,
                reason=str(directive.get("reason") or "")[:_MAX_REASON_CHARS],
                retry_after_seconds=_clamp_retry_after(
                    directive.get("retry_after", DEFAULT_RETRY_AFTER_SECONDS)
                ),
                occurrence_key=str(directive.get("occurrence") or occurrence_key),
            )
        if kind == PERMANENT_FAIL:
            return PreScriptOutcome(
                PERMANENT_FAIL,
                reason=str(directive.get("reason") or "")[:_MAX_REASON_CHARS],
                occurrence_key=occurrence_key,
            )
        if kind == DETACHED:
            run_id = str(directive.get("run_id") or "")
            if not _RUN_ID_RE.match(run_id):
                # A detached worker nobody can correlate is unreconcilable —
                # fail closed and say why, never leak a ghost worker.
                return PreScriptOutcome(
                    PERMANENT_FAIL,
                    reason=(
                        "detached directive rejected: run_id is missing or "
                        "invalid (expected 1-128 chars of [A-Za-z0-9._:-])"
                    ),
                    occurrence_key=occurrence_key,
                )
            return PreScriptOutcome(
                DETACHED,
                reason=str(directive.get("reason") or "")[:_MAX_REASON_CHARS],
                occurrence_key=occurrence_key,
                run_id=run_id,
                worker=str(directive.get("worker") or "")[:_MAX_WORKER_CHARS],
                lease_seconds=_clamp_lease_seconds(
                    directive.get(
                        "lease_seconds", DEFAULT_DETACHED_LEASE_SECONDS
                    )
                ),
            )
        if kind == SKIP or directive.get("wakeAgent", True) is False:
            return PreScriptOutcome(SKIP, occurrence_key=occurrence_key)

    return PreScriptOutcome(SUCCESS, occurrence_key=occurrence_key)
