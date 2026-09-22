"""Test that AIAgent initialization sets required instance attributes."""

import pytest


def test_agent_init_sets_image_rejection_tracking_attribute(tmp_path, monkeypatch):
    """AIAgent.__init__ must initialize _image_rejecting_models as an empty set.
    
    The turn_recovery and message_sanitization modules read this attribute to
    track which (provider, model) pairs rejected image content. Missing the
    initialization causes AttributeError at runtime (issue found 2026-09-22).
    """
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = AIAgent(
        api_key="test-key",
        base_url="https://api.example.com/v1",
        provider="custom",
        model="test-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    # The attribute must exist and be an empty set at init.
    assert hasattr(agent, "_image_rejecting_models")
    assert agent._image_rejecting_models == set()
    assert isinstance(agent._image_rejecting_models, set)


def test_agent_init_sets_ascii_payload_flag(tmp_path, monkeypatch):
    """AIAgent.__init__ must initialize _force_ascii_payload as False.
    
    This flag is set to True by turn_recovery when an ASCII-codec error is
    detected. Verifies it starts False so recovery can detect the first error.
    """
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = AIAgent(
        api_key="test-key",
        base_url="https://api.example.com/v1",
        provider="custom",
        model="test-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    assert hasattr(agent, "_force_ascii_payload")
    assert agent._force_ascii_payload is False
