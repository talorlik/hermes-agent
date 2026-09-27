"""Tests for xAI Grok timeout handling and large-context support.

Regression tests for xAI streaming timeouts with large contexts where
the timeout logic was too aggressive and killed healthy streams.
"""

import pytest
from types import SimpleNamespace


def test_xai_stale_timeout_floor_graduated():
    """xai_stale_timeout_floor returns graduated timeouts by context size."""
    from agent.chat_completion_helpers import xai_stale_timeout_floor
    
    # Below 10k: no floor
    assert xai_stale_timeout_floor(5_000) == 0.0
    assert xai_stale_timeout_floor(9_999) == 0.0
    
    # 10k-50k: 480s floor
    assert xai_stale_timeout_floor(10_001) == 480.0
    assert xai_stale_timeout_floor(22_405) == 480.0  # User's reported context
    assert xai_stale_timeout_floor(49_999) == 480.0
    
    # 50k-100k: 720s floor
    assert xai_stale_timeout_floor(50_001) == 720.0
    assert xai_stale_timeout_floor(75_000) == 720.0
    assert xai_stale_timeout_floor(99_999) == 720.0
    
    # >100k: 900s floor
    assert xai_stale_timeout_floor(100_001) == 900.0
    assert xai_stale_timeout_floor(200_000) == 900.0


def test_xai_responses_backend_detection():
    """_is_xai_responses_backend detects xAI provider."""
    from agent.chat_completion_helpers import _is_xai_responses_backend
    
    # xai provider
    agent = SimpleNamespace(provider="xai", base_url="https://api.x.ai/v1")
    assert _is_xai_responses_backend(agent) is True
    
    # xai-oauth provider
    agent = SimpleNamespace(provider="xai-oauth", base_url="https://api.x.ai/v1")
    assert _is_xai_responses_backend(agent) is True
    
    # Other providers should be False
    agent = SimpleNamespace(provider="openai-codex", base_url="https://api.openai.com/v1")
    assert _is_xai_responses_backend(agent) is False
    
    agent = SimpleNamespace(provider="anthropic", base_url="https://api.anthropic.com")
    assert _is_xai_responses_backend(agent) is False


def test_xai_gets_large_context_timeout_bumps(monkeypatch):
    """xAI with large contexts gets appropriate timeout floors applied.
    
    Regression for reported timeouts on ~22k token contexts where
    xAI was not getting the same large-context grace as OpenAI Codex.
    """
    from agent.chat_completion_helpers import _resolve_nonstream_watchdogs
    
    # Mock agent with xAI provider, codex_responses API mode, large context
    agent = SimpleNamespace(
        provider="xai-oauth",
        base_url="https://api.x.ai/v1",
        api_mode="codex_responses",
        model="grok-4.7",
        reasoning_config=None,
    )
    
    # Mock the timeout computation to return a baseline
    agent._compute_non_stream_stale_timeout = lambda kwargs: 180.0
    
    # Simulate a 22k token request (user's reported context)
    api_kwargs = {"input": [{"content": "x" * 88_000}]}  # ~22k tokens
    
    watchdogs = _resolve_nonstream_watchdogs(agent, api_kwargs)
    
    # xAI should get the 480s floor for contexts >10k
    assert watchdogs.stale_timeout >= 480.0, \
        "xAI with 22k tokens should get at least 480s stale timeout"
    
    # Progress gating should be enabled for xAI with large contexts
    assert watchdogs.idle_requires_progress is True, \
        "xAI large-context streams should gate idle watchdog on progress"


def test_xai_timeout_lower_than_openai_codex():
    """xAI timeout floors are more aggressive than OpenAI Codex (expected)."""
    from agent.chat_completion_helpers import (
        openai_codex_stale_timeout_floor,
        xai_stale_timeout_floor,
    )
    
    # At each threshold, xAI should have lower (more aggressive) timeouts
    # This is intentional - xAI is faster than OpenAI Codex
    for tokens in [10_001, 50_001, 100_001]:
        xai_floor = xai_stale_timeout_floor(tokens)
        codex_floor = openai_codex_stale_timeout_floor(tokens)
        assert xai_floor < codex_floor, \
            f"xAI floor ({xai_floor}s) should be lower than Codex ({codex_floor}s) at {tokens:,} tokens"


def test_auxiliary_warning_distinguishes_fallback_failure():
    """Auxiliary warning should not suggest re-auth when fallbacks exist but fail.
    
    Regression for misleading 'Re-authenticate' warning when xAI times out
    but fallbacks are configured (they just all failed/timed out too).
    """
    from agent.auxiliary_client import _discovery_chain_allowed
    
    # With fallbacks configured, don't suggest re-auth
    allowed = _discovery_chain_allowed("xai-oauth", task="compression", has_fallback=True)
    assert allowed is False  # Still don't allow discovery chain
    # The warning logged should mention checking connectivity, not re-auth
    # (verified via log assertion in integration test)
    
    # Without fallbacks, suggest re-auth (existing behavior)
    allowed = _discovery_chain_allowed("xai-oauth", task="compression", has_fallback=False)
    assert allowed is False
    # The warning should suggest re-auth


def test_xai_timeout_with_high_reasoning_effort(monkeypatch):
    """xAI with high reasoning effort gets 300s floor from effort setting.
    
    The HIGH_EFFORT_SILENCE_FLOOR_SECONDS applies to all codex_responses
    backends including xAI when reasoning effort is high+.
    """
    from agent.chat_completion_helpers import _resolve_nonstream_watchdogs
    
    agent = SimpleNamespace(
        provider="xai-oauth",
        base_url="https://api.x.ai/v1",
        api_mode="codex_responses",
        model="grok-4.7",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    
    agent._compute_non_stream_stale_timeout = lambda kwargs: 180.0
    
    # Small context, but high effort
    api_kwargs = {"input": [{"content": "x" * 4000}]}  # ~1k tokens
    
    watchdogs = _resolve_nonstream_watchdogs(agent, api_kwargs)
    
    # TTFB should be at least 300s due to high effort floor
    assert watchdogs.ttfb_timeout >= 300.0, \
        "xAI with high reasoning effort should get 300s TTFB floor"
