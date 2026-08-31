"""LT-1: shared durable-text canonicalizer at the kanban domain boundary.

Behavior contract:

* ``canonicalize_durable_text`` is the single shared canonicalizer for text
  that is about to be durably stored (comments, completion result/summary/
  metadata, review handoffs, block/unblock reasons). It force-redacts
  (``agent.redact.redact_sensitive_text(force=True)``) regardless of the
  user's logging redaction preference.
* Canonical-return mode: safe text is returned byte-identical; unsafe text
  is returned with every secret masked.
* Strict-reject mode: unsafe text raises ``UnsafeDurableTextError`` (a
  ``ValueError``) instead of being altered; safe text passes byte-identical.
* Every durable write path in ``kanban_db`` (``add_comment``,
  ``complete_task``, ``block_task``, ``unblock_task``, ``request_review``)
  routes through it, so raw secrets can never be durably stored no matter
  which surface (CLI / model tool / dashboard) called in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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


# ---------------------------------------------------------------------------
# Canonicalizer unit contract
# ---------------------------------------------------------------------------


class TestCanonicalizeDurableText:
    def test_safe_text_is_byte_identical(self):
        safe = "Ran 12 tests, all green.\n\nSee t_deadbeef for follow-up."
        assert kb.canonicalize_durable_text(safe) == safe

    def test_unsafe_text_is_masked_in_canonical_mode(self):
        out = kb.canonicalize_durable_text(f"deploy key is {FAKE_GHP} ok")
        assert FAKE_GHP not in out
        assert out != f"deploy key is {FAKE_GHP} ok"
        # Surrounding prose survives.
        assert out.startswith("deploy key is ")
        assert out.endswith(" ok")

    def test_strict_mode_rejects_unsafe_text(self):
        with pytest.raises(kb.UnsafeDurableTextError):
            kb.canonicalize_durable_text(f"token={FAKE_GHP}", strict=True)

    def test_strict_error_is_a_value_error_and_leaks_nothing(self):
        try:
            kb.canonicalize_durable_text(f"token={FAKE_GHP}", strict=True)
        except ValueError as exc:
            assert FAKE_GHP not in str(exc)
        else:
            pytest.fail("expected UnsafeDurableTextError")

    def test_strict_mode_passes_safe_text_byte_identical(self):
        safe = "no secrets here"
        assert kb.canonicalize_durable_text(safe, strict=True) == safe

    def test_recursive_value_canonicalization(self):
        value = {
            "note": f"key {FAKE_SK}",
            "files": [f"{FAKE_GHP}", "src/main.py"],
            "pair": ("ok", f"{FAKE_GHP}"),
            "count": 3,
            "none": None,
        }
        out = kb.canonicalize_durable_value(value)
        assert FAKE_SK not in out["note"]
        assert FAKE_GHP not in out["files"][0]
        assert out["files"][1] == "src/main.py"
        assert isinstance(out["pair"], tuple)
        assert out["pair"][0] == "ok"
        assert FAKE_GHP not in out["pair"][1]
        assert out["count"] == 3
        assert out["none"] is None

    def test_recursive_strict_mode_rejects_nested_secret(self):
        with pytest.raises(kb.UnsafeDurableTextError):
            kb.canonicalize_durable_value({"a": [f"x {FAKE_GHP}"]}, strict=True)

    def test_recursive_canonicalization_redacts_dictionary_keys(self):
        out = kb.canonicalize_durable_value({f"token {FAKE_GHP}": "safe"})
        key = next(iter(out))
        assert FAKE_GHP not in key
        assert out[key] == "safe"

    def test_recursive_canonicalization_rejects_key_collisions(self, monkeypatch):
        monkeypatch.setattr(
            kb,
            "canonicalize_durable_text",
            lambda text, strict=False: "same" if text in {"a", "b"} else text,
        )
        with pytest.raises(kb.UnsafeDurableTextError):
            kb.canonicalize_durable_value({"a": 1, "b": 2})

    def test_redact_review_value_delegates_to_canonicalizer(self):
        """Compatibility: the pre-existing review-boundary helper stays."""
        out = kb.redact_review_value({"s": f"k {FAKE_GHP}"})
        assert FAKE_GHP not in out["s"]

    def test_rejects_int_string_json_key_coercion_collision(self):
        """json.dumps coerces the int key 1 to "1" -- colliding with the
        distinct Python key '1' and producing duplicate JSON object keys."""
        with pytest.raises(kb.UnsafeDurableTextError):
            kb.canonicalize_durable_value({1: "int", "1": "str"})

    def test_rejects_none_null_json_key_coercion_collision(self):
        with pytest.raises(kb.UnsafeDurableTextError):
            kb.canonicalize_durable_value({None: "none", "null": "str"})

    def test_rejects_false_string_json_key_coercion_collision(self):
        with pytest.raises(kb.UnsafeDurableTextError):
            kb.canonicalize_durable_value({False: "bool", "false": "str"})

    def test_rejects_true_string_json_key_coercion_collision(self):
        with pytest.raises(kb.UnsafeDurableTextError):
            kb.canonicalize_durable_value({True: "bool", "true": "str"})

    def test_preserves_noncolliding_numeric_keys(self):
        """1.0 serializes as "1.0" while '1' stays "1" -- no collision, so
        valid numeric keys must pass through unchanged."""
        out = kb.canonicalize_durable_value({1.0: "float", "1": "str"})
        assert out == {1.0: "float", "1": "str"}

    def test_rejects_nested_json_key_coercion_collision(self):
        with pytest.raises(kb.UnsafeDurableTextError):
            kb.canonicalize_durable_value({"outer": [{2: "a", "2": "b"}]})


# ---------------------------------------------------------------------------
# Durable write boundaries — raw secrets never reach the DB
# ---------------------------------------------------------------------------


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
    def test_comment_body_canonicalized_at_db_boundary(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.add_comment(conn, tid, "alice", f"my key is {FAKE_GHP} thanks")
            stored = kb.list_comments(conn, tid)[-1].body
            assert FAKE_GHP not in stored
            assert stored.startswith("my key is ")
            assert FAKE_GHP not in _all_durable_text(conn, tid)

    def test_comment_safe_body_byte_identical(self, kanban_home):
        body = "Plain progress note: tests pass, moving on."
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.add_comment(conn, tid, "alice", body)
            assert kb.list_comments(conn, tid)[-1].body == body

    def test_comment_if_absent_replay_dedupes_on_canonical_form(self, kanban_home):
        """A retry of the same unsafe body must dedupe against the stored
        canonical form instead of inserting a second (masked) copy."""
        body = f"lost-ack retry with {FAKE_GHP} inside"
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            first = kb.add_comment(conn, tid, "alice", body, if_absent=True)
            replay = kb.add_comment(conn, tid, "alice", body, if_absent=True)
            assert first == replay
            assert len(kb.list_comments(conn, tid)) == 1

    def test_complete_result_summary_metadata_canonicalized(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            ok = kb.complete_task(
                conn,
                tid,
                result=f"result with {FAKE_GHP}",
                summary=f"summary with {FAKE_SK}",
                metadata={"note": f"meta {FAKE_GHP}", "tests_run": 12},
            )
            assert ok
            task = kb.get_task(conn, tid)
            assert FAKE_GHP not in (task.result or "")
            run = kb.latest_run(conn, tid)
            assert FAKE_SK not in (run.summary or "")
            assert FAKE_GHP not in str(run.metadata)
            assert run.metadata["tests_run"] == 12
            assert FAKE_GHP not in _all_durable_text(conn, tid)
            assert FAKE_SK not in _all_durable_text(conn, tid)

    def test_complete_safe_result_byte_identical(self, kanban_home):
        result = "All 7 acceptance criteria verified."
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            assert kb.complete_task(conn, tid, result=result, summary=result)
            task = kb.get_task(conn, tid)
            assert task.result == result
            assert kb.latest_run(conn, tid).summary == result

    def test_block_reason_canonicalized_everywhere(self, kanban_home):
        reason = f"blocked: cannot use {FAKE_GHP} to auth"
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            ok = kb.block_task(
                conn,
                tid,
                reason=reason,
                kind="needs_input",
                reason_comment_author="alice",
            )
            assert ok
            assert FAKE_GHP not in _all_durable_text(conn, tid)
            # The reason itself survives in masked form on the event.
            blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
            assert blocked and blocked[-1].payload["reason"].startswith("blocked: ")

    def test_unblock_reason_canonicalized(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="alice")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            assert kb.block_task(conn, tid, reason="wait", kind="needs_input")
            ok = kb.unblock_task(
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
        with kb.connect_closing() as conn:
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
