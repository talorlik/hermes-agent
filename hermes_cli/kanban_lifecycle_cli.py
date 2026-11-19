"""Fork-only ``hermes kanban`` flags for guarded lifecycle transitions (G-KANBAN-LIFECYCLE).

``install`` is called once from the tail of ``hermes_cli/kanban.py``, the only
line this feature keeps in an upstream file. It adds these flags to upstream's
argparse records and wraps the matching handlers:

* ``claim --claimer NAME`` (a repeat by the same claimer is an idempotent replay)
* ``comment --expected-status S --if-absent``
* ``complete|block|request-review --expected-status S --expected-run-id N``
* ``block|unblock --author NAME`` (reason comment written in the transition's transaction)
* ``unblock --expected-block-kind K``
* ``--json`` on all six verbs: one versioned receipt of the committed rows
* ``show --json``: a versioned snapshot read under one transaction

A call that uses none of them runs upstream's handler untouched. The database
half is :mod:`hermes_cli.kanban_lifecycle_overlay`.

Two protections keep an upstream change from taking the CLI down through this
module. The ``from hermes_cli... import name`` statements below, and those in
the overlay, are explicit and at module top level on purpose: the scheduled
updater's health check resolves exactly those imports against the merged tree
and drops this fork file, with a carry-debt record, when upstream renames one.
Do not make them lazy or wrap them. Everything ``install`` touches beyond those
imports (upstream's parser records and handler table) is read inside its guard.
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Callable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_lifecycle_overlay as lifecycle
from hermes_cli import kanban_parser
from hermes_cli.kanban_output import _bulk_apply, _err, _print_json

logger = logging.getLogger(__name__)

Handler = Callable[[argparse.Namespace], int]

# Upstream's handlers, captured by ``install``; the guarded handlers delegate to them.
_UPSTREAM: dict[str, Handler] = {}
# The argparse records this module appended, so a later ``install`` tells them from upstream's.
_INSTALLED_ARGS: list[tuple] = []
# Per verb, the argparse dests that select the guarded handler: the flags this module added
# (one upstream already owns is skipped), plus upstream's own ``show --json``.
_ADDED: dict[str, frozenset[str]] = {}


def _positive_run_id(value: str) -> int:
    """Argparse validator for an explicit run-ownership guard."""
    try:
        run_id = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--expected-run-id must be a positive integer") from exc
    if run_id <= 0:
        raise argparse.ArgumentTypeError("--expected-run-id must be a positive integer")
    return run_id


def _extra_args() -> dict[str, list[tuple]]:
    arg, json_flag = kanban_parser._arg, kanban_parser._json_flag
    statuses = sorted(kb.VALID_STATUSES)

    def expected_status(verb: str):
        return arg("--expected-status", choices=statuses,
                   help=f"Only {verb} if the task still has this exact status.")

    def expected_run_id(verb: str):
        return arg("--expected-run-id", type=_positive_run_id,
                   help=f"Only {verb} if this exact positive owning run is still current.")

    def receipt():
        return json_flag(help="Print a versioned lifecycle receipt instead of prose")

    return {
        "claim": [
            arg("--claimer", help="Explicit claim identity; matching retries are idempotent"),
            receipt(),
        ],
        "comment": [
            expected_status("comment"),
            arg("--if-absent", action="store_true",
                help="Succeed without writing when this exact author and comment body already exist."),
            receipt(),
        ],
        "complete": [expected_status("complete"), expected_run_id("complete"), receipt()],
        "block": [
            expected_status("block"),
            arg("--author", help="Explicit author for the transactional BLOCKED reason comment."),
            expected_run_id("block"),
            receipt(),
        ],
        "unblock": [
            arg("--expected-block-kind", choices=sorted(lifecycle.VALID_UNBLOCK_EXPECTED_KINDS),
                help="Only unblock a blocked task with this exact typed block kind."),
            arg("--author", help="Explicit author for the transactional UNBLOCK reason comment."),
            receipt(),
        ],
        "request-review": [
            expected_status("request review"), expected_run_id("request review"), receipt(),
        ],
    }


def _dest(flags: tuple[str, ...]) -> str:
    return flags[0].lstrip("-").replace("-", "_")


def _plan(handlers: dict[str, Handler]) -> tuple[list, list[tuple], dict, dict]:
    """Work out, without changing anything: the extended parser records, the argparse
    records to append, the dests per verb that select a guarded handler, and upstream's
    handlers."""
    specs = list(kanban_parser._SPECS)
    index = {record[0]: position for position, record in enumerate(specs)}
    appended: list[tuple] = []
    added: dict[str, frozenset[str]] = {"show": frozenset({"json"})}
    for verb, extras in _extra_args().items():
        name, parser_kw, args, children = specs[index[verb]]
        ours = [arg for arg in args if any(arg is known for known in _INSTALLED_ARGS)]
        taken = {flag for flags, _ in args for flag in flags}
        fresh = [extra for extra in extras if not taken.intersection(extra[0])]
        specs[index[verb]] = (name, parser_kw, (*args, *fresh), children)
        appended.extend(fresh)
        added[verb] = frozenset(_dest(flags) for flags, _ in (*ours, *fresh))
    upstream = {
        verb: _UPSTREAM[verb] if handlers[verb] is guarded else handlers[verb]
        for verb, guarded in _GUARDED.items()
    }
    return specs, appended, added, upstream


def install(handlers: dict[str, Handler]) -> None:
    """Add the guarded flags to upstream's parser records and wrap ``handlers`` in place.

    This runs while ``hermes_cli.kanban`` is imported, which ``hermes`` does to
    build its parser, and an unattended sync can change the records' shape under
    it. A failure must cost the guarded flags (their callers then fail on an
    unknown argument), never the CLI: nothing is applied unless the whole plan
    could be built, and any error is logged once and swallowed here.

    A handler table that is not wrapped yet (a re-imported ``hermes_cli.kanban``
    builds a new one) is always wrapped, so a guard is never parsed and then
    ignored.
    """
    try:
        if all(handlers.get(verb) is guarded for verb, guarded in _GUARDED.items()):
            return
        specs, appended, added, upstream = _plan(handlers)
        # The one step that can still refuse comes first; a dict takes it whole or not at all.
        handlers.update(_GUARDED)
        kanban_parser._SPECS = specs
        _INSTALLED_ARGS.extend(appended)
        _ADDED.update(added)
        _UPSTREAM.update(upstream)
    except Exception:
        logger.exception(
            "kanban lifecycle flags not installed: --expected-status, --expected-run-id, "
            "--expected-block-kind, --claimer, --if-absent, --author and the --json receipts "
            "are unavailable; upstream's kanban CLI is unchanged"
        )


def _uses(args: argparse.Namespace, verb: str) -> bool:
    """True when the call sets a flag this module added to ``verb``."""
    return any(getattr(args, dest, None) not in (None, False) for dest in _ADDED.get(verb, ()))


def _emit(receipt: lifecycle.LifecycleReceipt) -> int:
    _print_json(receipt.to_json_dict())
    return 0


def _expected_run_id_for(args: argparse.Namespace, task_id: str) -> Optional[int]:
    """Resolve explicit ownership and require a present worker attestation to agree."""
    from hermes_cli import kanban as cli

    explicit = getattr(args, "expected_run_id", None)
    if explicit is None:
        return cli._worker_run_id_for(task_id)

    env_task = os.environ.get("HERMES_KANBAN_TASK")
    env_run_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if env_task is not None and env_task != task_id:
        raise ValueError(
            f"--expected-run-id task {task_id!r} does not match HERMES_KANBAN_TASK {env_task!r}"
        )
    if env_run_raw is not None:
        if env_task != task_id:
            raise ValueError("HERMES_KANBAN_RUN_ID is present without a matching HERMES_KANBAN_TASK")
        try:
            env_run_id = int(env_run_raw)
        except ValueError as exc:
            raise ValueError("HERMES_KANBAN_RUN_ID is not a positive integer") from exc
        if env_run_id <= 0:
            raise ValueError("HERMES_KANBAN_RUN_ID is not a positive integer")
        if explicit != env_run_id:
            raise ValueError(
                f"--expected-run-id {explicit} does not match HERMES_KANBAN_RUN_ID {env_run_id}"
            )
    return explicit


def _single_id_run_guard(args: argparse.Namespace, ids: list[str]) -> tuple[Optional[int], int]:
    """``--json`` / ``--expected-run-id`` name one task: ``(run_id, 0)`` or ``(None, 2)``."""
    if getattr(args, "json", False) and len(ids) != 1:
        return None, _err("kanban: --json cannot be used with multiple task ids", 2)
    if getattr(args, "expected_run_id", None) is not None and len(ids) != 1:
        return None, _err("kanban: --expected-run-id cannot be used with multiple task ids", 2)
    try:
        return (_expected_run_id_for(args, ids[0]) if len(ids) == 1 else None), 0
    except ValueError as exc:
        return None, _err(f"kanban: {exc}", 2)


def _cmd_show(args: argparse.Namespace) -> int:
    if not _uses(args, "show"):
        return _UPSTREAM["show"](args)
    from hermes_cli import kanban as cli

    run_filter, rc = cli._run_state_kwargs(args, "show")
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        snapshot = lifecycle.build_task_snapshot(conn, args.task_id, run_filter=run_filter)
    if snapshot is None:
        return _err(f"no such task: {args.task_id}")
    _print_json(snapshot.to_dict())
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    if not _uses(args, "claim"):
        return _UPSTREAM["claim"](args)
    with kbc.connect_closing() as conn:
        claimed = lifecycle.guarded_claim(
            conn, args.task_id, ttl_seconds=args.ttl, claimer=getattr(args, "claimer", None),
        )
        if claimed is None:
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                return _err(f"no such task: {args.task_id}")
            return _err(f"cannot claim {args.task_id}: status={existing.status} "
                        f"lock={existing.claim_lock or '(none)'}")
        task, receipt = claimed
        workspace = kbw.resolve_workspace(task)
        kbw.set_workspace_path(conn, task.id, str(workspace))
    if getattr(args, "json", False):
        return _emit(receipt)
    print(f"Claimed {task.id}\nWorkspace: {workspace}")
    return 0


def _cmd_comment(args: argparse.Namespace) -> int:
    if not _uses(args, "comment"):
        return _UPSTREAM["comment"](args)
    from hermes_cli import kanban as cli

    # Body trimming mirrors upstream's ``_cmd_comment``.
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            return _err("kanban: --max-len must be positive", 2)
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    if_absent = bool(getattr(args, "if_absent", False))
    with kbc.connect_closing() as conn:
        receipt = lifecycle.guarded_comment(
            conn, args.task_id, args.author or cli._profile_author(), body,
            expected_status=getattr(args, "expected_status", None), if_absent=if_absent,
        )
    if getattr(args, "json", False):
        return _emit(receipt)
    print(f"Comment {'ensured on' if if_absent else 'added to'} {args.task_id}")
    return 0


def _complete_refusal(conn, tid: str, expected_status: Optional[str],
                      explicit_run_id: Optional[int]) -> str:
    """The stderr line for a refused guarded ``complete``, read before the attempt.

    It names the guard that does not hold: the status, else the run id.
    """
    current = kb.get_task(conn, tid)
    actual = current.status if current else "unknown id"
    if expected_status is not None and actual != expected_status:
        return f"refusing to complete {tid}: expected status {expected_status!r}, task is {actual!r}"
    actual_run_id = current.current_run_id if current else None
    if explicit_run_id is not None and actual_run_id != explicit_run_id:
        return (f"refusing to complete {tid}: expected run id {explicit_run_id!r}, "
                f"task current run is {actual_run_id!r}")
    if expected_status is not None or explicit_run_id is not None:
        return f"refusing to complete {tid}: task is {actual!r} and cannot be completed from there"
    return f"cannot complete {tid} (unknown id or terminal state)"


def _cmd_complete(args: argparse.Namespace) -> int:
    if not _uses(args, "complete"):
        return _UPSTREAM["complete"](args)
    from hermes_cli import kanban as cli

    ids, rc = cli._require_ids(args)
    if rc:
        return rc
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    expected_status = getattr(args, "expected_status", None)
    explicit_run_id = getattr(args, "expected_run_id", None)
    expected_run_id, rc = _single_id_run_guard(args, ids)
    if rc:
        return rc
    # Handoff fields are per-run; refuse to copy them across N runs (upstream's rule).
    if len(ids) > 1 and (summary or raw_meta):
        return _err("kanban: --summary / --metadata are per-task and can't be used "
                    "with multiple ids (would apply the same handoff to every task). "
                    "Complete tasks one at a time, or drop the flags for the bulk close.", 2)
    metadata, rc = cli._parse_metadata_flag(raw_meta)
    if rc:
        return rc
    fail_msg: dict[str, str] = {}
    with kbc.connect_closing() as conn:
        def op(tid: str) -> Optional[lifecycle.LifecycleReceipt]:
            gate_err = cli._goal_gate_error(
                conn, tid, (summary or args.result or "").strip(), "completion",
                "Re-scope with kanban edit, or record the block with kanban block instead of completing.",
                "Provide evidence matching the task's acceptance criteria.")
            if gate_err:
                fail_msg[tid] = gate_err
                return None
            fail_msg[tid] = _complete_refusal(conn, tid, expected_status, explicit_run_id)
            try:
                receipt = lifecycle.guarded_complete(
                    conn, tid, result=args.result, summary=summary, metadata=metadata,
                    expected_run_id=expected_run_id if len(ids) == 1 else cli._worker_run_id_for(tid),
                    expected_status=expected_status, force=bool(getattr(args, "force", False)),
                )
            except kb.LiveClaimError:
                fail_msg[tid] = (f"cannot complete {tid}: a live worker is running it. Wait for the "
                                 f"worker, `hermes kanban reclaim {tid}` to release it, or re-run with "
                                 f"--force to close its run and complete anyway.")
                return None
            except kb.EmptyCompletionError as empty_err:
                fail_msg[tid] = (f"cannot complete {tid}: {empty_err}. Pass --result/--summary "
                                 f"describing what was done (an empty completion is not evidence).")
                return None
            if receipt is None:
                blockers = kb.unsatisfied_parents(conn, tid)
                if blockers:
                    detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                    fail_msg[tid] = (f"cannot complete {tid}: unsatisfied parent dependencies: {detail}; "
                                     f"complete the parents first, or `hermes kanban unlink <parent> {tid}`.")
            return receipt

        if not getattr(args, "json", False):
            return _bulk_apply(ids, op, lambda tid: f"Completed {tid}", fail_msg.__getitem__)
        receipt = op(ids[0])
    return _emit(receipt) if receipt is not None else _err(fail_msg[ids[0]])


def _block_landing_line(conn, tid: str, kind: Optional[str], suffix: str) -> str:
    """Where a block landed, in the wording of upstream's ``_cmd_block``."""
    landed = kb.get_task(conn, tid)
    where = landed.status if landed else "blocked"
    if where == "todo":
        return f"{tid} → todo (dependency wait){suffix}"
    if kind == "dependency" and where == "blocked":
        return f"Blocked {tid} as needs_input (no open parent to wait on){suffix}"
    if where == "triage":
        verdict = ("needs a human decision" if (landed.block_kind if landed else kind) == "needs_input"
                   else "orchestration attention needed")
        return f"{tid} → triage (unblock loop detected — {verdict}){suffix}"
    return f"Blocked {tid}{suffix}"


def _cmd_block(args: argparse.Namespace) -> int:
    if not _uses(args, "block"):
        return _UPSTREAM["block"](args)
    from hermes_cli import kanban as cli

    reason = cli._joined_words(args.reason)
    kind = getattr(args, "kind", None)
    expected_status = getattr(args, "expected_status", None)
    explicit_author = getattr(args, "author", None)
    if explicit_author is not None:
        author = explicit_author.strip()
        if not author or not reason:
            return _err("kanban block: --author requires a non-empty reason and author", 2)
    else:
        author = cli._profile_author()
    ids = cli._bulk_ids(args)
    as_json = bool(getattr(args, "json", False))
    expected_run_id, rc = _single_id_run_guard(args, ids)
    if rc:
        return rc
    # ``--author`` alone keeps upstream's terse refusal; a guard or receipt names the cause.
    explain = expected_status is not None or getattr(args, "expected_run_id", None) is not None or as_json
    suffix = f": {reason}" if reason else ""
    fail_msg: dict[str, str] = {}
    with kbc.connect_closing() as conn:
        def op(tid: str) -> Optional[lifecycle.LifecycleReceipt]:
            receipt, why = lifecycle.guarded_block(
                conn, tid, reason=reason, kind=kind,
                expected_run_id=expected_run_id if len(ids) == 1 else cli._worker_run_id_for(tid),
                expected_status=expected_status, reason_comment_author=author if reason else None,
            )
            fail_msg[tid] = (f"refusing to block {tid}: {why or 'transactional guard refused'}"
                             if explain else f"cannot block {tid}")
            return receipt

        if not as_json:
            return _bulk_apply(ids, op, lambda tid: _block_landing_line(conn, tid, kind, suffix),
                               fail_msg.__getitem__)
        receipt = op(ids[0])
    return _emit(receipt) if receipt is not None else _err(fail_msg[ids[0]])


def _cmd_unblock(args: argparse.Namespace) -> int:
    if not _uses(args, "unblock"):
        return _UPSTREAM["unblock"](args)
    from hermes_cli import kanban as cli

    # Upstream's rule for ``unblock``, which the guarded path must not bypass.
    if os.environ.get("HERMES_KANBAN_TASK"):
        return _err("kanban unblock is orchestrator-only; workers must hand off their assigned task")
    ids, rc = cli._require_ids(args)
    if rc:
        return rc
    as_json = bool(getattr(args, "json", False))
    if as_json and len(ids) != 1:
        return _err("kanban: --json cannot be used with multiple task ids", 2)
    reason = cli._stripped_or_none(getattr(args, "reason", None))
    explicit_author = getattr(args, "author", None)
    if explicit_author is not None:
        author = explicit_author.strip()
        if not author or reason is None:
            return _err("kanban unblock: --author requires a non-empty reason and author", 2)
    else:
        author = cli._profile_author() if reason else None
    expected_kind = getattr(args, "expected_block_kind", None)
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        def op(tid: str) -> Optional[lifecycle.LifecycleReceipt]:
            return lifecycle.guarded_unblock(
                conn, tid, expected_block_kind=expected_kind, reason=reason,
                reason_comment_author=author,
            )

        def fail_msg(tid: str) -> str:
            if expected_kind is None:
                return f"cannot unblock {tid} (not blocked/scheduled?)"
            current = kb.get_task(conn, tid)
            actual = (f"status={current.status!r} kind={current.block_kind!r}"
                      if current else "unknown id")
            return (f"refusing to unblock {tid}: expected a blocked task with "
                    f"kind {expected_kind!r}, task is {actual}")

        if not as_json:
            return _bulk_apply(ids, op, lambda tid: f"Unblocked {tid}{suffix}", fail_msg)
        receipt = op(ids[0])
        if receipt is None:
            return _err(fail_msg(ids[0]))
    return _emit(receipt)


def _cmd_request_review(args: argparse.Namespace) -> int:
    if not _uses(args, "request-review"):
        return _UPSTREAM["request-review"](args)
    from hermes_cli import kanban as cli

    tid = args.task_id
    try:
        expected_run_id = _expected_run_id_for(args, tid)
    except ValueError as exc:
        return _err(f"kanban: {exc}", 2)
    summary = cli._stripped_or_none(getattr(args, "summary", None))
    metadata, rc = cli._parse_metadata_flag(getattr(args, "metadata", None))
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        gate_err = cli._goal_gate_error(
            conn, tid, summary or "", "review handoff",
            "Record the block with kanban block instead of requesting review.",
            "Provide acceptance evidence matching the task.")
        if gate_err:
            return _err(gate_err)
        receipt, why = lifecycle.guarded_request_review(
            conn, tid, summary=summary, metadata=metadata, reviewer=getattr(args, "reviewer", None),
            expected_run_id=expected_run_id, expected_status=getattr(args, "expected_status", None),
            force=bool(getattr(args, "force", False)),
        )
        if receipt is None:
            return _err(f"cannot request review for {tid}: {why or 'not running/ready?'}")
        if getattr(args, "json", False):
            return _emit(receipt)
        persisted_run = kb.latest_run(conn, tid)
        display_summary = persisted_run.summary if persisted_run else None
        print(f"Requested review for {tid}" + (f": {display_summary}" if display_summary else ""))
    return 0


_GUARDED: dict[str, Handler] = {
    "show": _cmd_show,
    "claim": _cmd_claim,
    "comment": _cmd_comment,
    "complete": _cmd_complete,
    "block": _cmd_block,
    "unblock": _cmd_unblock,
    "request-review": _cmd_request_review,
}
