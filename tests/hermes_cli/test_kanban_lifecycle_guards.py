"""Fork-only CLI contract for guarded Kanban lifecycle transitions.

Covers ``hermes_cli.kanban_lifecycle_cli`` and ``kanban_lifecycle_overlay``:
``claim --claimer`` replay, ``--expected-status`` / ``--expected-block-kind``
compare-and-swap guards, ``comment --if-absent``, and ``--author`` reason
comments bound to the block/unblock transaction. A refused guard exits
nonzero and leaves no comment, event or status change behind.

The CLI tests run the real CLI as a subprocess against an isolated
HERMES_HOME so exit codes, stderr and side effects are observed at the
process boundary.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _run_hermes(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


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


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbc.init_db()
    return home



# ---------------------------------------------------------------------------
# Parity with upstream. guarded_complete, guarded_block, guarded_request_review
# and guarded_unblock repeat upstream's transition inside their own
# transaction. With no guard set they must leave a task exactly as upstream's
# mutator does; a failure here after an upstream sync means the matching
# function in kanban_lifecycle_overlay.py has to be re-diffed against upstream.
# ---------------------------------------------------------------------------


def _durable_state(conn, tid: str) -> dict:
    """What a transition left behind, with task and run ids made comparable."""
    from hermes_cli import kanban_db as kb

    runs = kb.list_runs(conn, tid)
    ordinal = {run.id: position for position, run in enumerate(runs)}

    def payload(event) -> object:
        if not event.payload:
            return None
        kept = {k: v for k, v in event.payload.items() if k not in {"expires", "run_id"}}
        return json.dumps(kept, sort_keys=True).replace(tid, "<task>")

    task = kb.get_task(conn, tid)
    return {
        "task": {
            name: getattr(task, name)
            for name in ("status", "result", "assignee", "block_kind", "block_recurrences",
                         "claim_lock", "consecutive_failures", "last_failure_error")
        },
        "current_run": ordinal.get(task.current_run_id),
        "runs": [(r.status, r.outcome, r.summary, r.profile, r.metadata) for r in runs],
        "events": [(e.kind, payload(e), ordinal.get(e.run_id)) for e in kb.list_events(conn, tid)],
        "comments": [(c.author, c.body) for c in kb.list_comments(conn, tid)],
    }


def _ready(conn) -> str:
    from hermes_cli import kanban_db as kb

    tid = kb.create_task(conn, title="parity", assignee="alice")
    kb.recompute_ready(conn)
    return tid


def _running(conn) -> str:
    from hermes_cli import kanban_db as kb

    tid = _ready(conn)
    assert kb.claim_task(conn, tid, claimer="parity-claimer") is not None
    return tid


def _in_review(conn) -> str:
    from hermes_cli import kanban_db as kb

    tid = _running(conn)
    assert kb.request_review(conn, tid, summary="handoff", reviewer="bob", force=True)
    return tid


def _blocked(conn) -> str:
    from hermes_cli import kanban_db as kb

    tid = _running(conn)
    assert kb.block_task(conn, tid, reason="wait", kind="needs_input")
    return tid


_PARITY_CASES = [
    pytest.param(
        _running, "complete_task", "guarded_complete",
        {"result": "shipped", "summary": "all checks pass", "metadata": {"k": "v"}, "force": True},
        id="complete-running",
    ),
    pytest.param(_ready, "complete_task", "guarded_complete", {"result": "manual"},
                 id="complete-ready"),
    pytest.param(_in_review, "complete_task", "guarded_complete", {}, id="complete-review"),
    pytest.param(_running, "block_task", "guarded_block",
                 {"reason": "need creds", "kind": "needs_input"}, id="block-running"),
    pytest.param(_ready, "block_task", "guarded_block", {"kind": "dependency"},
                 id="block-ready-dependency"),
    pytest.param(_ready, "block_task", "guarded_block", {}, id="block-ready-untyped"),
    pytest.param(_running, "request_review", "guarded_request_review",
                 {"summary": "done", "reviewer": "bob", "force": True}, id="review-running"),
    pytest.param(_ready, "request_review", "guarded_request_review", {"metadata": {"k": "v"}},
                 id="review-ready"),
    pytest.param(_blocked, "unblock_task", "guarded_unblock", {}, id="unblock-blocked"),
]


@pytest.mark.parametrize("prepare,upstream_name,guarded_name,kwargs", _PARITY_CASES)
def test_guarded_transition_without_a_guard_matches_upstream(
    kanban_home, prepare, upstream_name, guarded_name, kwargs
):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_lifecycle_overlay as lifecycle

    with kbc.connect_closing() as conn:
        by_upstream, by_fork = prepare(conn), prepare(conn)
        assert getattr(kb, upstream_name)(conn, by_upstream, **kwargs)
        outcome = getattr(lifecycle, guarded_name)(conn, by_fork, **kwargs)
        receipt = outcome[0] if isinstance(outcome, tuple) else outcome
        assert receipt is not None
        assert _durable_state(conn, by_fork) == _durable_state(conn, by_upstream)
        assert receipt.final_status == kb.get_task(conn, by_fork).status


# ---------------------------------------------------------------------------
# install(): the one call site in hermes_cli/kanban.py runs at import time
# against upstream's parser records, which an upstream sync can change. It
# must never take the board CLI down, and upstream owns any flag name it adds.
# ---------------------------------------------------------------------------


def _upstream_shaped_specs(kanban_parser, *, complete_owns_json: bool = False) -> list:
    arg, cmd = kanban_parser._arg, kanban_parser._cmd
    complete = [arg("task_ids", nargs="+"), arg("--result")]
    if complete_owns_json:
        complete.append(kanban_parser._json_flag())
    return [
        cmd("show", [arg("task_id"), kanban_parser._json_flag()]),
        cmd("claim", [arg("task_id"), arg("--ttl", type=int, default=900)]),
        cmd("comment", [arg("task_id"), arg("text", nargs="+"), arg("--author")]),
        cmd("complete", complete),
        cmd("block", [arg("task_id"), arg("reason", nargs="*")]),
        cmd("unblock", [arg("task_ids", nargs="+")]),
        cmd("request-review", [arg("task_id")]),
    ]


_VERBS = ("show", "claim", "comment", "complete", "block", "unblock", "request-review")


def _sentinel_handlers() -> dict:
    return {verb: (lambda _args, verb=verb: f"upstream:{verb}") for verb in _VERBS}


def _reinstall(monkeypatch, specs, handlers=None) -> tuple[dict, object]:
    """Run ``install`` against ``specs`` and sentinel upstream handlers."""
    from hermes_cli import kanban_lifecycle_cli as lifecycle_cli
    from hermes_cli import kanban_parser

    monkeypatch.setattr(kanban_parser, "_SPECS", specs)
    monkeypatch.setattr(lifecycle_cli, "_UPSTREAM", {})
    monkeypatch.setattr(lifecycle_cli, "_ADDED", {})
    monkeypatch.setattr(lifecycle_cli, "_INSTALLED_ARGS", [])
    handlers = _sentinel_handlers() if handlers is None else handlers
    lifecycle_cli.install(handlers)
    return handlers, lifecycle_cli


def _parser_for_current_specs() -> argparse.ArgumentParser:
    from hermes_cli import kanban_parser

    parser = argparse.ArgumentParser()
    # Raises "conflicting option string" if a flag were added twice.
    kanban_parser._add_commands(parser.add_subparsers(dest="kanban_action"), kanban_parser._SPECS)
    return parser


def test_install_leaves_a_flag_upstream_already_owns_to_upstream(monkeypatch):
    from hermes_cli import kanban_parser

    specs = _upstream_shaped_specs(kanban_parser, complete_owns_json=True)
    handlers, _ = _reinstall(monkeypatch, specs)
    parser = _parser_for_current_specs()

    upstream_json = parser.parse_args(["complete", "t_x", "--json"])
    assert handlers["complete"](upstream_json) == "upstream:complete"
    # The flags upstream does not own are still added.
    guarded = parser.parse_args(["complete", "t_x", "--expected-status", "running"])
    assert guarded.expected_status == "running"
    plain = parser.parse_args(["block", "t_x", "stuck"])
    assert handlers["block"](plain) == "upstream:block"


def test_install_accepts_parser_records_held_in_a_tuple(monkeypatch):
    from hermes_cli import kanban_parser

    handlers, _ = _reinstall(monkeypatch, tuple(_upstream_shaped_specs(kanban_parser)))

    guarded = _parser_for_current_specs().parse_args(["comment", "t_x", "hi", "--if-absent"])
    assert guarded.if_absent is True
    assert handlers["comment"] is not _sentinel_handlers()["comment"]
    assert handlers["comment"].__module__ == "hermes_cli.kanban_lifecycle_cli"


class _ReadOnlyTable(dict):
    def update(self, *args, **kwargs):
        raise TypeError("handler table is read-only")


@pytest.mark.parametrize(
    "reshape",
    [
        pytest.param(lambda specs, handlers: ([{"name": "claim"}], handlers), id="records-are-dicts"),
        pytest.param(lambda specs, handlers: (specs[:3], handlers), id="a-verb-is-missing"),
        pytest.param(lambda specs, handlers: (specs, _ReadOnlyTable(handlers)), id="read-only-handlers"),
        pytest.param(lambda specs, handlers: (specs, None), id="no-handler-table"),
    ],
)
def test_a_failed_install_leaves_upstreams_cli_exactly_as_it_was(monkeypatch, caplog, reshape):
    from hermes_cli import kanban_lifecycle_cli as lifecycle_cli
    from hermes_cli import kanban_parser

    specs, handlers = reshape(_upstream_shaped_specs(kanban_parser), _sentinel_handlers())
    specs_before = list(specs)
    handlers_before = dict(handlers) if handlers is not None else None
    monkeypatch.setattr(kanban_parser, "_SPECS", specs)
    monkeypatch.setattr(lifecycle_cli, "_UPSTREAM", {})
    monkeypatch.setattr(lifecycle_cli, "_ADDED", {})
    monkeypatch.setattr(lifecycle_cli, "_INSTALLED_ARGS", [])

    with caplog.at_level(logging.ERROR, logger="hermes_cli.kanban_lifecycle_cli"):
        lifecycle_cli.install(handlers)  # must not raise

    assert kanban_parser._SPECS is specs and list(specs) == specs_before
    if handlers is not None:
        assert dict(handlers) == handlers_before
    assert lifecycle_cli._UPSTREAM == {} and lifecycle_cli._ADDED == {}
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "not installed" in errors[0].getMessage()


def test_guards_are_installed_again_on_a_fresh_handler_table(kanban_home):
    """Re-importing the CLI module builds a new handler table; the guard must follow it."""
    import importlib

    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="reload", assignee="alice")
        kb.recompute_ready(conn)
    stale_table = cli._HANDLERS
    cli = importlib.reload(cli)
    assert cli._HANDLERS is not stale_table

    refused = cli.run_slash(f"comment {tid} late note --expected-status running")
    assert "expected status" in refused
    with kbc.connect_closing() as conn:
        assert kb.list_comments(conn, tid) == []
    # An ordinary call still reaches upstream's handler through the new table.
    assert f"Comment added to {tid}" in cli.run_slash(f"comment {tid} plain note")


def test_complete_refusal_names_the_run_id_when_the_status_matched(kanban_home):
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="stale run", assignee="alice")
        kb.recompute_ready(conn)
        run_id = kb.claim_task(conn, tid).current_run_id

    refused = cli.run_slash(
        f"complete {tid} --result x --expected-status running --expected-run-id {run_id + 41}"
    )
    assert f"expected run id {run_id + 41}, task current run is {run_id}" in refused
    assert "expected status" not in refused
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
