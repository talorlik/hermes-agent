"""ESTOP must stop every dispatch entry point, not only the gateway wrapper.

The gateway checked the sentinel before ticking; ``hermes kanban dispatch`` and a forced
``daemon`` called ``dispatch_once`` directly and kept claiming and spawning while ``hermes
pause`` was engaged. The guard lives in ``dispatch_once`` so no caller can skip it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A spawnable assignee is an installed profile (identity marker present).
    (home / "profiles" / "worker").mkdir(parents=True)
    (home / "profiles" / "worker" / "config.yaml").write_text("{}\n", encoding="utf-8")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ready card", assignee="worker")
    return home, tid


def _spawn_recorder():
    calls = []

    def spawn(task, workspace, board=None):
        calls.append(task.id)
        return 4242

    return calls, spawn


def test_dispatch_once_while_paused_claims_nothing_and_spawns_nothing(board):
    from agent import estop

    home, tid = board
    estop.engage(reason="maintenance")
    calls, spawn = _spawn_recorder()
    try:
        with kbc.connect_closing() as conn:
            result = kbd.dispatch_once(conn, spawn_fn=spawn)
            task = kb.get_task(conn, tid)
            events = kb.list_events(conn, tid)
    finally:
        estop.disengage()

    assert result.skipped_paused is True
    assert calls == [] and result.spawned == []
    assert task.status == "ready" and task.current_run_id is None
    assert [e.kind for e in events] == ["created"] or all(e.kind != "claimed" for e in events)


def test_dispatch_once_dry_run_is_also_gated(board):
    from agent import estop

    estop.engage()
    try:
        with kbc.connect_closing() as conn:
            assert kbd.dispatch_once(conn, dry_run=True).skipped_paused is True
    finally:
        estop.disengage()


def test_dispatch_once_resumes_after_disengage(board):
    from agent import estop

    _home, tid = board
    estop.engage()
    estop.disengage()
    calls, spawn = _spawn_recorder()
    with kbc.connect_closing() as conn:
        result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert result.skipped_paused is False
    assert calls == [tid]


def test_corrupt_sentinel_still_counts_as_engaged(board):
    from agent import estop

    estop.sentinel_path().write_bytes(b"\xff\x00not json")
    try:
        with kbc.connect_closing() as conn:
            assert kbd.dispatch_once(conn).skipped_paused is True
    finally:
        estop.disengage()


def test_unimportable_estop_module_fails_closed_with_explicit_error(board, monkeypatch):
    """A guard that cannot be evaluated must not become permission to dispatch."""
    monkeypatch.setitem(sys.modules, "agent.estop", None)  # import raises ImportError
    calls, spawn = _spawn_recorder()

    with kbc.connect_closing() as conn:
        with pytest.raises(kbd.DispatchGuardUnavailable, match="ESTOP"):
            kbd.dispatch_once(conn, spawn_fn=spawn)
        assert kb.get_task(conn, board[1]).status == "ready"
    assert calls == []


def test_cli_dispatch_honors_the_guard(board, capsys):
    """``hermes kanban dispatch`` through its real command function."""
    import argparse

    from agent import estop
    from hermes_cli import kanban_ops

    _home, tid = board
    estop.engage(reason="maintenance")
    try:
        rc = kanban_ops._cmd_dispatch(argparse.Namespace(dry_run=False, max=None, failure_limit=3, json=True))
    finally:
        estop.disengage()

    out = capsys.readouterr().out
    assert rc == 0 and '"skipped_paused": true' in out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def _forced_daemon_one_tick(monkeypatch):
    """Run the real ``daemon --force`` command for exactly one tick; returns the tick results."""
    import argparse
    import threading

    from hermes_cli import kanban_ops

    ticks = []
    real = kbd.run_daemon

    def one_tick(**kwargs):
        stop = threading.Event()
        kwargs["stop_event"] = stop
        kwargs["on_tick"] = lambda res: (ticks.append(res), stop.set())
        return real(**kwargs)

    monkeypatch.setattr(kbd, "run_daemon", one_tick)
    rc = kanban_ops._cmd_daemon(argparse.Namespace(
        force=True, interval=0.01, max=None, pidfile=None, verbose=False, failure_limit=3))
    return rc, ticks


def test_forced_daemon_honors_the_guard_through_the_real_loop(board, monkeypatch):
    from agent import estop

    _home, tid = board
    estop.engage(reason="maintenance")
    try:
        rc, ticks = _forced_daemon_one_tick(monkeypatch)
    finally:
        estop.disengage()

    assert rc == 0
    assert [t.skipped_paused for t in ticks] == [True]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert all(e.kind != "claimed" for e in kb.list_events(conn, tid))


def test_forced_daemon_does_not_swallow_an_unevaluable_guard(board, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.estop", None)

    with pytest.raises(kbd.DispatchGuardUnavailable, match="ESTOP"):
        _forced_daemon_one_tick(monkeypatch)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, board[1]).status == "ready"
