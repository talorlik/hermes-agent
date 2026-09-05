"""Oneshot (-z) mode: send a prompt, get the final content block, exit.

Toolsets = explicit --toolsets, else the user's "cli" toolsets from `hermes tools`. The reserved
token `none`, passed alone, selects an explicit empty tool set (no native and no MCP tools);
combining it with any other entry is a validation error (exit 2). Rules /
memory / AGENTS.md / preloaded skills = same as a normal chat turn. Approvals are auto-bypassed
(HERMES_YOLO_MODE=1). Model/provider mirror `hermes chat`: both optional; only --model → auto-detect
the provider; only --provider → error (ambiguous).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gateway.session_context import declare_stateless_channel
from hermes_cli.fallback_config import get_fallback_chain

_ALL_TOOLSETS = {"all", "*"}

# Keys copied from the run result into the ``--usage-file`` report. ``service_tier`` is a
# billing-audit field: the tier REQUESTED via request_overrides.extra_body (None when unset), so
# batch pipelines can verify the tier they pay for went out on the wire.
_USAGE_KEYS = (
    "estimated_cost_usd", "cost_status", "cost_source", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens", "api_calls",
    "model", "provider", "session_id", "completed",
)


def _normalize_toolsets(toolsets: object = None) -> list[str] | None:
    """Split repeated/comma-separated toolset flags into a clean list (``None`` when empty)."""
    if not toolsets:
        return None
    items = toolsets if isinstance(toolsets, (list, tuple)) else [toolsets]
    parts = [str(item).split(",") if isinstance(item, str) else [str(item)] for item in items]
    return [p.strip() for chunk in parts for p in chunk if p.strip()] or None


def _normalize_skills(skills: object = None) -> list[str]:
    """Normalize repeated/comma-separated skill flags and preserve order."""
    return list(dict.fromkeys(_normalize_toolsets(skills) or []))


def _build_preloaded_skills_prompt(skills: object = None) -> str | None:
    """Load requested skills using the same partial-success contract as CLI chat."""
    parsed_skills = _normalize_skills(skills)
    if not parsed_skills:
        return None

    from agent.skill_commands import build_preloaded_skills_prompt

    skills_prompt, loaded_skills, missing_skills = build_preloaded_skills_prompt(parsed_skills)
    if missing_skills:
        missing_display = ", ".join(missing_skills)
        if not loaded_skills:
            raise ValueError(f"Unknown skill(s): {missing_display}")
        logging.warning(
            "Unknown skill(s) requested, skipping: %s. Continuing with: %s. "
            "List available skills with `hermes skills list`.",
            missing_display,
            ", ".join(loaded_skills),
        )
    return skills_prompt or None


def _configured_mcp_servers() -> tuple[set[str], set[str]]:
    """``(enabled, disabled)`` MCP server names from config; both empty on any error."""
    try:
        from hermes_cli.config import read_raw_config
        from hermes_cli.tools_config import _parse_enabled_flag

        cfg = read_raw_config()
        mcp_servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
        enabled: set[str] = set()
        disabled: set[str] = set()
        for name, server_cfg in mcp_servers.items():
            if not isinstance(server_cfg, dict):
                continue
            target = enabled if _parse_enabled_flag(server_cfg.get("enabled", True), default=True) else disabled
            target.add(str(name))
        return enabled, disabled
    except Exception:
        return set(), set()


# Reserved oneshot-only sentinel: `--toolsets none` means an explicit empty native/MCP tool set
# (AIAgent(enabled_toolsets=[])). Not a real toolset name. Only this exact lowercase spelling,
# alone, is the sentinel.
_TOOLSETS_NONE_SENTINEL = "none"

# Explicit-no-tools process guard. run_oneshot sets this to "1" around the agent build for the
# validated `none` sentinel, BEFORE run_agent/model_tools are imported, and restores the prior
# value exactly afterwards. model_tools consults it at import time to skip its import-time plugin
# discovery; hermes_cli.main establishes the same guard from raw argv at import time.
_EXPLICIT_NO_TOOLS_ENV = "HERMES_ONESHOT_EXPLICIT_NO_TOOLS"


def _raw_toolset_tokens(toolsets: object = None) -> list[str]:
    """Split --toolsets like ``_normalize_toolsets`` but KEEP blank segments.

    Each token is whitespace-stripped, so exact whitespace around a token normalizes away, but
    empty segments (``"none,"`` / ``["none", ""]``) are preserved: for the reserved ``none``
    sentinel a blank edge is an ambiguous half-typed list that must fail closed, not be silently
    dropped.
    """
    if not toolsets:
        return []
    raw_items = [toolsets] if isinstance(toolsets, str) else toolsets
    if not isinstance(raw_items, (list, tuple)):
        raw_items = [raw_items]

    tokens: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            tokens.extend(part.strip() for part in item.split(","))
        else:
            tokens.append(str(item).strip())
    return tokens


def _precheck_explicit_toolsets(
    toolsets: object = None,
) -> tuple[list[str] | None, str | None] | None:
    """Side-effect-free early validation of explicit --toolsets.

    Decides every outcome that must be known WITHOUT plugin discovery or MCP config reads: the
    reserved ``none`` sentinel (including its case / duplicate / blank-edge / mixture failure
    modes) and the all/* mixture rule. Returns ``None`` when the input was omitted or needs full
    resolution; otherwise the terminal ``(enabled_toolsets, error)`` outcome.

    hermes_cli.main uses this before ``_prepare_agent_startup`` so the no-tools and error paths
    never start plugin/MCP/hook discovery; ``run_oneshot`` re-validates and stays the single
    emitter of the exit-2 message.
    """
    normalized = _normalize_toolsets(toolsets)
    if normalized is None:
        return None

    raw = _raw_toolset_tokens(toolsets)
    if any(token.lower() == _TOOLSETS_NONE_SENTINEL for token in raw):
        # Exactly one lowercase `none` token (whitespace already stripped) is the sentinel. A
        # case variant, duplicate, blank segment, or any other entry alongside it is ambiguous
        # and fails closed instead of being silently ignored.
        if raw != [_TOOLSETS_NONE_SENTINEL]:
            return None, (
                "hermes -z: --toolsets 'none' selects an explicit empty tool "
                "set and must be the only toolset entry, spelled exactly "
                f"lowercase; got: {', '.join(raw)}. Pass 'none' alone, or "
                "remove it.\n"
            )
        return [], None

    if any(token in _ALL_TOOLSETS for token in normalized) and len(normalized) > 1:
        return None, (
            "hermes -z: --toolsets 'all' enables every toolset and must be "
            f"the only toolset entry; got: {', '.join(normalized)}. Pass "
            "'all' alone, or list specific toolsets.\n"
        )

    return None


def _validate_explicit_toolsets(toolsets: object = None) -> tuple[list[str] | None, str | None]:
    """Validate explicit --toolsets into ``(enabled_toolsets, error)``.

    Returns ``(None, None)`` when no explicit toolsets were passed (use the config path) or for
    ``all``/``*`` alone (every toolset), ``([], None)`` for the reserved ``none`` sentinel, the
    full user-ordered list when every entry is a valid toolset or an enabled MCP server, or
    ``(None, message)`` when the run must abort with exit 2.

    Explicit lists are all-or-nothing: a single unknown or disabled entry rejects the whole list.
    Partial enablement would silently narrow what the caller asked for.
    """
    normalized = _normalize_toolsets(toolsets)
    if normalized is None:
        return None, None

    early = _precheck_explicit_toolsets(toolsets)
    if early is not None:
        return early

    # `all` / `*` alone: every toolset. Returned as None so the agent takes its default
    # full-catalog path; mixtures were rejected by the precheck.
    if normalized[0] in _ALL_TOOLSETS:
        return None, None

    try:
        from toolsets import validate_toolset
    except Exception as exc:
        return None, f"hermes -z: failed to validate --toolsets: {exc}\n"

    unresolved = [name for name in normalized if not validate_toolset(name)]

    if unresolved:
        try:
            from hermes_cli.plugins import discover_plugins

            discover_plugins()
            unresolved = [name for name in unresolved if not validate_toolset(name)]
        except Exception:
            pass

    mcp_names, mcp_disabled = _configured_mcp_servers() if unresolved else (set(), set())
    disabled = [name for name in unresolved if name in mcp_disabled]
    unknown = [name for name in unresolved if name not in mcp_names and name not in mcp_disabled]

    if unknown or disabled:
        parts = []
        if unknown:
            parts.append(f"unknown entries: {', '.join(unknown)}")
        if disabled:
            parts.append(
                "disabled MCP servers (set enabled: true in config.yaml to "
                f"use): {', '.join(disabled)}"
            )
        return None, (
            "hermes -z: --toolsets is all-or-nothing; "
            + "; ".join(parts)
            + ". No tools were enabled; fix or remove the listed entries.\n"
        )

    return list(normalized), None


def _write_usage_file(path: Optional[str], result: dict, failure: Optional[str] = None) -> None:
    """Best-effort JSON usage report for pipelines (``-z --usage-file``).

    Written even on failure so callers can always account for spend. Never raises — a broken usage
    write must not mask the run's own outcome.
    """
    if not path:
        return
    try:
        report = {key: result.get(key) for key in _USAGE_KEYS}
        report["failed"] = bool(result.get("failed")) or failure is not None
        report["service_tier"] = result.get("service_tier")
        if failure is not None:
            report["failure"] = failure
        out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _restore_env_var(name: str, prior: str | None) -> None:
    """Restore an env var to its exact prior state; absent (None) and empty string are distinct
    prior states and must round-trip exactly."""
    if prior is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = prior


def run_oneshot(
    prompt: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: object = None,
    skills: object = None,
    usage_file: Optional[str] = None,
) -> int:
    """Execute a single prompt and print only the final content block.

    Model/provider fall back to ``HERMES_INFERENCE_MODEL`` and config.yaml. ``usage_file`` gets a
    JSON usage report even when the run fails. Returns the exit code; the caller owns process
    termination.

    The reserved ``toolsets`` token ``none``, passed alone, runs the agent with an explicit empty
    tool set (no native and no MCP tools); combining it with any other entry fails validation with
    exit 2.

    Global-state contract: one outer transaction snapshots - before the logging disable,
    validation, and every environment assignment below - the exact prior presence and value of
    HERMES_YOLO_MODE, HERMES_ACCEPT_HOOKS, and HERMES_ONESHOT_EXPLICIT_NO_TOOLS plus the exact
    logging-disable integer, and restores all four on every return and every exception path,
    BaseException included. That makes the call state-clean for those globals; it does NOT reverse
    tools.approval's import-time _YOLO_MODE_FROZEN snapshot - an in-process caller that already
    imported tools.approval keeps that frozen approval state regardless of the environment restore.
    """
    prior_yolo = os.environ.get("HERMES_YOLO_MODE")
    prior_accept_hooks = os.environ.get("HERMES_ACCEPT_HOOKS")
    prior_guard = os.environ.get(_EXPLICIT_NO_TOOLS_ENV)
    prior_logging_disable = logging.root.manager.disable

    devnull = None
    body_failed = False
    try:
        # Silence every stdlib logger: AIAgent, tools and provider adapters log to stderr through
        # the root logger. File handlers from setup_logging() keep working (level-independent).
        logging.disable(logging.CRITICAL)

        # --provider without --model is ambiguous (the provider may not host the configured model,
        # and picking its catalog default hides the mismatch). Validate BEFORE the stderr redirect.
        env_model_early = os.getenv("HERMES_INFERENCE_MODEL", "").strip()
        if provider and not ((model or "").strip() or env_model_early):
            sys.stderr.write(
                "hermes -z: --provider requires --model (or HERMES_INFERENCE_MODEL). "
                "Pass both explicitly, or neither to use your configured defaults.\n"
            )
            return 2

        explicit_toolsets, toolsets_error = _validate_explicit_toolsets(toolsets)
        if toolsets_error:
            sys.stderr.write(toolsets_error)
            return 2
        use_config_toolsets = _normalize_toolsets(toolsets) is None

        # Non-interactive by definition - an approval prompt would hang forever. Only after
        # side-effect-free validation succeeded: the exit-2 paths above must not mutate approval
        # state at all.
        os.environ["HERMES_YOLO_MODE"] = "1"
        os.environ["HERMES_ACCEPT_HOOKS"] = "1"

        # Nothing here drains process_registry.completion_queue (only cli.py's process_loop and
        # the gateway watchers do), so left unbound delegate_task would be forced background and
        # every subagent result discarded. Stateless routes it to the inline/synchronous path.
        declare_stateless_channel()

        # Redirect stderr AND stdout for the entire call tree; the final response goes to the real
        # stdout at the end.
        real_stdout = sys.stdout
        real_stderr = sys.stderr
        devnull = open(os.devnull, "w", encoding="utf-8")

        # Explicit-no-tools process guard: _run_agent imports run_agent, which transitively
        # imports model_tools; that module runs plugin discovery at import time unless this guard
        # is set. Set it before the agent build so a `none` run stays side-effect free. The outer
        # finally restores the prior value exactly (guard last) so nested or subsequent runs in
        # this process see an unchanged environment.
        if explicit_toolsets == []:
            os.environ[_EXPLICIT_NO_TOOLS_ENV] = "1"

        response: Optional[str] = None
        result: dict = {}
        failure: BaseException | None = None
        with redirect_stdout(devnull), redirect_stderr(devnull):
            try:
                response, result = _run_agent(
                    prompt,
                    model=model,
                    provider=provider,
                    toolsets=explicit_toolsets,
                    use_config_toolsets=use_config_toolsets,
                    skills=skills,
                )
            except BaseException as exc:  # noqa: BLE001
                # Capture anything escaping the agent (OSError from prompt_toolkit on a non-TTY
                # pipe, KeyboardInterrupt, SystemExit, ...) so it reaches the real stderr instead
                # of dying silently past the redirect - the worst failure mode in cron / SSH /
                # subprocess use.
                failure = exc

        if failure is not None:
            # Control-flow exceptions (Ctrl-C / sys.exit inside the agent) re-raise to the parent.
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                _write_usage_file(usage_file, result, failure=repr(failure))
                raise failure
            _write_usage_file(usage_file, result, failure=str(failure))
            real_stderr.write(f"hermes -z: agent failed: {failure}\n")
            real_stderr.flush()
            return 1

        _write_usage_file(usage_file, result)

        if response:
            # Lone UTF-16 surrogates would raise UnicodeEncodeError on a real stdout and abort
            # with exit 1 after the turn already completed - scrub to U+FFFD first. See #80366.
            from agent.message_sanitization import _sanitize_surrogates

            response = _sanitize_surrogates(response)
            real_stdout.write(response)
            if not response.endswith("\n"):
                real_stdout.write("\n")
            real_stdout.flush()

        if not (response or "").strip():
            if result.get("failed") or result.get("partial"):
                return 2
            real_stderr.write("hermes -z: no final response was produced; treating the run as failed.\n")
            real_stderr.flush()
            return 1
        return 0
    except BaseException:
        # Flag (not handle) an unwinding body failure so the finally below can rank it above a
        # devnull-close failure without consulting ambient sys.exc_info(), which an enclosing
        # except block in an in-process caller could pollute.
        body_failed = True
        raise
    finally:
        # Cleanup order (outer transaction): the stdout/stderr redirects and the
        # agent/session/process cleanup owned by _run_agent have already unwound inside the try
        # above; close devnull, then restore the named globals. The explicit-no-tools guard is
        # restored LAST so no cleanup import can trigger plugin discovery while teardown is still
        # running. close() is itself a failure boundary: an ordinary Exception stays swallowed, but
        # a BaseException is captured so the nested finally still restores all four globals, then
        # re-raised only when no body failure is already unwinding (the body failure must never be
        # masked by cleanup).
        close_failure: BaseException | None = None
        try:
            if devnull is not None:
                try:
                    devnull.close()
                except Exception:
                    pass
                except BaseException as exc:
                    close_failure = exc
        finally:
            _restore_env_var("HERMES_ACCEPT_HOOKS", prior_accept_hooks)
            _restore_env_var("HERMES_YOLO_MODE", prior_yolo)
            logging.disable(prior_logging_disable)
            _restore_env_var(_EXPLICIT_NO_TOOLS_ENV, prior_guard)
        if close_failure is not None and not body_failed:
            raise close_failure


def _create_session_db_for_oneshot():
    """Best-effort SessionDB — oneshot bypasses ``HermesCLI._init_agent()``, so it must wire the
    SQLite store itself or ``session_search`` is advertised but always unavailable."""
    try:
        from hermes_state import SessionDB

        return SessionDB()
    except Exception as exc:
        logging.debug("SQLite session store not available for oneshot mode: %s", exc)
        return None


@dataclass
class _ModelChoice:
    model: str
    provider: str | None
    base_url: str | None = None
    api_key: str | None = None


def _configured_model(model_cfg: object) -> str:
    if isinstance(model_cfg, str):
        return model_cfg
    raw = model_cfg.get("default") or model_cfg.get("model") or ""
    if isinstance(raw, dict):
        from hermes_cli.config import split_model_config_default

        return split_model_config_default(raw)[0]
    return str(raw or "")


def _resolve_model_and_provider(cfg: dict, model: Optional[str], provider: Optional[str]) -> _ModelChoice:
    """Effective model = arg → env → config; provider = arg → auto-detect → config/env.

    Auto-detection only runs when the model was explicitly requested (arg or env var) — same
    semantic as ``/model <name>`` — because the configured default provider may not host it.
    Config-sourced models are the "use my defaults" path and keep the configured provider.
    """
    from hermes_cli.models import detect_provider_for_model

    model_cfg = cfg.get("model") or {}
    env_model = os.getenv("HERMES_INFERENCE_MODEL", "").strip()
    explicit_model = (model or "").strip() or env_model
    choice = _ModelChoice(explicit_model or _configured_model(model_cfg), (provider or "").strip() or None)
    if choice.provider is not None or not explicit_model:
        return choice

    # DIRECT_ALIASES (config.yaml ``model_aliases:``) map a user alias to (model, provider,
    # base_url) for endpoints outside any catalog (local servers, custom proxies, ...).
    try:
        from hermes_cli import model_switch as _ms
        _ms._ensure_direct_aliases()
        direct = _ms.DIRECT_ALIASES.get(explicit_model.strip().lower())
    except Exception:
        direct = None
    if direct is None:
        cfg_provider = ""
        if isinstance(model_cfg, dict):
            cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
        current_provider = cfg_provider or os.getenv("HERMES_INFERENCE_PROVIDER", "").strip().lower() or "auto"
        detected = detect_provider_for_model(explicit_model, current_provider)
        if detected:
            choice.provider, choice.model = detected
        return choice

    choice.model = direct.model
    choice.provider = direct.provider
    # Resolve through the SAME owner the interactive `/model` path uses: passing `direct.provider`
    # with a URL-bearing alias would let a label like `anthropic` keep the alias's base_url yet
    # fall back to the live vendor token — a bearer credential crossing an origin boundary. The
    # helper forces bare `custom` for URL-bearing aliases and carries the alias's own key.
    try:
        choice.provider, choice.api_key = _ms.direct_alias_runtime_request(direct)
    except Exception:
        choice.api_key = None
    if direct.base_url:
        choice.base_url = direct.base_url.rstrip("/")
    return choice


def _run_agent(
    prompt: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: object = None,
    use_config_toolsets: bool = True,
    skills: object = None,
) -> tuple[str, dict]:
    """Build an AIAgent exactly like a normal CLI chat turn, run one conversation, and return
    ``(final_response, run_result)``. Imports are local to keep CLI startup cheap."""
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.tools_config import _get_platform_tools
    from run_agent import AIAgent

    cfg = load_config()
    choice = _resolve_model_and_provider(cfg, model, provider)
    runtime = resolve_runtime_provider(
        requested=choice.provider,
        target_model=choice.model or None,
        explicit_base_url=choice.base_url,
        explicit_api_key=choice.api_key,
    )

    # sorted() gives stable ordering for config-derived sets; explicit values preserve user order.
    #
    # An explicit empty list is the validated `none` sentinel and must be kept as exactly [] -
    # _normalize_toolsets collapses empty input to None, which downstream means "use
    # config/default toolsets" and would silently re-enable tools the caller asked to disable.
    explicit_empty = isinstance(toolsets, (list, tuple)) and len(toolsets) == 0
    if explicit_empty:
        toolsets_list: list[str] | None = []
    else:
        toolsets_list = _normalize_toolsets(toolsets)
    if toolsets_list is None and use_config_toolsets:
        toolsets_list = sorted(_get_platform_tools(cfg, "cli"))

    # Oneshot builds AIAgent directly, bypassing cli.py's MCP background discovery and
    # _init_agent's wait, so the construction-time tool snapshot would miss late MCP servers.
    # Idempotent start + bounded wait with the single-query bound (there is no later turn).
    # Ensure MCP tools are discovered before building the agent. This helper starts discovery if needed
    # (idempotent) and bounded-waits with the larger single-query bound (default 15s) because there is only
    # ONE turn and no between-turns late-binding refresh (#38448).
    #
    # Skipped entirely on the explicit-empty (`none` sentinel) path: with zero enabled toolsets no
    # MCP server can contribute a tool, so spawning discovery would be a pure startup side effect.
    if not explicit_empty:
        from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build

        ensure_mcp_discovery_before_agent_build(logger=logging.getLogger(__name__), single_query=True)

    skills_prompt = _build_preloaded_skills_prompt(skills)

    session_db = _create_session_db_for_oneshot()
    # The try spans agent construction (not just ``chat``) so the store is always closed, even when
    # ``AIAgent(...)`` raises — the one-shot exit path hard-exits via os._exit and skips finalizers.
    agent = None
    try:
        agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_mode=runtime.get("api_mode"),
            model=choice.model,
            enabled_toolsets=toolsets_list,
            quiet_mode=True,
            platform="cli",
            session_db=session_db,
            credential_pool=runtime.get("credential_pool"),
            fallback_model=get_fallback_chain(cfg) or None,
            ephemeral_system_prompt=skills_prompt,
            # The only interactive callback wired: no user sits at a terminal. Sudo prompts gate on
            # HERMES_INTERACTIVE (never set), hook approval via HERMES_ACCEPT_HOOKS=1, dangerous
            # commands via HERMES_YOLO_MODE=1, skill secret capture degrades gracefully.
            clarify_callback=_oneshot_clarify_callback,
        )
        # Belt-and-braces: no streaming display callbacks may bypass our stdout capture.
        agent.suppress_status_output = True
        agent.stream_delta_callback = None
        agent.tool_gen_callback = None

        result = agent.run_conversation(prompt)
        return (result.get("final_response") or "", result)
    finally:
        _close_agent(agent, session_db)


def _quietly(what: str, fn) -> None:
    """Run a cleanup step, logging (never raising) on failure."""
    try:
        fn()
    except Exception:
        logging.debug("oneshot %s failed", what, exc_info=True)


def _linger_for_background_completions() -> None:
    # Linger (bounded) for background processes this turn spawned with notify_on_complete=true BEFORE
    # agent.close(): close() calls process_registry.kill_all(task_id) and the dying parent owns the
    # children's stdout pipes, so exiting now destroys in-flight deliveries — including Bot Mode handoff
    # replies dispatched from a short-lived recipient (#90879).
    from tools.process_registry import process_registry

    process_registry.wait_for_pending_completions(None)


def _close_agent(agent, session_db) -> None:
    """Teardown mirroring gateway/run.py:_cleanup_agent_resources (NOT cli.py:_run_cleanup):
    oneshot has no _active_agent_ref and the hard-exit path skips finalizers."""
    if agent is not None:
        # Linger (bounded) for notify_on_complete background processes BEFORE agent.close():
        # close() kill_all()s the task and the dying parent owns the children's stdout pipes, so
        # exiting now destroys in-flight deliveries (e.g. Bot Mode handoff replies).
        _quietly("background completion wait", _linger_for_background_completions)
        session_messages = getattr(agent, "_session_messages", None)
        memory_args = (session_messages,) if isinstance(session_messages, list) else ()
        _quietly("memory/context cleanup", lambda: agent.shutdown_memory_provider(*memory_args))
        _quietly("agent cleanup", lambda: agent.close())
    # agent.close() ends the session but leaves the connection open; close it to checkpoint the WAL.
    if session_db is not None:
        _quietly("session store cleanup", lambda: session_db.close())


def _oneshot_clarify_callback(question: str, choices=None, multi_select=False) -> str:
    """Clarify is disabled in oneshot mode — tell the agent to pick a default and proceed."""
    if choices:
        what = "subset" if multi_select else "option"
        return (
            f"[oneshot mode: no user available. Pick the best {what} from "
            f"{choices} using your own judgment and continue.]"
        )
    return "[oneshot mode: no user available. Make the most reasonable assumption you can and continue.]"
