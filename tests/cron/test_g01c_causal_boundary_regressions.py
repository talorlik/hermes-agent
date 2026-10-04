"""Causal-boundary tests for two confirmed RED mechanisms on the frozen G01C candidate.

Independent of the broad acceptance suite: each test isolates ONE mechanism at the smallest
production seam that exhibits it, with a positive control beside it so a pass is evidence that
the seam works and a fail is evidence of the mechanism, not of the harness.

Mechanism A — unsubmitted fallback send coroutine (cron/scheduler_delivery.py:1719-1736).
    ``_standalone_send`` builds ``coro = _send()`` and runs it with ``asyncio.run``. When that
    raises the running-loop ``RuntimeError`` it closes THAT coroutine, then evaluates a SECOND
    ``_send()`` inline as an argument of ``pool.submit(...)``. If ``submit`` itself raises (the
    interpreter-shutdown race the lane exists for) the second coroutine is never awaited and never
    closed. Verdict is read with ``inspect.getcoroutinestate`` on retained references, never from
    GC warnings; the fixture closes any still-unstarted coroutine in teardown so a leak cannot
    pollute later tests, without changing the verdict already recorded.

Mechanism B — cross-profile Bot Chat child env (cron/scheduler_delivery.py:898-905).
    ``_deliver_to_bot_chat`` builds the child env as
    ``strip_launch_profile_env(delegated_child_subprocess_env(os.environ))`` with NO target home,
    then overwrites ``HERMES_HOME``. With no active home override the strip is a no-op, so a turn
    spawned for ANOTHER profile inherits the launch profile's authorization gates and credentials.
    The contract (tests/cron/test_bot_chat_delivery_child_env.py) is that the env is built for the
    TARGET via ``tools.environments.local.served_profile_child_env`` and that a failure to build it
    is refused as a string before any child is spawned. No real spawn, no network: the spawn seam
    ``_run_bot_chat_turn`` is captured, live-owner and deferred-lane lookups are pinned to "none".

Six test IDs total: three per mechanism (two controls + one regression each).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import cron.scheduler_delivery as delivery


# ---------------------------------------------------------------------------------------------
# Mechanism A — standalone send lane / fallback coroutine lifecycle
# ---------------------------------------------------------------------------------------------

# Documented message asyncio.run raises inside a running loop (first lane refusal).
_RUNNING_LOOP_ERROR = "asyncio.run() cannot be called from a running event loop"
# Documented message concurrent.futures raises once the interpreter finalizes (submit refusal).
_SHUTDOWN_ERROR = "cannot schedule new futures after interpreter shutdown"


def _lightweight_target(job_id: str = "causal-probe") -> SimpleNamespace:
    """The handful of ``_TargetDelivery`` attributes ``_standalone_send`` actually reads."""
    return SimpleNamespace(
        job={"id": job_id},
        platform="slack",
        pconfig=None,
        chat_id="D123",
        thread_id=None,
        where="slack:D123",
    )


class _RejectingPool:
    """ThreadPoolExecutor whose ``submit`` dies the way it does during interpreter shutdown."""

    def __init__(self, *args, **kwargs):
        pass

    def submit(self, fn, *args, **kwargs):
        raise RuntimeError(_SHUTDOWN_ERROR)

    def shutdown(self, *args, **kwargs):
        pass


class _InlinePool:
    """ThreadPoolExecutor that runs the submitted callable synchronously (positive control)."""

    def __init__(self, *args, **kwargs):
        pass

    def submit(self, fn, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — mirrored into the future like a real pool
            future.set_exception(exc)
        return future

    def shutdown(self, *args, **kwargs):
        pass


@pytest.fixture
def captured_send(monkeypatch):
    """Stub the standalone transport and retain every coroutine it allocates.

    Teardown closes any coroutine still in ``CORO_CREATED`` so a production leak never surfaces as
    a ``RuntimeWarning`` in an unrelated test. Teardown runs AFTER the test body's assertions, so
    the recorded verdict is unaffected.
    """
    import tools.send_message_tool as send_message_tool

    coroutines: list = []

    async def _transport(*args, **kwargs):
        return {"success": True}

    def _factory(*args, **kwargs):
        coro = _transport(*args, **kwargs)
        coroutines.append(coro)
        return coro

    monkeypatch.setattr(send_message_tool, "_send_to_platform", _factory)
    try:
        yield coroutines
    finally:
        for coro in coroutines:
            if inspect.getcoroutinestate(coro) == inspect.CORO_CREATED:
                coro.close()


@pytest.fixture
def running_loop_on_first_run(monkeypatch):
    """``asyncio.run`` refuses once with the documented running-loop error, then runs for real.

    The first refusal is the production trigger for the thread-pool fallback; a later call (the
    inline pool of the positive control) must genuinely drive the coroutine so the control proves
    the fallback lane itself is sound.
    """
    real_run = asyncio.run
    calls: list = []

    def _run(coro, *args, **kwargs):
        calls.append(coro)
        if len(calls) == 1:
            raise RuntimeError(_RUNNING_LOOP_ERROR)
        return real_run(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, "run", _run)
    return calls


def test_standalone_send_direct_lane_runs_single_coroutine_control(captured_send):
    """Control: with a usable ``asyncio.run`` exactly one send coroutine is built and consumed.

    Expected PASS on the frozen candidate.
    """
    result, error = delivery._standalone_send(_lightweight_target(), "scheduled result", [])

    assert error is None
    assert result == {"success": True}
    assert [inspect.getcoroutinestate(c) for c in captured_send] == [inspect.CORO_CLOSED]


def test_standalone_send_fallback_pool_executes_second_coroutine_control(
    captured_send, running_loop_on_first_run, monkeypatch,
):
    """Control: running-loop refusal -> first coroutine closed by production, second coroutine
    submitted to a pool that executes it -> both CLOSED and the send result is returned.

    Expected PASS on the frozen candidate.
    """
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _InlinePool)

    result, error = delivery._standalone_send(_lightweight_target(), "scheduled result", [])

    assert error is None
    assert result == {"success": True}
    assert len(running_loop_on_first_run) == 2, "fallback lane never reached asyncio.run again"
    assert len(captured_send) == 2, "fallback lane did not allocate its own send coroutine"
    assert [inspect.getcoroutinestate(c) for c in captured_send] == [
        inspect.CORO_CLOSED, inspect.CORO_CLOSED]


def test_standalone_send_pool_submit_rejection_closes_unsubmitted_coroutine(
    captured_send, running_loop_on_first_run, monkeypatch,
):
    """Regression: when ``pool.submit`` raises, the coroutine built inline as its argument must
    still be closed. The lane must report the failure as ``(None, <str>)``, not raise, and must
    not leave a never-awaited coroutine behind.

    Expected FAIL on the frozen candidate: the second coroutine stays ``CORO_CREATED``
    (cron/scheduler_delivery.py:1733 evaluates ``_send()`` as a ``submit`` argument and nothing
    closes it when ``submit`` raises).
    """
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _RejectingPool)

    result, error = delivery._standalone_send(_lightweight_target(), "scheduled result", [])

    # The lane's own contract: failures are reported, never raised past remaining targets.
    assert result is None
    assert isinstance(error, str) and error
    # Two coroutines were allocated: the direct-lane one and the fallback one. If only one shows
    # up the first-lane error was misclassified as shutdown and this test is misdirected.
    assert len(captured_send) == 2, (
        "fallback lane did not allocate its send coroutine; running-loop error was not treated "
        "as a retry trigger")
    direct_lane, fallback_lane = captured_send
    # Production closes the direct-lane coroutine explicitly (positive half of the boundary).
    assert inspect.getcoroutinestate(direct_lane) == inspect.CORO_CLOSED
    # The boundary under test: a submit rejection must not orphan the fallback coroutine.
    assert inspect.getcoroutinestate(fallback_lane) == inspect.CORO_CLOSED, (
        "fallback send coroutine was allocated as a pool.submit argument and left unawaited "
        "and unclosed after submit raised")


# ---------------------------------------------------------------------------------------------
# Mechanism B — Bot Chat CLI lane child environment for another profile
# ---------------------------------------------------------------------------------------------

# What a gateway process that loaded the ROOT profile's .env holds in os.environ.
LAUNCH_ENV = {
    "HERMES_MODEL": "root-model",
    "HERMES_LANGUAGE": "en",
    "TERMINAL_ENV": "docker",
    "TERMINAL_DOCKER_IMAGE": "root-only-image",
    "DISCORD_ALLOWED_USERS": "root-operator",  # an authorization gate (#113270)
    "DISCORD_IGNORED_CHANNELS": "999",
    "ANTHROPIC_API_KEY": "sk-root",
}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A root-profile gateway (fresh tmp HERMES_HOME) and a second profile to deliver into."""
    root = tmp_path / "hermes"
    beta = root / "profiles" / "beta"
    beta.mkdir(parents=True)
    (root / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in LAUNCH_ENV.items()) + "\n", encoding="utf-8")
    (beta / ".env").write_text("ANTHROPIC_API_KEY=sk-beta\nHERMES_LANGUAGE=ja\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    for key, value in LAUNCH_ENV.items():
        monkeypatch.setenv(key, value)
    return root, beta


def _must_not_run(label: str):
    def _refuse(*args, **kwargs):
        raise AssertionError(f"{label} must not run in this test")
    return _refuse


@pytest.fixture
def quiet_bot_chat_lane(monkeypatch):
    """Pin every non-spawn seam of the CLI lane so the run is deterministic and offline:
    no receipt, no live owner, no legacy owner (so nothing is deferred), config without a
    timeout override, and the spawn itself captured instead of executed.

    Returns the dict the fake spawn fills: ``{}`` means no child was built.
    """
    import cron.bot_chat_delivery as deferred_lane
    import tools.bot_live_delivery as mailbox

    monkeypatch.setattr(mailbox, "read_delivery_result", lambda *a, **k: None)
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda *a, **k: None)
    monkeypatch.setattr(mailbox, "find_canonical_owner", lambda *a, **k: None)
    monkeypatch.setattr(mailbox, "deliver_to_live_owner", _must_not_run("live-owner delivery"))
    monkeypatch.setattr(deferred_lane, "read_pending", lambda *a, **k: None)
    monkeypatch.setattr(deferred_lane, "defer", _must_not_run("deferred bot-chat lane"))
    monkeypatch.setattr(delivery._sched, "load_config", lambda: {"cron": {}})

    captured: dict = {}

    def _fake_turn(argv, env, report_path, timeout):
        captured.update(env=dict(env), argv=list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(delivery, "_run_bot_chat_turn", _fake_turn)
    return captured


def test_bot_chat_own_profile_turn_keeps_launch_environment_control(fleet, quiet_bot_chat_lane):
    """Control: ``deliver: bot-chat`` (no profile) is the job's own home; nothing is stripped and
    the own credentials/gates reach the child.

    Expected PASS on the frozen candidate.
    """
    root, _beta = fleet

    assert delivery._deliver_to_bot_chat({"id": "j", "name": "nightly"}, "the brief", "") is None

    env = quiet_bot_chat_lane["env"]
    assert env["HERMES_HOME"] == str(root)
    assert env["DISCORD_ALLOWED_USERS"] == "root-operator"
    assert env["ANTHROPIC_API_KEY"] == "sk-root"


def test_bot_chat_cross_profile_turn_is_isolated_from_launch_profile(fleet, quiet_bot_chat_lane):
    """Regression: a turn spawned for ANOTHER profile carries that profile's environment: its
    home, none of the launch profile's authorization gates, and its own credential rather than the
    gateway's.

    Expected FAIL on the frozen candidate: cron/scheduler_delivery.py:900 calls
    ``strip_launch_profile_env`` with no target, so with no active home override nothing is
    stripped and ``DISCORD_ALLOWED_USERS`` / ``ANTHROPIC_API_KEY=sk-root`` reach beta's turn.
    """
    _root, beta = fleet

    assert delivery._deliver_to_bot_chat({"id": "j", "name": "nightly"}, "the brief", "beta") is None

    env = quiet_bot_chat_lane["env"]
    # Destination is the discovered target home, never re-resolved.
    assert env["HERMES_HOME"] == str(beta)
    # Authorization gates decide who may talk to the agent; never inherited across profiles.
    assert "DISCORD_ALLOWED_USERS" not in env, (
        "launch profile's authorization gate reached another profile's turn (#113270)")
    # Credentials are the target profile's own, not the gateway's.
    assert env.get("ANTHROPIC_API_KEY") == "sk-beta", (
        f"expected beta's credential, child carries {env.get('ANTHROPIC_API_KEY')!r}")


def test_bot_chat_unbuildable_target_environment_is_refused_before_spawn(
    fleet, quiet_bot_chat_lane, monkeypatch,
):
    """Regression: when the target profile's child env cannot be built the lane must refuse with
    the string contract (``do not resend`` + the exception class) and spawn nothing.

    Expected FAIL on the frozen candidate: ``served_profile_child_env`` is never consulted
    (cron/scheduler_delivery.py:898-900), so the raise never happens, the turn is spawned with
    the launch environ, and the lane returns ``None``.
    """
    from tools.environments import local as local_env

    def _unbuildable(*args, **kwargs):
        raise PermissionError("home is 0700 for another user")

    monkeypatch.setattr(local_env, "served_profile_child_env", _unbuildable)

    result = delivery._deliver_to_bot_chat({"id": "j", "name": "nightly"}, "the brief", "beta")

    # Safety boundary first: no child may be built from an environment that could not be
    # constructed for the target.
    assert quiet_bot_chat_lane == {}, (
        f"a Bot Chat turn was spawned despite an unbuildable target env: {quiet_bot_chat_lane.get('argv')}")
    # Reporting contract: every failure is a string the fan-out can record; nothing raises past it.
    assert result is not None, "lane reported success for a turn it could not safely build"
    assert "do not resend" in result
    assert "PermissionError" in result


class _PendingFuture:
    """Future whose confirmation never arrives: ``result`` raises the documented wait timeout at
    once so the control does not sleep through production's 30s bound."""

    def result(self, timeout=None):
        raise concurrent.futures.TimeoutError()


class _TimingOutPool:
    """ThreadPoolExecutor whose ``submit`` ACCEPTS the work (recording what it was handed) but whose
    future never confirms. Models the slow-worker case: ownership of the coroutine has moved to
    the worker, which may still be driving it when the caller gives up waiting."""

    submitted: list = []

    def __init__(self, *args, **kwargs):
        pass

    def submit(self, fn, *args, **kwargs):
        type(self).submitted.append((fn, *args))
        return _PendingFuture()

    def shutdown(self, *args, **kwargs):
        pass


def test_standalone_send_submitted_coroutine_is_owned_by_worker_on_result_timeout_control(
    captured_send, running_loop_on_first_run, monkeypatch,
):
    """Positive control for the ownership half of Mechanism A: once ``pool.submit`` has ACCEPTED
    the fallback coroutine, the worker owns it. A ``result`` timeout is a slow confirmation, not a
    refusal, so the lane must report ``(None, <str>)`` and must NOT close the handed-over coroutine
    (closing a coroutine another thread may be driving raises inside that thread). The stub pool
    never starts it, so the honest state is ``CORO_CREATED``; the fixture releases it in teardown.

    Expected PASS on the frozen candidate and after the Mechanism A repair: the repair may only
    close a coroutine that ``submit`` refused.
    """
    _TimingOutPool.submitted.clear()
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _TimingOutPool)

    result, error = delivery._standalone_send(_lightweight_target(), "scheduled result", [])

    # The lane's contract: a timed-out confirmation is reported, never raised past other targets.
    assert result is None
    assert isinstance(error, str) and error
    assert len(captured_send) == 2, "fallback lane did not allocate its own send coroutine"
    direct_lane, fallback_lane = captured_send
    assert inspect.getcoroutinestate(direct_lane) == inspect.CORO_CLOSED
    # The coroutine the pool was handed is the retained fallback coroutine (causal ownership link).
    assert _TimingOutPool.submitted and fallback_lane in _TimingOutPool.submitted[-1], (
        "fallback coroutine was never handed to the pool")
    # Ownership moved with the submit: production must not close what the worker now owns.
    assert inspect.getcoroutinestate(fallback_lane) == inspect.CORO_CREATED, (
        "production closed or drove a coroutine it had already handed to the pool worker")


# ---------------------------------------------------------------------------------------------
# Mechanism C — Bot Chat CLI lane child import path outside the checkout (review P1)
# ---------------------------------------------------------------------------------------------
#
# ``served_profile_child_env`` scrubs the Hermes-owned PYTHONPATH and the child runs with the
# target home as its destination, outside the checkout. The parent's ``find_spec("hermes_cli")``
# answers for the PARENT: a child interpreter with no installed ``hermes_cli`` package (PM's
# bare store Python; a source-only interpreter that knew the checkout only through the parent's
# PYTHONPATH) cannot import ``hermes_cli.main`` from the target home. Contract: the delivered
# env carries EXACTLY the checkout root on PYTHONPATH (never the inherited value, never a
# site-packages dir: the child leases its own dependency generation through hermes_bootstrap,
# which hermes_cli.main imports first); argv keeps the frozen ``sys.executable -m
# hermes_cli.main`` shape. The probe below is a REAL child run with ``-S`` so the test
# interpreter's own site dir and ``.pth`` files cannot supply the import: only the env can.
#
# A round-2 draft here asserted a store-bound ``runtime_command`` argv (``-I -c`` bootstrap)
# with the store interpreter pinned to this process. It was never an accepted acceptance ID:
# it contradicted the frozen argv assertions in tests/cron/test_cron_bot_chat_delivery.py and
# tests/cron/test_bot_chat_cli_home.py, and it proved nothing about whether the child can
# import ``hermes_cli``. It is replaced by the behaviour-level probe, not kept as an obligation.

# Prints where the child would import ``hermes_cli`` from; exits 3 when it cannot be found.
_HERMES_CLI_ORIGIN_PROBE = (
    "import importlib.util, sys; spec = importlib.util.find_spec('hermes_cli'); "
    "sys.exit(3) if spec is None or not spec.origin else print(spec.origin)"
)


def _repo_root():
    from pathlib import Path
    return Path(delivery.__file__).resolve().parents[1]


def _probe_hermes_cli_import(env: dict, cwd) -> subprocess.CompletedProcess:
    """Real child under ``-S``: no site-packages, no ``.pth``; only *env* and *cwd* can supply
    ``hermes_cli``. Runs from *cwd* (the target home), as the delivery child's destination is."""
    import sys
    return subprocess.run(
        [sys.executable, "-S", "-c", _HERMES_CLI_ORIGIN_PROBE],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=120)


def test_bot_chat_served_profile_env_alone_cannot_import_hermes_cli_outside_checkout_control(fleet):
    """Negative control: the env ``served_profile_child_env`` builds for the target, with no
    overlay, does not let a ``-S`` child in the target home find ``hermes_cli``. Proves the probe
    is sensitive: a GREEN regression below is evidence of the delivered import path, not of the
    host's site installs.

    Expected PASS before and after the repair.
    """
    import os
    from tools.environments import local as local_env

    _root, beta = fleet
    env = local_env.served_profile_child_env(target_home=beta, inherit_credentials=True)
    assert str(_repo_root()) not in (env.get("PYTHONPATH") or "").split(os.pathsep), (
        "the scrub left the checkout on PYTHONPATH; this control cannot discriminate")

    probe = _probe_hermes_cli_import(env, beta)

    assert probe.returncode != 0, (
        f"probe found hermes_cli at {probe.stdout.strip()!r} with no delivered import path; "
        "the -S probe is not sensitive on this host")


def test_bot_chat_cross_profile_turn_can_import_hermes_cli_from_the_target_home(
    fleet, quiet_bot_chat_lane, monkeypatch, tmp_path,
):
    """Regression: the Bot Chat child built for ANOTHER profile carries exactly the checkout root
    on PYTHONPATH, so a child with no installed ``hermes_cli`` package still imports it from the
    target home. The inherited PYTHONPATH is replaced, not restored; no site-packages dir is
    handed over (no dependency generation captured). argv keeps its frozen shape and the
    target-profile env contract is unchanged.

    Expected FAIL on the frozen candidate: ``served_profile_child_env`` scrubs the Hermes-owned
    import path and nothing puts the checkout back, so the ``-S`` probe from the target home
    exits 3 (``hermes_cli`` not found).
    """
    import os
    import sys
    from pathlib import Path

    _root, beta = fleet
    repo_root = _repo_root()
    foreign = tmp_path / "foreign-pythonpath"
    foreign.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(foreign))

    assert delivery._deliver_to_bot_chat({"id": "j", "name": "nightly"}, "the brief", "beta") is None

    argv = quiet_bot_chat_lane["argv"]
    assert argv[:3] == [sys.executable, "-m", "hermes_cli.main"]
    assert argv[3:5] != ["-p", "default"], "a profiles/<name> home never gets -p default"
    env = quiet_bot_chat_lane["env"]
    # Exactly the checkout root: not the parent's PYTHONPATH, not a site-packages dir.
    assert env["PYTHONPATH"] == str(repo_root)
    assert str(foreign) not in env["PYTHONPATH"]
    assert "site-packages" not in env["PYTHONPATH"]
    assert os.pathsep not in env["PYTHONPATH"]
    # The target-profile environment contract is untouched by the overlay.
    assert env["HERMES_HOME"] == str(beta)
    assert env.get("ANTHROPIC_API_KEY") == "sk-beta"
    assert "DISCORD_ALLOWED_USERS" not in env

    probe = _probe_hermes_cli_import(env, beta)

    assert probe.returncode == 0, (
        f"hermes_cli not importable from the target home with the delivered env: "
        f"rc={probe.returncode} stderr={probe.stderr.strip()[-500:]!r}")
    origin = Path(probe.stdout.strip()).resolve()
    assert origin.is_relative_to(repo_root), f"hermes_cli resolved outside the checkout: {origin}"




@pytest.fixture
def pm_script_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a ``scripts/`` dir; PM install state for this checkout lives
    beneath it (``pm.environments.install_state_dir``), so nothing here touches a live root."""
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    return home


def _lease_managed_generation(generations, name: str, *, with_venv: bool):
    """A disposable lease-managed PM generation directory under the test's install state."""
    import sys
    from pathlib import Path

    generation = generations / name
    generation.mkdir(parents=True)
    (generation / ".lease-managed").touch()
    if with_venv:
        venv = generation / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to(sys.executable)
        (venv / "pyvenv.cfg").write_text(
            f"home = {Path(sys.base_prefix) / 'bin'}\ninclude-system-site-packages = false\n",
            encoding="utf-8")
    return generation


@pytest.mark.platforms("posix")
def test_pm_cron_script_leases_before_startup_pth(
    pm_script_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generation's startup hook must never execute before its child owns a lease."""
    import json
    import sys
    from pathlib import Path

    from cron import scheduler_script
    from hermes_cli.runtime_state import leases_held
    from pm.environments import install_state_dir, runtime_facts_path, site_packages

    # The canonical runner scrubs inherited variables, so set these in the test
    # as well: both the startup hook and activation run without host Git config.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    repo = Path(scheduler_script.__file__).resolve().parents[1]
    generation = _lease_managed_generation(
        install_state_dir(repo) / "environments", "startup", with_venv=True)
    deps = site_packages(generation / "venv")
    deps.mkdir(parents=True)
    runtime_facts_path(repo).write_text(json.dumps(
        {"packages": {"venv": {"environment": str(generation / "venv")}}}),
        encoding="utf-8")
    events = pm_script_home / "startup-events.jsonl"
    body = pm_script_home / "body-ran"
    (deps / "lease_startup_probe.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "before_activation = 'pm.environments' not in sys.modules\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "from hermes_cli.runtime_state import leases_held\n"
        f"held = leases_held(Path({str(generation)!r}))\n"
        f"with open({str(events)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'held': held, 'before_activation': before_activation, "
        "'git_global': os.environ.get('GIT_CONFIG_GLOBAL'), "
        "'git_system': os.environ.get('GIT_CONFIG_SYSTEM')}) + '\\n')\n"
        "if not held:\n"
        "    os.write(2, b'UNLEASED_PTH_BEFORE_ACTIVATION\\n')\n"
        "    os._exit(86)\n",
        encoding="utf-8")
    (deps / "startup.pth").write_text("import lease_startup_probe\n", encoding="utf-8")
    script = pm_script_home / "scripts" / "startup_probe.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(body)!r}).touch()\n"
        "print('leased startup completed')\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli._launchers.resolve_store_python", lambda repo: Path(sys.executable))

    success, output = scheduler_script._run_job_script(script.name)
    observations = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert success is True, (
        f"{output}; startup={observations}; script_body_ran={body.exists()}")
    assert observations == [{
        "held": True, "before_activation": False,
        "git_global": "/dev/null", "git_system": "/dev/null",
    }]
    assert body.is_file()
    assert output == "leased startup completed"
    assert leases_held(generation) is False


@pytest.mark.platforms("posix")
def test_pm_cron_script_starts_after_admitted_generation_is_collected(
    pm_script_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executable must survive a selection change between admission and spawn."""
    import json
    import os
    import sys
    from pathlib import Path

    from cron import scheduler_script
    from hermes_cli.runtime_state import collect_generations
    from pm.environments import install_state_dir, runtime_facts_path, site_packages

    repo = Path(scheduler_script.__file__).resolve().parents[1]
    generations = install_state_dir(repo) / "environments"
    old = _lease_managed_generation(generations, "admitted", with_venv=True)
    current = _lease_managed_generation(generations, "current", with_venv=True)
    for generation in (old, current):
        deps = site_packages(generation / "venv")
        deps.mkdir(parents=True)
        (deps / "selection_probe.py").write_text(
            f"VALUE = {generation.name!r}\n", encoding="utf-8")
    facts = runtime_facts_path(repo)
    facts.write_text(json.dumps(
        {"packages": {"venv": {"environment": str(old / "venv")}}}), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli._launchers.resolve_store_python", lambda repo: Path(sys.executable))
    script = pm_script_home / "scripts" / "selection_probe_script.py"
    script.write_text(
        "import selection_probe\nprint(selection_probe.VALUE)\n", encoding="utf-8")
    argv, error = scheduler_script._posix_managed_store_argv(script)
    assert error is None
    assert argv is not None

    facts.write_text(json.dumps(
        {"packages": {"venv": {"environment": str(current / "venv")}}}), encoding="utf-8")
    assert old in collect_generations(repo, min_age_seconds=0)
    assert not old.exists()
    result = subprocess.run(
        argv, env=dict(os.environ), cwd=script.parent,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "current"


@pytest.mark.platforms("posix")
def test_pm_cron_script_child_leases_its_dependency_generation_against_collection(
    pm_script_home, monkeypatch,
):
    """Regression (P2): a ``.py`` cron script on a POSIX managed-store install must hold a lease
    on the dependency generation it runs on, for exactly as long as it runs. A lease is a kernel
    lock held by the importing process (``hermes_cli.runtime_state``); the gateway's lease on ITS
    generation protects nothing the script loads, and ``collect_generations`` removes any
    unselected lease-managed generation with no live lock.

    Disposable install state under this test's HERMES_HOME: generation A (lease-managed, stale,
    unselected), B (committed in the facts record, carries ``probe_pkg``) and C (committed by the
    script itself mid-run, as an installer would). The script then runs the REAL collector in a
    separate process, as PM maintenance does, and reports what it observed. The selection is READ
    from the disposable facts record by parent and child, never patched; only "this process is the
    store Python" is pinned, as the frozen managed-store tests pin it.

    Positive control inside the same collector call: A, unleased, IS removed. Boundary under
    test: B, imported by the running script, is leased and survives a later selection of C; the
    lease ends with the process.

    Expected FAIL on the frozen candidate: cron/scheduler_script.py:311/:322 launch B's
    interpreter with ``site.addsitedir`` and no lease, so the collector reports B unheld and
    removes it under the running script.
    """
    import json
    import sys
    import textwrap
    from pathlib import Path

    from cron import scheduler_script
    from hermes_cli.runtime_state import leases_held
    from pm.environments import install_state_dir, runtime_facts_path, site_packages

    repo_root = Path(scheduler_script.__file__).resolve().parents[1]
    state = install_state_dir(repo_root)
    assert state.resolve().is_relative_to(pm_script_home.resolve()), state
    generations = state / "environments"
    gen_a = _lease_managed_generation(generations, "gen-a", with_venv=False)
    gen_b = _lease_managed_generation(generations, "gen-b", with_venv=True)
    gen_c = _lease_managed_generation(generations, "gen-c", with_venv=True)
    deps = site_packages(gen_b / "venv")
    deps.mkdir(parents=True)
    (deps / "probe_pkg.py").write_text("VALUE = 'generation-b'\n", encoding="utf-8")
    facts = runtime_facts_path(repo_root)
    facts.write_text(json.dumps(
        {"packages": {"venv": {"environment": str(gen_b / "venv")}}}), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli._launchers.resolve_store_python", lambda repo: Path(sys.executable))

    script = pm_script_home / "scripts" / "lease_probe.py"
    script.write_text(textwrap.dedent(f"""\
        import json, os, subprocess, sys
        import probe_pkg
        # An installer commits generation C while this script is still running on B.
        with open({str(facts)!r}, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {{"packages": {{"venv": {{"environment": {str(gen_c / "venv")!r}}}}}}}))
        # The collector is its own process, as PM maintenance is; it must find B leased.
        collector = subprocess.run([sys.executable, "-c", (
            "import json, sys; sys.path.insert(0, {str(repo_root)!r}); "
            "from pathlib import Path; "
            "from hermes_cli.runtime_state import collect_generations, leases_held; "
            "held = leases_held(Path({str(gen_b)!r})); "
            "removed = collect_generations(Path({str(repo_root)!r}), min_age_seconds=0); "
            "print(json.dumps({{'held': held, 'removed': sorted(p.name for p in removed)}}))")],
            capture_output=True, text=True)
        print(json.dumps({{
            "value": probe_pkg.VALUE,
            "pkg_in_b": os.path.abspath(probe_pkg.__file__).startswith({str(gen_b.resolve())!r}),
            "path0": sys.path[0],
            "pythonpath": os.environ.get("PYTHONPATH", ""),
            "collector_rc": collector.returncode,
            "collector_out": collector.stdout.strip(),
            "collector_err": collector.stderr.strip()[-800:],
        }}))
        """), encoding="utf-8")

    success, output = scheduler_script._run_job_script("lease_probe.py")
    assert success is True, output
    report = json.loads(output)
    assert report["value"] == "generation-b"
    assert report["pkg_in_b"] is True, "script did not import from the committed generation"
    assert Path(report["path0"]).resolve() == script.parent.resolve()
    assert report["pythonpath"] == ""
    assert report["collector_rc"] == 0, report["collector_err"]
    collector = json.loads(report["collector_out"])
    # Boundary: the running script held a lease on B, so B survived a later selection of C.
    assert collector["held"] is True, "running cron script holds no lease on its generation"
    # Positive control: the same collector call removed the unleased stale generation only.
    assert collector["removed"] == ["gen-a"], collector
    assert not gen_a.exists()
    assert (gen_b / "venv").is_dir()
    # The lease is the child's: nothing pins B once the script has exited.
    assert leases_held(gen_b) is False
