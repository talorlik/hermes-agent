"""Kanban dashboard plugin — backend API routes, mounted at /api/plugins/kanban/.

Every handler is a thin wrapper around ``hermes_cli.kanban_db`` (the same code paths the CLI
and gateway ``/kanban`` command use, so the surfaces cannot drift). The ``/events`` WebSocket
tails the append-only ``task_events`` table on a short poll (WAL reads run alongside the
dispatcher's write txns); it carries its credential in the query string (browsers can't set
``Authorization`` on an upgrade) and is gated by the dashboard's canonical WS auth check.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from fastapi import (
    APIRouter, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status as http_status)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hermes_cli import kanban_db
from hermes_cli.web_read_coalescing import coalesced_read
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_diagnostics as kd
from hermes_cli.kanban_db import KANBAN_ATTACHMENT_MAX_BYTES, _collision_free_path, _safe_attachment_name

log = logging.getLogger(__name__)

router = APIRouter()

_BOARD_Q = Query(None, description="Kanban board slug (omit for current)")


# --- Connection / board helpers ---------------------------------------------

def _ws_upgrade_authorized(ws: "WebSocket") -> bool:
    """Authorize a WS upgrade via the dashboard's canonical gate (``web_server_chat._ws_auth_ok``:
    ``?token=`` / ``?ticket=`` / ``?internal=``) so this endpoint can never drift from core
    auth; accepts when the dashboard isn't importable (bare-FastAPI test harness)."""
    try:
        from hermes_cli import web_server_chat as _ws
    except Exception:
        return True
    return bool(_ws._ws_auth_ok(ws))


def _normalize_slug_or_400(slug: str) -> Optional[str]:
    with _value_error_400():
        return kanban_db._normalize_board_slug(slug)


def _resolve_board(board: Optional[str]) -> Optional[str]:
    """Validate/normalise a board slug query param (400 malformed, 404 unknown);
    ``None`` when omitted so ``kb.connect()`` falls through to the active board."""
    if board is None or board == "":
        return None
    normed = _normalize_slug_or_400(board)
    if normed and normed != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {normed!r} does not exist")
    return normed


def _existing_board_slug(slug: str) -> str:
    """Normalise a path slug and require the board to exist (400 / 404)."""
    normed = _normalize_slug_or_400(slug)
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    return normed


def _conn(board: Optional[str] = None):
    """Connect to the already-normalised ``board`` (``None`` = active). ``init_db`` is
    idempotent; running it here lets a fresh install self-heal if POST /tasks arrives first."""
    try:
        kanban_db.init_db(board=board)
    except Exception as exc:
        log.warning("kanban init_db failed: %s", exc)
    return kbc.connect(board=board)


@contextmanager
def _board_conn(board: Optional[str]) -> Iterator[tuple[Optional[str], sqlite3.Connection]]:
    """Resolve the ``board`` query param, open a connection, close it on exit."""
    board = _resolve_board(board)
    with closing(_conn(board=board)) as conn:
        yield board, conn


def _with_board_pinned(board: Optional[str], fn: Callable[[], Any]) -> Any:
    """Run ``fn`` with the board pinned context-locally, not via the process-global
    ``HERMES_KANBAN_BOARD`` env var (concurrent requests for different boards would cross-write)."""
    with kanban_db.scoped_current_board(_resolve_board(board) or kanban_db.DEFAULT_BOARD):
        return fn()


def _require(getter: Callable, conn: sqlite3.Connection, ident, label: str):
    obj = getter(conn, ident)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{label} {ident} not found")
    return obj


def _run_aux(board: Optional[str], module: str, fn: str, task_id: str, author: Optional[str]) -> Any:
    """Run a slow auxiliary-LLM task helper (``hermes_cli.<module>.<fn>``) with the board pinned;
    the module is imported lazily so a missing aux client can't break plugin load."""
    def _run():
        return getattr(importlib.import_module(f"hermes_cli.{module}"), fn)(task_id, author=(author or None))
    return _with_board_pinned(board, _run)


def _require_task(conn: sqlite3.Connection, task_id: str) -> kanban_db.Task:
    return _require(kanban_db.get_task, conn, task_id, "task")


def _require_run(conn: sqlite3.Connection, run_id: int) -> kanban_db.Run:
    return _require(kanban_db.get_run, conn, run_id, "run")


def _require_ok(ok: bool) -> None:
    """404 when a kanban_db mutator reports the task vanished mid-request."""
    if not ok:
        raise HTTPException(status_code=404, detail="task not found")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


@contextmanager
def _map_errors(status: int, *types: type[BaseException]) -> Iterator[None]:
    """Map the given exception types to ``HTTPException(status, str(exc))``."""
    try:
        yield
    except types as e:
        raise HTTPException(status_code=status, detail=str(e))


_value_error_400 = partial(_map_errors, 400, ValueError)  # domain-layer validation refusals


@contextmanager
def _errors_to_500(prefix: str) -> Iterator[None]:
    """Map any unexpected exception to ``500 "<prefix>: <exc>"``; HTTPExceptions pass through."""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{prefix}: {exc}")


# --- Serialization helpers --------------------------------------------------

# Dashboard columns, left-to-right ("archived" is a filter toggle, not a column). Keep in
# sync with kanban_db.VALID_STATUSES — a status missing here gets mis-bucketed into ``todo``.
BOARD_COLUMNS: list[str] = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"]

_CARD_SUMMARY_PREVIEW_CHARS = 200


def _task_dict(task: kanban_db.Task, *, latest_summary: Optional[str] = None) -> dict[str, Any]:
    d = asdict(task)
    # Derived age metrics so the UI can colour stale cards without client deltas.
    try:
        d["age"] = kanban_db.task_age(task)
    except Exception:
        d["age"] = {"created_age_seconds": None, "started_age_seconds": None, "time_to_complete_seconds": None}
    # Latest non-null run summary (workers hand off via ``task_runs.summary``, not ``tasks.result``).
    d["latest_summary"] = latest_summary
    return d


def _attachment_dict(a: kanban_db.Attachment) -> dict[str, Any]:
    """``stored_path`` is the absolute on-disk path workers read; UI downloads by ``id``."""
    return {
        "id": a.id, "task_id": a.task_id, "filename": a.filename, "content_type": a.content_type,
        "size": a.size, "uploaded_by": a.uploaded_by, "stored_path": a.stored_path, "created_at": a.created_at}


def _placeholders(ids: list) -> str:
    return ",".join(["?"] * len(ids))


def _compute_task_diagnostics(conn: sqlite3.Connection, task_ids: Optional[list[str]] = None) -> dict[str, list[dict]]:
    """``{task_id: [diagnostic_dict, ...]}`` (tasks with none omitted) via three aggregate
    queries (tasks, events, runs) — slurps the board; paginate if profiling shows a hotspot."""
    from hermes_cli.config import load_config

    if task_ids is not None and not task_ids:
        return {}
    diag_config = kd.config_from_runtime_config(load_config())
    if task_ids is not None:
        rows = conn.execute(f"SELECT * FROM tasks WHERE id IN ({_placeholders(task_ids)})", tuple(task_ids)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall()
    if not rows:
        return {}
    row_ids = [r["id"] for r in rows]

    def _rows_by_task(table: str) -> dict[str, list]:
        by_task: dict[str, list] = {tid: [] for tid in row_ids}
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE task_id IN ({_placeholders(row_ids)}) ORDER BY id", tuple(row_ids)):
            by_task.setdefault(row["task_id"], []).append(row)
        return by_task

    events_by_task = _rows_by_task("task_events")
    runs_by_task = _rows_by_task("task_runs")
    graph_by_task = kanban_db.task_graph_contexts(conn, row_ids)
    out: dict[str, list[dict]] = {}
    for r in rows:
        tid = r["id"]
        diags = kd.compute_task_diagnostics(
            r, events_by_task[tid], runs_by_task[tid], config=diag_config, graph=graph_by_task.get(tid))
        if diags:
            out[tid] = [d.to_dict() for d in diags]
    return out


def _warnings_summary_from_diagnostics(diagnostics: list[dict]) -> Optional[dict]:
    """Compact card badge summary ``{count, kinds, latest_at, highest_severity}``; None when empty."""
    if not diagnostics:
        return None
    kinds: dict[str, int] = {}
    count = latest = 0
    highest_idx, highest_sev = -1, None
    for d in diagnostics:
        n = d.get("count", 1)
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + n
        count += n
        latest = max(latest, d.get("last_seen_at") or 0)
        sev = d.get("severity")
        if sev in kd.SEVERITY_ORDER and kd.SEVERITY_ORDER.index(sev) > highest_idx:
            highest_idx, highest_sev = kd.SEVERITY_ORDER.index(sev), sev
    return {"count": count, "kinds": kinds, "latest_at": latest, "highest_severity": highest_sev}


def _attach_diagnostics(task_d: dict, diags: Optional[list[dict]]) -> None:
    """Full list in the payload (drawer renders without a second round-trip); card badge gets the summary."""
    if diags:
        task_d["diagnostics"] = diags
        task_d["warnings"] = _warnings_summary_from_diagnostics(diags)


def _links_for(conn: sqlite3.Connection, task_id: str) -> dict[str, list[str]]:
    """Return {'parents': [...], 'children': [...]} for a task."""
    def _ids(col: str, other: str) -> list[str]:
        return [r[col] for r in conn.execute(f"SELECT {col} FROM task_links WHERE {other} = ? ORDER BY {col}", (task_id,))]
    return {"parents": _ids("parent_id", "child_id"), "children": _ids("child_id", "parent_id")}


# --- GET /board -------------------------------------------------------------

def get_board(
    tenant: Optional[str] = Query(None, description="Filter to a single tenant"),
    include_archived: bool = Query(False),
    board: Optional[str] = _BOARD_Q,
    workflow_template_id: Optional[str] = Query(None, description="Restrict to tasks using this workflow template id"),
    current_step_key: Optional[str] = Query(None, description="Restrict to tasks at this workflow step key")):
    """Full board grouped by status column; omitting ``board`` uses the active board
    (``HERMES_KANBAN_BOARD`` env → on-disk ``current`` pointer → ``default``)."""
    with _board_conn(board) as (board, conn):
        tasks = kanban_db.list_tasks(
            conn, tenant=tenant, include_archived=include_archived,
            workflow_template_id=workflow_template_id, current_step_key=current_step_key)
        # Link / comment / progress rollups are each one aggregate query rather than N per-task lookups.
        link_counts: dict[str, dict[str, int]] = {}
        for row in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall():
            link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
            link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
        comment_counts: dict[str, int] = {
            r["task_id"]: r["n"] for r in conn.execute("SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id")}
        progress: dict[str, dict[str, int]] = {}  # per parent: children done / total, rendered as "N/M"
        for row in conn.execute(
            "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l JOIN tasks t ON t.id = l.child_id").fetchall():
            p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
            p["total"] += 1
            p["done"] += row["cstatus"] == "done"
        diagnostics_per_task = _compute_task_diagnostics(conn, task_ids=None)
        latest_event_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]
        columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
        if include_archived:
            columns["archived"] = []
        # One window-function query for latest summaries (avoids N+1); cards get a
        # truncated preview, the full text comes from /tasks/:id.
        summary_map = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        for t in tasks:
            full = summary_map.get(t.id)
            d = _task_dict(t, latest_summary=(full[:_CARD_SUMMARY_PREVIEW_CHARS] if full else None))
            d["link_counts"] = link_counts.get(t.id, {"parents": 0, "children": 0})
            d["comment_count"] = comment_counts.get(t.id, 0)
            d["progress"] = progress.get(t.id)  # None when the task has no children
            _attach_diagnostics(d, diagnostics_per_task.get(t.id))
            columns[t.status if t.status in columns else "todo"].append(d)

        # Per-column ordering (priority DESC, created_at ASC) comes from list_tasks.
        tenants = [r["tenant"] for r in conn.execute("SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant")]
        assignees = [r["assignee"] for r in conn.execute(
            "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL AND status != 'archived' ORDER BY assignee")]
        return {
            "columns": [{"name": name, "tasks": columns[name]} for name in columns], "tenants": tenants,
            "assignees": assignees, "latest_event_id": int(latest_event_id), "now": int(time.time())}


_read_board = coalesced_read(get_board)


@router.get("/board")
async def get_board_endpoint(
    tenant: Optional[str] = Query(None, description="Filter to a single tenant"),
    include_archived: bool = Query(False),
    board: Optional[str] = _BOARD_Q,
    workflow_template_id: Optional[str] = Query(None, description="Restrict to tasks using this workflow template id"),
    current_step_key: Optional[str] = Query(None, description="Restrict to tasks at this workflow step key"),
):
    # Resolve selection before keying so a board switch cannot join an older read.
    return await _read_board(
        tenant=tenant,
        include_archived=include_archived,
        board=board or kanban_db.get_current_board(),
        workflow_template_id=workflow_template_id,
        current_step_key=current_step_key,
    )


# --- GET /tasks/:id ---------------------------------------------------------

@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    board: Optional[str] = Query(None),
    run_state_type: Optional[str] = Query(None, description="With run_state_name: filter runs by column 'status' or 'outcome'"),
    run_state_name: Optional[str] = Query(None, description="With run_state_type: exact value for that run column")):
    with _board_conn(board) as (board, conn):
        if (run_state_type is None) ^ (run_state_name is None):
            raise HTTPException(status_code=400, detail="run_state_type and run_state_name must be passed together or omitted")
        if run_state_type not in (None, "status", "outcome"):
            raise HTTPException(status_code=400, detail="run_state_type must be 'status' or 'outcome'")
        with kbc.read_txn(conn):
            snapshot = kanban_db.build_task_snapshot(
                conn,
                task_id,
                run_state_type=run_state_type,
                run_state_name=run_state_name,
            )
            if snapshot is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"task {task_id} not found",
                )
            serialized = snapshot.to_dict()
            task = snapshot.task
            # Preserve dashboard-only derived fields while sourcing canonical
            # lifecycle fields from the shared versioned serializer.
            task_d = _task_dict(task, latest_summary=snapshot.latest_summary)
            task_d.update(serialized["task"])
            links = _links_for(conn, task_id)
            child_summaries = kanban_db.latest_summaries(conn, snapshot.children)
            children = filter(
                None,
                (kanban_db.get_task(conn, cid) for cid in snapshot.children),
            )
            _attach_diagnostics(
                task_d,
                _compute_task_diagnostics(conn, task_ids=[task_id]).get(task_id)
                or [],
            )
            return {
                "schema_version": snapshot.schema_version,
                "task": task_d,
                "comments": [asdict(comment) for comment in snapshot.comments],
                "events": [asdict(event) for event in snapshot.events],
                "attachments": [
                    _attachment_dict(attachment)
                    for attachment in kanban_db.list_attachments(conn, task_id)
                ],
                "links": links,
                "child_results": [
                    {
                        "id": child.id,
                        "title": child.title,
                        "status": child.status,
                        "latest_summary": child_summaries.get(child.id),
                        "result": child.result,
                    }
                    for child in children
                ],
                "runs": [asdict(run) for run in snapshot.runs],
            }


# --- POST /tasks ------------------------------------------------------------

class CreateTaskBody(BaseModel):
    title: str
    body: Optional[str] = None
    assignee: Optional[str] = None
    tenant: Optional[str] = None
    priority: int = 0
    workspace_kind: Optional[str] = None  # None = scratch, or the board project's worktree when scoped
    workspace_path: Optional[str] = None
    parents: list[str] = Field(default_factory=list)
    triage: bool = False
    idempotency_key: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    skills: Optional[list[str]] = None
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    reasoning_effort: Optional[str] = None  # none|minimal|…|ultra; None inherits the profile's level
    project_id: Optional[str] = None  # None inherits the board's scoped project (if any)


@router.post("/tasks")
def create_task(payload: CreateTaskBody, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn), _value_error_400():
        # CreateTaskBody field names match create_task's keyword parameters.
        task_id = kanban_db.create_task(conn, created_by="dashboard", board=board, **payload.model_dump())
        task = kanban_db.get_task(conn, task_id)
        body: dict[str, Any] = {"task": _task_dict(task) if task else None}
        # Dispatcher-presence warning so the UI can banner a ready+assigned task that would
        # otherwise sit idle (no gateway / dispatch_in_gateway=false); triage/todo are expected
        # to wait, unassigned tasks can't dispatch anyway. Probe the request's active home: the
        # dashboard backend may run under a different HERMES_HOME than the board's profile.
        if task and task.status == "ready" and task.assignee:
            try:
                from hermes_cli.kanban import _check_dispatcher_presence
                from hermes_constants import get_hermes_home
                running, message = _check_dispatcher_presence(hermes_home=get_hermes_home())
                if not running and message:
                    body["warning"] = message
            except Exception:
                pass  # probe failure must never block the create itself
        return body


# --- Attachments — upload / list / download / delete ------------------------
# Size cap, filename sanitiser, and collision resolver live in ``kanban_db`` so the
# dashboard, agent toolset, and CLI share one implementation.

@router.get("/tasks/{task_id}/attachments")
def list_task_attachments(task_id: str, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
        return {"attachments": [_attachment_dict(a) for a in kanban_db.list_attachments(conn, task_id)]}


@router.post("/tasks/{task_id}/attachments")
async def upload_task_attachment(
    task_id: str,
    file: UploadFile = File(...),
    board: Optional[str] = Query(None),
    uploaded_by: Optional[str] = Form(None)):
    """Store an upload under ``attachments_root(board)/<task_id>/`` (sanitised,
    collision-resolved name; ``_safe_attachment_name`` ValueError → 400) and record it."""
    with _board_conn(board) as (board, conn), _value_error_400():
        _require_task(conn, task_id)
        safe_name = _safe_attachment_name(file.filename or "")
        dest_dir = kanban_db.task_attachments_dir(task_id, board=board)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = _collision_free_path(dest_dir, safe_name)  # foo.pdf → foo (1).pdf …
        total = 0  # stream in chunks with a hard size cap so one upload can't fill the disk
        try:
            with open(dest_path, "wb") as out:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > KANBAN_ATTACHMENT_MAX_BYTES:
                        out.close()
                        dest_path.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=413, detail=f"attachment exceeds {KANBAN_ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB limit")
                    out.write(chunk)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"failed to store attachment: {exc}")
        att_id = kanban_db.add_attachment(
            conn, task_id, filename=dest_path.name, stored_path=str(dest_path.resolve()),
            content_type=file.content_type, size=total, uploaded_by=(uploaded_by or "dashboard"))
        att = kanban_db.get_attachment(conn, att_id)
        return {"attachment": _attachment_dict(att) if att else None}


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: int, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        att = kanban_db.get_attachment(conn, attachment_id)
        if att is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        # Defense in depth against a tampered DB row: the blob must still live under the board's attachments root.
        root = kanban_db.attachments_root(board=board).resolve()
        try:
            stored = Path(att.stored_path).resolve()
            stored.relative_to(root)
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="attachment file unavailable")
        if not stored.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing on disk")
        return FileResponse(path=str(stored), filename=att.filename, media_type=att.content_type or "application/octet-stream")


@router.delete("/attachments/{attachment_id}")
def remove_attachment(attachment_id: int, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        if kanban_db.delete_attachment(conn, attachment_id) is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        return {"ok": True, "id": attachment_id}


# --- PATCH /tasks/:id  and  POST /tasks/bulk ---------------------------------

class UpdateTaskBody(BaseModel):
    status: Optional[str] = None
    assignee: Optional[str] = None
    priority: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None
    result: Optional[str] = None
    block_reason: Optional[str] = None
    # Handoff fields forwarded to complete_task on -> 'done' (parity with ``hermes kanban complete``).
    summary: Optional[str] = None
    metadata: Optional[dict] = None
    # In a PATCH ``None`` means "field not sent", so ``clear_*=True`` is the explicit clear signal.
    # ``reasoning_effort="none"`` is a VALUE (thinking off); it is cleared separately so
    # dropping a model override doesn't silently reset the depth.
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    clear_model_override: bool = False
    reasoning_effort: Optional[str] = None
    clear_reasoning_effort: bool = False


class BulkTaskBody(BaseModel):
    ids: list[str]
    status: Optional[str] = None
    assignee: Optional[str] = None  # "" or None = unassign
    priority: Optional[int] = None
    archive: bool = False
    result: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict] = None
    reclaim_first: bool = False
    # Same semantics as UpdateTaskBody.
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    clear_model_override: bool = False
    reasoning_effort: Optional[str] = None
    clear_reasoning_effort: bool = False


class _StatusRejected(Exception):
    """A status the dashboard may not set via this path; the message is user-facing."""


_RUNNING_DIRECT_MSG = "Cannot set status to 'running' directly; use the dispatcher/claim path"


def _drag_to(conn, task_id: str, s: str) -> bool:
    """Drag-drop into ready/todo/triage: blocked/scheduled -> ready re-opens via ``unblock_task``;
    leaving ``review`` goes through ``reopen_review_task`` (stale-run recovery, parent re-gate,
    ``review_reopened`` event) instead of a raw write; ``triage`` needs no current-state query."""
    current = kanban_db.get_task(conn, task_id) if s != "triage" else None
    if s == "ready" and current and current.status in ("blocked", "scheduled"):
        return kanban_db.unblock_task(conn, task_id)
    if current is not None and current.status == "review":
        return kanban_db.reopen_review_task(conn, task_id)
    return _set_status_direct(conn, task_id, s)


# Status verb dispatch shared by PATCH /tasks/{id} and POST /tasks/bulk: (conn, task_id,
# payload) -> ok. ``review`` uses request_review (never a block, so it can't trip unblock-loop
# detection) and ``done`` pass ``force=True``: a dashboard action is a human override of a live worker claim.
_STATUS_HANDLERS: dict[str, Any] = {
    "done": lambda conn, tid, p: kanban_db.complete_task(
        conn, tid, result=p.result, summary=p.summary, metadata=p.metadata, force=True),
    "blocked": lambda conn, tid, p: kanban_db.block_task(conn, tid, reason=getattr(p, "block_reason", None)),
    "scheduled": lambda conn, tid, p: kanban_db.schedule_task(conn, tid, reason=getattr(p, "block_reason", None)),
    "review": lambda conn, tid, p: kanban_db.request_review(
        conn, tid, summary=p.summary, metadata=p.metadata, reviewer=(p.assignee or None), force=True),
    "ready": lambda conn, tid, p: _drag_to(conn, tid, "ready"),
    "todo": lambda conn, tid, p: _drag_to(conn, tid, "todo"),
    "triage": lambda conn, tid, p: _drag_to(conn, tid, "triage")}


def _apply_status(conn, task_id: str, s: str, p, unknown_detail: str) -> bool:
    """Dispatch a status verb; raises ``_StatusRejected`` (user-facing message)
    for ``running`` or an unknown status (``unknown_detail``)."""
    if s == "running":
        raise _StatusRejected(_RUNNING_DIRECT_MSG)
    handler = _STATUS_HANDLERS.get(s)
    if handler is None:
        raise _StatusRejected(unknown_detail)
    return handler(conn, task_id, p)


def _set_priority(conn, task_id: str, priority: int, board: Optional[str]) -> None:
    with kanban_db.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (int(priority), task_id))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'reprioritized', ?, ?)",
            (task_id, json.dumps({"priority": int(priority)}), int(time.time())))
    # Mutation-boundary observer (post-commit): this direct-SQL write bypasses every kanban_db mutator.
    kanban_db.notify_task_updated(conn, task_id, ("priority",), board=board)


def _apply_model_override(conn, task_id: str, p) -> bool:
    """Raises ValueError/RuntimeError from kanban_db for the caller to map."""
    new_model = None if p.clear_model_override else (p.model_override or "").strip() or None
    return kanban_db.set_model_override(conn, task_id, new_model, provider=p.provider_override)


def _apply_reasoning_effort(conn, task_id: str, p) -> bool:
    return kanban_db.set_reasoning_effort(conn, task_id, None if p.clear_reasoning_effort else p.reasoning_effort)


# Override knobs shared by PATCH and bulk: (payload wants it?, apply, bulk refusal message).
_OVERRIDE_OPS = (
    (lambda p: p.clear_model_override or p.model_override is not None, _apply_model_override, "model override refused"),
    (lambda p: p.clear_reasoning_effort or p.reasoning_effort is not None, _apply_reasoning_effort, "reasoning override refused"),
)


def _patch_status(conn, task_id: str, payload: UpdateTaskBody, review_assignee_deferred: bool) -> None:
    """PATCH status phase: 400 on a rejected verb, 409 when the transition is refused
    (naming the blocking parent(s) for ``ready`` so the UI renders an actionable toast)."""
    s = payload.status
    if s == "archived":
        ok = kanban_db.archive_task(conn, task_id)
    else:
        with _map_errors(400, _StatusRejected):
            ok = _apply_status(conn, task_id, s, payload, f"unknown status: {s}")
        if s == "review" and ok and review_assignee_deferred and not payload.assignee:
            ok = kanban_db.assign_task(conn, task_id, None)
    if ok:
        return
    blockers = _parents_blocking_ready(conn, task_id) if s == "ready" else []
    if blockers:
        names = ", ".join(f"{p['title']!r} ({p['id']}, status={p['status']})" for p in blockers)
        raise _conflict(f"Cannot move to 'ready': blocked by parent(s) not done — {names}")
    raise _conflict(f"status transition to {s!r} not valid from current state")


def _patch_title_body(conn, task_id: str, payload: UpdateTaskBody, board: Optional[str]) -> None:
    """PATCH title/body phase: one UPDATE + ``edited`` event, then the post-commit observer
    (field names only — values never leave the DB via this payload)."""
    with kanban_db.write_txn(conn):
        sets, vals = [], []
        if payload.title is not None:
            if not payload.title.strip():
                raise HTTPException(status_code=400, detail="title cannot be empty")
            sets.append("title = ?")
            vals.append(payload.title.strip())
        if payload.body is not None:
            sets.append("body = ?")
            vals.append(payload.body)
        vals.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'edited', NULL, ?)",
            (task_id, int(time.time())))
    kanban_db.notify_task_updated(
        conn, task_id, [f for f in ("title", "body") if getattr(payload, f) is not None], board=board)


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: UpdateTaskBody, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
        # For a combined assignee+review patch, request_review must capture the
        # current implementer before the task is routed to the reviewer.
        review_assignee_deferred = payload.status == "review" and payload.assignee is not None
        if payload.assignee is not None and not review_assignee_deferred:
            with _map_errors(409, RuntimeError):
                _require_ok(kanban_db.assign_task(conn, task_id, payload.assignee or None))
        if payload.status is not None:
            _patch_status(conn, task_id, payload, review_assignee_deferred)
        for wanted, apply, _refused in _OVERRIDE_OPS:
            if wanted(payload):
                with _map_errors(400, ValueError, RuntimeError):
                    ok = apply(conn, task_id, payload)
                _require_ok(ok)
        if payload.priority is not None:
            _set_priority(conn, task_id, payload.priority, board)
        if payload.title is not None or payload.body is not None:
            _patch_title_body(conn, task_id, payload, board)
        updated = kanban_db.get_task(conn, task_id)
        return {"task": _task_dict(updated) if updated else None}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        if not kanban_db.delete_task(conn, task_id):
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return {"deleted": True, "task_id": task_id}


def _parents_blocking_ready(conn: sqlite3.Connection, task_id: str) -> list:
    """Parent rows (id, title, status) not ``done`` that block promotion to ``ready``.

    Used to enrich the 409 response from :func:`update_task` so the dashboard can show an actionable toast
    (#26744) instead of a silent no-op. Returns ``[]`` when nothing blocks the transition (e.g. no parents,
    or all parents already done).
    """
    rows = conn.execute(
        "SELECT t.id, t.title, t.status FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ? AND t.status != 'done'",
        (task_id,)).fetchall()
    return [{"id": r["id"], "title": r["title"], "status": r["status"]} for r in rows]


def _set_status_direct(conn: sqlite3.Connection, task_id: str, new_status: str) -> bool:
    """Direct status write for drag-drop moves without a structured verb (todo<->ready,
    running<->ready) + a ``status`` event. Leaving ``running`` closes the run as 'reclaimed'
    so attempt history isn't orphaned; the worker is killed only AFTER the txn commits."""
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    effective_status = new_status
    with kanban_db.write_txn(conn):
        prev = conn.execute(
            "SELECT status, current_run_id, worker_pid, claim_lock, worker_started_at FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
        if prev is None:
            return False
        if prev["status"] == "running" and new_status == "ready":
            resume_status = kanban_db._retry_status_for_run(conn, task_id, prev["current_run_id"])
            if resume_status == "review":
                effective_status = "review" if kanban_db._parents_satisfied(conn, task_id) else "todo"
        # Never promote to 'ready' unless all parents are done/archived — otherwise the
        # dispatcher spawns a child whose upstream work hasn't completed.
        if effective_status == "ready" and not kanban_db._parents_satisfied(conn, task_id):
            return False
        was_running = prev["status"] == "running"
        reopening_satisfied_parent = prev["status"] in {"done", "archived"} and effective_status not in {"done", "archived"}
        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (effective_status,) * 4 + (task_id,))
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and effective_status != "running" and prev["current_run_id"]:
            run_id = kanban_db._end_run(
                conn, task_id, outcome="reclaimed", status="reclaimed",
                summary=f"status changed to {effective_status} (dashboard/direct)")
            terminations.append((prev["worker_pid"], prev["claim_lock"], prev["worker_started_at"]))
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, json.dumps({"status": effective_status, "requested_status": new_status}), int(time.time())))
        if reopening_satisfied_parent:
            # Domain-layer invalidation composes via a savepoint inside our txn and hands
            # back worker terminations to perform post-commit.
            result = kanban_db.invalidate_descendants_for_parent_reopen(conn, task_id, author="dashboard")
            terminations.extend(result["terminations"])
    for pid, claim_lock, started_at in terminations:
        kanban_db._terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    # Re-opening something may have made children stale.
    if effective_status in {"done", "ready", "review"}:
        kanban_db.recompute_ready(conn)
    return True


# --- Comments / links -------------------------------------------------------

class CommentBody(BaseModel):
    body: str
    author: Optional[str] = "dashboard"


@router.post("/tasks/{task_id}/comments")
def add_comment(task_id: str, payload: CommentBody, board: Optional[str] = Query(None)):
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
        kanban_db.add_comment(conn, task_id, author=payload.author or "dashboard", body=payload.body)
        return {"ok": True}


class LinkBody(BaseModel):
    parent_id: str
    child_id: str


@router.post("/links")
def add_link(payload: LinkBody, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn), _value_error_400():
        gated = kanban_db.link_tasks(conn, payload.parent_id, payload.child_id)
        return {"ok": True, "gated": gated}


@router.delete("/links")
def delete_link(parent_id: str = Query(...), child_id: str = Query(...), board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        return {"ok": bool(kanban_db.unlink_tasks(conn, parent_id, child_id))}


def _bulk_apply_one(conn, tid: str, payload: BulkTaskBody, board: Optional[str], entry: dict) -> None:
    """Apply the bulk patch to one task, recording refusals in ``entry`` without aborting the
    remaining ops — except a rejected status verb (``_StatusRejected`` propagates)."""
    if payload.archive and not kanban_db.archive_task(conn, tid):
        entry.update(ok=False, error="archive refused")
    if payload.status is not None and not payload.archive:
        s = payload.status
        if not _apply_status(conn, tid, s, payload, f"unknown status {s!r}"):
            entry.update(ok=False, error=f"transition to {s!r} refused")
    if payload.assignee is not None:
        try:
            ok = (kanban_db.reassign_task(conn, tid, payload.assignee or None, reclaim_first=True) if payload.reclaim_first
                  else kanban_db.assign_task(conn, tid, payload.assignee or None))
            if not ok:
                entry.update(ok=False, error="assign refused")
        except RuntimeError as e:
            entry.update(ok=False, error=str(e))
    if payload.priority is not None:
        _set_priority(conn, tid, payload.priority, board)
    for wanted, apply, refused in _OVERRIDE_OPS:
        if wanted(payload):
            try:
                if not apply(conn, tid, payload):
                    entry.update(ok=False, error=refused)
            except (ValueError, RuntimeError) as e:
                entry.update(ok=False, error=str(e))


@router.post("/tasks/bulk")
def bulk_update(payload: BulkTaskBody, board: Optional[str] = Query(None)):
    """Apply the same patch to every id. Independent iteration — per-task
    failures don't abort siblings; returns per-id outcome for partials."""
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    results: list[dict] = []
    with _board_conn(board) as (board, conn):
        for tid in ids:
            entry: dict[str, Any] = {"id": tid, "ok": True}
            try:
                if kanban_db.get_task(conn, tid) is None:
                    entry.update(ok=False, error="not found")
                else:
                    _bulk_apply_one(conn, tid, payload, board, entry)
            except Exception as e:  # one bad id shouldn't kill the batch (incl. _StatusRejected)
                entry.update(ok=False, error=str(e))
            results.append(entry)
        return {"results": results}


# --- Diagnostics — fleet-wide distress signals (see kanban_diagnostics) ------

@router.get("/diagnostics")
def list_diagnostics(
    board: Optional[str] = _BOARD_Q,
    severity: Optional[str] = Query(None, description="Filter by severity: warning|error|critical")):
    """Tasks with an active diagnostic, highest severity first then most recent; also
    consumed by ``hermes kanban diagnostics`` when the dashboard runs."""
    with _board_conn(board) as (board, conn):
        diags_by_task = _compute_task_diagnostics(conn, task_ids=None)
        if severity and diags_by_task:
            diags_by_task = {
                tid: keep
                for tid, dl in diags_by_task.items()
                if (keep := [d for d in dl if kd.severity_at_or_above(d.get("severity"), severity)])}
        if not diags_by_task:
            return {"diagnostics": [], "count": 0}
        ids = list(diags_by_task.keys())
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({_placeholders(ids)})", tuple(ids)).fetchall()}
        out = []
        for tid, dl in diags_by_task.items():
            r = rows.get(tid) or {"title": None, "status": None, "assignee": None}
            out.append({
                "task_id": tid, "task_title": r["title"], "task_status": r["status"], "task_assignee": r["assignee"],
                "diagnostics": dl})
        sev_idx = {s: i for i, s in enumerate(kd.SEVERITY_ORDER)}
        out.sort(key=lambda row: (
            -sev_idx.get(row["diagnostics"][0].get("severity"), -1), -(row["diagnostics"][0].get("last_seen_at") or 0)))
        return {"diagnostics": out, "count": sum(len(d["diagnostics"]) for d in out)}


# --- Worker visibility — active-worker list, per-run inspect/terminate -------

try:
    import psutil as _psutil
except ImportError:
    _psutil = None  # type: ignore[assignment]


@router.get("/workers/active")
def list_active_workers(board: Optional[str] = _BOARD_Q):
    """Every running worker: an open ``task_runs`` row with a ``worker_pid`` whose
    task is ``running``. Returns ``{workers, count, checked_at}``."""
    with _board_conn(board) as (board, conn):
        rows = conn.execute(
            "SELECT r.id AS run_id, r.task_id, t.title AS task_title, t.status AS task_status, "
            "t.assignee AS task_assignee, r.profile, r.worker_pid, r.started_at, r.claim_lock, "
            "r.claim_expires, r.last_heartbeat_at, r.max_runtime_seconds "
            "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.ended_at IS NULL AND r.worker_pid IS NOT NULL AND t.status = 'running' "
            "ORDER BY r.started_at ASC").fetchall()
        workers = [dict(row) for row in rows]
        return {"workers": workers, "count": len(workers), "checked_at": int(time.time())}


@router.get("/runs/{run_id}")
def get_run_endpoint(run_id: int, board: Optional[str] = _BOARD_Q):
    """``{run: {...}}`` with the same serialisation as ``GET /tasks/{id}``; 404 if unknown."""
    with _board_conn(board) as (board, conn):
        return {"run": asdict(_require_run(conn, run_id))}


@router.get("/runs/{run_id}/inspect")
def inspect_run_endpoint(run_id: int, board: Optional[str] = _BOARD_Q):
    """Live psutil stats for a run's worker; ``{alive: false, reason}`` when unavailable and
    access-denied reported inline rather than as a 500."""
    with _board_conn(board) as (board, conn):
        r = _require_run(conn, run_id)

    def _dead(reason: str, **extra) -> dict:
        return {"run_id": run_id, "alive": False, **extra, "reason": reason}

    if r.ended_at is not None:
        return _dead("run already ended")
    pid = r.worker_pid
    if pid is None:
        return _dead("no worker_pid recorded")
    if _psutil is None:
        return _dead("psutil not available", pid=pid)
    try:
        proc = _psutil.Process(pid)
        info = proc.as_dict(attrs=["cpu_percent", "memory_info", "num_threads", "status", "create_time", "cmdline"])
        try:
            num_fds = proc.num_fds()
        except AttributeError:  # POSIX-only
            num_fds = None
        mem = info.get("memory_info")
        return {
            "run_id": run_id, "alive": True, "pid": pid,
            "cpu_percent": info.get("cpu_percent"),
            "memory_rss_bytes": mem.rss if mem else None,
            "memory_vms_bytes": mem.vms if mem else None,
            "num_threads": info.get("num_threads"), "num_fds": num_fds,
            "status": info.get("status"), "create_time": info.get("create_time"), "cmdline": info.get("cmdline")}
    except _psutil.NoSuchProcess:
        return _dead("process not found", pid=pid)
    except _psutil.AccessDenied:
        return {"run_id": run_id, "alive": True, "pid": pid, "error": "access denied"}


class TerminateRunBody(BaseModel):
    reason: Optional[str] = None


@router.post("/runs/{run_id}/terminate")
def terminate_run_endpoint(run_id: int, payload: TerminateRunBody, board: Optional[str] = _BOARD_Q):
    """Terminate an in-flight run via ``reclaim_task`` (same SIGTERM->SIGKILL flow, bookkeeping
    and events as ``POST /tasks/{id}/reclaim``); 409 if already ended / not reclaimable.

    Closes the gap left by PR #28432, which shipped the read-only sibling endpoints (``/workers/active``,
    ``/runs/{run_id}``, ``/runs/{run_id}/inspect``) but no termination control surface.
    """
    with _board_conn(board) as (board, conn):
        r = _require_run(conn, run_id)
        if r.ended_at is not None:
            raise _conflict(f"run {run_id} already ended")
        if not kanban_db.reclaim_task(conn, r.task_id, reason=payload.reason):
            raise _conflict(f"cannot terminate run {run_id}: task {r.task_id} is no longer in a reclaimable state")
        return {"ok": True, "run_id": run_id, "task_id": r.task_id}


# --- Recovery actions — reclaim / specify / reassign / estimate -------------

class ReclaimBody(BaseModel):
    reason: Optional[str] = None


@router.post("/tasks/{task_id}/reclaim")
def reclaim_task_endpoint(task_id: str, payload: ReclaimBody, board: Optional[str] = Query(None)):
    """Release an active worker claim without waiting for the claim TTL
    (``hermes kanban reclaim <task_id> --reason ...``)."""
    with _board_conn(board) as (board, conn):
        if not kanban_db.reclaim_task(conn, task_id, reason=payload.reason):
            raise _conflict(f"cannot reclaim {task_id}: not in a claimable state (not running, or unknown id)")
        return {"ok": True, "task_id": task_id}


class SpecifyBody(BaseModel):
    """Only the author is configurable; model + prompt come from
    ``auxiliary.triage_specifier`` in config.yaml, same as the CLI."""

    author: Optional[str] = None


@router.post("/tasks/{task_id}/specify")
def specify_task_endpoint(task_id: str, payload: SpecifyBody, board: Optional[str] = Query(None)):
    """Flesh out a triage task via the auxiliary LLM (``hermes kanban specify``). Non-OK is NOT
    an HTTP error — the UI renders the reason inline. Sync ``def`` → runs in the threadpool."""
    outcome = _run_aux(board, "kanban_specify", "specify_task", task_id, payload.author)
    return {"ok": bool(outcome.ok), "task_id": outcome.task_id, "reason": outcome.reason, "new_title": outcome.new_title}


class ReassignBody(BaseModel):
    profile: Optional[str] = None  # "" or None = unassign
    reclaim_first: bool = False
    reason: Optional[str] = None


@router.post("/tasks/{task_id}/reassign")
def reassign_task_endpoint(task_id: str, payload: ReassignBody, board: Optional[str] = Query(None)):
    """Reassign to another profile, optionally reclaiming first
    (``hermes kanban reassign <task_id> <profile> [--reclaim]``)."""
    with _board_conn(board) as (board, conn):
        ok = kanban_db.reassign_task(
            conn, task_id, payload.profile or None, reclaim_first=bool(payload.reclaim_first), reason=payload.reason)
        if not ok:
            raise _conflict(
                f"cannot reassign {task_id}: unknown id, or still "
                "running (pass reclaim_first=true to release the claim first)")
        return {"ok": True, "task_id": task_id, "assignee": payload.profile or None}


# Estimate: rough token/complexity read via the auxiliary model. NOT a dollar cost.
_ESTIMATE_SYSTEM_PROMPT = (
    "You estimate how much work an autonomous coding agent will spend on a "
    "kanban task. Given the task title and description, respond with STRICT "
    "JSON only (no prose, no code fence):\n"
    '{"est_tokens": <integer total tokens across the whole run>, '
    '"complexity": "S"|"M"|"L", '
    '"rationale": "<one short sentence>"}\n'
    "Base the token figure on a realistic multi-turn agent run (reading files, "
    "tool calls, edits, retries) — not a single reply. S≈small/localized, "
    "M≈multi-file, L≈broad or ambiguous. Be honest that this is a rough guess.")


class EstimateBody(BaseModel):
    title: str = ""
    body: Optional[str] = None


@router.post("/estimate")
def estimate_text_endpoint(payload: EstimateBody):
    """Estimate from raw title/body (create dialog, before a task exists)."""
    return _run_estimate(payload.title, payload.body, task_id=None)


@router.post("/tasks/{task_id}/estimate")
def estimate_task_endpoint(task_id: str, board: Optional[str] = Query(None)):
    """Estimate for an existing task; ``{ok, est_tokens, complexity, rationale, model}``."""
    with _board_conn(board) as (board, conn):
        task = _require_task(conn, task_id)
    return _run_estimate(task.title, task.body, task_id=task_id)


def _cap(s: Optional[str], n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _run_estimate(title: str, body: Optional[str], *, task_id: Optional[str]) -> dict:
    """Never raises — config/parse/API errors become ``{"ok": False, "reason"}`` so the UI renders them inline."""
    if not (title or "").strip():
        return {"ok": False, "reason": "a title is required to estimate"}
    try:
        from agent.auxiliary_client import call_llm
    except Exception:
        return {"ok": False, "reason": "auxiliary client unavailable"}
    user_msg = f"Title: {_cap(title, 400)}\n\nDescription:\n{_cap(body, 4000) or '(none)'}"
    # Headless like specify/decompose's _call_aux: without a bound affinity scope the relay-affinity
    # headers are omitted and the OpenCode Go relay answers 400 MissingSessionID (#112043). The
    # create dialog has no task yet, so it shares one stable key.
    from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
    affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task_id or 'estimate'}")
    try:
        resp = call_llm(
            task="kanban_estimator",
            messages=[{"role": "system", "content": _ESTIMATE_SYSTEM_PROMPT}, {"role": "user", "content": user_msg}],
            temperature=0.0, max_tokens=300, timeout=60)
    except Exception as exc:
        return {"ok": False, "reason": f"LLM error: {type(exc).__name__}"}
    finally:
        if affinity_token is not None:
            reset_affinity_scope(affinity_token)
    try:
        raw = (resp.choices[0].message.content or "").strip()
        model = getattr(resp, "model", None)
    except Exception:
        raw, model = "", None

    # Same tolerant JSON-blob extraction the specifier uses.
    try:
        m = None if raw.lstrip().startswith("{") else re.search(r"\{.*\}", raw, re.DOTALL)
        obj = json.loads(m.group(0) if m else raw)
        parsed = obj if isinstance(obj, dict) else None
    except Exception:
        parsed = None
    if not parsed:
        return {"ok": False, "reason": "could not parse an estimate from the model"}
    try:
        est_tokens = int(parsed.get("est_tokens") or 0)
    except (TypeError, ValueError):
        est_tokens = 0
    complexity = str(parsed.get("complexity") or "").strip().upper()
    return {
        "ok": True, "est_tokens": est_tokens, "complexity": complexity if complexity in {"S", "M", "L"} else None,
        "rationale": str(parsed.get("rationale") or "").strip() or None, "model": model}


# --- Plugin config ----------------------------------------------------------

def _load_config_or_empty() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


@router.get("/config")
def get_config():
    """Kanban dashboard preferences from the ``dashboard.kanban`` config section."""
    k_cfg = (_load_config_or_empty().get("dashboard") or {}).get("kanban") or {}
    return {
        "default_tenant": k_cfg.get("default_tenant") or "",
        "lane_by_profile": bool(k_cfg.get("lane_by_profile", True)),
        "include_archived_by_default": bool(k_cfg.get("include_archived_by_default", False)),
        "render_markdown": bool(k_cfg.get("render_markdown", True))}


# --- Home-channel subscriptions (per-task, per-platform toggles) -------------
# Each gateway platform has at most one "home" (chat_id, thread_id, name); a toggle-on writes
# exactly the notify_subs row ``/kanban create`` would, so the gateway notifier needs no plumbing.

def _configured_home_channels() -> list[dict]:
    """Every platform with a home_channel, from the live GatewayConfig (so env overlays
    like ``TELEGRAM_HOME_CHANNEL`` are honored), sorted by platform."""
    try:
        from gateway.config import load_gateway_config
        gw_cfg = load_gateway_config()
    except Exception:
        return []
    result = [
        {"platform": platform.value, "chat_id": pcfg.home_channel.chat_id,
         "thread_id": pcfg.home_channel.thread_id or "", "name": pcfg.home_channel.name or "Home"}
        for platform, pcfg in gw_cfg.platforms.items() if pcfg and pcfg.home_channel]
    result.sort(key=lambda r: r["platform"])
    return result


def _active_profile_name() -> str:
    """Current Hermes profile name for notify-sub ownership."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _home_for_platform(platform: str, detail: str) -> dict:
    home = next((h for h in _configured_home_channels() if h["platform"] == platform), None)
    if not home:
        raise HTTPException(status_code=404, detail=detail)
    return home


@router.get("/home-channels")
def get_home_channels(task_id: Optional[str] = Query(None), board: Optional[str] = Query(None)):
    """Every platform with a home channel plus whether *task_id* (if given) is
    subscribed to it; without ``task_id`` every ``subscribed`` is false."""
    homes = _configured_home_channels()
    subscribed_homes: set[tuple[str, str, str]] = set()
    if task_id:
        with _board_conn(board) as (board, conn):
            subs = kbn.list_notify_subs(conn, task_id)
        subscribed_homes = {
            (str(sub.get("platform") or ""), str(sub.get("chat_id") or ""), str(sub.get("thread_id") or "")) for sub in subs}
    return {"home_channels": [
        {**home, "subscribed": (home["platform"], home["chat_id"], home["thread_id"]) in subscribed_homes} for home in homes]}


@router.post("/tasks/{task_id}/home-subscribe/{platform}")
def subscribe_home(task_id: str, platform: str, board: Optional[str] = Query(None)):
    """Subscribe *task_id* to *platform*'s home channel. Idempotent at the DB
    layer; 404 when the platform has no home or the task doesn't exist."""
    home = _home_for_platform(
        platform,
        f"No home channel configured for platform {platform!r}. "
        f"Set one from the messenger via /sethome, or configure "
        f"gateway.platforms.{platform}.home_channel in config.yaml.")
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
        kbn.add_notify_sub(
            conn, task_id=task_id, platform=platform, chat_id=home["chat_id"],
            thread_id=home["thread_id"] or None, notifier_profile=_active_profile_name())
        return {"ok": True, "task_id": task_id, "home_channel": home}


@router.delete("/tasks/{task_id}/home-subscribe/{platform}")
def unsubscribe_home(task_id: str, platform: str, board: Optional[str] = Query(None)):
    """Remove any notify subscription on *task_id* matching *platform*'s home."""
    home = _home_for_platform(platform, f"No home channel configured for platform {platform!r}.")
    with _board_conn(board) as (board, conn):
        kbn.remove_notify_sub(
            conn, task_id=task_id, platform=platform, chat_id=home["chat_id"], thread_id=home["thread_id"] or None)
        return {"ok": True, "task_id": task_id, "home_channel": home}


# --- Stats / assignees / worker log / dispatch / model options ---------------

@router.get("/stats")
def get_stats(board: Optional[str] = Query(None)):
    """Per-status + per-assignee counts + oldest-ready age (HUD and router profiles)."""
    with _board_conn(board) as (board, conn):
        return kanban_db.board_stats(conn)


@router.get("/assignees")
def get_assignees(board: Optional[str] = Query(None)):
    """Union of on-disk profiles and assignees used on the board, so a fresh
    profile appears in the picker before it has any task."""
    with _board_conn(board) as (board, conn):
        return {"assignees": kanban_db.known_assignees(conn)}


@router.get("/tasks/{task_id}/log")
def get_task_log(task_id: str, tail: Optional[int] = Query(None, ge=1, le=2_000_000), board: Optional[str] = Query(None)):
    """Worker stdout/stderr log. ``tail`` caps the response bytes; 404 if the
    task never spawned. On-disk log rotates at 2 MiB with one ``.log.1`` kept."""
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
    content = kanban_db.read_worker_log(task_id, tail_bytes=tail, board=board)
    log_path = kanban_db.worker_log_path(task_id, board=board)
    size = log_path.stat().st_size if log_path.exists() else 0
    return {
        "task_id": task_id, "path": str(log_path), "exists": content is not None,
        "size_bytes": size, "content": content or "", "truncated": bool(tail and size > tail)}


@router.post("/dispatch")
def dispatch(dry_run: bool = Query(False), max_n: int = Query(8, alias="max"), board: Optional[str] = Query(None)):
    """Dispatch nudge so the UI doesn't wait out the 60 s dispatcher tick."""
    with _board_conn(board) as (board, conn):
        result = kbd.dispatch_once(conn, dry_run=dry_run, max_spawn=max_n, board=board)
        try:
            return asdict(result)  # DispatchResult is a dataclass
        except TypeError:
            return {"result": str(result)}


@router.get("/model-options")
def model_options():
    """Providers + curated models for the override dropdown via ``inventory.build_models_payload``
    (same substrate as the Models page) so it can't offer a pair Hermes rejects. Skips pricing
    and custom-provider probes: a slow/offline local endpoint must not hang the drawer."""
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        payload = build_models_payload(
            load_picker_context(), explicit_only=True, canonical_order=True, probe_custom_providers=False)
        return {
            "providers": [
                {"slug": row.get("slug", ""), "label": row.get("label") or row.get("slug", ""),
                 "models": list(row.get("models") or [])}
                for row in payload.get("providers", [])
                if row.get("models")]}
    except Exception:
        log.exception("kanban model-options failed")
        return {"providers": []}  # empty catalog → the UI falls back to a free-text input


# --- Boards CRUD (multi-project support) --------------------------------------

class CreateBoardBody(BaseModel):
    slug: str
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    default_workdir: Optional[str] = None
    # Project (id or slug) scoping the board: default_workdir mirrors its primary repo, tasks inherit it.
    project_id: Optional[str] = None
    switch: bool = False


class RenameBoardBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    # For both fields: ``None`` = leave unchanged; "" = clear; value = validate/resolve + set.
    default_workdir: Optional[str] = None
    project_id: Optional[str] = None


# Board transfer exchanges filesystem PATHS, not bytes (same contract as profile export/import):
# clients run the native save/open dialog on the machine hosting the backend.

class ExportBoardBody(BaseModel):
    output: str = ""  # empty → staging path under the kanban root
    attachments: bool = True
    logs: bool = False


class ImportBoardBody(BaseModel):
    archive: str  # path to a board .tar.gz on the backend's filesystem
    slug: Optional[str] = None  # override the archive's slug; collisions auto-suffix
    switch: bool = False


def _board_display_kwargs(p: BaseModel) -> dict[str, Any]:
    """Display-metadata fields shared by create_board / write_board_metadata."""
    return {"name": p.name, "description": p.description, "icon": p.icon, "color": p.color}


def _resolve_project(ref: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve a project id/slug to ``(id, name, primary_path)``; ``(None,)*3``
    for a falsy ref, 400 when a non-empty ref doesn't resolve."""
    if not ref or not ref.strip():
        return None, None, None
    with _errors_to_500("projects unavailable"):
        from hermes_cli import projects_db as pdb
        with pdb.connect_closing() as pconn:
            proj = pdb.get_project(pconn, ref.strip())
    if proj is None:
        raise HTTPException(status_code=400, detail=f"project {ref!r} does not exist")
    return proj.id, proj.name, (proj.primary_path or None)


def _projects_by_id() -> dict[str, Any]:
    """Map every project id -> Project (archived included) for annotation."""
    try:
        from hermes_cli import projects_db as pdb
        with pdb.connect_closing() as pconn:
            return {p.id: p for p in pdb.list_projects(pconn, include_archived=True)}
    except Exception:
        return {}


def _board_counts(slug: str) -> dict[str, int]:
    """``{status: count}`` for a board; ``{}`` on a missing/empty DB."""
    try:
        if not kanban_db.kanban_db_path(board=slug).exists():
            return {}
        with closing(kbc.connect(board=slug)) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
            return {r["status"]: int(r["n"]) for r in rows}
    except Exception:
        return {}


def _default_workspace_kind(board: dict[str, Any]) -> str:
    """Recommend a non-destructive task workspace from board metadata."""
    workdir = str(board.get("default_workdir") or "").strip()
    if not workdir:
        return "scratch"
    try:
        return "worktree" if kbw._git_toplevel(Path(workdir)) else "dir"
    except (OSError, ValueError):
        return "dir"


def _annotate_board_meta(meta: dict) -> dict:
    meta["default_workspace_kind"] = _default_workspace_kind(meta)
    _, meta["project_name"], _ = _resolve_project(meta.get("project_id"))
    return meta


@router.get("/projects")
def list_kanban_projects():
    """Live (non-archived) projects available for board scoping."""
    with _errors_to_500("failed to list projects"):
        from hermes_cli import projects_db as pdb
        with pdb.connect_closing() as pconn:
            projects = pdb.list_projects(pconn, include_archived=False)
    return {"projects": [
        {"id": p.id, "slug": p.slug, "name": p.name,
         "primary_path": p.primary_path or "", "icon": p.icon or "", "color": p.color or ""}
        for p in projects]}


@router.get("/boards")
def list_boards(include_archived: bool = Query(False)):
    """Every board on disk with task counts and the active slug."""
    boards = kanban_db.list_boards(include_archived=include_archived)
    current = kanban_db.get_current_board()
    proj_map = _projects_by_id()
    for b in boards:
        b["is_current"] = (b["slug"] == current)
        b["counts"] = _board_counts(b["slug"])
        # Live cards only — archived tasks are hidden from every default board view,
        # so counting them in the switcher badge would visibly disagree.
        b["total"] = sum(n for status, n in b["counts"].items() if status != "archived")
        b["default_workspace_kind"] = _default_workspace_kind(b)
        pid = b["project_id"] = b.get("project_id") or None
        proj = proj_map.get(pid) if pid else None
        b["project_name"] = proj.name if proj else None
    return {"boards": boards, "current": current}


def _validate_workdir(raw: str) -> str:
    """Board default_workdir must be an absolute, existing directory (400 otherwise)."""
    requested = Path(raw).expanduser()
    if not requested.is_absolute():
        raise HTTPException(status_code=400, detail="Project directory must be an absolute path.")
    if not requested.is_dir():
        raise HTTPException(status_code=400, detail="Project directory must be an existing directory.")
    return str(requested.resolve())


@router.post("/boards")
def create_board_endpoint(payload: CreateBoardBody):
    """Create a board. Idempotent — ``slug`` collision returns the existing one."""
    default_workdir = _validate_workdir(payload.default_workdir) if payload.default_workdir else None
    # A chosen project's primary repo becomes the default workdir unless one was passed explicitly.
    project_id, _pname, primary_path = _resolve_project(payload.project_id)
    if primary_path and not default_workdir:
        default_workdir = primary_path
    with _value_error_400():
        meta = kanban_db.create_board(
            payload.slug, default_workdir=default_workdir, project_id=project_id, **_board_display_kwargs(payload))
    if payload.switch:
        with _value_error_400():
            kanban_db.set_current_board(meta["slug"])
    return {"board": _annotate_board_meta(meta), "current": kanban_db.get_current_board()}


@router.patch("/boards/{slug}")
def rename_board(slug: str, payload: RenameBoardBody):
    """Update display metadata / default workdir / project scope (slug is immutable)."""
    normed = _existing_board_slug(slug)
    # write_board_metadata treats a falsy value as "clear", so pass "" through.
    default_workdir: Optional[str] = None
    if payload.default_workdir is not None:
        raw = payload.default_workdir.strip()
        default_workdir = _validate_workdir(raw) if raw else ""
    # A resolved project mirrors its repo into default_workdir unless the caller set it explicitly.
    project_id: Optional[str] = None
    if payload.project_id is not None:
        if payload.project_id.strip():
            project_id, _pname, primary_path = _resolve_project(payload.project_id)
            if primary_path and default_workdir is None:
                default_workdir = primary_path
        else:
            project_id = ""  # clear the scope
    meta = kanban_db.write_board_metadata(
        normed, default_workdir=default_workdir, project_id=project_id, **_board_display_kwargs(payload))
    return {"board": _annotate_board_meta(meta)}


@router.delete("/boards/{slug}")
def delete_board(slug: str, delete: bool = Query(False, description="Hard-delete instead of archive")):
    """Archive (default) or hard-delete a board."""
    with _value_error_400():
        res = kanban_db.remove_board(slug, archive=not delete)
    return {"result": res, "current": kanban_db.get_current_board()}


async def _run_transfer(fn, log_label: str):
    """Run a blocking kanban_transfer call off the event loop, mapping its errors
    to 404 (missing path) / 400 (invalid) / 500 (logged)."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, fn)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("%s failed", log_label)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/boards/{slug}/export")
async def export_board_endpoint(slug: str, body: ExportBoardBody):
    """Write ``slug`` to a portable archive; return the path written."""
    from hermes_cli import kanban_transfer

    output = (body.output or "").strip()
    if not output:
        staging = kanban_db.kanban_home() / "kanban" / "board-exports"
        try:
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not create export directory: {exc}")
        output = str(staging / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    return await _run_transfer(
        lambda: kanban_transfer.export_board(slug, output, include_attachments=body.attachments, include_logs=body.logs),
        f"POST /boards/{slug}/export")


@router.post("/boards/import")
async def import_board_endpoint(body: ImportBoardBody):
    """Import a board archive as a NEW board; return the landed board."""
    from hermes_cli import kanban_transfer

    archive = (body.archive or "").strip()
    if not archive:
        raise HTTPException(status_code=400, detail="archive path is required")
    result = await _run_transfer(
        lambda: kanban_transfer.import_board(archive, (body.slug or "").strip() or None, activate=body.switch),
        "POST /boards/import")
    return {**result, "current": kanban_db.get_current_board()}


@router.post("/boards/{slug}/switch")
def switch_board(slug: str):
    """Persist ``slug`` as the active board for CLI / slash-command parity
    (dashboard users pick boards client-side via localStorage)."""
    normed = _existing_board_slug(slug)
    kanban_db.set_current_board(normed)
    return {"current": normed}


# --- Profile metadata & description editing (kanban orchestrator) ------------

class DescribeBody(BaseModel):
    description: Optional[str] = None  # explicit user-authored text


class DescribeAutoBody(BaseModel):
    overwrite: bool = False


@router.get("/profiles")
def list_profile_roster():
    """Every installed profile with its description (profiles without one are
    still routable on name alone, just less precisely)."""
    with _errors_to_500("failed to list profiles"):
        from hermes_cli import profiles as profiles_mod
        profiles = profiles_mod.list_profiles()
    return {"profiles": [
        {"name": p.name, "is_default": bool(p.is_default), "model": p.model or "", "provider": p.provider or "",
         "description": p.description or "", "description_auto": bool(p.description_auto),
         "skill_count": int(p.skill_count or 0)}
        for p in profiles]}


@router.patch("/profiles/{profile_name}")
def update_profile_description(profile_name: str, payload: DescribeBody):
    """Set (``description_auto: false`` so the auto-describer won't overwrite it
    without ``--overwrite``) or clear (empty string) a profile's description."""
    with _errors_to_500("failed to update profile"):
        from hermes_cli import profiles as profiles_mod
        canon = profiles_mod.normalize_profile_name(profile_name)
        if canon == "default":
            from hermes_constants import get_hermes_home  # type: ignore
            profile_dir = Path(get_hermes_home())
        else:
            profile_dir = profiles_mod.get_profile_dir(canon)
        if not profile_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"profile '{profile_name}' not found")
        text = (payload.description or "").strip()
        profiles_mod.write_profile_meta(profile_dir, description=text, description_auto=False)
    return {"ok": True, "profile": canon, "description": text}


@router.post("/profiles/{profile_name}/describe-auto")
def auto_describe_profile(profile_name: str, payload: DescribeAutoBody):
    """``hermes profile describe <name> --auto``: persist with ``description_auto: true``.
    Non-OK outcomes are NOT HTTP errors — the UI renders the reason inline."""
    with _errors_to_500("describer crashed"):
        from hermes_cli import profile_describer
        outcome = profile_describer.describe_profile(profile_name, overwrite=bool(payload.overwrite))
    return {"ok": bool(outcome.ok), "profile": outcome.profile_name, "reason": outcome.reason, "description": outcome.description}


# --- Decompose (built-in decomposer fan-out) ----------------------------------

class DecomposeBody(BaseModel):
    author: Optional[str] = None


@router.post("/tasks/{task_id}/decompose")
def decompose_task_endpoint(task_id: str, payload: DecomposeBody, board: Optional[str] = Query(None)):
    """Fan a triage task out into child tasks via the auxiliary LLM (``hermes kanban decompose``).
    Non-OK is NOT an HTTP error. Sync ``def`` → runs in the threadpool."""
    outcome = _run_aux(board, "kanban_decompose", "decompose_task", task_id, payload.author)
    return {
        "ok": bool(outcome.ok), "task_id": outcome.task_id, "reason": outcome.reason,
        "fanout": bool(outcome.fanout), "child_ids": outcome.child_ids or [], "new_title": outcome.new_title}


# --- Orchestration settings (kanban.orchestrator_profile / default_assignee /
#     auto_decompose / auto_promote_children) ----------------------------------

class OrchestrationSettingsBody(BaseModel):
    orchestrator_profile: Optional[str] = None
    default_assignee: Optional[str] = None
    auto_decompose: Optional[bool] = None
    auto_promote_children: Optional[bool] = None


_PROFILE_SETTINGS = ("orchestrator_profile", "default_assignee")


@router.get("/orchestration")
def get_orchestration_settings():
    """Current orchestration knobs from config.yaml plus the resolved effective
    values (fallbacks filled the same way the decomposer does)."""
    cfg = _load_config_or_empty()
    kanban_cfg = (cfg.get("kanban") or {}) if isinstance(cfg, dict) else {}
    explicit = {k: (kanban_cfg.get(k) or "").strip() for k in _PROFILE_SETTINGS}
    resolved = dict(explicit)
    try:
        from hermes_cli import profiles as profiles_mod
        active_default = profiles_mod.get_active_profile_name() or "default"
        for k, v in explicit.items():
            if not v or not profiles_mod.profile_exists(v):
                resolved[k] = active_default
    except Exception:
        active_default = "default"
        resolved = {k: v or active_default for k, v in resolved.items()}
    return {
        "orchestrator_profile": explicit["orchestrator_profile"],
        "default_assignee": explicit["default_assignee"],
        "auto_decompose": bool(kanban_cfg.get("auto_decompose", True)),
        "auto_promote_children": bool(kanban_cfg.get("auto_promote_children", True)),
        "resolved_orchestrator_profile": resolved["orchestrator_profile"],
        "resolved_default_assignee": resolved["default_assignee"],
        "active_profile": active_default}


def _validated_profile_name(raw: Optional[str], profiles_mod) -> str:
    """Strip a profile name; 400 if non-empty and unknown. Fails open when the lookup itself errors."""
    name = (raw or "").strip()
    if name and profiles_mod is not None:
        try:
            exists = profiles_mod.profile_exists(name)
        except Exception:
            exists = True
        if not exists:
            raise HTTPException(status_code=400, detail=f"profile '{name}' does not exist")
    return name


@router.put("/orchestration")
def set_orchestration_settings(payload: OrchestrationSettingsBody):
    """Update orchestration knobs in config.yaml. Only fields explicitly passed
    are written; empty profile strings clear the override."""
    with _errors_to_500("failed to load config"):
        from hermes_cli.config import load_config, save_config
        cfg = load_config() or {}
    kanban_section = cfg.setdefault("kanban", {})
    if not isinstance(kanban_section, dict):
        kanban_section = cfg["kanban"] = {}
    try:
        from hermes_cli import profiles as profiles_mod
    except Exception:
        profiles_mod = None  # type: ignore
    # Field order == write order (profiles validated first, then the booleans).
    for key, value in payload.model_dump(exclude_none=True).items():
        kanban_section[key] = _validated_profile_name(value, profiles_mod) if key in _PROFILE_SETTINGS else bool(value)
    with _errors_to_500("failed to save config"):
        save_config(cfg)
    return get_orchestration_settings()  # callers re-render from the resolved state


# --- Board summary: /board-summary?board=<slug> -------------------------------
#
# Generic consumer for producer-written summary files under
# ``<HERMES_HOME>/state/board-summaries/<slug>.json`` (schema
# ``cc://board-summary/v2``). The dashboard only ever reads this one
# derived file — it never reaches the producer's engine, cron state, or
# arbitrary filesystem paths. Error contract is intentionally stable and
# opaque: 404 = no summary published, 503 = a file exists but cannot be
# served safely (never echoes parser/OS detail to the client).

_BOARD_SUMMARY_MAX_BYTES = 1_048_576  # bound reads; a summary is a few KB
_BOARD_SUMMARY_404 = "board summary not found"
_BOARD_SUMMARY_503 = "board summary unavailable"


def _board_summary_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail=_BOARD_SUMMARY_503)


def _open_board_summary_directory() -> int:
    """Open the active profile's summary directory without following ancestors."""
    from hermes_constants import get_hermes_home

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise _board_summary_unavailable()
    path = get_hermes_home() / "state" / "board-summaries"
    if (
        not path.is_absolute()
        or ".." in path.parts
        or os.path.normpath(str(path)) != str(path)
    ):
        raise _board_summary_unavailable()
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        fd = os.open("/", flags)
        for component in path.parts[1:]:
            try:
                entry = os.stat(component, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail=_BOARD_SUMMARY_404)
            if not stat.S_ISDIR(entry.st_mode):
                raise _board_summary_unavailable()
            next_fd = -1
            try:
                next_fd = os.open(component, flags, dir_fd=fd)
                info = os.fstat(next_fd)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_dev != entry.st_dev
                    or info.st_ino != entry.st_ino
                ):
                    raise _board_summary_unavailable()
            except BaseException:
                if next_fd >= 0:
                    os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except HTTPException:
        if fd >= 0:
            os.close(fd)
        raise
    except (OSError, TypeError, NotImplementedError):
        if fd >= 0:
            os.close(fd)
        raise _board_summary_unavailable()


def _summary_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_board_summary_bytes(slug: str) -> bytes:
    """Read one descriptor-bound regular file below non-symlink ancestors."""
    directory_fd = _open_board_summary_directory()
    file_fd = -1
    filename = f"{slug}.json"
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        try:
            file_fd = os.open(filename, flags, dir_fd=directory_fd)
        except (FileNotFoundError, NotADirectoryError):
            raise HTTPException(status_code=404, detail=_BOARD_SUMMARY_404)
        except (OSError, TypeError, NotImplementedError):
            raise _board_summary_unavailable()

        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _BOARD_SUMMARY_MAX_BYTES:
            raise _board_summary_unavailable()
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            try:
                chunk = os.read(file_fd, min(remaining, 65536))
            except InterruptedError:
                continue
            if not chunk:
                raise _board_summary_unavailable()
            chunks.append(chunk)
            remaining -= len(chunk)
        while True:
            try:
                extra = os.read(file_fd, 1)
                break
            except InterruptedError:
                continue
        if extra:
            raise _board_summary_unavailable()
        after = os.fstat(file_fd)
        entry = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(entry.st_mode)
            or _summary_file_identity(after) != _summary_file_identity(before)
            or _summary_file_identity(entry) != _summary_file_identity(before)
        ):
            raise _board_summary_unavailable()
        return b"".join(chunks)
    except HTTPException:
        raise
    except (OSError, TypeError, NotImplementedError):
        raise _board_summary_unavailable()
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)


def _parse_board_summary(raw: bytes) -> dict[str, Any]:
    def _reject_constant(name: str):
        raise ValueError(f"non-finite constant {name!r}")

    def _parse_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object member")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _board_summary_unavailable()
    if not isinstance(payload, dict):
        raise _board_summary_unavailable()
    return payload


# Published ``cc://board-summary/v2`` contract. Routing remains board-generic,
# while the versioned payload validator enforces the producer schema's fixed
# inventory constants, strict types (bools are not counts), and every cross-field
# count, digest, authorization, and clean-window invariant.

_BS_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_BS_BOARD_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_BS_API_BASE_RE = re.compile(r"^https?://.*$")
_BS_ABSOLUTE_PATH_RE = re.compile(r"^/.*$")
_BS_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_BS_OPT_HEX64_RE = re.compile(r"^(|[0-9a-f]{64})$")
_BS_CRON_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_BS_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_BS_WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_BS_PHASES = frozenset({"canary", "pre_removal", "post_removal"})
_BS_STATUSES = frozenset({"monitoring", "canary_passed", "removal_ready", "completed", "failed"})
_BS_EXPECTED_LEGACY_IDS = (
    "014c79161bf3",
    "62778bbc7f7b",
    "705134be1948",
    "bf5ff2b8c22a",
)
_BS_EXPECTED_LEGACY_DIGEST = "107ff5dc64a718f9c20216032b4ec25fcc98a6a4dad0c8dae5b0f2b690f2c9e2"

_BS_TOP_KEYS = frozenset({
    "schema_version", "board_slug", "generated_at", "expires_at", "staleness",
    "phase", "status", "cron_removal_authorized", "pre_removal_proof_digest",
    "clean_since", "clean_elapsed_seconds", "required_clean_seconds",
    "completed", "engine", "cron_jobs_remaining", "cron_inventory",
    "blocked_cards_count", "duplicate_blocked_signature_count",
    "pending_grace_count", "schedule_totals", "global", "barrier", "lanes",
    "schedules", "blocked_cards", "pending_occurrences", "findings", "source"})

_BS_CRON_KEYS = frozenset({
    "expected_legacy_ids", "expected_legacy_ids_digest", "live_ids",
    "live_ids_digest", "live_enabled_ids", "live_non_paused_ids",
    "source_observed", "source_ids", "source_ids_digest",
    "source_live_converged", "phase_expectation_met"})

_BS_GLOBAL_COUNT_KEYS = (
    "running_count", "paused_count", "success_count", "failed_count",
    "contention_count", "duplicate_count", "stale_prerequisite_count",
    "blocked_count", "cron_jobs_remaining", "schedule_count",
    "schedule_paused_count", "pending_grace_count", "missed_boundary_count",
    "findings_count")

_BS_LANE_KEYS = frozenset({
    "name", "observed_ids", "running_ids", "running_count", "paused_ids",
    "paused_count", "success_ids", "terminal_successes", "failed_ids",
    "failed_count", "contention_ids", "contention_count", "duplicate_ids",
    "duplicate_count", "stale_prerequisite_ids", "stale_prerequisite_count",
    "occurrences"})

_BS_SCHEDULE_KEYS = frozenset({
    "name", "cron_expression", "zone", "paused", "workflow_name",
    "workflow_version", "correlation_id", "task_to_domain", "workflow_input",
    "run_catchup_schedule_instances", "start_time", "end_time",
    "overlap_policy", "expected_minimum_occurrences", "observed_count",
    "matched_workflow_ids", "unmatched_workflow_ids", "pending_boundaries",
    "drift", "missed_boundaries"})

_BS_TOTALS_KEYS = frozenset({
    "configured", "observed", "paused", "drifted", "pending", "missed",
    "source_names", "live_keys", "paused_names", "drifted_names"})

_BS_CARD_KEYS = frozenset({"board", "id", "title", "status", "created_at", "incident_id"})
_BS_FINDING_KEYS = frozenset({"severity", "code", "message", "evidence"})
_BS_BARRIER_KEYS = frozenset({"passed", "inventory_digest", "workflow_digest", "waves"})
_BS_SOURCE_KEYS = frozenset({
    "api_base", "ledger_dir", "schedules_dir", "boards_root", "cron_jobs_path",
    "source_cron_jobs_path", "summary_path"})


class _SummaryInvalid(ValueError):
    """Raised (with an internal-only reason) for any v2 contract violation."""


def _bs_fail(reason: str) -> None:
    raise _SummaryInvalid(reason)


def _bs_require(ok: bool, reason: str) -> None:
    if not ok:
        _bs_fail(reason)


def _bs_int(value: Any, reason: str) -> int:
    _bs_require(isinstance(value, int) and not isinstance(value, bool), reason)
    return value


def _bs_count(value: Any, reason: str) -> int:
    _bs_require(_bs_int(value, reason) >= 0, reason)
    return value


def _bs_bool(value: Any, reason: str) -> bool:
    _bs_require(isinstance(value, bool), reason)
    return value


def _bs_str(value: Any, reason: str, pattern: Optional[re.Pattern] = None,
            allow_empty: bool = True) -> str:
    _bs_require(isinstance(value, str), reason)
    if not allow_empty:
        _bs_require(bool(value), reason)
    if pattern is not None:
        _bs_require(pattern.fullmatch(value) is not None, reason)
    return value


def _bs_ts(value: Any, reason: str) -> datetime:
    _bs_str(value, reason, pattern=_BS_TIMESTAMP_RE)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _bs_fail(reason)


def _bs_exact_keys(value: Any, keys: frozenset, reason: str) -> dict:
    _bs_require(isinstance(value, dict) and set(value.keys()) == keys, reason)
    return value


def _bs_sorted_unique(value: Any, reason: str, pattern: re.Pattern) -> list[str]:
    _bs_require(isinstance(value, list), reason)
    for item in value:
        _bs_str(item, reason, pattern=pattern)
    _bs_require(value == sorted(value) and len(value) == len(set(value)), reason)
    return value


def _bs_digest(ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode("utf-8")).hexdigest()


def _bs_validate_cron_inventory(payload: dict[str, Any]) -> dict[str, list[str]]:
    cron = _bs_exact_keys(payload["cron_inventory"], _BS_CRON_KEYS, "cron_inventory shape")
    id_lists = {
        field: _bs_sorted_unique(cron[field], f"cron_inventory.{field}", _BS_CRON_ID_RE)
        for field in ("expected_legacy_ids", "live_ids", "live_enabled_ids",
                      "live_non_paused_ids", "source_ids")}
    _bs_str(cron["expected_legacy_ids_digest"], "cron digest", pattern=_BS_HEX64_RE)
    _bs_str(cron["live_ids_digest"], "cron digest", pattern=_BS_HEX64_RE)
    _bs_str(cron["source_ids_digest"], "cron digest", pattern=_BS_OPT_HEX64_RE)
    observed = _bs_bool(cron["source_observed"], "cron booleans")
    converged = _bs_bool(cron["source_live_converged"], "cron booleans")
    _bs_bool(cron["phase_expectation_met"], "cron booleans")
    live = id_lists["live_ids"]
    _bs_require(payload["cron_jobs_remaining"] == len(live), "cron_jobs_remaining != len(live_ids)")
    _bs_require(set(id_lists["live_enabled_ids"]) <= set(live), "enabled ids not live")
    _bs_require(set(id_lists["live_non_paused_ids"]) <= set(live), "non-paused ids not live")
    _bs_require(cron["expected_legacy_ids_digest"] == _bs_digest(id_lists["expected_legacy_ids"]),
                "expected_legacy_ids_digest does not bind expected_legacy_ids")
    _bs_require(cron["live_ids_digest"] == _bs_digest(live), "live_ids_digest does not bind live_ids")
    if observed:
        _bs_require(cron["source_ids_digest"] == _bs_digest(id_lists["source_ids"]),
                    "source_ids_digest does not bind source_ids")
    else:
        _bs_require(not id_lists["source_ids"] and cron["source_ids_digest"] == "",
                    "unobserved source inventory must be empty")
    _bs_require(converged == (observed and id_lists["source_ids"] == live),
                "source_live_converged contradicts cron IDs")
    _bs_require(
        id_lists["expected_legacy_ids"] == list(_BS_EXPECTED_LEGACY_IDS)
        and cron["expected_legacy_ids_digest"] == _BS_EXPECTED_LEGACY_DIGEST,
        "expected legacy cron inventory does not match schema v2",
    )
    return id_lists


def _bs_validate_lanes(payload: dict[str, Any]) -> tuple[dict[str, int], set[str]]:
    lanes = payload["lanes"]
    _bs_require(isinstance(lanes, list), "lanes must be a list")
    names = [lane.get("name") if isinstance(lane, dict) else None for lane in lanes]
    _bs_require(all(isinstance(n, str) for n in names)
                and names == sorted(names) and len(names) == len(set(names)),
                "lanes must have unique sorted names")
    sums = {key: 0 for key in (
        "running_count", "paused_count", "success_count", "failed_count",
        "contention_count", "duplicate_count", "stale_prerequisite_count")}
    seen: set[str] = set()
    for lane in lanes:
        _bs_exact_keys(lane, _BS_LANE_KEYS, "lane shape")
        _bs_str(lane["name"], "lane name", pattern=_BS_NAME_RE)
        observed = _bs_sorted_unique(lane["observed_ids"], "lane observed_ids", _BS_WORKFLOW_ID_RE)
        _bs_require(not (seen & set(observed)), "workflow IDs appear in multiple lanes")
        seen.update(observed)
        partitions: list[set[str]] = []
        for ids_field, count_field, sum_field in (
                ("running_ids", "running_count", "running_count"),
                ("paused_ids", "paused_count", "paused_count"),
                ("success_ids", "terminal_successes", "success_count"),
                ("failed_ids", "failed_count", "failed_count")):
            ids = _bs_sorted_unique(lane[ids_field], f"lane {ids_field}", _BS_WORKFLOW_ID_RE)
            _bs_require(_bs_count(lane[count_field], "lane count") == len(ids),
                        f"lane {count_field} != len({ids_field})")
            sums[sum_field] += len(ids)
            partitions.append(set(ids))
        _bs_require(not any(a & b for i, a in enumerate(partitions) for b in partitions[i + 1:]),
                    "lane status ID arrays must be pairwise disjoint")
        _bs_require(set(observed) == set().union(*partitions),
                    "lane observed_ids must equal the status partition union")
        occurrences = lane["occurrences"]
        _bs_require(isinstance(occurrences, list), "lane occurrences must be a list")
        occurrence_ids = []
        for item in occurrences:
            _bs_exact_keys(item, frozenset({"workflow_id", "started_at"}), "lane occurrence shape")
            occurrence_ids.append(_bs_str(item["workflow_id"], "occurrence id", pattern=_BS_WORKFLOW_ID_RE))
            _bs_ts(item["started_at"], "occurrence started_at")
        _bs_require(occurrence_ids == observed, "lane occurrences must mirror observed_ids")
        for ids_field, count_field, sum_field in (
                ("contention_ids", "contention_count", "contention_count"),
                ("duplicate_ids", "duplicate_count", "duplicate_count"),
                ("stale_prerequisite_ids", "stale_prerequisite_count", "stale_prerequisite_count")):
            ids = _bs_sorted_unique(lane[ids_field], f"lane {ids_field}", _BS_WORKFLOW_ID_RE)
            _bs_require(_bs_count(lane[count_field], "lane count") == len(ids)
                        and set(ids) <= set(lane["failed_ids"]),
                        f"lane {count_field} must count a subset of failed_ids")
            sums[sum_field] += len(ids)
    return sums, seen


def _bs_validate_blocked_cards(payload: dict[str, Any]) -> None:
    cards = payload["blocked_cards"]
    _bs_require(isinstance(cards, list), "blocked_cards must be a list")
    identities = []
    signature_counts: dict[str, int] = {}
    for card in cards:
        _bs_exact_keys(card, _BS_CARD_KEYS, "blocked card shape")
        board = _bs_str(
            card["board"], "card board", pattern=_BS_BOARD_SLUG_RE, allow_empty=False
        )
        card_id = _bs_str(card["id"], "card id", allow_empty=False)
        _bs_str(card["title"], "card title")
        _bs_require(card["status"] in {"blocked", "triage"}, "card status")
        _bs_count(card["created_at"], "card created_at")
        incident = card["incident_id"]
        _bs_require(incident is None or isinstance(incident, str), "card incident_id")
        identities.append(f"{board}:{card_id}")
        if isinstance(incident, str) and incident:
            signature_counts[incident] = signature_counts.get(incident, 0) + 1
    _bs_require(identities == sorted(identities) and len(identities) == len(set(identities)),
                "blocked_cards must be uniquely sorted")
    _bs_require(payload["blocked_cards_count"] == len(cards)
                and payload["global"]["blocked_count"] == len(cards),
                "blocked card counts must equal blocked_cards length")
    duplicates = sum(1 for count in signature_counts.values() if count > 1)
    _bs_require(payload["duplicate_blocked_signature_count"] == duplicates,
                "duplicate_blocked_signature_count must count duplicate signature groups")


def _bs_validate_schedules(payload: dict[str, Any], observed_workflow_ids: set[str]) -> int:
    """Validate schedules + schedule_totals + pending_occurrences; return missed total."""
    schedules = payload["schedules"]
    _bs_require(isinstance(schedules, list), "schedules must be a list")
    names = [item.get("name") if isinstance(item, dict) else None for item in schedules]
    _bs_require(all(isinstance(n, str) for n in names)
                and names == sorted(names) and len(names) == len(set(names)),
                "schedules must have unique sorted names")
    all_matched: set[str] = set()
    expected_pending: list[dict[str, str]] = []
    missed_total = 0
    for schedule in schedules:
        _bs_exact_keys(schedule, _BS_SCHEDULE_KEYS, "schedule shape")
        _bs_str(schedule["name"], "schedule name", pattern=_BS_NAME_RE)
        _bs_str(schedule["workflow_name"], "schedule workflow_name", pattern=_BS_NAME_RE)
        _bs_str(schedule["cron_expression"], "schedule cron_expression", allow_empty=False)
        _bs_str(schedule["zone"], "schedule zone", allow_empty=False)
        _bs_bool(schedule["paused"], "schedule paused")
        _bs_require(_bs_int(schedule["workflow_version"], "schedule workflow_version") >= 1,
                    "schedule workflow_version")
        _bs_str(schedule["correlation_id"], "schedule correlation_id", allow_empty=False)
        _bs_require(isinstance(schedule["task_to_domain"], dict)
                    and isinstance(schedule["workflow_input"], dict), "schedule mappings")
        _bs_require(schedule["run_catchup_schedule_instances"] is None
                    or isinstance(schedule["run_catchup_schedule_instances"], bool),
                    "schedule run_catchup")
        for bound in ("start_time", "end_time"):
            value = schedule[bound]
            _bs_require(value is None or _bs_count(value, f"schedule {bound}") >= 0,
                        f"schedule {bound}")
        _bs_require(schedule["overlap_policy"] is None
                    or isinstance(schedule["overlap_policy"], str), "schedule overlap_policy")
        matched = _bs_sorted_unique(schedule["matched_workflow_ids"],
                                    "schedule matched ids", _BS_WORKFLOW_ID_RE)
        unmatched = _bs_sorted_unique(schedule["unmatched_workflow_ids"],
                                      "schedule unmatched ids", _BS_WORKFLOW_ID_RE)
        pending = _bs_sorted_unique(schedule["pending_boundaries"],
                                    "schedule pending", _BS_TIMESTAMP_RE)
        missed = _bs_sorted_unique(schedule["missed_boundaries"],
                                   "schedule missed", _BS_TIMESTAMP_RE)
        drift = schedule["drift"]
        _bs_require(isinstance(drift, list)
                    and all(isinstance(item, str) and item for item in drift), "schedule drift")
        _bs_require(_bs_count(schedule["observed_count"], "schedule observed_count") == len(matched),
                    "schedule observed_count must count matched_workflow_ids")
        _bs_require(
            _bs_count(schedule["expected_minimum_occurrences"], "schedule expected occurrences")
            == len(matched) + len(pending) + len(missed),
            "schedule occurrence equation is inconsistent")
        _bs_require(set(matched) <= observed_workflow_ids and set(unmatched) <= observed_workflow_ids,
                    "schedule workflow IDs must be observed lane IDs")
        _bs_require(not (set(matched) & set(unmatched)) and not (all_matched & set(matched)),
                    "matched workflow IDs must be disjoint")
        all_matched.update(matched)
        missed_total += len(missed)
        expected_pending.extend(
            {"schedule_name": schedule["name"], "boundary": boundary} for boundary in pending)

    totals = _bs_exact_keys(payload["schedule_totals"], _BS_TOTALS_KEYS, "schedule_totals shape")
    for key in ("configured", "observed", "paused", "drifted", "pending", "missed"):
        _bs_count(totals[key], f"schedule_totals.{key}")
    source_names = _bs_sorted_unique(totals["source_names"], "schedule_totals.source_names", _BS_NAME_RE)
    _bs_require(isinstance(totals["live_keys"], list)
                and all(isinstance(item, str) and item for item in totals["live_keys"]),
                "schedule_totals.live_keys")
    paused_names = _bs_sorted_unique(totals["paused_names"], "schedule_totals.paused_names", _BS_NAME_RE)
    drifted_names = totals["drifted_names"]
    _bs_require(isinstance(drifted_names, list)
                and all(isinstance(item, str) and item for item in drifted_names),
                "schedule_totals.drifted_names")
    _bs_require(totals["configured"] == len(source_names) and source_names == names,
                "schedule_totals.configured must mirror the schedule list")
    _bs_require(totals["observed"] == len(totals["live_keys"]),
                "schedule_totals.observed must equal live_keys length")
    expected_paused = [item["name"] for item in schedules if item["paused"]]
    _bs_require(totals["paused"] == len(paused_names) and paused_names == expected_paused,
                "schedule_totals.paused must mirror paused schedules")
    expected_drifted = {item["name"] for item in schedules if item["drift"]}
    _bs_require(totals["drifted"] == len(drifted_names)
                and expected_drifted <= set(drifted_names),
                "schedule_totals.drifted must identify schedule drift")

    pending_occurrences = payload["pending_occurrences"]
    _bs_require(isinstance(pending_occurrences, list), "pending_occurrences must be a list")
    for item in pending_occurrences:
        _bs_exact_keys(item, frozenset({"schedule_name", "boundary"}), "pending occurrence shape")
        _bs_str(item["schedule_name"], "pending schedule_name", pattern=_BS_NAME_RE)
        _bs_ts(item["boundary"], "pending boundary")
    expected_pending.sort(key=lambda item: (item["schedule_name"], item["boundary"]))
    _bs_require(pending_occurrences == expected_pending,
                "pending_occurrences must equal schedule pending boundaries")
    _bs_require(totals["pending"] == len(pending_occurrences)
                and payload["pending_grace_count"] == len(pending_occurrences),
                "pending counts must equal pending_occurrences length")
    _bs_require(totals["missed"] == missed_total, "schedule_totals.missed must equal missed boundaries")
    return missed_total


def _validate_board_summary(payload: dict[str, Any], slug: str) -> None:
    """Enforce the generic v2 contract; raise ``_SummaryInvalid`` on any violation."""
    _bs_exact_keys(payload, _BS_TOP_KEYS, "top-level shape")
    version = payload["schema_version"]
    _bs_require(isinstance(version, int) and not isinstance(version, bool) and version == 2,
                "unsupported schema_version")
    _bs_str(payload["board_slug"], "board_slug", pattern=_BS_BOARD_SLUG_RE)
    _bs_require(payload["board_slug"] == slug, "board_slug mismatch")
    generated = _bs_ts(payload["generated_at"], "generated_at")
    expires = _bs_ts(payload["expires_at"], "expires_at")
    _bs_ts(payload["clean_since"], "clean_since")
    staleness = _bs_exact_keys(payload["staleness"], frozenset({"fresh_for_seconds"}), "staleness shape")
    fresh = staleness["fresh_for_seconds"]
    _bs_require(_bs_int(fresh, "fresh_for_seconds") >= 1, "fresh_for_seconds must be positive")
    _bs_require(int((expires - generated).total_seconds()) == fresh,
                "fresh_for_seconds must equal expires_at - generated_at")
    phase = payload["phase"]
    status_value = payload["status"]
    _bs_require(phase in _BS_PHASES, "unknown phase")
    _bs_require(status_value in _BS_STATUSES, "unknown status")
    completed = _bs_bool(payload["completed"], "completed")
    authorized = _bs_bool(payload["cron_removal_authorized"], "cron_removal_authorized")
    proof = _bs_str(payload["pre_removal_proof_digest"], "pre_removal_proof_digest",
                    pattern=_BS_OPT_HEX64_RE)
    clean_elapsed = _bs_count(payload["clean_elapsed_seconds"], "clean_elapsed_seconds")
    required_clean = _bs_count(payload["required_clean_seconds"], "required_clean_seconds")
    _bs_require(
        (phase == "canary" and 30 <= required_clean <= 600)
        or (phase == "pre_removal" and required_clean == 7200)
        or (phase == "post_removal" and required_clean == 0),
        "required_clean_seconds contradicts the phase",
    )
    engine = _bs_exact_keys(payload["engine"], frozenset({"healthy"}), "engine shape")
    engine_healthy = _bs_bool(engine["healthy"], "engine.healthy")
    _bs_count(payload["cron_jobs_remaining"], "cron_jobs_remaining")

    findings = payload["findings"]
    _bs_require(isinstance(findings, list), "findings must be a list")
    for finding in findings:
        _bs_exact_keys(finding, _BS_FINDING_KEYS, "finding shape")
        _bs_require(finding["severity"] == "error", "finding severity")
        _bs_str(finding["code"], "finding code", allow_empty=False)
        _bs_str(finding["message"], "finding message", allow_empty=False)
        _bs_require(isinstance(finding["evidence"], dict), "finding evidence")

    # Phase/status/authorization contradictions.
    _bs_require(completed == (phase == "post_removal" and status_value == "completed"),
                "completed must identify only a completed post_removal phase")
    _bs_require(authorized == (phase == "pre_removal" and status_value == "removal_ready"),
                "cron_removal_authorized must identify only removal_ready pre_removal")
    _bs_require((phase == "post_removal") == bool(proof),
                "pre_removal_proof_digest must be present only in post_removal")
    _bs_require(status_value != "canary_passed" or phase == "canary", "canary_passed requires canary")
    _bs_require(status_value != "removal_ready" or phase == "pre_removal",
                "removal_ready requires pre_removal")
    _bs_require(status_value != "completed" or phase == "post_removal",
                "completed status requires post_removal")
    _bs_require(status_value != "failed" or bool(findings), "failed status requires findings")
    _bs_require(status_value != "monitoring" or not findings,
                "monitoring status cannot carry findings")

    global_counts = _bs_exact_keys(
        payload["global"], frozenset(_BS_GLOBAL_COUNT_KEYS) | {"conductor_healthy"}, "global shape")
    for key in _BS_GLOBAL_COUNT_KEYS:
        _bs_count(global_counts[key], f"global.{key}")
    _bs_require(_bs_bool(global_counts["conductor_healthy"], "global.conductor_healthy")
                == engine["healthy"], "global.conductor_healthy must equal engine.healthy")

    cron_ids = _bs_validate_cron_inventory(payload)
    lane_sums, observed_workflow_ids = _bs_validate_lanes(payload)
    for key, expected in lane_sums.items():
        _bs_require(global_counts[key] == expected, f"global.{key} must equal the lane sum")
    _bs_validate_blocked_cards(payload)
    missed_total = _bs_validate_schedules(payload, observed_workflow_ids)

    _bs_require(global_counts["cron_jobs_remaining"] == payload["cron_jobs_remaining"],
                "global.cron_jobs_remaining must equal cron_jobs_remaining")
    _bs_require(global_counts["schedule_count"] == len(payload["schedules"]),
                "global.schedule_count must equal schedules length")
    _bs_require(global_counts["schedule_paused_count"] == payload["schedule_totals"]["paused"],
                "global.schedule_paused_count must equal schedule_totals.paused")
    _bs_require(global_counts["pending_grace_count"] == payload["pending_grace_count"],
                "global.pending_grace_count must equal pending_grace_count")
    _bs_require(global_counts["missed_boundary_count"] == missed_total,
                "global.missed_boundary_count must equal missed boundaries")
    _bs_require(global_counts["findings_count"] == len(findings),
                "global.findings_count must equal len(findings)")

    barrier = _bs_exact_keys(payload["barrier"], _BS_BARRIER_KEYS, "barrier shape")
    passed = _bs_bool(barrier["passed"], "barrier.passed")
    _bs_str(barrier["inventory_digest"], "barrier.inventory_digest", pattern=_BS_OPT_HEX64_RE)
    _bs_str(barrier["workflow_digest"], "barrier.workflow_digest", pattern=_BS_OPT_HEX64_RE)
    _bs_count(barrier["waves"], "barrier.waves")
    if passed:
        _bs_require(_BS_HEX64_RE.fullmatch(barrier["inventory_digest"]) is not None
                    and _BS_HEX64_RE.fullmatch(barrier["workflow_digest"]) is not None
                    and barrier["waves"] >= 5,
                    "passed barrier requires five waves and digest evidence")

    cron = payload["cron_inventory"]
    expected_phase_met = (
        cron_ids["live_ids"] == list(_BS_EXPECTED_LEGACY_IDS)
        and not cron_ids["live_enabled_ids"]
        and not cron_ids["live_non_paused_ids"]
        if phase in {"canary", "pre_removal"}
        else cron["source_observed"]
        and not cron_ids["live_ids"]
        and not cron_ids["source_ids"]
    )
    _bs_require(
        cron["phase_expectation_met"] == expected_phase_met,
        "phase_expectation_met contradicts phase inventory",
    )
    _bs_require(
        cron["phase_expectation_met"] or (status_value == "failed" and bool(findings)),
        "false phase_expectation_met requires failed status and findings",
    )
    totals = payload["schedule_totals"]
    adverse_counts = (
        lane_sums["failed_count"],
        lane_sums["running_count"],
        lane_sums["paused_count"],
        lane_sums["contention_count"],
        lane_sums["duplicate_count"],
        lane_sums["stale_prerequisite_count"],
        global_counts["blocked_count"],
        payload["duplicate_blocked_signature_count"],
        len(payload["pending_occurrences"]),
        missed_total,
        totals["drifted"],
    )
    unmatched_scheduled = any(
        schedule["unmatched_workflow_ids"] for schedule in payload["schedules"]
    )
    success_terminal = status_value in {"canary_passed", "removal_ready", "completed"}
    _bs_require(
        not success_terminal
        or (
            not findings
            and totals["configured"] > 0
            and clean_elapsed >= required_clean
            and engine_healthy
            and not any(adverse_counts)
            and not unmatched_scheduled
            and passed
            and cron["phase_expectation_met"]
            and (phase == "canary" or totals["paused"] == 0)
        ),
        "terminal success contradicts clean-window, inventory, or barrier evidence",
    )

    # ``source`` is producer bookkeeping: validate schema patterns, never project it.
    source = _bs_exact_keys(payload["source"], _BS_SOURCE_KEYS, "source shape")
    _bs_str(
        source["api_base"],
        "source.api_base",
        pattern=_BS_API_BASE_RE,
        allow_empty=False,
    )
    for key in _BS_SOURCE_KEYS - {"api_base"}:
        _bs_str(
            source[key],
            f"source.{key}",
            pattern=_BS_ABSOLUTE_PATH_RE,
            allow_empty=False,
        )


def _board_summary_projection(payload: dict[str, Any], slug: str) -> dict[str, Any]:
    """Filtered public view: no producer ``source`` paths, no finding evidence,
    no schedule inputs/correlation ids — only display-safe scalars and counts."""
    expires = datetime.strptime(payload["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    cron = payload["cron_inventory"]
    totals = payload["schedule_totals"]
    barrier = payload["barrier"]
    return {
        "board": slug,
        "generated_at": payload["generated_at"],
        "expires_at": payload["expires_at"],
        "stale": datetime.now(timezone.utc) >= expires,
        "phase": payload["phase"],
        "status": payload["status"],
        "completed": payload["completed"],
        "cron_removal_authorized": payload["cron_removal_authorized"],
        "counts": dict(payload["global"]),
        "cron": {
            "jobs_remaining": payload["cron_jobs_remaining"],
            "live_ids_digest": cron["live_ids_digest"],
            "expected_legacy_ids_digest": cron["expected_legacy_ids_digest"],
            "source_ids_digest": cron["source_ids_digest"],
            "source_observed": cron["source_observed"],
            "source_live_converged": cron["source_live_converged"],
            "phase_expectation_met": cron["phase_expectation_met"],
        },
        "lanes": [
            {
                "name": lane["name"],
                "running_count": lane["running_count"],
                "paused_count": lane["paused_count"],
                "failed_count": lane["failed_count"],
                "terminal_successes": lane["terminal_successes"],
                "contention_count": lane["contention_count"],
                "duplicate_count": lane["duplicate_count"],
                "stale_prerequisite_count": lane["stale_prerequisite_count"],
            }
            for lane in payload["lanes"]
        ],
        "schedules": [
            {
                "name": schedule["name"],
                "paused": schedule["paused"],
                "cron_expression": schedule["cron_expression"],
                "zone": schedule["zone"],
                "observed_count": schedule["observed_count"],
                "missed_count": len(schedule["missed_boundaries"]),
                "drifted": bool(schedule["drift"]),
            }
            for schedule in payload["schedules"]
        ],
        "schedule_totals": {
            "configured": totals["configured"],
            "observed": totals["observed"],
            "paused": totals["paused"],
            "drifted": totals["drifted"],
            "pending": totals["pending"],
            "missed": totals["missed"],
        },
        "barrier": {
            "passed": barrier["passed"],
            "waves": barrier["waves"],
            "inventory_digest": barrier["inventory_digest"],
            "workflow_digest": barrier["workflow_digest"],
        },
        "findings_count": payload["global"]["findings_count"],
        "findings": [
            {
                "severity": finding["severity"],
                "code": finding["code"],
                "message": finding["message"][:300],
            }
            for finding in payload["findings"]
        ],
    }


@router.get("/board-summary")
def get_board_summary(board: str = Query(..., description="Kanban board slug")):
    """Serve the published summary for ``board``. 404 = none published;
    503 = a summary file exists but is unsafe or violates the v2 contract."""
    slug = _normalize_slug_or_400(board)
    if not slug:
        raise HTTPException(status_code=400, detail="board slug required")
    payload = _parse_board_summary(_read_board_summary_bytes(slug))
    try:
        _validate_board_summary(payload, slug)
        return _board_summary_projection(payload, slug)
    except HTTPException:
        raise
    except Exception:
        # Any structural surprise while projecting == invalid file.
        raise _board_summary_unavailable()


# --- WebSocket: /events?since=<event_id>&board=<slug> ------------------------

# Event tail poll interval: WAL + 300 ms polling is the simplest robust approach (negligible CPU).
_EVENT_POLL_SECONDS = 0.3


def _int_param(ws: WebSocket, name: str) -> int:
    try:
        return int(ws.query_params.get(name, "0"))
    except ValueError:
        return 0


def _ws_board(raw: Optional[str]) -> Optional[str]:
    try:
        return kanban_db._normalize_board_slug(raw) if raw else None
    except ValueError:
        return None


class _EventTail:
    """Per-socket ``task_events`` tailer. One SQLite connection, used/closed only on a
    dedicated single-thread executor (connections are thread-affine); reusing it avoids
    churning WAL/SHM sidecars while an idle dashboard polls."""

    def __init__(self, board: Optional[str]) -> None:
        self._board = board
        self._conn: Optional[sqlite3.Connection] = None
        self._executor: Optional[ThreadPoolExecutor] = None

    def _fetch(self, cursor: int) -> tuple[int, list[dict]]:
        if self._conn is None:
            self._conn = kbc.connect(board=self._board)
        rows = self._conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT 200",
            (cursor,)).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            out.append({**dict(r), "payload": payload})
        return (rows[-1]["id"] if rows else cursor), out

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def poll(self, cursor: int) -> tuple[int, list[dict]]:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kanban-events")
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._fetch, cursor)

    async def shutdown(self) -> None:
        if self._executor is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._close)
        except Exception as exc:
            log.warning("Kanban event stream connection cleanup failed: %s", exc)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


@router.websocket("/events")
async def stream_events(ws: WebSocket):
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    # Board is pinned at the handshake; the UI opens a new WS on board change
    # rather than reconciling two cursors mid-stream.
    tail = _EventTail(_ws_board(ws.query_params.get("board")))
    cursor = _int_param(ws, "since")
    try:
        while True:
            # Race receive() against the poll interval so a disconnect is detected even when no
            # events flow (else idle boards leak poll tasks). Other client messages are ignored.
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=_EVENT_POLL_SECONDS)
                if msg["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass  # no client message — poll the DB
            cursor, events = await tail.poll(cursor)
            if events:
                await ws.send_json({"events": events, "cursor": cursor})
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return  # normal shutdown; CancelledError is a BaseException the handler below wouldn't quiet
    except Exception as exc:  # never crash the dashboard worker
        log.warning("Kanban event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        await tail.shutdown()
