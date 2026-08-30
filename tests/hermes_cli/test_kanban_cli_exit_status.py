"""Regression coverage for Kanban CLI process exit status propagation."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parents[2]


def _run_hermes(home: Path, *args: str, marker: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if marker:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_delegated_child_kanban_cli_refusal_returns_nonzero_exit_status(tmp_path):
    """A printed Kanban mutation refusal must not look like CLI success."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "exit status probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    refused = _run_hermes(
        home,
        "kanban",
        "comment",
        task_id,
        "must be refused",
        marker=True,
    )

    assert refused.returncode == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks via the CLI" in refused.stderr


def _show_task(home: Path, task_id: str) -> dict:
    shown = _run_hermes(home, "kanban", "show", task_id, "--json")
    assert shown.returncode == 0, shown.stderr
    return json.loads(shown.stdout)["task"]


def test_claim_claimer_replay_is_idempotent_and_foreign_safe(tmp_path):
    """The CLI must recover a lost acknowledgement without accepting another actor."""
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "claim replay", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    first = _run_hermes(
        home,
        "kanban",
        "claim",
        task_id,
        "--claimer",
        "dream:daily-audit:2026-08-30",
    )
    assert first.returncode == 0, first.stderr
    replay = _run_hermes(
        home,
        "kanban",
        "claim",
        task_id,
        "--claimer",
        "dream:daily-audit:2026-08-30",
    )
    assert replay.returncode == 0, replay.stderr

    foreign = _run_hermes(
        home,
        "kanban",
        "claim",
        task_id,
        "--claimer",
        "foreign:worker",
    )
    assert foreign.returncode != 0

    shown = _run_hermes(home, "kanban", "show", task_id, "--json")
    assert shown.returncode == 0, shown.stderr
    payload = json.loads(shown.stdout)
    assert payload["task"]["status"] == "running"
    assert len(payload["runs"]) == 1
    assert sum(event["kind"] == "claimed" for event in payload["events"]) == 1


def test_guarded_block_and_unblock_forward_explicit_reason_author(tmp_path):
    """Dream must bind transactional lifecycle evidence to its own identity."""
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "author lifecycle", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr

    blocked = _run_hermes(
        home,
        "kanban",
        "block",
        task_id,
        "dream failure",
        "--kind",
        "transient",
        "--expected-status",
        "running",
        "--author",
        "dream-cron",
    )
    assert blocked.returncode == 0, blocked.stderr
    payload = _show_full(home, task_id)
    assert any(
        comment["author"] == "dream-cron"
        and comment["body"] == "BLOCKED: dream failure"
        for comment in payload["comments"]
    )

    unblocked = _run_hermes(
        home,
        "kanban",
        "unblock",
        task_id,
        "--reason",
        "dream retry",
        "--expected-block-kind",
        "transient",
        "--author",
        "dream-cron",
    )
    assert unblocked.returncode == 0, unblocked.stderr
    payload = _show_full(home, task_id)
    assert any(
        comment["author"] == "dream-cron"
        and comment["body"] == "UNBLOCK: dream retry"
        for comment in payload["comments"]
    )
    baseline = _show_full(home, task_id)
    invalid_commands = (
        ("block", task_id, "failure", "--author", ""),
        (
            "block",
            task_id,
            "   ",
            "--expected-status",
            "ready",
            "--author",
            "dream-cron",
        ),
        ("block", task_id, "--author", "dream-cron"),
        ("unblock", task_id, "--reason", "retry", "--author", ""),
        ("unblock", task_id, "--author", "dream-cron"),
    )
    for command in invalid_commands:
        refused = _run_hermes(home, "kanban", *command)
        assert refused.returncode == 2
        current = _show_full(home, task_id)
        assert current["task"]["status"] == baseline["task"]["status"]
        assert current["comments"] == baseline["comments"]
        assert current["events"] == baseline["events"]


def test_unguarded_explicit_lifecycle_author_success_is_transactional(tmp_path):
    """Unguarded explicit attribution persists with its lifecycle mutation."""
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "explicit author", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    blocked = _run_hermes(
        home,
        "kanban",
        "block",
        task_id,
        "valid reason",
        "--kind",
        "transient",
        "--author",
        "dream-cron",
    )
    assert blocked.returncode == 0, blocked.stderr
    payload = _show_full(home, task_id)
    assert payload["task"]["status"] == "blocked"
    assert sum(
        comment["author"] == "dream-cron"
        and comment["body"] == "BLOCKED: valid reason"
        for comment in payload["comments"]
    ) == 1
    assert sum(event["kind"] == "blocked" for event in payload["events"]) == 1

    unblocked = _run_hermes(
        home,
        "kanban",
        "unblock",
        task_id,
        "--reason",
        "valid retry",
        "--author",
        "dream-cron",
    )
    assert unblocked.returncode == 0, unblocked.stderr
    payload = _show_full(home, task_id)
    assert payload["task"]["status"] == "ready"
    assert sum(
        comment["author"] == "dream-cron"
        and comment["body"] == "UNBLOCK: valid retry"
        for comment in payload["comments"]
    ) == 1
    assert sum(event["kind"] == "unblocked" for event in payload["events"]) == 1


def test_unguarded_explicit_lifecycle_author_refusals_are_atomic(tmp_path):
    """Explicit lifecycle attribution must never escape a failed transition."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "completed target", "--json")
    assert created.returncode == 0, created.stderr
    completed_id = json.loads(created.stdout)["id"]
    claimed = _run_hermes(home, "kanban", "claim", completed_id)
    assert claimed.returncode == 0, claimed.stderr
    completed = _run_hermes(
        home,
        "kanban",
        "complete",
        completed_id,
        "--result",
        "done",
    )
    assert completed.returncode == 0, completed.stderr
    block_baseline = _show_full(home, completed_id)

    refused_block = _run_hermes(
        home,
        "kanban",
        "block",
        completed_id,
        "valid reason",
        "--author",
        "dream-cron",
    )
    assert refused_block.returncode == 1
    assert _show_full(home, completed_id) == block_baseline

    created = _run_hermes(home, "kanban", "create", "ready target", "--json")
    assert created.returncode == 0, created.stderr
    ready_id = json.loads(created.stdout)["id"]
    unblock_baseline = _show_full(home, ready_id)

    refused_unblock = _run_hermes(
        home,
        "kanban",
        "unblock",
        ready_id,
        "--reason",
        "valid reason",
        "--author",
        "dream-cron",
    )
    assert refused_unblock.returncode == 1
    assert _show_full(home, ready_id) == unblock_baseline


def test_complete_expected_status_mismatch_exits_nonzero_and_preserves_task(tmp_path):
    """A stale --expected-status running must refuse a blocked task."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "cas cli probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr
    blocked = _run_hermes(home, "kanban", "block", task_id, "waiting on input")
    assert blocked.returncode == 0, blocked.stderr
    assert _show_task(home, task_id)["status"] == "blocked"

    refused = _run_hermes(
        home,
        "kanban",
        "complete",
        task_id,
        "--expected-status",
        "running",
        "--result",
        "stale complete",
    )

    assert refused.returncode != 0
    assert "expected status" in refused.stderr.lower()
    task = _show_task(home, task_id)
    assert task["status"] == "blocked"
    assert task["result"] is None
    assert task["completed_at"] is None


def test_complete_expected_status_match_completes_running_task(tmp_path):
    """--expected-status running on a running task completes normally."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "cas cli match", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr

    completed = _run_hermes(
        home,
        "kanban",
        "complete",
        task_id,
        "--expected-status",
        "running",
        "--result",
        "shipped",
    )

    assert completed.returncode == 0, completed.stderr
    assert f"Completed {task_id}" in completed.stdout
    task = _show_task(home, task_id)
    assert task["status"] == "done"
    assert task["result"] == "shipped"


# ---------------------------------------------------------------------------
# Lifecycle CAS guards: comment/block/request-review --expected-status and
# unblock --expected-block-kind. Each guard must propagate a mismatch as a
# nonzero top-level exit status AND leave zero traces (no comment, no event,
# no status change) — including the reason comments the block/unblock
# commands normally write before the transition.
# ---------------------------------------------------------------------------


def _show_full(home: Path, task_id: str) -> dict:
    shown = _run_hermes(home, "kanban", "show", task_id, "--json")
    assert shown.returncode == 0, shown.stderr
    return json.loads(shown.stdout)


def test_comment_expected_status_mismatch_exits_nonzero_and_writes_nothing(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "comment cas probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    refused = _run_hermes(
        home, "kanban", "comment", task_id, "stale note",
        "--expected-status", "running",
    )

    assert refused.returncode != 0
    assert "expected status" in refused.stderr.lower()
    payload = _show_full(home, task_id)
    assert payload["comments"] == []
    assert "commented" not in [e["kind"] for e in payload["events"]]

    matched = _run_hermes(
        home, "kanban", "comment", task_id, "fresh note",
        "--expected-status", "ready",
    )
    assert matched.returncode == 0, matched.stderr
    payload = _show_full(home, task_id)
    assert [c["body"] for c in payload["comments"]] == ["fresh note"]


def test_block_expected_status_applies_guard_to_every_bulk_id(tmp_path):
    """Bulk block: stale id refuses (no reason comment), fresh id blocks."""
    home = tmp_path / "hermes"
    home.mkdir()
    ids = []
    for title in ("bulk cas one", "bulk cas two"):
        created = _run_hermes(home, "kanban", "create", title, "--json")
        assert created.returncode == 0, created.stderr
        ids.append(json.loads(created.stdout)["id"])
    stale_id, fresh_id = ids
    # Move the first task out of 'ready' so the shared guard goes stale.
    pre_blocked = _run_hermes(home, "kanban", "block", stale_id)
    assert pre_blocked.returncode == 0, pre_blocked.stderr

    bulk = _run_hermes(
        home, "kanban", "block", stale_id, "waiting on operator",
        "--ids", fresh_id, "--expected-status", "ready",
    )

    assert bulk.returncode == 1
    assert "expected status" in bulk.stderr.lower()
    assert stale_id in bulk.stderr
    stale = _show_full(home, stale_id)
    assert stale["task"]["status"] == "blocked"
    assert stale["comments"] == []
    fresh = _show_full(home, fresh_id)
    assert fresh["task"]["status"] == "blocked"
    assert [c["body"] for c in fresh["comments"]] == [
        "BLOCKED: waiting on operator"
    ]


def test_request_review_expected_status_mismatch_exits_nonzero(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "review cas probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr

    refused = _run_hermes(
        home, "kanban", "request-review", task_id, "--force",
        "--summary", "stale handoff", "--expected-status", "ready",
    )

    assert refused.returncode == 1
    assert "expected status" in refused.stderr.lower()
    payload = _show_full(home, task_id)
    assert payload["task"]["status"] == "running"
    assert "review_requested" not in [e["kind"] for e in payload["events"]]

    matched = _run_hermes(
        home, "kanban", "request-review", task_id, "--force",
        "--summary", "done and verified", "--expected-status", "running",
    )
    assert matched.returncode == 0, matched.stderr
    assert _show_task(home, task_id)["status"] == "review"


def test_unblock_expected_block_kind_mismatch_exits_nonzero(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "unblock cas probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr
    blocked = _run_hermes(
        home, "kanban", "block", task_id, "--kind", "needs_input",
    )
    assert blocked.returncode == 0, blocked.stderr

    refused = _run_hermes(
        home, "kanban", "unblock", task_id,
        "--expected-block-kind", "capability", "--reason", "stale clear",
    )

    assert refused.returncode == 1
    assert "needs_input" in refused.stderr
    payload = _show_full(home, task_id)
    assert payload["task"]["status"] == "blocked"
    assert payload["comments"] == []
    assert "unblocked" not in [e["kind"] for e in payload["events"]]

    matched = _run_hermes(
        home, "kanban", "unblock", task_id,
        "--expected-block-kind", "needs_input", "--reason", "input arrived",
    )
    assert matched.returncode == 0, matched.stderr
    payload = _show_full(home, task_id)
    assert payload["task"]["status"] == "ready"
    assert [c["body"] for c in payload["comments"]] == ["UNBLOCK: input arrived"]


def test_unblock_invalid_expected_block_kind_exits_nonzero_before_mutation(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "unblock cas typo", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    blocked = _run_hermes(home, "kanban", "block", task_id)
    assert blocked.returncode == 0, blocked.stderr

    refused = _run_hermes(
        home, "kanban", "unblock", task_id,
        "--expected-block-kind", "need_input",
    )

    assert refused.returncode == 2
    assert "invalid choice" in refused.stderr
    assert _show_task(home, task_id)["status"] == "blocked"


def test_unblock_dependency_expected_kind_exits_2_before_mutation(tmp_path):
    """dependency is not an assertable unblock kind: dependency waits are
    routed to todo, never blocked, so the guard could never match. The CLI
    must reject the choice at parse time — exit 2, task untouched."""
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "unblock dep kind", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    blocked = _run_hermes(home, "kanban", "block", task_id)
    assert blocked.returncode == 0, blocked.stderr

    refused = _run_hermes(
        home, "kanban", "unblock", task_id,
        "--expected-block-kind", "dependency",
    )

    assert refused.returncode == 2
    assert "invalid choice" in refused.stderr
    payload = _show_full(home, task_id)
    assert payload["task"]["status"] == "blocked"
    assert "unblocked" not in [e["kind"] for e in payload["events"]]


def test_guarded_block_passes_reason_comment_into_db_transaction(monkeypatch):
    """The CLI must not append a guarded reason after block_task commits."""
    from hermes_cli import kanban as cli

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli.kb, "connect_closing", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(cli, "_profile_author", lambda: "tester")
    monkeypatch.setattr(cli, "_worker_run_id_for", lambda _task_id: None)
    monkeypatch.setattr(
        cli.kb,
        "block_task",
        lambda _conn, _task_id, **kwargs: calls.append(kwargs) or (True, None),
    )
    monkeypatch.setattr(
        cli.kb,
        "add_comment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("guarded comment escaped block_task transaction")
        ),
    )
    monkeypatch.setattr(cli.kb, "get_task", lambda _conn, _task_id: SimpleNamespace(status="blocked"))
    args = argparse.Namespace(
        task_id="t_guarded",
        ids=[],
        reason=["race-safe"],
        kind="transient",
        expected_status="running",
    )

    assert cli._cmd_block(args) == 0
    assert calls == [
        {
            "reason": "race-safe",
            "kind": "transient",
            "expected_run_id": None,
            "expected_status": "running",
            "reason_comment_author": "tester",
            "with_reason": True,
        }
    ]


def test_guarded_unblock_passes_reason_comment_into_db_transaction(monkeypatch):
    """The CLI must not append a guarded reason after unblock_task commits."""
    from hermes_cli import kanban as cli

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli.kb, "connect_closing", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(cli, "_profile_author", lambda: "tester")
    monkeypatch.setattr(
        cli.kb,
        "unblock_task",
        lambda _conn, _task_id, **kwargs: calls.append(kwargs) or True,
    )
    monkeypatch.setattr(
        cli.kb,
        "add_comment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("guarded comment escaped unblock_task transaction")
        ),
    )
    args = argparse.Namespace(
        task_ids=["t_guarded"],
        reason="race-safe",
        expected_block_kind="transient",
    )

    assert cli._cmd_unblock(args) == 0
    assert calls == [
        {
            "expected_block_kind": "transient",
            "reason": "race-safe",
            "reason_comment_author": "tester",
        }
    ]


# ---------------------------------------------------------------------------
# comment --if-absent: guarded idempotent replay. Two identical guarded CLI
# calls must produce exactly one comment and one commented event; a
# different body under the same guard appends normally; a stale guard
# refuses with a nonzero exit before any side effect.
# ---------------------------------------------------------------------------


def test_comment_if_absent_guarded_replay_is_idempotent(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "if-absent probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr

    guarded = (
        "--author", "dream", "--expected-status", "running", "--if-absent",
    )
    first = _run_hermes(
        home, "kanban", "comment", task_id, "sync checkpoint", *guarded,
    )
    assert first.returncode == 0, first.stderr
    replay = _run_hermes(
        home, "kanban", "comment", task_id, "sync checkpoint", *guarded,
    )
    assert replay.returncode == 0, replay.stderr

    payload = _show_full(home, task_id)
    assert [(c["author"], c["body"]) for c in payload["comments"]] == [
        ("dream", "sync checkpoint")
    ]
    assert [e["kind"] for e in payload["events"]].count("commented") == 1

    different = _run_hermes(
        home, "kanban", "comment", task_id, "sync checkpoint v2", *guarded,
    )
    assert different.returncode == 0, different.stderr
    payload = _show_full(home, task_id)
    assert [c["body"] for c in payload["comments"]] == [
        "sync checkpoint", "sync checkpoint v2",
    ]
    assert [e["kind"] for e in payload["events"]].count("commented") == 2


def test_comment_if_absent_stale_guard_exits_nonzero_and_writes_nothing(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "if-absent stale", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    # Task stays 'ready'; the running guard is stale even for a replay of
    # an existing exact comment.
    seeded = _run_hermes(
        home, "kanban", "comment", task_id, "sync checkpoint",
        "--author", "dream",
    )
    assert seeded.returncode == 0, seeded.stderr

    refused = _run_hermes(
        home, "kanban", "comment", task_id, "sync checkpoint",
        "--author", "dream", "--expected-status", "running", "--if-absent",
    )

    assert refused.returncode == 1
    assert "expected status" in refused.stderr.lower()
    payload = _show_full(home, task_id)
    assert [c["body"] for c in payload["comments"]] == ["sync checkpoint"]
    assert [e["kind"] for e in payload["events"]].count("commented") == 1


def test_cmd_comment_forwards_if_absent_flag(monkeypatch):
    """The CLI must forward --if-absent into add_comment's transaction."""
    from hermes_cli import kanban as cli

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli.kb, "connect_closing", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(
        cli.kb,
        "add_comment",
        lambda _conn, _task_id, _author, _body, **kwargs: calls.append(kwargs) or 1,
    )
    args = argparse.Namespace(
        task_id="t_guarded",
        text=["sync", "checkpoint"],
        author="dream",
        max_len=None,
        expected_status="running",
        if_absent=True,
    )

    assert cli._cmd_comment(args) == 0
    assert calls == [{"expected_status": "running", "if_absent": True}]
