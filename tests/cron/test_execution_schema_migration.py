"""Execution-ledger schema compatibility with databases written by older code.

The two tests of the fork's schema-migration file that pass against the current
``cron/executions.py``: a database created with an earlier upstream DDL keeps its
``delivery_outcome`` and ``scheduled_instant`` values when the current code opens it, and a
writer that names only the legacy columns can still insert after the schema is initialised.
"""

from __future__ import annotations

import sqlite3

_LATEST_UPSTREAM_DDL = """CREATE TABLE executions (
     id TEXT PRIMARY KEY,
     job_id TEXT NOT NULL,
     source TEXT NOT NULL,
     process_id TEXT NOT NULL,
     pid INTEGER NOT NULL,
     process_started_at INTEGER,
     status TEXT NOT NULL CHECK(status IN
       ('claimed','running','completed','failed','unknown')),
     handoff_pending INTEGER NOT NULL DEFAULT 0,
     handoff_started_at REAL,
     claimed_at TEXT NOT NULL,
     started_at TEXT,
     finished_at TEXT,
     error TEXT,
     delivery_outcome TEXT,
     scheduled_instant TEXT
   )"""


def _point_ledger(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def test_latest_upstream_db_migrates_without_losing_additive_columns(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    path = executions.EXECUTIONS_FILE
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_LATEST_UPSTREAM_DDL)
        conn.execute(
            "INSERT INTO executions ("
            "id, job_id, source, process_id, pid, status, claimed_at, "
            "delivery_outcome, scheduled_instant"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "upstream-row",
                "job-upstream",
                "builtin",
                "proc",
                42,
                "completed",
                "2026-09-12T00:00:00+00:00",
                "delivered",
                "2026-09-12T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    row = executions.list_executions(job_id="job-upstream")[0]
    assert row["delivery_outcome"] == "delivered"
    assert row["scheduled_instant"] == "2026-09-12T00:00:00+00:00"


def test_old_writer_shape_still_inserts_after_migration(monkeypatch, tmp_path):
    """An old-code writer (INSERT without the new columns) must keep working."""
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.create_execution("job-w", source="builtin")  # triggers migration

    conn = sqlite3.connect(executions.EXECUTIONS_FILE)
    try:
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid,"
            " status, claimed_at) VALUES ('legacy-w', 'job-w', 'builtin',"
            " 'proc', 42, 'claimed', '2026-08-29T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    rows = executions.list_executions(job_id="job-w")
    assert {r["id"] for r in rows} >= {"legacy-w"}
