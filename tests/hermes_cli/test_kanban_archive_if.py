"""Atomic compare-and-archive: a card reopened after the caller's listing is never archived.

The custom cleanup sweeper listed ``done`` cards, then ran ``show`` + ``archive`` per card; a
card reopened in between was archived anyway. ``archive_task_if`` checks the expectation and
flips the status in one write transaction and reports a typed outcome.
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _done_card(conn, title="old"):
    tid = kb.create_task(conn, title=title, assignee="w")
    kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="ok")
    return tid


def _count(conn, tid, kind):
    return sum(1 for e in kb.list_events(conn, tid) if e.kind == kind)


def test_archives_when_status_and_completion_time_match(board):
    with kbc.connect_closing() as conn:
        tid = _done_card(conn)
        completed = kb.get_task(conn, tid).completed_at
        result = kb.archive_task_if(conn, tid, expected_status="done", expected_completed_at=completed)
        assert (result.outcome, result.status) == ("archived", "archived")
        assert _count(conn, tid, "archived") == 1


def test_reopened_card_is_skipped_with_no_archive_and_no_event(board):
    with kbc.connect_closing() as conn:
        tid = _done_card(conn)
        conn.execute("UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (tid,))
        conn.commit()
        result = kb.archive_task_if(conn, tid, expected_status="done")
        assert (result.outcome, result.status) == ("precondition_not_met", "ready")
        assert kb.get_task(conn, tid).status == "ready"
        assert _count(conn, tid, "archived") == 0


def test_completion_time_mismatch_is_skipped(board):
    with kbc.connect_closing() as conn:
        tid = _done_card(conn)
        completed = kb.get_task(conn, tid).completed_at
        result = kb.archive_task_if(conn, tid, expected_status="done", expected_completed_at=completed + 1)
        assert result.outcome == "precondition_not_met" and result.completed_at == completed
        assert kb.get_task(conn, tid).status == "done"


def test_missing_and_already_archived_are_typed(board):
    with kbc.connect_closing() as conn:
        assert kb.archive_task_if(conn, "nope", expected_status="done").outcome == "not_found"
        tid = _done_card(conn)
        assert kb.archive_task(conn, tid) is True
        assert kb.archive_task_if(conn, tid, expected_status="done").outcome == "already_archived"


def test_legacy_archive_task_is_unchanged(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        assert kb.archive_task(conn, tid) is True
        assert kb.archive_task(conn, tid) is False
        assert kb.archive_task(conn, "missing") is False


def test_concurrent_reopen_and_archive_never_archives_a_reopened_card(board):
    """Two real connections racing: whichever order wins, an archive only follows a still-done card."""
    for _ in range(25):
        with kbc.connect_closing() as setup:
            tid = _done_card(setup)
        start = threading.Barrier(2)
        outcome = {}

        def archiver():
            with kbc.connect_closing() as conn:
                start.wait()
                outcome["archive"] = kb.archive_task_if(conn, tid, expected_status="done")

        def reopener():
            with kbc.connect_closing() as conn:
                start.wait()
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ? AND status = 'done'",
                        (tid,))

        threads = [threading.Thread(target=archiver), threading.Thread(target=reopener)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        with kbc.connect_closing() as conn:
            final = kb.get_task(conn, tid).status
            archived_events = _count(conn, tid, "archived")
        if outcome["archive"].outcome == "archived":
            assert final == "archived" and archived_events == 1
        else:
            assert outcome["archive"].outcome == "precondition_not_met"
            assert final == "ready" and archived_events == 0


def _run(capsys, **kw):
    args = argparse.Namespace(task_ids=kw.pop("task_ids"), purge_ids=None, json=True,
                              expected_status=kw.pop("expected_status", None),
                              expected_completed_at=kw.pop("expected_completed_at", None))
    rc = kanban_cli._cmd_archive(args)
    return rc, json.loads(capsys.readouterr().out)


def test_cli_exit_codes_and_json_shape(board, capsys):
    with kbc.connect_closing() as conn:
        done = _done_card(conn, "a")
        completed = kb.get_task(conn, done).completed_at
        reopened = _done_card(conn, "b")
        conn.execute("UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (reopened,))
        conn.commit()

    rc, payload = _run(capsys, task_ids=[done], expected_status="done", expected_completed_at=completed)
    assert rc == 0
    assert payload["results"] == [
        {"task_id": done, "outcome": "archived", "status": "archived", "completed_at": completed}]

    rc, payload = _run(capsys, task_ids=[reopened], expected_status="done")
    assert rc == 3 and payload["results"][0]["outcome"] == "precondition_not_met"
    assert payload["results"][0]["status"] == "ready"

    rc, payload = _run(capsys, task_ids=["nope"], expected_status="done")
    assert rc == 1 and payload["results"][0]["outcome"] == "not_found"


def test_cli_completed_at_requires_a_single_task(board, capsys):
    args = argparse.Namespace(task_ids=["a", "b"], purge_ids=None, json=True,
                              expected_status="done", expected_completed_at=1)
    assert kanban_cli._cmd_archive(args) == 1


def test_cli_rm_with_expectations_is_refused_before_any_deletion(board, capsys):
    with kbc.connect_closing() as conn:
        tid = _done_card(conn)
        assert kb.archive_task(conn, tid)
    args = argparse.Namespace(task_ids=[], purge_ids=[tid], json=False,
                              expected_status="done", expected_completed_at=None)

    assert kanban_cli._cmd_archive(args) == 2
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid) is not None  # not deleted


def test_archive_outcomes_are_a_closed_set():
    import typing

    assert set(typing.get_args(kb.ArchiveOutcome)) == {
        "archived", "precondition_not_met", "not_found", "already_archived"}
