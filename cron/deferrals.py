"""Durable deferred obligations for cron logical occurrences.

When a pre-script reports TRANSIENT_DEFER, the scheduler must not consume
the job's logical occurrence (a contended daily 09:00 run still owes the
user its 09:00 outcome). This module persists that debt:

* one obligation per job + logical occurrence — a retry fire attaches to
  the existing pending row (its wall-clock occurrence differs, the logical
  one does not), so obligations can never duplicate;
* exactly ``MAX_DEFER_RETRIES`` persisted retries — the next consecutive
  defer flips the obligation to ``exhausted`` and the scheduler routes it
  through the normal permanent-failure machinery;
* obligations resolve (``completed`` / ``permanent`` / ``skipped``) only
  at a real terminal outcome, which is also the only point the normal
  schedule advances.

Obligations live in the SAME ``cron/executions.db`` as the executions
ledger and incidents (one durable cron store per profile); the schema is
lazily created on connect. State validity lives in ``DEFERRAL_STATES``
(Python), not a SQLite CHECK, so future slices can add states without a
table rebuild (mirrors ``cron.incidents``).
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

from cron.outcomes import (
    DEFAULT_RETRY_AFTER_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    MIN_RETRY_AFTER_SECONDS,
)

# Optional test override (mirrors ``cron.executions.EXECUTIONS_FILE``).
EXECUTIONS_FILE: Optional[Path] = None

DEFERRAL_STATES = ("pending", "completed", "permanent", "skipped", "exhausted")
# One persisted retry per obligation. A bounded wait is an availability
# feature, not a correctness one — correctness is the durable row.
MAX_DEFER_RETRIES = 1
_MAX_REASON_CHARS = 500

_lock = threading.RLock()


def _db_path() -> Path:
    """Resolve the shared cron DB path (same precedence as cron.incidents)."""
    try:
        from cron.executions import EXECUTIONS_FILE as _EXEC_OVERRIDE

        if _EXEC_OVERRIDE is not None:
            return Path(_EXEC_OVERRIDE)
    except Exception:
        pass
    if EXECUTIONS_FILE is not None:
        return Path(EXECUTIONS_FILE)
    return get_hermes_home().resolve() / "cron" / "executions.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(conn, db_label="cron/executions.db")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cron_deferrals (
             id             TEXT PRIMARY KEY,
             job_id         TEXT NOT NULL,
             occurrence_key TEXT NOT NULL,
             state          TEXT NOT NULL,
             reason         TEXT NOT NULL DEFAULT '',
             retry_at       TEXT NOT NULL,
             attempts       INTEGER NOT NULL DEFAULT 0,
             created_at     TEXT NOT NULL,
             updated_at     TEXT NOT NULL,
             resolved_at    TEXT,
             UNIQUE(job_id, occurrence_key)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_deferrals_job_state "
        "ON cron_deferrals(job_id, state)"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    Mirrors ``cron.executions._transaction``: schema init runs inside the
    ``try`` so a PRAGMA/DDL failure after a successful ``connect()`` still
    closes the connection instead of leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _clamp_retry_after(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    return max(MIN_RETRY_AFTER_SECONDS, min(MAX_RETRY_AFTER_SECONDS, seconds))


def record_defer(
    job_id: str,
    occurrence_key: str,
    *,
    reason: str = "",
    retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS,
) -> Dict[str, Any]:
    """Persist (or escalate) the durable retry obligation for one defer.

    A pending obligation for the job — regardless of the wall-clock
    occurrence the retry fired under — is the SAME logical debt: attempts
    increments, and once the retry budget is spent the row flips to
    ``exhausted`` so the caller can fail the occurrence permanently.
    Returns the resulting row, plus a ``prior`` key holding the exact row
    this write replaced (``None`` for a fresh insert) so
    :func:`rollback_defer` can compensate deterministically when the fenced
    job-state handoff fails after this persists.
    """
    job_id = str(job_id or "")
    reason = str(reason or "")[:_MAX_REASON_CHARS]
    now = _hermes_now()
    retry_at = (
        now + timedelta(seconds=_clamp_retry_after(retry_after_seconds))
    ).isoformat()
    now_iso = now.isoformat()

    with _transaction() as conn:
        row = conn.execute(
            """SELECT * FROM cron_deferrals
               WHERE job_id=? AND state='pending'
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        if row is not None:
            attempts = int(row["attempts"]) + 1
            state = "exhausted" if attempts >= MAX_DEFER_RETRIES else "pending"
            conn.execute(
                """UPDATE cron_deferrals
                   SET attempts=?, state=?, reason=?, retry_at=?,
                       updated_at=?, resolved_at=?
                   WHERE id=?""",
                (attempts, state, reason, retry_at, now_iso,
                 now_iso if state == "exhausted" else None, row["id"]),
            )
            refreshed = conn.execute(
                "SELECT * FROM cron_deferrals WHERE id=?", (row["id"],)
            ).fetchone()
            if refreshed is None:
                raise RuntimeError(
                    "cron_deferrals row vanished inside its own transaction"
                )
            return dict(refreshed, prior=dict(row))

        prior_row = conn.execute(
            """SELECT * FROM cron_deferrals
               WHERE job_id=? AND occurrence_key=?""",
            (job_id, str(occurrence_key)),
        ).fetchone()
        deferral_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO cron_deferrals
               (id, job_id, occurrence_key, state, reason, retry_at,
                attempts, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, 0, ?, ?)
               ON CONFLICT(job_id, occurrence_key) DO UPDATE SET
                 state='pending', attempts=attempts+1, reason=excluded.reason,
                 retry_at=excluded.retry_at, updated_at=excluded.updated_at,
                 resolved_at=NULL""",
            (deferral_id, job_id, str(occurrence_key), reason, retry_at,
             now_iso, now_iso),
        )
        refreshed = conn.execute(
            """SELECT * FROM cron_deferrals
               WHERE job_id=? AND occurrence_key=?""",
            (job_id, str(occurrence_key)),
        ).fetchone()
        if refreshed is None:
            raise RuntimeError(
                "cron_deferrals row vanished inside its own transaction"
            )
        return dict(
            refreshed, prior=dict(prior_row) if prior_row is not None else None
        )


def rollback_defer(obligation: Dict[str, Any]) -> bool:
    """Deterministically undo ONE :func:`record_defer` write.

    Called when the fenced job-state handoff fails AFTER the obligation
    persisted (fire-claim ownership lost): a fresh row is deleted, an
    escalated row is restored to the exact ``prior`` snapshot the write
    replaced. Fenced against replacements — the undo applies only while
    the row still matches what that ``record_defer`` wrote, so a
    replacement owner's newer obligation is never clobbered (and can never
    be exhausted by a stale owner's leftovers). Returns whether the
    compensation applied.
    """
    obligation_id = str(obligation.get("id") or "")
    prior = obligation.get("prior")
    with _transaction() as conn:
        current = conn.execute(
            "SELECT * FROM cron_deferrals WHERE id=?", (obligation_id,)
        ).fetchone()
        if current is None:
            return False
        if (
            current["state"] != obligation.get("state")
            or int(current["attempts"]) != int(obligation.get("attempts") or 0)
            or current["updated_at"] != obligation.get("updated_at")
        ):
            # The row moved since our write — it belongs to a replacement.
            return False
        if prior is None:
            conn.execute(
                "DELETE FROM cron_deferrals WHERE id=?", (obligation_id,)
            )
            return True
        conn.execute(
            """UPDATE cron_deferrals
               SET state=?, reason=?, retry_at=?, attempts=?,
                   updated_at=?, resolved_at=?
               WHERE id=?""",
            (prior["state"], prior["reason"], prior["retry_at"],
             int(prior["attempts"]), prior["updated_at"],
             prior["resolved_at"], obligation_id),
        )
        return True


def pending_deferral(job_id: str) -> Optional[Dict[str, Any]]:
    """Return the job's newest pending obligation, if any."""
    with _transaction() as conn:
        row = conn.execute(
            """SELECT * FROM cron_deferrals
               WHERE job_id=? AND state='pending'
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (str(job_id),),
        ).fetchone()
    return _record(row)


def resolve_pending(
    job_id: str, state: str, *, occurrence_key: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Resolve the job's pending obligation at a real terminal outcome.

    ``state`` must be one of the resolution states (``completed`` /
    ``permanent`` / ``skipped``). No-op (``None``) when nothing is pending —
    every terminal path may call this unconditionally.
    """
    if state not in ("completed", "permanent", "skipped"):
        return None
    now_iso = _hermes_now().isoformat()
    with _transaction() as conn:
        clauses = ["job_id=?", "state='pending'"]
        params: List[Any] = [str(job_id)]
        if occurrence_key is not None:
            clauses.append("occurrence_key=?")
            params.append(str(occurrence_key))
        where = " AND ".join(clauses)
        row = conn.execute(
            f"SELECT id FROM cron_deferrals WHERE {where} "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """UPDATE cron_deferrals
               SET state=?, resolved_at=?, updated_at=? WHERE id=?""",
            (state, now_iso, now_iso, row["id"]),
        )
        refreshed = conn.execute(
            "SELECT * FROM cron_deferrals WHERE id=?", (row["id"],)
        ).fetchone()
    return _record(refreshed)


def list_deferrals(
    job_id: Optional[str] = None, state: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return obligations, newest first, optionally filtered."""
    if state is not None and state not in DEFERRAL_STATES:
        return []
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if state is not None:
        clauses.append("state=?")
        params.append(state)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM cron_deferrals" + where
            + " ORDER BY updated_at DESC, id DESC",
            params,
        ).fetchall()
    return [dict(row) for row in rows]
