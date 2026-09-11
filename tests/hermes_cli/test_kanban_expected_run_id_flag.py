"""LT-3: explicit ``--expected-run-id`` ownership guard on lifecycle verbs.

Behavior contract (CLI-process level):

* ``complete``, ``block``, and ``request-review`` accept an explicit
  ``--expected-run-id`` that is passed to the DB-layer compare-and-swap
  guard. ``comment`` and ``unblock`` do NOT (their DB functions carry no
  ``expected_run_id`` parameter), and must keep rejecting the flag.
* The explicit value must be a positive, bool-excluding integer.
* The ``HERMES_KANBAN_TASK``/``HERMES_KANBAN_RUN_ID`` fallback is
  retained. When the explicit flag and the env attestation both exist,
  they must agree exactly: the env task must equal the target task and
  the env run id must equal the explicit value.
* Any malformed value, explicit/env disagreement, foreign env task, or
  stale run id exits nonzero and leaves every persisted row (tasks,
  comments, events, runs, hooks, audits, attachments) exactly unchanged.
* Omitting the flag preserves today's behavior byte-for-byte.

Every test runs the real CLI as a subprocess against an isolated
HERMES_HOME so exit codes, stderr prose, and side effects are observed at
the process boundary.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import pytest

ROOT = Path(__file__).parents[2]

ENV_TASK = "HERMES_KANBAN_TASK"
ENV_RUN_ID = "HERMES_KANBAN_RUN_ID"


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
        ENV_TASK,
        ENV_RUN_ID,
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


def _running_task(home: Path, title: str = "lt3 guard probe") -> tuple[str, int]:
    """Create + claim a task; return (task_id, current_run_id)."""
    created = _run_hermes(home, "kanban", "create", title, "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr
    task = _show(home, task_id)["task"]
    assert task["status"] == "running"
    run_id = task["current_run_id"]
    assert isinstance(run_id, int) and run_id > 0
    return task_id, run_id


def _dump_db(home: Path) -> dict:
    """Full-table dump of the board DB: the 'exactly unchanged' oracle.

    Covers every persisted surface the contract names — tasks, comments,
    events, runs, hook/audit rows, and attachment metadata — without
    depending on this test knowing the schema.
    """
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


def _complete_argv(tid: str) -> list[str]:
    return ["kanban", "complete", tid, "--result", "lt3 done"]


def _block_argv(tid: str) -> list[str]:
    return ["kanban", "block", tid, "lt3", "stuck"]


def _request_review_argv(tid: str) -> list[str]:
    return ["kanban", "request-review", tid, "--summary", "lt3 evidence"]


# (argv builder, post-success status, stale-refusal stderr phrase,
#  whether the verb succeeds today on a claimed running task with
#  neither flag nor env attestation)
VERBS = [
    pytest.param(_complete_argv, "done", "refusing to complete", True, id="complete"),
    pytest.param(_block_argv, "blocked", "refusing to block", True, id="block"),
    pytest.param(
        _request_review_argv,
        "review",
        "cannot request review",
        False,
        id="request-review",
    ),
]


@pytest.mark.parametrize("argv_for,ok_status,stale_phrase,no_env_ok", VERBS)
class TestExpectedRunIdFlag:
    def test_explicit_matching_run_id_succeeds(
        self,
        tmp_path: Path,
        argv_for: Callable[[str], list[str]],
        ok_status: str,
        stale_phrase: str,
        no_env_ok: bool,
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home, *argv_for(tid), "--expected-run-id", str(run_id)
        )
        assert result.returncode == 0, result.stderr
        envelope = _show(home, tid)
        assert envelope["task"]["status"] == ok_status
        if argv_for is _block_argv:
            # The reason comment must survive the transactional binding.
            assert "BLOCKED: lt3 stuck" in json.dumps(envelope["comments"])

    def test_explicit_and_matching_env_succeeds(
        self,
        tmp_path: Path,
        argv_for: Callable[[str], list[str]],
        ok_status: str,
        stale_phrase: str,
        no_env_ok: bool,
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home,
            *argv_for(tid),
            "--expected-run-id",
            str(run_id),
            env_extra={ENV_TASK: tid, ENV_RUN_ID: str(run_id)},
        )
        assert result.returncode == 0, result.stderr
        assert _show(home, tid)["task"]["status"] == ok_status

    def test_stale_explicit_run_id_refuses_without_side_effects(
        self,
        tmp_path: Path,
        argv_for: Callable[[str], list[str]],
        ok_status: str,
        stale_phrase: str,
        no_env_ok: bool,
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        before = _dump_db(home)
        result = _run_hermes(
            home, *argv_for(tid), "--expected-run-id", str(run_id + 41)
        )
        assert result.returncode != 0
        assert stale_phrase in result.stderr
        assert _dump_db(home) == before

    @pytest.mark.parametrize("bad", ["0", "-3", "True"])
    def test_malformed_explicit_run_id_refuses_without_side_effects(
        self,
        tmp_path: Path,
        argv_for: Callable[[str], list[str]],
        ok_status: str,
        stale_phrase: str,
        no_env_ok: bool,
        bad: str,
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, _run_id = _running_task(home)
        before = _dump_db(home)
        result = _run_hermes(home, *argv_for(tid), f"--expected-run-id={bad}")
        assert result.returncode != 0
        assert "must be a positive integer" in result.stderr
        assert _dump_db(home) == before

    def test_explicit_env_run_id_mismatch_refuses_without_side_effects(
        self,
        tmp_path: Path,
        argv_for: Callable[[str], list[str]],
        ok_status: str,
        stale_phrase: str,
        no_env_ok: bool,
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        before = _dump_db(home)
        result = _run_hermes(
            home,
            *argv_for(tid),
            "--expected-run-id",
            str(run_id),
            env_extra={ENV_TASK: tid, ENV_RUN_ID: str(run_id + 7)},
        )
        assert result.returncode != 0
        assert "does not match HERMES_KANBAN_RUN_ID" in result.stderr
        assert _dump_db(home) == before

    def test_foreign_env_task_refuses_without_side_effects(
        self,
        tmp_path: Path,
        argv_for: Callable[[str], list[str]],
        ok_status: str,
        stale_phrase: str,
        no_env_ok: bool,
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        other_tid, _other_run = _running_task(home, "lt3 foreign env task")
        before = _dump_db(home)
        result = _run_hermes(
            home,
            *argv_for(tid),
            "--expected-run-id",
            str(run_id),
            env_extra={ENV_TASK: other_tid, ENV_RUN_ID: str(run_id)},
        )
        assert result.returncode != 0
        assert "HERMES_KANBAN_TASK" in result.stderr
        assert _dump_db(home) == before

    def test_omitted_flag_env_fallback_unchanged(
        self,
        tmp_path: Path,
        argv_for: Callable[[str], list[str]],
        ok_status: str,
        stale_phrase: str,
        no_env_ok: bool,
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home,
            *argv_for(tid),
            env_extra={ENV_TASK: tid, ENV_RUN_ID: str(run_id)},
        )
        assert result.returncode == 0, result.stderr
        assert _show(home, tid)["task"]["status"] == ok_status

    def test_omitted_flag_without_env_baseline_unchanged(
        self,
        tmp_path: Path,
        argv_for: Callable[[str], list[str]],
        ok_status: str,
        stale_phrase: str,
        no_env_ok: bool,
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, _run_id = _running_task(home)
        result = _run_hermes(home, *argv_for(tid))
        if no_env_ok:
            assert result.returncode == 0, result.stderr
            assert _show(home, tid)["task"]["status"] == ok_status
        else:
            # request-review keeps refusing an ownerless handoff of a
            # claimed running task (pre-existing live-claim guard).
            assert result.returncode != 0
            assert stale_phrase in result.stderr


class TestLegacyRunGuardCompatibility:
    def test_complete_expected_status_refusal_prose_is_byte_identical(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        created = _run_hermes(home, "kanban", "create", "legacy prose", "--json")
        tid = json.loads(created.stdout)["id"]
        result = _run_hermes(
            home,
            "kanban",
            "complete",
            tid,
            "--result",
            "x",
            "--expected-status",
            "running",
        )
        assert result.returncode == 1
        assert result.stderr == (
            f"refusing to complete {tid}: expected status 'running', "
            "task is 'ready'\n"
        )

    def test_env_only_complete_guard_keeps_generic_refusal_prose(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home,
            "kanban",
            "complete",
            tid,
            "--result",
            "x",
            env_extra={ENV_TASK: tid, ENV_RUN_ID: str(run_id + 1)},
        )
        assert result.returncode == 1
        assert result.stderr == (
            f"cannot complete {tid} (unknown id or terminal state)\n"
        )

    def test_env_only_block_guard_keeps_comment_first_and_generic_prose(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        result = _run_hermes(
            home,
            "kanban",
            "block",
            tid,
            "legacy",
            "reason",
            env_extra={ENV_TASK: tid, ENV_RUN_ID: str(run_id + 1)},
        )
        assert result.returncode == 1
        assert result.stderr == f"cannot block {tid}\n"
        shown = _show(home, tid)
        assert [comment["body"] for comment in shown["comments"]] == [
            "BLOCKED: legacy reason"
        ]


class TestFlagScopeAndBulkGuards:
    def test_complete_multiple_ids_with_flag_refused(self, tmp_path: Path) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid_a, run_a = _running_task(home, "lt3 multi a")
        tid_b, _run_b = _running_task(home, "lt3 multi b")
        before = _dump_db(home)
        result = _run_hermes(
            home,
            "kanban",
            "complete",
            tid_a,
            tid_b,
            "--result",
            "x",
            "--expected-run-id",
            str(run_a),
        )
        assert result.returncode != 0
        assert "multiple task ids" in result.stderr
        assert _dump_db(home) == before

    def test_block_bulk_ids_with_flag_refused(self, tmp_path: Path) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        tid_a, run_a = _running_task(home, "lt3 bulk a")
        tid_b, _run_b = _running_task(home, "lt3 bulk b")
        before = _dump_db(home)
        result = _run_hermes(
            home,
            "kanban",
            "block",
            tid_a,
            "stuck",
            "--ids",
            tid_b,
            "--expected-run-id",
            str(run_a),
        )
        assert result.returncode != 0
        assert "multiple task ids" in result.stderr
        assert _dump_db(home) == before

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["kanban", "comment", "{tid}", "hello"], id="comment"),
            pytest.param(["kanban", "unblock", "{tid}"], id="unblock"),
        ],
    )
    def test_unsupported_verbs_keep_rejecting_the_flag(
        self, tmp_path: Path, argv: list[str]
    ) -> None:
        """add_comment/unblock_task have no expected_run_id parameter; the
        CLI must not silently accept-and-ignore the flag there."""
        home = tmp_path / "hermes"
        home.mkdir()
        tid, run_id = _running_task(home)
        before = _dump_db(home)
        args = [a.format(tid=tid) for a in argv]
        result = _run_hermes(home, *args, "--expected-run-id", str(run_id))
        assert result.returncode != 0
        assert "unrecognized arguments" in result.stderr
        assert _dump_db(home) == before
