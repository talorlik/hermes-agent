"""Regression tests for -z/--oneshot flag contracts.

Skills: -z must honor -s/--skills (#31548, #65119). The oneshot path builds
its AIAgent directly (bypassing HermesCLI), so the --skills preload has to be
forwarded explicitly and injected via ``ephemeral_system_prompt``. These tests
pin the forwarding contract and the partial-success semantics shared with
normal CLI chat.

Toolsets: ``--toolsets none`` is a reserved oneshot-only sentinel meaning an
explicit empty native/MCP tool set. Only the exact lowercase token ``none``,
alone (whitespace around that one token is tolerated), is the sentinel; any
case variant, duplicate, blank-edge segment (e.g. ``"none,"``), or mixture
fails with deterministic exit-2 prose before any plugin/MCP resolution.
Explicit toolset lists are all-or-nothing: one unknown or disabled entry
rejects the whole list. The sentinel must survive all normalization as
``AIAgent(enabled_toolsets=[])`` - never collapse to None, config toolsets,
or default tools - and the explicit-no-tools process guard env var must be
set around the agent build and restored exactly afterwards.
"""

import logging
import os
import sys

import pytest

import hermes_cli.oneshot as oneshot_mod
from hermes_cli.oneshot import (
    _build_preloaded_skills_prompt,
    _normalize_skills,
    _validate_explicit_toolsets,
    run_oneshot,
)

# Explicit-no-tools process guard contract (consumed by model_tools at import
# time). Kept as a literal here so the test pins the published env var name.
GUARD_ENV = "HERMES_ONESHOT_EXPLICIT_NO_TOOLS"


class TestNormalizeSkills:
    def test_none_and_empty(self):
        assert _normalize_skills(None) == []
        assert _normalize_skills("") == []
        assert _normalize_skills([]) == []

    def test_comma_separated_string(self):
        assert _normalize_skills("a,b") == ["a", "b"]

    def test_repeated_flags_deduped_order_preserved(self):
        assert _normalize_skills(["b", "a", "b"]) == ["b", "a"]


class TestBuildPreloadedSkillsPrompt:
    def test_no_skills_returns_none(self):
        assert _build_preloaded_skills_prompt(None) is None

    def test_all_missing_raises(self, monkeypatch):
        import agent.skill_commands as sc

        monkeypatch.setattr(
            sc, "build_preloaded_skills_prompt",
            lambda parsed, **kw: ("", [], list(parsed)),
        )
        with pytest.raises(ValueError, match="Unknown skill"):
            _build_preloaded_skills_prompt("not-a-skill")

    def test_partial_success_returns_prompt(self, monkeypatch):
        import agent.skill_commands as sc

        monkeypatch.setattr(
            sc, "build_preloaded_skills_prompt",
            lambda parsed, **kw: ("PROMPT", ["good"], ["bad"]),
        )
        assert _build_preloaded_skills_prompt(["good", "bad"]) == "PROMPT"

    def test_loaded_prompt_returned(self, monkeypatch):
        import agent.skill_commands as sc

        monkeypatch.setattr(
            sc, "build_preloaded_skills_prompt",
            lambda parsed, **kw: ("SKILL CONTENT", ["s"], []),
        )
        assert _build_preloaded_skills_prompt("s") == "SKILL CONTENT"


def _forbid_discovery(monkeypatch):
    """Record (instead of executing) plugin discovery and MCP config reads.

    Returns the call log so tests can assert the sentinel path never touches
    either surface. Recording is used rather than raising because the
    production code wraps both calls in ``except Exception`` blocks that
    would swallow an AssertionError.
    """
    import hermes_cli.config as config_mod
    import hermes_cli.plugins as plugins_mod

    calls: list[str] = []
    monkeypatch.setattr(
        plugins_mod, "discover_plugins", lambda *a, **k: calls.append("discover_plugins")
    )
    monkeypatch.setattr(
        config_mod,
        "read_raw_config",
        lambda *a, **k: calls.append("read_raw_config") or {},
    )
    return calls


class TestValidateNoneSentinel:
    """--toolsets 'none' is the explicit empty tool set (oneshot-only)."""

    def test_none_alone_returns_explicit_empty_list(self):
        assert _validate_explicit_toolsets("none") == ([], None)

    def test_none_alone_in_list_form_returns_explicit_empty_list(self):
        assert _validate_explicit_toolsets(["none"]) == ([], None)

    @pytest.mark.parametrize(
        "spelling", ["none", " none ", "\tnone", ["  none  "], ("none",)]
    )
    def test_whitespace_around_one_lowercase_token_normalizes_to_sentinel(
        self, spelling
    ):
        # Exact whitespace around exactly one lowercase token is the only
        # tolerated variation; list and tuple single-token forms included.
        assert _validate_explicit_toolsets(spelling) == ([], None)

    @pytest.mark.parametrize(
        "variant", ["None", "NONE", "nOnE", ["None"], ("NONE",), "None,web"]
    )
    def test_non_lowercase_none_like_variant_is_exit2_error(self, variant):
        valid, err = _validate_explicit_toolsets(variant)
        assert valid is None
        assert err is not None
        assert "lowercase" in err
        assert "'none'" in err

    def test_none_mixed_with_other_token_is_exit2_error(self):
        valid, err = _validate_explicit_toolsets("none,web")
        assert valid is None
        assert err is not None
        assert "'none'" in err
        assert "only toolset entry" in err
        assert "none, web" in err

    @pytest.mark.parametrize(
        "dup", ["none,none", ["none", "none"], ("none", "none")]
    )
    def test_none_repeated_is_exit2_error(self, dup):
        valid, err = _validate_explicit_toolsets(dup)
        assert valid is None
        assert err is not None
        assert "only toolset entry" in err

    @pytest.mark.parametrize(
        "blank_edge", ["none,", ",none", "none, ,", ["none", ""], ("none", "  ")]
    )
    def test_blank_edge_segments_around_none_are_exit2_error(self, blank_edge):
        # A blank segment next to `none` is an ambiguous half-typed list, not
        # a tolerated spelling; it must fail closed instead of being dropped
        # by normalization.
        valid, err = _validate_explicit_toolsets(blank_edge)
        assert valid is None
        assert err is not None
        assert "only toolset entry" in err

    @pytest.mark.parametrize("combo", ["all,none", "none,all", "none,*", "*,none"])
    def test_none_with_all_or_star_is_exit2_error(self, combo):
        valid, err = _validate_explicit_toolsets(combo)
        assert valid is None
        assert err is not None
        assert "only toolset entry" in err

    def test_sentinel_paths_never_discover_plugins_or_read_mcp_config(self, monkeypatch):
        calls = _forbid_discovery(monkeypatch)
        assert _validate_explicit_toolsets("none") == ([], None)
        for early_error in ("none,web", "NONE", "none,", ["none", "none"], "all,web"):
            valid, err = _validate_explicit_toolsets(early_error)
            assert valid is None
            assert err is not None
        assert calls == []


class TestStrictExplicitToolsets:
    """Explicit --toolsets lists are all-or-nothing (no partial enablement)."""

    @pytest.fixture(autouse=True)
    def _deterministic_resolution(self, monkeypatch):
        # No real plugin scan; a fixed MCP config with one enabled and one
        # disabled server so MCP resolution is deterministic.
        import hermes_cli.config as config_mod
        import hermes_cli.plugins as plugins_mod

        monkeypatch.setattr(plugins_mod, "discover_plugins", lambda *a, **k: None)
        monkeypatch.setattr(
            config_mod,
            "read_raw_config",
            lambda *a, **k: {
                "mcp_servers": {
                    "goodmcp": {"enabled": True},
                    "offmcp": {"enabled": False},
                }
            },
        )

    def test_wholly_valid_list_preserves_user_order(self):
        assert _validate_explicit_toolsets("terminal,web") == (
            ["terminal", "web"],
            None,
        )

    def test_enabled_mcp_server_is_valid(self):
        assert _validate_explicit_toolsets("goodmcp") == (["goodmcp"], None)

    def test_builtin_plus_enabled_mcp_is_valid(self):
        assert _validate_explicit_toolsets("terminal,goodmcp") == (
            ["terminal", "goodmcp"],
            None,
        )

    @pytest.mark.parametrize("token", ["all", "*", " all "])
    def test_all_or_star_alone_preserved(self, token):
        assert _validate_explicit_toolsets(token) == (None, None)

    @pytest.mark.parametrize("combo", ["all,terminal", "terminal,*", "all,all"])
    def test_all_or_star_mixed_with_anything_is_exit2_error(self, combo):
        valid, err = _validate_explicit_toolsets(combo)
        assert valid is None
        assert err is not None
        assert "only toolset entry" in err

    def test_valid_plus_unknown_fails_whole_list(self):
        valid, err = _validate_explicit_toolsets("terminal,bogus-toolset-xyz")
        assert valid is None
        assert err is not None
        assert "all-or-nothing" in err
        assert "bogus-toolset-xyz" in err

    def test_enabled_mcp_plus_unknown_fails_whole_list(self):
        valid, err = _validate_explicit_toolsets("goodmcp,bogus-toolset-xyz")
        assert valid is None
        assert err is not None
        assert "all-or-nothing" in err
        assert "bogus-toolset-xyz" in err

    def test_disabled_mcp_server_fails_whole_list(self):
        valid, err = _validate_explicit_toolsets("terminal,offmcp")
        assert valid is None
        assert err is not None
        assert "all-or-nothing" in err
        assert "offmcp" in err
        assert "enabled: true" in err

    def test_wholly_unknown_list_is_exit2_error(self):
        valid, err = _validate_explicit_toolsets("definitely-not-a-toolset-xyz")
        assert valid is None
        assert err is not None
        assert "all-or-nothing" in err
        assert "definitely-not-a-toolset-xyz" in err
        assert "only toolset entry" not in err


class TestRunOneshotNoneSentinel:
    """run_oneshot must forward the sentinel as an explicit empty list."""

    def _capture_run_agent(self, monkeypatch):
        captured: dict = {}

        def fake_run_agent(prompt, **kwargs):
            captured.update(kwargs)
            return "ok", {
                "final_response": "ok",
                "failed": False,
                "partial": False,
                "completed": True,
            }

        monkeypatch.setattr(oneshot_mod, "_run_agent", fake_run_agent)
        # run_oneshot mutates these in os.environ; route through monkeypatch
        # so the original values are restored after the test.
        monkeypatch.setenv("HERMES_YOLO_MODE", "0")
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "0")
        return captured

    def test_none_reaches_agent_as_explicit_empty_toolsets(self, monkeypatch):
        captured = self._capture_run_agent(monkeypatch)
        rc = run_oneshot("hi", toolsets="none")
        assert rc == 0
        assert captured["toolsets"] == []
        assert captured["use_config_toolsets"] is False

    def test_mixed_none_exits_2_without_running_agent(self, monkeypatch, capsys):
        captured = self._capture_run_agent(monkeypatch)
        rc = run_oneshot("hi", toolsets="none,web")
        assert rc == 2
        assert captured == {}
        err = capsys.readouterr().err
        assert "'none'" in err
        assert "only toolset entry" in err

    def test_omitted_toolsets_keep_config_path(self, monkeypatch):
        captured = self._capture_run_agent(monkeypatch)
        rc = run_oneshot("hi")
        assert rc == 0
        assert captured["toolsets"] is None
        assert captured["use_config_toolsets"] is True


class TestExplicitNoToolsProcessGuard:
    """run_oneshot must set the guard before the agent build (which imports
    run_agent/model_tools) and restore the prior value exactly in finally."""

    def _run_agent_recording_guard(self, monkeypatch, seen, fail=False):
        def fake_run_agent(prompt, **kwargs):
            import os

            seen["guard_during"] = os.environ.get(GUARD_ENV)
            if fail:
                raise RuntimeError("boom")
            return "ok", {
                "final_response": "ok",
                "failed": False,
                "partial": False,
                "completed": True,
            }

        monkeypatch.setattr(oneshot_mod, "_run_agent", fake_run_agent)
        monkeypatch.setenv("HERMES_YOLO_MODE", "0")
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "0")

    def test_guard_env_name_is_published(self):
        assert oneshot_mod._EXPLICIT_NO_TOOLS_ENV == GUARD_ENV

    def test_guard_set_during_agent_build_and_removed_after(self, monkeypatch):
        import os

        seen: dict = {}
        self._run_agent_recording_guard(monkeypatch, seen)
        monkeypatch.delenv(GUARD_ENV, raising=False)
        rc = run_oneshot("hi", toolsets="none")
        assert rc == 0
        assert seen["guard_during"] == "1"
        assert GUARD_ENV not in os.environ

    def test_prior_guard_value_restored_exactly(self, monkeypatch):
        import os

        seen: dict = {}
        self._run_agent_recording_guard(monkeypatch, seen)
        monkeypatch.setenv(GUARD_ENV, "prior-value")
        rc = run_oneshot("hi", toolsets="none")
        assert rc == 0
        assert seen["guard_during"] == "1"
        assert os.environ[GUARD_ENV] == "prior-value"

    def test_guard_restored_when_agent_fails(self, monkeypatch):
        import os

        seen: dict = {}
        self._run_agent_recording_guard(monkeypatch, seen, fail=True)
        monkeypatch.delenv(GUARD_ENV, raising=False)
        rc = run_oneshot("hi", toolsets="none")
        assert rc == 1
        assert seen["guard_during"] == "1"
        assert GUARD_ENV not in os.environ

    def test_guard_not_set_for_ordinary_runs(self, monkeypatch):
        seen: dict = {}
        self._run_agent_recording_guard(monkeypatch, seen)
        monkeypatch.delenv(GUARD_ENV, raising=False)
        rc = run_oneshot("hi")
        assert rc == 0
        assert seen["guard_during"] is None


class TestExplicitEmptyToolsetsOwnerBoundary:
    """The explicit empty set survives into AIAgent(enabled_toolsets=[]) and
    resolves to zero tool definitions / zero valid tool names."""

    def test_run_agent_builds_aiagent_with_empty_toolsets(self, monkeypatch):
        import run_agent as run_agent_mod

        import hermes_cli.config as config_mod
        import hermes_cli.mcp_startup as mcp_startup_mod
        import hermes_cli.runtime_provider as runtime_provider_mod
        import hermes_cli.tools_config as tools_config_mod
        from tools.process_registry import process_registry

        captured: dict = {}
        platform_tools_calls: list = []
        mcp_calls: list = []

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run_conversation(self, prompt):
                return {"final_response": "ok"}

            def shutdown_memory_provider(self, *a, **k):
                pass

            def close(self):
                pass

        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        monkeypatch.setattr(config_mod, "load_config", lambda: {})
        monkeypatch.setattr(
            runtime_provider_mod,
            "resolve_runtime_provider",
            lambda **k: {"provider": "test"},
        )
        monkeypatch.setattr(
            tools_config_mod,
            "_get_platform_tools",
            lambda *a, **k: platform_tools_calls.append(a) or set(),
        )
        monkeypatch.setattr(
            mcp_startup_mod,
            "ensure_mcp_discovery_before_agent_build",
            lambda *a, **k: mcp_calls.append(k),
        )
        monkeypatch.setattr(run_agent_mod, "AIAgent", FakeAgent)
        monkeypatch.setattr(oneshot_mod, "_create_session_db_for_oneshot", lambda: None)
        monkeypatch.setattr(oneshot_mod, "get_fallback_chain", lambda cfg: None)
        monkeypatch.setattr(
            process_registry, "wait_for_pending_completions", lambda *a, **k: None
        )

        response, result = oneshot_mod._run_agent(
            "hi", toolsets=[], use_config_toolsets=False
        )

        assert response == "ok"
        assert result == {"final_response": "ok"}
        # The falsy empty list must survive as exactly [] - not None (which
        # AIAgent treats as "all/default tools") and not config toolsets.
        assert captured["enabled_toolsets"] == []
        assert captured["enabled_toolsets"] is not None
        assert platform_tools_calls == []
        # Emptiness is established without MCP discovery side effects.
        assert mcp_calls == []

    def test_empty_enabled_toolsets_resolve_to_zero_tools(self, monkeypatch):
        # Owner-boundary contract: AIAgent derives agent.tools and
        # agent.valid_tool_names directly from get_tool_definitions(), so an
        # empty enabled_toolsets list must produce zero tool definitions.
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        from model_tools import get_tool_definitions

        tools = get_tool_definitions(enabled_toolsets=[], quiet_mode=True)
        assert tools == []
        assert {t["function"]["name"] for t in tools} == set()


class TestRealAgentInitExplicitEmptyToolsets:
    """Real AIAgent initialization (agent.agent_init) with enabled_toolsets=[]
    must produce an empty tool surface, skip plugin discovery, and opt out of
    the between-turns MCP refresh. None / nonempty lists are unchanged."""

    def _build_agent(self, monkeypatch, enabled_toolsets, discover_calls):
        import hermes_cli.plugins as plugins_mod
        import run_agent as run_agent_mod

        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.setattr(
            plugins_mod,
            "discover_plugins",
            lambda *a, **k: discover_calls.append("discover"),
        )
        monkeypatch.setattr(
            run_agent_mod, "OpenAI", lambda **_kw: object(), raising=False
        )
        if enabled_toolsets is None or len(enabled_toolsets) > 0:
            # The full/named catalog path is not under test here; keep the
            # control cases fast and environment-independent.
            monkeypatch.setattr(
                run_agent_mod,
                "get_tool_definitions",
                lambda *a, **k: [
                    {"type": "function", "function": {"name": "stub_tool"}}
                ],
            )
        agent = run_agent_mod.AIAgent(
            model="gpt-5",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            platform="cli",
            max_iterations=1,
            quiet_mode=True,
            skip_memory=True,
            enabled_toolsets=enabled_toolsets,
        )
        return agent

    def test_empty_list_yields_no_tools_and_skips_discovery_and_refresh(
        self, monkeypatch
    ):
        discover_calls: list = []
        agent = self._build_agent(monkeypatch, [], discover_calls)
        assert agent.tools == []
        assert agent.valid_tool_names == set()
        assert agent._skip_mcp_refresh is True
        assert discover_calls == []

    def test_none_toolsets_keep_discovery_and_refresh(self, monkeypatch):
        discover_calls: list = []
        agent = self._build_agent(monkeypatch, None, discover_calls)
        assert agent._skip_mcp_refresh is False
        assert discover_calls == ["discover"]
        assert agent.valid_tool_names == {"stub_tool"}

    def test_nonempty_toolsets_keep_discovery_and_refresh(self, monkeypatch):
        discover_calls: list = []
        agent = self._build_agent(monkeypatch, ["terminal"], discover_calls)
        assert agent._skip_mcp_refresh is False
        assert discover_calls == ["discover"]
        assert agent.valid_tool_names == {"stub_tool"}


# =========================================================================
# run_oneshot global-state transaction (OR1/OR2)
# =========================================================================

# The three process globals run_oneshot mutates, plus the logging-disable
# integer. Absent, "", "0", "1", and arbitrary strings are DISTINCT prior
# states and must all round-trip exactly.
_STATE_VARS = ("HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS", GUARD_ENV)

_SUCCESS_RESULT = {
    "final_response": "ok",
    "failed": False,
    "partial": False,
    "completed": True,
}


def _global_state_snapshot() -> tuple[dict, int]:
    return (
        {name: os.environ.get(name) for name in _STATE_VARS},
        logging.root.manager.disable,
    )


def _apply_prior_state(monkeypatch, prior) -> None:
    for name in _STATE_VARS:
        if prior is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, prior)


@pytest.fixture
def _logging_threshold_guard():
    """Restore the process logging-disable threshold after each test — the
    exact global the transaction under test must also restore."""
    prior = logging.root.manager.disable
    yield
    logging.disable(prior)


class TestRunOneshotGlobalStateTransaction:
    """OR1: a successful run_oneshot must restore the exact prior presence
    and value of HERMES_YOLO_MODE, HERMES_ACCEPT_HOOKS, and the
    explicit-no-tools guard, plus the exact prior logging-disable integer.
    A leaked HERMES_YOLO_MODE=1 would hand automatic approval bypass to any
    later ordinary agent run in the same process."""

    @pytest.mark.parametrize("toolsets", ["none", None], ids=["none", "omitted"])
    @pytest.mark.parametrize("threshold", [0, logging.WARNING])
    @pytest.mark.parametrize(
        "prior", [None, "", "0", "1", "custom-prior"],
        ids=["absent", "empty", "zero", "one", "arbitrary"],
    )
    def test_success_restores_all_globals_exactly(
        self, monkeypatch, _logging_threshold_guard, prior, threshold, toolsets
    ):
        monkeypatch.setattr(
            oneshot_mod,
            "_run_agent",
            lambda prompt, **kw: ("ok", dict(_SUCCESS_RESULT)),
        )
        _apply_prior_state(monkeypatch, prior)
        logging.disable(threshold)
        expected = _global_state_snapshot()

        rc = run_oneshot("hi", toolsets=toolsets)

        assert rc == 0
        assert _global_state_snapshot() == expected
        # Presence (not just value) must round-trip: absent and "" differ.
        for name in _STATE_VARS:
            assert (name in os.environ) == (prior is not None)


class _OneshotProbeError(BaseException):
    """Deliberately BaseException-derived: the transaction must survive
    exceptions that are not Exception subclasses."""


class TestRunOneshotBoundaryFailureRestoration:
    """OR2: every run_oneshot boundary — validation exit 2, stateless-channel
    declaration, devnull creation, _run_agent, usage reporting, response
    sanitization, and the final output write — must restore all four globals
    exactly, whether the failure becomes a return code or propagates."""

    _EXC_IDS = [
        "RuntimeError",
        "KeyboardInterrupt",
        "SystemExit-int",
        "SystemExit-none",
        "SystemExit-str",
        "BaseException",
    ]

    @staticmethod
    def _exceptions():
        return [
            RuntimeError("agent boom"),
            KeyboardInterrupt(),
            SystemExit(3),
            SystemExit(None),
            SystemExit("exit message"),
            _OneshotProbeError("agent base"),
        ]

    def _arrange(self, monkeypatch):
        """Distinct prior states across the three vars plus a nonzero logging
        threshold, so a partial restore cannot pass by accident."""
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "")
        monkeypatch.setenv(GUARD_ENV, "prior-guard")
        logging.disable(logging.INFO)
        return _global_state_snapshot()

    def _stub_success_agent(self, monkeypatch):
        monkeypatch.setattr(
            oneshot_mod,
            "_run_agent",
            lambda prompt, **kw: ("ok", dict(_SUCCESS_RESULT)),
        )

    def test_provider_validation_exit2_restores(
        self, monkeypatch, _logging_threshold_guard, capsys
    ):
        expected = self._arrange(monkeypatch)
        assert run_oneshot("hi", provider="openrouter") == 2
        assert _global_state_snapshot() == expected

    def test_toolsets_validation_exit2_restores(
        self, monkeypatch, _logging_threshold_guard, capsys
    ):
        expected = self._arrange(monkeypatch)
        assert run_oneshot("hi", toolsets="none,web") == 2
        assert _global_state_snapshot() == expected

    @pytest.mark.parametrize("exc_index", range(6), ids=_EXC_IDS)
    def test_agent_failure_restores(
        self, monkeypatch, _logging_threshold_guard, exc_index
    ):
        exc = self._exceptions()[exc_index]

        def _boom(prompt, **kw):
            raise exc

        monkeypatch.setattr(oneshot_mod, "_run_agent", _boom)
        expected = self._arrange(monkeypatch)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            with pytest.raises(type(exc)):
                run_oneshot("hi", toolsets="none")
        else:
            # Ordinary and custom-BaseException agent failures become rc 1.
            assert run_oneshot("hi", toolsets="none") == 1
        assert _global_state_snapshot() == expected

    @pytest.mark.parametrize("exc_index", range(6), ids=_EXC_IDS)
    def test_stateless_declaration_failure_restores(
        self, monkeypatch, _logging_threshold_guard, exc_index
    ):
        exc = self._exceptions()[exc_index]
        self._stub_success_agent(monkeypatch)

        def _declare():
            raise exc

        monkeypatch.setattr(oneshot_mod, "declare_stateless_channel", _declare)
        expected = self._arrange(monkeypatch)
        with pytest.raises(type(exc)):
            run_oneshot("hi", toolsets="none")
        assert _global_state_snapshot() == expected

    def test_devnull_open_failure_restores(
        self, monkeypatch, _logging_threshold_guard
    ):
        import builtins

        self._stub_success_agent(monkeypatch)
        real_open = builtins.open

        def _open(file, *args, **kwargs):
            if file == os.devnull:
                raise OSError("devnull unavailable")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _open)
        expected = self._arrange(monkeypatch)
        with pytest.raises(OSError, match="devnull unavailable"):
            run_oneshot("hi", toolsets="none")
        assert _global_state_snapshot() == expected

    def test_usage_reporting_failure_restores(
        self, monkeypatch, _logging_threshold_guard
    ):
        self._stub_success_agent(monkeypatch)

        def _usage(*args, **kwargs):
            raise RuntimeError("usage boom")

        monkeypatch.setattr(oneshot_mod, "_write_usage_file", _usage)
        expected = self._arrange(monkeypatch)
        with pytest.raises(RuntimeError, match="usage boom"):
            run_oneshot("hi", toolsets="none")
        assert _global_state_snapshot() == expected

    def test_sanitization_failure_restores(
        self, monkeypatch, _logging_threshold_guard
    ):
        import agent.message_sanitization as sanitization_mod

        self._stub_success_agent(monkeypatch)

        def _sanitize(_text):
            raise RuntimeError("sanitize boom")

        monkeypatch.setattr(sanitization_mod, "_sanitize_surrogates", _sanitize)
        expected = self._arrange(monkeypatch)
        with pytest.raises(RuntimeError, match="sanitize boom"):
            run_oneshot("hi", toolsets="none")
        assert _global_state_snapshot() == expected

    def test_final_output_write_failure_restores(
        self, monkeypatch, _logging_threshold_guard
    ):
        self._stub_success_agent(monkeypatch)

        class _BrokenStdout:
            def write(self, _data):
                raise OSError("stdout gone")

            def flush(self):
                pass

        monkeypatch.setattr(sys, "stdout", _BrokenStdout())
        expected = self._arrange(monkeypatch)
        with pytest.raises(OSError, match="stdout gone"):
            run_oneshot("hi", toolsets="none")
        assert _global_state_snapshot() == expected


class TestRunOneshotDevnullCloseFailureRestoration:
    """OR2 extension: devnull.close() inside the outer finally is itself a
    failure boundary. A BaseException raised there must not skip restoring
    HERMES_YOLO_MODE, HERMES_ACCEPT_HOOKS, the logging-disable threshold,
    or the explicit-no-tools guard; it must propagate when the body
    finished normally and must never mask a failure already unwinding out
    of the body. Ordinary Exceptions from close stay swallowed."""

    def _arrange(self, monkeypatch):
        """Distinct prior states across the three vars plus a nonzero logging
        threshold, so a partial restore cannot pass by accident."""
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "")
        monkeypatch.setenv(GUARD_ENV, "prior-guard")
        logging.disable(logging.INFO)
        return _global_state_snapshot()

    def _stub_success_agent(self, monkeypatch):
        monkeypatch.setattr(
            oneshot_mod,
            "_run_agent",
            lambda prompt, **kw: ("ok", dict(_SUCCESS_RESULT)),
        )

    def _bomb_devnull_close(self, monkeypatch, exc):
        """Make run_oneshot's devnull handle raise ``exc`` from close().
        The real file is closed first so the test leaks no descriptor."""
        import builtins

        real_open = builtins.open

        class _CloseBomb:
            def __init__(self, inner):
                self._inner = inner

            def write(self, data):
                return self._inner.write(data)

            def flush(self):
                self._inner.flush()

            def close(self):
                self._inner.close()
                raise exc

        def _open(file, *args, **kwargs):
            handle = real_open(file, *args, **kwargs)
            if file == os.devnull:
                return _CloseBomb(handle)
            return handle

        monkeypatch.setattr(builtins, "open", _open)

    @pytest.mark.parametrize(
        "exc_index",
        range(3),
        ids=["KeyboardInterrupt", "SystemExit-int", "BaseException"],
    )
    def test_close_base_exception_restores_then_propagates(
        self, monkeypatch, _logging_threshold_guard, exc_index
    ):
        exc = [KeyboardInterrupt(), SystemExit(3), _OneshotProbeError("close bomb")][
            exc_index
        ]
        self._stub_success_agent(monkeypatch)
        self._bomb_devnull_close(monkeypatch, exc)
        expected = self._arrange(monkeypatch)
        with pytest.raises(type(exc)):
            run_oneshot("hi", toolsets="none")
        assert _global_state_snapshot() == expected

    def test_close_ordinary_exception_swallowed_and_restores(
        self, monkeypatch, _logging_threshold_guard
    ):
        self._stub_success_agent(monkeypatch)
        self._bomb_devnull_close(monkeypatch, OSError("close boom"))
        expected = self._arrange(monkeypatch)
        assert run_oneshot("hi", toolsets="none") == 0
        assert _global_state_snapshot() == expected

    @pytest.mark.parametrize(
        "exc_index",
        range(4),
        ids=["RuntimeError", "KeyboardInterrupt", "SystemExit-int", "BaseException"],
    )
    def test_body_failure_wins_over_close_base_exception(
        self, monkeypatch, _logging_threshold_guard, exc_index
    ):
        body_exc = [
            RuntimeError("usage boom"),
            KeyboardInterrupt(),
            SystemExit(3),
            _OneshotProbeError("usage base"),
        ][exc_index]
        self._stub_success_agent(monkeypatch)
        self._bomb_devnull_close(monkeypatch, _OneshotProbeError("close bomb"))

        def _usage(*args, **kwargs):
            raise body_exc

        monkeypatch.setattr(oneshot_mod, "_write_usage_file", _usage)
        expected = self._arrange(monkeypatch)
        with pytest.raises(type(body_exc)) as excinfo:
            run_oneshot("hi", toolsets="none")
        # The body failure itself must surface, not the close bomb.
        assert excinfo.value is body_exc
        assert _global_state_snapshot() == expected
