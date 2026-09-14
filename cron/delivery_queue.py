"""Profile-local durable handoff for cron delivery through live gateway adapters.

Legacy queue rows remain in ``deliveries.db`` for compatibility. Exact-target
rows are owned entirely by ``executions.db`` through :mod:`cron.outbox`, so a
claim, attempt, incident, execution projection, and job projection share one
SQLite commit point.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from agent.redact import redact_sensitive_text
from cron.executions import _owner_is_live, _process_start_time
from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

DELIVERY_DB: Optional[Path] = None
_PROCESS_ID = uuid.uuid4().hex
_lock = threading.RLock()
_ACTIVE_DELIVERIES: set[str] = set()
_TERMINAL = ("delivered", "failed", "unknown")
MAX_TERMINAL_DELIVERIES = 1000
DEFAULT_DELIVERY_WAIT_TIMEOUT_SECONDS = 300.0
EXACT_LEASE_SECONDS = 120


def _path() -> Path:
    return DELIVERY_DB or (get_hermes_home().resolve() / "cron" / "deliveries.db")


def _prune_terminal_unlocked(conn: sqlite3.Connection) -> None:
    """Bound only truly terminal legacy rows and obsolete exact rows."""
    terminal = """((delivery_contract=0 AND status IN ('delivered','failed','unknown'))
                 OR (delivery_contract=1 AND state IN ('SUCCEEDED','DEAD','UNKNOWN')))"""
    conn.execute(
        f"""UPDATE deliveries SET job_json='{{}}', content='', destination_json=NULL
            WHERE {terminal}
              AND (job_json != '{{}}' OR content != '' OR destination_json IS NOT NULL)"""
    )
    keep = max(0, int(MAX_TERMINAL_DELIVERIES))
    terminal_count = int(
        conn.execute(f"SELECT COUNT(*) FROM deliveries WHERE {terminal}").fetchone()[0]
    )
    excess = terminal_count - keep
    if excess <= 0:
        return
    conn.execute(
        f"""INSERT OR IGNORE INTO delivery_tombstones
            (execution_id, terminal_status, finished_at)
            SELECT execution_id, status, finished_at FROM deliveries
            WHERE {terminal}
            ORDER BY finished_at, created_at, execution_id LIMIT ?""",
        (excess,),
    )
    conn.execute(
        f"""DELETE FROM deliveries WHERE execution_id IN (
              SELECT execution_id FROM deliveries WHERE {terminal}
              ORDER BY finished_at, created_at, execution_id LIMIT ?
            )""",
        (excess,),
    )


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli.sqlite_util import add_column_if_missing

    conn.execute(
        """CREATE TABLE IF NOT EXISTS deliveries (
             execution_id TEXT PRIMARY KEY,
             job_json TEXT NOT NULL,
             content TEXT NOT NULL,
             destination_json TEXT,
             destination_hash TEXT,
             outbox_id TEXT,
             delivery_contract INTEGER NOT NULL DEFAULT 0,
             generation INTEGER NOT NULL DEFAULT 0,
             state TEXT,
             owner_token TEXT,
             lease_expires_at TEXT,
             outcome_error TEXT,
             for_failure INTEGER NOT NULL DEFAULT 0,
             status TEXT NOT NULL CHECK(status IN
               ('pending','delivering','delivered','failed','unknown')),
             owner_process_id TEXT,
             owner_pid INTEGER,
             owner_started_at INTEGER,
             created_at TEXT NOT NULL,
             finished_at TEXT,
             error TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_tombstones (
             execution_id TEXT PRIMARY KEY,
             terminal_status TEXT NOT NULL CHECK(terminal_status IN
               ('delivered','failed','unknown')),
             finished_at TEXT
           )"""
    )
    additive = (
        ("for_failure", "for_failure INTEGER NOT NULL DEFAULT 0"),
        ("destination_json", "destination_json TEXT"),
        ("outbox_id", "outbox_id TEXT"),
        ("delivery_contract", "delivery_contract INTEGER NOT NULL DEFAULT 0"),
        ("destination_hash", "destination_hash TEXT"),
        ("generation", "generation INTEGER NOT NULL DEFAULT 0"),
        ("state", "state TEXT"),
        ("owner_token", "owner_token TEXT"),
        ("lease_expires_at", "lease_expires_at TEXT"),
        ("outcome_error", "outcome_error TEXT"),
    )
    for column, ddl in additive:
        add_column_if_missing(conn, "deliveries", column, ddl)


def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from hermes_cli.sqlite_util import open_db

    path = _path()
    conn = open_db(path, db_label="cron/deliveries.db", synchronous_full=True, initialize=_initialize_schema)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    # Pruning is done explicitly by the paths that create terminal
    # rows (_finish / recover_abandoned / _terminalize_wait_timeout);
    # read-only polls must not pay for a full-table UPDATE + COUNT.
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect()) as conn:
        yield conn


def _delivery_key(execution_id: str, outbox_id: Optional[str]) -> str:
    return str(outbox_id) if outbox_id is not None else str(execution_id)


def _exact_status(row: dict) -> dict:
    state = str(row.get("exact_state") or "")
    status = {
        "READY": "pending",
        "IN_FLIGHT": "delivering",
        "DELIVERED": "delivered",
        "RETRYABLE_FAILED": "failed",
        "DEAD": "failed",
        "UNKNOWN": "unknown",
    }.get(state, "failed")
    return {
        **row,
        "execution_id": str(row["id"]),
        "outbox_id": str(row["id"]),
        "status": status,
        "state": state,
        "error": row.get("outcome_error") or row.get("last_error"),
    }


def enqueue(
    execution_id: str,
    job: dict,
    content: str,
    *,
    for_failure: bool = False,
    destination: Optional[dict] = None,
    outbox_id: Optional[str] = None,
) -> dict:
    """Persist one idempotent delivery request before the worker waits."""
    if outbox_id is not None:
        if destination is None:
            raise ValueError("exact queued delivery requires a destination")
        from cron.outbox import configure_exact_delivery

        row = configure_exact_delivery(
            str(outbox_id), job=job, content=str(content), destination=destination
        )
        return _exact_status(row)

    delivery_key = str(execution_id)
    destination_json = (
        json.dumps(destination, ensure_ascii=False, sort_keys=True)
        if destination is not None
        else None
    )
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        tombstone = conn.execute(
            """SELECT terminal_status, finished_at FROM delivery_tombstones
               WHERE execution_id=?""",
            (delivery_key,),
        ).fetchone()
        if tombstone is not None:
            return {
                "execution_id": delivery_key,
                "status": tombstone["terminal_status"],
                "finished_at": tombstone["finished_at"],
            }
        conn.execute(
            """INSERT OR IGNORE INTO deliveries
               (execution_id, job_json, content, destination_json, outbox_id,
                delivery_contract, for_failure, status, created_at)
               VALUES (?, ?, ?, ?, NULL, 0, ?, 'pending', ?)""",
            (
                delivery_key,
                json.dumps(job, ensure_ascii=False, sort_keys=True),
                str(content),
                destination_json,
                int(bool(for_failure)),
                _hermes_now().isoformat(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (delivery_key,)
        ).fetchone()
    if row is None:
        raise RuntimeError("legacy delivery vanished inside its transaction")
    return dict(row)


def get_status(execution_id: str, *, outbox_id: Optional[str] = None) -> Optional[dict]:
    exact_id = str(outbox_id or execution_id)
    from cron.outbox import get_exact_state as get_outbox_exact_state

    exact = get_outbox_exact_state(exact_id)
    if exact is not None:
        return _exact_status(exact)
    if outbox_id is not None:
        return None
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=? AND delivery_contract=0",
            (str(execution_id),),
        ).fetchone()
        if row is not None:
            return dict(row)
        tombstone = conn.execute(
            """SELECT execution_id, terminal_status, finished_at
               FROM delivery_tombstones WHERE execution_id=?""",
            (str(execution_id),),
        ).fetchone()
    if tombstone is None:
        return None
    return {
        "execution_id": tombstone["execution_id"],
        "status": tombstone["terminal_status"],
        "finished_at": tombstone["finished_at"],
        "error": None,
    }


def get_exact_state(outbox_id: str) -> Optional[dict]:
    from cron.outbox import get_exact_state as get_outbox_exact_state

    row = get_outbox_exact_state(str(outbox_id))
    return _exact_status(row) if row is not None else None


def reactivate_exact(outbox_id: str) -> bool:
    from cron.outbox import reactivate_exact as reactivate

    return reactivate(str(outbox_id))


def reconcile_exact(outbox_id: str) -> Optional[str]:
    row = get_exact_state(str(outbox_id))
    return str(row["state"]) if row is not None else None


def _hydrate_exact(row: dict) -> dict:
    from cron.outbox import canonical_destination_json, decode_persisted_destination

    if int(row.get("delivery_contract") or 0) != 1:
        raise ValueError("exact delivery contract was downgraded")
    raw_destination = row.get("destination_json")
    if raw_destination is None:
        raise ValueError("exact delivery is missing its destination")
    destination = decode_persisted_destination(raw_destination)
    canonical = canonical_destination_json(destination)
    import hashlib

    if hashlib.sha256(canonical.encode()).hexdigest() != row.get("destination_hash"):
        raise ValueError("exact delivery destination hash mismatch")
    raw_job = row.get("job_json")
    if raw_job is None:
        raise ValueError("exact delivery is missing its job snapshot")
    job = json.loads(raw_job)
    if not isinstance(job, dict) or not job.get("id"):
        raise ValueError("exact delivery job snapshot is invalid")
    return {
        **_exact_status(row),
        "job": job,
        "content": str(row["content"]),
        "destination": destination,
        "for_failure": int(row.get("for_failure") or 0),
    }


def _claim_legacy() -> Optional[dict]:
    pid = os.getpid()
    started = _process_start_time(pid)
    with _transaction() as conn:
        while True:
            row = conn.execute(
                """SELECT * FROM deliveries
                   WHERE status='pending' AND delivery_contract=0
                   ORDER BY created_at, execution_id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            try:
                job = json.loads(result["job_json"])
                destination_json = result.get("destination_json")
                destination = json.loads(destination_json) if destination_json else None
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                safe_error = redact_sensitive_text(
                    f"{type(exc).__name__}: {exc}",
                    force=True,
                    redact_url_credentials=True,
                )
                conn.execute(
                    """UPDATE deliveries
                       SET status='failed', finished_at=?, error=?,
                           job_json='{}', content='', destination_json=NULL
                       WHERE execution_id=? AND status='pending' AND delivery_contract=0""",
                    (_hermes_now().isoformat(), safe_error, row["execution_id"]),
                )
                _prune_terminal_unlocked(conn)
                continue
            cur = conn.execute(
                """UPDATE deliveries SET status='delivering', owner_process_id=?,
                   owner_pid=?, owner_started_at=?
                   WHERE execution_id=? AND status='pending' AND delivery_contract=0""",
                (_PROCESS_ID, pid, started, row["execution_id"]),
            )
            if cur.rowcount != 1:
                continue
            claimed = conn.execute(
                "SELECT * FROM deliveries WHERE execution_id=?", (row["execution_id"],)
            ).fetchone()
            result = dict(claimed)
            result.pop("job_json", None)
            result.pop("destination_json", None)
            result["job"] = job
            result["destination"] = destination
            return result


def claim_next(*, exact_outbox_ids: Optional[Sequence[str]] = None) -> Optional[dict]:
    """Claim exact work from executions.db before legacy queue work."""
    from cron.outbox import claim_exact, finish_exact

    while True:
        exact = claim_exact(
            owner_process_id=_PROCESS_ID,
            owner_pid=os.getpid(),
            owner_started_at=_process_start_time(os.getpid()),
            outbox_ids=exact_outbox_ids,
        )
        if exact is None:
            return None if exact_outbox_ids is not None else _claim_legacy()
        delivery_id = str(exact["id"])
        _ACTIVE_DELIVERIES.add(delivery_id)
        try:
            return _hydrate_exact(exact)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            finish_exact(
                delivery_id,
                generation=int(exact["generation"]),
                owner_token=str(exact["owner_token"]),
                error=f"{type(exc).__name__}: {exc}",
                permanent=True,
            )
            _ACTIVE_DELIVERIES.discard(delivery_id)


def _finish(
    execution_id: str,
    *,
    error: Optional[str],
    generation: Optional[int] = None,
    owner_token: Optional[str] = None,
    permanent: bool = False,
    transport_status: Optional[str] = None,
    receipt_id: Optional[str] = None,
) -> bool:
    if generation is not None or owner_token is not None:
        if generation is None or owner_token is None:
            return False
        from cron.outbox import finish_exact

        return finish_exact(
            str(execution_id),
            generation=int(generation),
            owner_token=str(owner_token),
            error=error,
            permanent=permanent,
            transport_status=transport_status,
            receipt_id=receipt_id,
        )
    safe_error = (
        redact_sensitive_text(str(error), force=True, redact_url_credentials=True)
        if error
        else None
    )
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE deliveries SET status=?, finished_at=?, error=?
               WHERE execution_id=? AND status='delivering'
                 AND owner_process_id=? AND owner_pid=? AND delivery_contract=0""",
            (
                "failed" if error else "delivered",
                _hermes_now().isoformat(),
                safe_error,
                str(execution_id),
                _PROCESS_ID,
                os.getpid(),
            ),
        )
        _prune_terminal_unlocked(conn)
    return cur.rowcount == 1


def recover_abandoned() -> int:
    """Release dead exact claims and expired inactive claims from this process."""
    from cron.outbox import recover_exact_abandoned

    changed = recover_exact_abandoned(
        _owner_is_live,
        local_process_id=_PROCESS_ID,
        active_outbox_ids=tuple(_ACTIVE_DELIVERIES),
    )
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT execution_id, owner_process_id, owner_pid, owner_started_at
               FROM deliveries WHERE status='delivering' AND delivery_contract=0"""
        ).fetchall()
        for row in rows:
            same_process = row["owner_process_id"] == _PROCESS_ID
            if same_process and row["execution_id"] in _ACTIVE_DELIVERIES:
                continue
            if not same_process and _owner_is_live(
                int(row["owner_pid"]), row["owner_started_at"]
            ):
                continue
            error = (
                "Gateway finished delivery but could not persist its outcome; send was not retried."
                if same_process
                else "Gateway exited during delivery; send outcome is unknown and was not retried."
            )
            cur = conn.execute(
                """UPDATE deliveries SET status='unknown', finished_at=?, error=?
                   WHERE execution_id=? AND status='delivering' AND delivery_contract=0""",
                (_hermes_now().isoformat(), error, row["execution_id"]),
            )
            changed += cur.rowcount
        # Obsolete exact rows in deliveries.db are never transport candidates.
        conn.execute(
            """UPDATE deliveries
               SET status='unknown', state='UNKNOWN', finished_at=?,
                   error='Exact delivery migrated to executions.db; legacy row was not replayed.',
                   job_json='{}', content='', destination_json=NULL
               WHERE delivery_contract=1 AND state NOT IN ('SUCCEEDED','DEAD','UNKNOWN')""",
            (_hermes_now().isoformat(),),
        )
        _prune_terminal_unlocked(conn)
    return changed


def _start_lease_renewer(
    row: dict,
) -> tuple[threading.Event, Optional[threading.Thread]]:
    if int(row.get("delivery_contract") or 0) != 1:
        return threading.Event(), None
    from cron.outbox import renew_exact_lease

    stop = threading.Event()

    def renew() -> None:
        interval = max(1.0, EXACT_LEASE_SECONDS / 3)
        while not stop.wait(interval):
            if not renew_exact_lease(
                str(row["outbox_id"]),
                generation=int(row["generation"]),
                owner_token=str(row["owner_token"]),
            ):
                return

    worker = threading.Thread(target=renew, name="cron-delivery-lease", daemon=True)
    worker.start()
    return stop, worker


def _bot_admission(row: dict) -> tuple[Optional[str], Optional[str]]:
    destination = row.get("destination")
    if not isinstance(destination, dict) or destination.get("platform") != "bot-chat":
        return None, None
    receipts = row["job"].get("_bot_chat_delivery_receipts", {})
    if not isinstance(receipts, dict):
        return None, None
    for receipt in receipts.values():
        if isinstance(receipt, dict) and receipt.get("status") in {"queued", "claimed"}:
            return str(receipt["status"]), str(receipt.get("delivery_id") or "")
    return None, None


def reconcile_admitted() -> int:
    """Settle admitted Bot Chat rows from their durable owner receipts."""
    from cron.outbox import (
        admitted_exact_deliveries,
        decode_persisted_destination,
        settle_admitted_delivery,
    )
    from hermes_cli.profiles import get_profile_dir
    from tools.bot_live_delivery import read_delivery_result

    settled = 0
    for row in admitted_exact_deliveries():
        try:
            destination = decode_persisted_destination(row["destination_json"])
            profile = str(destination["chat_id"])
            home = (
                get_profile_dir(profile).resolve()
                if profile
                else get_hermes_home().resolve()
            )
            receipt_id = str(row["receipt_id"])
            receipt = read_delivery_result(home, receipt_id)
            if not isinstance(receipt, dict):
                continue
            receipt_status = str(receipt.get("status") or "")
            if receipt_status in {
                "settled",
                "failed",
                "cancelled",
                "ambiguous",
            } and settle_admitted_delivery(
                str(row["id"]),
                receipt_id=receipt_id,
                receipt_status=receipt_status,
                receipt_error=str(receipt.get("error") or ""),
                receipt_reason=str(receipt.get("reason") or ""),
            ):
                settled += 1
        except Exception:
            logger.debug(
                "Could not reconcile admitted exact delivery %s",
                row.get("id"),
                exc_info=True,
            )
    return settled


def drain(
    send: Callable[..., Optional[str]],
    *,
    limit: int = 20,
    exact_outbox_ids: Optional[Sequence[str]] = None,
) -> int:
    """Send claimed work and CAS every exact mutation to its owner generation."""
    reconcile_admitted()
    recover_abandoned()
    processed = 0
    for _ in range(max(0, limit)):
        row = claim_next(exact_outbox_ids=exact_outbox_ids)
        if row is None:
            break
        delivery_id = str(row["execution_id"])
        _ACTIVE_DELIVERIES.add(delivery_id)
        stop, renewer = _start_lease_renewer(row)
        try:
            exact_kwargs = {}
            content = row["content"]
            temporary: Optional[tempfile.TemporaryDirectory[str]] = None
            try:
                if int(row.get("delivery_contract") or 0) == 1:
                    from cron.outbox import materialize_exact_content

                    row["job"]["_exact_delivery_attempt_id"] = (
                        f"{row['outbox_id']}:{row['generation']}"
                    )
                    temporary = tempfile.TemporaryDirectory(
                        prefix="hermes-cron-delivery-"
                    )
                    try:
                        content = materialize_exact_content(
                            str(row["outbox_id"]), Path(temporary.name)
                        )
                    except BaseException as exc:
                        if not _finish(
                            delivery_id,
                            error=f"{type(exc).__name__}: {exc}",
                            generation=int(row["generation"]),
                            owner_token=str(row["owner_token"]),
                        ):
                            raise RuntimeError(
                                "exact delivery preparation failure was not persisted"
                            ) from exc
                        raise
                    exact_kwargs = {
                        "destination": row["destination"],
                        "outbox_id": row["outbox_id"],
                    }
                try:
                    error = send(
                        row["job"], content, bool(row["for_failure"]), **exact_kwargs
                    )
                except BaseException as exc:
                    if int(row.get("delivery_contract") or 0) == 1:
                        from cron.outbox import release_exact_claim

                        release_exact_claim(
                            str(row["outbox_id"]),
                            generation=int(row["generation"]),
                            owner_token=str(row["owner_token"]),
                        )
                    else:
                        _finish(delivery_id, error=f"{type(exc).__name__}: {exc}")
                    raise
            finally:
                if temporary is not None:
                    temporary.cleanup()
            transport_status, receipt_id = _bot_admission(row)
            if not _finish(
                delivery_id,
                error=error,
                generation=(
                    int(row["generation"])
                    if int(row.get("delivery_contract") or 0) == 1
                    else None
                ),
                owner_token=(
                    str(row["owner_token"])
                    if int(row.get("delivery_contract") or 0) == 1
                    else None
                ),
                transport_status=transport_status,
                receipt_id=receipt_id,
            ):
                raise RuntimeError("delivery outcome was not persisted")
            if error and int(row.get("delivery_contract") or 0) == 1:
                thread_updates = row["job"].pop("_exact_thread_updates", {})
                opened_thread_id = (
                    thread_updates.get(str(row["outbox_id"]))
                    if isinstance(thread_updates, dict)
                    else None
                )
                if opened_thread_id:
                    from cron.outbox import update_destination_thread

                    updated = update_destination_thread(
                        str(row["outbox_id"]), str(opened_thread_id)
                    )
                    if updated is not None:
                        reactivate_exact(str(row["outbox_id"]))
        finally:
            stop.set()
            if renewer is not None:
                renewer.join(timeout=1)
            _ACTIVE_DELIVERIES.discard(delivery_id)
        processed += 1
    return processed


def _terminalize_wait_timeout(
    execution_id: str, *, pending_is_error: bool = False
) -> str:
    row = get_status(str(execution_id))
    if row is None:
        return "timed out waiting for live gateway delivery"
    if row["status"] == "pending":
        message = "no live gateway within the wait budget; delivery remains queued"
        return message if pending_is_error else ""
    if row["status"] == "delivering":
        if int(row.get("delivery_contract") or 0) == 0:
            message = "delivery outcome is unknown after wait timeout"
            with _transaction() as conn:
                conn.execute(
                    """UPDATE deliveries
                       SET status='unknown', finished_at=?, error=?,
                           job_json='{}', content='', destination_json=NULL
                       WHERE execution_id=? AND status='delivering'
                         AND delivery_contract=0 AND owner_process_id=? AND owner_pid=?""",
                    (
                        _hermes_now().isoformat(),
                        message,
                        str(execution_id),
                        _PROCESS_ID,
                        os.getpid(),
                    ),
                )
                _prune_terminal_unlocked(conn)
            return message
        return "timed out observing gateway delivery; queue attempt remains in flight"
    if row["status"] == "delivered":
        return ""
    return str(row.get("error") or f"delivery {row['status']}")


def enqueue_and_wait(
    execution_id: str,
    job: dict,
    content: str,
    *,
    for_failure: bool = False,
    timeout: Optional[float] = None,
    destination: Optional[dict] = None,
    outbox_id: Optional[str] = None,
) -> Optional[str]:
    """Queue delivery and wait for its terminal or admitted state."""
    delivery_key = _delivery_key(execution_id, outbox_id)
    queued = enqueue(
        execution_id,
        job,
        content,
        for_failure=for_failure,
        destination=destination,
        outbox_id=outbox_id,
    )
    if queued["status"] in _TERMINAL:
        return (
            None
            if queued["status"] == "delivered"
            else str(queued.get("error") or f"delivery {queued['status']}")
        )
    wait_timeout = (
        DEFAULT_DELIVERY_WAIT_TIMEOUT_SECONDS if timeout is None else max(0.0, timeout)
    )
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        row = get_status(delivery_key)
        if row and (
            row["status"] in _TERMINAL
            or row.get("state") == "IN_FLIGHT"
            and row.get("transport_status")
        ):
            return (
                None
                if row["status"] == "delivered"
                else str(row.get("error") or f"delivery {row['status']}")
            )
        time.sleep(1.0)
    return (
        _terminalize_wait_timeout(delivery_key, pending_is_error=outbox_id is not None)
        or None
    )
