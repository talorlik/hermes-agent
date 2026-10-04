"""Delivery-run producer: fanout admission, generation tokens, aggregate finalization, backlog.

One run's delivery is one *generation* ``(job_id, execution_id, projection_revision)``:

* ``admit_run_delivery_generation`` begins the execution's projection generation when the run
  starts, so the finalized-projection overlay in ``cron.jobs.get_job`` is scoped to the latest
  execution and a run without delivery never inherits an older settled projection;
* ``attempt_concrete_deliveries`` is the fanout admission: ``cron.outbox.enqueue_deliveries_with_intent``
  has already persisted every concrete destination atomically under one projection revision, and
  this binds that revision to the invocation (``job["_delivery_projection_revision"]``) and to the
  durable queue snapshot BEFORE the first transport. Callbacks and terminal receipts therefore
  carry the original identity; nothing here ever borrows the newest projection row;
* ``DeliveryRun`` aggregates destination outcomes and finalizes exactly once per invocation;
  cross-job and post-finalize contributions are rejected;
* ``retry_pending_deliveries`` replays the durable backlog BEFORE new work on every tick.

Kept out of ``cron.scheduler`` so the facade stays a thin caller. ``cron.scheduler`` and
``cron.scheduler_delivery`` are late-bound through ``sys.modules`` so monkeypatching the facade
keeps working (tests patch ``cron.scheduler._deliver_result``).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("cron.scheduler")

# Monotonic Bot Chat receipt order: deduplicating one receipt key keeps the furthest-along status.
_RECEIPT_RANK = {
    "queued": 0,
    "claimed": 1,
    "suppressed": 2,
    "ambiguous": 3,
    "failed": 4,
    "cancelled": 5,
    "settled": 6,
}


def _receipt_rank(status: Any) -> int:
    return _RECEIPT_RANK.get(str(status or ""), -1)


def _normalize_execution_id(value: Any) -> Optional[str]:
    return str(value) if value else None


def _normalize_revision(value: Any) -> Optional[int]:
    return int(value) if value is not None else None


def _generation_token(job: dict) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """``(job_id, execution_id, projection_revision)`` as the CAS recorder would read them.

    Normalized so a token round-tripped through JSON (``"7"`` vs ``7``) still compares equal to
    the one the run captured; only a real identity change counts as a mutation.
    """
    return (
        str(job["id"]),
        _normalize_execution_id(job.get("execution_id")),
        _normalize_revision(job.get("_delivery_projection_revision")),
    )


@dataclass(frozen=True)
class DestinationDeliveryOutcome:
    """One concrete destination's contribution to a delivery run."""

    delivery_id: Optional[str]
    target: Dict[str, Any]
    status: str
    bot_chat_receipts: Tuple[Tuple[str, Dict[str, Any]], ...] = ()
    unverified_targets: Tuple[str, ...] = ()
    error: Optional[str] = None


class DeliveryRun:
    """Aggregate of one invocation's destination outcomes; finalizes exactly once."""

    def __init__(self, job: dict):
        self.job = job
        self.job_id = str(job["id"])
        self.execution_id = job.get("execution_id")
        self.projection_revision = job.get("_delivery_projection_revision")
        self._outcomes: List[DestinationDeliveryOutcome] = []
        self._finalized = False
        self.summary: Optional[Dict[str, Any]] = None

    @property
    def finalized(self) -> bool:
        return self._finalized

    def record(self, outcome: DestinationDeliveryOutcome) -> None:
        if self._finalized:
            raise RuntimeError(
                f"delivery run for job {self.job_id!r} is final; late outcome "
                f"{getattr(outcome, 'delivery_id', None)!r} rejected"
            )
        if not isinstance(outcome, DestinationDeliveryOutcome):
            raise TypeError("delivery run outcomes must be DestinationDeliveryOutcome")
        self._outcomes.append(outcome)

    def finalize(self) -> Optional[Dict[str, Any]]:
        """Merge receipts/unverified targets and publish ONCE under the owned generation.

        The generation is the one captured when the run opened. The job dict is shared with the
        scheduler and can be re-admitted (new execution id and revision) while this run is still
        open; the recorder reads its CAS tokens from that dict, so publishing then would stamp
        this run's outcome under the newer token. A mutated generation is rejected: the run
        still finalizes exactly once, but neither the job dict nor the store receives the
        superseded outcome.
        """
        if self._finalized:
            return None
        self._finalized = True
        captured = (self.job_id, _normalize_execution_id(self.execution_id),
                    _normalize_revision(self.projection_revision))
        current = _generation_token(self.job)
        if current != captured:
            logger.warning(
                "Job '%s': delivery run for execution %s (projection revision %s) not published: "
                "the job record now carries execution %s (projection revision %s), so the "
                "superseded outcome is rejected rather than stamped under the newer generation",
                self.job_id, captured[1], captured[2], current[1], current[2])
            self.summary = {
                "job_id": self.job_id,
                "execution_id": self.execution_id,
                "projection_revision": self.projection_revision,
                "outcomes": len(self._outcomes),
                "receipts": {},
                "unverified_targets": [],
                "errors": [o.error for o in self._outcomes if o.error],
                "rejected": "job generation changed before finalize",
            }
            return self.summary
        receipts: Dict[str, Dict[str, Any]] = {}
        unverified: List[str] = []
        for outcome in self._outcomes:
            for key, receipt in outcome.bot_chat_receipts:
                current = receipts.get(key)
                if current is None or _receipt_rank(receipt.get("status")) >= _receipt_rank(
                    current.get("status")
                ):
                    receipts[key] = dict(receipt)
            for target in outcome.unverified_targets:
                if target not in unverified:
                    unverified.append(target)
        job = self.job
        if receipts:
            job["_bot_chat_delivery_receipts"] = receipts
        else:
            job.pop("_bot_chat_delivery_receipts", None)
        if self._outcomes and all(o.status == "suppressed" for o in self._outcomes):
            job["_notification_all_targets_suppressed"] = True
        delivery = sys.modules["cron.scheduler_delivery"]
        # The recorder CASes on the invocation's own tokens (execution id + admitted revision) and
        # refuses partial tokens; it never consults the newest projection row.
        delivery._record_delivery_verification(job, list(unverified))
        job.setdefault("last_delivery_unverified", None)
        job.setdefault("last_delivery_queued", None)
        self.summary = {
            "job_id": self.job_id,
            "execution_id": self.execution_id,
            "projection_revision": self.projection_revision,
            "outcomes": len(self._outcomes),
            "receipts": receipts,
            "unverified_targets": list(unverified),
            "errors": [o.error for o in self._outcomes if o.error],
        }
        return self.summary


def _begin_delivery_run(job: dict) -> DeliveryRun:
    """Open the aggregate for one invocation (the job carries its own generation token, if any)."""
    return DeliveryRun(job)


def admit_run_delivery_generation(job: dict, execution_id: Optional[str]) -> Optional[int]:
    """Begin this execution's projection generation at run start and hand the token to the run.

    Best-effort: a store fault leaves the run without a token (recorders then refuse to write the
    modern projection rather than borrow one). The fanout admission below re-stamps the token with
    the revision its atomic publication actually created.
    """
    job.pop("_delivery_projection_revision", None)
    if not execution_id:
        return None
    try:
        from cron import outbox

        begun = outbox.begin_job_delivery_projection(str(job["id"]), str(execution_id))
    except Exception as exc:
        logger.warning(
            "Job '%s': could not begin the delivery projection generation for execution %s: %s",
            job.get("id"), execution_id, exc)
        return None
    revision = int(begun["revision"])
    job["_delivery_projection_revision"] = revision
    return revision


def admit_fanout_generation(job: dict, entries: Sequence[Dict[str, Any]]) -> Optional[int]:
    """Bind the invocation to the projection revision its fanout publication created.

    The published rows carry ``projection_revision`` from the same transaction that rotated the
    projection; when a caller hands rows without it, the projection is read ONCE and accepted only
    if it belongs to this very execution (same-execution admission read, not a borrowed identity).
    """
    execution_id = job.get("execution_id")
    job.pop("_delivery_projection_revision", None)
    if not execution_id:
        return None
    revisions = {
        int(entry["projection_revision"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("projection_revision") is not None
    }
    if len(revisions) > 1:
        logger.warning(
            "Job '%s': fanout rows carry conflicting projection revisions %s; refusing a token",
            job.get("id"), sorted(revisions))
        return None
    if revisions:
        revision = revisions.pop()
    else:
        from cron import outbox

        projection = outbox.get_job_delivery_projection(str(job["id"]))
        if (
            projection is None
            or str(projection.get("execution_id")) != str(execution_id)
            or projection.get("finalized")
        ):
            logger.warning(
                "Job '%s': no admitted projection generation for execution %s; callbacks will not "
                "publish the modern projection", job.get("id"), execution_id)
            return None
        revision = int(projection["revision"])
    job["_delivery_projection_revision"] = revision
    return revision


def _destination_label(destination: Dict[str, Any]) -> str:
    return f"{destination.get('platform')}:{destination.get('chat_id') or '(own)'}"


def _settled_error(
    outbox_id: str, destination: Dict[str, Any], state: Optional[dict]
) -> Optional[str]:
    """Translate one exact row's durable state into the run's delivery error (None = settled ok)."""
    if state is None:
        return f"exact delivery {outbox_id} for {_destination_label(destination)} has no durable row"
    exact = str(state.get("state") or "")
    if exact == "DELIVERED":
        return None
    if exact == "IN_FLIGHT" and state.get("transport_status") in ("queued", "claimed"):
        return None  # admitted to a Bot Chat owner; the receipt settles it
    if exact in ("RETRYABLE_FAILED", "DEAD", "UNKNOWN"):
        return str(state.get("error") or f"delivery {state.get('status') or exact.lower()}")
    return (
        f"delivery to {_destination_label(destination)} remains queued "
        f"({exact.lower() or 'pending'})"
    )


def attempt_concrete_deliveries(
    job: dict,
    content: str,
    entries: Sequence[Dict[str, Any]],
    destinations: Sequence[Dict[str, Any]],
    *,
    adapters: Any,
    loop: Any,
    for_failure: bool = False,
) -> Optional[str]:
    """Transport every published concrete intent under the owned generation; aggregate once.

    ``entries`` are the rows ``enqueue_deliveries_with_intent`` persisted (already durable) and
    ``destinations`` their concrete targets in the same order. Returns the aggregate delivery error
    (None when every destination settled or was admitted). A transport ``BaseException`` (shutdown)
    propagates after the queue released the claim; ordinary exceptions become the error.

    The caller owns the transport (gateway adapters or standalone senders). A restart-safe
    external worker never reaches this: it hands its send to the live gateway through the legacy
    durable queue (``_deliver_result``'s ``enqueue_and_wait`` branch), see
    ``cron.scheduler._deliver_run_result``.
    """
    from cron import delivery_queue as queue

    entries = list(entries)
    destinations = list(destinations)
    if len(entries) != len(destinations):
        raise ValueError("fanout entries and destinations must align one-to-one")
    sched = sys.modules["cron.scheduler"]
    outbox_ids = [str(entry["id"]) for entry in entries]
    execution_id = job.get("execution_id")
    admit_fanout_generation(job, entries)
    run = DeliveryRun(job)
    errors: List[str] = []

    # Queue ownership for every sibling, with the token-bearing job snapshot, before any send.
    for entry, destination in zip(entries, destinations):
        try:
            queue.enqueue(
                str(execution_id), job, content, for_failure=for_failure,
                destination=destination, outbox_id=str(entry["id"]))
        except Exception as exc:
            logger.error(
                "Job '%s': exact delivery %s could not be queue-configured: %s",
                job.get("id"), entry.get("id"), exc)
            errors.append(f"exact delivery admission failed: {exc}")

    def send(queued_job, queued_content, queued_for_failure, **exact):
        return sched._deliver_result(
            queued_job, queued_content, adapters=adapters, loop=loop,
            for_failure=bool(queued_for_failure), delivery_run=run, **exact)

    try:
        # A continuation-thread update re-admits a row once, so allow two passes per row.
        queue.drain(send, limit=len(outbox_ids) * 2, exact_outbox_ids=outbox_ids)
    except Exception as exc:
        logger.error("Job '%s': delivery queue drain failed: %s", job.get("id"), exc, exc_info=True)
        errors.append(f"delivery queue drain failed: {exc}")
    run.finalize()

    for entry, destination in zip(entries, destinations):
        outbox_id = str(entry["id"])
        try:
            state = queue.get_exact_state(outbox_id)
        except Exception as exc:
            errors.append(
                f"could not confirm delivery outcome for {_destination_label(destination)}: {exc}")
            continue
        error = _settled_error(outbox_id, destination, state)
        if error:
            errors.append(error)
    deduped = list(dict.fromkeys(error for error in errors if error))
    return "; ".join(deduped) if deduped else None


def retry_pending_deliveries(adapters=None, loop=None, limit: int = 20) -> int:
    """Replay the durable outbox backlog BEFORE new work; returns the number of legacy rows sent.

    Exact rows (``delivery_contract=1``) are queue-owned: this never resends them, it only releases
    ``RETRYABLE_FAILED`` rows back to ``READY`` so ``drain_delivery_queue`` claims them under a fresh
    generation. Legacy expression rows replay through ``_deliver_result`` against the job's CURRENT
    record (their lane preserved) and record one attempt each.
    """
    from cron import delivery_queue as queue
    from cron import jobs
    from cron import outbox

    sched = sys.modules["cron.scheduler"]
    replayed = 0
    for row in outbox.pending_outbox(limit=limit):
        outbox_id = str(row["id"])
        if int(row.get("delivery_contract") or 0) == outbox.EXACT_QUEUE:
            try:
                state = queue.get_exact_state(outbox_id)
            except Exception as exc:
                logger.debug("Outbox %s: exact state unavailable for retry: %s", outbox_id, exc)
                continue
            if state is not None and state.get("state") == "RETRYABLE_FAILED":
                queue.reactivate_exact(outbox_id)
            continue
        job = jobs.get_job(str(row["job_id"]))
        if job is None:
            outbox.record_attempt(
                outbox_id, status="failed", error="job no longer exists", abandon=True)
            continue
        for_failure = bool(row.get("for_failure"))
        try:
            error = sched._deliver_result(
                job, str(row["content"]), adapters=adapters, loop=loop, for_failure=for_failure)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            logger.error("Outbox replay failed for job %s: %s", row["job_id"], exc)
        outbox.record_attempt(outbox_id, status="failed" if error else "delivered", error=error)
        replayed += 1
    return replayed
