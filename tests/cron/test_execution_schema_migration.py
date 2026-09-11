"""Additive execution-ledger schema: outcomes, delivery, detached, retention.

Contract under test (recurring-automation P1/P2):

* An existing pre-upgrade ``executions.db`` (old CHECK constraint, no
  outcome/delivery/detached columns) migrates in place, non-destructively,
  the first time the new code touches it.
* Old readers (raw SELECT of the legacy columns) and old writers (INSERT
  without the new columns) keep working after migration.
* Typed outcomes — ``deferred`` — persist without being counted as
  success or failure.
* Per-execution delivery target/status/attempt-count/sanitized-error are
  recordable.
* Retention is PER JOB with a 30-day floor: a high-frequency job's volume
  can never evict another job's (e.g. weekly) evidence, and rows younger
  than 30 days are never pruned.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from hermes_time import now as _now

# The exact pre-upgrade DDL (copied from the shipped cron/executions.py)
# so the migration test exercises the real starting point.
_LEGACY_DDL = """CREATE TABLE executions (
     id TEXT PRIMARY KEY,
     job_id TEXT NOT NULL,
     source TEXT NOT NULL,
     process_id TEXT NOT NULL,
     pid INTEGER NOT NULL,
     process_started_at INTEGER,
     status TEXT NOT NULL CHECK(status IN
       ('claimed','running','completed','failed','unknown')),
     claimed_at TEXT NOT NULL,
     started_at TEXT,
     finished_at TEXT,
     error TEXT
   )"""


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def _seed_legacy_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_LEGACY_DDL)
        for row in rows:
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid,"
                " status, claimed_at, finished_at, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()


def test_legacy_db_migrates_without_losing_rows(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    _seed_legacy_db(
        executions.EXECUTIONS_FILE,
        [
            ("old-1", "job-a", "builtin", "proc", 1234, "completed",
             "2026-08-01T09:00:00+00:00", "2026-08-01T09:01:00+00:00", None),
            ("old-2", "job-a", "builtin", "proc", 1234, "failed",
             "2026-08-02T09:00:00+00:00", "2026-08-02T09:01:00+00:00", "boom"),
        ],
    )

    # First touch by the new code migrates; legacy rows survive verbatim.
    rows = executions.list_executions(job_id="job-a")
    assert {r["id"] for r in rows} == {"old-1", "old-2"}
    by_id = {r["id"]: r for r in rows}
    assert by_id["old-2"]["error"] == "boom"
    # New columns exist and default sanely on legacy rows.
    assert by_id["old-1"]["outcome"] is None
    assert by_id["old-1"]["delivery_attempts"] == 0

    # The widened schema accepts a deferred terminal state post-migration.
    claimed = executions.create_execution("job-a", source="builtin")
    deferred = executions.defer_execution(
        claimed["id"],
        reason="lock held",
        occurrence_key="job-a:2026-08-29T09:00:00+00:00",
        retry_at="2026-08-29T09:05:00+00:00",
    )
    assert deferred["status"] == "deferred"
    assert deferred["outcome"] == "deferred"


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


def test_defer_execution_is_neither_completed_nor_failed(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    claimed = executions.create_execution("job-d", source="builtin")
    executions.mark_execution_running(claimed["id"])
    record = executions.defer_execution(
        claimed["id"],
        reason="upstream 429",
        occurrence_key="job-d:2026-08-29T09:00:00+00:00",
        retry_at="2026-08-29T09:10:00+00:00",
    )
    assert record["status"] == "deferred"
    assert record["occurrence_key"] == "job-d:2026-08-29T09:00:00+00:00"
    # Terminal-once: a defer cannot be rewritten into success/failure later
    # through the ordinary terminal API.
    assert executions.finish_execution(claimed["id"], success=True) is None


def test_delivery_metadata_recorded_per_execution(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    claimed = executions.create_execution("job-del", source="builtin")
    executions.mark_execution_running(claimed["id"])
    executions.finish_execution(claimed["id"], success=True)

    first = executions.record_delivery(
        claimed["id"], target="telegram:12345", status="failed",
        error="socket timeout token=sk-abc123def456ghi789jkl012mno345pqr678",
    )
    assert first["delivery_target"] == "telegram:12345"
    assert first["delivery_status"] == "failed"
    assert first["delivery_attempts"] == 1
    # Sanitized: raw secrets must not persist in the ledger.
    assert "sk-abc123def456ghi789jkl012mno345pqr678" not in (
        first["delivery_error"] or ""
    )

    second = executions.record_delivery(
        claimed["id"], target="telegram:12345", status="delivered"
    )
    assert second["delivery_attempts"] == 2
    assert second["delivery_status"] == "delivered"


def test_retention_is_per_job_with_30_day_floor(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 5)

    now = _now()
    recent = (now - timedelta(days=1)).isoformat()
    ancient = (now - timedelta(days=90)).isoformat()

    # Weekly job: a handful of old-but-precious rows, all past 30 days —
    # but within its own per-job cap, so they must survive any pruning
    # triggered by the noisy neighbour.
    executions.create_execution("weekly-job", source="builtin")
    conn = sqlite3.connect(executions.EXECUTIONS_FILE)
    try:
        for i in range(3):
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid,"
                " status, claimed_at) VALUES (?, 'weekly-job', 'builtin',"
                " 'proc', 1, 'completed', ?)",
                (f"weekly-{i}", ancient),
            )
        # High-frequency job: far beyond the cap, all recent (<30 days).
        for i in range(40):
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid,"
                " status, claimed_at) VALUES (?, 'chatty-job', 'builtin',"
                " 'proc', 1, 'completed', ?)",
                (f"chatty-{i:03d}", recent),
            )
        # High-frequency job's ANCIENT overflow: only these are prunable.
        for i in range(10):
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid,"
                " status, claimed_at) VALUES (?, 'chatty-job', 'builtin',"
                " 'proc', 1, 'completed', ?)",
                (f"chatty-old-{i:03d}", ancient),
            )
        conn.commit()
    finally:
        conn.close()

    # Any terminal write triggers pruning.
    fresh = executions.create_execution("chatty-job", source="builtin")
    executions.finish_execution(fresh["id"], success=True)

    weekly_rows = executions.list_executions(job_id="weekly-job", limit=500)
    chatty_rows = executions.list_executions(job_id="chatty-job", limit=500)

    # The weekly job's evidence survives — retention is per job.
    assert {r["id"] for r in weekly_rows} >= {"weekly-0", "weekly-1", "weekly-2"}
    # Recent rows are NEVER pruned, even beyond the per-job cap.
    chatty_ids = {r["id"] for r in chatty_rows}
    assert {f"chatty-{i:03d}" for i in range(40)} <= chatty_ids
    # Ancient overflow past the per-job cap IS pruned.
    assert not any(r_id.startswith("chatty-old-") for r_id in chatty_ids)
