"""Detached-run ledger: launcher registers, worker finalizes, scheduler reconciles.

Contract under test (recurring-automation P1/P2, requirement 6):

* A launcher registers RUN_STARTED against an execution; the execution
  stays NONTERMINAL until the detached terminal state is reconciled — in
  particular the restart-recovery sweep must NOT rewrite it to ``unknown``
  just because the launcher process died (detaching is the whole point).
* A detached worker finalizes RUN_SUCCEEDED / RUN_FAILED by run id; the
  originating execution still stays nonterminal until the scheduler-facing
  reconcile converts it.
* Reconcile turns succeeded → completed, failed → failed (error visible,
  incident minted), and an expired lease without any terminal report into
  a visible permanent failure (``lost``).
"""

from __future__ import annotations

import sqlite3
from multiprocessing import Event, get_context
from pathlib import Path

import pytest


def _finalize_after_selection(
    db_path: str,
    run_id: str,
    success: bool,
    error: str | None,
    selected: Event,
    finalized: Event,
) -> None:
    assert selected.wait(timeout=10)
    import cron.executions as executions

    executions.EXECUTIONS_FILE = Path(db_path)
    try:
        assert (
            executions.finalize_detached_run(run_id, success=success, error=error)
            is not None
        )
    finally:
        finalized.set()


def _intercept_reconcile_selection(ledger, monkeypatch, callback):
    class _Cursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchall(self):
            rows = self._cursor.fetchall()
            callback(rows)
            return rows

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class _Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if (
                "SELECT * FROM executions" in sql
                and "detached_status IN ('started','succeeded','failed')" in sql
            ):
                return _Cursor(cursor)
            return cursor

    monkeypatch.setattr(
        ledger,
        "_connect",
        lambda: sqlite3.connect(
            ledger.EXECUTIONS_FILE,
            timeout=5,
            factory=_Connection,
        ),
    )


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def _disown(executions, execution_id, dead_pid=99999999):
    """Rewrite an execution's owner to a provably-dead foreign process."""
    conn = sqlite3.connect(executions.EXECUTIONS_FILE)
    try:
        conn.execute(
            "UPDATE executions SET process_id='foreign-proc', pid=?,"
            " process_started_at=1 WHERE id=?",
            (dead_pid, execution_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_registered_detached_run_survives_owner_death(ledger):
    detached = ledger.create_execution("job-det", source="builtin")
    ledger.mark_execution_running(detached["id"])
    record = ledger.register_detached_run(detached["id"], lease_seconds=3600)
    assert record["detached_status"] == "started"
    assert record["detached_run_id"]
    assert record["lease_expires_at"]
    assert record["status"] == "running"  # nonterminal

    control = ledger.create_execution("job-control", source="builtin")
    ledger.mark_execution_running(control["id"])

    _disown(ledger, detached["id"])
    _disown(ledger, control["id"])
    changed = ledger.recover_interrupted_executions()

    # The control row's owner is dead with no detached lease → unknown.
    assert ledger.latest_execution("job-control")["status"] == "unknown"
    # The detached row is leased to a worker the launcher's death says
    # nothing about → untouched.
    assert ledger.latest_execution("job-det")["status"] == "running"
    assert changed == 1


def test_finalize_succeeded_then_reconcile_completes_execution(ledger):
    row = ledger.create_execution("job-ok", source="builtin")
    ledger.mark_execution_running(row["id"])
    record = ledger.register_detached_run(row["id"], lease_seconds=3600)

    finalized = ledger.finalize_detached_run(
        record["detached_run_id"], success=True
    )
    assert finalized["detached_status"] == "succeeded"
    # Still nonterminal until the scheduler reconciles.
    assert finalized["status"] == "running"

    reconciled = ledger.reconcile_detached_runs()
    assert [r["id"] for r in reconciled] == [row["id"]]
    final = ledger.latest_execution("job-ok")
    assert final["status"] == "completed"
    assert final["detached_status"] == "succeeded"


def test_finalize_failed_then_reconcile_is_visible_permanent_failure(
    ledger, tmp_path, monkeypatch
):
    from cron import incidents as I

    row = ledger.create_execution("job-bad", source="builtin")
    ledger.mark_execution_running(row["id"])
    record = ledger.register_detached_run(row["id"], lease_seconds=3600)

    ledger.finalize_detached_run(
        record["detached_run_id"], success=False, error="worker OOM-killed"
    )
    ledger.reconcile_detached_runs()

    final = ledger.latest_execution("job-bad")
    assert final["status"] == "failed"
    assert "OOM" in final["error"]
    assert final["detached_status"] == "failed"
    # Permanent failure is incident-visible.
    assert I.count_incidents() == 1


@pytest.mark.parametrize(
    (
        "scheduler_success",
        "scheduler_error",
        "worker_success",
        "worker_error",
        "detached_status",
    ),
    [
        pytest.param(True, None, True, None, "succeeded", id="late-success"),
        pytest.param(
            False,
            "authoritative scheduler failure",
            False,
            "late worker failure",
            "failed",
            id="late-failure",
        ),
    ],
)
def test_late_detached_finalization_preserves_scheduler_terminal_evidence(
    ledger,
    scheduler_success,
    scheduler_error,
    worker_success,
    worker_error,
    detached_status,
):
    row = ledger.create_execution("job-late", source="builtin")
    ledger.mark_execution_running(row["id"])
    registered = ledger.register_detached_run(row["id"], run_id="corr-late")
    ledger.finish_execution(
        row["id"],
        success=scheduler_success,
        error=scheduler_error,
        delivery_outcome="authoritative delivery outcome",
        outcome="authoritative scheduler outcome",
        occurrence_key="authoritative occurrence",
    )
    ledger.record_delivery(
        row["id"],
        target="authoritative target",
        status="authoritative delivery status",
        error="authoritative delivery error",
    )
    before = ledger.get_execution(row["id"])

    finalized = ledger.finalize_detached_run(
        registered["detached_run_id"],
        success=worker_success,
        error=worker_error,
    )

    after = ledger.get_execution(row["id"])
    assert finalized == after
    assert after["detached_status"] == detached_status
    assert {key: value for key, value in after.items() if key != "detached_status"} == {
        key: value for key, value in before.items() if key != "detached_status"
    }


def test_expired_lease_without_report_reconciles_as_lost_failure(ledger):
    from cron import incidents as I

    row = ledger.create_execution("job-lost", source="builtin")
    ledger.mark_execution_running(row["id"])
    ledger.register_detached_run(row["id"], lease_seconds=-1)

    reconciled = ledger.reconcile_detached_runs()
    assert [r["id"] for r in reconciled] == [row["id"]]
    final = ledger.latest_execution("job-lost")
    assert final["status"] == "failed"
    assert final["detached_status"] == "lost"
    assert "lease" in (final["error"] or "").lower()
    assert I.count_incidents() == 1


@pytest.mark.parametrize(
    ("success", "worker_error", "terminal_status", "incident_count"),
    [
        (True, None, "completed", 0),
        (False, "worker evidence", "failed", 1),
    ],
)
def test_worker_report_racing_expired_reconciliation_wins_when_committed_first(
    ledger,
    monkeypatch,
    success,
    worker_error,
    terminal_status,
    incident_count,
):
    from cron import incidents as I

    row = ledger.create_execution("job-race", source="builtin")
    ledger.mark_execution_running(row["id"])
    registered = ledger.register_detached_run(
        row["id"], run_id="corr-race", lease_seconds=-1
    )

    ctx = get_context("spawn")
    selected = ctx.Event()
    finalized = ctx.Event()
    worker = ctx.Process(
        target=_finalize_after_selection,
        args=(
            str(ledger.EXECUTIONS_FILE),
            registered["detached_run_id"],
            success,
            worker_error,
            selected,
            finalized,
        ),
    )

    def _wait_for_worker_report(_rows):
        selected.set()
        assert finalized.wait(timeout=10)

    _intercept_reconcile_selection(ledger, monkeypatch, _wait_for_worker_report)

    worker.start()
    raced = ledger.reconcile_detached_runs()
    worker.join(timeout=10)
    assert worker.exitcode == 0
    assert raced == []

    reported = ledger.latest_execution("job-race")
    assert reported["status"] == "running"
    assert reported["detached_status"] == ("succeeded" if success else "failed")
    if worker_error:
        assert reported["error"] == worker_error
    assert I.count_incidents() == 0

    reconciled = ledger.reconcile_detached_runs()
    assert [record["id"] for record in reconciled] == [row["id"]]
    final = ledger.latest_execution("job-race")
    assert final["status"] == terminal_status
    assert final["detached_status"] != "lost"
    if worker_error:
        assert final["error"] == worker_error
    assert I.count_incidents() == incident_count


def test_unknown_run_id_finalize_is_a_noop(ledger):
    assert ledger.finalize_detached_run("no-such-run", success=True) is None


def test_register_detached_run_rejects_duplicate_correlation_id(ledger):
    first = ledger.create_execution("job-first", source="builtin")
    second = ledger.create_execution("job-second", source="builtin")
    ledger.mark_execution_running(first["id"])
    ledger.mark_execution_running(second["id"])

    assert ledger.register_detached_run(first["id"], run_id="corr-duplicate")
    assert ledger.register_detached_run(second["id"], run_id="corr-duplicate") is None
    assert ledger.latest_execution("job-second")["detached_run_id"] is None


def test_register_detached_run_cannot_replace_existing_correlation_id(ledger):
    execution = ledger.create_execution("job-reregister", source="builtin")
    ledger.mark_execution_running(execution["id"])

    first = ledger.register_detached_run(execution["id"], run_id="run-first")
    assert first is not None
    assert ledger.register_detached_run(execution["id"], run_id="run-second") is None
    first_lookup = ledger.find_detached_run("run-first")
    assert first_lookup is not None
    assert first_lookup["id"] == execution["id"]
    assert ledger.find_detached_run("run-second") is None


def test_reconcile_duplicate_inserted_after_selection_is_side_effect_free(
    ledger, monkeypatch
):
    from cron import incidents as I

    selected = ledger.create_execution("job-selected", source="builtin")
    duplicate = ledger.create_execution("job-duplicate", source="builtin")
    ledger.mark_execution_running(selected["id"])
    ledger.mark_execution_running(duplicate["id"])
    ledger.register_detached_run(
        selected["id"], run_id="corr-late-duplicate", lease_seconds=-1
    )
    injected = False

    def _insert_duplicate(rows):
        nonlocal injected
        if injected:
            return
        conn = sqlite3.connect(ledger.EXECUTIONS_FILE)
        try:
            conn.execute(
                "UPDATE executions SET detached_run_id=?, "
                "detached_status='started', lease_expires_at=? WHERE id=?",
                (
                    "corr-late-duplicate",
                    rows[0]["lease_expires_at"],
                    duplicate["id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        injected = True

    _intercept_reconcile_selection(ledger, monkeypatch, _insert_duplicate)

    assert ledger.reconcile_detached_runs() == []
    first_sweep = ledger.list_executions(limit=10)
    correlated = [
        row for row in first_sweep if row["detached_run_id"] == "corr-late-duplicate"
    ]
    assert len(correlated) == 2
    assert {row["status"] for row in correlated} == {"running"}
    assert {row["detached_status"] for row in correlated} == {"started"}
    assert I.count_incidents() == 0

    assert ledger.reconcile_detached_runs() == []
    assert I.count_incidents() == 0


def test_finalize_duplicate_correlation_id_is_side_effect_free(ledger):
    first = ledger.create_execution("job-first", source="builtin")
    second = ledger.create_execution("job-second", source="builtin")
    ledger.mark_execution_running(first["id"])
    ledger.mark_execution_running(second["id"])
    conn = sqlite3.connect(ledger.EXECUTIONS_FILE)
    try:
        conn.execute(
            "UPDATE executions SET detached_run_id='corr-corrupt', "
            "detached_status='started' WHERE id IN (?, ?)",
            (first["id"], second["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    assert ledger.find_detached_run("corr-corrupt") is None
    assert ledger.finalize_detached_run("corr-corrupt", success=True) is None
    rows = ledger.list_executions(limit=10)
    duplicates = [row for row in rows if row["detached_run_id"] == "corr-corrupt"]
    assert len(duplicates) == 2
    assert {row["detached_status"] for row in duplicates} == {"started"}


def test_finalize_mixed_status_duplicate_is_side_effect_free(ledger):
    first = ledger.create_execution("job-finalized", source="builtin")
    second = ledger.create_execution("job-started", source="builtin")
    ledger.mark_execution_running(first["id"])
    ledger.mark_execution_running(second["id"])
    conn = sqlite3.connect(ledger.EXECUTIONS_FILE)
    try:
        conn.execute(
            "UPDATE executions SET detached_run_id='corr-mixed', "
            "detached_status='succeeded' WHERE id=?",
            (first["id"],),
        )
        conn.execute(
            "UPDATE executions SET detached_run_id='corr-mixed', "
            "detached_status='started' WHERE id=?",
            (second["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    assert ledger.finalize_detached_run("corr-mixed", success=False) is None
    rows = ledger.list_executions(limit=10)
    duplicates = [row for row in rows if row["detached_run_id"] == "corr-mixed"]
    assert {row["detached_status"] for row in duplicates} == {
        "started",
        "succeeded",
    }


def test_finalize_duplicate_inserted_after_selection_is_side_effect_free(
    ledger, monkeypatch
):
    selected = ledger.create_execution("job-selected", source="builtin")
    duplicate = ledger.create_execution("job-duplicate", source="builtin")
    ledger.mark_execution_running(selected["id"])
    ledger.mark_execution_running(duplicate["id"])
    ledger.register_detached_run(selected["id"], run_id="corr-finalize-late")
    injected = False

    class _Cursor:
        def __init__(self, cursor, connection):
            self._cursor = cursor
            self._connection = connection

        def fetchall(self):
            nonlocal injected
            rows = self._cursor.fetchall()
            if not injected:
                self._connection.execute(
                    "UPDATE executions SET detached_run_id=?, "
                    "detached_status='started' WHERE id=?",
                    ("corr-finalize-late", duplicate["id"]),
                )
                injected = True
            return rows

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class _Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if (
                "SELECT id, detached_status FROM executions" in sql
                and "detached_run_id=?" in sql
            ):
                return _Cursor(cursor, self)
            return cursor

    monkeypatch.setattr(
        ledger,
        "_connect",
        lambda: sqlite3.connect(
            ledger.EXECUTIONS_FILE,
            timeout=5,
            factory=_Connection,
        ),
    )

    assert ledger.finalize_detached_run("corr-finalize-late", success=True) is None
    rows = ledger.list_executions(limit=10)
    duplicates = [row for row in rows if row["detached_run_id"] == "corr-finalize-late"]
    assert len(duplicates) == 2
    assert {row["detached_status"] for row in duplicates} == {"started"}


def test_incident_write_failure_rolls_back_detached_terminalization(
    ledger, monkeypatch
):
    """Terminalization + incident are ONE transaction: an incident-write
    failure must not leave a terminal execution with no incident. The whole
    reconcile rolls back, the failure surfaces to the caller, and a later
    reconcile (writer healed) converges."""
    from cron import incidents as I

    row = ledger.create_execution("job-atomic", source="builtin")
    ledger.mark_execution_running(row["id"])
    record = ledger.register_detached_run(row["id"], lease_seconds=3600)
    ledger.finalize_detached_run(
        record["detached_run_id"], success=False, error="worker crashed"
    )

    real_upsert = I.upsert_incident_in
    outage = {"active": True}

    def _flaky(conn, *args, **kwargs):
        if outage["active"]:
            raise RuntimeError("incident store unavailable")
        return real_upsert(conn, *args, **kwargs)

    monkeypatch.setattr(I, "upsert_incident_in", _flaky)
    with pytest.raises(RuntimeError, match="incident store unavailable"):
        ledger.reconcile_detached_runs()

    # Rolled back: still nonterminal, worker report retained, no incident.
    partial = ledger.latest_execution("job-atomic")
    assert partial["status"] == "running"
    assert partial["detached_status"] == "failed"
    assert I.count_incidents() == 0

    outage["active"] = False
    reconciled = ledger.reconcile_detached_runs()
    assert [r["id"] for r in reconciled] == [row["id"]]
    assert ledger.latest_execution("job-atomic")["status"] == "failed"
    assert I.count_incidents() == 1


def test_register_detached_run_stores_worker_and_occurrence(ledger):
    row = ledger.create_execution("job-meta", source="builtin")
    ledger.mark_execution_running(row["id"])
    record = ledger.register_detached_run(
        row["id"],
        run_id="corr-77",
        lease_seconds=600,
        worker="update-runner@host:4242",
        occurrence_key="job-meta:2026-08-29T09:00:00+00:00",
    )
    assert record["detached_run_id"] == "corr-77"
    assert record["detached_worker"] == "update-runner@host:4242"
    assert record["occurrence_key"] == "job-meta:2026-08-29T09:00:00+00:00"
    assert record["status"] == "running"


def test_finalize_redacts_failure_evidence(ledger):
    row = ledger.create_execution("job-secret", source="builtin")
    ledger.mark_execution_running(row["id"])
    record = ledger.register_detached_run(row["id"], run_id="corr-sec")

    finalized = ledger.finalize_detached_run(
        "corr-sec",
        success=False,
        error="auth failed: sk-abc123def456ghi789jkl token rejected",
    )
    assert "sk-abc123def456ghi789jkl" not in (finalized["error"] or "")
    assert record["detached_run_id"] == "corr-sec"


def test_find_detached_run_reads_back_by_correlation_id(ledger):
    row = ledger.create_execution("job-rb", source="builtin")
    ledger.mark_execution_running(row["id"])
    ledger.register_detached_run(row["id"], run_id="corr-rb")

    found = ledger.find_detached_run("corr-rb")
    assert found is not None
    assert found["id"] == row["id"]
    assert found["detached_status"] == "started"
    assert ledger.find_detached_run("nope") is None


def test_no_agent_detached_directive_end_to_end(tmp_path, monkeypatch):
    """Production protocol: a no_agent script emits a DETACHED (RUN_STARTED)
    directive → the scheduler registers the lease atomically and keeps the
    execution nonterminal; the detached worker finalizes by correlation id
    through the stable CLI; the next reconcile sweep completes the run.
    Nothing here is specific to any one custom job."""
    import json as _json
    from types import SimpleNamespace

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    (hermes_home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.executions as E
    import cron.jobs as J

    monkeypatch.setattr(J, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(J, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", hermes_home / "cron" / "output")
    monkeypatch.setattr(
        E, "EXECUTIONS_FILE", hermes_home / "cron" / "executions.db"
    )

    directive = _json.dumps({
        "outcome": "detached",
        "run_id": "corr-e2e-1",
        "lease_seconds": 600,
        "worker": "runner@host:99",
    })
    (hermes_home / "scripts" / "launch.sh").write_text(
        f"#!/bin/bash\necho 'worker launched'\necho '{directive}'\nexit 0\n"
    )
    job = J.create_job(
        prompt="update", schedule="every 10m", no_agent=True,
        script="launch.sh", deliver="telegram",
    )
    from datetime import datetime, timedelta, timezone

    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    J.update_job(job["id"], {"next_run_at": due})

    from cron import incidents as I
    from cron import scheduler as S

    delivered = []
    monkeypatch.setattr(
        S, "_deliver_result", lambda *a, **k: delivered.append(a) or None
    )
    S.tick(verbose=False, sync=True)

    latest = E.latest_execution(job["id"])
    assert latest["status"] == "running"  # nonterminal
    assert latest["detached_status"] == "started"
    assert latest["detached_run_id"] == "corr-e2e-1"
    assert latest["detached_worker"] == "runner@host:99"
    refreshed = J.get_job(job["id"])
    assert refreshed["last_status"] == "detached"
    assert refreshed.get("fire_claim") is None  # claims released
    assert not refreshed.get("failure_streak")
    assert I.count_incidents() == 0
    assert delivered == []  # nothing to deliver until the worker reports

    # Worker finalizes via the stable CLI, by correlation id.
    from hermes_cli.cron import cron_finalize_detached

    ok_args = SimpleNamespace(
        run_id="corr-e2e-1", success=True, failed=False, error=None
    )
    assert cron_finalize_detached(ok_args) == 0
    # Idempotent readback: repeating the same finalize is a clean no-op.
    assert cron_finalize_detached(ok_args) == 0
    # A conflicting rewrite is refused, truthfully.
    conflict = SimpleNamespace(
        run_id="corr-e2e-1", success=False, failed=True, error="nope"
    )
    assert cron_finalize_detached(conflict) == 1
    # Unknown correlation id is a distinct, visible failure.
    unknown = SimpleNamespace(
        run_id="corr-missing", success=True, failed=False, error=None
    )
    assert cron_finalize_detached(unknown) == 2

    # Next sweep reconciles the reported success into the terminal state.
    monkeypatch.setattr(S, "_last_dead_owner_reap_at", None)
    S.tick(verbose=False, sync=True)
    final = E.latest_execution(job["id"])
    assert final["status"] == "completed"
    assert final["detached_status"] == "succeeded"
    assert I.count_incidents() == 0
