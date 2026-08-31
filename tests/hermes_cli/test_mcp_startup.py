"""Regression tests for bounded/lazy CLI MCP startup."""

from __future__ import annotations

from argparse import Namespace
from contextlib import nullcontext
import sys
import threading
import time
import types

import pytest

import cli as cli_mod
from hermes_cli import main as main_mod
from hermes_cli import mcp_startup


@pytest.fixture(autouse=True)
def _reset_mcp_startup_state():
    saved_started = mcp_startup._mcp_discovery_started
    saved_thread = mcp_startup._mcp_discovery_thread
    try:
        mcp_startup._mcp_discovery_started = False
        mcp_startup._mcp_discovery_thread = None
        yield
    finally:
        thread = mcp_startup._mcp_discovery_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        mcp_startup._mcp_discovery_started = saved_started
        mcp_startup._mcp_discovery_thread = saved_thread


def _agent_args(**overrides) -> Namespace:
    base = {
        "accept_hooks": False,
        "command": "chat",
        "cron_command": None,
        "gateway_command": None,
        "mcp_action": None,
        "tui": False,
    }
    base.update(overrides)
    return Namespace(**base)


def _spy_startup_surfaces(monkeypatch):
    """Stub every discovery/registration surface _prepare_agent_startup can
    touch and return the shared call log."""
    # Make sure the real oneshot module (and its import chain through
    # hermes_cli.config) is cached before hermes_cli.config is stubbed below —
    # the oneshot precheck import inside _prepare_agent_startup must not be
    # poisoned by the partial stub.
    import hermes_cli.oneshot  # noqa: F401

    calls: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.plugins",
        types.SimpleNamespace(
            discover_plugins=lambda: calls.append("plugins"),
            start_background_plugin_discovery=lambda: calls.append("plugins"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_tool",
        types.SimpleNamespace(discover_mcp_tools=lambda: calls.append("mcp")),
    )
    monkeypatch.setattr(
        mcp_startup,
        "start_background_mcp_discovery",
        lambda **_k: calls.append("mcp"),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(
            read_raw_config=lambda: {},
            load_config=lambda: {},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "agent.shell_hooks",
        types.SimpleNamespace(
            register_from_config=lambda *_a, **_k: calls.append("hooks")
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "agent.outbound_webhooks",
        types.SimpleNamespace(
            register_from_config=lambda *_a, **_k: calls.append("webhooks")
        ),
    )
    return calls


def _oneshot_args(**overrides) -> Namespace:
    # Top-level `hermes -z ...` parses with command=None and the oneshot
    # prompt on args.oneshot — the exact dispatch shape main() hands to
    # _prepare_agent_startup before _run_and_exit_oneshot.
    base = {
        "accept_hooks": False,
        "command": None,
        "cron_command": None,
        "gateway_command": None,
        "mcp_action": None,
        "tui": False,
        "yolo": False,
        "safe_mode": False,
        "oneshot": "prompt",
        "toolsets": None,
    }
    base.update(overrides)
    return Namespace(**base)


def test_prepare_agent_startup_skips_all_discovery_for_oneshot_none(monkeypatch):
    calls = _spy_startup_surfaces(monkeypatch)
    main_mod._prepare_agent_startup(_oneshot_args(toolsets="none"))
    assert calls == [], (
        "hermes -z --toolsets none must not start plugin/MCP/hook discovery: "
        f"got {calls}"
    )


def test_prepare_agent_startup_skips_all_discovery_for_oneshot_precheck_error(
    monkeypatch,
):
    calls = _spy_startup_surfaces(monkeypatch)
    for toolsets in ("none,web", "NONE", "none,none", "none,", "all,web"):
        main_mod._prepare_agent_startup(_oneshot_args(toolsets=toolsets))
    assert calls == [], (
        "a -z toolset validation error must not start discovery; run_oneshot "
        f"emits the exit-2 message itself: got {calls}"
    )


def test_prepare_agent_startup_fails_closed_when_precheck_raises(monkeypatch):
    import hermes_cli.oneshot as oneshot_mod

    calls = _spy_startup_surfaces(monkeypatch)

    def _boom(_toolsets):
        raise RuntimeError("precheck boom")

    monkeypatch.setattr(oneshot_mod, "_precheck_explicit_toolsets", _boom)
    for toolsets in ("none", "terminal"):
        main_mod._prepare_agent_startup(_oneshot_args(toolsets=toolsets))
    assert calls == [], (
        "a raising precheck for an explicit --toolsets must fail closed: "
        "run_oneshot cannot be shown safe to proceed, so no plugin/MCP/hook/"
        f"webhook activation may start: got {calls}"
    )


def test_prepare_agent_startup_precheck_raise_with_omitted_toolsets_keeps_startup(
    monkeypatch,
):
    import hermes_cli.oneshot as oneshot_mod

    calls = _spy_startup_surfaces(monkeypatch)

    def _boom(_toolsets):
        raise RuntimeError("precheck boom")

    monkeypatch.setattr(oneshot_mod, "_precheck_explicit_toolsets", _boom)
    main_mod._prepare_agent_startup(_oneshot_args(toolsets=None))
    # Omitted --toolsets can never resolve to the sentinel, so an internal
    # precheck failure must not degrade ordinary -z startup.
    assert "plugins" in calls
    assert "hooks" in calls


def test_prepare_agent_startup_precheck_base_exception_zero_activation(monkeypatch):
    import hermes_cli.oneshot as oneshot_mod

    calls = _spy_startup_surfaces(monkeypatch)

    class _PrecheckBase(BaseException):
        pass

    def _boom(_toolsets):
        raise _PrecheckBase("precheck base")

    monkeypatch.setattr(oneshot_mod, "_precheck_explicit_toolsets", _boom)
    with pytest.raises(_PrecheckBase):
        main_mod._prepare_agent_startup(_oneshot_args(toolsets="none"))
    # Control-flow BaseExceptions abort startup outright — still zero
    # plugin/MCP/hook/webhook activation.
    assert calls == []


def test_oneshot_precheck_helper_fails_closed_only_for_explicit_toolsets(monkeypatch):
    import hermes_cli.oneshot as oneshot_mod

    def _boom(_toolsets):
        raise RuntimeError("precheck boom")

    monkeypatch.setattr(oneshot_mod, "_precheck_explicit_toolsets", _boom)
    assert (
        main_mod._oneshot_explicit_no_tools_precheck(_oneshot_args(toolsets="none"))
        is True
    )
    assert (
        main_mod._oneshot_explicit_no_tools_precheck(_oneshot_args(toolsets="terminal"))
        is True
    )
    assert (
        main_mod._oneshot_explicit_no_tools_precheck(_oneshot_args(toolsets=None))
        is False
    )


def test_prepare_agent_startup_still_runs_discovery_for_valid_oneshot_toolsets(
    monkeypatch,
):
    calls = _spy_startup_surfaces(monkeypatch)
    main_mod._prepare_agent_startup(_oneshot_args(toolsets="terminal"))
    assert "plugins" in calls
    assert "hooks" in calls


def test_prepare_agent_startup_still_runs_discovery_for_omitted_oneshot_toolsets(
    monkeypatch,
):
    calls = _spy_startup_surfaces(monkeypatch)
    main_mod._prepare_agent_startup(_oneshot_args(toolsets=None))
    assert "plugins" in calls
    assert "hooks" in calls


def test_prepare_agent_startup_oneshot_none_still_applies_safe_mode(monkeypatch):
    import os

    calls = _spy_startup_surfaces(monkeypatch)
    # setenv (not delenv) so monkeypatch restores the pre-test state even
    # though production overwrites the values during the call.
    for var in ("HERMES_SAFE_MODE", "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES"):
        monkeypatch.setenv(var, "0")
    monkeypatch.setenv("HERMES_YOLO_MODE", "0")
    main_mod._prepare_agent_startup(
        _oneshot_args(toolsets="none", safe_mode=True, yolo=True)
    )
    # Discovery skipped, but the env chokepoints still apply.
    assert calls == []
    assert os.environ["HERMES_SAFE_MODE"] == "1"
    assert os.environ["HERMES_IGNORE_USER_CONFIG"] == "1"
    assert os.environ["HERMES_IGNORE_RULES"] == "1"
    assert os.environ["HERMES_YOLO_MODE"] == "1"


def test_prepare_agent_startup_backgrounds_blocking_mcp_for_chat(monkeypatch):
    stop = threading.Event()
    calls = {"mcp": 0}

    def _blocking_discover():
        calls["mcp"] += 1
        stop.wait()

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(
            read_raw_config=lambda: {"mcp_servers": {"demo": {"transport": "stdio"}}},
            load_config=lambda: {},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "agent.shell_hooks",
        types.SimpleNamespace(register_from_config=lambda *_a, **_k: None),
    )
    # Stub mcp_oauth so the background thread doesn't pay the real (cold,
    # ~0.75s) ``tools.mcp_oauth`` import before calling discovery. This test
    # asserts the *backgrounding contract* (main thread returns fast, discovery
    # runs off-thread), not OAuth suppression — the unrelated import latency
    # would otherwise blow the polling deadline on a loaded CI runner.
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_oauth",
        types.SimpleNamespace(suppress_interactive_oauth=lambda: nullcontext()),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_tool",
        types.SimpleNamespace(discover_mcp_tools=_blocking_discover),
    )

    try:
        start = time.monotonic()
        main_mod._prepare_agent_startup(_agent_args())
        elapsed = time.monotonic() - start
        assert elapsed < 0.2
        deadline = time.monotonic() + 3.0
        while calls["mcp"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls["mcp"] == 1
        assert mcp_startup._mcp_discovery_thread is not None
        assert mcp_startup._mcp_discovery_thread.is_alive()
    finally:
        stop.set()


def test_background_mcp_discovery_suppresses_interactive_oauth(monkeypatch):
    state = {"active": False, "during_discover": None}

    class SuppressInteractiveOAuth:
        def __enter__(self):
            state["active"] = True

        def __exit__(self, *_exc):
            state["active"] = False

    def _discover():
        state["during_discover"] = state["active"]

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(
            read_raw_config=lambda: {"mcp_servers": {"demo": {"url": "https://mcp.example.test/mcp"}}},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_oauth",
        types.SimpleNamespace(
            suppress_interactive_oauth=lambda: SuppressInteractiveOAuth(),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_tool",
        types.SimpleNamespace(discover_mcp_tools=_discover),
    )

    mcp_startup.start_background_mcp_discovery(
        logger=types.SimpleNamespace(debug=lambda *_a, **_k: None),
        thread_name="test-mcp-discovery",
    )
    assert mcp_startup._mcp_discovery_thread is not None
    mcp_startup._mcp_discovery_thread.join(timeout=1.0)

    assert state["during_discover"] is True
    assert state["active"] is False


def test_portable_only_mcp_configuration_opens_startup_gate(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(read_raw_config=lambda: {}),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.agent_plugins",
        types.SimpleNamespace(
            has_enabled_agent_plugin_mcp=lambda _config: True,
        ),
    )

    assert mcp_startup._has_configured_mcp_servers() is True








def _retry_logger():
    return types.SimpleNamespace(
        debug=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
    )


def _install_retry_stubs(monkeypatch, *, connected: bool, calls: dict):
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(
            read_raw_config=lambda: {"mcp_servers": {"demo": {"transport": "stdio"}}},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_oauth",
        types.SimpleNamespace(suppress_interactive_oauth=lambda: nullcontext()),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_tool",
        types.SimpleNamespace(
            discover_mcp_tools=lambda: calls.__setitem__("mcp", calls["mcp"] + 1),
            get_mcp_status=lambda: [{"connected": connected}],
        ),
    )




# =========================================================================
# Cold-start raw-argv explicit-no-tools guard (OR3-OR6)
# =========================================================================
#
# The real console script imports hermes_cli.main — which loads dotenv, reads
# raw config, and initializes logging at module import — before main() or
# run_oneshot ever executes. For a top-level -z launch whose --toolsets value
# is decidable from raw argv alone (exact `none`, none-like case/duplicate/
# blank/mixture errors, all-mixtures), HERMES_ONESHOT_EXPLICIT_NO_TOOLS must
# already be "1" for every one of those startup reads, and the import-time
# lease must restore the exact prior value at import completion. main() must
# acquire its own lease from the current argv and restore it when it returns
# or raises (a real oneshot hard-exits via os._exit, keeping the launch lease
# until process end). Configuration stays readable under the guard — but
# plugin discovery, MCP discovery/startup, shell-hook registration,
# outbound-webhook registration, and configured-toolset selection must never
# run (OR6).

import json
import os
import subprocess

from pathlib import Path

_GUARD_ENV = "HERMES_ONESHOT_EXPLICIT_NO_TOOLS"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Capability-bearing configuration that must stay INERT (parsed as data only)
# during an exact-none or reserved-token-error oneshot launch.
_COLD_START_CONFIG = """\
model:
  provider: openai
  default: gpt-4o-mini
plugins:
  enabled: true
mcp_servers:
  cold-start-probe:
    enabled: true
    transport: stdio
    command: /bin/false
shell_hooks:
  - name: cold-start-hook
    command: /bin/true
    events: [session_start]
outbound_webhooks:
  - url: https://webhook.invalid/cold-start
    events: [turn_complete]
"""

_COLD_START_DRIVER = r'''
import json
import os
import sys
import types

MODE = __MODE__                # "import" | "console" | "runpy"
MAIN_ACTION = __MAIN_ACTION__  # "run" | "impl-return" | "impl-raise"
STUB_RUN_ONESHOT = __STUB__
ARGV = __ARGV__

sys.argv = ["hermes"] + ARGV

_REC = {"on": False}
_GUARD = "HERMES_ONESHOT_EXPLICIT_NO_TOOLS"


def _emit(payload):
    sys.stdout.write("EVT " + json.dumps(payload) + "\n")
    sys.stdout.flush()


def _obs(name):
    if _REC["on"]:
        _emit({"kind": "obs", "name": name, "guard": os.environ.get(_GUARD)})


def _cap(name):
    if _REC["on"]:
        _emit({"kind": "capability", "name": name})


# Instrument the guard-sensitive startup surfaces on their real (light)
# modules BEFORE hermes_cli.main is imported; recording starts only at the
# cold import so driver plumbing is not observed.
import hermes_cli.config as _config_mod

_real_read_raw = _config_mod.read_raw_config


def _read_raw(*a, **k):
    _obs("read_raw_config")
    return _real_read_raw(*a, **k)


_config_mod.read_raw_config = _read_raw

import hermes_cli.env_loader as _env_mod

_real_dotenv = _env_mod.load_hermes_dotenv


def _dotenv(*a, **k):
    _obs("load_hermes_dotenv")
    return _real_dotenv(*a, **k)


_env_mod.load_hermes_dotenv = _dotenv

import hermes_logging as _log_mod

_real_setup_logging = _log_mod.setup_logging


def _setup_logging(*a, **k):
    _obs("setup_logging")
    return _real_setup_logging(*a, **k)


_log_mod.setup_logging = _setup_logging


def _count_capability(mod, name):
    def _fn(*a, **k):
        _cap(mod.__name__ + "." + name)

    setattr(mod, name, _fn)


import hermes_cli.plugins as _plugins_mod

for _name in ("discover_plugins", "start_background_plugin_discovery"):
    if hasattr(_plugins_mod, _name):
        _count_capability(_plugins_mod, _name)

import hermes_cli.mcp_startup as _mcp_startup_mod

for _name in (
    "start_background_mcp_discovery",
    "ensure_mcp_discovery_before_agent_build",
    "wait_for_mcp_discovery",
):
    if hasattr(_mcp_startup_mod, _name):
        _count_capability(_mcp_startup_mod, _name)

import hermes_cli.tools_config as _tools_config_mod


def _get_platform_tools(*a, **k):
    _cap("hermes_cli.tools_config._get_platform_tools")
    return set()


_tools_config_mod._get_platform_tools = _get_platform_tools


def _stub_module(name, counted):
    mod = types.ModuleType(name)

    def _module_getattr(attr, _name=name, _counted=counted):
        if attr.startswith("__"):
            raise AttributeError(attr)
        if attr in _counted:
            return lambda *a, **k: _cap(_name + "." + attr)
        return lambda *a, **k: None

    mod.__getattr__ = _module_getattr
    sys.modules[name] = mod


_stub_module("tools.mcp_tool", {"discover_mcp_tools", "refresh_agent_mcp_tools"})
_stub_module("agent.shell_hooks", {"register_from_config"})
_stub_module("agent.outbound_webhooks", {"register_from_config"})

if STUB_RUN_ONESHOT:
    import hermes_cli.oneshot as _oneshot_mod

    def _stub_run_oneshot(prompt, **kwargs):
        _emit(
            {
                "kind": "run_oneshot",
                "guard": os.environ.get(_GUARD),
                "toolsets": kwargs.get("toolsets"),
            }
        )
        return 0

    _oneshot_mod.run_oneshot = _stub_run_oneshot

_emit({"kind": "pre-import", "guard": os.environ.get(_GUARD)})
_REC["on"] = True

if MODE == "runpy":
    import runpy

    runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)
    _emit({"kind": "runpy-returned", "guard": os.environ.get(_GUARD)})
else:
    import hermes_cli.main as _main_mod

    _emit({"kind": "import-done", "guard": os.environ.get(_GUARD)})
    if MODE == "console":
        if MAIN_ACTION == "impl-return":

            def _impl():
                _emit({"kind": "main-impl", "guard": os.environ.get(_GUARD)})

            _main_mod._main_impl = _impl
            _main_mod.main()
            _emit({"kind": "main-returned", "guard": os.environ.get(_GUARD)})
        elif MAIN_ACTION == "impl-raise":

            class _ProbeBase(BaseException):
                pass

            def _impl_raise():
                _emit({"kind": "main-impl", "guard": os.environ.get(_GUARD)})
                raise _ProbeBase("probe")

            _main_mod._main_impl = _impl_raise
            try:
                _main_mod.main()
            except _ProbeBase:
                _emit({"kind": "main-raised", "guard": os.environ.get(_GUARD)})
        else:
            _main_mod.main()
            _emit(
                {
                    "kind": "main-returned-unexpectedly",
                    "guard": os.environ.get(_GUARD),
                }
            )
'''


def _run_cold_start(
    tmp_path,
    argv,
    *,
    mode="console",
    main_action="run",
    stub_run_oneshot=False,
    prior_guard=None,
    extra_env=None,
    timeout=300,
):
    home = tmp_path / "hermes-home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(_COLD_START_CONFIG, encoding="utf-8")
    program = (
        _COLD_START_DRIVER.replace("__MODE__", repr(mode))
        .replace("__MAIN_ACTION__", repr(main_action))
        .replace("__STUB__", repr(bool(stub_run_oneshot)))
        .replace("__ARGV__", repr(list(argv)))
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HOME"] = str(tmp_path)
    for name in (
        _GUARD_ENV,
        "TERMUX_VERSION",
        "HERMES_DISABLE_FAST_CHAT_LAUNCH",
        "HERMES_TUI",
        "HERMES_YOLO_MODE",
        "HERMES_ACCEPT_HOOKS",
        "HERMES_INFERENCE_MODEL",
        "HERMES_INFERENCE_PROVIDER",
        "HERMES_SAFE_MODE",
    ):
        env.pop(name, None)
    if prior_guard is not None:
        env[_GUARD_ENV] = prior_guard
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", program],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    events = [
        json.loads(line[4:])
        for line in proc.stdout.splitlines()
        if line.startswith("EVT ")
    ]
    return proc, events


def _obs_events(events):
    return [e for e in events if e["kind"] == "obs"]


def _capability_events(events):
    return [e for e in events if e["kind"] == "capability"]


def _single(events, kind):
    matches = [e for e in events if e["kind"] == kind]
    assert len(matches) == 1, (kind, events)
    return matches[0]


class TestColdImportGuardLease:
    """OR3 + OR5 + OR6: cold module import under console-script argv."""

    def test_exact_none_guards_every_startup_read(self, tmp_path):
        proc, events = _run_cold_start(
            tmp_path, ["-z", "hi", "-t", "none"], mode="import"
        )
        assert proc.returncode == 0, proc.stderr
        obs = _obs_events(events)
        names = {e["name"] for e in obs}
        # The MCP-bearing fixture config exists, so the import really reads
        # it (OR6: reads are allowed, activation is not).
        assert {"load_hermes_dotenv", "read_raw_config", "setup_logging"} <= names, (
            events,
            proc.stderr,
        )
        unguarded = [e for e in obs if e["guard"] != "1"]
        assert unguarded == [], f"startup reads without guard: {unguarded}"
        assert _capability_events(events) == []
        # Import lease restored at successful module-import completion.
        assert _single(events, "import-done")["guard"] is None

    def test_import_lease_restores_arbitrary_prior_value(self, tmp_path):
        proc, events = _run_cold_start(
            tmp_path,
            ["-z", "hi", "-t", "none"],
            mode="import",
            prior_guard="prior-value",
        )
        assert proc.returncode == 0, proc.stderr
        assert all(e["guard"] == "1" for e in _obs_events(events)), events
        assert _single(events, "import-done")["guard"] == "prior-value"

    def test_main_lease_restores_on_return(self, tmp_path):
        proc, events = _run_cold_start(
            tmp_path,
            ["-z", "hi", "-t", "none"],
            mode="console",
            main_action="impl-return",
        )
        assert proc.returncode == 0, proc.stderr
        assert _single(events, "main-impl")["guard"] == "1"
        assert _single(events, "main-returned")["guard"] is None

    def test_main_lease_restores_prior_value_on_base_exception(self, tmp_path):
        proc, events = _run_cold_start(
            tmp_path,
            ["-z", "hi", "-t", "none"],
            mode="console",
            main_action="impl-raise",
            prior_guard="prior-value",
        )
        assert proc.returncode == 0, proc.stderr
        assert _single(events, "main-impl")["guard"] == "1"
        assert _single(events, "main-raised")["guard"] == "prior-value"


class TestColdMainDispatchGuard:
    """OR4: none-like/all-mixed and exact-none launches across the full,
    fast-chat, Termux fast-CLI, and python -m dispatch paths must keep every
    startup read guarded and every capability counter at zero."""

    _INVALID_VARIANTS = ["NONE", "none,none", "none, ,", "none,", "none,web", "all,web"]

    @pytest.mark.parametrize("variant", _INVALID_VARIANTS)
    def test_invalid_variant_full_dispatch_exits_2_with_zero_activation(
        self, tmp_path, variant
    ):
        proc, events = _run_cold_start(
            tmp_path,
            ["-z", "hi", "-t", variant],
            mode="console",
            extra_env={"HERMES_DISABLE_FAST_CHAT_LAUNCH": "1"},
        )
        assert proc.returncode == 2, (proc.returncode, proc.stderr, events)
        unguarded = [e for e in _obs_events(events) if e["guard"] != "1"]
        assert unguarded == [], f"startup reads without guard: {unguarded}"
        assert _capability_events(events) == []

    @pytest.mark.parametrize(
        "launch",
        ["full", "fast", "termux", "python-m"],
    )
    def test_exact_none_dispatch_paths_guard_and_zero_activation(
        self, tmp_path, launch
    ):
        extra_env = {}
        mode = "console"
        if launch == "full":
            extra_env["HERMES_DISABLE_FAST_CHAT_LAUNCH"] = "1"
        elif launch == "termux":
            extra_env["TERMUX_VERSION"] = "0.118"
        elif launch == "python-m":
            mode = "runpy"
        proc, events = _run_cold_start(
            tmp_path,
            ["-z", "hi", "-t", "none"],
            mode=mode,
            stub_run_oneshot=True,
            extra_env=extra_env,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr, events)
        dispatched = _single(events, "run_oneshot")
        assert dispatched["guard"] == "1"
        assert dispatched["toolsets"] == "none"
        unguarded = [e for e in _obs_events(events) if e["guard"] != "1"]
        assert unguarded == [], f"startup reads without guard: {unguarded}"
        assert _capability_events(events) == []
        # OR6: the capability-bearing config was actually read — inertly.
        assert any(
            e["name"] == "read_raw_config" for e in _obs_events(events)
        ), events

    @pytest.mark.parametrize("launch", ["fast", "termux"])
    def test_invalid_variant_fast_paths_exit_2_with_zero_activation(
        self, tmp_path, launch
    ):
        extra_env = {}
        if launch == "termux":
            extra_env["TERMUX_VERSION"] = "0.118"
        proc, events = _run_cold_start(
            tmp_path,
            ["-z", "hi", "-t", "NONE"],
            mode="console",
            extra_env=extra_env,
        )
        assert proc.returncode == 2, (proc.returncode, proc.stderr, events)
        unguarded = [e for e in _obs_events(events) if e["guard"] != "1"]
        assert unguarded == [], f"startup reads without guard: {unguarded}"
        assert _capability_events(events) == []

    @pytest.mark.parametrize(
        "launch",
        ["full", "fast", "termux", "python-m"],
    )
    def test_separated_abbreviation_none_dispatch_zero_activation(
        self, tmp_path, launch
    ):
        # argparse allow_abbrev binds `--o hi --to none` exactly like
        # `--oneshot hi --toolsets none`; the separated-abbreviation launch
        # must therefore stay guarded AND must never run plugin discovery
        # before dispatch (SEC-1: _first_positional_argv misread `hi` as a
        # positional and forced the eager plugin-discovery path).
        extra_env = {}
        mode = "console"
        if launch == "full":
            extra_env["HERMES_DISABLE_FAST_CHAT_LAUNCH"] = "1"
        elif launch == "termux":
            extra_env["TERMUX_VERSION"] = "0.118"
        elif launch == "python-m":
            mode = "runpy"
        proc, events = _run_cold_start(
            tmp_path,
            ["--o", "hi", "--to", "none"],
            mode=mode,
            stub_run_oneshot=True,
            extra_env=extra_env,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr, events)
        dispatched = _single(events, "run_oneshot")
        assert dispatched["guard"] == "1"
        assert dispatched["toolsets"] == "none"
        unguarded = [e for e in _obs_events(events) if e["guard"] != "1"]
        assert unguarded == [], f"startup reads without guard: {unguarded}"
        assert _capability_events(events) == []

    @pytest.mark.parametrize("launch", ["full", "fast", "termux", "python-m"])
    def test_top_level_oneshot_chat_local_none_is_guarded(
        self, tmp_path, launch
    ):
        extra_env = {}
        mode = "console"
        if launch == "full":
            extra_env["HERMES_DISABLE_FAST_CHAT_LAUNCH"] = "1"
        elif launch == "termux":
            extra_env["TERMUX_VERSION"] = "0.118"
        elif launch == "python-m":
            mode = "runpy"
        proc, events = _run_cold_start(
            tmp_path,
            ["-z", "prompt", "chat", "-t", "none"],
            mode=mode,
            stub_run_oneshot=True,
            extra_env=extra_env,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr, events)
        dispatched = _single(events, "run_oneshot")
        assert dispatched["guard"] == "1"
        assert dispatched["toolsets"] == "none"
        assert [e for e in _obs_events(events) if e["guard"] != "1"] == []
        assert _capability_events(events) == []

    @pytest.mark.parametrize(
        "launch",
        ["full", "fast", "termux", "python-m"],
    )
    def test_ambiguous_long_option_rejected_argv_never_claims_guard(
        self, tmp_path, launch
    ):
        # `--t` is ambiguous (--toolsets/--tui): argparse rejects this argv
        # with a usage error on every dispatch path, so no startup phase —
        # import lease included — may ever observe the guard as established.
        extra_env = {}
        mode = "console"
        if launch == "full":
            extra_env["HERMES_DISABLE_FAST_CHAT_LAUNCH"] = "1"
        elif launch == "termux":
            extra_env["TERMUX_VERSION"] = "0.118"
        elif launch == "python-m":
            mode = "runpy"
        proc, events = _run_cold_start(
            tmp_path,
            ["--t=x", "-z", "hi", "--toolsets=none"],
            mode=mode,
            extra_env=extra_env,
        )
        assert proc.returncode == 2, (proc.returncode, proc.stderr, events)
        claimed = [e for e in events if e.get("guard") == "1"]
        assert claimed == [], (
            f"guard claimed for argparse-rejected argv: {claimed}"
        )


class TestRawArgvPreflightClassifier:
    """The stdlib-only raw argv classifier: only the top-level value-taking
    -z/--oneshot surface with an effective -t/--toolsets value that is
    decidable without capability discovery establishes the guard. Prompt
    text is never inspected; argparse's last-value-wins and the ``--``
    terminator are honored; the chat subcommand's boolean --oneshot is a
    different interface. Long options follow argparse allow_abbrev
    semantics: a unique prefix of one top-level long option resolves to
    it, while ambiguous or unknown prefixes never establish the guard
    (argparse rejects those argvs before oneshot can run)."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["-z", "hi", "-t", "none"],
            ["-z", "hi", "--toolsets", "none"],
            ["-z", "hi", "--toolsets=none"],
            ["-tnone", "-z", "hi"],
            ["-z", "hi", "-t", " none "],
            ["-t", "web", "-z", "hi", "-t", "none"],
            ["-z", "hi", "-t", "NONE"],
            ["-t", "none,none", "-z", "hi"],
            ["-z", "hi", "-t", "none,web"],
            ["-z", "hi", "-t", "none,"],
            ["-z", "hi", "-t", "none, ,"],
            ["-z", "hi", "-t", "all,web"],
            ["-z", "hi", "-t", "web,*"],
            ["-z", "hi", "-t", "all,all"],
            ["--oneshot", "hi", "-t", "none"],
            ["--oneshot=hi", "-t", "none"],
            ["--safe-mode", "-z", "hi", "-t", "none"],
            ["-m", "gpt-5", "-z", "hi", "-t", "none"],
            # argparse allow_abbrev: unique long-option prefixes parse
            # identically to the exact spellings, so they need the guard too.
            ["--one=hi", "--toolsets=none"],
            ["--oneshot=hi", "--tools=none"],
            ["--one", "hi", "--tools", "none"],
            ["--o=hi", "--to=none"],
            ["--o", "hi", "--to", "none"],
            ["--to", "none", "--o", "hi"],
            ["--ones", "hi", "-t", "none"],
            ["-t", "web", "-z", "hi", "--tools", "none"],
            ["--tools=none,web", "--one=hi"],
            ["--pro", "openai", "-z", "hi", "-t", "none"],
            ["--cont", "sess", "-z", "hi", "-t", "none"],
            ["--in", "f.txt", "-z", "hi", "-t", "none"],
            # Top-level oneshot may cross the built-in chat subcommand and
            # take a chat-local toolset override.
            ["-z", "prompt", "chat", "-t", "none"],
            ["--one=prompt", "chat", "--tools=none"],
        ],
    )
    def test_guard_establishing_argv(self, argv):
        assert main_mod._raw_oneshot_no_tools_preflight(argv) is True

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["-z", "hi"],
            ["-z", "hi", "-t", "terminal"],
            ["-z", "hi", "-t", "terminal,unknown"],
            ["-z", "hi", "-t", "all"],
            ["-z", "hi", "-t", "*"],
            ["-z", "hi", "-t", ""],
            ["-z", "-t none"],
            ["-t", "none"],
            ["chat", "--oneshot", "-t", "none"],
            ["chat", "-t", "none"],
            ["-z", "hi", "-t", "none", "-t", "web"],
            ["-z", "hi", "--", "-t", "none"],
            ["--toolsets", "none"],
            # Ambiguity boundaries: argparse rejects these before oneshot can
            # run (--t/--i are ambiguous; --prof matches nothing because
            # --profile is consumed pre-argparse and abbreviates nowhere).
            ["--t=none", "-z", "hi"],
            ["-z", "hi", "--t", "none"],
            ["--i", "x", "-z", "hi", "-t", "none"],
            ["--prof", "x", "-z", "hi", "-t", "none"],
            # Abbreviations obey last-value-wins and the -- terminator.
            ["-z", "hi", "--tools", "none", "--to", "web"],
            ["--one=hi"],
            ["--one=hi", "--", "--tools=none"],
            # Parser differential: an unknown or ambiguous long option
            # anywhere in the top-level region makes argparse reject the
            # whole argv with a usage error, so the classifier must refuse
            # the guard immediately — even around an otherwise valid
            # exact-none pair (the 41-mismatch reviewer classes).
            ["--t=x", "--oneshot=hi", "--toolsets=none"],
            ["--oneshot=hi", "--t=x", "--toolsets=none"],
            ["--oneshot=hi", "--toolsets=none", "--t=x"],
            ["--t", "--oneshot=hi", "--toolsets=none"],
            ["-z", "hi", "--t", "--toolsets", "none"],
            ["--i", "--oneshot=hi", "--toolsets=none"],
            ["--i=x", "-z", "hi", "-t", "none"],
            ["--frobnicate", "--oneshot=hi", "--toolsets=none"],
            ["--frobnicate=x", "--oneshot=hi", "--toolsets=none"],
            ["--oneshot=hi", "--frobnicate=x", "--toolsets=none"],
            ["--oneshot=hi", "--toolsets=none", "--frobnicate=x"],
            # Boolean long options never accept an inline value — argparse
            # rejects them with "ignored explicit argument".
            ["--safe-mode=1", "-z", "hi", "-t", "none"],
            ["--yolo=x", "--oneshot=hi", "--toolsets=none"],
            # Required oneshot prompt values cannot be option-like tokens or
            # the option terminator; argparse rejects these before dispatch.
            ["-z", "-t none", "-t", "none"],
            ["-z", "--t=x", "-t", "none"],
            ["-z", "--frobnicate", "-t", "none"],
            ["-z", "--", "-t", "none"],
            # Profile preprocessing mirrors first-match exact selectors only;
            # version and non-chat subcommands take precedence over one-shot.
            ["-palpha", "-z", "prompt", "-t", "none"],
            ["-p", "alpha", "-p", "beta", "-z", "prompt", "-t", "none"],
            ["-p", "Bad", "-z", "prompt", "-t", "none"],
            ["--version", "-z", "prompt", "-t", "none"],
            ["-V", "--oneshot=prompt", "--toolsets=none,web"],
            ["-z", "prompt", "-t", "none", "logs"],
            ["-z", "prompt", "-t", "none", "status"],
        ],
    )
    def test_non_guarding_argv(self, argv):
        assert main_mod._raw_oneshot_no_tools_preflight(argv) is False

    def test_profile_resolution_matches_existing_and_missing_profiles(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "hermes"
        (root / "profiles" / "alpha").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(root))
        base = ["-z", "prompt", "-t", "none"]
        assert main_mod._raw_oneshot_no_tools_preflight(
            ["-p", "alpha", *base]
        )
        assert main_mod._raw_oneshot_no_tools_preflight(
            ["--profile=alpha", *base]
        )
        assert main_mod._raw_oneshot_no_tools_preflight(
            ["--profile=Alpha", *base]
        )
        assert main_mod._raw_oneshot_no_tools_preflight(
            ["--profile=Default", *base]
        )
        assert not main_mod._raw_oneshot_no_tools_preflight(
            ["-p", "Alpha", *base]
        )
        assert not main_mod._raw_oneshot_no_tools_preflight(
            ["--profile", "Default", *base]
        )
        assert not main_mod._raw_oneshot_no_tools_preflight(
            ["-p", "missing", *base]
        )
        assert not main_mod._raw_oneshot_no_tools_preflight(
            ["--profile=missing", *base]
        )

    def test_invalid_sticky_active_profile_refuses_guard(self, tmp_path, monkeypatch):
        root = tmp_path / "hermes"
        root.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(root))
        (root / "active_profile").write_text("missing\n", encoding="utf-8")
        assert not main_mod._raw_oneshot_no_tools_preflight(
            ["-z", "prompt", "-t", "NONE"]
        )

    def test_lease_acquire_and_restore_roundtrip(self, monkeypatch):
        import os as os_mod

        monkeypatch.delenv(_GUARD_ENV, raising=False)
        lease = main_mod._acquire_explicit_no_tools_lease(["-z", "hi", "-t", "none"])
        assert os_mod.environ[_GUARD_ENV] == "1"
        main_mod._restore_explicit_no_tools_lease(lease)
        assert _GUARD_ENV not in os_mod.environ

        monkeypatch.setenv(_GUARD_ENV, "")
        lease = main_mod._acquire_explicit_no_tools_lease(["-z", "hi", "-t", "none"])
        assert os_mod.environ[_GUARD_ENV] == "1"
        main_mod._restore_explicit_no_tools_lease(lease)
        assert os_mod.environ[_GUARD_ENV] == ""

        monkeypatch.setenv(_GUARD_ENV, "prior-value")
        lease = main_mod._acquire_explicit_no_tools_lease(["-z", "hi"])
        # Non-qualifying argv must not establish the guard...
        assert os_mod.environ[_GUARD_ENV] == "prior-value"
        main_mod._restore_explicit_no_tools_lease(lease)
        # ...and restore is still exact.
        assert os_mod.environ[_GUARD_ENV] == "prior-value"


class TestPreflightArgparseDifferential:
    """Strict parser differential between the raw classifier and the
    authoritative top-level argparse parser, reproducing the reviewer's
    mismatch classes: ambiguous or unknown long options (inline and bare)
    before, between, and after an otherwise valid exact-none pair.

    Invariants:
    - classifier True  => argparse accepts the argv AND binds a truthy
      -z/--oneshot with effective --toolsets exactly ``none``;
    - argparse rejects => classifier False (the guard is never claimed
      for an argv that dies with a usage error);
    - argparse accepts an exact-none oneshot => classifier True (zero
      activation stays decidable from raw argv).
    """

    _NONE_PAIRS = [
        ["-z", "hi", "-t", "none"],
        ["--oneshot", "hi", "--toolsets", "none"],
        ["--oneshot=hi", "--toolsets=none"],
        ["--one=hi", "--tools=none"],
        ["--o", "hi", "--to", "none"],
        ["--o=hi", "--to=none"],
    ]
    # argparse rejects every argv containing one of these (ambiguous
    # abbreviation, unknown option, or boolean flag with inline value).
    _REJECTED_INJECTIONS = [
        ["--t=x"],
        ["--t"],
        ["--i"],
        ["--i=x"],
        ["--frobnicate"],
        ["--frobnicate=x"],
        ["--safe-mode=1"],
        ["--yolo=x"],
    ]
    # argparse accepts these alongside the pair; the guard must survive.
    _ACCEPTED_INJECTIONS = [
        [],
        ["--safe-mode"],
        ["--pro", "openai"],
        ["-m", "gpt-5"],
    ]

    def test_classifier_matches_authoritative_argparse(self):
        import contextlib
        import io

        from hermes_cli._parser import build_top_level_parser

        parser = build_top_level_parser()[0]

        def authoritative(argv):
            sink = io.StringIO()
            with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
                try:
                    return parser.parse_args(list(argv))
                except SystemExit:
                    return None

        cases = []
        for pair in self._NONE_PAIRS:
            mid = len(pair) // 2
            for injection in self._REJECTED_INJECTIONS + self._ACCEPTED_INJECTIONS:
                cases.append(injection + pair)
                cases.append(pair[:mid] + injection + pair[mid:])
                cases.append(pair + injection)

        mismatches = []
        for argv in cases:
            claimed = main_mod._raw_oneshot_no_tools_preflight(list(argv))
            args = authoritative(argv)
            if args is None:
                if claimed:
                    mismatches.append(
                        (argv, "guard claimed for argparse-rejected argv")
                    )
                continue
            exact_none = (
                bool(getattr(args, "oneshot", None))
                and getattr(args, "toolsets", None) == "none"
            )
            if exact_none and not claimed:
                mismatches.append(
                    (argv, "no guard for parser-accepted exact-none oneshot")
                )
            if claimed and not exact_none:
                mismatches.append(
                    (argv, "guard claimed but authoritative parse is not exact-none")
                )
        assert mismatches == [], mismatches


class TestFirstPositionalArgvParserMirror:
    """_first_positional_argv must resolve long options through the same
    stdlib argparse mirror as the raw preflight classifier: unique long
    abbreviations of (optional-)value flags consume their values exactly
    like allow_abbrev argparse, and unknown or ambiguous long options
    conservatively force plugin discovery instead of misclassifying the
    following value token as the subcommand."""

    def _first(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["hermes"] + argv)
        return main_mod._first_positional_argv()

    @pytest.mark.parametrize(
        "argv",
        [
            ["--o", "hi", "--to", "none"],
            ["--one", "hi", "--tools", "none"],
            ["--o=hi", "--to=none"],
            ["--pro", "openai", "-z", "hi"],
            ["--cont", "sess"],
            ["--cont", "--tui"],
            ["-t", "none", "-z", "hi"],
            ["-tnone", "-z", "hi"],
        ],
    )
    def test_abbreviated_value_flags_consume_their_values(self, monkeypatch, argv):
        assert self._first(monkeypatch, argv) is None
        assert main_mod._plugin_cli_discovery_needed() is False

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["-m", "gpt5", "--provider", "openai", "chat", "msg"], "chat"),
            (["--reasoning", "high", "chat"], "chat"),
            (["--safe-mode", "logs"], "logs"),
            (["--", "kanban"], "kanban"),
            (["kanban", "--t"], "kanban"),
            (["--o", "hi", "--to", "none", "chat"], "chat"),
            (["--cont", "sess", "chat"], "chat"),
        ],
    )
    def test_first_positional_matches_argparse_routing(
        self, monkeypatch, argv, expected
    ):
        assert self._first(monkeypatch, argv) == expected

    @pytest.mark.parametrize(
        "argv",
        [
            ["--t", "chat"],
            ["--t=x", "chat"],
            ["--i", "chat"],
            ["--frobnicate", "chat"],
            ["--frobnicate=x", "chat"],
        ],
    )
    def test_unresolved_long_options_force_discovery_without_misclassifying(
        self, monkeypatch, argv
    ):
        first = self._first(monkeypatch, argv)
        # The token after an unknown/ambiguous long option may be that
        # option's value — never report it (or None) as the subcommand...
        assert first not in {None, "chat"}
        # ...and conservatively keep the eager plugin-discovery path so
        # authoritative argparse produces the outcome (usage error or a
        # plugin subcommand the mirror cannot know about).
        assert main_mod._plugin_cli_discovery_needed() is True
