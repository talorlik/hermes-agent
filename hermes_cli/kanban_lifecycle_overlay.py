"""Fork-only guarded Kanban lifecycle transitions (ledger entry G-KANBAN-LIFECYCLE).

Database half of the guarded ``hermes kanban`` verbs; the flags and handlers
are in :mod:`hermes_cli.kanban_lifecycle_cli`. Nothing here replaces an
upstream name, so every other caller of ``kanban_db`` (dispatcher, model
tools, dashboard) runs upstream's mutators unchanged.

Each guarded function returns a :class:`LifecycleReceipt` only after its
write transaction has committed. Two shapes:

* ``guarded_comment`` and ``guarded_claim`` compose upstream's ``add_comment``
  and ``claim_task``; upstream changes to those reach them for free.
* ``guarded_complete``, ``guarded_block``, ``guarded_request_review`` and
  ``guarded_unblock`` must test the guard and write the reason comment inside
  the transaction that changes the status. Upstream's mutators open that
  transaction themselves and refuse to nest, so each of these four is
  upstream's body with the guard, comment and receipt lines added (marked
  ``# fork:``). When upstream changes ``complete_task``, ``block_task``,
  ``request_review`` or ``unblock_task``, re-diff the matching function here.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from hermes_cli.kanban_db import (
    VALID_BLOCK_KINDS,
    VALID_STATUSES,
    Comment,
    Event,
    LiveClaimError,
    Run,
    Task,
    _REVIEW_APPROVED_NOTE,
    _append_event,
    _canonical_assignee,
    _claim_is_live,
    _cleaned_artifact_paths,
    _cleanup_workspace,
    _clear_failure_counter,
    _completed_event_payload,
    _discard_staged_copies,
    _end_or_synthesize_run,
    _end_run,
    _fire_task_hook,
    _first_line,
    _flag_phantom_prose_refs,
    _gate_empty_completion,
    _landing_status_after_parents,
    _merge_completion_prose_artifacts,
    _parents_satisfied,
    _prior_reviewer,
    _reclaim_dangling_run,
    _resume_status_from_events,
    _retry_status_for_run,
    _route_block,
    _row_get,
    _stage_completion_artifacts,
    _synthesize_ended_run,
    add_comment,
    child_ids,
    claim_task,
    get_task,
    latest_summary,
    list_comments,
    list_events,
    list_runs,
    parent_ids,
    recompute_ready,
    redact_review_value,
)
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_output import _SHOW_RUN_FIELDS, _obj_dict, _task_to_dict

LIFECYCLE_RECEIPT_SCHEMA_VERSION = 1
TASK_SNAPSHOT_SCHEMA_VERSION = 1

# A dependency wait parks in ``todo``, never ``blocked``, so it can never match.
VALID_UNBLOCK_EXPECTED_KINDS = VALID_BLOCK_KINDS - {"dependency"}

# ``show --json`` is upstream's envelope plus fork additions, never a copy of
# upstream's field lists: the task and run objects come from upstream's own
# serializers, so a field upstream adds flows through without a change here.
# The fork adds ``schema_version``, these three task fields, and the row ``id``
# of each comment and event.
_SNAPSHOT_TASK_ADDITIONS = ("block_kind", "block_recurrences", "current_run_id")


def _row_fields(row_type: type) -> tuple[str, ...]:
    """A comment's or event's own fields: what upstream's ``show`` prints, plus ``id``.

    ``task_id`` is left out as upstream leaves it out; the envelope names the task once.
    """
    return tuple(f.name for f in dataclasses.fields(row_type) if f.name != "task_id")


@dataclass(frozen=True)
class LifecycleReceipt:
    """Versioned evidence for one committed lifecycle operation."""

    operation: str
    task_id: str
    prior_status: Optional[str]
    final_status: Optional[str]
    newly_committed: bool
    run_id: Optional[int] = None
    event_id: Optional[int] = None
    comment_id: Optional[int] = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LIFECYCLE_RECEIPT_SCHEMA_VERSION,
            "operation": self.operation,
            "task_id": self.task_id,
            "prior_status": self.prior_status,
            "final_status": self.final_status,
            "run_id": self.run_id,
            "event_id": self.event_id,
            "comment_id": self.comment_id,
            "newly_committed": self.newly_committed,
            "idempotent_replay": not self.newly_committed,
        }


def _check_expected_status(expected_status: Optional[str]) -> None:
    if expected_status is not None and expected_status not in VALID_STATUSES:
        raise ValueError(f"expected_status must be one of {sorted(VALID_STATUSES)}")


def _latest_event_id(conn: sqlite3.Connection, task_id: str, kind: str) -> Optional[int]:
    """Row id of the task's newest ``kind`` event.

    Called under the writer lock right after ``_append_event``, so it is the
    event this transaction wrote, whatever else that helper inserts.
    """
    row = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = ?", (task_id, kind),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _claimed_event_id(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = 'claimed' AND run_id = ? "
        "ORDER BY id DESC LIMIT 1", (task_id, run_id),
    ).fetchone()
    return int(row["id"]) if row else None


@contextlib.contextmanager
def _read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Pin every read in the block to one point in time (joins an open transaction)."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")  # read-only: nothing to keep


def guarded_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str, *,
    expected_status: Optional[str] = None, if_absent: bool = False,
) -> LifecycleReceipt:
    """Append a comment under a status guard; ``if_absent`` replays an identical one.

    The guard, the duplicate lookup and upstream's ``add_comment`` (a savepoint
    here) share one write transaction. A stale guard raises ``ValueError``.
    """
    _check_expected_status(expected_status)
    with write_txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown task {task_id}")
        status = row["status"]
        if expected_status is not None and status != expected_status:
            raise ValueError(
                f"refusing to comment: expected status {expected_status!r}, task is {status!r}"
            )
        if if_absent:
            existing = conn.execute(
                "SELECT id FROM task_comments WHERE task_id = ? AND author = ? AND body = ? "
                "ORDER BY id LIMIT 1", (task_id, author.strip(), body.strip()),
            ).fetchone()
            if existing is not None:
                return LifecycleReceipt(
                    operation="comment", task_id=task_id, prior_status=status, final_status=status,
                    newly_committed=False, comment_id=int(existing["id"]),
                )
        comment_id = add_comment(conn, task_id, author, body)
        return LifecycleReceipt(
            operation="comment", task_id=task_id, prior_status=status, final_status=status,
            newly_committed=True, event_id=_latest_event_id(conn, task_id, "commented"),
            comment_id=comment_id,
        )


def _claim_replay(
    conn: sqlite3.Connection, task_id: str, claimer: str,
) -> Optional[tuple[Optional[Task], Optional[LifecycleReceipt]]]:
    """Decide a repeated ``claim --claimer`` on a task that is already running.

    ``None``: the task is not running, so the caller attempts a fresh claim.
    ``(None, None)``: it is running but not provably under ``claimer``'s
    unexpired run. ``(task, receipt)``: ``claimer`` owns the current run; the
    receipt names the original run and ``claimed`` event.
    """
    with _read_snapshot(conn):
        current = get_task(conn, task_id)
        if current is None or current.status != "running":
            return None
        # A matching task lease alone is insufficient: the run row must agree.
        run = conn.execute(
            "SELECT id FROM task_runs WHERE id = ? AND task_id = ? "
            "AND status = 'running' AND ended_at IS NULL AND outcome IS NULL "
            "AND claim_lock = ? AND claim_expires = ? AND claim_expires >= ?",
            (current.current_run_id, task_id, claimer, current.claim_expires, int(time.time())),
        ).fetchone()
        event_id = _claimed_event_id(conn, task_id, current.current_run_id)
        if current.claim_lock != claimer or run is None or event_id is None:
            return None, None
        return current, LifecycleReceipt(
            operation="claim", task_id=task_id, prior_status="running", final_status="running",
            newly_committed=False, run_id=current.current_run_id, event_id=event_id,
        )


def guarded_claim(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[tuple[Task, LifecycleReceipt]]:
    """Claim through upstream's ``claim_task``; a named ``claimer`` may replay its own claim.

    ``None`` when the claim is refused.
    """
    if claimer is not None:
        replay = _claim_replay(conn, task_id, claimer)
        if replay is not None:
            task, receipt = replay
            return None if task is None or receipt is None else (task, receipt)
    claimed = claim_task(conn, task_id, ttl_seconds=ttl_seconds, claimer=claimer)
    if claimed is None:
        return None
    return claimed, LifecycleReceipt(
        operation="claim", task_id=task_id, prior_status="ready", final_status="running",
        newly_committed=True, run_id=claimed.current_run_id,
        event_id=_claimed_event_id(conn, task_id, claimed.current_run_id),
    )


def _observation_is_stale(
    conn: sqlite3.Connection, task_id: str, expected_status: Optional[str],
    expected_run_id: Optional[int],
) -> bool:
    """Cheap pre-check so a stale caller is refused before any audit side effect."""
    if expected_status is None and expected_run_id is None:
        return False
    observed = get_task(conn, task_id)
    if observed is None:
        return True
    if expected_status is not None and observed.status != expected_status:
        return True
    return expected_run_id is not None and observed.current_run_id != int(expected_run_id)


def guarded_complete(
    conn: sqlite3.Connection, task_id: str, *, result: Optional[str] = None,
    summary: Optional[str] = None, metadata: Optional[dict] = None,
    expected_run_id: Optional[int] = None, expected_status: Optional[str] = None,
    force: bool = False,
) -> Optional[LifecycleReceipt]:
    """Upstream ``complete_task`` plus an ``expected_status`` compare-and-swap.

    ``None`` on any refusal; raises what ``complete_task`` raises.
    """
    _check_expected_status(expected_status)  # fork:
    if _observation_is_stale(conn, task_id, expected_status, expected_run_id):  # fork:
        return None
    now = int(time.time())
    # Cheap pre-check; re-checked inside the txn to close the parent-reopen race.
    if not _parents_satisfied(conn, task_id):
        return None
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance, record_acceptance
    verified_cards: list[str] = []  # the CLI passes no created_cards
    _gate_empty_completion(conn, task_id, result=result, summary=summary)
    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    handoff_summary = summary if summary is not None else result
    acceptance = prepare_acceptance(conn, task_id, expected_run_id, metadata)
    if acceptance is False:
        return None
    with write_txn(conn):
        # Hard invariant even for human review approval: a parent may have
        # reopened while this task waited.
        if not _parents_satisfied(conn, task_id):
            return None
        if expected_status is not None:  # fork: decided under the writer lock, before any write
            fresh = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if fresh is None or fresh["status"] != expected_status:
                return None
        if acceptance is not None and not record_acceptance(conn, task_id, acceptance):
            return None
        trow = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        prior_status = trow["status"] if trow else None
        # Refuse to close a LIVE worker's run without proof of ownership
        # (expected_run_id) or an explicit human override (force=True); see
        # _claim_is_live for what "live" means.
        if expected_run_id is None and not force and trow and _claim_is_live(trow):
            raise LiveClaimError(task_id)
        sql = """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'review')
                """
        params: tuple = (result, now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return None
        if isinstance(metadata, dict):
            _stage_completion_artifacts(conn, task_id, metadata, now)
        run_id = _end_run(
            conn, task_id, outcome="completed", status="done", summary=handoff_summary,
            metadata=metadata,
        )
        # Never-claimed task: synthesize a run so the handoff fields survive.
        if run_id is None and (summary or metadata or result or prior_status == "review"):
            synth_summary, synth_metadata = handoff_summary, metadata
            if prior_status == "review" and not synth_summary and not synth_metadata:
                synth_summary = _REVIEW_APPROVED_NOTE
                synth_metadata = {"source_status": "review", "approval": "manual"}
            run_id = _synthesize_ended_run(
                conn, task_id, outcome="completed", summary=synth_summary, metadata=synth_metadata,
            )
        event_summary = handoff_summary
        if prior_status == "review" and not event_summary:
            event_summary = _REVIEW_APPROVED_NOTE
        _append_event(
            conn, task_id, "completed",
            _completed_event_payload(result, event_summary, verified_cards, metadata),
            run_id=run_id,
        )
        receipt = LifecycleReceipt(  # fork:
            operation="complete", task_id=task_id, prior_status=prior_status, final_status="done",
            newly_committed=True, run_id=run_id,
            event_id=_latest_event_id(conn, task_id, "completed"),
        )
    _flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)
    # Success wipes the breaker counter (history stays on the event log).
    _clear_failure_counter(conn, task_id)
    recompute_ready(conn)  # separate txn so children see ``done``
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    _fire_task_hook("kanban_task_completed", _done_task, task_id, run_id, summary=handoff_summary)
    return receipt


def guarded_block(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    kind: Optional[str] = None, expected_run_id: Optional[int] = None,
    expected_status: Optional[str] = None, reason_comment_author: Optional[str] = None,
) -> tuple[Optional[LifecycleReceipt], Optional[str]]:
    """Upstream ``block_task`` plus an ``expected_status`` compare-and-swap.

    With ``reason_comment_author`` the ``BLOCKED: reason`` comment is written
    in the block's own transaction. Returns ``(receipt, None)`` or
    ``(None, why)``.
    """
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
    _check_expected_status(expected_status)  # fork:
    with write_txn(conn):
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if cur_row is None:
            return None, "task not found"
        # fork: decided under the writer lock, so the UPDATE below needs no extra clause
        if expected_status is not None and cur_row["status"] != expected_status:
            return None, f"expected status {expected_status!r}, task is {cur_row['status']!r}"
        # The breaker (``_record_task_failure``) parks cards ``blocked`` with no
        # ``block_kind`` and no ``blocked`` event -- the policy is the
        # supervisor's, not the kernel's -- but the transition guard below only
        # matches running/ready, so that policy could never be attached later
        # (#117363). Classify in place; never re-type or flap status. A caller
        # asserting run ownership (``expected_run_id``) cannot own a parked
        # card -- its run is over -- so it is refused like any stale worker.
        if cur_row["status"] == "blocked":
            if kind is None or expected_run_id is not None or _row_get(cur_row, "block_kind") is not None:
                return None, "blocked card cannot be reclassified"
            classified = conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = 1 "
                "WHERE id = ? AND status = 'blocked' AND block_kind IS NULL "
                "AND current_run_id IS NULL",
                (kind, task_id),
            ).rowcount
            if classified != 1:
                return None, "blocked card changed concurrently"
            _append_event(conn, task_id, "blocked", {
                "kind": kind, "reason": reason, "classified_in_place": True,
            })
            return LifecycleReceipt(  # fork:
                operation="block", task_id=task_id, prior_status="blocked", final_status="blocked",
                newly_committed=True, event_id=_latest_event_id(conn, task_id, "blocked"),
            ), None
        source_status = _retry_status_for_run(conn, task_id) if cur_row["status"] == "running" else "ready"
        requested_kind = kind
        rekind_reason = None
        # ``dependency`` only waits on incomplete parents. A worker filing that
        # kind with none open would park in ``todo`` and ``recompute_ready``
        # would promote+respawn it context-free on the next tick. Re-kind to
        # ``needs_input`` so it is sticky until a human unblocks.
        if kind == "dependency" and _parents_satisfied(conn, task_id):
            kind = "needs_input"
            rekind_reason = "no_open_parent"
        new_status, event_kind, set_sql, params, payload = _route_block(
            kind, reason, source_status, prev_kind=_row_get(cur_row, "block_kind"),
            prev_recurrences=int(_row_get(cur_row, "block_recurrences") or 0),
        )
        if rekind_reason:
            payload["requested_kind"] = requested_kind
            payload["rekind_reason"] = rekind_reason
        sql = f"""
                UPDATE tasks
                   SET status        = '{new_status}',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       {set_sql}
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """
        params = (*params, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return None, "task changed concurrently"
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="blocked", status="blocked", summary=reason, synthesize=bool(reason),
        )
        _append_event(conn, task_id, event_kind, payload, run_id=run_id)
        event_id = _latest_event_id(conn, task_id, event_kind)  # fork:
        comment_id = None
        if reason and reason_comment_author:  # fork:
            comment_id = add_comment(conn, task_id, reason_comment_author, f"BLOCKED: {reason}")
        receipt = LifecycleReceipt(  # fork:
            operation="block", task_id=task_id, prior_status=cur_row["status"],
            final_status=new_status, newly_committed=True, run_id=run_id, event_id=event_id,
            comment_id=comment_id,
        )
        blocked_task = get_task(conn, task_id)
    _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
    return receipt, None


def guarded_request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, expected_status: Optional[str] = None,
    force: bool = False,
) -> tuple[Optional[LifecycleReceipt], Optional[str]]:
    """Upstream ``request_review`` plus an ``expected_status`` compare-and-swap.

    Returns ``(receipt, None)`` or ``(None, why)``.
    """
    _check_expected_status(expected_status)  # fork:
    if expected_status is not None:  # fork: refuse before artifacts are merged
        observed = get_task(conn, task_id)
        if observed is None:
            return None, "task not found"
        if observed.status != expected_status:
            return None, f"expected status {expected_status!r}, task is {observed.status!r}"
    summary = redact_review_value(summary)
    metadata = redact_review_value(metadata)
    # Declared (metadata["artifacts"]) and prose-referenced files
    # must be durable BEFORE anything can clean the scratch workspace up: for a
    # review-bound card the reviewer's completion is the cleanup trigger.
    metadata = _merge_completion_prose_artifacts(conn, task_id, metadata, summary=summary, result=None)
    now = int(time.time())
    # Staged copies live outside the txn: a rollback after staging must not
    # leave orphans that make the retry stage ``name_1.ext`` beside them.
    staged_copies: list[Path] = []
    try:
        with write_txn(conn):
            if not _parents_satisfied(conn, task_id):
                return None, "parent dependencies are not satisfied"
            trow = conn.execute(
                "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
                "worker_started_at FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if trow is None:
                return None, "task not found"
            if expected_status is not None and trow["status"] != expected_status:  # fork:
                return None, f"expected status {expected_status!r}, task is {trow['status']!r}"
            # Refuse to clear a live worker's claim without proof of ownership
            # (expected_run_id) or an explicit human override (force=True);
            # the same fence as complete_task (_claim_is_live).
            if expected_run_id is None and not force and _claim_is_live(trow):
                return (
                    None, "task is running under a live claim; pass expected_run_id "
                    "(worker ownership) or force=True (explicit operator "
                    "override) instead of clearing the live run's claim",
                )
            if reviewer is None:
                reviewer = _prior_reviewer(conn, task_id)
                if reviewer is False:
                    return (
                        None, "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
            reviewer = _canonical_assignee(reviewer)
            # The actor is the run that did the work. ``assignee`` is the actor
            # only while a worker holds the card; on a never-claimed card it is
            # whoever the operator assigned -- possibly the reviewer itself,
            # which is what ``kanban create --assignee <reviewer>`` followed by
            # ``request-review`` produces. Recording the reviewer as its own
            # implementer is worse than recording nothing: request_changes()
            # routes on this field, and it already refuses a handoff that
            # carries no implementer provenance.
            implementer = None
            if trow["current_run_id"] is not None:
                arow = conn.execute(
                    "SELECT profile FROM task_runs WHERE id = ?",
                    (trow["current_run_id"],),
                ).fetchone()
                implementer = arow["profile"] if arow else None
            if implementer is None and trow["assignee"] != reviewer:
                implementer = trow["assignee"]
            assignee_sql = ", assignee = ?" if reviewer is not None else ""
            run_guard = "" if expected_run_id is None else " AND current_run_id = ?"
            params: tuple[Any, ...] = (
                *(() if reviewer is None else (reviewer,)), task_id,
                *(() if expected_run_id is None else (int(expected_run_id),)),
            )
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'review',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL
                """ + assignee_sql + """
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """ + run_guard,
                params,
            )
            if cur.rowcount != 1:
                return None, "task is not in running/ready (or expected_run_id did not match the current run)"
            if isinstance(metadata, dict):
                staged_copies = _stage_completion_artifacts(
                    conn, task_id, metadata, now, uploaded_by="kanban_request_review",
                )
            run_id = _end_or_synthesize_run(
                conn, task_id, outcome="review_requested", status="review",
                summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
                profile=implementer,
            )
            payload: dict = {
                "summary": _first_line(summary, 400) or None,
                "implementer": implementer,
                "reviewer": reviewer,
            }
            staged = _cleaned_artifact_paths(metadata)
            if staged:
                payload["artifacts"] = staged
            _append_event(conn, task_id, "review_requested", payload, run_id=run_id)
            receipt = LifecycleReceipt(  # fork:
                operation="request_review", task_id=task_id, prior_status=trow["status"],
                final_status="review", newly_committed=True, run_id=run_id,
                event_id=_latest_event_id(conn, task_id, "review_requested"),
            )
    except Exception:
        if staged_copies:
            _discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise
    return receipt, None


def guarded_unblock(
    conn: sqlite3.Connection, task_id: str, *, expected_block_kind: Optional[str] = None,
    reason: Optional[str] = None, reason_comment_author: Optional[str] = None,
) -> Optional[LifecycleReceipt]:
    """Upstream ``unblock_task`` plus an ``expected_block_kind`` compare-and-swap.

    With ``reason_comment_author`` the ``UNBLOCK: reason`` comment is written
    in the unblock's own transaction. ``None`` on any refusal.
    """
    if expected_block_kind is not None and expected_block_kind not in VALID_UNBLOCK_EXPECTED_KINDS:
        raise ValueError(
            f"expected_block_kind must be one of {sorted(VALID_UNBLOCK_EXPECTED_KINDS)}"
        )
    if reason is not None:
        reason = redact_review_value(reason)
        if not isinstance(reason, str) or not reason.strip():
            reason = None
    now = int(time.time())
    with write_txn(conn):
        current = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if expected_block_kind is not None and (  # fork:
            current is None
            or current["status"] != "blocked"
            or current["block_kind"] != expected_block_kind
        ):
            return None
        resume_status = (
            _resume_status_from_events(conn, task_id)
            if current is not None and current["status"] == "blocked"
            else "ready"
        )
        _reclaim_dangling_run(
            conn, task_id, statuses=("blocked", "scheduled"), now=now,
            note="invariant recovery on unblock",
        )
        # Re-gate on parent completion before restoring the source phase.
        landing_status = _landing_status_after_parents(conn, task_id)
        new_status = (
            "review"
            if landing_status == "ready" and resume_status == "review"
            else landing_status
        )
        # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
        # resetting them is the amnesia that let cron-unblock <-> re-block loop
        # unbounded; only complete_task clears them. ``consecutive_failures``
        # (the dispatcher's spawn/crash counter) IS reset — a deliberate unblock
        # is a fresh start for the retry budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')", (new_status, task_id),
        )
        if cur.rowcount != 1:
            return None
        comment_id = None
        if reason and reason_comment_author:  # fork:
            comment_id = add_comment(conn, task_id, reason_comment_author, f"UNBLOCK: {reason}")
        payload: Optional[dict] = (
            {"status": new_status, "resume_status": resume_status}
            if new_status != "ready" or resume_status != "ready"
            else None
        )
        if reason:  # fork: the guarded unblock records why on its own event
            payload = {**(payload or {}), "reason": reason}
        _append_event(conn, task_id, "unblocked", payload)
        return LifecycleReceipt(  # fork:
            operation="unblock", task_id=task_id, prior_status=current["status"],
            final_status=new_status, newly_committed=True,
            event_id=_latest_event_id(conn, task_id, "unblocked"), comment_id=comment_id,
        )


@dataclass
class KanbanTaskSnapshot:
    """One task's durable state captured at a single point in time."""

    task: Task
    parents: list[str]
    children: list[str]
    comments: list[Comment]
    events: list[Event]
    runs: list[Run]
    latest_summary: Optional[str]
    schema_version: int = TASK_SNAPSHOT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Upstream's ``show --json`` envelope plus the fork's additions."""
        task = _task_to_dict(self.task)
        task.update(_obj_dict(self.task, _SNAPSHOT_TASK_ADDITIONS))
        comment_fields, event_fields = _row_fields(Comment), _row_fields(Event)
        return {
            "schema_version": self.schema_version,
            "task": task,
            "latest_summary": self.latest_summary,
            "parents": list(self.parents),
            "children": list(self.children),
            "comments": [_obj_dict(c, comment_fields) for c in self.comments],
            "events": [_obj_dict(e, event_fields) for e in self.events],
            "runs": [_obj_dict(r, _SHOW_RUN_FIELDS) for r in self.runs],
        }


def build_task_snapshot(
    conn: sqlite3.Connection, task_id: str, *, run_filter: Optional[dict[str, str]] = None,
) -> Optional[KanbanTaskSnapshot]:
    """Capture one task and every ``show`` collection under one read transaction.

    ``run_filter`` is the ``state_type``/``state_name`` pair ``list_runs`` takes.
    """
    with _read_snapshot(conn):
        task = get_task(conn, task_id)
        if task is None:
            return None
        return KanbanTaskSnapshot(
            task=task,
            parents=parent_ids(conn, task_id),
            children=child_ids(conn, task_id),
            comments=list_comments(conn, task_id),
            events=list_events(conn, task_id),
            runs=list_runs(conn, task_id, **(run_filter or {})),
            latest_summary=latest_summary(conn, task_id),
        )
