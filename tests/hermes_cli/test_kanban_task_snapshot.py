"""LT-2: one additive versioned KanbanTaskSnapshot builder/serializer.

Behavior contract:

* ``kb.build_task_snapshot(conn, task_id)`` captures the task row, parents,
  children, comments, events, runs, and latest summary under ONE SQLite
  read transaction — a concurrent commit between the internal reads
  cannot produce a torn envelope.
* The snapshot carries the authoritative ``block_kind``,
  ``block_recurrences``, and ``current_run_id`` and a schema version.
* Serialized comments/events expose their stable rowids.
* The CLI ``show --json`` consumer preserves every legacy key/envelope,
  adds ``schema_version`` + additive fields, and never truncates events.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class _InjectingConn:
    """Connection proxy that fires a side effect after the Nth SELECT.

    Simulates a concurrent writer committing between the snapshot
    builder's internal reads.
    """

    def __init__(self, real: sqlite3.Connection, *, after_select: int, side_effect):
        self._real = real
        self._after_select = after_select
        self._side_effect = side_effect
        self._selects = 0
        self._fired = False

    def execute(self, sql, *args):
        cur = self._real.execute(sql, *args)
        if sql.lstrip().upper().startswith("SELECT"):
            self._selects += 1
            if self._selects == self._after_select and not self._fired:
                self._fired = True
                self._side_effect()
        return cur

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestSnapshotBuilder:
    def test_snapshot_has_version_and_authoritative_fields(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            task = kb.claim_task(conn, tid)
            assert task is not None
            kb.add_comment(conn, tid, "alice", "working on it")
            snap = kb.build_task_snapshot(conn, tid)
            assert snap is not None
            assert snap.schema_version == kb.TASK_SNAPSHOT_SCHEMA_VERSION == 1
            assert snap.task.current_run_id == task.current_run_id
            assert snap.task.block_kind is None
            assert snap.task.block_recurrences == 0
            assert [c.body for c in snap.comments] == ["working on it"]
            assert any(e.kind == "claimed" for e in snap.events)
            assert len(snap.runs) == 1
            # Blocked state carries the typed kind authoritatively.
            assert kb.block_task(conn, tid, reason="need creds", kind="needs_input")
            snap2 = kb.build_task_snapshot(conn, tid)
            assert snap2.task.block_kind == "needs_input"
            assert snap2.task.block_recurrences == 1

    def test_snapshot_unknown_task_returns_none(self, kanban_home):
        with kb.connect_closing() as conn:
            assert kb.build_task_snapshot(conn, "t_nope") is None

    def test_snapshot_consistent_under_injected_concurrent_commit(
        self, kanban_home
    ):
        """A commit landing between the builder's reads must not tear the
        envelope: a snapshot that says 'ready' cannot contain the
        concurrent claim's run, event, or comment."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)

        def concurrent_claim():
            with kb.connect_closing() as other:
                claimed = kb.claim_task(other, tid)
                assert claimed is not None
                kb.add_comment(other, tid, "alice", "racing comment")

        with kb.connect_closing() as raw:
            proxy = _InjectingConn(raw, after_select=1, side_effect=concurrent_claim)
            snap = kb.build_task_snapshot(proxy, tid)
            assert snap is not None

        # One consistent point in time: everything pre-claim...
        assert snap.task.status == "ready"
        assert snap.task.current_run_id is None
        assert not [e for e in snap.events if e.kind == "claimed"]
        assert snap.runs == []
        assert not [c for c in snap.comments if c.body == "racing comment"]
        # ...and the injected commit really did land (sanity).
        with kb.connect_closing() as conn:
            after = kb.get_task(conn, tid)
            assert after.status == "running"

    def test_snapshot_to_dict_exposes_stable_ids(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            cid = kb.add_comment(conn, tid, "alice", "note")
            snap = kb.build_task_snapshot(conn, tid)
        d = snap.to_dict()
        assert d["schema_version"] == 1
        assert d["task"]["id"] == tid
        assert d["task"]["block_kind"] is None
        assert d["task"]["current_run_id"] is not None
        assert [c["id"] for c in d["comments"]] == [cid]
        assert all(isinstance(e["id"], int) for e in d["events"])
        assert all(
            set(e) >= {"id", "kind", "payload", "created_at", "run_id"}
            for e in d["events"]
        )
        assert all(
            set(r) >= {"id", "profile", "step_key", "status", "outcome",
                       "summary", "error", "metadata", "worker_pid",
                       "started_at", "ended_at"}
            for r in d["runs"]
        )


class TestCliShowConsumer:
    def _make_busy_task(self, n_extra_events: int = 60) -> str:
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="busy", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            for i in range(n_extra_events):
                kb.heartbeat_worker(conn, tid, note=f"hb {i}")
            kb.add_comment(conn, tid, "alice", "progress note")
            return tid

    def test_show_json_preserves_legacy_envelope_and_adds_version(
        self, kanban_home
    ):
        tid = self._make_busy_task(n_extra_events=1)
        payload = json.loads(kc.run_slash(f"show {tid} --json"))
        # Additive versioning + authoritative fields.
        assert payload["schema_version"] == 1
        assert payload["task"]["block_kind"] is None
        assert payload["task"]["block_recurrences"] == 0
        assert payload["task"]["current_run_id"] is not None
        # Legacy top-level envelope intact.
        assert set(payload) >= {
            "task", "latest_summary", "parents", "children",
            "comments", "events", "runs",
        }
        # Legacy task keys intact, including the [] normalization for skills.
        assert set(payload["task"]) >= {
            "id", "title", "body", "assignee", "status", "priority", "tenant",
            "workspace_kind", "workspace_path", "branch_name", "project_id",
            "created_by", "created_at", "started_at", "completed_at", "result",
            "skills", "max_retries", "model_override", "provider_override",
            "session_id", "workflow_template_id", "current_step_key",
        }
        assert payload["task"]["skills"] == []
        # Legacy per-row keys intact + additive stable ids.
        assert all(
            set(c) >= {"id", "author", "body", "created_at"}
            for c in payload["comments"]
        )
        assert all(
            set(e) >= {"id", "kind", "payload", "created_at", "run_id"}
            for e in payload["events"]
        )
        assert all(
            set(r) >= {"id", "profile", "step_key", "status", "outcome",
                       "summary", "error", "metadata", "worker_pid",
                       "started_at", "ended_at"}
            for r in payload["runs"]
        )

    def test_show_json_never_truncates_events(self, kanban_home):
        tid = self._make_busy_task(n_extra_events=60)
        with kb.connect_closing() as conn:
            total = len(kb.list_events(conn, tid))
        assert total > 55
        payload = json.loads(kc.run_slash(f"show {tid} --json"))
        assert len(payload["events"]) == total


class TestCommentOrderContract:
    """Public comment sequences must be fully ordered: created_at, then id.

    SQLite's planner currently satisfies ``ORDER BY created_at`` for these
    queries via the ``(task_id, created_at)`` index, whose equal-key entries
    happen to come back rowid-ascending -- so a same-second burst cannot be
    flipped behaviorally today. The order among ties is nevertheless
    UNSPECIFIED SQL; the contract test below pins the declared tie-breaker
    so a planner or schema change cannot silently destabilize count
    boundaries, and the behavioral test is the permanent same-second
    regression across every public surface.
    """

    def test_list_comments_declares_id_tiebreak(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="order contract")
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            kb.list_comments(conn, tid)
            conn.set_trace_callback(None)
            select = next(
                s for s in statements if "FROM task_comments" in s
            )
            normalized = " ".join(select.split()).lower()
            assert "order by created_at asc, id asc" in normalized

    def test_same_second_comments_order_by_id_everywhere(
        self, kanban_home, monkeypatch
    ):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="same-second order")
            # Freeze the clock so all three comment rows share one
            # created_at second.
            monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)
            ids = [
                kb.add_comment(conn, tid, "author", f"note {i}")
                for i in range(3)
            ]
            assert ids == sorted(ids)
            comments = kb.list_comments(conn, tid)
            assert len({c.created_at for c in comments}) == 1
            assert [c.id for c in comments] == ids
            snapshot = kb.build_task_snapshot(conn, tid)
            assert [c.id for c in snapshot.comments] == ids
            payload = snapshot.to_dict()
            assert [c["id"] for c in payload["comments"]] == ids
            # Adversarial scan order: a fully-constrained ORDER BY must hold
            # even when SQLite reverses every unspecified scan order.
            conn.execute("PRAGMA reverse_unordered_selects=ON")
            assert [c.id for c in kb.list_comments(conn, tid)] == ids


class _RollbackClosureConn:
    """Proxy over a real connection whose first ROLLBACK cannot close.

    ``raise`` mode raises a non-benign ``sqlite3.OperationalError`` (disk
    I/O error) without executing the statement; ``swallow`` mode returns
    normally without executing it. Either way the REAL transaction stays
    open: ``in_transaction`` delegates to the real connection and keeps
    reporting True.
    """

    def __init__(self, real: sqlite3.Connection, *, mode: str):
        self._real = real
        self._mode = mode
        self._fired = False

    def execute(self, sql, *args):
        if (
            isinstance(sql, str)
            and sql.strip().upper() == "ROLLBACK"
            and not self._fired
        ):
            self._fired = True
            if self._mode == "raise":
                raise sqlite3.OperationalError("disk I/O error")
            return None
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestReadTxnClosureContract:
    """``read_txn`` must never return a result over an unproven ROLLBACK."""

    @pytest.mark.parametrize("mode", ["raise", "swallow"])
    def test_unprovable_rollback_never_returns_read_result(
        self, kanban_home, mode
    ):
        """Regression: ``read_txn`` suppressed EVERY OperationalError from
        its closing ROLLBACK and trusted a wrapper's bare return, so a
        genuinely failed rollback returned the read result while the
        connection still held the open transaction -- pinning a stale
        snapshot and poisoning the next BEGIN on the connection.
        """
        with kb.connect_closing() as raw:
            tid = kb.create_task(raw, title="closure")
            proxy = _RollbackClosureConn(raw, mode=mode)
            body_ran = False
            with pytest.raises(kb.TransactionOutcomeUnknownError) as excinfo:
                with kb.read_txn(proxy):
                    row = proxy.execute(
                        "SELECT id FROM tasks WHERE id = ?", (tid,)
                    ).fetchone()
                    assert row["id"] == tid
                    body_ran = True
            # The body completed; only the closure proof failed.
            assert body_ran
            if mode == "raise":
                assert isinstance(
                    excinfo.value.__cause__, sqlite3.OperationalError
                )
                assert "disk I/O error" in str(excinfo.value.__cause__)
            else:
                assert excinfo.value.__cause__ is not None
            # No leaked receipt frame under either identity.
            assert id(raw) not in kb._RECEIPT_TXNS
            assert id(proxy) not in kb._RECEIPT_TXNS
            # The real transaction is genuinely still open; reuse only
            # after explicit teardown.
            assert raw.in_transaction
            raw.execute("ROLLBACK")
            assert not raw.in_transaction
            with kb.read_txn(raw):
                count = raw.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            assert count == 1
            assert not raw.in_transaction

    def test_benign_no_transaction_error_corroborated_returns_result(
        self, kanban_home
    ):
        """The benign auto-rollback signal stays accepted -- but only when
        ``in_transaction`` is False, corroborating that nothing is open.
        """
        with kb.connect_closing() as raw:
            kb.create_task(raw, title="benign")

            class _AutoRolledBackConn(_RollbackClosureConn):
                def execute(self, sql, *args):
                    if (
                        isinstance(sql, str)
                        and sql.strip().upper() == "ROLLBACK"
                        and not self._fired
                    ):
                        self._fired = True
                        # SQLite already rolled back (e.g. on I/O error):
                        # the real transaction closes, then the statement
                        # reports no active transaction.
                        self._real.execute("ROLLBACK")
                        raise sqlite3.OperationalError(
                            "cannot rollback - no transaction is active"
                        )
                    return self._real.execute(sql, *args)

            proxy = _AutoRolledBackConn(raw, mode="raise")
            with kb.read_txn(proxy):
                count = proxy.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            assert count == 1
            assert not raw.in_transaction
