"""Regression coverage for Kanban CLI process exit status propagation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


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
