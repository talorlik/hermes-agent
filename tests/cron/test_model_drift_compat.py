"""Tests for cron/model_drift_compat.py: fallback resolve_cron_model_drift_defaults.

The compat module provides resolve_cron_model_drift_defaults when upstream removes it from
hermes_cli.config, so gateway cron bootstrap stays operational during merge windows.
"""

import pytest


def test_model_drift_compat_resolves_string_config():
    """String model config (legacy form) is extracted."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {"model": "gpt-4"}
    provider, model = resolve_cron_model_drift_defaults(config, environ={})
    assert model == "gpt-4"
    assert provider == ""


def test_model_drift_compat_resolves_dict_config():
    """Dict model config extracts provider and default model."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {"model": {"provider": "openai", "default": "gpt-4"}}
    provider, model = resolve_cron_model_drift_defaults(config, environ={})
    assert provider == "openai"
    assert model == "gpt-4"


def test_model_drift_compat_dict_config_model_alias():
    """Dict model config recognizes 'model' alias for 'default'."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {"model": {"provider": "anthropic", "model": "claude-3-opus"}}
    provider, model = resolve_cron_model_drift_defaults(config, environ={})
    assert provider == "anthropic"
    assert model == "claude-3-opus"


def test_model_drift_compat_dict_config_name_alias():
    """Dict model config recognizes 'name' alias for 'default'."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {"model": {"provider": "gemini", "name": "gemini-2.0-flash"}}
    provider, model = resolve_cron_model_drift_defaults(config, environ={})
    assert provider == "gemini"
    assert model == "gemini-2.0-flash"


def test_model_drift_compat_dict_config_precedence():
    """Dict model config: 'default' wins over 'model' wins over 'name'."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {
        "model": {
            "provider": "openai",
            "default": "gpt-4",
            "model": "gpt-3.5-turbo",
            "name": "text-davinci-003",
        }
    }
    provider, model = resolve_cron_model_drift_defaults(config, environ={})
    assert model == "gpt-4"


def test_model_drift_compat_falls_back_to_env():
    """HERMES_MODEL from environ is used when config.model is absent."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {}
    provider, model = resolve_cron_model_drift_defaults(
        config, environ={"HERMES_MODEL": "claude-3-sonnet"}
    )
    assert model == "claude-3-sonnet"
    assert provider == ""


def test_model_drift_compat_config_wins_env():
    """Configured model wins over HERMES_MODEL (mirrors scheduler precedence)."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {"model": "gpt-4"}
    provider, model = resolve_cron_model_drift_defaults(
        config, environ={"HERMES_MODEL": "claude-3-sonnet"}
    )
    assert model == "gpt-4"


def test_model_drift_compat_empty_config_empty_env():
    """Empty config and empty env yield empty strings."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    provider, model = resolve_cron_model_drift_defaults({}, environ={})
    assert provider == ""
    assert model == ""


def test_model_drift_compat_strips_whitespace():
    """Provider and model values are stripped."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {"model": {"provider": "  openai  ", "default": "  gpt-4  "}}
    provider, model = resolve_cron_model_drift_defaults(config, environ={})
    assert provider == "openai"
    assert model == "gpt-4"


def test_model_drift_compat_rejects_malformed_values():
    """Non-string model/provider values are treated as empty."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    config = {"model": {"provider": None, "default": 123}}
    provider, model = resolve_cron_model_drift_defaults(config, environ={})
    assert provider == ""
    assert model == ""


def test_model_drift_compat_none_config():
    """None config is handled gracefully."""
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    provider, model = resolve_cron_model_drift_defaults(None, environ={})
    assert provider == ""
    assert model == ""


def test_scheduler_imports_compat_on_missing_upstream():
    """Scheduler falls back to cron.model_drift_compat when hermes_cli.config lacks the function.
    
    This simulates the ImportError recovery path by importing the compat module directly and
    verifying it provides the same interface as the upstream version.
    """
    from cron.model_drift_compat import resolve_cron_model_drift_defaults

    # Verify the function is callable and returns the expected tuple
    config = {"model": {"provider": "openai", "default": "gpt-4"}}
    result = resolve_cron_model_drift_defaults(config, environ={})
    assert isinstance(result, tuple)
    assert len(result) == 2
    provider, model = result
    assert isinstance(provider, str)
    assert isinstance(model, str)
    assert provider == "openai"
    assert model == "gpt-4"


def test_scheduler_import_fallback_behavior(monkeypatch):
    """Verify scheduler import fallback behaves correctly when upstream function is missing.
    
    This test simulates the import path by patching the hermes_cli.config import to raise
    ImportError for resolve_cron_model_drift_defaults, then verifying the scheduler can still
    import successfully via the compat module.
    """
    import sys
    from importlib import reload

    # Save the original hermes_cli.config module state
    original_config = sys.modules.get("hermes_cli.config")

    try:
        # Create a mock hermes_cli.config module that has load_config but not resolve_cron_model_drift_defaults
        class MockConfigModule:
            def load_config(*args, **kwargs):
                return {}

            def load_config_readonly(*args, **kwargs):
                return {}

        # Replace hermes_cli.config with our mock
        mock_config = MockConfigModule()
        sys.modules["hermes_cli.config"] = mock_config

        # Force scheduler to reimport (this would happen naturally in the real import path)
        if "cron.scheduler" in sys.modules:
            del sys.modules["cron.scheduler"]

        # Import scheduler - it should fall back to compat and not raise ImportError
        from cron import scheduler

        # Verify the scheduler has access to resolve_cron_model_drift_defaults
        assert hasattr(scheduler, "resolve_cron_model_drift_defaults")

        # Verify it's the compat version by checking the flag
        assert scheduler._USING_MODEL_DRIFT_COMPAT is True

        # Verify it works
        config = {"model": "gpt-4"}
        provider, model = scheduler.resolve_cron_model_drift_defaults(
            config, environ={}
        )
        assert model == "gpt-4"

    finally:
        # Restore original state
        if original_config is not None:
            sys.modules["hermes_cli.config"] = original_config
        elif "hermes_cli.config" in sys.modules:
            del sys.modules["hermes_cli.config"]

        # Clean up scheduler module so subsequent tests get fresh imports
        if "cron.scheduler" in sys.modules:
            del sys.modules["cron.scheduler"]


def test_model_assignment_text_helper():
    """_model_assignment_text strips strings and rejects non-strings."""
    from cron.model_drift_compat import _model_assignment_text

    assert _model_assignment_text("  gpt-4  ") == "gpt-4"
    assert _model_assignment_text("claude-3-opus") == "claude-3-opus"
    assert _model_assignment_text("") == ""
    assert _model_assignment_text("   ") == ""
    assert _model_assignment_text(None) == ""
    assert _model_assignment_text(123) == ""
    assert _model_assignment_text([]) == ""
    assert _model_assignment_text({}) == ""
