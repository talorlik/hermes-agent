"""Tests for SSL/TLS certificate trust store initialization.

Ensures agent_init.py correctly imports and calls the TLS trust store
initialization, preventing "No module named 'agent.ssl_guard'" errors.
"""

import sys
import pytest
from unittest.mock import patch, MagicMock


def test_ssl_verify_module_exists():
    """agent.ssl_verify module must exist and be importable."""
    try:
        import agent.ssl_verify
        assert agent.ssl_verify is not None
    except ImportError as e:
        pytest.fail(f"agent.ssl_verify module not importable: {e}")


def test_install_truststore_function_exists():
    """agent.ssl_verify must provide install_truststore function."""
    from agent.ssl_verify import install_truststore
    
    assert callable(install_truststore), \
        "install_truststore must be a callable function"


def test_ssl_guard_module_does_not_exist():
    """agent.ssl_guard module must NOT exist (it was removed in TLS refactor).
    
    Regression test for the error:
    "No module named 'agent.ssl_guard'"
    
    The module was deleted in commit 2f20ceb6f7 (refactor(tls): trust the OS
    certificate store) but was incorrectly referenced again after fork syncs.
    """
    # Temporarily remove agent.ssl_guard from sys.modules if it was somehow imported
    ssl_guard_module = sys.modules.pop('agent.ssl_guard', None)
    
    try:
        with pytest.raises(ModuleNotFoundError, match=r"No module named 'agent\.ssl_guard'"):
            import agent.ssl_guard
    finally:
        # Restore if it was there (it shouldn't be)
        if ssl_guard_module is not None:
            sys.modules['agent.ssl_guard'] = ssl_guard_module


def test_agent_init_imports_correct_ssl_module():
    """agent_init.py must import from agent.ssl_verify, not agent.ssl_guard.
    
    This test reads the source code to verify the import statement is correct.
    It's a simple but effective guard against fork-drift regressions.
    """
    with open('agent/agent_init.py', 'r') as f:
        content = f.read()
    
    # The correct import (after TLS refactor)
    assert 'from agent.ssl_verify import install_truststore' in content, \
        "agent_init.py must import from agent.ssl_verify, not agent.ssl_guard"
    
    # The incorrect import (before TLS refactor, should NOT be present)
    assert 'from agent.ssl_guard import' not in content, \
        "agent_init.py must NOT import from agent.ssl_guard (module was deleted)"
    
    # Verify the function is called
    assert 'install_truststore()' in content, \
        "agent_init.py must call install_truststore()"


def test_install_truststore_can_be_called():
    """install_truststore() must be callable without errors in test environment.
    
    This is a smoke test that the function signature hasn't changed.
    """
    from agent.ssl_verify import install_truststore
    
    # Mock the truststore module since it might not be available in test env
    with patch('agent.ssl_verify.truststore', create=True) as mock_truststore:
        mock_truststore.extract_from_ssl = MagicMock()
        mock_truststore.inject_into_ssl = MagicMock()
        
        # Should not raise
        try:
            result = install_truststore()
            # The function returns a bool indicating if truststore was installed
            assert isinstance(result, bool) or result is None, \
                "install_truststore should return bool or None"
        except Exception as e:
            # If it fails, it should be due to missing truststore module, not signature
            if "truststore" not in str(e).lower():
                pytest.fail(f"install_truststore() failed unexpectedly: {e}")


def test_agent_init_ssl_integration():
    """Test that _init_openai_client calls install_truststore correctly.
    
    Integration test simulating the agent initialization path that was broken.
    """
    # Mock dependencies to test just the SSL initialization path
    with (
        patch('agent.agent_init.build_anthropic_client'),
        patch('agent.agent_init.cfg_get', return_value=None),
        patch('agent.ssl_verify.install_truststore', return_value=True) as mock_install,
        patch('agent.agent_init.AIAgent._create_openai_client'),
        patch('model_tools.get_tool_definitions', return_value=[]),
        patch('model_tools.check_toolset_requirements', return_value={}),
    ):
        from agent.agent_init import _init_openai_client
        
        # Create a minimal mock agent
        agent = MagicMock()
        agent.quiet_mode = True
        agent.provider = "openai"
        agent.base_url = "https://api.openai.com/v1"
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        
        # Call the function that should trigger SSL initialization
        client_kwargs = {
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
        }
        
        try:
            _init_openai_client(agent, client_kwargs, _provider_timeout=30.0)
            
            # Verify install_truststore was called (not verify_ca_bundle)
            assert mock_install.called, \
                "install_truststore should be called during OpenAI client init"
        except ImportError as e:
            if 'ssl_guard' in str(e):
                pytest.fail(
                    "agent_init tried to import from agent.ssl_guard! "
                    "This module was deleted. Use agent.ssl_verify instead."
                )
            raise


def test_no_verify_ca_bundle_references():
    """Ensure old verify_ca_bundle references are gone from agent_init.py.
    
    The old SSL guard functions were:
    - verify_ca_bundle()
    - verify_ca_bundle_with_fallback()
    
    These should NOT be referenced in agent_init.py anymore.
    """
    with open('agent/agent_init.py', 'r') as f:
        content = f.read()
    
    assert 'verify_ca_bundle' not in content, \
        "agent_init.py must not reference verify_ca_bundle (old SSL guard function)"
    
    assert 'verify_ca_bundle_with_fallback' not in content, \
        "agent_init.py must not reference verify_ca_bundle_with_fallback (old SSL guard function)"
