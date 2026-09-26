"""Tests for parameter signature parity between AIAgent.__init__ and init_agent.

Ensures all parameters accepted by AIAgent.__init__ are also accepted by init_agent,
preventing "unexpected keyword argument" errors when callers pass parameters.
"""

import inspect
import pytest


def test_aiagent_init_and_init_agent_signature_parity():
    """AIAgent.__init__ and init_agent must accept the same parameters (except tool_delay).
    
    This is a critical integration test that prevents fork-drift issues where
    parameters are removed from init_agent but still passed by AIAgent.__init__
    or other callers.
    
    tool_delay is explicitly excluded: it's deprecated, accepted by AIAgent.__init__
    for compatibility, but immediately stripped before forwarding to init_agent.
    """
    from run_agent import AIAgent
    from agent.agent_init import init_agent
    
    aiagent_sig = inspect.signature(AIAgent.__init__)
    init_agent_sig = inspect.signature(init_agent)
    
    # Extract parameter names (excluding 'self' for __init__ and 'agent' for init_agent)
    aiagent_params = set(aiagent_sig.parameters.keys()) - {'self'}
    init_agent_params = set(init_agent_sig.parameters.keys()) - {'agent'}
    
    # tool_delay is deprecated and explicitly handled in AIAgent.__init__
    # It's stripped before forwarding to init_agent, so it shouldn't be in init_agent
    expected_excluded = {'tool_delay'}
    aiagent_params_normalized = aiagent_params - expected_excluded
    
    # Check for missing parameters
    missing = aiagent_params_normalized - init_agent_params
    assert not missing, (
        f"init_agent is missing {len(missing)} parameter(s) that AIAgent.__init__ passes: "
        f"{sorted(missing)}. These must be added to init_agent signature."
    )
    
    # Check for extra parameters (informational, not a failure)
    extra = init_agent_params - aiagent_params_normalized
    if extra:
        # This is OK - init_agent might have parameters that aren't in AIAgent.__init__
        # but are used internally. Just log it.
        print(f"Note: init_agent has {len(extra)} extra parameter(s) not in AIAgent.__init__: {sorted(extra)}")
    
    print(f"✓ Signature parity verified: {len(aiagent_params_normalized)} parameters match")


def test_tool_result_metadata_callback_accepted():
    """tool_result_metadata_callback must be accepted by init_agent.
    
    Regression test for the specific error that triggered this comprehensive fix.
    """
    from agent.agent_init import init_agent
    import inspect
    
    sig = inspect.signature(init_agent)
    assert 'tool_result_metadata_callback' in sig.parameters, \
        "init_agent must accept tool_result_metadata_callback parameter"
    
    param = sig.parameters['tool_result_metadata_callback']
    assert param.default is None, \
        "tool_result_metadata_callback should default to None"


def test_tool_result_metadata_callback_in_callback_params():
    """tool_result_metadata_callback must be in _CALLBACK_PARAMS.
    
    _CALLBACK_PARAMS controls which callbacks are stored as agent attributes.
    """
    from agent.agent_init import _CALLBACK_PARAMS
    
    assert 'tool_result_metadata_callback' in _CALLBACK_PARAMS, \
        "tool_result_metadata_callback must be in _CALLBACK_PARAMS tuple"


def test_all_callback_params_in_init_agent_signature():
    """Every callback in _CALLBACK_PARAMS must be in init_agent signature.
    
    Ensures callbacks are properly registered for attribute storage.
    """
    from agent.agent_init import _CALLBACK_PARAMS, init_agent
    import inspect
    
    sig = inspect.signature(init_agent)
    sig_params = set(sig.parameters.keys())
    
    missing_callbacks = []
    for callback in _CALLBACK_PARAMS:
        if callback not in sig_params:
            missing_callbacks.append(callback)
    
    assert not missing_callbacks, (
        f"init_agent signature is missing {len(missing_callbacks)} callback(s) from _CALLBACK_PARAMS: "
        f"{missing_callbacks}"
    )


def test_cli_chat_can_create_agent_with_all_params():
    """Simulate CLI chat creating AIAgent to ensure all parameters work.
    
    This is the call path that was failing before the fix.
    """
    cfg = {"memory": {}, "agent": {}}
    
    from unittest.mock import patch
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.model_metadata.get_model_context_length", return_value=200_000),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        from run_agent import AIAgent
        
        # This is the pattern that was failing with:
        # init_agent() got an unexpected keyword argument 'tool_result_metadata_callback'
        try:
            agent = AIAgent(
                api_key="test-key",
                base_url="https://test.com/v1",
                quiet_mode=True,
                skip_context_files=True,
                tool_result_metadata_callback=lambda tc_id, name, args, result: {},
            )
            success = True
        except TypeError as e:
            if "unexpected keyword argument" in str(e):
                success = False
                pytest.fail(f"AIAgent rejected parameter: {e}")
            else:
                raise
    
    assert success, "AIAgent should accept all parameters including tool_result_metadata_callback"


def test_parameter_counts_match():
    """Verify expected parameter counts for regression tracking.
    
    If this test fails after a legitimate parameter addition/removal,
    update the expected counts and ensure signature parity is maintained.
    """
    from run_agent import AIAgent
    from agent.agent_init import init_agent
    import inspect
    
    aiagent_sig = inspect.signature(AIAgent.__init__)
    init_agent_sig = inspect.signature(init_agent)
    
    # Exclude 'self' and 'agent' from counts
    aiagent_count = len(aiagent_sig.parameters) - 1
    init_agent_count = len(init_agent_sig.parameters) - 1
    
    # AIAgent has tool_delay (deprecated), init_agent doesn't
    # So init_agent should have exactly 1 fewer parameter
    expected_difference = 1
    actual_difference = aiagent_count - init_agent_count
    
    assert actual_difference == expected_difference, (
        f"Parameter count mismatch: AIAgent.__init__ has {aiagent_count} parameters, "
        f"init_agent has {init_agent_count} parameters. Expected difference: {expected_difference}, "
        f"actual: {actual_difference}. Check for missing or extra parameters."
    )


def test_critical_parameters_present():
    """Verify critical parameters that have caused fork-drift issues are present.
    
    These are parameters that were previously removed and caused runtime errors.
    """
    from agent.agent_init import init_agent
    import inspect
    
    sig = inspect.signature(init_agent)
    params = sig.parameters
    
    critical_params = [
        'side_agent',  # Issue #1: missing after upstream sync
        'memory_manager',  # Issue #2: missing after G-ONESHOT-ISOLATION
        'tool_result_metadata_callback',  # Issue #3: missing after G-ONESHOT-ISOLATION
    ]
    
    missing = []
    for param in critical_params:
        if param not in params:
            missing.append(param)
    
    assert not missing, (
        f"init_agent is missing {len(missing)} critical parameter(s) that previously "
        f"caused fork-drift issues: {missing}"
    )
    
    # All should default to None
    for param in critical_params:
        assert params[param].default is None, (
            f"Parameter {param} should default to None, got {params[param].default}"
        )
