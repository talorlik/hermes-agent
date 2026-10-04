"""Typed pre-script outcome plumbing for the scheduler.

Producer side: ``classify_pre_script`` turns the runner's ``ScriptResult`` (exit code attached) into
a ``PreScriptOutcome`` keyed on the job's LOGICAL occurrence, and ``deferred_run_result`` is the
typed ``run_job`` result for a TRANSIENT_DEFER — returned BEFORE the ordinary ``fail_closed`` /
``continue`` / ``no_agent`` handling so a deferred occurrence never wakes the agent, never trips the
failure policy and never produces a delivery.

Consumer side: ``consume_pre_script_defer`` is the durable handoff the run body performs instead of
ordinary completion bookkeeping: persist the retry obligation (``cron.deferrals.record_defer``),
hand the job state over under the fire-claim fence (``cron.jobs.mark_job_deferred`` — the logical
occurrence is retained, no run/failure counters move), and record the attempt as DEFERRED in the
ledger (``cron.executions.defer_execution``). A lost fence compensates the obligation
(``rollback_defer``) and finishes the attempt as a stale failure. An exhausted budget is NOT a
defer: the caller receives an ordinary failure text and runs the normal failure path (incident,
failure delivery through the durable producer, schedule advance).

Ownership: the pending debt belongs to whoever holds the fire claim. A stale owner (claim moved
to a replacement, or released) must neither hand the job over nor consume the replacement's
remaining retry budget. Both branches are fenced: the pending branch through the owner-fenced
``mark_job_deferred`` (lost -> ``rollback_defer``), the exhausted branch through an owner check
AFTER the obligation write (lost -> ``rollback_defer`` restores the exact pending row the write
escalated) plus ``compensate_exhausted_defer`` when the authoritative owner-fenced terminal write
later refuses. A pre-check alone is insufficient because ownership can move after it.

Terminal resolution is keyed on the completing run's logical occurrence
(``resolve_deferral_at_terminal``): a delayed old completion cannot settle an obligation a newer
admitted occurrence owns.

Kept out of ``cron.scheduler`` so the facade stays a thin caller; ``cron.jobs`` is reached through
the module so the fenced handoff stays monkeypatchable where the canonical tests patch it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from cron.outcomes import (
    TRANSIENT_DEFER,
    CronPreScriptDefer,
    PreScriptOutcome,
    classify_script_result,
    job_occurrence_key,
)

logger = logging.getLogger("cron.scheduler")

STALE_DEFER_ERROR = "Fire claim ownership lost; stale defer was discarded."


def _owner_holds_fire_claim(
    jobs_mod: Any, job_id: str, fire_owner: Optional[str]
) -> bool:
    """True when *fire_owner* still holds the job's fire claim (``None`` owner = unfenced run).
    Fails closed: a store fault counts as lost ownership, never as a licence to touch the debt."""
    if fire_owner is None:
        return True
    try:
        return bool(jobs_mod.heartbeat_fire_claim(job_id, expected_owner=fire_owner))
    except Exception as exc:
        logger.warning(
            "Job '%s': fire claim ownership could not be verified (%s); treating as lost",
            job_id,
            exc,
        )
        return False


def _finish_stale_defer(execution_id: Optional[str]) -> DeferDisposition:
    from cron import executions

    if execution_id:
        executions.finish_execution(
            execution_id, success=False, error=STALE_DEFER_ERROR
        )
    return DeferDisposition(handled=True, stale=True)


def classify_pre_script(job: dict, result: Any) -> PreScriptOutcome:
    """Classify a script runner result for *job*. Plain 2-tuples (mocks, legacy) carry no exit
    code and therefore never classify as a defer from the exit code alone."""
    ok, output = result
    return classify_script_result(
        bool(ok),
        str(output or ""),
        getattr(result, "returncode", None),
        occurrence_key=job_occurrence_key(job),
    )


def is_transient_defer(outcome: PreScriptOutcome) -> bool:
    return outcome.kind == TRANSIENT_DEFER


def pre_script_defer(outcome: PreScriptOutcome) -> CronPreScriptDefer:
    return CronPreScriptDefer(
        outcome.reason,
        retry_after_seconds=outcome.retry_after_seconds,
        occurrence_key=outcome.occurrence_key,
    )


def deferred_run_result(
    job: dict,
    job_id: str,
    job_name: str,
    outcome: PreScriptOutcome,
    *,
    mode: str,
    now_iso: str,
) -> Tuple[bool, str, str, CronPreScriptDefer]:
    """``run_job`` result for a transient defer: not a success, not a failure, nothing to deliver.
    The reason comes from the runner's already-redacted output."""
    marker = pre_script_defer(outcome)
    doc = (
        f"# Cron Job: {job_name} (DEFERRED)\n\n"
        f"**Job ID:** {job_id}\n"
        f"**Run Time:** {now_iso}\n"
        f"**Mode:** {mode}\n"
        "**Status:** DEFERRED — transient pre-script defer; the logical occurrence "
        f"({marker.occurrence_key}) is retained and retried once after "
        f"{marker.retry_after_seconds}s\n\n"
        "## Reason\n\n"
        f"{marker.reason or '(no reason given)'}\n\n"
        "No agent or model was invoked and nothing was delivered.\n"
    )
    return False, doc, "", marker


@dataclass(frozen=True)
class DeferDisposition:
    """Outcome of consuming one typed defer. ``handled`` means the attempt ended here (deferred, or
    stale and discarded) and the run body must return without ordinary bookkeeping; otherwise
    ``error`` is the ordinary failure text for an exhausted retry budget."""

    handled: bool
    stale: bool = False
    error: Optional[str] = None
    obligation: Optional[dict] = None


def consume_pre_script_defer(
    job: dict,
    defer: CronPreScriptDefer,
    *,
    fire_owner: Optional[str],
    execution_id: Optional[str],
) -> DeferDisposition:
    """Durable defer handoff: obligation -> fenced job state -> deferred ledger row."""
    from cron import deferrals, executions
    from cron import jobs as jobs_mod

    job_id = str(job["id"])
    # Ownership pre-check: a departed owner (claim released or taken over) never touches the debt.
    # Not the authority — ownership can move after this read, so both branches re-fence below.
    if not _owner_holds_fire_claim(jobs_mod, job_id, fire_owner):
        logger.warning(
            "Job '%s': defer from a stale fire owner (%s); obligation left untouched",
            job_id,
            fire_owner,
        )
        return _finish_stale_defer(execution_id)
    obligation = deferrals.record_defer(
        job_id,
        defer.occurrence_key,
        reason=defer.reason,
        retry_after_seconds=defer.retry_after_seconds,
    )
    attempts = int(obligation.get("attempts") or 0)
    if obligation.get("state") == "exhausted":
        # The exhaustion just written may be another owner's remaining budget: re-fence AFTER the
        # write and restore the exact pending row it escalated when the claim has moved.
        if not _owner_holds_fire_claim(jobs_mod, job_id, fire_owner):
            compensated = deferrals.rollback_defer(obligation)
            logger.warning(
                "Job '%s': stale fire owner exhausted a pending obligation; %s",
                job_id,
                "restored to its replacement owner"
                if compensated
                else "row already moved on",
            )
            return _finish_stale_defer(execution_id)
        logger.warning(
            "Job '%s': deferred retry budget exhausted for occurrence %s; failing permanently",
            job_id,
            obligation.get("occurrence_key"),
        )
        return DeferDisposition(
            handled=False,
            obligation=obligation,
            error=(
                f"Deferred retry budget exhausted after {attempts} "
                f"{'retry' if attempts == 1 else 'retries'} "
                f"(occurrence {obligation.get('occurrence_key')}): "
                f"{defer.reason or 'transient defer'}"
            ),
        )
    handed_off = jobs_mod.mark_job_deferred(
        job_id,
        obligation["retry_at"],
        reason=defer.reason,
        occurrence_key=str(obligation.get("occurrence_key") or defer.occurrence_key),
        attempts=attempts,
        expected_fire_owner=fire_owner,
    )
    if not handed_off:
        # The fire claim moved to a replacement owner: the obligation this run just wrote must not
        # linger for that owner to inherit and exhaust. Compensate, then record the stale attempt.
        compensated = deferrals.rollback_defer(obligation)
        logger.warning(
            "Job '%s': defer handoff lost the fire claim; obligation %s",
            job_id,
            "rolled back" if compensated else "left to its replacement owner",
        )
        return _finish_stale_defer(execution_id)
    if execution_id:
        executions.defer_execution(
            execution_id,
            reason=defer.reason,
            occurrence_key=str(
                obligation.get("occurrence_key") or defer.occurrence_key
            ),
            retry_at=str(obligation["retry_at"]),
        )
    logger.info(
        "Job '%s': occurrence %s deferred until %s (attempt %d)",
        job_id,
        obligation.get("occurrence_key"),
        obligation.get("retry_at"),
        attempts,
    )
    return DeferDisposition(handled=True, obligation=obligation)


def compensate_exhausted_defer(obligation: Optional[dict]) -> bool:
    """The authoritative owner-fenced terminal write refused AFTER this run exhausted the pending
    obligation: the debt belongs to the replacement owner, so restore the exact pending row the
    exhaustion replaced (``rollback_defer`` is itself fenced on the row's state/attempts/stamp)."""
    if not obligation or obligation.get("state") != "exhausted":
        return False
    from cron import deferrals

    try:
        compensated = deferrals.rollback_defer(obligation)
    except Exception as exc:
        logger.warning(
            "Job '%s': could not compensate the exhausted obligation after ownership loss: %s",
            obligation.get("job_id"),
            exc,
        )
        return False
    logger.warning(
        "Job '%s': terminal write lost the fire claim after exhausting occurrence %s; %s",
        obligation.get("job_id"),
        obligation.get("occurrence_key"),
        "pending obligation restored" if compensated else "obligation already moved on",
    )
    return compensated


def terminal_occurrence_key(job: Any) -> Optional[str]:
    """Logical occurrence the completing run settles: the deferred occurrence it is retrying
    (``last_defer.occurrence_key`` on the dispatched record) or its own occurrence key. ``None``
    for a bare job id (legacy caller: unfiltered)."""
    if not isinstance(job, dict):
        return None
    last_defer = job.get("last_defer")
    if isinstance(last_defer, dict) and last_defer.get("occurrence_key"):
        return str(last_defer["occurrence_key"])
    return job_occurrence_key(job)


def resolve_deferral_at_terminal(job: Any, *, success: bool) -> None:
    """A real terminal outcome settles the pending obligation of ITS OWN logical occurrence only
    (a delayed old completion must not resolve a newer admitted occurrence); never breaks the
    caller. *job* is the dispatched job record (or a bare id for legacy unfiltered callers)."""
    from cron import deferrals

    job_id = str(job.get("id") if isinstance(job, dict) else job)
    try:
        deferrals.resolve_pending(
            job_id,
            "completed" if success else "permanent",
            occurrence_key=terminal_occurrence_key(job),
        )
    except Exception as exc:
        logger.debug("Job '%s': could not resolve pending deferral: %s", job_id, exc)
