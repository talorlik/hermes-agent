"""Test set_config_context_length and config_context_length_for_runtime helpers.

These functions were added to ensure the model.context_length pin stays synchronized across
agent._config_context_length and context_compressor._config_context_length. Regression test
for the ImportError that broke model switching when these helpers were missing.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.agent_init import config_context_length_for_runtime, set_config_context_length
from agent.context_compressor import ContextCompressor


def test_set_config_context_length_updates_agent_and_compressor():
    """set_config_context_length must write to both cached copies of the pin."""
    compressor = ContextCompressor(model="test-model", threshold_percent=0.50, quiet_mode=True)
    agent = SimpleNamespace(_config_context_length=None, context_compressor=compressor)

    set_config_context_length(agent, 300_000)

    assert agent._config_context_length == 300_000
    assert compressor._config_context_length == 300_000


def test_set_config_context_length_clears_both_pins():
    """Clearing the pin (None) must clear both copies."""
    compressor = ContextCompressor(
        model="test-model", threshold_percent=0.50, config_context_length=250_000, quiet_mode=True
    )
    agent = SimpleNamespace(_config_context_length=250_000, context_compressor=compressor)

    set_config_context_length(agent, None)

    assert agent._config_context_length is None
    assert compressor._config_context_length is None


def test_set_config_context_length_works_without_compressor():
    """Agents without a compressor should not raise."""
    agent = SimpleNamespace(_config_context_length=None, context_compressor=None)

    set_config_context_length(agent, 200_000)

    assert agent._config_context_length == 200_000


def test_config_context_length_for_runtime_returns_pin_for_configured_route():
    """config_context_length_for_runtime re-reads the pin when the agent matches the default route."""
    agent = SimpleNamespace(
        model="model-a",
        provider="custom:acme",
        base_url="http://127.0.0.1:8123/v1",
    )
    cfg = {
        "model": {
            "default": "model-a",
            "provider": "custom:acme",
            "base_url": "http://127.0.0.1:8123/v1",
            "context_length": 400_000,
        },
        "custom_providers": [{"name": "acme", "base_url": "http://127.0.0.1:8123/v1", "models": {}}],
    }

    with patch("hermes_cli.config.load_config", return_value=cfg):
        result = config_context_length_for_runtime(agent)

    assert result == 400_000


def test_config_context_length_for_runtime_returns_none_for_other_route():
    """The pin is scoped to the configured default route; other runtimes get None."""
    agent = SimpleNamespace(
        model="other-model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg = {
        "model": {
            "default": "model-a",
            "provider": "custom:acme",
            "base_url": "http://127.0.0.1:8123/v1",
            "context_length": 400_000,
        },
        "custom_providers": [{"name": "acme", "base_url": "http://127.0.0.1:8123/v1", "models": {}}],
    }

    with patch("hermes_cli.config.load_config", return_value=cfg):
        result = config_context_length_for_runtime(agent)

    assert result is None


def test_config_context_length_for_runtime_returns_none_when_not_configured():
    """No pin configured means None."""
    agent = SimpleNamespace(
        model="model-a",
        provider="openrouter",
        base_url="https://api.openai.com/v1",
    )
    cfg = {"model": {"default": "model-a", "provider": "openrouter"}}

    with patch("hermes_cli.config.load_config", return_value=cfg):
        result = config_context_length_for_runtime(agent)

    assert result is None


def test_config_context_length_for_runtime_accepts_explicit_config():
    """Passing config directly avoids load_config."""
    agent = SimpleNamespace(
        model="model-a",
        provider="custom:acme",
        base_url="http://127.0.0.1:8123/v1",
    )
    cfg = {
        "model": {
            "default": "model-a",
            "provider": "custom:acme",
            "base_url": "http://127.0.0.1:8123/v1",
            "context_length": 350_000,
        },
        "custom_providers": [{"name": "acme", "base_url": "http://127.0.0.1:8123/v1", "models": {}}],
    }

    result = config_context_length_for_runtime(agent, config=cfg)

    assert result == 350_000
