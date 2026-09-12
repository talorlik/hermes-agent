"""Transactional cron delivery outbox with at-least-once exact delivery.

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

Exact claims are at-least-once: an interrupted owner releases or leaves its
durable intent for a replacement gateway to claim. A crash after transport
acceptance can duplicate a send, but cannot silently lose it.

Rows live in the SAME ``cron/executions.db`` as the executions ledger,
incidents, and deferrals (one durable cron store per profile).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# Optional test override (mirrors ``cron.executions.EXECUTIONS_FILE``).
EXECUTIONS_FILE: Optional[Path] = None

OUTBOX_STATES = ("pending", "admitted", "delivered", "abandoned")
LEGACY_DYNAMIC = 0
EXACT_QUEUE = 1
EXACT_STATES = (
    "READY",
    "IN_FLIGHT",
    "DELIVERED",
    "RETRYABLE_FAILED",
    "DEAD",
    "UNKNOWN",
)
MAX_OUTBOX_ATTEMPTS = 5
EXACT_LEASE_SECONDS = 120
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


def _materialize_failure_lanes(
    conn: sqlite3.Connection, outbox_id: Optional[str] = None
) -> None:
    """Persist inferred lanes only for legacy rows whose lane is still NULL."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'"
    ).fetchone()
    params: list[Any] = []
    row_filter = "for_failure IS NULL"
    if outbox_id is not None:
        row_filter += " AND id=?"
        params.append(str(outbox_id))
    # Dynamic SQL fragments below are selected only from fixed schema
    # capabilities. Runtime identifiers remain bound parameters.
    default_sql = "UPDATE cron_outbox SET for_failure=0 WHERE " + row_filter
    if table is None:
        conn.execute(default_sql, params)
        return

    execution_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(executions)")
    }
    failed_predicates: list[str] = []
    if "outcome" in execution_columns:
        failed_predicates.append("e.outcome='failed'")
    if "status" in execution_columns:
        status_failed = "e.status='failed'"
        if "outcome" in execution_columns:
            status_failed = f"(e.outcome IS NULL AND {status_failed})"
        failed_predicates.append(status_failed)
    if not failed_predicates:
        conn.execute(default_sql, params)
        return
    failed = " OR ".join(failed_predicates)
    conn.execute(
        f"""UPDATE cron_outbox
               SET for_failure=COALESCE((
                 SELECT CASE WHEN {failed} THEN 1 ELSE 0 END
                 FROM executions AS e WHERE e.id=cron_outbox.execution_id
               ), 0)
               WHERE {row_filter}""",
        params,
    )


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
             destination_json TEXT,
             destination_hash TEXT,
             delivery_contract INTEGER NOT NULL DEFAULT 0
               CHECK(delivery_contract IN (0, 1)),
             exact_state TEXT CHECK(exact_state IS NULL OR exact_state IN
               ('READY','IN_FLIGHT','DELIVERED','RETRYABLE_FAILED','DEAD','UNKNOWN')),
             generation INTEGER NOT NULL DEFAULT 0,
             owner_token TEXT,
             owner_process_id TEXT,
             owner_pid INTEGER,
             owner_started_at INTEGER,
             lease_expires_at TEXT,
             outcome_error TEXT,
             job_json TEXT,
             transport_status TEXT,
             receipt_id TEXT,
             projection_revision INTEGER,
             accounted_queue_generation INTEGER NOT NULL DEFAULT 0,
             revision INTEGER NOT NULL DEFAULT 0,
             next_attempt_at TEXT,
             for_failure  INTEGER,
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
        """CREATE TABLE IF NOT EXISTS cron_outbox_attachments (
             outbox_id TEXT NOT NULL,
             ordinal INTEGER NOT NULL,
             original_path TEXT NOT NULL,
             suffix TEXT NOT NULL,
             content BLOB NOT NULL,
             PRIMARY KEY (outbox_id, ordinal)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cron_job_delivery_projection (
             job_id TEXT PRIMARY KEY,
             execution_id TEXT NOT NULL,
             revision INTEGER NOT NULL,
             last_status TEXT,
             last_delivery_error TEXT,
             last_delivery_unverified TEXT,
             last_delivery_queued TEXT,
             finalized INTEGER NOT NULL DEFAULT 0,
             updated_at TEXT NOT NULL
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
    # Atomic co-writes touch the incident table on this connection.
    from cron.incidents import _initialize_schema as _init_incidents

    _init_incidents(conn)

    # Failure-lane migration is local to the outbox. It cannot rely on the
    # executions module having upgraded the shared database first.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cron_outbox_migrations (
                 name       TEXT PRIMARY KEY,
                 applied_at TEXT NOT NULL
               )"""
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(cron_outbox)")}
        if "for_failure" not in columns:
            conn.execute("ALTER TABLE cron_outbox ADD COLUMN for_failure INTEGER")
        if "destination_json" not in columns:
            conn.execute("ALTER TABLE cron_outbox ADD COLUMN destination_json TEXT")
        additive_columns = (
            ("destination_hash", "TEXT"),
            ("exact_state", "TEXT"),
            ("generation", "INTEGER NOT NULL DEFAULT 0"),
            ("owner_token", "TEXT"),
            ("owner_process_id", "TEXT"),
            ("owner_pid", "INTEGER"),
            ("owner_started_at", "INTEGER"),
            ("lease_expires_at", "TEXT"),
            ("outcome_error", "TEXT"),
            ("job_json", "TEXT"),
            ("transport_status", "TEXT"),
            ("receipt_id", "TEXT"),
            ("projection_revision", "INTEGER"),
        )
        for column, ddl in additive_columns:
            if column not in columns:
                conn.execute(f"ALTER TABLE cron_outbox ADD COLUMN {column} {ddl}")
        if "delivery_contract" not in columns:
            conn.execute(
                "ALTER TABLE cron_outbox ADD COLUMN delivery_contract INTEGER NOT NULL "
                "DEFAULT 0 CHECK(delivery_contract IN (0, 1))"
            )
        if "accounted_queue_generation" not in columns:
            conn.execute(
                "ALTER TABLE cron_outbox ADD COLUMN accounted_queue_generation "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "revision" not in columns:
            conn.execute(
                "ALTER TABLE cron_outbox ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        if "next_attempt_at" not in columns:
            conn.execute("ALTER TABLE cron_outbox ADD COLUMN next_attempt_at TEXT")
        exact_rows = conn.execute(
            """SELECT id, execution_id, job_id, state, destination_json, job_json
               FROM cron_outbox
               WHERE delivery_contract=? AND exact_state IS NULL""",
            (EXACT_QUEUE,),
        ).fetchall()
        for exact_row in exact_rows:
            destination_json = exact_row["destination_json"]
            exact_state = {
                "delivered": "DELIVERED",
                "abandoned": "DEAD",
            }.get(str(exact_row["state"]), "READY")
            conn.execute(
                """UPDATE cron_outbox
                   SET exact_state=?, destination_hash=?, job_json=COALESCE(job_json, ?)
                   WHERE id=?""",
                (
                    exact_state,
                    hashlib.sha256(str(destination_json).encode()).hexdigest()
                    if destination_json is not None
                    else None,
                    json.dumps(
                        {
                            "id": str(exact_row["job_id"]),
                            "execution_id": exact_row["execution_id"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    exact_row["id"],
                ),
            )
        attempt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cron_outbox_attempts)")
        }
        if "queue_generation" not in attempt_columns:
            conn.execute(
                "ALTER TABLE cron_outbox_attempts ADD COLUMN queue_generation INTEGER"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS cron_outbox_attempt_queue_generation_uq "
            "ON cron_outbox_attempts(outbox_id, queue_generation) "
            "WHERE queue_generation IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS cron_outbox_exact_destination_uq "
            "ON cron_outbox(execution_id, for_failure, destination_hash) "
            "WHERE delivery_contract=1 AND execution_id IS NOT NULL"
        )
        invalid_exact = """(
          (NEW.delivery_contract=0 AND NEW.exact_state IS NOT NULL)
          OR (NEW.delivery_contract=1 AND NEW.exact_state IS NULL)
          OR (NEW.delivery_contract=1
              AND NEW.exact_state IN ('READY','IN_FLIGHT','RETRYABLE_FAILED')
              AND (NEW.destination_json IS NULL OR NEW.destination_hash IS NULL
                   OR NEW.job_json IS NULL))
        )"""
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS cron_outbox_exact_insert_guard
                BEFORE INSERT ON cron_outbox WHEN {invalid_exact}
                BEGIN SELECT RAISE(ABORT, 'invalid exact delivery contract'); END"""
        )
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS cron_outbox_exact_update_guard
                BEFORE UPDATE ON cron_outbox WHEN {invalid_exact}
                BEGIN SELECT RAISE(ABORT, 'invalid exact delivery contract'); END"""
        )
        migrated = conn.execute(
            "SELECT 1 FROM cron_outbox_migrations WHERE name='failure_lane_v1'"
        ).fetchone()
        if migrated is None:
            _materialize_failure_lanes(conn)
            conn.execute(
                "INSERT INTO cron_outbox_migrations (name, applied_at) VALUES (?, ?)",
                ("failure_lane_v1", _hermes_now().isoformat()),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


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

        text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:
        return "[REDACTED - delivery error unavailable]"
    return text[:_MAX_ERROR_CHARS]


def _serialize_destination(destination: Dict[str, Any]) -> str:
    payload = {
        "platform": str(destination["platform"]),
        "chat_id": str(destination["chat_id"]),
        "thread_id": (
            str(destination["thread_id"])
            if destination.get("thread_id") is not None
            else None
        ),
        "_resolved_from": destination.get("_resolved_from"),
    }
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def decode_persisted_destination(raw: Any) -> Dict[str, Any]:
    """Decode one durable exact target; never reinterpret invalid data as legacy."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("persisted destination must be a JSON object")
    platform = payload.get("platform")
    chat_id = payload.get("chat_id")
    if not isinstance(platform, str) or not platform.strip():
        raise ValueError("persisted destination requires a non-empty platform")
    thread_id = payload.get("thread_id")
    current_profile_bot_chat = (
        platform == "bot-chat"
        and chat_id == ""
        and thread_id is None
        and payload.get("_resolved_from") is None
    )
    if not isinstance(chat_id, str) or (not chat_id and not current_profile_bot_chat):
        raise ValueError("persisted destination requires a non-empty chat_id")
    if thread_id is not None and not isinstance(thread_id, str):
        raise ValueError("persisted destination thread_id must be a string or null")
    return payload


def canonical_destination_json(destination: Dict[str, Any]) -> str:
    """Return the validated canonical representation used by outbox and queue."""
    return _serialize_destination(
        decode_persisted_destination(_serialize_destination(destination))
    )


def get_outbox(outbox_id: str) -> Optional[Dict[str, Any]]:
    """Return one durable outbox row."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (str(outbox_id),)
        ).fetchone()
    return _record(row)


def configure_exact_delivery(
    outbox_id: str,
    *,
    job: Dict[str, Any],
    content: str,
    destination: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate and refresh an unsent exact row without creating a second queue."""
    destination_json = canonical_destination_json(destination)
    destination_hash = hashlib.sha256(destination_json.encode()).hexdigest()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (str(outbox_id),)
        ).fetchone()
        if row is None:
            raise ValueError("linked exact outbox row does not exist")
        if (
            int(row["delivery_contract"] or 0) != EXACT_QUEUE
            or row["exact_state"] is None
        ):
            raise ValueError("linked outbox row does not use the exact contract")
        if row["destination_json"] != destination_json:
            raise ValueError("queue and outbox destinations do not match")
        if row["destination_hash"] != destination_hash:
            raise ValueError("exact outbox destination hash mismatch")
        if str(row["content"]) != str(content):
            raise ValueError(
                "exact outbox content does not match its immutable snapshot"
            )
        if row["exact_state"] in {"READY", "RETRYABLE_FAILED"}:
            conn.execute(
                """UPDATE cron_outbox
                   SET job_json=?, generation=generation+1, updated_at=?
                   WHERE id=? AND exact_state IN ('READY','RETRYABLE_FAILED')""",
                (
                    json.dumps(job, ensure_ascii=False, sort_keys=True),
                    _hermes_now().isoformat(),
                    str(outbox_id),
                ),
            )
        refreshed = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (str(outbox_id),)
        ).fetchone()
    if refreshed is None:
        raise RuntimeError("exact outbox row vanished inside its transaction")
    return dict(refreshed)


def get_exact_state(outbox_id: str) -> Optional[Dict[str, Any]]:
    """Return one exact state row from the authoritative cron database."""
    row = get_outbox(str(outbox_id))
    if row is None or row.get("exact_state") is None:
        return None
    return row


def claim_exact(
    *,
    owner_process_id: str,
    owner_pid: int,
    owner_started_at: Optional[int],
    outbox_ids: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Claim one READY exact delivery by generation and opaque owner token."""
    constrained_ids = tuple(str(outbox_id) for outbox_id in (outbox_ids or ()))
    if outbox_ids is not None and not constrained_ids:
        return None
    owner_token = uuid.uuid4().hex
    now = _hermes_now()
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        id_clause = ""
        params: list[Any] = [EXACT_QUEUE]
        if outbox_ids is not None:
            id_clause = f" AND id IN ({','.join('?' for _ in constrained_ids)})"
            params.extend(constrained_ids)
        row = conn.execute(
            f"""SELECT id FROM cron_outbox
               WHERE delivery_contract=? AND exact_state='READY' AND state='pending'
               {id_clause}
               ORDER BY created_at, id LIMIT 1""",
            params,
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            """UPDATE cron_outbox
               SET exact_state='IN_FLIGHT', generation=generation+1,
                   owner_token=?, owner_process_id=?, owner_pid=?, owner_started_at=?,
                   lease_expires_at=?, transport_status=NULL, receipt_id=NULL,
                   updated_at=?
               WHERE id=? AND delivery_contract=? AND exact_state='READY'
                 AND state='pending'""",
            (
                owner_token,
                str(owner_process_id),
                int(owner_pid),
                owner_started_at,
                (now + timedelta(seconds=EXACT_LEASE_SECONDS)).isoformat(),
                now.isoformat(),
                row["id"],
                EXACT_QUEUE,
            ),
        )
        if cur.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (row["id"],)
        ).fetchone()
    return _record(claimed)


def renew_exact_lease(outbox_id: str, *, generation: int, owner_token: str) -> bool:
    """Renew a live claim without weakening its generation fence."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE cron_outbox SET lease_expires_at=?, updated_at=?
               WHERE id=? AND exact_state='IN_FLIGHT' AND generation=?
                 AND owner_token=? AND state='pending'""",
            (
                (_hermes_now() + timedelta(seconds=EXACT_LEASE_SECONDS)).isoformat(),
                _hermes_now().isoformat(),
                str(outbox_id),
                int(generation),
                str(owner_token),
            ),
        )
    return cur.rowcount == 1


def _finalize_projection_if_settled_in(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> None:
    execution_id = row["execution_id"]
    projection_revision = row["projection_revision"]
    if execution_id is None or projection_revision is None:
        return
    execution_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'"
    ).fetchone()
    unsettled = conn.execute(
        """SELECT 1 FROM cron_outbox
           WHERE execution_id=? AND delivery_contract=?
             AND exact_state NOT IN ('DELIVERED','DEAD','UNKNOWN') LIMIT 1""",
        (str(execution_id), EXACT_QUEUE),
    ).fetchone()
    if unsettled is not None:
        return
    failures = conn.execute(
        """SELECT outcome_error FROM cron_outbox
           WHERE execution_id=? AND delivery_contract=?
             AND exact_state IN ('DEAD','UNKNOWN') ORDER BY created_at, id""",
        (str(execution_id), EXACT_QUEUE),
    ).fetchall()
    delivery_error = (
        "; ".join(
            str(item["outcome_error"] or "delivery outcome unknown")
            for item in failures
        )
        or None
    )
    delivery_status = "failed" if failures else "delivered"
    now_iso = _hermes_now().isoformat()
    if execution_table is not None:
        conn.execute(
            """UPDATE executions
               SET delivery_target=?, delivery_outcome=?, delivery_status=?, delivery_error=?,
                   delivery_attempts=(SELECT COALESCE(SUM(attempts), 0)
                     FROM cron_outbox WHERE execution_id=? AND delivery_contract=?)
               WHERE id=?""",
            (
                str(row["target"]),
                delivery_status,
                delivery_status,
                _sanitize_error(delivery_error),
                str(execution_id),
                EXACT_QUEUE,
                str(execution_id),
            ),
        )
    job_status = (
        "delivery_failed"
        if failures
        else ("failed" if bool(row["for_failure"]) else "completed")
    )
    conn.execute(
        """UPDATE cron_job_delivery_projection
           SET last_status=?, last_delivery_error=?, finalized=1,
               revision=revision+1, updated_at=?
           WHERE job_id=? AND execution_id=? AND revision=? AND finalized=0""",
        (
            job_status,
            _sanitize_error(delivery_error),
            now_iso,
            str(row["job_id"]),
            str(execution_id),
            int(projection_revision),
        ),
    )


def finish_exact(
    outbox_id: str,
    *,
    generation: int,
    owner_token: str,
    error: Optional[str],
    permanent: bool = False,
    transport_status: Optional[str] = None,
    receipt_id: Optional[str] = None,
) -> bool:
    """CAS-finalize one claim and all accounting in executions.db."""
    clean_error = _sanitize_error(error)
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT * FROM cron_outbox
               WHERE id=? AND delivery_contract=? AND exact_state='IN_FLIGHT'
                 AND generation=? AND owner_token=?""",
            (str(outbox_id), EXACT_QUEUE, int(generation), str(owner_token)),
        ).fetchone()
        if row is None:
            return False
        now_iso = _hermes_now().isoformat()
        if transport_status in {"queued", "claimed"}:
            attempt_no = int(row["attempts"]) + 1
            conn.execute(
                """UPDATE cron_outbox
                   SET state='admitted', attempts=?, transport_status=?, receipt_id=?,
                       lease_expires_at=NULL, updated_at=?, last_error=NULL
                   WHERE id=? AND exact_state='IN_FLIGHT' AND generation=?
                     AND owner_token=?""",
                (
                    attempt_no,
                    transport_status,
                    str(receipt_id or ""),
                    now_iso,
                    str(outbox_id),
                    int(generation),
                    str(owner_token),
                ),
            )
            conn.execute(
                """INSERT INTO cron_outbox_attempts
                   (outbox_id, attempt_no, at, status, error, queue_generation)
                   VALUES (?, ?, ?, 'admitted', NULL, ?)""",
                (str(outbox_id), attempt_no, now_iso, int(generation)),
            )
            return True
        outcome = (
            "dead" if permanent else ("retryable_failed" if error else "succeeded")
        )
        desired = {
            "succeeded": "DELIVERED",
            "retryable_failed": "RETRYABLE_FAILED",
            "dead": "DEAD",
        }[outcome]
        cur = conn.execute(
            """UPDATE cron_outbox
               SET exact_state=?, outcome_error=?, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND delivery_contract=? AND exact_state='IN_FLIGHT'
                 AND generation=? AND owner_token=?""",
            (
                desired,
                clean_error,
                now_iso,
                str(outbox_id),
                EXACT_QUEUE,
                int(generation),
                str(owner_token),
            ),
        )
        if cur.rowcount != 1:
            return False
        if not record_exact_queue_outcome_in(
            conn,
            outbox_id=str(outbox_id),
            generation=int(generation),
            outcome=outcome,
            error=clean_error,
        ):
            raise RuntimeError("exact delivery accounting CAS failed")
        refreshed = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (str(outbox_id),)
        ).fetchone()
        if refreshed is None:
            raise RuntimeError("exact outbox row vanished during finalization")
        if (
            refreshed["state"] == "abandoned"
            and refreshed["exact_state"] == "RETRYABLE_FAILED"
        ):
            conn.execute(
                "UPDATE cron_outbox SET exact_state='DEAD' WHERE id=?",
                (str(outbox_id),),
            )
            refreshed = conn.execute(
                "SELECT * FROM cron_outbox WHERE id=?", (str(outbox_id),)
            ).fetchone()
        _finalize_projection_if_settled_in(conn, refreshed)
        finalized = dict(refreshed)
    if clean_error:
        _write_local_fallback(finalized, clean_error)
    return True


def _release_exact_claim_in(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    generation: int,
    owner_token: str,
) -> bool:
    cur = conn.execute(
        """UPDATE cron_outbox
           SET exact_state='READY', owner_token=NULL, owner_process_id=NULL,
               owner_pid=NULL, owner_started_at=NULL, lease_expires_at=NULL,
               transport_status=NULL, receipt_id=NULL, updated_at=?
           WHERE id=? AND delivery_contract=? AND exact_state='IN_FLIGHT'
             AND generation=? AND owner_token=? AND state='pending'""",
        (
            _hermes_now().isoformat(),
            str(outbox_id),
            EXACT_QUEUE,
            int(generation),
            str(owner_token),
        ),
    )
    return cur.rowcount == 1


def release_exact_claim(outbox_id: str, *, generation: int, owner_token: str) -> bool:
    """Release an interrupted claim so its durable intent can be retried."""
    with _transaction() as conn:
        return _release_exact_claim_in(
            conn,
            outbox_id=str(outbox_id),
            generation=int(generation),
            owner_token=str(owner_token),
        )


def recover_exact_abandoned(
    owner_is_live: Any,
    *,
    local_process_id: Optional[str] = None,
    active_outbox_ids: Sequence[str] = (),
) -> int:
    """Release dead owners and expired inactive claims from this process."""
    changed = 0
    active_ids = {str(outbox_id) for outbox_id in active_outbox_ids}
    now_iso = _hermes_now().isoformat()
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM cron_outbox
               WHERE delivery_contract=? AND exact_state='IN_FLIGHT' AND state='pending'""",
            (EXACT_QUEUE,),
        ).fetchall()
        for row in rows:
            same_process = local_process_id is not None and str(
                row["owner_process_id"]
            ) == str(local_process_id)
            if same_process and str(row["id"]) in active_ids:
                continue
            owner_live = owner_is_live(int(row["owner_pid"]), row["owner_started_at"])
            lease_expired = (
                row["lease_expires_at"] is not None
                and str(row["lease_expires_at"]) <= now_iso
            )
            if owner_live and not (same_process and lease_expired):
                continue
            changed += int(
                _release_exact_claim_in(
                    conn,
                    outbox_id=str(row["id"]),
                    generation=int(row["generation"]),
                    owner_token=str(row["owner_token"]),
                )
            )
    return changed


def reactivate_exact(outbox_id: str) -> bool:
    """Reactivate only a due retryable row below its authoritative cap."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE cron_outbox
               SET exact_state='READY', generation=generation+1,
                   owner_token=NULL, owner_process_id=NULL, owner_pid=NULL,
                   owner_started_at=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND delivery_contract=? AND exact_state='RETRYABLE_FAILED'
                 AND state='pending' AND attempts < ?""",
            (
                _hermes_now().isoformat(),
                str(outbox_id),
                EXACT_QUEUE,
                MAX_OUTBOX_ATTEMPTS,
            ),
        )
    return cur.rowcount == 1


def settle_admitted_delivery(
    outbox_id: str,
    *,
    receipt_id: str,
    receipt_status: str,
    receipt_error: Optional[str] = None,
    receipt_reason: Optional[str] = None,
) -> bool:
    """Reconcile an admitted Bot Chat row from its matching terminal receipt."""
    if receipt_status not in {"settled", "failed", "cancelled", "ambiguous"}:
        return False
    if receipt_status == "settled":
        outcome = "succeeded"
        exact_state = "DELIVERED"
        error = None
    elif receipt_status == "ambiguous":
        outcome = "unknown"
        exact_state = "UNKNOWN"
        error = (
            receipt_error or receipt_reason or "Bot Chat delivery outcome is ambiguous"
        )
    elif receipt_status == "cancelled":
        outcome = "dead"
        exact_state = "DEAD"
        error = receipt_error or receipt_reason or "Bot Chat delivery was cancelled"
    else:
        from tools.bot_failure_reasons import is_auto_retryable

        outcome = (
            "retryable_failed"
            if is_auto_retryable(str(receipt_reason or ""))
            else "dead"
        )
        exact_state = "RETRYABLE_FAILED" if outcome == "retryable_failed" else "DEAD"
        error = receipt_error or receipt_reason or "Bot Chat delivery failed"
    clean_error = _sanitize_error(error)
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT * FROM cron_outbox
               WHERE id=? AND delivery_contract=? AND exact_state='IN_FLIGHT'
                 AND state='admitted' AND receipt_id=?""",
            (str(outbox_id), EXACT_QUEUE, str(receipt_id)),
        ).fetchone()
        if row is None:
            return False
        cur = conn.execute(
            """UPDATE cron_outbox
               SET exact_state=?, transport_status=?, outcome_error=?, updated_at=?
               WHERE id=? AND delivery_contract=? AND exact_state='IN_FLIGHT'
                 AND state='admitted' AND receipt_id=? AND generation=?""",
            (
                exact_state,
                receipt_status,
                clean_error,
                _hermes_now().isoformat(),
                str(outbox_id),
                EXACT_QUEUE,
                str(receipt_id),
                int(row["generation"]),
            ),
        )
        if cur.rowcount != 1:
            return False
        if not record_exact_queue_outcome_in(
            conn,
            outbox_id=str(outbox_id),
            generation=int(row["generation"]),
            outcome=outcome,
            error=clean_error,
            account_existing_attempt=True,
        ):
            raise RuntimeError("admitted exact delivery accounting CAS failed")
        refreshed = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (str(outbox_id),)
        ).fetchone()
        if refreshed is None:
            raise RuntimeError("admitted exact delivery vanished during reconciliation")
        _finalize_projection_if_settled_in(conn, refreshed)
    return True


def admitted_exact_deliveries(limit: int = 100) -> List[Dict[str, Any]]:
    """Return admitted exact deliveries that require receipt reconciliation."""
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM cron_outbox
               WHERE delivery_contract=? AND exact_state='IN_FLIGHT'
                 AND state='admitted' AND receipt_id IS NOT NULL
               ORDER BY updated_at, id LIMIT ?""",
            (EXACT_QUEUE, max(1, min(int(limit), 1000))),
        ).fetchall()
    return [dict(row) for row in rows]


def materialize_exact_content(outbox_id: str, directory: Path) -> str:
    """Resolve retry MEDIA references only from immutable enqueue-time blobs."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT content FROM cron_outbox WHERE id=? AND delivery_contract=?",
            (str(outbox_id), EXACT_QUEUE),
        ).fetchone()
        attachments = conn.execute(
            """SELECT ordinal, original_path, suffix, content
               FROM cron_outbox_attachments WHERE outbox_id=? ORDER BY ordinal""",
            (str(outbox_id),),
        ).fetchall()
    if row is None:
        raise ValueError("exact outbox row does not exist")
    content = str(row["content"])
    directory.mkdir(parents=True, exist_ok=True)
    for attachment in attachments:
        path = directory / f"{int(attachment['ordinal']):04d}{attachment['suffix']}"
        path.write_bytes(bytes(attachment["content"]))
        content = content.replace(str(attachment["original_path"]), str(path))
    return content


def record_exact_queue_outcome_in(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    generation: int,
    outcome: str,
    error: Optional[str],
    account_existing_attempt: bool = False,
) -> bool:
    """Account one persisted exact queue generation in the caller's transaction."""
    if outcome not in {"succeeded", "retryable_failed", "dead", "unknown"}:
        raise ValueError(f"unsupported exact queue outcome: {outcome}")
    outbox_table = "cron_outbox"
    attempts_table = "cron_outbox_attempts"
    incidents_table = "cron_incidents"
    clean_error = _sanitize_error(error)
    now = _hermes_now()
    now_iso = now.isoformat()
    row = conn.execute(
        f"SELECT * FROM {outbox_table} WHERE id=? AND delivery_contract=?",
        (str(outbox_id), EXACT_QUEUE),
    ).fetchone()
    if row is None:
        return False
    attempt_no = int(row["attempts"]) + (0 if account_existing_attempt else 1)
    if outcome == "succeeded":
        state = "delivered"
        next_attempt_at = None
        attempt_status = "delivered"
    elif outcome == "unknown":
        state = "abandoned"
        next_attempt_at = None
        attempt_status = "unknown"
    elif outcome == "dead" or attempt_no >= MAX_OUTBOX_ATTEMPTS:
        state = "abandoned"
        next_attempt_at = None
        attempt_status = "failed"
    else:
        state = "pending"
        next_attempt_at = (now + timedelta(seconds=30)).isoformat()
        attempt_status = "failed"
    attempts_assignment = (
        "attempts=attempts" if account_existing_attempt else "attempts=attempts+1"
    )
    cur = conn.execute(
        f"""UPDATE {outbox_table}
            SET {attempts_assignment}, state=?, updated_at=?, last_error=?,
                next_attempt_at=?, accounted_queue_generation=?, revision=revision+1
            WHERE id=? AND delivery_contract=?
              AND accounted_queue_generation < ?
              AND state NOT IN ('delivered','abandoned')""",
        (
            state,
            now_iso,
            clean_error,
            next_attempt_at,
            int(generation),
            str(outbox_id),
            EXACT_QUEUE,
            int(generation),
        ),
    )
    if cur.rowcount != 1:
        return False
    if account_existing_attempt:
        attempt_cur = conn.execute(
            f"""UPDATE {attempts_table}
                SET at=?, status=?, error=?
                WHERE outbox_id=? AND queue_generation=? AND status='admitted'""",
            (
                now_iso,
                attempt_status,
                clean_error,
                str(outbox_id),
                int(generation),
            ),
        )
        if attempt_cur.rowcount != 1:
            raise RuntimeError("admitted exact delivery attempt is missing")
    else:
        conn.execute(
            f"""INSERT INTO {attempts_table}
                (outbox_id, attempt_no, at, status, error, queue_generation)
                VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(outbox_id),
                attempt_no,
                now_iso,
                attempt_status,
                clean_error,
                int(generation),
            ),
        )
    if row["execution_id"] is not None:
        execution_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'"
        ).fetchone()
        if execution_table is not None:
            conn.execute(
                """UPDATE executions
                   SET delivery_target=?, delivery_status=?, delivery_error=?,
                       delivery_attempts=(SELECT COALESCE(SUM(attempts), 0)
                         FROM cron_outbox
                         WHERE execution_id=? AND delivery_contract=?)
                   WHERE id=?""",
                (
                    str(row["target"]),
                    attempt_status,
                    clean_error,
                    str(row["execution_id"]),
                    EXACT_QUEUE,
                    str(row["execution_id"]),
                ),
            )
    if outcome != "succeeded":
        from cron.incidents import _error_signature, _incident_id, _redact_error

        incident_error = (
            f"delivery failed to {row['target']}: {clean_error or 'unknown error'}"
        )
        signature = _error_signature(str(row["job_id"]), incident_error)
        incident_id = _incident_id(str(row["job_id"]), signature)
        stored_error = _redact_error(incident_error)
        incident = conn.execute(
            f"SELECT state FROM {incidents_table} WHERE id=?", (incident_id,)
        ).fetchone()
        if incident is None:
            conn.execute(
                f"""INSERT INTO {incidents_table}
                    (id, job_id, error_sig, state, failure_type, first_seen_at,
                     last_seen_at, error, output_file)
                    VALUES (?, ?, ?, 'detected', 'delivery', ?, ?, ?, NULL)""",
                (
                    incident_id,
                    str(row["job_id"]),
                    signature,
                    now_iso,
                    now_iso,
                    stored_error,
                ),
            )
        else:
            conn.execute(
                f"""UPDATE {incidents_table}
                    SET last_seen_at=?, error=?,
                        state=CASE WHEN state='recovered' THEN 'detected' ELSE state END,
                        recovered_at=CASE WHEN state='recovered' THEN NULL ELSE recovered_at END
                    WHERE id=?""",
                (now_iso, stored_error, incident_id),
            )
    else:
        unsettled = conn.execute(
            f"""SELECT 1 FROM {outbox_table}
                WHERE job_id=? AND state IN ('pending','abandoned') LIMIT 1""",
            (str(row["job_id"]),),
        ).fetchone()
        if unsettled is None:
            conn.execute(
                f"""UPDATE {incidents_table}
                    SET state='recovered', recovered_at=?
                    WHERE job_id=? AND state IN ('detected','alerted')
                      AND failure_type='delivery'""",
                (now_iso, str(row["job_id"])),
            )
    return True


def _stamp_execution_intent(
    conn: sqlite3.Connection,
    execution_id: Optional[str],
    *,
    intent_success: bool,
    intent_error: Optional[str],
) -> None:
    if not execution_id:
        return
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'"
    ).fetchone()
    if table is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
    if "outcome" not in columns or "status" not in columns:
        # Without the nonterminal status guard, intent stamping could
        # overwrite an execution that has already settled.
        return
    assignments = ["outcome=?"]
    values: list[Any] = ["completed" if intent_success else "failed"]
    if "error" in columns:
        assignments.append("error=COALESCE(?, error)")
        values.append(_sanitize_error(intent_error))
    values.append(execution_id)
    conn.execute(
        f"""UPDATE executions
            SET {", ".join(assignments)}
            WHERE id=? AND status IN ('claimed','running')""",
        values,
    )


def _snapshot_attachments(content: str) -> list[tuple[str, str, bytes]]:
    """Read policy-approved MEDIA attachments before the durable enqueue commits."""
    from gateway.media_policy import apply_media_policy_env
    from gateway.platforms.base import BasePlatformAdapter, validate_media_delivery_path

    apply_media_policy_env()
    media_files, _ = BasePlatformAdapter.extract_media(str(content))
    snapshots: list[tuple[str, str, bytes]] = []
    for raw_path, _is_voice in media_files:
        safe_path = validate_media_delivery_path(str(raw_path))
        if safe_path is None:
            continue
        path = Path(safe_path)
        snapshots.append((str(raw_path), path.suffix, path.read_bytes()))
    return snapshots


def _begin_job_delivery_projection_in(
    conn: sqlite3.Connection, job_id: str, execution_id: str
) -> Dict[str, Any]:
    now_iso = _hermes_now().isoformat()
    conn.execute(
        """INSERT INTO cron_job_delivery_projection
           (job_id, execution_id, revision, finalized, updated_at)
           VALUES (?, ?, 1, 0, ?)
           ON CONFLICT(job_id) DO UPDATE SET
             execution_id=excluded.execution_id,
             revision=cron_job_delivery_projection.revision+1,
             last_status=NULL,
             last_delivery_error=NULL,
             last_delivery_unverified=NULL,
             last_delivery_queued=NULL,
             finalized=0,
             updated_at=excluded.updated_at""",
        (str(job_id), str(execution_id), now_iso),
    )
    row = conn.execute(
        "SELECT * FROM cron_job_delivery_projection WHERE job_id=?", (str(job_id),)
    ).fetchone()
    if row is None:
        raise RuntimeError("job delivery projection vanished inside its transaction")
    return dict(row)


def begin_job_delivery_projection(job_id: str, execution_id: str) -> Dict[str, Any]:
    """Start one execution-bound job projection generation."""
    with _transaction() as conn:
        return _begin_job_delivery_projection_in(conn, job_id, execution_id)


def get_job_delivery_projection(job_id: str) -> Optional[Dict[str, Any]]:
    """Return the canonical execution-bound delivery projection for a job."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM cron_job_delivery_projection WHERE job_id=?", (str(job_id),)
        ).fetchone()
    result = _record(row)
    if result is None:
        return None
    for key in ("last_delivery_unverified", "last_delivery_queued"):
        if result[key] is not None:
            result[key] = json.loads(result[key])
    return result


def finalize_job_delivery_projection(
    job_id: str,
    *,
    execution_id: str,
    expected_revision: int,
    last_status: Optional[str],
    last_delivery_error: Optional[str],
    last_delivery_unverified: Any = None,
    last_delivery_queued: Any = None,
) -> bool:
    """CAS-finalize one job delivery projection exactly once."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE cron_job_delivery_projection
               SET last_status=?, last_delivery_error=?,
                   last_delivery_unverified=?, last_delivery_queued=?,
                   finalized=1, revision=revision+1, updated_at=?
               WHERE job_id=? AND execution_id=? AND revision=? AND finalized=0""",
            (
                last_status,
                _sanitize_error(last_delivery_error),
                json.dumps(last_delivery_unverified, ensure_ascii=False)
                if last_delivery_unverified is not None
                else None,
                json.dumps(last_delivery_queued, ensure_ascii=False)
                if last_delivery_queued is not None
                else None,
                _hermes_now().isoformat(),
                str(job_id),
                str(execution_id),
                int(expected_revision),
            ),
        )
    return cur.rowcount == 1


def enqueue_deliveries_with_intent(
    *,
    execution_id: Optional[str],
    job_id: str,
    target: str,
    destinations: List[Dict[str, Any]],
    content: str,
    intent_success: bool,
    intent_error: Optional[str] = None,
    job: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Atomically persist concrete destinations and stamp terminal intent once."""
    now_iso = _hermes_now().isoformat()
    canonical: dict[str, str] = {}
    lane = 0 if intent_success else 1
    for destination in destinations:
        serialized = canonical_destination_json(destination)
        if serialized in canonical:
            raise ValueError("duplicate canonical delivery destination")
        if execution_id is None:
            outbox_id = uuid.uuid4().hex
        else:
            identity = f"exact-v1\0{execution_id}\0{lane}\0{serialized}"
            outbox_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        canonical[serialized] = outbox_id
    pending = [
        (outbox_id, destination_json)
        for destination_json, outbox_id in canonical.items()
    ]
    if not pending:
        return []
    queued_job = dict(job or {"id": str(job_id), "execution_id": execution_id})
    queued_job.setdefault("id", str(job_id))
    if execution_id is not None:
        queued_job.setdefault("execution_id", str(execution_id))
    job_json = json.dumps(queued_job, ensure_ascii=False, sort_keys=True)

    def existing_publication(
        conn: sqlite3.Connection,
    ) -> Optional[List[Dict[str, Any]]]:
        rows = conn.execute(
            f"SELECT * FROM cron_outbox WHERE id IN ({','.join('?' for _ in pending)})",
            [outbox_id for outbox_id, _ in pending],
        ).fetchall()
        if not rows:
            return None
        by_id = {str(row["id"]): row for row in rows}
        if len(by_id) != len(pending):
            raise ValueError("partial exact delivery publication already exists")
        for outbox_id, destination_json in pending:
            row = by_id[outbox_id]
            expected = {
                "execution_id": execution_id,
                "job_id": str(job_id),
                "target": str(target),
                "destination_json": destination_json,
                "for_failure": lane,
                "content": str(content),
            }
            if int(row["delivery_contract"] or 0) != EXACT_QUEUE or any(
                row[key] != value for key, value in expected.items()
            ):
                raise ValueError(
                    "exact delivery publication conflicts with durable intent"
                )
        return [dict(by_id[outbox_id]) for outbox_id, _ in pending]

    with _transaction() as conn:
        existing = existing_publication(conn)
    if existing is not None:
        return existing

    snapshots = _snapshot_attachments(str(content))
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = existing_publication(conn)
        if existing is not None:
            return existing
        projection_revision: Optional[int] = None
        if execution_id is not None:
            projection_revision = int(
                _begin_job_delivery_projection_in(conn, str(job_id), str(execution_id))[
                    "revision"
                ]
            )
        for outbox_id, destination_json in pending:
            destination_hash = hashlib.sha256(destination_json.encode()).hexdigest()
            conn.execute(
                """INSERT INTO cron_outbox
                   (id, execution_id, job_id, target, destination_json, destination_hash,
                    delivery_contract, exact_state, generation, job_json,
                    projection_revision, for_failure,
                    content, state, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'READY', 0, ?, ?, ?, ?,
                           'pending', 0, ?, ?)""",
                (
                    outbox_id,
                    execution_id,
                    str(job_id),
                    str(target),
                    destination_json,
                    destination_hash,
                    EXACT_QUEUE,
                    job_json,
                    projection_revision,
                    lane,
                    str(content),
                    now_iso,
                    now_iso,
                ),
            )
            for ordinal, (original_path, suffix, attachment) in enumerate(snapshots):
                conn.execute(
                    """INSERT INTO cron_outbox_attachments
                       (outbox_id, ordinal, original_path, suffix, content)
                       VALUES (?, ?, ?, ?, ?)""",
                    (outbox_id, ordinal, original_path, suffix, attachment),
                )
        _stamp_execution_intent(
            conn, execution_id, intent_success=intent_success, intent_error=intent_error
        )
        rows = conn.execute(
            f"SELECT * FROM cron_outbox WHERE id IN ({','.join('?' for _ in pending)})",
            [outbox_id for outbox_id, _ in pending],
        ).fetchall()
    by_id = {row["id"]: dict(row) for row in rows}
    return [by_id[outbox_id] for outbox_id, _ in pending]


def enqueue_with_intent(
    *,
    execution_id: Optional[str],
    job_id: str,
    target: str,
    content: str,
    intent_success: bool,
    intent_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist one legacy expression-based row with explicit lane intent."""
    outbox_id = uuid.uuid4().hex
    now_iso = _hermes_now().isoformat()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO cron_outbox
               (id, execution_id, job_id, target, destination_json,
                delivery_contract, for_failure, content,
                state, attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, NULL, ?, ?, ?, 'pending', 0, ?, ?)""",
            (
                outbox_id,
                execution_id,
                str(job_id),
                str(target),
                LEGACY_DYNAMIC,
                0 if intent_success else 1,
                str(content),
                now_iso,
                now_iso,
            ),
        )
        _stamp_execution_intent(
            conn, execution_id, intent_success=intent_success, intent_error=intent_error
        )
        row = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("cron_outbox row vanished inside its own transaction")
    return dict(row)


def update_destination_thread(
    outbox_id: str, thread_id: str
) -> Optional[Dict[str, Any]]:
    """Update the canonical unsent target and invalidate stale claim generations."""
    with _transaction() as conn:
        row = conn.execute(
            """SELECT destination_json, exact_state FROM cron_outbox
               WHERE id=? AND delivery_contract=?""",
            (str(outbox_id), EXACT_QUEUE),
        ).fetchone()
        if (
            row is None
            or row["destination_json"] is None
            or row["exact_state"] not in {"READY", "RETRYABLE_FAILED"}
        ):
            return None
        destination = decode_persisted_destination(row["destination_json"])
        destination["thread_id"] = str(thread_id)
        destination_json = canonical_destination_json(destination)
        cur = conn.execute(
            """UPDATE cron_outbox
               SET destination_json=?, destination_hash=?, generation=generation+1,
                   updated_at=?
               WHERE id=? AND delivery_contract=?
                 AND exact_state IN ('READY','RETRYABLE_FAILED')""",
            (
                destination_json,
                hashlib.sha256(destination_json.encode()).hexdigest(),
                _hermes_now().isoformat(),
                str(outbox_id),
                EXACT_QUEUE,
            ),
        )
        if cur.rowcount != 1:
            return None
        refreshed = conn.execute(
            "SELECT * FROM cron_outbox WHERE id=?", (str(outbox_id),)
        ).fetchone()
    return _record(refreshed)


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
            row.get("id"),
            exc_info=True,
        )


def write_enqueue_failure_fallback(
    *,
    execution_id: Optional[str],
    job_id: str,
    target: str,
    content: str,
    error: BaseException,
) -> str:
    """Persist content locally when the durable outbox cannot enqueue it."""
    clean_error = _sanitize_error(f"Outbox enqueue failed: {error}")
    fallback_id = f"enqueue-{execution_id or uuid.uuid4().hex}"
    _write_local_fallback(
        {
            "id": fallback_id,
            "job_id": job_id,
            "target": target,
            "attempts": 0,
            "content": content,
        },
        clean_error,
    )
    return clean_error or "Outbox enqueue failed"


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
        if (
            row["exact_state"] is not None
            or int(row["delivery_contract"] or 0) == EXACT_QUEUE
        ):
            # Exact rows are mutation-fenced by generation and owner token.
            # Legacy callers may observe them, but cannot reverse terminal state.
            return dict(row)
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
            (
                outbox_id,
                attempts,
                now_iso,
                "delivered" if delivered else "failed",
                clean_error,
            ),
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
            unsettled_sibling = conn.execute(
                """SELECT 1 FROM cron_outbox
                   WHERE job_id=? AND state IN ('pending','abandoned')
                   LIMIT 1""",
                (str(row["job_id"]),),
            ).fetchone()
            if unsettled_sibling is None:
                # Recover only after every destination for the job has
                # settled. Execution incidents are a separate category.
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
        sql = (
            "SELECT * FROM cron_outbox WHERE state='pending' "
            "AND (delivery_contract=? OR next_attempt_at IS NULL OR next_attempt_at<=?) "
            "ORDER BY created_at ASC, id ASC LIMIT ?"
        )
        params = (
            EXACT_QUEUE,
            _hermes_now().isoformat(),
            max(1, min(int(limit), 100)),
        )
        rows = conn.execute(sql, params).fetchall()
        late_legacy_ids = [row["id"] for row in rows if row["for_failure"] is None]
        for outbox_id in late_legacy_ids:
            _materialize_failure_lanes(conn, str(outbox_id))
        if late_legacy_ids:
            rows = conn.execute(sql, params).fetchall()
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
            "SELECT * FROM cron_outbox" + where + " ORDER BY created_at DESC, id DESC",
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
