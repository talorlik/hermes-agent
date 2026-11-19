"""LT-2: one additive versioned KanbanTaskSnapshot builder/serializer.

Behavior contract:

* ``kanban_lifecycle_overlay.build_task_snapshot(conn, task_id)`` captures
  the task row, parents,
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
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_lifecycle_overlay as lifecycle


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbc.init_db()
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
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            task = kb.claim_task(conn, tid)
            assert task is not None
            kb.add_comment(conn, tid, "alice", "working on it")
            snap = lifecycle.build_task_snapshot(conn, tid)
            assert snap is not None
            assert snap.schema_version == lifecycle.TASK_SNAPSHOT_SCHEMA_VERSION == 1
            assert snap.task.current_run_id == task.current_run_id
            assert snap.task.block_kind is None
            assert snap.task.block_recurrences == 0
            assert [c.body for c in snap.comments] == ["working on it"]
            assert any(e.kind == "claimed" for e in snap.events)
            assert len(snap.runs) == 1
            # Blocked state carries the typed kind authoritatively.
            assert kb.block_task(conn, tid, reason="need creds", kind="needs_input")
            snap2 = lifecycle.build_task_snapshot(conn, tid)
            assert snap2.task.block_kind == "needs_input"
            assert snap2.task.block_recurrences == 1

    def test_snapshot_unknown_task_returns_none(self, kanban_home):
        with kbc.connect_closing() as conn:
            assert lifecycle.build_task_snapshot(conn, "t_nope") is None

    def test_snapshot_consistent_under_injected_concurrent_commit(
        self, kanban_home
    ):
        """A commit landing between the builder's reads must not tear the
        envelope: a snapshot that says 'ready' cannot contain the
        concurrent claim's run, event, or comment."""
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)

        def concurrent_claim():
            with kbc.connect_closing() as other:
                claimed = kb.claim_task(other, tid)
                assert claimed is not None
                kb.add_comment(other, tid, "alice", "racing comment")

        with kbc.connect_closing() as raw:
            proxy = _InjectingConn(raw, after_select=1, side_effect=concurrent_claim)
            snap = lifecycle.build_task_snapshot(proxy, tid)
            assert snap is not None

        # One consistent point in time: everything pre-claim...
        assert snap.task.status == "ready"
        assert snap.task.current_run_id is None
        assert not [e for e in snap.events if e.kind == "claimed"]
        assert snap.runs == []
        assert not [c for c in snap.comments if c.body == "racing comment"]
        # ...and the injected commit really did land (sanity).
        with kbc.connect_closing() as conn:
            after = kb.get_task(conn, tid)
            assert after.status == "running"

    def test_snapshot_to_dict_exposes_stable_ids(self, kanban_home):
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            cid = kb.add_comment(conn, tid, "alice", "note")
            snap = lifecycle.build_task_snapshot(conn, tid)
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
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="busy", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            for i in range(n_extra_events):
                kbd.heartbeat_worker(conn, tid, note=f"hb {i}")
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
        with kbc.connect_closing() as conn:
            total = len(kb.list_events(conn, tid))
        assert total > 55
        payload = json.loads(kc.run_slash(f"show {tid} --json"))
        assert len(payload["events"]) == total


# Keys external consumers REQUIRE in ``show --json``, with the type each one
# must have. Upstream's own fields and any it adds later are allowed on top.
def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value)


def _opt(check):
    return lambda value: value is None or check(value)


def _any_str(value) -> bool:
    return isinstance(value, str)


def _positive(value) -> bool:
    return _is_int(value) and value > 0


def _object(value) -> bool:
    return isinstance(value, dict)


def _array(value) -> bool:
    return isinstance(value, list)


REQUIRED_ENVELOPE = {
    "schema_version": lambda value: _is_int(value) and value == 1,
    "task": _object, "latest_summary": _opt(_any_str), "parents": _array,
    "children": _array, "comments": _array, "events": _array, "runs": _array,
}
REQUIRED_TASK = {
    "id": _text, "title": _text, "workspace_kind": _text, "status": _text,
    "priority": _is_int, "created_at": _is_int,
    "started_at": _opt(_is_int), "completed_at": _opt(_is_int), "max_retries": _opt(_is_int),
    **{
        name: _opt(_any_str)
        for name in (
            "body", "assignee", "tenant", "created_by", "workspace_path", "branch_name",
            "project_id", "result", "session_id", "provider_override", "model_override",
            "workflow_template_id", "current_step_key", "completion_contract",
            "last_failure_error",
        )
    },
    "skills": _array,
    "block_kind": _opt(_text), "block_recurrences": _is_int, "current_run_id": _opt(_positive),
}
REQUIRED_COMMENT = {"id": _positive, "author": _text, "body": _any_str, "created_at": _is_int}
REQUIRED_EVENT = {
    "id": _positive, "kind": _text, "payload": _opt(_object), "created_at": _is_int,
    "run_id": _opt(_positive),
}
REQUIRED_RUN = {
    "id": _positive, "status": _text, "started_at": _is_int,
    "ended_at": _opt(_is_int), "worker_pid": _opt(_is_int),
    "outcome": _opt(_any_str), "summary": _opt(_any_str), "error": _opt(_any_str),
    "profile": _opt(_any_str), "step_key": _opt(_any_str), "metadata": _opt(_object),
}


def _missing_or_mistyped(record: dict, required: dict) -> list[str]:
    return [key for key, check in required.items() if key not in record or not check(record[key])]


class TestShowJsonRequiredKeys:
    def _cards(self) -> list[str]:
        """Cards in ready, running, blocked, review and done."""
        with kbc.connect_closing() as conn:
            ids = [
                kb.create_task(conn, title=f"card {n}", assignee="alice", max_runtime_seconds=600)
                for n in range(5)
            ]
            kb.recompute_ready(conn)
            for tid in ids[1:]:
                assert kb.claim_task(conn, tid) is not None
                kb.add_comment(conn, tid, "alice", "note")
            assert kb.block_task(conn, ids[2], reason="wait", kind="needs_input")
            assert kb.request_review(conn, ids[3], summary="handoff", reviewer="bob", force=True)
            assert kb.complete_task(conn, ids[4], result="done", summary="all good", force=True)
        return ids

    def test_every_object_carries_the_required_keys_with_their_types(self, kanban_home):
        assert len(REQUIRED_TASK) == 28
        statuses, comments, runs = set(), 0, 0
        for tid in self._cards():
            payload = json.loads(kc.run_slash(f"show {tid} --json"))
            statuses.add(payload["task"]["status"])
            assert _missing_or_mistyped(payload, REQUIRED_ENVELOPE) == []
            assert _missing_or_mistyped(payload["task"], REQUIRED_TASK) == []
            assert payload["events"]
            for comment in payload["comments"]:
                assert _missing_or_mistyped(comment, REQUIRED_COMMENT) == []
            for event in payload["events"]:
                assert _missing_or_mistyped(event, REQUIRED_EVENT) == []
            for run in payload["runs"]:
                assert _missing_or_mistyped(run, REQUIRED_RUN) == []
            comments += len(payload["comments"])
            runs += len(payload["runs"])
        assert statuses == {"ready", "running", "blocked", "review", "done"}
        assert comments >= 4 and runs >= 4

    def test_snapshot_to_dict_carries_the_required_task_keys(self, kanban_home):
        tid = self._cards()[1]
        with kbc.connect_closing() as conn:
            snapshot = lifecycle.build_task_snapshot(conn, tid)
        assert _missing_or_mistyped(snapshot.to_dict()["task"], REQUIRED_TASK) == []


class TestCommentOrderContract:
    """Public comment sequences must be fully ordered: created_at, then id.

    SQLite's planner currently satisfies ``ORDER BY created_at`` for these
    queries via the ``(task_id, created_at)`` index, whose equal-key entries
    happen to come back rowid-ascending -- so a same-second burst cannot be
    flipped behaviorally today. The order among ties is nevertheless
    UNSPECIFIED SQL; the behavioral test below is the same-second
    regression across every public surface.
    """

    def test_same_second_comments_order_by_id_everywhere(
        self, kanban_home, monkeypatch
    ):
        with kbc.connect_closing() as conn:
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
            snapshot = lifecycle.build_task_snapshot(conn, tid)
            assert [c.id for c in snapshot.comments] == ids
            payload = snapshot.to_dict()
            assert [c["id"] for c in payload["comments"]] == ids
            # Adversarial scan order: a fully-constrained ORDER BY must hold
            # even when SQLite reverses every unspecified scan order.
            conn.execute("PRAGMA reverse_unordered_selects=ON")
            assert [c.id for c in kb.list_comments(conn, tid)] == ids
