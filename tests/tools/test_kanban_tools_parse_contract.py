"""Cold-import contract for ``tools.kanban_tools`` after the upstream sync merge.

The merge that produced 9eaa008 (``sync(upstream): merge e05b16348b1d into main``) left the
worker-identity block of ``tools/kanban_tools.py`` without its ``def`` headers: the bodies of
``_persisted_identity`` and ``register_current_worker_from_env`` sit at module scope next to ``_worker_run_id`` and
``_stamp_worker_session_metadata``. A bare ``return`` outside a function is a SyntaxError, so
every importer of the module (gateway bootstrap, the kanban toolset, the dispatcher worker
preamble) fails before any handler runs.

These tests target behaviour, never source shape:

* a cold, isolated interpreter (``-I -S -B``, private pycache prefix, worktree root first on
  ``sys.path`` ahead of the approved dependency directories only) imports the module from THIS
  worktree, and the restored worker-identity callables resolve alongside the surviving ones;
* the pure helpers next to the defect keep their contract (``_worker_run_id``,
  ``_stamp_worker_session_metadata``);
* the mutation guards that gate every run-lifecycle write keep refusing what they must refuse.

Every import of the module under test happens inside a test function, so this file collects
even while the source is syntactically broken; the failure is then the module's own error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[2]

# Top-level packages/modules of this product tree that the kanban closure imports. Each must
# resolve from the worktree root, never from an installed wheel, an editable hook, or a
# sibling checkout.
_PRODUCT_TOP_LEVEL = frozenset({
    "agent",
    "cron",
    "gateway",
    "hermes_bootstrap",
    "hermes_cli",
    "hermes_constants",
    "hermes_logging",
    "hermes_platform",
    "hermes_state",
    "hermes_time",
    "model_tools",
    "plugins",
    "run_agent",
    "tools",
    "toolsets",
    "utils",
})

# Worker-identity callables restored by the repair, next to the neighbours that survived the
# merge and have live callsites in this file's handlers.
RESTORED_CALLABLES = ["_persisted_identity", "register_current_worker_from_env"]
SURVIVING_CALLABLES = [
    "_worker_run_id",
    "_own_task_env",
    "_stamp_worker_session_metadata",
    "_enforce_worker_task_ownership",
    "_reject_delegated_child_mutation",
    "_worker_guard",
    "heartbeat_current_worker_from_env",
    "inject_new_comments_from_env",
    "_kanban_handler",
    "_check_kanban_mode",
    "_check_kanban_orchestrator_mode",
]
PRESENT_CONSTANTS = [
    "_RUN_LIFECYCLE_TOOLS",
    "_UNDECLARED_ARGS",
    "KANBAN_LIST_MAX_LIMIT",
]

# Runs in the child. The import is deliberately unguarded: the module's own SyntaxError must
# surface as the child's traceback (the causal RED), never be summarised away.
_CHILD_PROGRAM = r"""
import importlib
import json
import os
import sys

spec = json.loads(sys.argv[1])
root = os.path.realpath(spec["root"])
sys.path[:0] = [root] + list(spec["dependency_dirs"])

target = importlib.import_module("tools.kanban_tools")


def under_root(path):
    try:
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except ValueError:
        return False


root_names = set()
for entry in os.listdir(root):
    full = os.path.join(root, entry)
    if entry.endswith(".py"):
        root_names.add(entry[:-3])
    elif os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
        root_names.add(entry)
watched = root_names & set(spec["product_top_level"])

foreign = {}
for name, module in list(sys.modules.items()):
    file = getattr(module, "__file__", None)
    if file and name.split(".", 1)[0] in watched and not under_root(file):
        foreign[name] = file

attrs = {}
for name in spec["names"]:
    present = hasattr(target, name)
    attrs[name] = {
        "present": present,
        "callable": bool(present and callable(getattr(target, name))),
    }

report = {
    "module_file": getattr(target, "__file__", None),
    "foreign": foreign,
    "attrs": attrs,
    "flags": {
        "isolated": bool(sys.flags.isolated),
        "no_site": bool(sys.flags.no_site),
        "pycache_prefix": sys.pycache_prefix,
    },
}
# ``_persisted_identity`` is a pure read of profile/OS identity. Called only once it is known
# to be a function so the report always records the real failure mode.
if attrs.get("_persisted_identity", {}).get("callable"):
    identity = target._persisted_identity()
    report["persisted_identity"] = {"type": type(identity).__name__, "value": identity}

with open(spec["report_path"], "w", encoding="utf-8") as handle:
    json.dump(report, handle)
"""


def _approved_dependency_dirs() -> list[str]:
    """Site/dist-packages directories from THIS interpreter's ``sys.path`` and nothing else: no
    editable ``.pth`` hooks (``-S`` never processes them), no other product trees, no PYTHONPATH
    extras, no test directories. The worktree root is prepended separately by the child."""
    approved: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        path = Path(entry)
        if not path.is_dir() or path.name not in {"site-packages", "dist-packages"}:
            continue
        resolved = str(path.resolve())
        if resolved not in approved:
            approved.append(resolved)
    return approved


def _child_env(tmp_path: Path) -> dict[str, str]:
    """A scrubbed environment: private HOME/HERMES_HOME/HERMES_KANBAN_HOME/TMPDIR under
    ``tmp_path`` (never the system temp root), no credentials, no behavioural HERMES_* flags,
    no HERMES_KANBAN_TASK so the module loads as it does for a non-worker process."""
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    kanban_home = tmp_path / "kanban-home"
    scratch = tmp_path / "tmp"
    for directory in (home, hermes_home, kanban_home, scratch):
        directory.mkdir(parents=True, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT")
        if key in os.environ
    }
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HERMES_HOME": str(hermes_home),
        "HERMES_KANBAN_HOME": str(kanban_home),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # A fixed OS user so ``_persisted_identity``'s OS-user fallback is deterministic in the child.
        "USER": "closure-test-user",
        # Keeps hermes_state's live-DB guard armed in the child (tests/conftest.py, #82770).
        "HERMES_TEST_ISOLATION": str(hermes_home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
    })
    return env


def _cold_import_report(tmp_path: Path) -> dict:
    """Import ``tools.kanban_tools`` in a fresh isolated interpreter rooted at this worktree
    and return the child's JSON report. A non-zero exit (SyntaxError, ImportError, NameError
    at import time) fails the test with the child's traceback."""
    report_path = tmp_path / "kanban-import-report.json"
    spec = {
        "root": str(WORKTREE_ROOT),
        "dependency_dirs": _approved_dependency_dirs(),
        "names": RESTORED_CALLABLES + SURVIVING_CALLABLES + PRESENT_CONSTANTS,
        "product_top_level": sorted(_PRODUCT_TOP_LEVEL),
        "report_path": str(report_path),
    }
    pycache = tmp_path / "pycache"
    pycache.mkdir()
    cmd = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-X",
        "utf8",
        "-X",
        f"pycache_prefix={pycache}",
        "-c",
        _CHILD_PROGRAM,
        json.dumps(spec),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(tmp_path),
        env=_child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"cold import of tools.kanban_tools from {WORKTREE_ROOT} failed (exit {proc.returncode})\n"
        f"--- stderr tail ---\n{proc.stderr[-8000:]}\n--- stdout tail ---\n{proc.stdout[-2000:]}"
    )
    assert report_path.is_file(), (
        f"child imported tools.kanban_tools but wrote no report\n{proc.stderr[-4000:]}"
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def test_cold_isolated_interpreter_imports_kanban_tools_from_this_worktree(tmp_path):
    report = _cold_import_report(tmp_path)

    assert report["flags"]["isolated"] and report["flags"]["no_site"]
    assert report["flags"]["pycache_prefix"], (
        "child must not read bytecode beside the sources"
    )

    module_file = report["module_file"]
    assert module_file, "tools.kanban_tools has no __file__ in the child"
    assert Path(module_file).resolve().is_relative_to(WORKTREE_ROOT), (
        f"tools.kanban_tools resolved outside the worktree: {module_file}"
    )
    assert report["foreign"] == {}, (
        "product modules imported from outside the worktree (stale tree or installed copy): "
        f"{report['foreign']}"
    )

    expected_callable = RESTORED_CALLABLES + SURVIVING_CALLABLES
    missing = [
        name
        for name in expected_callable + PRESENT_CONSTANTS
        if not report["attrs"][name]["present"]
    ]
    assert missing == [], f"tools.kanban_tools lost exports: {missing}"
    not_callable = [
        name for name in expected_callable if not report["attrs"][name]["callable"]
    ]
    assert not_callable == [], (
        f"tools.kanban_tools exports are no longer callable: {not_callable}"
    )

    # The restored identity helper resolves an author even with no profile configured: it
    # falls through to the OS user the child was given, never to None or an empty string.
    assert report["persisted_identity"]["type"] == "str"
    assert report["persisted_identity"]["value"].strip()


# --- Pure helpers beside the defect (data in, data out; env-scoped to the worker's own task) ---


@pytest.mark.parametrize(
    ("env_task", "env_run_id", "task_id", "expected"),
    [
        pytest.param("task-a", "42", "task-a", 42, id="own-task-parses-run-id"),
        pytest.param("task-a", "42", "task-b", None, id="foreign-task-sees-no-run-id"),
        pytest.param(
            "task-a", "not-an-int", "task-a", None, id="malformed-run-id-is-unbound"
        ),
        pytest.param("task-a", None, "task-a", None, id="absent-run-id-is-unbound"),
    ],
)
def test_worker_run_id_is_scoped_to_the_workers_own_task(
    monkeypatch, env_task, env_run_id, task_id, expected
):
    from tools.kanban_tools import _worker_run_id

    monkeypatch.setenv("HERMES_KANBAN_TASK", env_task)
    if env_run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", env_run_id)

    assert _worker_run_id(task_id) == expected


def test_stamp_worker_session_metadata_adds_the_session_only_for_the_own_task(
    monkeypatch,
):
    from tools.kanban_tools import _stamp_worker_session_metadata

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-a")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-1")

    stamped = _stamp_worker_session_metadata("task-a", {"artifacts": ["a.txt"]})
    assert stamped == {"artifacts": ["a.txt"], "worker_session_id": "sess-1"}
    assert _stamp_worker_session_metadata("task-a", None) == {
        "worker_session_id": "sess-1"
    }

    # Another task's metadata is passed through untouched (same object, no stamp).
    original = {"artifacts": ["a.txt"]}
    assert _stamp_worker_session_metadata("task-b", original) is original
    assert _stamp_worker_session_metadata("task-b", None) is None


def test_stamp_worker_session_metadata_is_a_no_op_without_a_session(monkeypatch):
    from tools.kanban_tools import _stamp_worker_session_metadata

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-a")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    original = {"k": "v"}
    assert _stamp_worker_session_metadata("task-a", original) is original
    assert _stamp_worker_session_metadata("task-a", None) is None


# --- Mutation guards (every run-lifecycle write goes through these) ---


def _rejection_message(exc_info) -> str:
    """A ``_Reject`` carries the finished ``tool_error`` JSON payload as its only arg."""
    payload = json.loads(exc_info.value.args[0])
    return payload["error"]


def test_worker_refuses_to_mutate_a_task_it_is_not_scoped_to(monkeypatch):
    from tools.kanban_tools import _Reject, _enforce_worker_task_ownership

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-a")

    _enforce_worker_task_ownership("task-a")
    with pytest.raises(_Reject) as exc_info:
        _enforce_worker_task_ownership("task-b")
    message = _rejection_message(exc_info)
    assert "task-a" in message and "task-b" in message

    # Orchestrators and CLI callers (no env task) route child tasks freely.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    _enforce_worker_task_ownership("task-b")


@pytest.mark.parametrize(
    "tool_name",
    sorted([
        "kanban_complete",
        "kanban_block",
        "kanban_schedule",
        "kanban_request_review",
        "kanban_request_changes",
    ]),
)
def test_unbound_worker_is_refused_on_every_run_lifecycle_tool(monkeypatch, tool_name):
    from tools.kanban_tools import _RUN_LIFECYCLE_TOOLS, _Reject, _worker_guard

    assert tool_name in _RUN_LIFECYCLE_TOOLS
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-a")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    with pytest.raises(_Reject) as exc_info:
        _worker_guard(tool_name, {})
    assert "HERMES_KANBAN_RUN_ID" in _rejection_message(exc_info)


def test_bound_worker_passes_the_guard_and_non_lifecycle_tools_skip_the_run_id_check(
    monkeypatch,
):
    from tools.kanban_tools import _RUN_LIFECYCLE_TOOLS, _worker_guard

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-a")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    # Bound worker: the env task is the default task id and the run id proves ownership.
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    assert _worker_guard("kanban_complete", {}) == "task-a"

    # Heartbeat never terminates a run, so an unbound worker may still signal liveness.
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert "kanban_heartbeat" not in _RUN_LIFECYCLE_TOOLS
    assert _worker_guard("kanban_heartbeat", {}) == "task-a"
