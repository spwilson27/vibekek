#!/usr/bin/env python3
"""Unit tests for the RAG integration module."""

import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workflow_lib.rag_integration import get_rag_help_text, start_rag_server, stop_rag_server


def test_get_rag_help_text_returns_string():
    """Test that get_rag_help_text returns a non-empty string."""
    help_text = get_rag_help_text()
    assert isinstance(help_text, str)
    assert len(help_text) > 0
    assert "RAG MCP" in help_text
    assert "rag-mcp-cli rag" in help_text
    assert "rag-mcp-cli semantic" in help_text
    assert "rag-mcp-cli serve" in help_text
    print("✓ get_rag_help_text returns valid content")


def test_get_rag_help_text_contains_workflows():
    """Test that help text includes common workflows."""
    help_text = get_rag_help_text()
    assert "UNDERSTAND A FEATURE" in help_text
    assert "FIND AND ANALYZE A CLASS" in help_text
    assert "TRACE A BUG" in help_text
    assert "EXPLORE A CODEBASE" in help_text
    print("✓ get_rag_help_text contains workflow examples")


def test_get_rag_help_text_contains_tips():
    """Test that help text includes tips for agents."""
    help_text = get_rag_help_text()
    assert "Tips" in help_text or "TIPS" in help_text
    assert "status" in help_text
    assert "semantic" in help_text
    print("✓ get_rag_help_text contains tips")


def test_start_rag_server_invalid_path():
    """Test that start_rag_server handles invalid paths gracefully."""
    # Should return None for non-existent RAG tool directory
    # (This test assumes RAG tool dir exists, but tests error handling)
    result = start_rag_server("/nonexistent/path", verbose=False)
    # Result may be None (RAG dir not found) or a PID (if server starts)
    # We just verify it doesn't crash
    print(f"✓ start_rag_server handles invalid path (result={result})")


def test_stop_rag_server_invalid_path():
    """Test that stop_rag_server handles invalid paths gracefully."""
    # Should return False for non-existent PID file
    result = stop_rag_server("/nonexistent/path", verbose=False)
    assert result is False
    print("✓ stop_rag_server handles invalid path correctly")


def test_rag_help_text_format():
    """Test that help text has proper markdown formatting."""
    help_text = get_rag_help_text()
    
    # Check for markdown headers
    assert "##" in help_text
    
    # Check for code blocks
    assert "```" in help_text
    
    # Check for bullet points
    assert "- " in help_text or "* " in help_text
    
    print("✓ get_rag_help_text has proper markdown formatting")


def test_rag_help_text_mentions_auto_start():
    """Test that help text mentions automatic server startup."""
    help_text = get_rag_help_text()
    assert "automatically started" in help_text.lower()
    print("✓ get_rag_help_text mentions automatic server startup")


def test_rag_help_text_mentions_docker():
    """Test that help text is suitable for Docker workflows."""
    help_text = get_rag_help_text()
    # The help text should work in both Docker and non-Docker contexts
    assert "workspace" in help_text.lower() or "directory" in help_text.lower()
    print("✓ get_rag_help_text is suitable for containerized workflows")


def test_start_rag_server_with_container_name():
    """Test that start_rag_server accepts container_name parameter."""
    import inspect
    sig = inspect.signature(start_rag_server)
    params = list(sig.parameters.keys())
    assert "container_name" in params
    print("✓ start_rag_server accepts container_name parameter")


def test_start_rag_server_returns_none_for_missing_rag_tool():
    """Test that start_rag_server returns None when RAG tool dir doesn't exist."""
    result = start_rag_server("/nonexistent", verbose=False)
    assert result is None
    print("✓ start_rag_server returns None for missing RAG tool")


def test_stop_rag_server_returns_false_for_missing_pid():
    """Test that stop_rag_server returns False when PID file doesn't exist."""
    result = stop_rag_server("/nonexistent", verbose=False)
    assert result is False
    print("✓ stop_rag_server returns False for missing PID file")


def test_start_rag_server_missing_rag_dir_verbose():
    """Test verbose output when RAG tool dir is missing."""
    result = start_rag_server("/nonexistent", verbose=True)
    assert result is None


def test_stop_rag_server_verbose_missing_pid():
    """Test verbose output when PID file missing."""
    result = stop_rag_server("/nonexistent", verbose=True)
    assert result is False


def test_stop_rag_server_stale_pid_file(tmp_path):
    """Test stop_rag_server with stale PID file (process not running)."""
    pid_file = tmp_path / ".rag-mcp-server.pid"
    pid_file.write_text("999999999")
    result = stop_rag_server(str(tmp_path), verbose=True)
    assert result is False
    assert not pid_file.exists()


def test_stop_rag_server_invalid_pid_content(tmp_path):
    """Test stop_rag_server with non-integer PID file content."""
    pid_file = tmp_path / ".rag-mcp-server.pid"
    pid_file.write_text("not-a-pid")
    result = stop_rag_server(str(tmp_path), verbose=True)
    assert result is False
    assert not pid_file.exists()


def test_start_rag_server_existing_pid_stale(tmp_path):
    """Test start_rag_server when PID file exists but process is dead."""
    from unittest.mock import patch
    pid_file = tmp_path / ".rag-mcp-server.pid"
    pid_file.write_text("999999999")
    # RAG_TOOL_DIR must exist for the function to proceed past first check
    with patch("workflow_lib.rag_integration.RAG_TOOL_DIR", str(tmp_path)):
        # start_rag_server will try to clean up stale PID, then start server
        # but subprocess will fail since RAG_CLI_MODULE doesn't exist
        result = start_rag_server(str(tmp_path), verbose=True)
    # Either None (if Popen fails) or a PID (if it somehow starts)
    # The stale PID file should be cleaned up regardless


def test_start_rag_server_existing_pid_valid_content(tmp_path):
    """Test start_rag_server when PID file has invalid content."""
    from unittest.mock import patch
    pid_file = tmp_path / ".rag-mcp-server.pid"
    pid_file.write_text("not-a-number")
    with patch("workflow_lib.rag_integration.RAG_TOOL_DIR", str(tmp_path)):
        result = start_rag_server(str(tmp_path), verbose=True)


def test_wait_for_rag_indexing_timeout():
    """Test wait_for_rag_indexing returns False on timeout."""
    from workflow_lib.rag_integration import wait_for_rag_indexing
    result = wait_for_rag_indexing("/nonexistent", timeout=1, verbose=True)
    assert result is False


def test_start_rag_server_container_name_no_rag_dir():
    """Test start_rag_server with container_name but no RAG dir."""
    result = start_rag_server("/nonexistent", container_name="test-container", verbose=True)
    assert result is None


if __name__ == "__main__":
    # Run tests
    tests = [
        test_get_rag_help_text_returns_string,
        test_get_rag_help_text_contains_workflows,
        test_get_rag_help_text_contains_tips,
        test_start_rag_server_invalid_path,
        test_stop_rag_server_invalid_path,
        test_rag_help_text_format,
        test_rag_help_text_mentions_auto_start,
        test_rag_help_text_mentions_docker,
        test_start_rag_server_with_container_name,
        test_start_rag_server_returns_none_for_missing_rag_tool,
        test_stop_rag_server_returns_false_for_missing_pid,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"✗ {test.__name__} failed: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__} error: {e}")
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    
    sys.exit(0 if failed == 0 else 1)
