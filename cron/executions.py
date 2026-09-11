"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue. Interrupted attempts
become ``unknown`` only after their exact owner process is proved gone. Terminal states are
immutable.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from cron.ledger import ledger_transaction, open_ledger, prepare_ledger
from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

# Optional test override. Production resolves the path at transaction time so dashboard operations
# that temporarily enter another profile cannot leak that profile's records into the import-time
# home.
EXECUTIONS_FILE: Optional[Path] = None
# Retention is PER JOB (a chatty per-minute job must never evict another
# job's weekly evidence) with a hard 30-day floor: terminal rows younger
# than RETENTION_DAYS are never pruned regardless of per-job volume.
MAX_TERMINAL_EXECUTIONS = 1000
HANDOFF_ADOPTION_GRACE_SECONDS = 30.0
RETENTION_DAYS = 30
_TERMINAL_STATES = ("completed", "failed", "unknown", "deferred")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex

# Additive columns introduced by the durable-outcomes upgrade. Kept nullable
# (or defaulted) so pre-upgrade writers that INSERT only the legacy columns
# keep working, and pre-upgrade readers that SELECT legacy columns see an
# unchanged shape.
_ADDITIVE_COLUMNS = (
    ("handoff_pending", "INTEGER NOT NULL DEFAULT 0"),
    ("handoff_started_at", "REAL"),
    ("outcome", "TEXT"),
    ("occurrence_key", "TEXT"),
    ("retry_at", "TEXT"),
    ("delivery_target", "TEXT"),
    ("delivery_status", "TEXT"),
    ("delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("delivery_error", "TEXT"),
    ("detached_run_id", "TEXT"),
    ("detached_status", "TEXT"),
    ("detached_worker", "TEXT"),
    ("lease_expires_at", "TEXT"),
)

_CREATE_EXECUTIONS_SQL = """CREATE TABLE IF NOT EXISTS executions (
     id TEXT PRIMARY KEY,
     job_id TEXT NOT NULL,
     source TEXT NOT NULL,
     process_id TEXT NOT NULL,
     pid INTEGER NOT NULL,
     process_started_at INTEGER,
     status TEXT NOT NULL CHECK(status IN
       ('claimed','running','completed','failed','unknown','deferred')),
     handoff_pending INTEGER NOT NULL DEFAULT 0,
     handoff_started_at REAL,
     claimed_at TEXT NOT NULL,
     started_at TEXT,
     finished_at TEXT,
     error TEXT,
     outcome TEXT,
     occurrence_key TEXT,
     retry_at TEXT,
     delivery_target TEXT,
     delivery_status TEXT,
     delivery_attempts INTEGER NOT NULL DEFAULT 0,
     delivery_error TEXT,
     detached_run_id TEXT,
     detached_status TEXT,
     detached_worker TEXT,
     lease_expires_at TEXT
   )"""


# --- executions ledger --------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    return open_ledger(EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db"))


def _initialize_schema(conn: sqlite3.Connection) -> None:
    prepare_ledger(conn, db_label="cron/executions.db")
    from hermes_cli.sqlite_util import add_column_if_missing

    conn.execute(_CREATE_EXECUTIONS_SQL)
    _migrate_schema_unlocked(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    add_column_if_missing(conn, "executions", "delivery_outcome", "delivery_outcome TEXT")
    add_column_if_missing(conn, "executions", "scheduled_instant", "scheduled_instant TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_occurrence "
        "ON executions(job_id, scheduled_instant) WHERE status='completed'"
    )


def _migrate_schema_unlocked(conn: sqlite3.Connection) -> None:
    """Bring a pre-upgrade executions table up to the current schema.

    Non-destructive by construction: every legacy row and legacy column is
    copied verbatim. Two shapes exist in the field:

    * a table whose CHECK constraint predates the ``deferred`` state — the
      constraint text cannot be altered, so the table is rebuilt in place
      (rename → create → copy → drop) inside the caller's transaction;
    * a current-CHECK table that merely lacks newer additive columns —
      plain ``ALTER TABLE ADD COLUMN``.
    """
    # Crash recovery: schema init runs in autocommit, so a process that died
    # between the rebuild's rename and drop leaves the legacy table behind
    # while ``CREATE TABLE IF NOT EXISTS`` has already minted a fresh one.
    # Re-adopt those rows (PRIMARY KEY dedupes a partially-copied batch)
    # before any other schema decision.
    leftover = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='executions_pre_outcomes'"
    ).fetchone()
    if leftover is not None:
        legacy_cols = ", ".join(
            r[1]
            for r in conn.execute("PRAGMA table_info(executions_pre_outcomes)")
        )
        conn.execute(
            f"INSERT OR IGNORE INTO executions ({legacy_cols}) "
            f"SELECT {legacy_cols} FROM executions_pre_outcomes"
        )
        conn.execute("DROP TABLE executions_pre_outcomes")
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='executions'"
    ).fetchone()
    table_sql = str(row[0] if row else "")
    if "'deferred'" not in table_sql:
        conn.execute("ALTER TABLE executions RENAME TO executions_pre_outcomes")
        conn.execute(_CREATE_EXECUTIONS_SQL)
        legacy_cols = ", ".join(
            r[1]
            for r in conn.execute("PRAGMA table_info(executions_pre_outcomes)")
        )
        conn.execute(
            f"INSERT INTO executions ({legacy_cols}) "
            f"SELECT {legacy_cols} FROM executions_pre_outcomes"
        )
        conn.execute("DROP TABLE executions_pre_outcomes")
        return
    existing = {r[1] for r in conn.execute("PRAGMA table_info(executions)")}
    for name, ddl in _ADDITIVE_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE executions ADD COLUMN {name} {ddl}")


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    with ledger_transaction(_lock, _connect, _initialize_schema) as conn:
        yield conn


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _fetch(conn: sqlite3.Connection, execution_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    return _record(row)


def _emit_execution_state(
    record: Optional[Dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    """Prune terminal rows PER JOB, never inside the 30-day retention floor.

    The cap partitions by job so a high-frequency job's volume can only ever
    evict its own history — a weekly job's handful of rows survives any
    neighbour. Rows younger than ``RETENTION_DAYS`` are never deleted even
    beyond the cap; unparseable timestamps compare as NULL and are kept.
    """
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    cutoff = (_hermes_now() - timedelta(days=RETENTION_DAYS)).isoformat()
    states = ",".join(f"'{state}'" for state in _TERMINAL_STATES)
    conn.execute(
        f"""DELETE FROM executions WHERE id IN (
              SELECT id FROM (
                SELECT id, claimed_at, ROW_NUMBER() OVER (
                  PARTITION BY job_id
                  ORDER BY claimed_at DESC, id DESC
                ) AS row_rank
                FROM executions WHERE status IN ({states})
              ) WHERE row_rank > ? AND julianday(claimed_at) < julianday(?)
            )""",
        (limit, cutoff),
    )


def create_execution(
    job_id: str, *, source: str, scheduled_instant: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    from cron.occurrences import scheduled_instant as canonical_instant

    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, scheduled_instant)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now, canonical_instant(scheduled_instant)),
        )
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def set_execution_occurrence(execution_id: str, instant: Optional[str]) -> None:
    """Bind the store-claimed snapshot before a provider hands it to a worker."""
    from cron.occurrences import scheduled_instant

    with _transaction() as conn:
        cur = conn.execute(
            "UPDATE executions SET scheduled_instant=? WHERE id=? AND status='claimed' "
            "AND handoff_pending=0 AND process_id=? AND pid=?",
            (scheduled_instant(instant), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Cron occurrence could not be bound before dispatch")


def mark_execution_handoff_pending(execution_id: str) -> Optional[Dict[str, Any]]:
    """Fence restart recovery while an external worker is adopting a claim."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET handoff_pending=1, handoff_started_at=?
               WHERE id=? AND status='claimed'
                 AND process_id=? AND pid=?""",
            (time.time(), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def adopt_claimed_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Atomically transfer and start an attempt in its worker process.

    The dispatching gateway creates the row before spawning a restart-safe
    worker.  Adoption is the single ``claimed`` → ``running`` gate: only the
    winner may acknowledge ownership or run side effects.
    """
    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET process_id=?, pid=?, process_started_at=?,
                   status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=1""",
            (_PROCESS_ID, pid, process_started_at, now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=0
                 AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
    outcome: Optional[str] = None,
    occurrence_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten.

    ``outcome`` refines the binary status for the durable-outcomes contract
    (e.g. ``permanent_fail`` vs a retryable failure); it defaults to the
    status so legacy callers keep producing queryable rows.
    """
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, handoff_pending=0,
                   handoff_started_at=NULL, delivery_outcome=?, outcome=?,
                   occurrence_key=COALESCE(?, occurrence_key)
               WHERE id=? AND status IN ('claimed','running')
                 AND process_id=? AND pid=?""",
            (
                status,
                now,
                detail,
                delivery_outcome,
                outcome or status,
                occurrence_key,
                execution_id,
                _PROCESS_ID,
                os.getpid(),
            ),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _fetch(conn, execution_id)
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    return record


def defer_execution(
    execution_id: str, *, reason: str, occurrence_key: str, retry_at: str,
) -> Optional[Dict[str, Any]]:
    """Terminate one attempt as DEFERRED — neither success nor failure.

    The durable retry obligation itself lives in ``cron.deferrals``; this
    row is the per-attempt audit evidence. Terminal-once like every other
    terminal state: a deferred attempt cannot later be rewritten into a
    success/failure through ``finish_execution``.
    """
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='deferred', outcome='deferred',
                 finished_at=?, error=?, occurrence_key=?, retry_at=?
               WHERE id=? AND status IN ('claimed','running')""",
            (now, str(reason or "") or None, str(occurrence_key),
             str(retry_at), execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


def _sanitize_delivery_error(error: Optional[str]) -> Optional[str]:
    """Force-redact a delivery error before it persists in the ledger."""
    if error is None:
        return None
    text = str(error)
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(
            text, force=True, redact_url_credentials=True
        )
    except Exception:
        # Fail safe: never persist a string the redactor could not scrub.
        return "[REDACTED - delivery error unavailable]"
    return text[:500]


def record_delivery(
    execution_id: str, *, target: str, status: str,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Record one delivery attempt's target/status on its execution row.

    Increments the per-execution attempt counter; the full per-attempt
    history lives in the delivery outbox. Valid on terminal rows — delivery
    legitimately outlives the execution's terminal write (outbox retries).
    """
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET delivery_target=?, delivery_status=?,
                 delivery_attempts=delivery_attempts+1, delivery_error=?
               WHERE id=?""",
            (str(target), str(status), _sanitize_delivery_error(error),
             execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    return record


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at, detached_status
               FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["detached_status"] == "started":
                # Leased to a detached worker: the launcher's death says
                # nothing about the run. Lease expiry is judged by
                # reconcile_detached_runs, never by owner-process liveness.
                continue
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            handoff_started_at = row["handoff_started_at"]
            if (
                row["handoff_pending"]
                and handoff_started_at is not None
                and time.time() - float(handoff_started_at)
                < HANDOFF_ADOPTION_GRACE_SECONDS
            ):
                continue
            cur = conn.execute(
                """UPDATE executions
                   SET status='unknown', finished_at=?, error=?,
                       handoff_pending=0, handoff_started_at=NULL
                   WHERE id=? AND status=? AND process_id=? AND pid=?
                     AND handoff_pending=?
                     AND handoff_started_at IS ?""",
                (now,
                 "Scheduler restarted after this execution's owner exited before a durable "
                 "terminal state; whether side effects ran is unknown.",
                 row["id"], row["status"], row["process_id"], row["pid"],
                 row["handoff_pending"], row["handoff_started_at"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _fetch(conn, row["id"])
                if record is not None:
                    recovered.append(record)
        if changed:
            _prune_unlocked(conn)
    for record in recovered:
        _emit_execution_state(record)
    return changed


def register_detached_run(
    execution_id: str, *, run_id: Optional[str] = None,
    lease_seconds: int = 3600, worker: Optional[str] = None,
    occurrence_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """RUN_STARTED: lease this execution to a detached worker.

    One atomic UPDATE: correlation id, worker metadata, the logical
    occurrence, and the bounded lease land together. The execution stays
    NONTERMINAL (running) until the worker's terminal report is reconciled;
    restart recovery skips leased rows because the launcher process is
    expected to exit. Returns the record (with the minted run id) or None
    when the execution is already terminal.
    """
    run_id = str(run_id or uuid.uuid4().hex)
    lease_expires = (
        _hermes_now() + timedelta(seconds=int(lease_seconds))
    ).isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status='running',
                   started_at=COALESCE(started_at, ?),
                   detached_run_id=?, detached_status='started',
                   detached_worker=?,
                   occurrence_key=COALESCE(?, occurrence_key),
                   lease_expires_at=?
               WHERE id=? AND status IN ('claimed','running')""",
            (_hermes_now().isoformat(), run_id,
             str(worker) if worker else None,
             str(occurrence_key) if occurrence_key else None,
             lease_expires, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


def finalize_detached_run(
    run_id: str, *, success: bool, error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """RUN_SUCCEEDED / RUN_FAILED from the detached worker, keyed by run id.

    Writes only the detached terminal report — the originating execution
    stays nonterminal until :func:`reconcile_detached_runs` converts it, so
    the scheduler is the single writer of execution terminal states.
    """
    detached_status = "succeeded" if success else "failed"
    detail = None
    if not success:
        # Worker-supplied failure evidence is force-redacted before it
        # persists: detached workers commonly echo command lines and env
        # fragments that can carry credentials.
        detail = _sanitize_delivery_error(
            str(error) if error else "unknown failure"
        )
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET detached_status=?, error=COALESCE(?, error)
               WHERE detached_run_id=? AND detached_status='started'""",
            (detached_status, detail, str(run_id)),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE detached_run_id=?", (str(run_id),)
        ).fetchone())
    _emit_execution_state(record)
    return record


def find_detached_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Idempotent readback: the execution row for one correlation id."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE detached_run_id=?",
            (str(run_id),),
        ).fetchone()
    return _record(row)


def reconcile_detached_runs() -> List[Dict[str, Any]]:
    """Convert reported/expired detached runs into terminal executions.

    * ``succeeded`` → completed; ``failed`` → failed (error retained).
    * a still-``started`` lease past expiry → ``lost``: a visible permanent
      failure, never a silent hang.

    Failures (including lost leases) mint incidents through the product
    incident writer IN THE SAME TRANSACTION as the terminalization: either
    both commit or neither does, and an incident-store failure propagates
    to the caller (the tick retries next sweep) instead of leaving a
    terminal failure with no incident. Returns the affected records.
    """
    from cron.incidents import _initialize_schema as _init_incidents
    from cron.incidents import upsert_incident_in

    now = _hermes_now()
    now_iso = now.isoformat()
    reconciled: List[Dict[str, Any]] = []
    with _transaction() as conn:
        # Incidents share this SQLite file; make sure their schema exists on
        # this connection (idempotent CREATEs) so the co-write below can
        # never fail on a fresh store.
        _init_incidents(conn)
        rows = conn.execute(
            """SELECT * FROM executions
               WHERE detached_run_id IS NOT NULL
                 AND status IN ('claimed','running')
                 AND detached_status IN ('started','succeeded','failed')"""
        ).fetchall()
        for row in rows:
            detached_status = row["detached_status"]
            if detached_status == "started":
                expiry = str(row["lease_expires_at"] or "")
                expired = False
                try:
                    expired = bool(expiry) and datetime.fromisoformat(expiry) < now
                except ValueError:
                    expired = True  # unparseable lease cannot vouch for the run
                if not expired:
                    continue
                conn.execute(
                    """UPDATE executions
                       SET status='failed', outcome='failed', finished_at=?,
                           detached_status='lost', error=?
                       WHERE id=? AND status IN ('claimed','running')""",
                    (now_iso,
                     "Detached run lease expired with no terminal report; "
                     "the worker is presumed dead.",
                     row["id"]),
                )
            elif detached_status == "succeeded":
                conn.execute(
                    """UPDATE executions
                       SET status='completed', outcome='completed',
                           finished_at=?, error=NULL
                       WHERE id=? AND status IN ('claimed','running')""",
                    (now_iso, row["id"]),
                )
            else:  # failed
                conn.execute(
                    """UPDATE executions
                       SET status='failed', outcome='failed', finished_at=?
                       WHERE id=? AND status IN ('claimed','running')""",
                    (now_iso, row["id"]),
                )
            record = _record(conn.execute(
                "SELECT * FROM executions WHERE id=?", (row["id"],)
            ).fetchone())
            if record is not None:
                reconciled.append(record)
                if record["status"] == "failed":
                    # Permanent failure must be visible: the incident lands
                    # in the SAME commit as the terminal write. No swallow —
                    # a raise here rolls the whole batch back.
                    upsert_incident_in(
                        conn,
                        record["job_id"],
                        f"detached run {record['detached_run_id']} "
                        f"({record['detached_status']}): "
                        + (record["error"] or "unknown failure"),
                    )
        if reconciled:
            _prune_unlocked(conn)
    for record in reconciled:
        _emit_execution_state(record)
    return reconciled


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50, before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Return one exact execution attempt, or ``None`` when it is absent."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?",
            (str(execution_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
