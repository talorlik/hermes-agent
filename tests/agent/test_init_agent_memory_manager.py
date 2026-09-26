"""Tests for memory_manager parameter acceptance in init_agent.

Regression test for missing memory_manager parameter that broke CLI startup
and gateway API server memory session reuse after G-ONESHOT-ISOLATION refactor.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest


class MockMemoryProvider:
    """Minimal memory provider for testing manager handoff."""
    
    name = "mock-provider"
    
    def __init__(self):
        self.init_called = False
        self.session_id = None
        
    def is_available(self):
        return True
        
    def initialize(self, session_id, **kwargs):
        self.init_called = True
        self.session_id = session_id
        
    def get_tool_schemas(self):
        return []
        
    def shutdown(self):
        pass


def test_init_agent_accepts_memory_manager_parameter():
    """init_agent must accept memory_manager parameter (regression for fork-drift)."""
    from agent.agent_init import init_agent
    import inspect
    
    sig = inspect.signature(init_agent)
    assert 'memory_manager' in sig.parameters, \
        "init_agent must accept memory_manager parameter"
    
    param = sig.parameters['memory_manager']
    assert param.default is None, \
        "memory_manager parameter should default to None"


def test_aiagent_accepts_memory_manager_parameter():
    """AIAgent.__init__ must accept memory_manager parameter."""
    from run_agent import AIAgent
    import inspect
    
    sig = inspect.signature(AIAgent.__init__)
    assert 'memory_manager' in sig.parameters, \
        "AIAgent.__init__ must accept memory_manager parameter"


def test_memory_manager_passed_to_init_memory():
    """Verify memory_manager is forwarded to _init_memory."""
    from agent.agent_init import init_agent
    from unittest.mock import MagicMock
    
    # Create a mock agent and memory manager
    agent = MagicMock()
    agent.enabled_toolsets = []
    agent.disabled_toolsets = []
    
    mock_manager = MagicMock()
    
    # Patch _init_memory to verify it receives memory_manager
    with patch('agent.agent_init._init_memory') as mock_init_memory:
        # Mock other functions that init_agent calls
        with (
            patch('agent.agent_init._init_routing'),
            patch('agent.agent_init._init_callbacks'),
            patch('agent.agent_init._build_client'),
            patch('agent.agent_init._init_tool_definitions'),
            patch('agent.agent_init._init_session'),
            patch('agent.agent_init._apply_display_config'),
            patch('agent.agent_init._apply_agent_section'),
            patch('agent.agent_init._init_context_engine'),
            patch('agent.agent_init._init_compression'),
            patch('agent.agent_init._init_prompt_cache_config'),
            patch('agent.agent_init._init_turn_state'),
            patch('agent.agent_init._init_credential_pool'),
            patch('agent.agent_init._apply_reasoning_config'),
            patch('agent.agent_init._init_prefill'),
            patch('agent.agent_init._build_activity_provenance'),
            patch('hermes_cli.config.load_config', return_value={}),
            patch('hermes_cli.config.load_config_readonly', return_value={}),
        ):
            # Call init_agent with memory_manager
            init_agent(
                agent,
                base_url="https://test.com",
                api_key="test",
                skip_memory=False,
                memory_manager=mock_manager,
            )
        
        # Verify _init_memory was called with memory_manager
        mock_init_memory.assert_called_once()
        call_kwargs = mock_init_memory.call_args[1]
        assert 'memory_manager' in call_kwargs
        assert call_kwargs['memory_manager'] is mock_manager


def test_pre_initialized_memory_manager_skips_provider_load():
    """When memory_manager is provided, skip loading new provider (API server pattern)."""
    provider = MockMemoryProvider()
    cfg = {"memory": {"provider": "mock-provider"}, "agent": {}}
    
    # Create a pre-initialized manager
    from agent.memory_manager import MemoryManager
    pre_initialized_manager = MemoryManager()
    pre_initialized_manager.add_provider(provider)
    provider.init_called = False  # Reset for test
    
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("plugins.memory.load_memory_provider") as load_provider_mock,
        patch("agent.model_metadata.get_model_context_length", return_value=200_000),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        from run_agent import AIAgent
        
        # Pass pre-initialized manager
        agent = AIAgent(
            api_key="test-key",
            base_url="https://test.com/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            memory_manager=pre_initialized_manager,
        )
    
    # Verify the manager was reused
    assert agent._memory_manager is pre_initialized_manager
    # Provider should NOT be loaded again
    load_provider_mock.assert_not_called()
    # Provider should NOT be initialized again
    assert not provider.init_called


def test_memory_manager_none_loads_provider_normally():
    """When memory_manager=None, normal provider loading occurs."""
    provider = MockMemoryProvider()
    cfg = {"memory": {"provider": "mock-provider"}, "agent": {}}
    
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider) as load_provider_mock,
        patch("agent.model_metadata.get_model_context_length", return_value=200_000),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        from run_agent import AIAgent
        
        # Do NOT pass memory_manager (default None)
        agent = AIAgent(
            api_key="test-key",
            base_url="https://test.com/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="test-session",
        )
    
    # Provider should be loaded normally
    load_provider_mock.assert_called_once()
    assert agent._memory_manager is not None
    # Provider should be initialized
    assert provider.init_called
    assert provider.session_id == "test-session"


def test_memory_manager_with_skip_memory_true_does_not_reuse():
    """When skip_memory=True, even a provided manager is ignored."""
    from agent.memory_manager import MemoryManager
    pre_initialized_manager = MemoryManager()
    
    cfg = {"memory": {}, "agent": {}}
    
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.model_metadata.get_model_context_length", return_value=200_000),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        from run_agent import AIAgent
        
        agent = AIAgent(
            api_key="test-key",
            base_url="https://test.com/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,  # Skip memory entirely
            memory_manager=pre_initialized_manager,  # But provide manager
        )
    
    # Manager should NOT be used when skip_memory=True
    assert agent._memory_manager is None


def test_cli_chat_can_pass_memory_manager():
    """Simulate CLI chat path to ensure memory_manager can be passed."""
    cfg = {"memory": {}, "agent": {}}
    
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.model_metadata.get_model_context_length", return_value=200_000),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        from run_agent import AIAgent
        
        # This is the pattern that was failing:
        # CLI or gateway creates AIAgent with memory_manager=...
        try:
            agent = AIAgent(
                api_key="test-key",
                base_url="https://test.com/v1",
                quiet_mode=True,
                skip_context_files=True,
                memory_manager=None,  # Explicitly pass None
            )
            success = True
        except TypeError as e:
            if "memory_manager" in str(e):
                success = False
            else:
                raise
    
    assert success, "AIAgent should accept memory_manager parameter"
