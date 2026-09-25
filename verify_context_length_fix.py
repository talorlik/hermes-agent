#!/usr/bin/env python3
"""
Verification script for the context length helper functions fix.

Run this after deploying the fix to ensure model switching will work correctly.
"""

import sys
from types import SimpleNamespace


def main():
    print("Verifying context length helper functions fix...")
    print()
    
    # Test 1: Import the functions
    print("1. Testing imports from agent.agent_init...")
    try:
        from agent.agent_init import (
            set_config_context_length,
            config_context_length_for_runtime,
        )
        print("   ✓ Successfully imported set_config_context_length")
        print("   ✓ Successfully imported config_context_length_for_runtime")
    except ImportError as e:
        print(f"   ✗ Import failed: {e}")
        return 1
    
    # Test 2: Verify the functions are callable
    print()
    print("2. Testing function signatures...")
    try:
        # Create a minimal mock agent
        agent = SimpleNamespace(_config_context_length=None, context_compressor=None)
        
        # Test set_config_context_length
        set_config_context_length(agent, 300_000)
        if agent._config_context_length == 300_000:
            print("   ✓ set_config_context_length works correctly")
        else:
            print("   ✗ set_config_context_length did not update agent correctly")
            return 1
        
        # Test config_context_length_for_runtime with None return (no config)
        result = config_context_length_for_runtime(agent, config={})
        if result is None:
            print("   ✓ config_context_length_for_runtime works correctly")
        else:
            print("   ✗ config_context_length_for_runtime returned unexpected result")
            return 1
            
    except Exception as e:
        print(f"   ✗ Function test failed: {e}")
        return 1
    
    # Test 3: Verify call sites can import
    print()
    print("3. Testing imports from call sites...")
    try:
        # Check tui_gateway/session_compression.py
        import ast
        with open('tui_gateway/session_compression.py', 'r') as f:
            tree = ast.parse(f.read())
            found = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == 'agent.agent_init':
                    names = [alias.name for alias in node.names]
                    if 'set_config_context_length' in names and 'config_context_length_for_runtime' in names:
                        found = True
            if found:
                print("   ✓ tui_gateway/session_compression.py imports correctly")
            else:
                print("   ✗ tui_gateway/session_compression.py missing imports")
                return 1
        
        # Check agent/agent_runtime_helpers.py
        with open('agent/agent_runtime_helpers.py', 'r') as f:
            tree = ast.parse(f.read())
            found = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == 'agent.agent_init':
                    names = [alias.name for alias in node.names]
                    if 'set_config_context_length' in names:
                        found = True
            if found:
                print("   ✓ agent/agent_runtime_helpers.py imports correctly")
            else:
                print("   ✗ agent/agent_runtime_helpers.py missing import")
                return 1
                
    except Exception as e:
        print(f"   ✗ Call site verification failed: {e}")
        return 1
    
    print()
    print("=" * 60)
    print("✓ All verification tests passed!")
    print("=" * 60)
    print()
    print("The fix has been successfully applied. Model switching should")
    print("now work without ImportError.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
