"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue. Interrupted attempts
become ``unknown`` only after their owner process is proved gone — a start-time reading that fails
to match the claim-time fingerprint is not proof of death. Terminal states are immutable.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from cron.constants import CLAIM_TTL_INACTIVITY_HEADROOM
from hermes_cli.observability.shared_metrics_gateway import record_cron_finish

# Optional test override. Production resolves the path at transaction time so dashboard operations
# that temporarily enter another profile cannot leak that profile's records into the import-time
# home.
EXECUTIONS_FILE: Optional[Path] = None
# Default retention: each job keeps its newest PER_JOB_TERMINAL_EXECUTIONS terminal attempts, and
# no terminal attempt that finished inside TERMINAL_RETENTION_FLOOR_DAYS is pruned. Neither is a
# ledger-wide bound.
PER_JOB_TERMINAL_EXECUTIONS = 1000
TERMINAL_RETENTION_FLOOR_DAYS = 30
# Optional ledger-wide ceiling on terminal attempts: a separate quantity from the per-job quota,
# and None (shipped) means there is none. It is the ceiling a profile takes when its config.yaml
# does not carry ``cron.max_terminal_executions`` at all; a configured value, explicit null
# included, always wins (``terminal_execution_ceiling``). Upstream used this name as an
# unconditional bound of 1000. A number here overrides the floor and deletes recent evidence
# across jobs as soon as the whole ledger passes it, so it stays None unless that loss is the
# intent; the shipped ``DEFAULT_CONFIG`` value is null for the same reason.
MAX_TERMINAL_EXECUTIONS: Optional[int] = None
HANDOFF_ADOPTION_GRACE_SECONDS = 30.0
# Floor for the live-owner stale-claim bound (#115692); see _live_owner_stale_after_seconds.
LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS = 7200.0
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex
logger = logging.getLogger(__name__)
_CEILING_KEY = "max_terminal_executions"
# (source, problem) pairs already reported: retention runs on every terminal write, and a bad
# value should warn once per profile (or once for the fallback constant), not once per finished
# job.
_reported_ceiling_problems: set[tuple[str, str]] = set()


# --- executions ledger --------------------------------------------------------------------------


def _ledger_path() -> Path:
    """Ledger file for this execution context; the one resolution shared by the connection and
    by the retention policy lookup, so the two can never name different profiles."""
    return EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")


def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from cron.jobs import _ensure_cron_dir
    from hermes_cli.sqlite_util import open_db

    path = _ledger_path()
    _ensure_cron_dir(path.parent)
    return open_db(
        path,
        db_label="cron/executions.db",
        synchronous_full=True,
        initialize=_initialize_schema,
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
     lease_expires_at TEXT,
     delivery_outcome TEXT,
     scheduled_instant TEXT
   )"""

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
    ("delivery_outcome", "TEXT"),
    ("scheduled_instant", "TEXT"),
)


def _initialize_schema(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_occurrence "
        "ON executions(job_id, scheduled_instant) WHERE status='completed'"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect()) as conn:
        # Rows are addressed by column name; a connection from a test or a future ``_connect``
        # that does not set the factory must not turn every lookup into a tuple index.
        conn.row_factory = sqlite3.Row
        yield conn


def _fetch(conn: sqlite3.Connection, execution_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM executions WHERE id=?", (execution_id,)
    ).fetchone()
    return dict(row) if row is not None else None


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
    if current is None:
        return True  # cannot compare -> cannot prove death; a misread must not rewrite state
    # Drifted same-host readings (#117505) are not proof of death; a live misread is still
    # bounded by the stale-claim sweep below.
    from gateway.status import start_time_fingerprints_match

    return start_time_fingerprints_match(started_at, current)


def _live_owner_stale_after_seconds() -> Optional[float]:
    """Age past which a claimed/running row with a LIVE owner is treated as wedged.

    Derived from the existing knobs, never a bare wall-clock constant:
    ``max(3 × HERMES_CRON_TIMEOUT, cron script timeout, 7200)``. Returns ``None`` (never reclaim
    live owners — today's behaviour) when the inactivity timeout is 0/unlimited or not a finite
    positive number: with no bound to derive from, fail closed.
    """
    from cron.scheduler import _cron_inactivity_seconds
    from cron.scheduler_script import _get_script_timeout

    inactivity = float(_cron_inactivity_seconds())
    if not math.isfinite(inactivity) or inactivity <= 0:
        return None
    return max(
        inactivity * CLAIM_TTL_INACTIVITY_HEADROOM,
        float(_get_script_timeout()),
        LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS,
    )


def _claim_age_seconds(claimed_at: str) -> float:
    """Seconds since ``claimed_at`` (NOT NULL, always the aware ISO string from hermes_time.now)."""
    return (_hermes_now() - datetime.fromisoformat(claimed_at)).total_seconds()


def _report_ceiling_problem(source: str, problem: str) -> None:
    key = (source, problem)
    if key in _reported_ceiling_problems:
        return
    _reported_ceiling_problems.add(key)
    logger.warning(
        "Ignoring %s: %s. It must be a non-negative integer or null; until it is fixed no "
        "ledger-wide ceiling is applied and no terminal execution history is pruned.",
        source,
        problem,
    )


def _retention_policy() -> Tuple[bool, Optional[int]]:
    """``(usable, ceiling)`` for the profile that owns the active ledger: whether its retention
    policy could be established, and the ledger-wide ceiling it sets (``None`` for none).

    ``usable`` is False for every problem ``terminal_execution_ceiling`` reports, and
    ``_prune_unlocked`` then deletes nothing at all. Never raises for a configuration problem:
    it runs inside the terminal write's transaction, which must still commit.
    """
    ledger = _ledger_path()
    value: Any = MAX_TERMINAL_EXECUTIONS
    source = "cron.executions.MAX_TERMINAL_EXECUTIONS"
    if ledger.parent.name == "cron":
        config_path = ledger.parent.parent / "config.yaml"
        configured = f"cron.{_CEILING_KEY} in {config_path}"
        try:
            from hermes_cli.config_effective import load_user_config_effective

            # This reader does not merge DEFAULT_CONFIG, so an absent key stays distinguishable
            # from an explicit null. strict_section: the user's file and the managed one are each
            # judged as written, before either is normalized or merged. A file that cannot be
            # read or parsed, a root that is not a mapping or a ``cron`` section written as
            # anything but a mapping must raise here, from either layer, rather than be served
            # as (or overlaid into) a mapping that reads as "key absent".
            effective = load_user_config_effective(
                config_path, fail_closed=True, strict_section="cron"
            )
        except Exception as exc:
            _report_ceiling_problem(
                configured, f"configuration unreadable ({type(exc).__name__})"
            )
            return False, None
        # Each layer's own section is a mapping or missing by now, so the merged one is too.
        cron_cfg = effective.get("cron", {})
        if _CEILING_KEY in cron_cfg:
            value, source = cron_cfg[_CEILING_KEY], configured
    if value is None:
        return True, None
    # type() rather than isinstance(): bool subclasses int, and ``true`` must not become 1.
    if type(value) is not int or value < 0:
        # The value itself is never rendered: repr() of a Python int past the int/str digit
        # limit (and of any list or mapping holding one) raises ValueError, which would escape
        # this policy read and roll back the terminal write that ran it.
        _report_ceiling_problem(source, "invalid value")
        return False, None
    return True, value


def terminal_execution_ceiling() -> Optional[int]:
    """Ledger-wide ceiling on terminal attempts for the profile that owns the active ledger.

    ``cron.max_terminal_executions`` is read at call time from the ``config.yaml`` beside the
    ledger's own ``cron/`` directory, never from the process environment or an import-time value:
    one process ticks several profiles, and a ceiling read from the launch profile would delete
    another profile's history. The configured key wins whenever the file carries it, explicit
    null included. ``MAX_TERMINAL_EXECUTIONS`` applies only when the key is absent, or when the
    ledger was re-pointed outside a ``<home>/cron/`` layout and so has no owning profile.

    ``None`` means no ceiling. Only a literal non-negative integer enables one. A bool, float,
    string (numeric or not) or negative number, from either source, yields ``None`` with one
    warning, because a mistyped value must retain evidence rather than delete it.

    "The key is absent" is concluded only from a usable structure: no file at all, or a file
    whose root is a mapping and whose ``cron`` section is either missing or a mapping without the
    key. Anything else cannot say whether the key is absent or null, so it yields ``None`` with
    one warning and never falls through to the fallback constant: a file that does not parse or
    cannot be read, a root that is not a mapping (a scalar, a list, an explicit null, an empty
    file), and a ``cron`` section that is present but not a mapping (``cron:`` and
    ``cron: null`` included; a missing section is not the same answer as a null one). The user's
    file is judged as its own parse saw it, before it is normalized or overlaid
    (``strict_section``), so the answer depends neither on which reader warmed the process's
    config caches first nor on a managed layer supplying a mapping where the user's section is
    not one. The managed file is judged the same way, from its own read: no managed file is a
    valid empty layer, but one that is present and unreadable, unparseable or not such a mapping
    is not "no managed layer" here, although it stays exactly that for every ordinary reader.

    Every problem reported here also suspends retention as a whole: ``None`` from an unusable
    policy is not "no ceiling", and ``_prune_unlocked`` deletes nothing until it is fixed.
    """
    return _retention_policy()[1]


# Terminal finish instant as a julian day number; NULL when it cannot be proved.
#   finished_at IS NULL   a legacy row written before the finish stamp existed. The claim stamp
#                         stands in: it is the only instant such a row has.
#   finished_at present   only that stamp counts, and only when it carries a calendar date. An
#                         unreadable one does NOT fall back to the claim: a long-running attempt
#                         claimed 90 days ago may have finished today, so an old claim is not
#                         proof of an old finish.
# The date-shape test exists because julianday() also accepts values this ledger never writes:
# 'now', a bare time ('12:00' is that time on 2000-01-01) and a bare number (a Julian day). Each
# would give damaged evidence a confident, usually ancient, instant. julianday() applies the UTC
# offset, so stamps compare as instants across DST changes and timezone edits.
_FINISH_INSTANT_SQL = """CASE
                    WHEN finished_at IS NULL THEN julianday(claimed_at)
                    WHEN finished_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                    THEN julianday(finished_at)
                END"""
# Every terminal attempt with its rank inside its own job, newest finish first. Only terminal
# states are selected: claimed, running (a leased detached run stays ``running``) and deferred
# rows are obligations, never retention candidates. Under DESC SQLite sorts NULL last, so an
# undatable row never takes a quota slot from a dated one; raw text then id make the order total.
_RANKED_TERMINAL_SQL = f"""SELECT id, {_FINISH_INSTANT_SQL} AS finish_instant,
                COALESCE(finished_at, claimed_at) AS finish_text,
                ROW_NUMBER() OVER (
                    PARTITION BY job_id
                    ORDER BY {_FINISH_INSTANT_SQL} DESC,
                             COALESCE(finished_at, claimed_at) DESC, id DESC
                ) AS job_rank
         FROM executions
         WHERE status IN ({", ".join("?" for _ in _TERMINAL_STATES)})"""


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    """Apply terminal-attempt retention inside the caller's write transaction.

    Default: a row is deleted only when it is beyond its own job's quota AND finished before the
    floor. A row with no provable finish instant (a finish stamp that is present but unreadable,
    or a legacy NULL finish with an unreadable claim) cannot be proved old and is kept. One job's
    volume therefore never evicts another job's evidence, and the ledger has no overall bound.

    With a ceiling enabled (``terminal_execution_ceiling``), the ledger is then cut to that many
    terminal rows regardless of the floor, undatable rows included: enabling it is the explicit
    choice to trade evidence for a bound. Eviction is fair across jobs: rows leave in order of
    per-job rank (the most over-represented job gives up its oldest first), then oldest finish
    instant with undatable rows first, then raw stamp text, then id.

    A policy that cannot be established (``_retention_policy``) deletes nothing, the default
    per-job pruning included: the terminal write that called this still commits, and every row
    stays until the configuration is fixed.
    """
    usable, ceiling = _retention_policy()
    if not usable:
        return
    cutoff = (_hermes_now() - timedelta(days=TERMINAL_RETENTION_FLOOR_DAYS)).isoformat()
    conn.execute(
        f"""DELETE FROM executions WHERE id IN (
              SELECT id FROM ({_RANKED_TERMINAL_SQL})
              WHERE job_rank > ? AND finish_instant < julianday(?)
            )""",
        (*_TERMINAL_STATES, max(0, int(PER_JOB_TERMINAL_EXECUTIONS)), cutoff),
    )
    if ceiling is None:
        return
    # Compared in Python, where the ceiling is an unbounded int: SQLite binds only signed 64-bit
    # integers, so a ceiling at or past 2**63 would raise OverflowError on OFFSET and roll back
    # the terminal write. A ceiling the ledger has not reached deletes nothing, so it is never
    # bound; one below the count is at most the count, which always fits.
    terminal_count = conn.execute(
        f"""SELECT COUNT(*) FROM executions
            WHERE status IN ({", ".join("?" for _ in _TERMINAL_STATES)})""",
        _TERMINAL_STATES,
    ).fetchone()[0]
    if ceiling >= terminal_count:
        return
    conn.execute(
        f"""DELETE FROM executions WHERE id IN (
              SELECT id FROM ({_RANKED_TERMINAL_SQL})
              ORDER BY job_rank ASC, finish_instant DESC, finish_text DESC, id DESC
              LIMIT -1 OFFSET ?
            )""",
        (*_TERMINAL_STATES, ceiling),
    )


def create_execution(
    job_id: str,
    *,
    source: str,
    scheduled_instant: Optional[str] = None,
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
            (
                execution_id,
                str(job_id),
                str(source),
                _PROCESS_ID,
                pid,
                _process_start_time(pid),
                now,
                canonical_instant(scheduled_instant),
            ),
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
    execution_id: str,
    *,
    success: bool,
    error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
    outcome: Optional[str] = None,
    occurrence_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten. ``outcome`` and
    ``occurrence_key`` stamp the typed scheduler evidence when given; existing values are kept."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, handoff_pending=0,
                   handoff_started_at=NULL, delivery_outcome=?,
                   outcome=COALESCE(?, ?), occurrence_key=COALESCE(?, occurrence_key)
               WHERE id=? AND status IN ('claimed','running')
                 AND process_id=? AND pid=?""",
            (
                status,
                now,
                detail,
                delivery_outcome,
                outcome,
                status,
                occurrence_key,
                execution_id,
                _PROCESS_ID,
                os.getpid(),
            ),
        )
        if cur.rowcount != 1:
            return None
        # Read back before retention: a configured ceiling (0, or one already filled by rows with
        # later finish stamps) can evict the row this call just made terminal, and ``None`` here
        # means "this caller did not finish the attempt", which would misreport a durable write.
        record = _fetch(conn, execution_id)
        _prune_unlocked(conn)
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    record_cron_finish(record, delivery_outcome)
    return record


_OWNER_GONE_REASON = (
    "Scheduler restarted after this execution's owner exited before a durable "
    "terminal state; whether side effects ran is unknown."
)
_OWNER_WEDGED_REASON = (
    "Owner process is still alive but the claim outlived the derived stale bound; "
    "treated as wedged (#115692). The process was not terminated; whether side effects "
    "ran is unknown."
)


def recover_interrupted_executions() -> int:
    """Mark abandoned attempts unknown without scheduling retries: rows whose owner is provably
    dead, plus rows whose live owner holds a claim older than the derived stale bound (the
    process is not killed)."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    # Derived on the first live-owned row only: the bound reads config, and the idle gateway
    # tick must stay config-free (tests/cron/test_idle_tick_config_skip.py).
    stale_after: Optional[float] = None
    stale_after_resolved = False
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at, claimed_at, detached_status
               FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if row["detached_status"] == "started":
                # Leased to a detached worker: the launcher's death says nothing about the run.
                # Its lease/report is settled by reconcile_detached_runs, never by owner liveness.
                continue
            reason = _OWNER_GONE_REASON
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                # A live owner is normally a legitimately running job. A worker permanently
                # deadlocked (e.g. futex_wait behind a route/proxy flip, #115692) also passes
                # this check, so a claim older than the derived bound is treated as wedged
                # and released — the external-worker wait loop polls this ledger for a
                # terminal status, so the job can fire again. The wedged worker PROCESS is
                # NOT terminated here (leaked until host restart); rows owned by this process
                # (process_id == _PROCESS_ID, in-process runs) are skipped above and remain
                # out of scope.
                if not stale_after_resolved:
                    stale_after = _live_owner_stale_after_seconds()
                    stale_after_resolved = True
                if (
                    stale_after is None
                    or _claim_age_seconds(row["claimed_at"]) <= stale_after
                ):
                    continue
                reason = _OWNER_WEDGED_REASON
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
                (
                    now,
                    reason,
                    row["id"],
                    row["status"],
                    row["process_id"],
                    row["pid"],
                    row["handoff_pending"],
                    row["handoff_started_at"],
                ),
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


def terminalize_dead_owner(execution_id: str, *, reason: str) -> bool:
    """Record one attempt as ``unknown`` with a cause this process actually observed.

    ``recover_interrupted_executions`` sweeps every attempt whose owner is provably
    dead, and knows nothing but that absence — so all it can write is
    ``_OWNER_GONE_REASON``, which asserts a scheduler restart. A waiter that held the
    worker's ``Popen`` knows more: the owner was that external worker, and it exited
    with a known status. Without this, a manual run whose worker dies is filed as
    "Scheduler restarted ..." (a restart that never happened) and, because the sweep
    leaves the row terminal, the waiter reports success and never records the run —
    the job's ``fire_claim`` then blocks the next manual fire for the whole lease
    (#128509).

    The attempt stays ``unknown``, not ``failed``: whether side effects ran is still
    unknown. Only the CAUSE becomes truthful. Returns False — leaving the caller to
    fall back to the generic sweep — when the row is absent, already terminal, owned by
    this process, inside the handoff adoption grace, or owned by a live process: a
    worker that is still running must never be terminalized out from under itself.
    """
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        row = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at
               FROM executions WHERE id=?""",
            (execution_id,),
        ).fetchone()
        if row is None or row["status"] not in ("claimed", "running"):
            return False
        if row["process_id"] == _PROCESS_ID:
            return False
        if _owner_is_live(int(row["pid"]), row["process_started_at"]):
            return False
        handoff_started_at = row["handoff_started_at"]
        if (
            row["handoff_pending"]
            and handoff_started_at is not None
            and time.time() - float(handoff_started_at) < HANDOFF_ADOPTION_GRACE_SECONDS
        ):
            return False
        cur = conn.execute(
            """UPDATE executions
               SET status='unknown', finished_at=?, error=?,
                   handoff_pending=0, handoff_started_at=NULL
               WHERE id=? AND status=? AND process_id=? AND pid=?""",
            (now, reason, row["id"], row["status"], row["process_id"], row["pid"]),
        )
        if cur.rowcount != 1:
            return False
        record = _fetch(conn, execution_id)
        _prune_unlocked(conn)
    _emit_execution_state(record)
    return True


def list_executions(
    *,
    job_id: Optional[str] = None,
    limit: int = 50,
    before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        # Same (instant, text) key as the ORDER BY, so a page never skips or repeats a row.
        clauses.append("(julianday(claimed_at), claimed_at) < (julianday(?), ?)")
        params.extend([str(before_claimed_at)] * 2)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    # Stamps carry the local offset, which changes at DST and on a timezone change, so text order
    # is not time order. julianday() compares instants (ms); the text breaks same-ms ties.
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions"
            + where
            + " ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT ?",
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


def live_inflight_execution(job_id: str) -> Optional[Dict[str, Any]]:
    """The job's latest attempt while it is still claimed/running under a LIVE owner, else ``None``.

    This is scheduler OWNERSHIP, not recent activity: a run inside a long tool call writes no
    heartbeat yet stays owned, while a run whose process died (watchdog kill, crash) does not.
    Read-only — unlike ``recover_interrupted_executions`` it never rewrites a row.
    """
    record = latest_execution(job_id)
    if not record or record.get("status") not in ("claimed", "running"):
        return None
    if not _owner_is_live(int(record["pid"]), record.get("process_started_at")):
        return None
    return record


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    # One windowed sort: a per-row correlated ORDER BY julianday() cannot use the index and
    # grows quadratically with history (~90 ms at 1000 rows).
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e WHERE e.id IN (
                  SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                             PARTITION BY job_id
                             ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC
                           ) AS rn
                    FROM executions WHERE job_id IN ({placeholders}))
                  WHERE rn=1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}


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
            r[1] for r in conn.execute("PRAGMA table_info(executions_pre_outcomes)")
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
            r[1] for r in conn.execute("PRAGMA table_info(executions_pre_outcomes)")
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


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def defer_execution(
    execution_id: str,
    *,
    reason: str,
    occurrence_key: str,
    retry_at: str,
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
            (
                now,
                str(reason or "") or None,
                str(occurrence_key),
                str(retry_at),
                execution_id,
            ),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _record(
            conn.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
        )
    _emit_execution_state(record)
    return record


def _sanitize_delivery_error(error: Optional[str]) -> Optional[str]:
    """Force-redact a delivery error before it persists in the ledger."""
    if error is None:
        return None
    text = str(error)
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:
        # Fail safe: never persist a string the redactor could not scrub.
        return "[REDACTED - delivery error unavailable]"
    return text[:500]


def record_delivery(
    execution_id: str,
    *,
    target: str,
    status: str,
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
            (str(target), str(status), _sanitize_delivery_error(error), execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _record(
            conn.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
        )
    return record


def register_detached_run(
    execution_id: str,
    *,
    run_id: Optional[str] = None,
    lease_seconds: int = 3600,
    worker: Optional[str] = None,
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
    lease_expires = (_hermes_now() + timedelta(seconds=int(lease_seconds))).isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status='running',
                   started_at=COALESCE(started_at, ?),
                   detached_run_id=?, detached_status='started',
                   detached_worker=?,
                   occurrence_key=COALESCE(?, occurrence_key),
                   lease_expires_at=?
               WHERE id=? AND status IN ('claimed','running')
                 AND detached_run_id IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM executions AS other
                   WHERE other.detached_run_id=?
                     AND other.id<>executions.id
                 )""",
            (
                _hermes_now().isoformat(),
                run_id,
                str(worker) if worker else None,
                str(occurrence_key) if occurrence_key else None,
                lease_expires,
                execution_id,
                run_id,
            ),
        )
        if cur.rowcount != 1:
            return None
        record = _record(
            conn.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
        )
    _emit_execution_state(record)
    return record


def finalize_detached_run(
    run_id: str,
    *,
    success: bool,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """RUN_SUCCEEDED / RUN_FAILED from the detached worker, keyed by run id.

    Writes the detached terminal report while the originating execution stays
    nonterminal until :func:`reconcile_detached_runs` converts it. If the
    scheduler has already terminalized the execution, only detached bookkeeping
    changes; scheduler-owned result evidence remains immutable.
    """
    detached_status = "succeeded" if success else "failed"
    detail = None
    if not success:
        # Worker-supplied failure evidence is force-redacted before it
        # persists: detached workers commonly echo command lines and env
        # fragments that can carry credentials.
        detail = _sanitize_delivery_error(str(error) if error else "unknown failure")
    with _transaction() as conn:
        matches = conn.execute(
            "SELECT id, detached_status FROM executions WHERE detached_run_id=? "
            "ORDER BY id LIMIT 2",
            (str(run_id),),
        ).fetchall()
        if len(matches) != 1 or str(matches[0]["detached_status"]) != "started":
            return None
        execution_id = str(matches[0]["id"])
        cur = conn.execute(
            """UPDATE executions
               SET detached_status=?,
                   error=CASE
                     WHEN status IN ('claimed','running')
                     THEN COALESCE(?, error)
                     ELSE error
                   END
               WHERE id=? AND detached_run_id=? AND detached_status='started'
                 AND NOT EXISTS (
                   SELECT 1 FROM executions AS other
                   WHERE other.detached_run_id=?
                     AND other.id<>executions.id
                 )""",
            (detached_status, detail, execution_id, str(run_id), str(run_id)),
        )
        if cur.rowcount != 1:
            return None
        record = _record(
            conn.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
        )
    _emit_execution_state(record)
    return record


def find_detached_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Idempotent readback: the execution row for one correlation id."""
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions WHERE detached_run_id=? ORDER BY id LIMIT 2",
            (str(run_id),),
        ).fetchall()
    return _record(rows[0]) if len(rows) == 1 else None


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
                cur = conn.execute(
                    """UPDATE executions
                       SET status='failed', outcome='failed', finished_at=?,
                           detached_status='lost', error=?
                       WHERE id=? AND detached_status=? AND detached_run_id=?
                         AND status=?
                         AND NOT EXISTS (
                           SELECT 1 FROM executions AS other
                           WHERE other.detached_run_id=?
                             AND other.id<>executions.id
                         )""",
                    (
                        now_iso,
                        "Detached run lease expired with no terminal report; "
                        "the worker is presumed dead.",
                        row["id"],
                        detached_status,
                        row["detached_run_id"],
                        row["status"],
                        row["detached_run_id"],
                    ),
                )
            elif detached_status == "succeeded":
                cur = conn.execute(
                    """UPDATE executions
                       SET status='completed', outcome='completed',
                           finished_at=?, error=NULL
                       WHERE id=? AND detached_status=? AND detached_run_id=?
                         AND status=?
                         AND NOT EXISTS (
                           SELECT 1 FROM executions AS other
                           WHERE other.detached_run_id=?
                             AND other.id<>executions.id
                         )""",
                    (
                        now_iso,
                        row["id"],
                        detached_status,
                        row["detached_run_id"],
                        row["status"],
                        row["detached_run_id"],
                    ),
                )
            else:  # failed
                cur = conn.execute(
                    """UPDATE executions
                       SET status='failed', outcome='failed', finished_at=?
                       WHERE id=? AND detached_status=? AND detached_run_id=?
                         AND status=?
                         AND NOT EXISTS (
                           SELECT 1 FROM executions AS other
                           WHERE other.detached_run_id=?
                             AND other.id<>executions.id
                         )""",
                    (
                        now_iso,
                        row["id"],
                        detached_status,
                        row["detached_run_id"],
                        row["status"],
                        row["detached_run_id"],
                    ),
                )
            if cur.rowcount != 1:
                continue
            record = _record(
                conn.execute(
                    "SELECT * FROM executions WHERE id=?", (row["id"],)
                ).fetchone()
            )
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
