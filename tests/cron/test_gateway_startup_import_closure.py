"""Import-closure and behaviour regression for the cron store after the upstream sync merge.

The merge that produced 9eaa008 (``sync(upstream): merge e05b16348b1d into main``) left
``cron/jobs.py`` with merge-corrupted declarations: duplicated ``def`` headers and closing
parentheses, a mangled script-failure-policy block, upstream-style timezone helper bodies grafted
under the fork's instant-based helpers (a second ``_instant_before`` with a different signature),
and shredded fragments of the snapshot/pin routing functions. The gateway boots the cron ticker
through ``cron.scheduler_provider`` and the ``cron`` package entry, so a SyntaxError there is a
gateway startup failure, not a cron-only bug.

These tests target behaviour, never source shape:

* a cold, isolated interpreter (``-I -S -B``, private pycache prefix, worktree root first on
  ``sys.path`` ahead of the approved dependency directories only) imports the gateway's cron
  closure from THIS worktree and resolves every export that has a live callsite;
* the fork semantics that have real consumers survive: instant-based timezone helpers
  (``cron/quota_hold.py``, ``cron/occurrences.py``, ``cron/scheduler_provider.py``), the
  ``pinned`` main-model pin (``create_job``/``update_job``), and the resume-keeps-due-slot rule;
* the upstream semantics the head tree already depends on survive: the script failure policy
  (also covered end-to-end by ``test_cron_script_failure_policy.py``) and the unpinned
  model-default snapshot that ``tools/cronjob_tools.py`` imports (``resnapshot_job``).

Every import of the module under test happens inside a test function, so this file collects even
while the source is syntactically broken; the failure is then the module's own error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[2]

# Top-level packages/modules of this product tree that the cron closure imports. Each must
# resolve from the worktree root (which the child puts first on ``sys.path``), never from an
# installed wheel, an editable hook, or a sibling checkout.
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

# Exports with live callsites in the head tree. ``callable`` entries must be functions/classes,
# ``present`` entries only need to exist (constants).
COLD_IMPORT_TARGETS = {
    "cron": {
        "callable": [
            "tick",
            "create_job",
            "get_job",
            "list_jobs",
            "remove_job",
            "update_job",
            "pause_job",
            "resume_job",
            "trigger_job",
            "rearm_oneshot",
        ],
        "present": ["JOBS_FILE", "__all__"],
    },
    "cron.scheduler_provider": {
        "callable": [
            "CronScheduler",
            "InProcessCronScheduler",
            "resolve_cron_scheduler",
            "scheduler_for_profile_mode",
            "fire_overdue_jobs",
            "_profile_cron_scope",
            "routed_profile_fire",
            "provider_supports_force_fire",
        ],
        "present": ["DEFAULT_MISFIRE_GRACE_MINUTES"],
    },
    "cron.jobs": {
        "callable": [
            # Store API (cron/__init__.py, tools/cronjob_tools.py, hermes_cli/cron.py).
            "create_job",
            "get_job",
            "list_jobs",
            "update_job",
            "remove_job",
            "pause_job",
            "resume_job",
            "trigger_job",
            "rearm_oneshot",
            "load_jobs",
            "save_jobs",
            "use_cron_store",
            "get_due_jobs",
            "claim_job_for_fire",
            "mark_job_run",
            "compute_next_run",
            "parse_schedule",
            "parse_duration",
            "is_job_runnable",
            "resolve_job_ref",
            # Script failure policy (upstream feature; create/update/scheduler callsites).
            "validate_script_failure_policy",
            "get_job_script_failure_policy",
            "_normalize_script_failure_policy",
            # Main-model pin routing (fork feature; create_job ``pinned`` / update ``pinned``).
            "_main_model_pin",
            "_apply_pin_update",
            # Unpinned model-default snapshots (upstream feature; tools/cronjob_tools.py imports).
            "resnapshot_job",
            "resnapshot_all_unpinned",
            # Instant-based helpers consumed by siblings: cron/quota_hold.py,
            # cron/occurrences.py, cron/unreachable_retry.py, cron/scheduler_provider.py.
            "_elapsed_seconds",
            "_ensure_aware",
            "_parse_aware",
            "_instant_after",
            "_instant_before",
            "_instant_at_or_before",
            "_seconds_after",
            "_schedule_cadence_seconds",
            "_claim_is_live",
            "_job_running_in_this_process",
            "_machine_id",
            "_hermes_now",
            # Ticker markers the scheduler provider writes every cycle.
            "record_ticker_heartbeat",
            "record_ticker_error",
            "clear_ticker_error",
        ],
        "present": [
            "ONESHOT_GRACE_SECONDS",
            "SCRIPT_FAILURE_POLICIES",
            "SCRIPT_FAILURE_POLICY_WITHOUT_SCRIPT_ERROR",
            "JOBS_FILE",
        ],
    },
    "tools.kanban_tools": {
        "callable": [
            "register_current_worker_from_env",
            "heartbeat_current_worker_from_env",
            "inject_new_comments_from_env",
            "_persisted_identity",
            "_kanban_handler",
            "_worker_guard",
            "_check_kanban_mode",
            "_check_kanban_orchestrator_mode",
        ],
        "present": [
            "_RUN_LIFECYCLE_TOOLS",
            "_UNDECLARED_ARGS",
            "KANBAN_LIST_MAX_LIMIT",
        ],
    },
}

# Runs in the child. Imports are deliberately unguarded: the module's own SyntaxError/ImportError
# must surface as the child's traceback (the causal RED), never be summarised away.
_CHILD_PROGRAM = r"""
import importlib
import json
import os
import pathlib
import sys

spec = json.loads(sys.argv[1])
root = os.path.realpath(spec["root"])
sys.path[:0] = [root] + list(spec["dependency_dirs"])

target = importlib.import_module(spec["module"])


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
for name in spec["callable"] + spec["present"]:
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
if spec["module"] == "cron":
    report["all"] = {
        name: {"present": hasattr(target, name), "callable": callable(getattr(target, name, None))}
        for name in target.__all__
    }
    report["jobs_file_is_path"] = isinstance(getattr(target, "JOBS_FILE", None), pathlib.PurePath)

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
    ``tmp_path`` (never the system temp root), no credentials, no behavioural HERMES_* flags."""
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
        # Keeps hermes_state's live-DB guard armed in the child (tests/conftest.py, #82770).
        "HERMES_TEST_ISOLATION": str(hermes_home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
    })
    return env


def _cold_import_report(tmp_path: Path, module: str) -> dict:
    """Import *module* in a fresh isolated interpreter rooted at this worktree and return the
    child's JSON report. A non-zero exit (SyntaxError, ImportError, NameError at import time)
    fails the test with the child's traceback."""
    target = COLD_IMPORT_TARGETS[module]
    report_path = tmp_path / "import-closure-report.json"
    spec = {
        "root": str(WORKTREE_ROOT),
        "dependency_dirs": _approved_dependency_dirs(),
        "module": module,
        "callable": target["callable"],
        "present": target["present"],
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
        f"cold import of {module} from {WORKTREE_ROOT} failed (exit {proc.returncode})\n"
        f"--- stderr tail ---\n{proc.stderr[-8000:]}\n--- stdout tail ---\n{proc.stdout[-2000:]}"
    )
    assert report_path.is_file(), (
        f"child imported {module} but wrote no report\n{proc.stderr[-4000:]}"
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("module", sorted(COLD_IMPORT_TARGETS))
def test_cold_isolated_interpreter_imports_module_from_this_worktree(tmp_path, module):
    report = _cold_import_report(tmp_path, module)

    assert report["flags"]["isolated"] and report["flags"]["no_site"]
    assert report["flags"]["pycache_prefix"], (
        "child must not read bytecode beside the sources"
    )

    module_file = report["module_file"]
    assert module_file, f"{module} has no __file__ in the child"
    assert Path(module_file).resolve().is_relative_to(WORKTREE_ROOT), (
        f"{module} resolved outside the worktree: {module_file}"
    )
    assert report["foreign"] == {}, (
        "product modules imported from outside the worktree (stale tree or installed copy): "
        f"{report['foreign']}"
    )

    target = COLD_IMPORT_TARGETS[module]
    missing = [
        name
        for name in target["callable"] + target["present"]
        if not report["attrs"][name]["present"]
    ]
    assert missing == [], f"{module} lost exports with live callsites: {missing}"
    not_callable = [
        name for name in target["callable"] if not report["attrs"][name]["callable"]
    ]
    assert not_callable == [], (
        f"{module} exports are no longer callable: {not_callable}"
    )

    if module == "cron":
        broken_api = {
            name: info
            for name, info in report["all"].items()
            if name != "JOBS_FILE" and not info["callable"]
        }
        assert broken_api == {}, (
            f"cron.__all__ names that are not callable: {broken_api}"
        )
        assert report["all"].get("JOBS_FILE", {}).get("present"), (
            "cron.__all__ lost JOBS_FILE"
        )
        assert report["jobs_file_is_path"], "cron.JOBS_FILE must be a filesystem path"


# --- Script failure policy (upstream feature the head tree validates on create/update/run) ---


@pytest.mark.parametrize("policy", ["continue", "fail_closed"])
def test_validate_script_failure_policy_accepts_canonical_values(policy):
    from cron.jobs import SCRIPT_FAILURE_POLICIES, validate_script_failure_policy

    assert policy in SCRIPT_FAILURE_POLICIES
    assert validate_script_failure_policy(policy) == policy


@pytest.mark.parametrize("policy", ["FAIL_CLOSED", "fail-open", "", None, 1])
def test_validate_script_failure_policy_rejects_malformed_values(policy):
    from cron.jobs import validate_script_failure_policy

    with pytest.raises(ValueError, match="script_failure_policy"):
        validate_script_failure_policy(policy)


def test_stored_policy_defaults_only_when_the_key_is_genuinely_absent():
    from cron.jobs import get_job_script_failure_policy

    assert get_job_script_failure_policy({"id": "legacy"}) == "continue"
    assert (
        get_job_script_failure_policy({"script_failure_policy": "fail_closed"})
        == "fail_closed"
    )
    with pytest.raises(ValueError, match="script_failure_policy"):
        get_job_script_failure_policy({"script_failure_policy": None})


def test_create_time_policy_defaults_none_but_rejects_explicit_garbage():
    from cron.jobs import _normalize_script_failure_policy

    assert _normalize_script_failure_policy(None) == "continue"
    assert _normalize_script_failure_policy("fail_closed") == "fail_closed"
    with pytest.raises(ValueError, match="script_failure_policy"):
        _normalize_script_failure_policy("")


# --- Main-model pin routing (fork feature: ``pinned`` is a per-job lock, never stored) ---


@pytest.mark.parametrize(
    ("job", "updates", "expected"),
    [
        pytest.param(
            {"model": "m", "provider": "p"},
            {"pinned": False},
            {"provider": None, "model": None},
            id="release-follows-main-model",
        ),
        pytest.param(
            {"model": None},
            {"pinned": True, "model": "explicit/model"},
            {"model": "explicit/model"},
            id="explicit-model-wins-over-pin",
        ),
        pytest.param(
            {"model": "already/pinned"},
            {"pinned": True},
            {},
            id="already-pinned-untouched",
        ),
        pytest.param(
            {"model": "m"},
            {"schedule": "every 1h"},
            {"schedule": "every 1h"},
            id="no-pinned-key-no-op",
        ),
    ],
)
def test_apply_pin_update_rewrites_pin_without_storing_the_flag(job, updates, expected):
    from cron.jobs import _apply_pin_update

    _apply_pin_update(job, updates)

    assert "pinned" not in updates
    assert updates == expected


def _write_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_pinned_job_locks_the_main_model_and_release_unlocks_it():
    from cron import jobs

    home = Path(os.environ["HERMES_HOME"])
    _write_config(home, "model:\n  default: test/main-default\n")
    with jobs.use_cron_store(home):
        unpinned = jobs.create_job(prompt="brief", schedule="every 1h")
        pinned = jobs.create_job(
            prompt="pinned brief", schedule="every 1h", pinned=True
        )
        assert unpinned["model"] is None
        assert pinned["model"] == "test/main-default"

        released = jobs.update_job(pinned["id"], {"pinned": False})
        assert released["model"] is None and released["provider"] is None

        relocked = jobs.update_job(pinned["id"], {"pinned": True})
        assert relocked["model"] == "test/main-default"
        assert jobs.get_job(pinned["id"])["model"] == "test/main-default"


# --- Unpinned model-default snapshot (upstream feature tools/cronjob_tools.py imports) ---


def test_unpinned_job_snapshot_prefers_cron_model_while_pin_uses_main_default():
    from cron import jobs

    home = Path(os.environ["HERMES_HOME"])
    _write_config(
        home,
        "cron:\n  model: test/cron-default\nmodel:\n  default: test/main-default\n",
    )
    with jobs.use_cron_store(home):
        unpinned = jobs.create_job(prompt="brief", schedule="every 1h")
        pinned = jobs.create_job(
            prompt="pinned brief", schedule="every 1h", pinned=True
        )

    assert unpinned["model"] is None
    assert unpinned["model_snapshot"] == "test/cron-default"
    assert pinned["model"] == "test/main-default"


def test_resnapshot_adopts_the_current_default_for_unpinned_axes_only():
    from cron import jobs

    home = Path(os.environ["HERMES_HOME"])
    with jobs.use_cron_store(home):
        unpinned = jobs.create_job(prompt="brief", schedule="every 1h")
        explicit = jobs.create_job(
            prompt="brief", schedule="every 1h", model="explicit/model"
        )
        assert unpinned.get("model_snapshot") is None

        _write_config(home, "model:\n  default: test/adopted-model\n")
        refreshed = jobs.resnapshot_job(unpinned["id"])
        kept = jobs.resnapshot_job(explicit["id"])

    assert refreshed["model_snapshot"] == "test/adopted-model"
    assert refreshed["model"] is None
    assert kept["model"] == "explicit/model"
    assert kept.get("model_snapshot") is None


# --- Instant-based timezone helpers (fork semantics; consumed by quota_hold/occurrences) ---

_A = datetime(2026, 10, 3, 12, 0, tzinfo=timezone(timedelta(hours=2)))  # 10:00Z
_B = datetime(2026, 10, 3, 11, 0, tzinfo=timezone.utc)  # 11:00Z


def test_instant_helpers_compare_absolute_instants_across_offsets():
    from cron.jobs import (
        _elapsed_seconds,
        _instant_after,
        _instant_at_or_before,
        _instant_before,
        _seconds_after,
    )

    assert _elapsed_seconds(_B, _A) == 3600.0
    assert _elapsed_seconds(_A, _B) == -3600.0
    assert _instant_after(_B, _A) is True and _instant_after(_A, _B) is False
    assert _instant_before(_A, _B) is True and _instant_before(_B, _A) is False
    assert (
        _instant_at_or_before(_A, _A) is True and _instant_at_or_before(_B, _A) is False
    )

    later = _seconds_after(_A, 90)
    assert isinstance(later, datetime)
    assert later == _A + timedelta(seconds=90)
    assert later.utcoffset() == _A.utcoffset()


@pytest.mark.parametrize(
    ("offset_seconds", "eligible"),
    [
        pytest.param(-60, True, id="one-minute-late-within-grace"),
        pytest.param(-3600, False, id="one-hour-late-beyond-grace"),
        pytest.param(3600, True, id="future-run"),
    ],
)
def test_one_shot_grace_is_measured_in_elapsed_seconds_not_wall_clock(
    offset_seconds, eligible
):
    from cron.jobs import ONESHOT_GRACE_SECONDS, compute_next_run

    assert ONESHOT_GRACE_SECONDS < 3600
    # Rendered in a non-UTC offset so a wall-clock comparison cannot pass by accident.
    run_at = (
        (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds))
        .astimezone(timezone(timedelta(hours=2)))
        .isoformat()
    )

    result = compute_next_run({"kind": "once", "run_at": run_at})

    assert result == (run_at if eligible else None)
    assert (
        compute_next_run({"kind": "once", "run_at": run_at}, last_run_at=run_at) is None
    )


@pytest.mark.parametrize(
    ("age_seconds", "live"),
    [
        pytest.param(10, True, id="fresh-claim"),
        pytest.param(400, False, id="expired-claim"),
        pytest.param(-60, False, id="future-dated-claim-is-stale"),
    ],
)
def test_claim_liveness_window_counts_elapsed_seconds_from_now(age_seconds, live):
    from cron.jobs import _claim_is_live

    now = datetime.now(timezone.utc)
    claimed_at = (now - timedelta(seconds=age_seconds)).astimezone(
        timezone(timedelta(hours=-5))
    )
    # A foreign host in ``by`` keeps the owner-liveness probe out of the picture.
    claim = {"at": claimed_at.isoformat(), "by": "elsewhere.invalid:1"}

    assert _claim_is_live(claim, now, 300) is live
    assert _claim_is_live(None, now, 300) is False


def test_interval_next_run_anchors_on_the_last_run_instant():
    from cron.jobs import compute_next_run

    last_run = datetime(
        2026, 10, 3, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    result = compute_next_run({"kind": "interval", "minutes": 30}, last_run.isoformat())

    assert datetime.fromisoformat(result) == last_run + timedelta(minutes=30)


def test_resume_keeps_a_recurring_slot_that_elapsed_while_paused():
    from cron import jobs

    home = Path(os.environ["HERMES_HOME"])
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt="brief", schedule="every 1h")
        assert jobs.pause_job(job["id"])["state"] == "paused"
        jobs.update_job(job["id"], {"next_run_at": past})
        resumed = jobs.resume_job(job["id"])

    assert resumed["state"] == "scheduled"
    assert resumed["next_run_at"] == past


# --- Schedule parsing (pure functions; data in, data out) ---


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("30m", 30), ("2h", 120), ("1d", 1440), ("hour", 60), (" 45 min ", 45)],
)
def test_parse_duration_returns_minutes(text, minutes):
    from cron.jobs import parse_duration

    assert parse_duration(text) == minutes


def test_parse_schedule_distinguishes_recurring_interval_from_one_shot_delay():
    from cron.jobs import parse_schedule

    interval = parse_schedule("every 2h")
    assert interval["kind"] == "interval" and interval["minutes"] == 120

    once = parse_schedule("in 30m")
    assert once["kind"] == "once"
    delay = datetime.fromisoformat(once["run_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < delay <= timedelta(minutes=31)

    with pytest.raises(ValueError, match="Invalid schedule"):
        parse_schedule("whenever")


@pytest.mark.parametrize(
    ("skill", "skills", "expected"),
    [
        ("a", None, ["a"]),
        (None, "solo", ["solo"]),
        ("legacy", ["x", " y ", "x", ""], ["x", "y"]),
        (None, None, []),
    ],
)
def test_normalize_skill_list_merges_legacy_and_multi_skill_inputs(
    skill, skills, expected
):
    from cron.jobs import _normalize_skill_list

    assert _normalize_skill_list(skill, skills) == expected
