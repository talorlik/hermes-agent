"""Fork lifecycle contracts bound onto the current upstream Kanban database.

The integrate fitter drops a rewritten upstream function body. These contracts
therefore live in a fork-only module and replace the published names after
upstream's module has finished loading.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_UPSTREAM_HELPERS = (
    "Any",
    "Comment",
    "Event",
    "Iterable",
    "LiveClaimError",
    "Optional",
    "Path",
    "Run",
    "Task",
    "VALID_BLOCK_KINDS",
    "VALID_STATUSES",
    "_REVIEW_APPROVED_NOTE",
    "_append_event",
    "_canonical_assignee",
    "_claim_and_open_run",
    "_claim_is_live",
    "_claimer_id",
    "_cleaned_artifact_paths",
    "_cleanup_workspace",
    "_clear_failure_counter",
    "_completed_event_payload",
    "_discard_staged_copies",
    "_end_or_synthesize_run",
    "_end_run",
    "_fire_task_hook",
    "_first_line",
    "_flag_phantom_prose_refs",
    "_gate_created_cards",
    "_gate_empty_completion",
    "_landing_status_after_parents",
    "_merge_completion_prose_artifacts",
    "_parents_satisfied",
    "_prior_reviewer",
    "_reclaim_dangling_run",
    "_resolve_claim_ttl_seconds",
    "_resume_status_from_events",
    "_retry_status_for_run",
    "_route_block",
    "_row_get",
    "_stage_completion_artifacts",
    "_synthesize_ended_run",
    "child_ids",
    "get_task",
    "latest_summary",
    "list_comments",
    "list_events",
    "list_runs",
    "parent_ids",
    "recompute_ready",
    "redact_review_value",
    "sqlite3",
    "time",
    "write_txn",
)


def _install_upstream_helpers() -> None:
    """Bind helper names from the partially initialized upstream module."""
    from hermes_cli import kanban_db as _db

    for _name in _UPSTREAM_HELPERS:
        globals()[_name] = getattr(_db, _name)


_install_upstream_helpers()


VALID_UNBLOCK_EXPECTED_KINDS = VALID_BLOCK_KINDS - {"dependency"}


def add_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str, *,
    expected_status: Optional[str] = None, if_absent: bool = False,
    receipt_capture: Optional[Any] = None,
) -> int:
    """Append a guarded comment, optionally replaying an existing canonical body."""
    _install_upstream_helpers()
    from hermes_cli.kanban_db_connect import (
        LifecycleReceipt,
        _clear_receipt_capture,
        _stage_receipt,
    )

    _clear_receipt_capture(conn, receipt_capture)
    if expected_status is not None and expected_status not in VALID_STATUSES:
        raise ValueError(f"expected_status must be one of {sorted(VALID_STATUSES)}")
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    # ``allow_nested=True``: graph builders (kanban_swarm blackboard seeding)
    # compose comment writes under one outer commit.
    with write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown task {task_id}")
        if expected_status is not None and row["status"] != expected_status:
            raise ValueError(
                f"refusing to comment: expected status {expected_status!r}, task is {row['status']!r}"
            )
        # The guard precedes dedup, and both run under the same writer lock.
        existing = None
        if if_absent:
            existing = conn.execute(
                "SELECT id FROM task_comments WHERE task_id = ? AND author = ? "
                "AND body = ? ORDER BY id LIMIT 1",
                (task_id, author.strip(), body.strip()),
            ).fetchone()
        event_id = None
        if existing is not None:
            comment_id = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)", (task_id, author.strip(), body.strip(), now),
            )
            comment_id = int(cur.lastrowid or 0)
            _append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
            event_row = conn.execute("SELECT last_insert_rowid()").fetchone()
            event_id = int(event_row[0]) if event_row and event_row[0] else None
        _stage_receipt(
            conn, receipt_capture,
            LifecycleReceipt(
                operation="comment", task_id=task_id,
                prior_status=row["status"], final_status=row["status"],
                newly_committed=existing is None,
                event_id=event_id, comment_id=comment_id,
            ),
        )
        return comment_id


def claim_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None, idempotent_replay: bool = False,
    receipt_capture: Optional[Any] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status). Replay requires an
    explicit claimer and the same unexpired, authoritative current run.
    """
    _install_upstream_helpers()
    from hermes_cli.kanban_db_connect import (
        LifecycleReceipt,
        _clear_receipt_capture,
        _stage_receipt,
    )

    _clear_receipt_capture(conn, receipt_capture)
    if idempotent_replay and not claimer:
        raise ValueError("idempotent_replay requires an explicit claimer")
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        if idempotent_replay:
            current = get_task(conn, task_id)
            if current is not None and current.status == "running":
                # Validate both sides of the run pointer while holding the
                # writer lock; a matching task lease alone is insufficient.
                run = conn.execute(
                    "SELECT id FROM task_runs WHERE id = ? AND task_id = ? "
                    "AND status = 'running' AND ended_at IS NULL AND outcome IS NULL "
                    "AND claim_lock = ? AND claim_expires = ? AND claim_expires >= ?",
                    (current.current_run_id, task_id, lock, current.claim_expires,
                     int(time.time())),
                ).fetchone()
                if current.claim_lock != lock or run is None:
                    return None
                event = conn.execute(
                    "SELECT id FROM task_events WHERE task_id = ? "
                    "AND kind = 'claimed' AND run_id = ? ORDER BY id DESC LIMIT 1",
                    (task_id, current.current_run_id),
                ).fetchone()
                if event is None:
                    return None
                _stage_receipt(
                    conn, receipt_capture,
                    LifecycleReceipt(
                        operation="claim", task_id=task_id,
                        prior_status="running", final_status="running",
                        newly_committed=False, run_id=current.current_run_id,
                        event_id=event["id"],
                    ),
                )
                return current
        # Single enforcement point: never ready -> running with an undone
        # parent, whichever writer set 'ready'. Demote to 'todo';
        # recompute_ready re-promotes when the parents finish.
        if not _parents_satisfied(conn, task_id):
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'", (task_id,),
            )
            _append_event(conn, task_id, "claim_rejected", {"reason": "parents_not_done"})
            return None
        # Close a leaked prior run so the CAS below doesn't strand it.
        _reclaim_dangling_run(
            conn, task_id, statuses=("ready",), now=now, note="invariant recovery on re-claim",
        )
        run_id = _claim_and_open_run(conn, task_id, "ready", lock, expires, now)
        if run_id is None:
            return None
        claimed = get_task(conn, task_id)
        event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? "
            "AND kind = 'claimed' AND run_id = ? ORDER BY id DESC LIMIT 1",
            (task_id, run_id),
        ).fetchone()
        _stage_receipt(
            conn, receipt_capture,
            LifecycleReceipt(
                operation="claim", task_id=task_id,
                prior_status="ready", final_status="running",
                newly_committed=True, run_id=run_id,
                event_id=event["id"] if event else None,
            ),
        )
    _fire_task_hook("kanban_task_claimed", claimed, task_id, run_id)
    return claimed


def complete_task(
    conn: sqlite3.Connection, task_id: str, *, result: Optional[str] = None,
    summary: Optional[str] = None, metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None, expected_run_id: Optional[int] = None,
    expected_status: Optional[str] = None,
    fire_lifecycle_hook: bool = True, force: bool = False,
    receipt_capture: Optional[Any] = None,
) -> bool:
    """``running|ready|blocked|review -> done``; records ``result``.

    ``ready`` is accepted for manual CLI completion, ``review`` for human
    approval. A ``running`` task under a live claim is only completed with
    proof of ownership (``expected_run_id``) or ``force=True`` (explicit
    operator override) — otherwise :class:`LiveClaimError`, the same fence
    :func:`request_review` applies. With no active run the handoff fields survive via
    :func:`_synthesize_ended_run`. ``summary`` (defaults to ``result``) and
    ``metadata`` land on the closing run for :func:`build_worker_context`.
    ``created_cards`` are verified first — a phantom id raises
    :class:`HallucinatedCardsError` after an auditable event; afterwards the
    prose is scanned for unresolvable ``t_<hex>`` refs (advisory event only).
    Completions from non-review statuses need evidence: a stripped ``result``
    or ``summary``, or a stripped result already stored on the card. Empty or
    whitespace-only evidence raises :class:`EmptyCompletionError` after an
    auditable event. Approving a card out of ``review`` stays exempt.

    ``expected_status`` is a compare-and-swap guard. A typo raises. A stale
    observation returns False before card-audit or acceptance side effects,
    and is checked again inside the write transaction. ``receipt_capture``
    publishes the committed completion; a mismatch publishes nothing.
    """
    _install_upstream_helpers()
    from hermes_cli.kanban_db_connect import (
        LifecycleReceipt,
        _clear_receipt_capture,
        _stage_receipt,
    )

    _clear_receipt_capture(conn, receipt_capture)
    if expected_status is not None and expected_status not in VALID_STATUSES:
        raise ValueError(
            f"expected_status must be one of {sorted(VALID_STATUSES)}"
        )
    if expected_status is not None or expected_run_id is not None:
        observed = get_task(conn, task_id)
        if observed is None:
            return False
        if expected_status is not None and observed.status != expected_status:
            return False
        if (
            expected_run_id is not None
            and observed.current_run_id != int(expected_run_id)
        ):
            return False
    now = int(time.time())
    # Cheap pre-check; re-checked inside the txn to close the parent-reopen race.
    if not _parents_satisfied(conn, task_id):
        return False
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance, record_acceptance
    verified_cards = _gate_created_cards(conn, task_id, created_cards, summary or result)
    _gate_empty_completion(conn, task_id, result=result, summary=summary)
    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    handoff_summary = summary if summary is not None else result
    acceptance = prepare_acceptance(conn, task_id, expected_run_id, metadata)
    if acceptance is False:
        return False
    with write_txn(conn):
        # Hard invariant even for human review approval: a parent may have
        # reopened while this task waited.
        if not _parents_satisfied(conn, task_id):
            return False
        if expected_status is not None:
            fresh = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if fresh is None or fresh["status"] != expected_status:
                return False
        if acceptance is not None and not record_acceptance(conn, task_id, acceptance):
            return False
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
        if expected_status is not None:
            sql += " AND status = ?"
            params = (*params, expected_status)
        if conn.execute(sql, params).rowcount != 1:
            return False
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
        event_row = conn.execute("SELECT last_insert_rowid()").fetchone()
        event_id = int(event_row[0]) if event_row and event_row[0] else None
        _stage_receipt(
            conn,
            receipt_capture,
            LifecycleReceipt(
                operation="complete",
                task_id=task_id,
                prior_status=prior_status,
                final_status="done",
                newly_committed=True,
                run_id=run_id,
                event_id=event_id,
            ),
        )
    _flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)
    # Success wipes the breaker counter (history stays on the event log).
    _clear_failure_counter(conn, task_id)
    recompute_ready(conn)  # separate txn so children see ``done``
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    if fire_lifecycle_hook:
        _fire_task_hook("kanban_task_completed", _done_task, task_id, run_id, summary=handoff_summary)
    return True


def block_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    kind: Optional[str] = None, expected_run_id: Optional[int] = None,
    expected_status: Optional[str] = None,
    reason_comment_author: Optional[str] = None,
    with_reason: bool = False,
    receipt_capture: Optional[Any] = None,
):
    """``running``/``ready`` -> ``blocked`` (or ``todo`` / ``triage``, see
    :func:`_route_block`). ``kind='dependency'`` with no incomplete parent is
    re-kinded to ``needs_input`` (sticky) so ``recompute_ready`` cannot
    promote it into a context-free respawn. ``transient`` still counts
    toward the loop breaker so a forever-flaky task escalates. True on any
    transition. When ``with_reason`` is set, the return is ``(ok, why)``.
    ``expected_status`` is a compare-and-swap guard. ``receipt_capture``
    publishes the committed block for ``kanban block --json``.

    An already-``blocked`` card that the failure breaker parked UNTYPED
    (``block_kind IS NULL``, no live run) is classified in place when *kind*
    is supplied: ``block_kind``/``block_recurrences`` are set and a ``blocked``
    audit event is appended, while status, failure evidence and the terminal
    runs stay exactly as the breaker left them. A typed block, a card with a
    live run, or a kind-less call on a blocked card are still refused.
    """
    _install_upstream_helpers()
    from hermes_cli.kanban_db_connect import (
        LifecycleReceipt,
        _clear_receipt_capture,
        _stage_receipt,
    )

    def _ret(ok: bool, why: Optional[str] = None):
        return (ok, why) if with_reason else ok

    _clear_receipt_capture(conn, receipt_capture)
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
    if expected_status is not None and expected_status not in VALID_STATUSES:
        raise ValueError(f"expected_status must be one of {sorted(VALID_STATUSES)}")
    with write_txn(conn):
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if cur_row is None:
            return _ret(False, "task not found")
        if expected_status is not None and cur_row["status"] != expected_status:
            return _ret(
                False,
                f"expected status {expected_status!r}, task is {cur_row['status']!r}",
            )
        # The breaker (``_record_task_failure``) parks cards ``blocked`` with no
        # ``block_kind`` and no ``blocked`` event -- the policy is the
        # supervisor's, not the kernel's -- but the transition guard below only
        # matches running/ready, so that policy could never be attached later
        # (#117363). Classify in place; never re-type or flap status. A caller
        # asserting run ownership (``expected_run_id``) cannot own a parked
        # card -- its run is over -- so it is refused like any stale worker.
        if cur_row["status"] == "blocked":
            if kind is None or expected_run_id is not None or _row_get(cur_row, "block_kind") is not None:
                return _ret(False, "blocked card cannot be reclassified")
            classified = conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = 1 "
                "WHERE id = ? AND status = 'blocked' AND block_kind IS NULL "
                "AND current_run_id IS NULL",
                (kind, task_id),
            ).rowcount
            if classified != 1:
                return _ret(False, "blocked card changed concurrently")
            event_id = _append_event(conn, task_id, "blocked", {
                "kind": kind, "reason": reason, "classified_in_place": True,
            })
            _stage_receipt(
                conn, receipt_capture,
                LifecycleReceipt(
                    operation="block", task_id=task_id,
                    prior_status="blocked", final_status="blocked",
                    newly_committed=True, run_id=None, event_id=event_id,
                    comment_id=None,
                ),
            )
            return _ret(True)
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
        if expected_status is not None:
            sql += " AND status = ?"
            params = (*params, expected_status)
        if conn.execute(sql, params).rowcount != 1:
            return _ret(False, "task changed concurrently")
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="blocked", status="blocked", summary=reason, synthesize=bool(reason),
        )
        event_id = _append_event(conn, task_id, event_kind, payload, run_id=run_id)
        comment_id = None
        if reason and reason_comment_author:
            comment_id = add_comment(conn, task_id, reason_comment_author, f"BLOCKED: {reason}")
        _stage_receipt(
            conn, receipt_capture,
            LifecycleReceipt(
                operation="block", task_id=task_id,
                prior_status=cur_row["status"], final_status=new_status,
                newly_committed=True, run_id=run_id, event_id=event_id,
                comment_id=comment_id,
            ),
        )
        blocked_task = get_task(conn, task_id)
        if kind == "dependency":
            # Historical ordering: the dependency lane fires inside the txn.
            _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
            return _ret(True)
    _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
    return _ret(True)


def request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, force: bool = False, with_reason: bool = False,
    expected_status: Optional[str] = None,
    receipt_capture: Optional[Any] = None,
):
    """``running``/``ready`` -> ``review``; never touches block recurrence accounting.

    Implementer and reviewer are recorded on the event so requested changes
    route back to the right profile; ``reviewer`` reassigns the task, and on
    re-review defaults to the latest ``changes_requested`` provenance. A live
    claim is only cleared with proof of ownership (``expected_run_id``) or
    ``force=True``. Returns ``bool``, or ``(ok, reason)`` with ``with_reason``.

    ``metadata["artifacts"]`` names the handoff's deliverable
    files; a review handoff is the last implementer transition, and the
    *reviewer's* completion is what cleans the managed scratch workspace up, so
    the files are staged into the task's durable attachments dir here and the
    staged paths ride the ``review_requested`` payload for the notifier to
    upload. A declared artifact that cannot be preserved raises
    :class:`ArtifactPreservationError`, rolling the whole transition back: the
    task stays ``running`` and retryable, with no attachments and no event.
    """
    _install_upstream_helpers()

    from hermes_cli.kanban_db_connect import (
        LifecycleReceipt,
        ReceiptFinalizationError,
        TransactionOutcomeUnknownError,
        _clear_receipt_capture,
        _stage_receipt,
    )

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    _clear_receipt_capture(conn, receipt_capture)
    if expected_status is not None and expected_status not in VALID_STATUSES:
        raise ValueError(f"expected_status must be one of {sorted(VALID_STATUSES)}")
    if expected_status is not None:
        observed = get_task(conn, task_id)
        if observed is None:
            return _ret(False, "task not found")
        if observed.status != expected_status:
            return _ret(False, f"expected status {expected_status!r}, task is {observed.status!r}")
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
                return _ret(False, "parent dependencies are not satisfied")
            trow = conn.execute(
                "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
                "worker_started_at FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if trow is None:
                return _ret(False, "task not found")
            if expected_status is not None and trow["status"] != expected_status:
                return _ret(
                    False, f"expected status {expected_status!r}, task is {trow['status']!r}",
                )
            # Refuse to clear a live worker's claim without proof of ownership
            # (expected_run_id) or an explicit human override (force=True);
            # the same fence as complete_task (_claim_is_live).
            if expected_run_id is None and not force and _claim_is_live(trow):
                return _ret(
                    False, "task is running under a live claim; pass expected_run_id "
                    "(worker ownership) or force=True (explicit operator "
                    "override) instead of clearing the live run's claim",
                )
            if reviewer is None:
                reviewer = _prior_reviewer(conn, task_id)
                if reviewer is False:
                    return _ret(
                        False, "re-review has no durable reviewer provenance (the "
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
                return _ret(
                    False, "task is not in running/ready (or expected_run_id did not match the current run)",
                )
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
            event_row = conn.execute("SELECT last_insert_rowid()").fetchone()
            event_id = int(event_row[0]) if event_row and event_row[0] else None
            _stage_receipt(
                conn, receipt_capture,
                LifecycleReceipt(
                    operation="request_review", task_id=task_id,
                    prior_status=trow["status"], final_status="review",
                    newly_committed=True, run_id=run_id, event_id=event_id,
                ),
            )
    except (ReceiptFinalizationError, TransactionOutcomeUnknownError):
        # A committed or uncertain transaction can reference these copies;
        # deleting them would destroy the durable review evidence.
        raise
    except Exception:
        if staged_copies:
            _discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise
    return _ret(True)


def unblock_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_block_kind: Optional[str] = None,
    reason: Optional[str] = None,
    reason_comment_author: Optional[str] = None,
    receipt_capture: Optional[Any] = None,
) -> bool:
    """``blocked``/``scheduled`` -> its resumable phase, unless a typed-kind guard mismatches.

    An ``expected_block_kind`` demands a blocked task of that exact kind and
    refuses before any mutation. Guarded reason comments, the event, and the
    receipt share the status-change transaction. ``block_kind`` survives so a
    same-kind re-block still counts as a loop.
    """
    _install_upstream_helpers()
    from hermes_cli.kanban_db_connect import (
        LifecycleReceipt,
        _clear_receipt_capture,
        _stage_receipt,
    )

    _clear_receipt_capture(conn, receipt_capture)
    if (
        expected_block_kind is not None
        and expected_block_kind not in VALID_UNBLOCK_EXPECTED_KINDS
    ):
        raise ValueError(
            "expected_block_kind must be one of "
            f"{sorted(VALID_UNBLOCK_EXPECTED_KINDS)}"
        )
    if reason is not None:
        reason = redact_review_value(reason)
        if not isinstance(reason, str) or not reason.strip():
            reason = None
    now = int(time.time())
    with write_txn(conn):
        current = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if expected_block_kind is not None and (
            current is None
            or current["status"] != "blocked"
            or current["block_kind"] != expected_block_kind
        ):
            return False
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
            "WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        comment_id = None
        if reason and reason_comment_author:
            comment_id = add_comment(
                conn, task_id, reason_comment_author, f"UNBLOCK: {reason}",
            )
        payload: Optional[dict] = (
            {"status": new_status, "resume_status": resume_status}
            if new_status != "ready" or resume_status != "ready"
            else None
        )
        if reason:
            payload = dict(payload or {})
            payload["reason"] = reason
        _append_event(conn, task_id, "unblocked", payload)
        event_row = conn.execute("SELECT last_insert_rowid()").fetchone()
        event_id = int(event_row[0]) if event_row and event_row[0] else None
        _stage_receipt(
            conn,
            receipt_capture,
            LifecycleReceipt(
                operation="unblock",
                task_id=task_id,
                prior_status=current["status"] if current is not None else None,
                final_status=new_status,
                newly_committed=True,
                event_id=event_id,
                comment_id=comment_id,
            ),
        )
        return True


TASK_SNAPSHOT_SCHEMA_VERSION = 1


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
        """Serialize the legacy show envelope plus versioned lifecycle fields."""
        task = self.task
        task_payload = {
            "id": task.id,
            "title": task.title,
            "body": task.body,
            "assignee": task.assignee,
            "status": task.status,
            "priority": task.priority,
            "tenant": task.tenant,
            "workspace_kind": task.workspace_kind,
            "workspace_path": task.workspace_path,
            "branch_name": task.branch_name,
            "project_id": task.project_id,
            "created_by": task.created_by,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "result": task.result,
            "skills": list(task.skills) if task.skills else [],
            "max_retries": task.max_retries,
            "model_override": task.model_override,
            "provider_override": task.provider_override,
            "session_id": task.session_id,
            "workflow_template_id": task.workflow_template_id,
            "current_step_key": task.current_step_key,
            "completion_contract": task.completion_contract,
            "last_failure_error": task.last_failure_error,
            "block_kind": task.block_kind,
            "block_recurrences": task.block_recurrences,
            "current_run_id": task.current_run_id,
        }
        return {
            "schema_version": self.schema_version,
            "task": task_payload,
            "latest_summary": self.latest_summary,
            "parents": list(self.parents),
            "children": list(self.children),
            "comments": [
                {
                    "id": comment.id,
                    "author": comment.author,
                    "body": comment.body,
                    "created_at": comment.created_at,
                }
                for comment in self.comments
            ],
            "events": [
                {
                    "id": event.id,
                    "kind": event.kind,
                    "payload": event.payload,
                    "created_at": event.created_at,
                    "run_id": event.run_id,
                }
                for event in self.events
            ],
            "runs": [
                {
                    "id": run.id,
                    "profile": run.profile,
                    "step_key": run.step_key,
                    "status": run.status,
                    "outcome": run.outcome,
                    "summary": run.summary,
                    "error": run.error,
                    "metadata": run.metadata,
                    "worker_pid": run.worker_pid,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                }
                for run in self.runs
            ],
        }


def build_task_snapshot(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_state_type: Optional[str] = None,
    run_state_name: Optional[str] = None,
) -> Optional[KanbanTaskSnapshot]:
    """Capture one task and all show collections under one read snapshot."""
    _install_upstream_helpers()
    from hermes_cli.kanban_db_connect import read_txn
    with read_txn(conn):
        task = get_task(conn, task_id)
        if task is None:
            return None
        return KanbanTaskSnapshot(
            task=task,
            parents=parent_ids(conn, task_id),
            children=child_ids(conn, task_id),
            comments=list_comments(conn, task_id),
            events=list_events(conn, task_id),
            runs=list_runs(
                conn,
                task_id,
                state_type=run_state_type,
                state_name=run_state_name,
            ),
            latest_summary=latest_summary(conn, task_id),
        )
