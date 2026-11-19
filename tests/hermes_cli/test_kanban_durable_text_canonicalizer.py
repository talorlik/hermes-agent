"""Durable Kanban text stays redacted at the database boundary.

What is left of the LT-1 suite. The shared ``canonicalize_durable_text``
helper it was written for no longer exists; these are the tests that still
hold against the current tree:

* ``redact_review_value`` masks secrets inside nested values.
* Safe comment bodies and completion results are stored byte-identical.
* A review summary and a guarded unblock reason never reach the database
  with a raw secret in them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_lifecycle_overlay as lifecycle


# Realistic fake credentials (never real; entropy-shaped to match vendor
# prefixes so the redactor's prefix patterns fire).
FAKE_GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_SK = "sk-proj-Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8Nn7"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class TestCanonicalizeDurableText:
    def test_redact_review_value_delegates_to_canonicalizer(self):
        """Compatibility: the pre-existing review-boundary helper stays."""
        out = kb.redact_review_value({"s": f"k {FAKE_GHP}"})
        assert FAKE_GHP not in out["s"]


def _all_durable_text(conn, task_id: str) -> str:
    """Concatenate every durable text surface for a task."""
    import json as _json

    parts: list[str] = []
    task = kb.get_task(conn, task_id)
    parts.append(task.result or "")
    for c in kb.list_comments(conn, task_id):
        parts.append(c.body)
    for e in kb.list_events(conn, task_id):
        parts.append(_json.dumps(e.payload) if e.payload else "")
    for r in kb.list_runs(conn, task_id):
        parts.append(r.summary or "")
        parts.append(r.error or "")
        parts.append(_json.dumps(r.metadata) if r.metadata else "")
    return "\n".join(parts)


class TestDurableBoundaries:
    def test_comment_safe_body_byte_identical(self, kanban_home):
        body = "Plain progress note: tests pass, moving on."
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.add_comment(conn, tid, "alice", body)
            assert kb.list_comments(conn, tid)[-1].body == body

    def test_complete_safe_result_byte_identical(self, kanban_home):
        result = "All 7 acceptance criteria verified."
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            assert kb.complete_task(conn, tid, result=result, summary=result)
            task = kb.get_task(conn, tid)
            assert task.result == result
            assert kb.latest_run(conn, tid).summary == result

    def test_unblock_reason_canonicalized(self, kanban_home):
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            assert kb.block_task(conn, tid, reason="wait", kind="needs_input")
            ok = lifecycle.guarded_unblock(
                conn,
                tid,
                reason=f"operator supplied {FAKE_SK} out of band",
                reason_comment_author="op",
            )
            assert ok
            assert FAKE_SK not in _all_durable_text(conn, tid)
            unblocked = [
                e for e in kb.list_events(conn, tid) if e.kind == "unblocked"
            ]
            assert unblocked and "operator supplied" in unblocked[-1].payload["reason"]

    def test_review_summary_canonicalized(self, kanban_home):
        """Pre-existing request_review redaction keeps working through the
        shared canonicalizer (parity guard for the LT-1 refactor)."""
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            task = kb.claim_task(conn, tid)
            assert task is not None
            ok = kb.request_review(
                conn,
                tid,
                summary=f"done, used {FAKE_GHP}",
                expected_run_id=task.current_run_id,
            )
            assert ok
            assert FAKE_GHP not in _all_durable_text(conn, tid)
