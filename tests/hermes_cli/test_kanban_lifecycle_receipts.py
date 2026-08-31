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


class TestReceiptTransactionBoundaries:
    def test_raw_outer_transaction_cannot_capture_nested_receipt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        conn = kb.connect(tmp_path / "raw-outer.db")
        try:
            tid = kb.create_task(conn, title="raw outer")
            capture = kb.LifecycleReceiptCapture()
            conn.execute("BEGIN IMMEDIATE")
            with pytest.raises(RuntimeError, match="owner-managed outer"):
                kb.add_comment(
                    conn,
                    tid,
                    "reviewer",
                    "must not escape raw outer",
                    receipt_capture=capture,
                )
            assert capture.receipt is None
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    def test_release_failure_rolls_back_nested_mutation_before_outer_commit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        conn = kb.connect(tmp_path / "release-failure.db")
        connection_type = type(conn)
        original_execute = connection_type.execute
        failed = False

        def fail_first_release(self, sql, *args, **kwargs):
            nonlocal failed
            if self is conn and sql.startswith("RELEASE ") and not failed:
                failed = True
                raise sqlite3.OperationalError("injected release failure")
            return original_execute(self, sql, *args, **kwargs)

        try:
            tid = kb.create_task(conn, title="release failure")
            capture = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(connection_type, "execute", fail_first_release)
            with kb.write_txn(conn):
                with pytest.raises(sqlite3.OperationalError, match="release failure"):
                    kb.add_comment(
                        conn,
                        tid,
                        "reviewer",
                        "must roll back",
                        receipt_capture=capture,
                    )
            assert capture.receipt is None
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_capture_reuse_across_active_connections_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        conn_a = kb.connect(tmp_path / "reuse-a.db")
        conn_b = kb.connect(tmp_path / "reuse-b.db")
        try:
            tid_a = kb.create_task(conn_a, title="reuse A")
            tid_b = kb.create_task(conn_b, title="reuse B")
            capture = kb.LifecycleReceiptCapture()
            with kb.write_txn(conn_a):
                kb.add_comment(
                    conn_a,
                    tid_a,
                    "reviewer",
                    "A committed",
                    receipt_capture=capture,
                )
                with kb.write_txn(conn_b):
                    with pytest.raises(RuntimeError, match="another connection"):
                        kb.add_comment(
                            conn_b,
                            tid_b,
                            "reviewer",
                            "B refused",
                            receipt_capture=capture,
                        )
            assert capture.receipt is not None
            assert capture.receipt.task_id == tid_a
            assert conn_a.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid_a,)
            ).fetchone()[0] == 1
            assert conn_b.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid_b,)
            ).fetchone()[0] == 0
        finally:
            conn_a.close()
            conn_b.close()

    def test_caught_savepoint_rollback_cannot_publish_phantom_receipt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        conn = kb.connect(home / "kanban" / "boards" / "default" / "kanban.db")
        try:
            tid = kb.create_task(conn, title="savepoint receipt")
            capture = kb.LifecycleReceiptCapture()
            with kb.write_txn(conn):
                try:
                    with kb.write_txn(conn, allow_nested=True):
                        kb.add_comment(
                            conn,
                            tid,
                            "reviewer",
                            "rolled back",
                            receipt_capture=capture,
                        )
                        raise RuntimeError("force savepoint rollback")
                except RuntimeError as exc:
                    assert str(exc) == "force savepoint rollback"
            assert capture.receipt is None
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'commented'",
                (tid,),
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_interleaved_connections_bind_receipt_to_mutation_connection(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        conn_a = kb.connect(tmp_path / "a.db")
        conn_b = kb.connect(tmp_path / "b.db")
        try:
            tid = kb.create_task(conn_a, title="connection A")
            capture = kb.LifecycleReceiptCapture()
            try:
                with kb.write_txn(conn_a):
                    with kb.write_txn(conn_b):
                        kb.add_comment(
                            conn_a,
                            tid,
                            "reviewer",
                            "belongs to A",
                            receipt_capture=capture,
                        )
                    raise RuntimeError("force A rollback")
            except RuntimeError as exc:
                assert str(exc) == "force A rollback"
            assert capture.receipt is None
            assert conn_a.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
        finally:
            conn_a.close()
            conn_b.close()

    def test_independent_connections_can_hold_write_transactions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        path_a = tmp_path / "a.db"
        path_b = tmp_path / "b.db"
        conn_a = kb.connect(path_a)
        conn_b = kb.connect(path_b)
        try:
            with kb.write_txn(conn_a):
                with kb.write_txn(conn_b):
                    conn_b.execute("SELECT 1")
        finally:
            conn_a.close()
            conn_b.close()

    def test_nested_receipt_is_not_published_after_outer_rollback(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        from hermes_cli import kanban_db as kb

        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        capture = kb.LifecycleReceiptCapture()
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="nested receipt")
            with pytest.raises(RuntimeError, match="rollback outer"):
                with kb.write_txn(conn):
                    kb.add_comment(
                        conn,
                        tid,
                        "lt4",
                        "not durable",
                        receipt_capture=capture,
                    )
                    assert capture.receipt is None
                    raise RuntimeError("rollback outer")
            assert capture.receipt is None
            assert kb.list_comments(conn, tid) == []

    def test_reused_capture_is_cleared_before_refusal(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        from hermes_cli import kanban_db as kb

        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        capture = kb.LifecycleReceiptCapture()
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="capture reuse")
            kb.add_comment(conn, tid, "lt4", "first", receipt_capture=capture)
            assert capture.receipt is not None
            with pytest.raises(ValueError, match="unknown task"):
                kb.add_comment(
                    conn,
                    "t_missing",
                    "lt4",
                    "refused",
                    receipt_capture=capture,
                )
            assert capture.receipt is None

    def test_keyboard_interrupt_after_nested_stage_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        conn = kb.connect(tmp_path / "interrupt-nested.db")
        try:
            tid = kb.create_task(conn, title="nested interrupt")
            capture = kb.LifecycleReceiptCapture()
            with kb.write_txn(conn):
                try:
                    with kb.write_txn(conn, allow_nested=True):
                        kb.add_comment(
                            conn,
                            tid,
                            "reviewer",
                            "interrupted before durability",
                            receipt_capture=capture,
                        )
                        raise KeyboardInterrupt
                except KeyboardInterrupt:
                    # A caller that catches the interruption must observe a
                    # fully cleaned-up nested scope before the outer commit.
                    pass
            assert capture.receipt is None
            assert capture._active_connection_id is None
            assert not conn.in_transaction
            assert id(conn) not in kb._RECEIPT_TXNS
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
            # The connection and capture must remain usable after recovery.
            kb.add_comment(
                conn, tid, "reviewer", "recovered", receipt_capture=capture
            )
            assert capture.receipt is not None
            assert capture.receipt.task_id == tid
        finally:
            conn.close()

    def test_keyboard_interrupt_in_outer_txn_leaves_connection_clean(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        conn = kb.connect(tmp_path / "interrupt-outer.db")
        try:
            tid = kb.create_task(conn, title="outer interrupt")
            capture = kb.LifecycleReceiptCapture()
            with pytest.raises(KeyboardInterrupt):
                with kb.write_txn(conn):
                    kb.add_comment(
                        conn,
                        tid,
                        "reviewer",
                        "interrupted before commit",
                        receipt_capture=capture,
                    )
                    raise KeyboardInterrupt
            assert capture.receipt is None
            assert capture._active_connection_id is None
            assert not conn.in_transaction
            assert id(conn) not in kb._RECEIPT_TXNS
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
            # The connection and capture must remain usable after recovery.
            with kb.write_txn(conn):
                conn.execute("SELECT 1")
            kb.add_comment(
                conn, tid, "reviewer", "recovered", receipt_capture=capture
            )
            assert capture.receipt is not None
        finally:
            conn.close()


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

    def test_refused_handoff_emits_no_receipt(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        tid, _run_id = _running_task(home)
        before = _dump_db(home)
        # Live-claim guard: no ownership proof and no --force.
        result = _run_hermes(
            home, "kanban", "request-review", tid,
            "--summary", "lt4 evidence", "--json",
        )
        assert result.returncode != 0
        assert result.stdout == ""
        assert "cannot request review" in result.stderr
        assert _dump_db(home) == before


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


class TestTransactionOutcomeContract:
    """Fail-closed outcome semantics for boundary BaseExceptions.

    Deterministic one-shot injections model an asynchronous interrupt
    landing at each transaction boundary. Arbitrarily repeated interrupts
    cannot be closed; the contract is that a single cleanup interruption
    yields the explicit unknown outcome instead of a silent ordinary
    failure. ``pytest.raises(BaseException)`` is deliberate: a RED run
    must report the escaping ``KeyboardInterrupt`` as a test failure, not
    abort the session.
    """

    def _connect(self, tmp_path: Path, monkeypatch, name: str):
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        return kb.connect(tmp_path / name)

    def test_outer_rollback_interrupt_raises_outcome_unknown(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "outcome-a.db")
        connection_type = type(conn)
        original_execute = connection_type.execute
        interrupted = False

        def interrupt_first_rollback(self_, sql, *args, **kwargs):
            nonlocal interrupted
            if (
                self_ is conn
                and isinstance(sql, str)
                and sql.strip().upper() == "ROLLBACK"
                and not interrupted
            ):
                interrupted = True
                raise KeyboardInterrupt
            return original_execute(self_, sql, *args, **kwargs)

        try:
            tid = kb.create_task(conn, title="outcome A")
            capture = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(
                connection_type, "execute", interrupt_first_rollback
            )
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(conn):
                    kb.add_comment(
                        conn,
                        tid,
                        "reviewer",
                        "rollback interrupted",
                        receipt_capture=capture,
                    )
                    raise ValueError("original body failure")
            monkeypatch.setattr(connection_type, "execute", original_execute)
            # Explicit unknown outcome with the ORIGINAL exception chained --
            # never the bare interrupt, never an ordinary false claim.
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, ValueError)
            assert "original body failure" in str(excinfo.value.__cause__)
            # The error identifies the connection state it left behind.
            assert "transaction" in str(excinfo.value)
            # Frame gone, capture unbound and empty.
            assert id(conn) not in kb._RECEIPT_TXNS
            assert capture.receipt is None
            assert capture._active_connection_id is None
            # The interrupted ROLLBACK never executed: the transaction is
            # still open, and closing it discards the mutation.
            assert conn.in_transaction
            conn.execute("ROLLBACK")
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
        finally:
            monkeypatch.setattr(connection_type, "execute", original_execute)
            conn.close()

    def _failing_rollback_patch(self, conn, connection_type):
        """One-shot ROLLBACK failure that leaves the transaction OPEN.

        Models an ordinary rollback failure (disk I/O error), not the
        benign no-active-transaction auto-rollback signal: the statement
        never executes, so ``conn.in_transaction`` stays True and the
        pending mutation remains visible on the connection.
        """
        original_execute = connection_type.execute
        state = {"fired": False}

        def fail_first_rollback(self_, sql, *args, **kwargs):
            if (
                self_ is conn
                and isinstance(sql, str)
                and sql.strip().upper() == "ROLLBACK"
                and not state["fired"]
            ):
                state["fired"] = True
                raise sqlite3.OperationalError("disk I/O error")
            return original_execute(self_, sql, *args, **kwargs)

        return original_execute, fail_first_rollback

    def test_rollback_operational_error_with_open_txn_is_outcome_unknown(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An ordinary OperationalError from ROLLBACK is not proof of closure.

        Regression: cleanup treated EVERY OperationalError from ROLLBACK
        as the benign no-active-transaction auto-rollback signal, so a
        genuinely failed ROLLBACK with the transaction still open
        re-raised the body failure as an ordinary error -- falsely
        implying rollback while the pending mutation stayed visible.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "outcome-f.db")
        connection_type = type(conn)
        original_execute, fail_first_rollback = self._failing_rollback_patch(
            conn, connection_type
        )

        try:
            tid = kb.create_task(conn, title="outcome F")
            capture = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(connection_type, "execute", fail_first_rollback)
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(conn):
                    kb.add_comment(
                        conn,
                        tid,
                        "reviewer",
                        "rollback failed ordinarily",
                        receipt_capture=capture,
                    )
                    raise ValueError("original body failure")
            monkeypatch.setattr(connection_type, "execute", original_execute)
            # The failed ROLLBACK left the transaction open: never re-raise
            # the body failure as an ordinary (falsely "rolled back") error;
            # escalate to the explicit unknown outcome, original chained.
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, ValueError)
            assert "original body failure" in str(excinfo.value.__cause__)
            # Frame gone, capture unbound and empty despite the escalation.
            assert id(conn) not in kb._RECEIPT_TXNS
            assert capture.receipt is None
            assert capture._active_connection_id is None
            # The transaction is genuinely still open; closing it discards
            # the pending mutation.
            assert conn.in_transaction
            conn.execute("ROLLBACK")
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
        finally:
            monkeypatch.setattr(connection_type, "execute", original_execute)
            conn.close()

    def test_begin_interrupt_with_failed_rollback_is_outcome_unknown(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BEGIN executed, interrupt landed, ROLLBACK failed: escalate.

        Regression: the ordinary rollback failure was swallowed as benign,
        so the bare KeyboardInterrupt re-raised while the connection still
        held the open transaction from the executed BEGIN.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "outcome-g.db")
        connection_type = type(conn)
        original_boundary = kb._execute_boundary_with_retry
        original_execute, fail_first_rollback = self._failing_rollback_patch(
            conn, connection_type
        )

        def begin_then_interrupt(target, sql):
            original_boundary(target, sql)
            if sql == "BEGIN IMMEDIATE":
                raise KeyboardInterrupt

        try:
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", begin_then_interrupt
            )
            monkeypatch.setattr(connection_type, "execute", fail_first_rollback)
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(conn):
                    raise AssertionError("body must not run")
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            monkeypatch.setattr(connection_type, "execute", original_execute)
            # Closure was not proved: the interrupt escalates instead of
            # re-raising bare over an open transaction.
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, KeyboardInterrupt)
            assert id(conn) not in kb._RECEIPT_TXNS
            assert conn.in_transaction
            conn.execute("ROLLBACK")
            with kb.write_txn(conn):
                conn.execute("SELECT 1")
        finally:
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            monkeypatch.setattr(connection_type, "execute", original_execute)
            conn.close()

    def test_commit_failure_with_failed_rollback_is_outcome_unknown(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """COMMIT failed with the txn open, then ROLLBACK failed: escalate.

        Regression: the ordinary rollback failure was swallowed as benign,
        so the COMMIT failure re-raised as an ordinary error implying the
        transaction was closed, while it remained open with the pending
        mutation visible.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "outcome-h.db")
        connection_type = type(conn)
        original_boundary = kb._execute_boundary_with_retry
        original_execute, fail_first_rollback = self._failing_rollback_patch(
            conn, connection_type
        )

        def fail_commit(target, sql):
            if sql == "COMMIT":
                # Exhausted BUSY retries: COMMIT never executed, the
                # transaction is still open.
                raise sqlite3.OperationalError("database is locked")
            original_boundary(target, sql)

        try:
            tid = kb.create_task(conn, title="outcome H")
            capture = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(kb, "_execute_boundary_with_retry", fail_commit)
            monkeypatch.setattr(connection_type, "execute", fail_first_rollback)
            with pytest.raises(BaseException) as excinfo:
                kb.add_comment(
                    conn,
                    tid,
                    "reviewer",
                    "commit fails, rollback fails",
                    receipt_capture=capture,
                )
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            monkeypatch.setattr(connection_type, "execute", original_execute)
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
            assert "database is locked" in str(excinfo.value.__cause__)
            assert capture.receipt is None
            assert capture._active_connection_id is None
            assert id(conn) not in kb._RECEIPT_TXNS
            assert conn.in_transaction
            conn.execute("ROLLBACK")
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
        finally:
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            monkeypatch.setattr(connection_type, "execute", original_execute)
            conn.close()

    def _swallowing_rollback_patch(self, conn, connection_type):
        """One-shot ROLLBACK swallow: execute returns, txn stays OPEN.

        Models a connection wrapper (retry shim, instrumentation proxy)
        whose ``execute`` returns from ``ROLLBACK`` without running the
        statement: cleanup observes a "successful" ROLLBACK while the
        real connection still holds the open transaction with the
        pending mutation visible.
        """
        original_execute = connection_type.execute
        state = {"fired": False}

        def swallow_first_rollback(self_, sql, *args, **kwargs):
            if (
                self_ is conn
                and isinstance(sql, str)
                and sql.strip().upper() == "ROLLBACK"
                and not state["fired"]
            ):
                state["fired"] = True
                return None
            return original_execute(self_, sql, *args, **kwargs)

        return original_execute, swallow_first_rollback

    @pytest.mark.parametrize(
        "boundary, original_exc_type",
        [
            ("body", ValueError),
            ("begin", KeyboardInterrupt),
            ("commit", sqlite3.OperationalError),
        ],
    )
    def test_swallowed_rollback_with_open_txn_is_outcome_unknown(
        self, tmp_path: Path, monkeypatch, boundary: str, original_exc_type: type
    ) -> None:
        """A ROLLBACK that returns without closing the txn proves nothing.

        Regression: cleanup returned successful closure immediately after
        ``conn.execute("ROLLBACK")`` returned, without corroborating
        ``conn.in_transaction``. A connection wrapper that returns from
        ROLLBACK without executing it made every boundary failure --
        body error, interrupt after an executed BEGIN, COMMIT failure --
        re-raise as an ordinary (falsely "rolled back") error while the
        real transaction stayed open with the mutation visible.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, f"swallow-{boundary}.db")
        connection_type = type(conn)
        original_boundary = kb._execute_boundary_with_retry
        original_execute, swallow_first_rollback = (
            self._swallowing_rollback_patch(conn, connection_type)
        )

        def begin_then_interrupt(target, sql):
            original_boundary(target, sql)
            if sql == "BEGIN IMMEDIATE":
                raise KeyboardInterrupt

        def fail_commit(target, sql):
            if sql == "COMMIT":
                # Exhausted BUSY retries: COMMIT never executed, the
                # transaction is still open.
                raise sqlite3.OperationalError("database is locked")
            original_boundary(target, sql)

        try:
            tid = kb.create_task(conn, title=f"swallow {boundary}")
            capture = kb.LifecycleReceiptCapture()
            if boundary == "begin":
                monkeypatch.setattr(
                    kb, "_execute_boundary_with_retry", begin_then_interrupt
                )
            elif boundary == "commit":
                monkeypatch.setattr(
                    kb, "_execute_boundary_with_retry", fail_commit
                )
            monkeypatch.setattr(
                connection_type, "execute", swallow_first_rollback
            )
            with pytest.raises(BaseException) as excinfo:
                if boundary == "begin":
                    with kb.write_txn(conn):
                        raise AssertionError("body must not run")
                elif boundary == "commit":
                    kb.add_comment(
                        conn,
                        tid,
                        "reviewer",
                        "commit fails, rollback swallowed",
                        receipt_capture=capture,
                    )
                else:
                    with kb.write_txn(conn):
                        kb.add_comment(
                            conn,
                            tid,
                            "reviewer",
                            "rollback swallowed",
                            receipt_capture=capture,
                        )
                        raise ValueError("original body failure")
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            monkeypatch.setattr(connection_type, "execute", original_execute)
            # Closure was never proven: escalate to the explicit unknown
            # outcome with the ORIGINAL failure chained, never an ordinary
            # error falsely implying rollback.
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, original_exc_type)
            # Frame gone, capture unbound and empty despite the escalation.
            assert id(conn) not in kb._RECEIPT_TXNS
            assert capture.receipt is None
            assert capture._active_connection_id is None
            # The swallowed ROLLBACK left the real transaction open; explicit
            # teardown discards the pending mutation.
            assert conn.in_transaction
            conn.execute("ROLLBACK")
            assert not conn.in_transaction
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
            # The connection is reusable after the explicit teardown.
            with kb.write_txn(conn):
                conn.execute("SELECT 1")
        finally:
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            monkeypatch.setattr(connection_type, "execute", original_execute)
            conn.close()

    @pytest.mark.parametrize("cleanup_method", ["clear", "_release"])
    def test_release_cleanup_interrupt_preserves_original_and_unbinds(
        self, tmp_path: Path, monkeypatch, cleanup_method: str
    ) -> None:
        """One cleanup interrupt on the rollback path cannot mask the body failure.

        Regression: the non-commit release loop called the public
        ``clear()``/``_release()`` unguarded inside the failure path, so a
        single cleanup interrupt replaced the authoritative body exception
        and left the interrupted capture bound after its frame was
        already removed.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "outcome-i.db")
        original_method = getattr(kb.LifecycleReceiptCapture, cleanup_method)
        # Armed only after staging: the mutation helpers also call the
        # public cleanup methods on entry (capture reuse), and the
        # injection must land in the release loop, not there.
        state = {"armed": False, "fired": False}

        def interrupt_once(self_, *args, **kwargs):
            if state["armed"] and not state["fired"]:
                state["fired"] = True
                raise KeyboardInterrupt
            return original_method(self_, *args, **kwargs)

        try:
            tid = kb.create_task(conn, title="outcome I")
            first = kb.LifecycleReceiptCapture()
            second = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, cleanup_method, interrupt_once
            )
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(conn):
                    kb.add_comment(
                        conn, tid, "reviewer", "first staged",
                        receipt_capture=first,
                    )
                    kb.add_comment(
                        conn, tid, "reviewer", "second staged",
                        receipt_capture=second,
                    )
                    state["armed"] = True
                    raise ValueError("original body failure")
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, cleanup_method, original_method
            )
            assert state["fired"]
            # The body failure stays authoritative; the cleanup interrupt
            # is recorded but must not replace it.
            assert isinstance(excinfo.value, ValueError)
            assert "original body failure" in str(excinfo.value)
            notes = "\n".join(getattr(excinfo.value, "__notes__", []))
            assert "KeyboardInterrupt" in notes
            # EVERY affected capture is deterministically cleared and
            # unbound despite the one-shot interrupt; frame gone, DB
            # rolled back.
            for capture in (first, second):
                assert capture.receipt is None
                assert capture._active_connection_id is None
            assert id(conn) not in kb._RECEIPT_TXNS
            assert not conn.in_transaction
            assert conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0
            # Capture reuse after the interrupted cleanup publishes normally.
            kb.add_comment(
                conn, tid, "reviewer", "reuse works", receipt_capture=first
            )
            assert first.receipt is not None
            assert first.receipt.newly_committed is True
        finally:
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, cleanup_method, original_method
            )
            conn.close()

    def test_publish_cleanup_second_interrupt_keeps_finalization_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A second interruption inside publication cleanup cannot escape.

        Regression: the publication failure handler called the public
        ``clear()``/``_release()`` unguarded, so a SystemExit landing in
        cleanup replaced ReceiptFinalizationError (and its authoritative
        publication-interrupt cause) and left the capture bound while the
        mutation was already durably committed.
        """
        from hermes_cli import kanban_db as kb

        db_path = tmp_path / "outcome-j.db"
        conn = self._connect(tmp_path, monkeypatch, "outcome-j.db")
        original_publish = kb.LifecycleReceiptCapture._publish
        original_clear = kb.LifecycleReceiptCapture.clear
        state = {"publish_fired": False, "clear_fired": False}

        def interrupt_publish(self_, conn_, receipt):
            if not state["publish_fired"]:
                state["publish_fired"] = True
                raise KeyboardInterrupt
            return original_publish(self_, conn_, receipt)

        def exit_first_cleanup_clear(self_):
            # Armed by the publication interrupt: entry-reuse clears
            # before it must pass through untouched.
            if state["publish_fired"] and not state["clear_fired"]:
                state["clear_fired"] = True
                raise SystemExit(3)
            return original_clear(self_)

        try:
            tid = kb.create_task(conn, title="outcome J")
            first = kb.LifecycleReceiptCapture()
            second = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, "_publish", interrupt_publish
            )
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, "clear", exit_first_cleanup_clear
            )
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(conn):
                    kb.add_comment(
                        conn, tid, "reviewer", "first staged",
                        receipt_capture=first,
                    )
                    kb.add_comment(
                        conn, tid, "reviewer", "second staged",
                        receipt_capture=second,
                    )
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, "_publish", original_publish
            )
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, "clear", original_clear
            )
            assert state["publish_fired"] and state["clear_fired"]
            # The finalization error survives with the publication
            # interrupt as its authoritative cause; the later SystemExit
            # is recorded but replaces neither.
            assert isinstance(excinfo.value, kb.ReceiptFinalizationError)
            assert isinstance(excinfo.value.__cause__, KeyboardInterrupt)
            notes = "\n".join(getattr(excinfo.value, "__notes__", []))
            assert "SystemExit" in notes
            # Cleanup finished for EVERY unpublished capture despite the
            # second interruption: cleared, unbound, frame gone.
            for capture in (first, second):
                assert capture.receipt is None
                assert capture._active_connection_id is None
            assert id(conn) not in kb._RECEIPT_TXNS
            assert not conn.in_transaction
            # The COMMIT preceded publication: both mutations are durable
            # on a fresh connection; no fabricated receipt exists.
            fresh = kb.connect(db_path)
            try:
                assert fresh.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                    (tid,),
                ).fetchone()[0] == 2
            finally:
                fresh.close()
            # Capture reuse after the failure publishes normally.
            kb.add_comment(
                conn, tid, "reviewer", "reuse works", receipt_capture=first
            )
            assert first.receipt is not None
            assert first.receipt.newly_committed is True
        finally:
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, "_publish", original_publish
            )
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, "clear", original_clear
            )
            conn.close()

    def test_begin_boundary_interrupt_leaves_no_open_transaction(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "outcome-b.db")
        original_boundary = kb._execute_boundary_with_retry

        def begin_then_interrupt(target, sql):
            original_boundary(target, sql)
            if sql == "BEGIN IMMEDIATE":
                raise KeyboardInterrupt

        try:
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", begin_then_interrupt
            )
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(conn):
                    raise AssertionError("body must not run")
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            # Cleanup proved closure, so the ORIGINAL interrupt re-raises.
            assert isinstance(excinfo.value, KeyboardInterrupt)
            # The BEGIN that executed before the interrupt must not leak an
            # active transaction or a receipt frame.
            assert not conn.in_transaction
            assert id(conn) not in kb._RECEIPT_TXNS
            with kb.write_txn(conn):
                conn.execute("SELECT 1")
        finally:
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            conn.close()

    def test_commit_boundary_interrupt_is_outcome_unknown_and_durable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        db_path = tmp_path / "outcome-c.db"
        conn = self._connect(tmp_path, monkeypatch, "outcome-c.db")
        original_boundary = kb._execute_boundary_with_retry

        def commit_then_interrupt(target, sql):
            original_boundary(target, sql)
            if sql == "COMMIT":
                raise KeyboardInterrupt

        try:
            tid = kb.create_task(conn, title="outcome C commit")
            capture = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", commit_then_interrupt
            )
            with pytest.raises(BaseException) as excinfo:
                kb.add_comment(
                    conn,
                    tid,
                    "reviewer",
                    "durable but unreported",
                    receipt_capture=capture,
                )
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            # COMMIT executed before the interrupt: never claim rollback,
            # never publish success -- explicit unknown, interrupt chained.
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, KeyboardInterrupt)
            assert capture.receipt is None
            assert capture._active_connection_id is None
            assert id(conn) not in kb._RECEIPT_TXNS
            assert not conn.in_transaction
            # Authoritative readback on a FRESH connection: the mutation is
            # durable even though no receipt was published.
            fresh = kb.connect(db_path)
            try:
                assert fresh.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                    (tid,),
                ).fetchone()[0] == 1
            finally:
                fresh.close()
        finally:
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            conn.close()

    def test_durability_check_interrupt_is_outcome_unknown(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        db_path = tmp_path / "outcome-c2.db"
        conn = self._connect(tmp_path, monkeypatch, "outcome-c2.db")

        def interrupt_durability(target):
            raise KeyboardInterrupt

        try:
            tid = kb.create_task(conn, title="outcome C durability")
            capture = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(
                kb, "_check_file_length_invariant", interrupt_durability
            )
            with pytest.raises(BaseException) as excinfo:
                kb.add_comment(
                    conn,
                    tid,
                    "reviewer",
                    "durable, verification interrupted",
                    receipt_capture=capture,
                )
            monkeypatch.undo()
            # Post-commit failure cannot roll back: suppress the receipt and
            # raise the stronger explicit error chained from the durability
            # failure, not an ordinary success/failure ambiguity.
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, KeyboardInterrupt)
            assert capture.receipt is None
            assert capture._active_connection_id is None
            assert id(conn) not in kb._RECEIPT_TXNS
            fresh = kb.connect(db_path)
            try:
                assert fresh.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                    (tid,),
                ).fetchone()[0] == 1
            finally:
                fresh.close()
        finally:
            conn.close()

    def test_ordinary_durability_failure_is_outcome_unknown(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "outcome-c3.db")

        def torn_extend(target):
            raise sqlite3.DatabaseError("torn-extend detected")

        try:
            tid = kb.create_task(conn, title="outcome C ordinary")
            monkeypatch.setattr(
                kb, "_check_file_length_invariant", torn_extend
            )
            with pytest.raises(kb.TransactionOutcomeUnknownError) as excinfo:
                kb.add_comment(conn, tid, "reviewer", "durable, torn check")
            monkeypatch.undo()
            assert isinstance(excinfo.value.__cause__, sqlite3.DatabaseError)
            assert id(conn) not in kb._RECEIPT_TXNS
        finally:
            conn.close()

    def test_nested_rollback_to_interrupt_poisons_outer_commit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        db_path = tmp_path / "outcome-d1.db"
        conn = self._connect(tmp_path, monkeypatch, "outcome-d1.db")
        connection_type = type(conn)
        original_execute = connection_type.execute
        interrupted = False

        def interrupt_rollback_to(self_, sql, *args, **kwargs):
            nonlocal interrupted
            if (
                self_ is conn
                and isinstance(sql, str)
                and sql.startswith("ROLLBACK TO ")
                and not interrupted
            ):
                interrupted = True
                raise KeyboardInterrupt
            return original_execute(self_, sql, *args, **kwargs)

        try:
            tid = kb.create_task(conn, title="outcome D1")
            capture = kb.LifecycleReceiptCapture()
            monkeypatch.setattr(connection_type, "execute", interrupt_rollback_to)
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(conn):
                    try:
                        with kb.write_txn(conn, allow_nested=True):
                            kb.add_comment(
                                conn,
                                tid,
                                "reviewer",
                                "phantom candidate",
                                receipt_capture=capture,
                            )
                            raise ValueError("inner failure")
                    except ValueError:
                        # The caller swallows the inner failure; the outer
                        # exit must still refuse to commit.
                        pass
            monkeypatch.setattr(connection_type, "execute", original_execute)
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, KeyboardInterrupt)
            assert id(conn) not in kb._RECEIPT_TXNS
            assert capture.receipt is None
            assert capture._active_connection_id is None
            # No phantom durability on a fresh connection.
            fresh = kb.connect(db_path)
            try:
                assert fresh.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                    (tid,),
                ).fetchone()[0] == 0
            finally:
                fresh.close()
            # The connection and capture remain usable after recovery.
            kb.add_comment(
                conn, tid, "reviewer", "recovered", receipt_capture=capture
            )
            assert capture.receipt is not None
        finally:
            monkeypatch.setattr(connection_type, "execute", original_execute)
            conn.close()

    def test_swallowed_release_escalation_skips_commit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        db_path = tmp_path / "outcome-d2.db"
        conn = self._connect(tmp_path, monkeypatch, "outcome-d2.db")
        connection_type = type(conn)
        original_execute = connection_type.execute
        fired = False
        statements: list = []

        def release_executes_then_interrupts(self_, sql, *args, **kwargs):
            nonlocal fired
            if self_ is conn and isinstance(sql, str):
                statements.append(sql)
            result = original_execute(self_, sql, *args, **kwargs)
            if (
                self_ is conn
                and isinstance(sql, str)
                and sql.startswith("RELEASE ")
                and not fired
            ):
                fired = True
                raise KeyboardInterrupt
            return result

        try:
            tid = kb.create_task(conn, title="outcome D2")
            monkeypatch.setattr(
                connection_type, "execute", release_executes_then_interrupts
            )
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(conn):
                    try:
                        with kb.write_txn(conn, allow_nested=True):
                            conn.execute(
                                "INSERT INTO task_comments "
                                "(task_id, author, body, created_at) "
                                "VALUES (?, 'reviewer', 'phantom', 1)",
                                (tid,),
                            )
                    except KeyboardInterrupt:
                        # The caller swallows the inner escalation; the
                        # outer exit must not replace it with cannot-commit.
                        pass
            monkeypatch.setattr(connection_type, "execute", original_execute)
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            # The invalidated transaction must never reach COMMIT.
            assert not any(s.strip().upper().startswith("COMMIT") for s in statements)
            assert id(conn) not in kb._RECEIPT_TXNS
            fresh = kb.connect(db_path)
            try:
                assert fresh.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                    (tid,),
                ).fetchone()[0] == 0
            finally:
                fresh.close()
            with kb.write_txn(conn):
                conn.execute("SELECT 1")
        finally:
            monkeypatch.setattr(connection_type, "execute", original_execute)
            conn.close()

    def test_publish_interrupt_raises_receipt_finalization_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        db_path = tmp_path / "outcome-e.db"
        conn = self._connect(tmp_path, monkeypatch, "outcome-e.db")

        try:
            tid = kb.create_task(conn, title="outcome E")
            capture = kb.LifecycleReceiptCapture()
            original_publish = kb.LifecycleReceiptCapture._publish
            fired = False

            def interrupt_publish(self_, conn_, receipt):
                nonlocal fired
                if not fired:
                    fired = True
                    raise KeyboardInterrupt
                return original_publish(self_, conn_, receipt)

            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, "_publish", interrupt_publish
            )
            with pytest.raises(BaseException) as excinfo:
                kb.add_comment(
                    conn,
                    tid,
                    "reviewer",
                    "committed, receipt interrupted",
                    receipt_capture=capture,
                )
            monkeypatch.setattr(
                kb.LifecycleReceiptCapture, "_publish", original_publish
            )
            # The mutation committed durably; the interrupted publication
            # surfaces as an explicit finalization failure -- no fabricated
            # receipt, capture cleared and unbound, frame gone.
            assert isinstance(excinfo.value, kb.ReceiptFinalizationError)
            assert isinstance(excinfo.value.__cause__, KeyboardInterrupt)
            assert capture.receipt is None
            assert capture._active_connection_id is None
            assert id(conn) not in kb._RECEIPT_TXNS
            fresh = kb.connect(db_path)
            try:
                assert fresh.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                    (tid,),
                ).fetchone()[0] == 1
            finally:
                fresh.close()
            # Capture reuse after the failure works and publishes normally.
            kb.add_comment(
                conn, tid, "reviewer", "second comment", receipt_capture=capture
            )
            assert capture.receipt is not None
            assert capture.receipt.newly_committed is True
        finally:
            conn.close()


class TestDependencyBlockHookOrdering:
    """The dependency-block lifecycle hook must fire post-commit only."""

    def _connect(self, tmp_path: Path, monkeypatch, name: str):
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        return kb.connect(tmp_path / name)

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
            original_boundary = kb._execute_boundary_with_retry

            def fail_commit(target, sql):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error")
                return original_boundary(target, sql)

            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", fail_commit
            )
            with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
                kb.block_task(
                    conn, tid, reason="waiting on parent", kind="dependency"
                )
            monkeypatch.setattr(
                kb, "_execute_boundary_with_retry", original_boundary
            )
            # The transition rolled back, so the hook must never have fired.
            assert hooks == []
            assert kb.get_task(conn, tid).status == "ready"
        finally:
            conn.close()

    def test_dependency_block_hook_fires_once_post_commit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "dep-hook-ok.db")
        try:
            tid = kb.create_task(conn, title="dep hook ok", assignee="alice")
            recorded: list = []

            def record_hook(event, task_id, **fields):
                recorded.append(
                    (event, task_id, dict(fields), conn.in_transaction)
                )

            monkeypatch.setattr(
                kb, "_fire_kanban_lifecycle_hook", record_hook
            )
            assert kb.block_task(
                conn, tid, reason="waiting on parent", kind="dependency"
            ) is True
            assert len(recorded) == 1
            event, task_id, fields, in_txn = recorded[0]
            assert event == "kanban_task_blocked"
            assert task_id == tid
            # Post-commit: the write transaction is closed when it fires.
            assert in_txn is False
            # Fresh task fields from the committed transition.
            assert fields["assignee"] == "alice"
            assert fields["reason"] == "waiting on parent"
            assert kb.get_task(conn, tid).status == "todo"
        finally:
            conn.close()


class _OpaqueTelemetryConn:
    """Proxy over a real connection with NO ``in_transaction`` at all.

    Deliberately does not define or delegate unknown attributes: a retry
    shim or instrumentation wrapper built on ``object`` exposes only the
    methods it wraps, so every telemetry read raises ``AttributeError``.
    The first ROLLBACK is swallowed (returns without executing), so the
    REAL transaction stays open behind a wrapper that cannot report it.
    Optional boundary injections model an interrupt landing after an
    executed BEGIN IMMEDIATE and an ordinary (non-BUSY) COMMIT failure.
    """

    def __init__(
        self,
        real: sqlite3.Connection,
        *,
        interrupt_begin: bool = False,
        fail_commit: bool = False,
    ) -> None:
        self._real = real
        self._interrupt_begin = interrupt_begin
        self._fail_commit = fail_commit
        self.swallowed_rollbacks = 0

    def execute(self, sql, *args):
        if isinstance(sql, str):
            statement = sql.strip().upper()
            if statement == "ROLLBACK":
                self.swallowed_rollbacks += 1
                return None
            if statement == "BEGIN IMMEDIATE" and self._interrupt_begin:
                self._real.execute(sql, *args)
                raise KeyboardInterrupt
            if statement == "COMMIT" and self._fail_commit:
                raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args)


class _RaisingTelemetryConn:
    """Proxy whose ``in_transaction`` property itself raises RuntimeError.

    ``execute`` delegates fully, so every statement -- including
    cleanup's ROLLBACK -- runs on the real connection; only the telemetry
    read is broken. Fail-closed probing must treat the state as unknown
    instead of letting the RuntimeError replace the authoritative body
    failure (write) or escape raw (read).
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.telemetry_reads = 0

    @property
    def in_transaction(self):
        self.telemetry_reads += 1
        raise RuntimeError("telemetry probe failed")

    def execute(self, sql, *args):
        return self._real.execute(sql, *args)


class _InvalidTelemetryConn:
    """Proxy whose ``in_transaction`` is a truthy NON-boolean.

    Models a wrapper exposing a look-alike attribute (a status string, a
    counter) that does not honor the sqlite3 boolean contract. The first
    ROLLBACK is swallowed, so closure can never be proven through the
    wrapper and the REAL transaction stays open behind it.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.in_transaction = "open"
        self.swallowed_rollbacks = 0

    def execute(self, sql, *args):
        if isinstance(sql, str) and sql.strip().upper() == "ROLLBACK":
            self.swallowed_rollbacks += 1
            return None
        return self._real.execute(sql, *args)


class TestOwnerTelemetryFailClosed:
    """Missing or raising ``in_transaction`` telemetry must fail CLOSED.

    Regression (reviewer blockers A and B): closure proof relied on
    ``getattr(conn, "in_transaction", <default>)`` fallbacks that
    silently CHOSE an outcome when the wrapper could not report one. A
    wrapper without the attribute made every boundary failure escape as
    an ordinary error (or, for a completed read, return the body result)
    while the real transaction stayed open behind a swallowed ROLLBACK;
    a wrapper whose property raises let the telemetry RuntimeError
    replace the authoritative failure in write cleanup and escape raw in
    read cleanup. Unprovable state is UNKNOWN, and UNKNOWN escalates to
    ``TransactionOutcomeUnknownError`` -- never an ordinary claim.
    """

    def _connect(self, tmp_path: Path, monkeypatch, name: str):
        from hermes_cli import kanban_db as kb

        home = _home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        kb._INITIALIZED_PATHS.clear()
        return kb.connect(tmp_path / name)

    @staticmethod
    def _staged_receipt(kb, tid: str):
        return kb.LifecycleReceipt(
            operation="comment",
            task_id=tid,
            prior_status=None,
            final_status=None,
            newly_committed=True,
        )

    @pytest.mark.parametrize(
        "boundary, original_exc_type",
        [
            ("body", ValueError),
            ("begin", KeyboardInterrupt),
            ("commit", sqlite3.OperationalError),
            ("read", None),
        ],
    )
    def test_wrapper_without_telemetry_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch,
        boundary: str,
        original_exc_type: Optional[type],
    ) -> None:
        """Blocker A: no ``in_transaction`` + swallowed ROLLBACK escalates.

        The wrapper cannot prove closure at any boundary, so a write body
        failure, an interrupt after an executed BEGIN, a COMMIT failure,
        and even a COMPLETED read must all raise the explicit unknown
        outcome -- with the original failure chained where one exists --
        instead of an ordinary error/result over an open transaction.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, f"opaque-{boundary}.db")
        try:
            title = f"opaque {boundary}"
            tid = kb.create_task(conn, title=title)
            wrapper = _OpaqueTelemetryConn(
                conn,
                interrupt_begin=(boundary == "begin"),
                fail_commit=(boundary == "commit"),
            )
            capture = kb.LifecycleReceiptCapture()
            body_ran = False
            with pytest.raises(BaseException) as excinfo:
                if boundary == "read":
                    with kb.read_txn(wrapper):
                        row = wrapper.execute(
                            "SELECT title FROM tasks WHERE id = ?", (tid,)
                        ).fetchone()
                        assert row[0] == title
                        body_ran = True
                else:
                    with kb.write_txn(wrapper):
                        if boundary == "begin":
                            raise AssertionError("body must not run")
                        wrapper.execute(
                            "UPDATE tasks SET title = ? WHERE id = ?",
                            ("escaped mutation", tid),
                        )
                        kb._stage_receipt(
                            wrapper, capture, self._staged_receipt(kb, tid)
                        )
                        if boundary == "body":
                            raise ValueError("original body failure")
            # Explicit unknown outcome -- never an ordinary error claim
            # (or, for the read, an ordinary returned result).
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            if boundary == "read":
                assert body_ran
                # No original failure exists; the unknown outcome chains
                # the cleanup/telemetry failure instead.
                assert excinfo.value.__cause__ is not None
            else:
                assert isinstance(excinfo.value.__cause__, original_exc_type)
            # Cleanup attempted the ROLLBACK (the wrapper swallowed it):
            # UNKNOWN state must never skip the rollback attempt.
            assert wrapper.swallowed_rollbacks >= 1
            # Frames gone under both identities, capture unbound and empty.
            assert id(wrapper) not in kb._RECEIPT_TXNS
            assert id(conn) not in kb._RECEIPT_TXNS
            assert capture.receipt is None
            assert capture._active_connection_id is None
            # The swallowed ROLLBACK left the REAL transaction open; the
            # test tears it down explicitly and the mutation is discarded.
            assert conn.in_transaction
            conn.execute("ROLLBACK")
            assert not conn.in_transaction
            assert conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (tid,)
            ).fetchone()[0] == title
            # The real connection is reusable after the teardown.
            with kb.write_txn(conn):
                conn.execute("SELECT 1")
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    @pytest.mark.parametrize("boundary", ["body", "read"])
    def test_invalid_telemetry_value_fails_closed(
        self, tmp_path: Path, monkeypatch, boundary: str
    ) -> None:
        """A non-boolean ``in_transaction`` is UNKNOWN, never an outcome.

        A truthy status string must not route entry to the nested path
        (write) or the reentrant passthrough (read), and with the
        ROLLBACK swallowed it can never prove closure: both surfaces
        escalate to the explicit unknown outcome instead of an ordinary
        error/result over the open real transaction.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, f"invalid-{boundary}.db")
        try:
            title = f"invalid {boundary}"
            tid = kb.create_task(conn, title=title)
            wrapper = _InvalidTelemetryConn(conn)
            capture = kb.LifecycleReceiptCapture()
            body_ran = False
            with pytest.raises(BaseException) as excinfo:
                if boundary == "read":
                    with kb.read_txn(wrapper):
                        row = wrapper.execute(
                            "SELECT title FROM tasks WHERE id = ?", (tid,)
                        ).fetchone()
                        assert row[0] == title
                        body_ran = True
                else:
                    with kb.write_txn(wrapper):
                        wrapper.execute(
                            "UPDATE tasks SET title = ? WHERE id = ?",
                            ("escaped mutation", tid),
                        )
                        kb._stage_receipt(
                            wrapper, capture, self._staged_receipt(kb, tid)
                        )
                        raise ValueError("original body failure")
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            if boundary == "read":
                assert body_ran
                assert excinfo.value.__cause__ is not None
            else:
                assert isinstance(excinfo.value.__cause__, ValueError)
                assert "original body failure" in str(excinfo.value.__cause__)
            assert wrapper.swallowed_rollbacks >= 1
            assert id(wrapper) not in kb._RECEIPT_TXNS
            assert id(conn) not in kb._RECEIPT_TXNS
            assert capture.receipt is None
            assert capture._active_connection_id is None
            # The swallowed ROLLBACK left the REAL transaction open;
            # explicit teardown discards any pending mutation.
            assert conn.in_transaction
            conn.execute("ROLLBACK")
            assert not conn.in_transaction
            assert conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (tid,)
            ).fetchone()[0] == title
            with kb.write_txn(conn):
                conn.execute("SELECT 1")
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    def test_raising_telemetry_write_body_keeps_original_cause(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Blocker B (write): telemetry RuntimeError never replaces the cause.

        The body ValueError stays the authoritative ``__cause__``; the
        raising probe is recorded as context in the message only. The
        cleanup ROLLBACK still executes on the real connection, but an
        unreadable post-state cannot prove closure, so the boundary
        escalates instead of re-raising an ordinary (falsely closed)
        failure.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "raising-write.db")
        try:
            tid = kb.create_task(conn, title="raising telemetry")
            wrapper = _RaisingTelemetryConn(conn)
            capture = kb.LifecycleReceiptCapture()
            with pytest.raises(BaseException) as excinfo:
                with kb.write_txn(wrapper):
                    wrapper.execute(
                        "UPDATE tasks SET title = ? WHERE id = ?",
                        ("escaped mutation", tid),
                    )
                    kb._stage_receipt(
                        wrapper, capture, self._staged_receipt(kb, tid)
                    )
                    raise ValueError("original body failure")
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            # Authoritative cause: the ORIGINAL body failure, never the
            # telemetry RuntimeError.
            assert isinstance(excinfo.value.__cause__, ValueError)
            assert "original body failure" in str(excinfo.value.__cause__)
            # The telemetry failure is context in the message.
            assert "telemetry probe failed" in str(excinfo.value)
            assert wrapper.telemetry_reads >= 1
            # Frames gone, capture unbound and empty.
            assert id(wrapper) not in kb._RECEIPT_TXNS
            assert id(conn) not in kb._RECEIPT_TXNS
            assert capture.receipt is None
            assert capture._active_connection_id is None
            # Cleanup's ROLLBACK executed on the real connection (delegating
            # execute), so the real transaction is closed and the mutation
            # was discarded -- it was just unprovable through the wrapper.
            assert not conn.in_transaction
            assert conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (tid,)
            ).fetchone()[0] == "raising telemetry"
            with kb.write_txn(conn):
                conn.execute("SELECT 1")
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    def test_raising_telemetry_read_completion_chains_cleanup_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Blocker B (read): the RuntimeError must not escape raw.

        A completed read body has no original failure; the explicit
        unknown outcome chains the telemetry/cleanup RuntimeError as
        ``__cause__`` and the body result is never returned.
        """
        from hermes_cli import kanban_db as kb

        conn = self._connect(tmp_path, monkeypatch, "raising-read.db")
        try:
            tid = kb.create_task(conn, title="raising read")
            wrapper = _RaisingTelemetryConn(conn)
            body_ran = False
            with pytest.raises(BaseException) as excinfo:
                with kb.read_txn(wrapper):
                    row = wrapper.execute(
                        "SELECT title FROM tasks WHERE id = ?", (tid,)
                    ).fetchone()
                    assert row[0] == "raising read"
                    body_ran = True
            assert body_ran
            assert isinstance(excinfo.value, kb.TransactionOutcomeUnknownError)
            assert isinstance(excinfo.value.__cause__, RuntimeError)
            assert not isinstance(
                excinfo.value.__cause__, kb.TransactionOutcomeUnknownError
            )
            assert "telemetry probe failed" in str(excinfo.value.__cause__)
            assert wrapper.telemetry_reads >= 1
            assert id(wrapper) not in kb._RECEIPT_TXNS
            assert id(conn) not in kb._RECEIPT_TXNS
            # The closing ROLLBACK executed on the real connection.
            assert not conn.in_transaction
            with kb.read_txn(conn):
                assert conn.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0] == 1
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()
