"""LT-4: backward-compatible ``--json`` lifecycle receipts.

Behavior contract (CLI-process level):

* ``claim``, ``comment``, ``complete``, ``request-review``, ``block``, and
  ``unblock`` accept ``--json``. On success they print exactly one JSON
  receipt object on stdout and nothing else.
* Omitting ``--json`` preserves today's stdout prose, stderr, and exit
  codes byte-for-byte.
* The receipt envelope is one versioned schema shared by every verb:
  ``schema_version``, ``operation``, ``task_id``, ``prior_status``,
  ``final_status``, ``run_id``, ``event_id``, ``comment_id``,
  ``newly_committed``, ``idempotent_replay`` — exactly these keys.
  ``run_id``/``event_id``/``comment_id`` are stable DB row ids (or null
  where not applicable).
* Claim same-claimer replay and comment ``--if-absent`` replay return
  authoritative replay receipts (``idempotent_replay`` true, ids pointing
  at the original committed rows).
* Terminal replay (completing a done task) fails closed: nonzero exit,
  empty stdout, zero mutation. Stale guards likewise emit no success
  receipt and leave every persisted row unchanged.
* ``--json`` with multiple task ids is refused (exit 2) for the bulk
  verbs, mirroring the ``--expected-run-id`` single-id rule.

Every test runs the real CLI as a subprocess against an isolated
HERMES_HOME so exit codes, stdout/stderr bytes, and side effects are
observed at the process boundary.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from hermes_cli import kanban_db_connect as kbc

ROOT = Path(__file__).parents[2]

RECEIPT_KEYS = {
    "schema_version",
    "operation",
    "task_id",
    "prior_status",
    "final_status",
    "run_id",
    "event_id",
    "comment_id",
    "newly_committed",
    "idempotent_replay",
}


def _run_hermes(
    home: Path, *args: str, env_extra: Optional[dict] = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _show(home: Path, task_id: str) -> dict:
    shown = _run_hermes(home, "kanban", "show", task_id, "--json")
    assert shown.returncode == 0, shown.stderr
    return json.loads(shown.stdout)


def _create_task(home: Path, title: str = "lt4 receipt probe") -> str:
    created = _run_hermes(home, "kanban", "create", title, "--json")
    assert created.returncode == 0, created.stderr
    return json.loads(created.stdout)["id"]


def _running_task(home: Path, title: str = "lt4 receipt probe") -> tuple[str, int]:
    task_id = _create_task(home, title)
    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr
    task = _show(home, task_id)["task"]
    assert task["status"] == "running"
    run_id = task["current_run_id"]
    assert isinstance(run_id, int) and run_id > 0
    return task_id, run_id


def _db_rows(home: Path, sql: str, params: tuple = ()) -> list[tuple]:
    db = home / "kanban.db"
    assert db.exists(), "board DB missing"
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchall()


def _events(home: Path, task_id: str, kind: str) -> list[tuple]:
    """(id, run_id) rows for a task's events of one kind, oldest first."""
    return _db_rows(
        home,
        "SELECT id, run_id FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id ASC",
        (task_id, kind),
    )


def _comments(home: Path, task_id: str) -> list[tuple]:
    """(id, author, body) rows for a task's comments, oldest first."""
    return _db_rows(
        home,
        "SELECT id, author, body FROM task_comments "
        "WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    )


def _dump_db(home: Path) -> dict:
    """Full-table dump of the board DB: the zero-mutation oracle."""
    db = home / "kanban.db"
    assert db.exists(), "board DB missing"
    with sqlite3.connect(db) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        dump: dict = {}
        for table in tables:
            try:
                rows = conn.execute(
                    f'SELECT rowid, * FROM "{table}" ORDER BY rowid'
                ).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            dump[table] = rows
    return dump


def _receipt(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Parse stdout as exactly one JSON receipt with the full envelope."""
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert isinstance(receipt, dict)
    assert set(receipt) == RECEIPT_KEYS, sorted(set(receipt) ^ RECEIPT_KEYS)
    assert receipt["schema_version"] == 1
    assert receipt["idempotent_replay"] is (not receipt["newly_committed"])
    return receipt


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    return home


class TestClaimReceipts:
    def test_default_prose_unchanged(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)
        result = _run_hermes(home, "kanban", "claim", tid)
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert len(lines) == 2
        assert lines[0] == f"Claimed {tid}"
        assert lines[1].startswith("Workspace: ")

    def test_json_success_envelope(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)
        result = _run_hermes(home, "kanban", "claim", tid, "--json")
        receipt = _receipt(result)
        task = _show(home, tid)["task"]
        assert task["status"] == "running"
        run_id = task["current_run_id"]
        claimed_events = _events(home, tid, "claimed")
        assert len(claimed_events) == 1
        assert receipt["operation"] == "claim"
        assert receipt["task_id"] == tid
        assert receipt["prior_status"] == "ready"
        assert receipt["final_status"] == "running"
        assert receipt["run_id"] == run_id
        assert receipt["event_id"] == claimed_events[0][0]
        assert claimed_events[0][1] == run_id
        assert receipt["comment_id"] is None
        assert receipt["newly_committed"] is True
        # Workspace assignment still happens under --json (behavior parity
        # with the prose path; only the rendering changes).
        assert _show(home, tid)["task"].get("workspace_path")

    def test_same_claimer_replay_receipt(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)
        first = _run_hermes(
            home, "kanban", "claim", tid, "--claimer", "lt4-claimer", "--json"
        )
        first_receipt = _receipt(first)
        assert first_receipt["newly_committed"] is True
        replay = _run_hermes(
            home, "kanban", "claim", tid, "--claimer", "lt4-claimer", "--json"
        )
        receipt = _receipt(replay)
        assert receipt["operation"] == "claim"
        assert receipt["task_id"] == tid
        assert receipt["prior_status"] == "running"
        assert receipt["final_status"] == "running"
        assert receipt["newly_committed"] is False
        assert receipt["idempotent_replay"] is True
        # Authoritative replay evidence: the ORIGINAL committed rows.
        assert receipt["run_id"] == first_receipt["run_id"]
        assert receipt["event_id"] == first_receipt["event_id"]
        assert receipt["comment_id"] is None
        # The replay wrote nothing: still exactly one claimed event / run.
        assert len(_events(home, tid, "claimed")) == 1
        runs = _db_rows(
            home, "SELECT id FROM task_runs WHERE task_id = ?", (tid,)
        )
        assert len(runs) == 1

    def test_refused_claim_emits_no_receipt(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        before = _dump_db(home)
        result = _run_hermes(home, "kanban", "claim", tid, "--json")
        assert result.returncode != 0
        assert result.stdout == ""
        assert "cannot claim" in result.stderr
        assert _dump_db(home) == before

    def test_replay_without_original_claimed_event_fails_closed(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)
        first = _run_hermes(
            home, "kanban", "claim", tid, "--claimer", "lt4-claimer", "--json"
        )
        _receipt(first)
        db = home / "kanban.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "DELETE FROM task_events WHERE task_id = ? AND kind = 'claimed'",
                (tid,),
            )
        replay = _run_hermes(
            home, "kanban", "claim", tid, "--claimer", "lt4-claimer", "--json"
        )
        assert replay.returncode != 0
        assert replay.stdout == ""


class TestCommentReceipts:
    def test_default_prose_unchanged(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)
        result = _run_hermes(home, "kanban", "comment", tid, "lt4", "hello")
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"Comment added to {tid}\n"

    def test_json_success_envelope(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)
        status = _show(home, tid)["task"]["status"]
        result = _run_hermes(
            home, "kanban", "comment", tid, "lt4", "hello",
            "--author", "lt4", "--json",
        )
        receipt = _receipt(result)
        comments = _comments(home, tid)
        assert len(comments) == 1
        commented = _events(home, tid, "commented")
        assert len(commented) == 1
        assert receipt["operation"] == "comment"
        assert receipt["task_id"] == tid
        assert receipt["prior_status"] == status
        assert receipt["final_status"] == status
        assert receipt["run_id"] is None
        assert receipt["event_id"] == commented[0][0]
        assert receipt["comment_id"] == comments[0][0]
        assert receipt["newly_committed"] is True
        assert comments[0][1] == "lt4"
        assert comments[0][2] == "lt4 hello"

    def test_if_absent_replay_receipt(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)
        first = _run_hermes(
            home, "kanban", "comment", tid, "lt4", "hello",
            "--author", "lt4", "--if-absent", "--json",
        )
        first_receipt = _receipt(first)
        assert first_receipt["newly_committed"] is True
        replay = _run_hermes(
            home, "kanban", "comment", tid, "lt4", "hello",
            "--author", "lt4", "--if-absent", "--json",
        )
        receipt = _receipt(replay)
        assert receipt["operation"] == "comment"
        assert receipt["newly_committed"] is False
        assert receipt["idempotent_replay"] is True
        # Authoritative replay evidence: the original comment row id. No
        # event was written by the replay and none is claimed for it.
        assert receipt["comment_id"] == first_receipt["comment_id"]
        assert receipt["event_id"] is None
        assert receipt["run_id"] is None
        assert len(_comments(home, tid)) == 1
        assert len(_events(home, tid, "commented")) == 1

    def test_stale_guard_emits_no_receipt(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)
        before = _dump_db(home)
        result = _run_hermes(
            home, "kanban", "comment", tid, "lt4", "hello",
            "--expected-status", "running", "--json",
        )
        assert result.returncode != 0
        assert result.stdout == ""
        assert "refusing to comment" in result.stderr
        assert _dump_db(home) == before


class TestCompleteReceipts:
    def test_default_prose_unchanged(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        result = _run_hermes(
            home, "kanban", "complete", tid, "--result", "lt4 done"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"Completed {tid}\n"

    def test_json_success_envelope(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home, "kanban", "complete", tid, "--result", "lt4 done", "--json"
        )
        receipt = _receipt(result)
        completed = _events(home, tid, "completed")
        assert len(completed) == 1
        assert receipt["operation"] == "complete"
        assert receipt["task_id"] == tid
        assert receipt["prior_status"] == "running"
        assert receipt["final_status"] == "done"
        assert receipt["run_id"] == run_id
        assert receipt["event_id"] == completed[0][0]
        assert completed[0][1] == run_id
        assert receipt["comment_id"] is None
        assert receipt["newly_committed"] is True
        assert _show(home, tid)["task"]["status"] == "done"

    def test_terminal_replay_fails_closed(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        done = _run_hermes(
            home, "kanban", "complete", tid, "--result", "lt4 done", "--json"
        )
        _receipt(done)
        before = _dump_db(home)
        replay = _run_hermes(
            home, "kanban", "complete", tid, "--result", "lt4 done", "--json"
        )
        assert replay.returncode != 0
        assert replay.stdout == ""
        assert "cannot complete" in replay.stderr
        assert _dump_db(home) == before

    def test_stale_guard_emits_no_receipt(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        before = _dump_db(home)
        result = _run_hermes(
            home, "kanban", "complete", tid, "--result", "x",
            "--expected-status", "ready", "--json",
        )
        assert result.returncode != 0
        assert result.stdout == ""
        assert "refusing to complete" in result.stderr
        assert _dump_db(home) == before

    def test_multiple_ids_with_json_refused(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid_a, _ra = _running_task(home, "lt4 multi a")
        tid_b, _rb = _running_task(home, "lt4 multi b")
        before = _dump_db(home)
        result = _run_hermes(
            home, "kanban", "complete", tid_a, tid_b,
            "--result", "x", "--json",
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert "--json" in result.stderr
        assert "multiple task ids" in result.stderr
        assert _dump_db(home) == before


class TestRequestReviewReceipts:
    def test_default_prose_unchanged(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home, "kanban", "request-review", tid,
            "--summary", "lt4 evidence", "--expected-run-id", str(run_id),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"Requested review for {tid}: lt4 evidence\n"

    def test_json_success_envelope(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home, "kanban", "request-review", tid,
            "--summary", "lt4 evidence", "--expected-run-id", str(run_id),
            "--json",
        )
        receipt = _receipt(result)
        events = _events(home, tid, "review_requested")
        assert len(events) == 1
        assert receipt["operation"] == "request_review"
        assert receipt["task_id"] == tid
        assert receipt["prior_status"] == "running"
        assert receipt["final_status"] == "review"
        assert receipt["run_id"] == run_id
        assert receipt["event_id"] == events[0][0]
        assert events[0][1] == run_id
        assert receipt["comment_id"] is None
        assert receipt["newly_committed"] is True
        assert _show(home, tid)["task"]["status"] == "review"


class TestBlockReceipts:
    def test_default_prose_unchanged(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        result = _run_hermes(home, "kanban", "block", tid, "lt4", "stuck")
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"Blocked {tid}: lt4 stuck\n"

    def test_json_success_envelope(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home, "kanban", "block", tid, "lt4", "stuck", "--json"
        )
        receipt = _receipt(result)
        events = _events(home, tid, "blocked")
        assert len(events) == 1
        comments = _comments(home, tid)
        assert len(comments) == 1
        assert comments[0][2] == "BLOCKED: lt4 stuck"
        assert receipt["operation"] == "block"
        assert receipt["task_id"] == tid
        assert receipt["prior_status"] == "running"
        assert receipt["final_status"] == "blocked"
        assert receipt["run_id"] == run_id
        assert receipt["event_id"] == events[0][0]
        assert events[0][1] == run_id
        # Under --json the reason comment is bound to the block's own
        # write transaction, so its row id is same-transaction evidence.
        assert receipt["comment_id"] == comments[0][0]
        assert receipt["newly_committed"] is True
        assert _show(home, tid)["task"]["status"] == "blocked"

    def test_json_without_reason_has_null_comment_id(
        self, tmp_path: Path
    ) -> None:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        result = _run_hermes(home, "kanban", "block", tid, "--json")
        receipt = _receipt(result)
        assert receipt["operation"] == "block"
        assert receipt["comment_id"] is None
        assert _comments(home, tid) == []

    def test_stale_guard_emits_no_receipt(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        before = _dump_db(home)
        result = _run_hermes(
            home, "kanban", "block", tid, "lt4", "stuck",
            "--expected-status", "ready", "--json",
        )
        assert result.returncode != 0
        assert result.stdout == ""
        assert "refusing to block" in result.stderr
        assert _dump_db(home) == before

    def test_bulk_ids_with_json_refused(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid_a, _ra = _running_task(home, "lt4 bulk a")
        tid_b, _rb = _running_task(home, "lt4 bulk b")
        before = _dump_db(home)
        result = _run_hermes(
            home, "kanban", "block", tid_a, "stuck", "--ids", tid_b, "--json"
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert "--json" in result.stderr
        assert "multiple task ids" in result.stderr
        assert _dump_db(home) == before


class TestUnblockReceipts:
    def _blocked_task(self, tmp_path: Path) -> tuple[Path, str]:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        blocked = _run_hermes(home, "kanban", "block", tid, "lt4", "stuck")
        assert blocked.returncode == 0, blocked.stderr
        assert _show(home, tid)["task"]["status"] == "blocked"
        return home, tid

    def test_default_prose_unchanged(self, tmp_path: Path) -> None:
        home, tid = self._blocked_task(tmp_path)
        result = _run_hermes(home, "kanban", "unblock", tid)
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"Unblocked {tid}\n"

    def test_json_success_envelope(self, tmp_path: Path) -> None:
        home, tid = self._blocked_task(tmp_path)
        result = _run_hermes(home, "kanban", "unblock", tid, "--json")
        receipt = _receipt(result)
        events = _events(home, tid, "unblocked")
        assert len(events) == 1
        final_status = _show(home, tid)["task"]["status"]
        assert receipt["operation"] == "unblock"
        assert receipt["task_id"] == tid
        assert receipt["prior_status"] == "blocked"
        assert receipt["final_status"] == final_status
        assert receipt["run_id"] is None
        assert receipt["event_id"] == events[0][0]
        assert receipt["comment_id"] is None
        assert receipt["newly_committed"] is True

    def test_json_with_reason_binds_comment(self, tmp_path: Path) -> None:
        home, tid = self._blocked_task(tmp_path)
        result = _run_hermes(
            home, "kanban", "unblock", tid,
            "--reason", "lt4 resume", "--json",
        )
        receipt = _receipt(result)
        unblock_comments = [
            c for c in _comments(home, tid) if c[2] == "UNBLOCK: lt4 resume"
        ]
        assert len(unblock_comments) == 1
        assert receipt["comment_id"] == unblock_comments[0][0]
        assert receipt["newly_committed"] is True

    def test_refused_unblock_emits_no_receipt(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid = _create_task(home)  # ready, not blocked
        before = _dump_db(home)
        result = _run_hermes(home, "kanban", "unblock", tid, "--json")
        assert result.returncode != 0
        assert result.stdout == ""
        assert "cannot unblock" in result.stderr
        assert _dump_db(home) == before

    def test_multiple_ids_with_json_refused(self, tmp_path: Path) -> None:
        home, tid = self._blocked_task(tmp_path)
        other = _create_task(home, "lt4 other")
        before = _dump_db(home)
        result = _run_hermes(
            home, "kanban", "unblock", tid, other, "--json"
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert "--json" in result.stderr
        assert "multiple task ids" in result.stderr
        assert _dump_db(home) == before


class TestReceiptTransactionBoundaries:
    def test_independent_connections_can_hold_write_transactions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        path_a = tmp_path / "a.db"
        path_b = tmp_path / "b.db"
        conn_a = kbc.connect(path_a)
        conn_b = kbc.connect(path_b)
        try:
            with kbc.write_txn(conn_a):
                with kbc.write_txn(conn_b):
                    conn_b.execute("SELECT 1")
        finally:
            conn_a.close()
            conn_b.close()


class TestDependencyBlockHookOrdering:
    """The dependency-block lifecycle hook must fire post-commit only."""

    def _connect(self, tmp_path: Path, monkeypatch, name: str):
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kbc._INITIALIZED_PATHS.clear()
        return kbc.connect(tmp_path / name)

    def test_no_hook_when_dependency_block_commit_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "dep-hook-fail.db")
        try:
            tid = kb.create_task(conn, title="dep hook fail")
            assert kb.get_task(conn, tid).status == "ready"
            hooks: list = []
            monkeypatch.setattr(
                kb,
                "_fire_kanban_lifecycle_hook",
                lambda event, task_id, **fields: hooks.append(event),
            )
            original_boundary = kbc._execute_boundary_with_retry

            def fail_commit(target, sql):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error")
                return original_boundary(target, sql)

            monkeypatch.setattr(
                kbc, "_execute_boundary_with_retry", fail_commit
            )
            with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
                kb.block_task(
                    conn, tid, reason="waiting on parent", kind="dependency"
                )
            monkeypatch.setattr(
                kbc, "_execute_boundary_with_retry", original_boundary
            )
            # The transition rolled back, so the hook must never have fired.
            assert hooks == []
            assert kb.get_task(conn, tid).status == "ready"
        finally:
            conn.close()
