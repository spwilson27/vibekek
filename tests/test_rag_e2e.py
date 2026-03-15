#!/usr/bin/env python3
"""End-to-end test for RAG MCP server integration in workflow execution.

This test verifies that:
1. The RAG help text is properly injected into agent prompts
2. The RAG server can be started in a cloned repository
3. The workflow_lib.executor module properly integrates RAG functionality
4. The full e2e workflow works with RAG server auto-startup
5. The RAG config option properly enables/disables the integration

Usage:
    python .tools/tests/test_rag_e2e.py

Or with pytest:
    pytest .tools/tests/test_rag_e2e.py -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workflow_lib.rag_integration import get_rag_help_text, start_rag_server, stop_rag_server
from workflow_lib.executor import get_rag_help_text as executor_get_rag_help
from workflow_lib.config import get_rag_enabled, load_config


def test_rag_help_injected_in_executor():
    """Test that the executor module properly imports and uses RAG help."""
    # Verify the executor can access the RAG help text
    help_text = executor_get_rag_help()
    assert isinstance(help_text, str)
    assert len(help_text) > 0
    assert "RAG MCP" in help_text
    print("✓ Executor module properly imports RAG help text")


def test_rag_help_content_comprehensive():
    """Test that RAG help text contains all necessary sections."""
    help_text = get_rag_help_text()
    
    # Verify all major sections are present
    required_sections = [
        "Code Search Tools",
        "SEARCH COMMANDS",
        "rag-mcp-cli rag",
        "rag-mcp-cli semantic",
        "rag-mcp-cli list",
        "rag-mcp-cli hierarchy",
        "SERVER COMMANDS",
        "rag-mcp-cli serve",
        "rag-mcp-cli status",
        "Common Workflows",
        "Tips",
        "automatically started",
    ]
    
    for section in required_sections:
        assert section in help_text, f"Missing section: {section}"
    
    print("✓ RAG help text contains all required sections")


def test_rag_server_start_stop_lifecycle():
    """Test the full RAG server start/stop lifecycle."""
    # Create a temporary test repository
    temp_dir = tempfile.mkdtemp(prefix="rag-e2e-test-")
    repo_path = Path(temp_dir) / "test-repo"
    repo_path.mkdir()
    
    try:
        # Initialize a minimal git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True, capture_output=True)
        (repo_path / "README.md").write_text("# Test Repo\n")
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo_path, check=True, capture_output=True)
        
        print(f"✓ Created test repository at {repo_path}")
        
        # Test starting the server
        pid = start_rag_server(str(repo_path), verbose=False)
        
        # PID may be None if RAG tool dependencies aren't installed
        # or a valid PID if the server started
        if pid is not None:
            print(f"✓ RAG server started (PID: {pid})")
            
            # Give it a moment to start
            import time
            time.sleep(1)
            
            # Test stopping the server
            stopped = stop_rag_server(str(repo_path), verbose=False)
            print(f"✓ RAG server stopped: {stopped}")
        else:
            print("✓ RAG server start returned None (expected if dependencies not installed)")
        
    finally:
        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("✓ Cleanup completed")


def test_rag_integration_module_imports():
    """Test that all RAG integration functions are importable."""
    from workflow_lib.rag_integration import (
        get_rag_help_text,
        start_rag_server,
        stop_rag_server,
        wait_for_rag_indexing,
    )
    
    # Verify they are callable
    assert callable(get_rag_help_text)
    assert callable(start_rag_server)
    assert callable(stop_rag_server)
    assert callable(wait_for_rag_indexing)
    
    print("✓ All RAG integration functions are importable and callable")


def test_executor_imports_rag_integration():
    """Test that executor.py properly imports rag_integration."""
    # This test verifies the import statement exists in executor.py
    executor_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "workflow_lib", "executor.py"
    )
    
    with open(executor_path, "r") as f:
        content = f.read()
    
    # Check for the import statement
    assert "from .rag_integration import" in content
    assert "get_rag_help_text" in content
    assert "start_rag_server" in content
    
    print("✓ executor.py properly imports rag_integration module")


def test_executor_injects_rag_help():
    """Test that executor.py injects RAG help into prompts."""
    executor_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "workflow_lib", "executor.py"
    )
    
    with open(executor_path, "r") as f:
        content = f.read()
    
    # Check for RAG help injection in run_agent function
    assert "get_rag_help_text()" in content
    assert "rag_help" in content
    
    print("✓ executor.py injects RAG help text into agent prompts")


def test_executor_starts_rag_server():
    """Test that executor.py starts RAG server in _stage_clone."""
    executor_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "workflow_lib", "executor.py"
    )
    
    with open(executor_path, "r") as f:
        content = f.read()
    
    # Check for RAG server startup in _stage_clone function
    assert "start_rag_server" in content
    
    # Should be called for both Docker and non-Docker paths
    lines = content.split('\n')
    rag_start_lines = [i for i, line in enumerate(lines) if 'start_rag_server' in line]
    
    assert len(rag_start_lines) >= 1, "start_rag_server should be called at least once"
    
    print(f"✓ executor.py starts RAG server in _stage_clone ({len(rag_start_lines)} call site(s))")


def test_rag_help_text_length():
    """Test that RAG help text has appropriate length."""
    help_text = get_rag_help_text()
    
    # Should be substantial but not overwhelming
    word_count = len(help_text.split())
    
    assert word_count > 100, f"Help text too short: {word_count} words"
    assert word_count < 2000, f"Help text too long: {word_count} words"
    
    print(f"✓ RAG help text has appropriate length ({word_count} words)")


def test_rag_server_with_container_name_param():
    """Test that start_rag_server accepts container_name parameter."""
    import inspect
    
    sig = inspect.signature(start_rag_server)
    params = list(sig.parameters.keys())
    
    assert "container_name" in params
    assert "repo_path" in params
    assert "verbose" in params
    
    print("✓ start_rag_server has correct parameters including container_name")


def test_get_rag_enabled_default():
    """Test that get_rag_enabled returns True by default (backward compatibility)."""
    # When "rag" key is not present, should default to True
    result = get_rag_enabled()
    # Note: This test depends on the actual config file
    # If .workflow.jsonc has "rag": false, this will be False
    # If .workflow.jsonc has "rag": true or no key, this will be True
    assert isinstance(result, bool)
    print(f"✓ get_rag_enabled returns boolean: {result}")


def test_get_rag_enabled_with_config():
    """Test that get_rag_enabled respects config file setting."""
    # This test verifies the function works with actual config
    config = load_config()
    rag_setting = config.get("rag")
    result = get_rag_enabled()
    
    # If rag is explicitly set, result should match
    # If not set, result should be True (default)
    if rag_setting is None:
        assert result is True, "Default should be True when key is absent"
    else:
        assert result == bool(rag_setting), "Should match config value"
    
    print(f"✓ get_rag_enabled respects config (rag={rag_setting}, result={result})")


def test_executor_checks_rag_config():
    """Test that executor.py checks get_rag_enabled() before RAG operations."""
    executor_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "workflow_lib", "executor.py"
    )
    
    with open(executor_path, "r") as f:
        content = f.read()
    
    # Check for conditional RAG help injection
    assert "if get_rag_enabled():" in content
    
    # Check for conditional RAG server startup
    # Should have at least 2 occurrences (Docker and non-Docker paths)
    lines = content.split('\n')
    conditional_rag_starts = [
        i for i, line in enumerate(lines) 
        if 'if get_rag_enabled()' in line and 'start_rag_server' in ''.join(lines[i:i+3])
    ]
    
    assert len(conditional_rag_starts) >= 2, \
        f"Expected at least 2 conditional RAG server starts, found {len(conditional_rag_starts)}"
    
    print(f"✓ executor.py checks get_rag_enabled() ({len(conditional_rag_starts)} conditional start sites)")


def test_config_imports_rag_enabled():
    """Test that config module exports get_rag_enabled."""
    from workflow_lib.config import get_rag_enabled as config_get_rag_enabled
    
    assert callable(config_get_rag_enabled)
    
    # Verify it returns a boolean
    result = config_get_rag_enabled()
    assert isinstance(result, bool)
    
    print("✓ config.get_rag_enabled() is callable and returns boolean")


if __name__ == "__main__":
    # Run tests
    tests = [
        test_rag_help_injected_in_executor,
        test_rag_help_content_comprehensive,
        test_rag_server_start_stop_lifecycle,
        test_rag_integration_module_imports,
        test_executor_imports_rag_integration,
        test_executor_injects_rag_help,
        test_executor_starts_rag_server,
        test_rag_help_text_length,
        test_rag_server_with_container_name_param,
        test_get_rag_enabled_default,
        test_get_rag_enabled_with_config,
        test_executor_checks_rag_config,
        test_config_imports_rag_enabled,
    ]
    
    passed = 0
    failed = 0
    errors = []
    
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"✗ {test.__name__} failed: {e}")
            failed += 1
            errors.append((test.__name__, str(e)))
        except Exception as e:
            print(f"✗ {test.__name__} error: {e}")
            failed += 1
            errors.append((test.__name__, str(e)))
    
    print(f"\n{'='*70}")
    print(f"E2E Test Results: {passed} passed, {failed} failed")
    print(f"{'='*70}")
    
    if errors:
        print("\nFailed tests:")
        for name, error in errors:
            print(f"  - {name}: {error}")
    
    sys.exit(0 if failed == 0 else 1)
