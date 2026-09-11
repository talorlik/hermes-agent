"""Transactional cron delivery outbox — at-least-once platform delivery.

The scheduler used to fire-and-forget its platform sends: a crash between
"agent produced a result" and "telegram accepted the message" silently lost
the delivery. The outbox makes delivery durable:

* ``enqueue_with_intent`` persists the pending delivery AND the execution's
  terminal intent in ONE transaction, BEFORE any send is attempted — a
  crash mid-send leaves a queryable pending row plus the intent evidence;
* every attempt lands in ``cron_outbox_attempts`` (per-attempt history);
* a failed attempt keeps the row pending (retried before new work on the
  next tick), mints/refreshes the delivery incident in the SAME
  transaction, and drops the content into a durable local fallback file so
  a broken primary channel never means silent loss — no external
  credentials involved;
* after ``MAX_OUTBOX_ATTEMPTS`` failures the row is ``abandoned`` (still
  queryable; the incident stays open).

At-least-once, deliberately: a crash after a successful send but before
the delivered mark re-sends on retry. Duplicates over silent loss.

Rows live in the SAME ``cron/executions.db`` as the executions ledger,
incidents, and deferrals (one durable cron store per profile).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# Optional test override (mirrors ``cron.executions.EXECUTIONS_FILE``).
EXECUTIONS_FILE: Optional[Path] = None

OUTBOX_STATES = ("pending", "delivered", "abandoned")
MAX_OUTBOX_ATTEMPTS = 5
_MAX_ERROR_CHARS = 500

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
        """CREATE TABLE IF NOT EXISTS cron_outbox (
             id           TEXT PRIMARY KEY,
             execution_id TEXT,
             job_id       TEXT NOT NULL,
             target       TEXT NOT NULL,
             content      TEXT NOT NULL,
             state        TEXT NOT NULL,
             attempts     INTEGER NOT NULL DEFAULT 0,
             created_at   TEXT NOT NULL,
             updated_at   TEXT NOT NULL,
             last_error   TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cron_outbox_attempts (
             id         INTEGER PRIMARY KEY AUTOINCREMENT,
             outbox_id  TEXT NOT NULL,
             attempt_no INTEGER NOT NULL,
             at         TEXT NOT NULL,
             status     TEXT NOT NULL,
             error      TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_outbox_state "
        "ON cron_outbox(state, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_outbox_attempts_outbox "
        "ON cron_outbox_attempts(outbox_id, attempt_no)"
    )
    # Atomic co-writes touch the incident table on this connection; make
    # sure its schema exists here too (idempotent CREATEs).
    from cron.incidents import _initialize_schema as _init_incidents

    _init_incidents(conn)


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


def _sanitize_error(error: Optional[str]) -> Optional[str]:
    if error is None:
        return None
    text = str(error)
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(
            text, force=True, redact_url_credentials=True
        )
    except Exception:
        return "[REDACTED - delivery error unavailable]"
    return text[:_MAX_ERROR_CHARS]


def enqueue_with_intent(
    *,
    execution_id: Optional[str],
    job_id: str,
    target: str,
    content: str,
    intent_success: bool,
    intent_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist the pending delivery plus the execution's terminal intent.

    ONE transaction, BEFORE any send: the execution row's ``outcome``
    column is stamped with what the scheduler intends to record
    (``completed``/``failed``) while its status stays nonterminal, and the
    outbox row is created pending. A crash anywhere after this commit
    leaves both the evidence and the retryable work.
    """
    outbox_id = uuid.uuid4().hex
    now_iso = _hermes_now().isoformat()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO cron_outbox
               (id, execution_id, job_id, target, content, state, attempts,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (outbox_id, execution_id, str(job_id), str(target), str(content),
             now_iso, now_iso),
        )
        if execution_id:
            conn.execute(
                """UPDATE executions
                   SET outcome=?, error=COALESCE(?, error)
                   WHERE id=? AND status IN ('claimed','running')""",
                ("completed" if intent_success else "failed",
                 _sanitize_error(intent_error), execution_id),
            )
        row = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "cron_outbox row vanished inside its own transaction"
            )
    return dict(row)


def _fallback_dir() -> Path:
    return get_hermes_home().resolve() / "cron" / "failed_deliveries"


def _write_local_fallback(row: Dict[str, Any], error: Optional[str]) -> None:
    """Durable local fallback for a failed primary channel.

    A plain file under the profile home — always writable with zero
    external credentials — so a broken adapter never means the content is
    gone. Overwrite-idempotent per outbox row.
    """
    try:
        directory = _fallback_dir() / str(row.get("job_id") or "unknown")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{row['id']}.md"
        path.write_text(
            f"# Undelivered cron message\n\n"
            f"- **Job:** {row.get('job_id')}\n"
            f"- **Target:** {row.get('target')}\n"
            f"- **Attempts:** {row.get('attempts')}\n"
            f"- **Last error:** {error or 'unknown'}\n\n"
            "---\n\n"
            f"{row.get('content') or ''}\n",
            encoding="utf-8",
        )
    except Exception:
        logger.warning(
            "Outbox: failed writing local fallback for %r",
            row.get("id"), exc_info=True,
        )


def record_attempt(
    outbox_id: str,
    *,
    status: str,
    error: Optional[str] = None,
    abandon: bool = False,
) -> Optional[Dict[str, Any]]:
    """Record one delivery attempt; incident write is in the same commit.

    ``delivered`` closes the row. A failure keeps it pending (or abandons
    it past ``MAX_OUTBOX_ATTEMPTS`` / on ``abandon=True``), refreshes the
    delivery incident atomically, and mirrors the content to the local
    fallback so the operator always has a durable copy.
    """
    from cron.incidents import record_recovery_in, upsert_incident_in

    clean_error = _sanitize_error(error)
    now_iso = _hermes_now().isoformat()
    delivered = status == "delivered"
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (outbox_id,)
        ).fetchone()
        if row is None:
            return None
        attempts = int(row["attempts"]) + 1
        if delivered:
            state = "delivered"
        elif abandon or attempts >= MAX_OUTBOX_ATTEMPTS:
            state = "abandoned"
        else:
            state = "pending"
        conn.execute(
            """UPDATE cron_outbox
               SET attempts=?, state=?, updated_at=?, last_error=?
               WHERE id=?""",
            (attempts, state, now_iso, clean_error, outbox_id),
        )
        conn.execute(
            """INSERT INTO cron_outbox_attempts
               (outbox_id, attempt_no, at, status, error)
               VALUES (?, ?, ?, ?, ?)""",
            (outbox_id, attempts, now_iso,
             "delivered" if delivered else "failed", clean_error),
        )
        if not delivered:
            # Delivery-only failure must be incident-visible; same commit
            # as the attempt so the two can never disagree.
            upsert_incident_in(
                conn,
                str(row["job_id"]),
                f"delivery failed to {row['target']}: {clean_error or 'unknown error'}",
                failure_type="delivery",
            )
        else:
            # The channel healed: recover the job's DELIVERY incidents in
            # the same commit as the delivered attempt. Execution incidents
            # are a different category and are never touched from here.
            record_recovery_in(conn, str(row["job_id"]), category="delivery")
        refreshed = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (outbox_id,)
        ).fetchone()
    result = _record(refreshed)
    if result is not None and not delivered:
        _write_local_fallback(result, clean_error)
    return result


def pending_outbox(limit: int = 10) -> List[Dict[str, Any]]:
    """Oldest-first pending deliveries — the retry backlog."""
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM cron_outbox WHERE state='pending'
               ORDER BY created_at ASC, id ASC LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_outbox(
    job_id: Optional[str] = None, state: Optional[str] = None
) -> List[Dict[str, Any]]:
    if state is not None and state not in OUTBOX_STATES:
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
            "SELECT * FROM cron_outbox" + where
            + " ORDER BY created_at DESC, id DESC",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_attempts(outbox_id: str) -> List[Dict[str, Any]]:
    """Full per-attempt history for one outbox entry, oldest first."""
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM cron_outbox_attempts WHERE outbox_id=?
               ORDER BY attempt_no ASC, id ASC""",
            (str(outbox_id),),
        ).fetchall()
    return [dict(row) for row in rows]
