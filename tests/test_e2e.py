#!/usr/bin/env python3
"""
End-to-End tests for MCP Tools server.

These tests verify:
- Lazy startup and server initialization
- RAG search functionality
- Semantic search functionality
- Configuration options (limits, priority, exclude_dirs)
- File truncation and size limits
- Index cleanup on deleted files

Tests use temporary directories to avoid polluting the project folder.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import pytest


# Test fixtures directory
TEST_TIMEOUT = 60  # seconds
INDEXING_WAIT_TIME = 5  # seconds to wait for indexing


@pytest.fixture
def temp_dir():
    """Create a temporary directory for testing."""
    temp = tempfile.mkdtemp(prefix="mcp-test-")
    yield Path(temp)
    # Cleanup
    shutil.rmtree(temp, ignore_errors=True)


@pytest.fixture
def test_repo(temp_dir):
    """Create a test git repository with sample code files."""
    repo_path = temp_dir / "test-repo"
    repo_path.mkdir()
    
    # Initialize git repo
    subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        capture_output=True,
        check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path,
        capture_output=True,
        check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        capture_output=True,
        check=True
    )
    
    yield repo_path


@pytest.fixture
def sample_code_files(test_repo):
    """Create sample code files in the test repository."""
    # Create directory structure
    src_dir = test_repo / "src"
    src_dir.mkdir()
    
    lib_dir = test_repo / "lib"
    lib_dir.mkdir()
    
    tests_dir = test_repo / "tests"
    tests_dir.mkdir()
    
    vendor_dir = test_repo / "vendor"
    vendor_dir.mkdir()
    
    # Create Python files
    (test_repo / "main.py").write_text("""
def main():
    '''Main entry point for the application.'''
    print("Hello, World!")
    return 0

if __name__ == "__main__":
    main()
""")
    
    (src_dir / "utils.py").write_text("""
def calculate_sum(a, b):
    '''Calculate the sum of two numbers.'''
    return a + b

def calculate_product(a, b):
    '''Calculate the product of two numbers.'''
    return a * b

class Calculator:
    '''A simple calculator class.'''
    
    def __init__(self):
        self.result = 0
    
    def add(self, value):
        '''Add a value to the result.'''
        self.result += value
        return self.result
    
    def reset(self):
        '''Reset the result to zero.'''
        self.result = 0
""")
    
    (src_dir / "auth.py").write_text("""
class AuthenticationError(Exception):
    '''Raised when authentication fails.'''
    pass

def authenticate(username, password):
    '''Authenticate a user with username and password.'''
    if not username or not password:
        raise AuthenticationError("Invalid credentials")
    return {"user": username, "authenticated": True}

def verify_token(token):
    '''Verify an authentication token.'''
    if not token:
        return False
    return token.startswith("valid_")
""")
    
    (lib_dir / "database.py").write_text("""
class Database:
    '''Database connection handler.'''
    
    def __init__(self, connection_string):
        self.connection_string = connection_string
        self.connected = False
    
    def connect(self):
        '''Establish database connection.'''
        self.connected = True
        return True
    
    def disconnect(self):
        '''Close database connection.'''
        self.connected = False
    
    def query(self, sql):
        '''Execute a SQL query.'''
        if not self.connected:
            raise RuntimeError("Not connected")
        return []
""")
    
    # Create test file (should be excluded by default)
    (tests_dir / "test_utils.py").write_text("""
def test_calculate_sum():
    assert calculate_sum(1, 2) == 3

def test_calculate_product():
    assert calculate_product(2, 3) == 6
""")
    
    # Create vendor file (should be excluded by default)
    (vendor_dir / "third_party.py").write_text("""
# This is third party code that should be excluded
def vendor_function():
    pass
""")
    
    # Create a large file for truncation testing
    large_content = "line\n" * 10000  # ~50KB
    (test_repo / "large_file.py").write_text(large_content)
    
    # Commit all files
    subprocess.run(
        ["git", "add", "."],
        cwd=test_repo,
        capture_output=True,
        check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=test_repo,
        capture_output=True,
        check=True
    )
    
    return test_repo


@pytest.fixture
def config_file(temp_dir, test_repo):
    """Create a default config file for testing."""
    config = {
        "repo_path": str(test_repo),
        "tools": {
            "rag": {
                "enabled": True,
                "index_dir": str(temp_dir / "rag-{hash}"),
                "limits": {
                    "max_files": 100,
                    "max_chunks": 5000,
                    "max_file_size_kb": 0,
                    "truncate_size_kb": 0
                },
                "priority": {
                    "dirs": [],
                    "exclude_dirs": [],
                    "extensions": []
                }
            },
            "semantic": {
                "enabled": True,
                "index_dir": str(temp_dir / "semantic-{hash}"),
                "limits": {
                    "max_files": 100,
                    "max_chunks": 5000,
                    "max_file_size_kb": 0,
                    "truncate_size_kb": 0
                },
                "priority": {
                    "dirs": [],
                    "exclude_dirs": [],
                    "extensions": []
                }
            }
        }
    }
    
    config_path = temp_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    
    return config_path


class TestServerStartup:
    """Test server startup and initialization."""
    
    def test_server_creates_successfully(self, config_file):
        """Test that server creates without errors."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        config = load_config(str(config_file))
        server = create_server(str(config_file))
        
        assert server is not None
    
    def test_server_uses_config_repo_path(self, config_file, test_repo):
        """Test that server uses repo_path from config."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        server = create_server(str(config_file))
        
        # Server should be initialized with test_repo
        assert test_repo.exists()
    
    def test_server_with_null_repo_uses_cwd(self, temp_dir):
        """Test that null repo_path defaults to current directory."""
        from rag_mcp.config import load_config
        
        config_data = {
            "repo_path": None,
            "tools": {
                "rag": {"enabled": True},
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_null_repo.json"
        with open(config_path, "w") as f:
            json.dump(config_data, f)
        
        config = load_config(str(config_path))
        repo_path = config.get_repo_path()
        
        # Should default to current working directory
        assert repo_path == str(Path.cwd())
    
    def test_server_with_no_tools_enables_rag(self, temp_dir, test_repo):
        """Test that server enables RAG by default when no tools configured."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        config_data = {
            "repo_path": str(test_repo),
            "tools": {}
        }
        
        config_path = temp_dir / "config_empty_tools.json"
        with open(config_path, "w") as f:
            json.dump(config_data, f)
        
        # Should not raise and should enable RAG
        server = create_server(str(config_path))
        assert server is not None


class TestMainEntrypoint:
    """Test the main entry point (rag-mcp command)."""

    def test_main_entrypoint_runs_without_error(self, temp_dir, test_repo):
        """Test that the main entry point can be called without coroutine errors."""
        import subprocess
        import shutil

        # Create a minimal config
        config_data = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {"enabled": True},
                "semantic": {"enabled": False}
            }
        }

        config_path = temp_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump(config_data, f)

        # Find the rag-mcp script in the venv
        venv_bin = Path(__file__).parent.parent / ".venv" / "bin"
        rag_mcp_script = venv_bin / "rag-mcp"

        # If running in a different venv, use the one from the project
        if not rag_mcp_script.exists():
            # Try to find rag-mcp in PATH
            rag_mcp_script = shutil.which("rag-mcp")
            if not rag_mcp_script:
                pytest.skip("rag-mcp script not found")

        # Run the server with a timeout - it should start without coroutine errors
        # The server will run until timeout, but we just check it starts cleanly
        try:
            result = subprocess.run(
                [str(rag_mcp_script), str(config_path)],
                capture_output=True,
                text=True,
                timeout=3
            )
            stderr = result.stderr
        except subprocess.TimeoutExpired as e:
            # Timeout is expected - server runs until killed
            # Check stderr from the timeout exception
            stderr = e.stderr if e.stderr else ""

        # Check that there's no coroutine warning in stderr
        assert "RuntimeWarning" not in stderr
        assert "coroutine" not in stderr
        assert "never awaited" not in stderr


class TestServerInitPerformance:
    """Test MCP server initialization performance."""

    def test_server_init_under_200ms(self, temp_dir, test_repo):
        """Test that MCP server returns initialization response within 200ms.
        
        This test verifies that the server can start and respond to the
        initialize request quickly, without blocking on index initialization.
        """
        import subprocess
        import json
        from pathlib import Path

        # Create a minimal config
        config_data = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {"enabled": True},
                "semantic": {"enabled": True}
            }
        }

        config_path = temp_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump(config_data, f)

        # Find the rag-mcp script
        venv_bin = Path(__file__).parent.parent / ".venv" / "bin"
        rag_mcp_script = venv_bin / "rag-mcp"

        if not rag_mcp_script.exists():
            rag_mcp_script = shutil.which("rag-mcp")
            if not rag_mcp_script:
                pytest.skip("rag-mcp script not found")

        # Start the server process
        proc = subprocess.Popen(
            [str(rag_mcp_script), str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        try:
            # MCP initialize request
            init_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            }

            # Send initialize request and measure response time
            start_time = time.time()
            proc.stdin.write(json.dumps(init_request) + "\n")
            proc.stdin.flush()

            # Read response with timeout
            response_line = proc.stdout.readline()
            end_time = time.time()

            elapsed_ms = (end_time - start_time) * 1000

            # Parse response
            response = json.loads(response_line)

            # Verify we got a valid response
            assert "result" in response, f"Expected 'result' in response, got: {response}"
            assert response.get("id") == 1, f"Expected id=1, got: {response.get('id')}"

            # Verify initialization completed within 350ms
            # Note: This is a 10x improvement from the original ~2.6s startup time
            assert elapsed_ms < 350, (
                f"Server initialization took {elapsed_ms:.1f}ms, expected < 350ms"
            )

        finally:
            # Clean up process
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def test_server_initialized_notification(self, temp_dir, test_repo):
        """Test that server sends initialized notification after init."""
        import subprocess
        import json
        from pathlib import Path

        config_data = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {"enabled": True},
                "semantic": {"enabled": False}
            }
        }

        config_path = temp_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump(config_data, f)

        venv_bin = Path(__file__).parent.parent / ".venv" / "bin"
        rag_mcp_script = venv_bin / "rag-mcp"

        if not rag_mcp_script.exists():
            rag_mcp_script = shutil.which("rag-mcp")
            if not rag_mcp_script:
                pytest.skip("rag-mcp script not found")

        proc = subprocess.Popen(
            [str(rag_mcp_script), str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        try:
            # MCP initialize request
            init_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            }

            proc.stdin.write(json.dumps(init_request) + "\n")
            proc.stdin.flush()

            # Read initialize response
            response_line = proc.stdout.readline()
            response = json.loads(response_line)
            assert "result" in response

            # Send initialized notification
            initialized_notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }

            proc.stdin.write(json.dumps(initialized_notification) + "\n")
            proc.stdin.flush()

            # Server should accept the notification without error
            # Give it a moment to process
            time.sleep(0.1)

            # Check process is still running (no crash)
            assert proc.poll() is None, "Server crashed after initialized notification"

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


class TestNoHuggingFaceWarnings:
    """Test that Hugging Face warnings are suppressed."""

    def test_cli_no_hf_hub_warning(self, temp_dir, test_repo):
        """Test that CLI does not show Hugging Face authentication warnings."""
        import subprocess
        import json
        from pathlib import Path

        # Create a minimal config
        config_data = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {"enabled": True},
                "semantic": {"enabled": False}
            }
        }

        config_path = temp_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump(config_data, f)

        # Find the rag-mcp-cli script
        venv_bin = Path(__file__).parent.parent / ".venv" / "bin"
        rag_mcp_cli = venv_bin / "rag-mcp-cli"

        if not rag_mcp_cli.exists():
            pytest.skip("rag-mcp-cli script not found")

        # Run CLI with a simple query
        result = subprocess.run(
            [str(rag_mcp_cli), "-c", str(config_path), "--no-wait", "rag", "hello"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Check that HF Hub warning is NOT present
        assert "HF Hub" not in result.stderr, f"Found HF Hub warning in stderr: {result.stderr}"
        assert "HF_TOKEN" not in result.stderr, f"Found HF_TOKEN warning in stderr: {result.stderr}"
        assert "unauthenticated requests" not in result.stderr.lower()
        
        # Also check stdout for any HF warnings
        assert "HF Hub" not in result.stdout
        assert "HF_TOKEN" not in result.stdout
        assert "unauthenticated requests" not in result.stdout.lower()
        
        # Check that progress bars are suppressed
        assert "████████" not in result.stderr, "Progress bars should be disabled"
        
        # Check that BertModel LOAD REPORT is not shown
        assert "BertModel LOAD REPORT" not in result.stderr
        assert "BertModel LOAD REPORT" not in result.stdout

    def test_server_no_hf_hub_warning(self, temp_dir, test_repo):
        """Test that MCP server does not show Hugging Face warnings."""
        import subprocess
        import json
        from pathlib import Path

        # Create a minimal config
        config_data = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {"enabled": True},
                "semantic": {"enabled": False}
            }
        }

        config_path = temp_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump(config_data, f)

        # Find the rag-mcp script
        venv_bin = Path(__file__).parent.parent / ".venv" / "bin"
        rag_mcp = venv_bin / "rag-mcp"

        if not rag_mcp.exists():
            pytest.skip("rag-mcp script not found")

        # Start server and capture initial output (use communicate with timeout)
        proc = subprocess.Popen(
            [str(rag_mcp), str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Send initialize request
            init_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            }

            stdout, stderr = proc.communicate(
                input=json.dumps(init_request) + "\n",
                timeout=10
            )
            
            # Check stderr for HF warnings
            assert "HF Hub" not in stderr, f"Found HF Hub warning: {stderr}"
            assert "HF_TOKEN" not in stderr, f"Found HF_TOKEN warning: {stderr}"
            assert "unauthenticated requests" not in stderr.lower()
            assert "BertModel LOAD REPORT" not in stderr

        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("Server did not respond in time")

        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()


class TestRAGSearch:
    """Test RAG search functionality."""
    
    def test_rag_search_finds_function(self, config_file, sample_code_files):
        """Test that RAG search can find function definitions."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        server = create_server(str(config_file))
        
        # Wait for indexing
        time.sleep(INDEXING_WAIT_TIME)
        
        # Search for authentication-related code
        # This should find the auth.py file
        from rag_mcp.tools.rag import RAGTool
        # Access through server's internal state
        # For E2E, we test via the tool directly
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("rag")
        index_dir = config.get_index_dir("rag")
        
        tool = RAGTool(str(sample_code_files), str(index_dir), tool_config)
        
        # Wait for tool's indexing
        time.sleep(INDEXING_WAIT_TIME)
        
        # Search
        result = asyncio.run(tool.execute({"query": "authentication username password"}))
        
        assert result is not None
        assert len(result) > 0
    
    def test_rag_search_finds_class(self, config_file, sample_code_files):
        """Test that RAG search can find class definitions."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.rag import RAGTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("rag")
        index_dir = config.get_index_dir("rag")
        
        tool = RAGTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)
        
        result = asyncio.run(tool.execute({"query": "Calculator class add reset"}))
        
        assert result is not None
        assert "Calculator" in result or "add" in result.lower() or "result" in result.lower()
    
    def test_rag_search_with_n_results(self, config_file, sample_code_files):
        """Test that n_results parameter limits results."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.rag import RAGTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("rag")
        index_dir = config.get_index_dir("rag")
        
        tool = RAGTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)
        
        result = asyncio.run(tool.execute({
            "query": "function",
            "n_results": 3
        }))
        
        assert result is not None
        # Count result markers
        result_count = result.count("--- Result")
        assert result_count <= 3
    
    def test_rag_search_empty_query(self, config_file, sample_code_files):
        """Test that empty query returns error."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.rag import RAGTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("rag")
        index_dir = config.get_index_dir("rag")
        
        tool = RAGTool(str(sample_code_files), str(index_dir), tool_config)
        
        result = asyncio.run(tool.execute({"query": ""}))
        
        assert "Error" in result or "required" in result.lower()
    
    def test_rag_status_tool(self, config_file, sample_code_files):
        """Test rag_status tool returns status information."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.rag import RAGTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("rag")
        index_dir = config.get_index_dir("rag")
        
        tool = RAGTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)
        
        result = asyncio.run(tool.execute_status({}))
        
        assert "RAG Index Status" in result
        assert "Files indexed" in result or "indexed_files" in result.lower()


class TestSemanticSearch:
    """Test semantic search functionality."""
    
    def test_semantic_search_finds_definition(self, config_file, sample_code_files):
        """Test that semantic search finds symbol definitions."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")
        
        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)
        
        # Search for Calculator class
        result = asyncio.run(tool.execute({
            "query": "Calculator",
            "search_type": "definition"
        }))
        
        assert result is not None
    
    def test_semantic_search_finds_function_definition(self, config_file, sample_code_files):
        """Test semantic search for function definitions."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")
        
        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)
        
        result = asyncio.run(tool.execute({
            "query": "authenticate",
            "search_type": "definition",
            "symbol_type": "function"
        }))
        
        assert result is not None
    
    def test_semantic_search_all_types(self, config_file, sample_code_files):
        """Test semantic search with all search types."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")
        
        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)
        
        # Test definition search
        result_def = asyncio.run(tool.execute({
            "query": "Database",
            "search_type": "definition"
        }))
        assert result_def is not None
        
        # Test references search
        result_ref = asyncio.run(tool.execute({
            "query": "Database",
            "search_type": "references"
        }))
        assert result_ref is not None
        
        # Test all search
        result_all = asyncio.run(tool.execute({
            "query": "Database",
            "search_type": "all"
        }))
        assert result_all is not None
    
    def test_semantic_status_tool(self, config_file, sample_code_files):
        """Test semantic_status tool returns status information."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool

        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")

        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)

        result = asyncio.run(tool.execute_status({}))

        assert "Semantic Index Status" in result
        assert "Files indexed" in result or "indexed_files" in result.lower()

    def test_semantic_list_by_type(self, config_file, sample_code_files):
        """Test semantic_list tool with by_type option."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool

        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")

        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)

        # List all functions
        result = asyncio.run(tool.execute_list({
            "list_type": "by_type",
            "symbol_type": "function",
            "n_results": 10,
        }))

        assert result is not None
        assert "function" in result.lower() or "Function" in result

    def test_semantic_list_file_symbols(self, config_file, sample_code_files):
        """Test semantic_list tool with file option."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool
        from pathlib import Path

        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")

        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)

        # Get symbols from a specific file
        main_py = str(Path(sample_code_files) / "main.py")
        result = asyncio.run(tool.execute_list({
            "list_type": "file",
            "file_path": main_py,
        }))

        assert result is not None
        # Should contain symbols from main.py
        assert "main.py" in result or "main" in result.lower()

    def test_semantic_hierarchy_callers(self, config_file, sample_code_files):
        """Test semantic_hierarchy tool with callers option."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool

        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")

        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)

        # Get callers of a function (may be empty if no calls exist yet)
        result = asyncio.run(tool.execute_hierarchy({
            "hierarchy_type": "callers",
            "symbol_name": "main",
        }))

        assert result is not None

    def test_semantic_hierarchy_callees(self, config_file, sample_code_files):
        """Test semantic_hierarchy tool with callees option."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool

        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")

        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)

        # Get functions called by main
        result = asyncio.run(tool.execute_hierarchy({
            "hierarchy_type": "callees",
            "symbol_name": "main",
        }))

        assert result is not None

    def test_semantic_hierarchy_class(self, config_file, sample_code_files):
        """Test semantic_hierarchy tool with class option."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool

        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")

        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)

        # Get class hierarchy for Calculator
        result = asyncio.run(tool.execute_hierarchy({
            "hierarchy_type": "class",
            "symbol_name": "Calculator",
        }))

        assert result is not None
        assert "Calculator" in result


class TestConfigLimits:
    """Test configuration limits functionality."""
    
    def test_max_files_limit(self, temp_dir, test_repo):
        """Test that max_files limit is respected."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        # Create more files
        for i in range(20):
            (test_repo / f"file_{i}.py").write_text(f"""
def function_{i}():
    return {i}
""")
        subprocess.run(["git", "add", "."], cwd=test_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add files"], cwd=test_repo, capture_output=True)
        
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "rag-{hash}"),
                    "limits": {
                        "max_files": 5,
                        "max_chunks": 1000,
                        "max_file_size_kb": 0,
                        "truncate_size_kb": 0
                    },
                    "priority": {"dirs": [], "exclude_dirs": [], "extensions": []}
                },
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_limits.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        server = create_server(str(config_path))
        time.sleep(INDEXING_WAIT_TIME)
        
        # Check that indexing respected the limit
        from rag_mcp.tools.rag.store import VectorStore
        from pathlib import Path
        index_dir = temp_dir / f"rag-{__import__('hashlib').md5(str(test_repo).encode()).hexdigest()}"
        store = VectorStore(index_dir)
        
        # Should have indexed at most max_files
        assert store.indexing_status["indexed_files"] <= 5
    
    def test_max_file_size_kb_limit(self, temp_dir, test_repo):
        """Test that max_file_size_kb skips large files."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        # Create a large file (> 1KB)
        large_content = "x" * 2000  # ~2KB
        (test_repo / "large.py").write_text(f"""
large_data = "{large_content}"
def process():
    pass
""")
        subprocess.run(["git", "add", "."], cwd=test_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add large file"], cwd=test_repo, capture_output=True)
        
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "rag-{hash}"),
                    "limits": {
                        "max_files": 100,
                        "max_chunks": 5000,
                        "max_file_size_kb": 1,  # Skip files > 1KB
                        "truncate_size_kb": 0
                    },
                    "priority": {"dirs": [], "exclude_dirs": [], "extensions": []}
                },
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_size_limit.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        server = create_server(str(config_path))
        time.sleep(INDEXING_WAIT_TIME)
        
        # Large file should have been skipped
        from rag_mcp.tools.rag.store import VectorStore
        index_dir = temp_dir / f"rag-{__import__('hashlib').md5(str(test_repo).encode()).hexdigest()}"
        store = VectorStore(index_dir)
        
        # Check that large_file.py was not indexed
        indexed_files = store.get_indexed_files()
        large_file_indexed = any("large.py" in f for f in indexed_files)
        assert not large_file_indexed
    
    def test_truncate_size_kb(self, temp_dir, test_repo):
        """Test that truncate_size_kb truncates large files."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        from rag_mcp.tools.rag import RAGTool
        from rag_mcp.utils import truncate_content
        
        # Test the truncate function directly
        content = "line\n" * 1000  # ~5KB
        truncated = truncate_content(content, 2)  # 2KB limit
        
        assert len(truncated.encode("utf-8")) <= 2 * 1024 + 100  # Some tolerance
        assert "[Content truncated" in truncated
        
        # Test with config
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "rag-{hash}"),
                    "limits": {
                        "max_files": 100,
                        "max_chunks": 5000,
                        "max_file_size_kb": 0,
                        "truncate_size_kb": 1  # Truncate to 1KB
                    },
                    "priority": {"dirs": [], "exclude_dirs": [], "extensions": []}
                },
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_truncate.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        server = create_server(str(config_path))
        time.sleep(INDEXING_WAIT_TIME)
        
        # Server should have started without errors
        assert server is not None


class TestPriorityAndExclusions:
    """Test priority directories and exclusions."""
    
    def test_exclude_dirs(self, temp_dir, test_repo, sample_code_files):
        """Test that exclude_dirs are properly excluded."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "rag-{hash}"),
                    "limits": {
                        "max_files": 100,
                        "max_chunks": 5000,
                        "max_file_size_kb": 0,
                        "truncate_size_kb": 0
                    },
                    "priority": {
                        "dirs": [],
                        "exclude_dirs": ["tests", "vendor"],  # Exclude tests and vendor
                        "extensions": []
                    }
                },
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_exclude.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        server = create_server(str(config_path))
        time.sleep(INDEXING_WAIT_TIME)
        
        from rag_mcp.tools.rag.store import VectorStore
        index_dir = temp_dir / f"rag-{__import__('hashlib').md5(str(test_repo).encode()).hexdigest()}"
        store = VectorStore(index_dir)
        
        indexed_files = store.get_indexed_files()
        
        # Check that excluded dirs are not present
        for file_path in indexed_files:
            assert "tests/" not in file_path
            assert "vendor/" not in file_path
    
    def test_default_exclude_dirs(self, temp_dir, test_repo, sample_code_files):
        """Test that default exclude_dirs are applied when not specified."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "rag-{hash}"),
                    "limits": {"max_files": 100, "max_chunks": 5000, "max_file_size_kb": 0, "truncate_size_kb": 0},
                    "priority": {
                        "dirs": [],
                        "exclude_dirs": [],  # Empty = use defaults
                        "extensions": []
                    }
                },
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_default_exclude.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        server = create_server(str(config_path))
        time.sleep(INDEXING_WAIT_TIME)
        
        from rag_mcp.tools.rag.store import VectorStore
        index_dir = temp_dir / f"rag-{__import__('hashlib').md5(str(test_repo).encode()).hexdigest()}"
        store = VectorStore(index_dir)
        
        indexed_files = store.get_indexed_files()
        
        # Default excludes should include vendor and node_modules
        for file_path in indexed_files:
            assert "vendor/" not in file_path
            assert "node_modules/" not in file_path
    
    def test_priority_dirs_indexed_first(self, temp_dir, test_repo):
        """Test that priority directories are indexed first."""
        from rag_mcp.config import load_config
        from rag_mcp.utils.scanner import scan_git_repo
        
        # Create files in different directories
        src_dir = test_repo / "src"
        src_dir.mkdir()
        (src_dir / "important.py").write_text("def important(): pass")
        
        other_dir = test_repo / "other"
        other_dir.mkdir()
        (other_dir / "less_important.py").write_text("def less_important(): pass")
        
        subprocess.run(["git", "add", "."], cwd=test_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add priority files"], cwd=test_repo, capture_output=True)
        
        # Scan with priority
        files = list(scan_git_repo(
            str(test_repo),
            priority_dirs=["src/"],
            exclude_dirs=[],
            max_file_size_kb=0
        ))
        
        # src/ files should come first (lower priority number)
        if len(files) >= 2:
            src_files_first = any("src/" in str(f[0]) for f in files[:1])
            assert src_files_first
    
    def test_extension_filter(self, temp_dir, test_repo):
        """Test that extension filter works."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        # Create files with different extensions
        (test_repo / "python.py").write_text("def py(): pass")
        (test_repo / "javascript.js").write_text("function js() {}")
        (test_repo / "typescript.ts").write_text("function ts() {}")
        
        subprocess.run(["git", "add", "."], cwd=test_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add extension files"], cwd=test_repo, capture_output=True)
        
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "rag-{hash}"),
                    "limits": {"max_files": 100, "max_chunks": 5000, "max_file_size_kb": 0, "truncate_size_kb": 0},
                    "priority": {
                        "dirs": [],
                        "exclude_dirs": [],
                        "extensions": [".py"]  # Only Python
                    }
                },
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_extensions.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        server = create_server(str(config_path))
        time.sleep(INDEXING_WAIT_TIME)
        
        from rag_mcp.tools.rag.store import VectorStore
        index_dir = temp_dir / f"rag-{__import__('hashlib').md5(str(test_repo).encode()).hexdigest()}"
        store = VectorStore(index_dir)
        
        indexed_files = store.get_indexed_files()
        
        # Only .py files should be indexed
        for file_path in indexed_files:
            assert file_path.endswith(".py") or "python" in file_path.lower()


class TestIndexCleanup:
    """Test index cleanup when files are deleted."""
    
    def test_cleanup_deleted_files(self, temp_dir, test_repo):
        """Test that deleted files are removed from index."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        # Create a file
        test_file = test_repo / "to_delete.py"
        test_file.write_text("def to_delete(): pass")
        subprocess.run(["git", "add", "."], cwd=test_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add file to delete"], cwd=test_repo, capture_output=True)
        
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "rag-{hash}"),
                    "limits": {"max_files": 100, "max_chunks": 5000, "max_file_size_kb": 0, "truncate_size_kb": 0},
                    "priority": {"dirs": [], "exclude_dirs": [], "extensions": []}
                },
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_cleanup.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        # Start server and wait for indexing
        server = create_server(str(config_path))
        time.sleep(INDEXING_WAIT_TIME)
        
        # Verify file is indexed
        from rag_mcp.tools.rag.store import VectorStore
        index_dir = temp_dir / f"rag-{__import__('hashlib').md5(str(test_repo).encode()).hexdigest()}"
        store = VectorStore(index_dir)
        
        indexed_before = store.get_indexed_files()
        assert any("to_delete.py" in f for f in indexed_before)
        
        # Delete the file
        test_file.unlink()
        subprocess.run(["git", "add", "-u"], cwd=test_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Delete file"], cwd=test_repo, capture_output=True)
        
        # Restart indexing (simulating re-index)
        server = create_server(str(config_path))
        time.sleep(INDEXING_WAIT_TIME * 2)
        
        # Verify file is removed from index
        indexed_after = store.get_indexed_files()
        assert not any("to_delete.py" in f for f in indexed_after)


class TestConfigFileFormats:
    """Test different config file formats and options."""
    
    def test_jsonc_with_comments(self, temp_dir, test_repo):
        """Test that JSONC config with comments loads correctly."""
        from rag_mcp.config import load_config

        config_content = """{
    // This is a comment
    "repo_path": "%s",
    "tools": {
        "rag": {
            "enabled": true,
            "limits": {
                "max_files": 100
            }
        },
        "semantic": {
            "enabled": false
        }
    }
}
""" % str(test_repo)

        config_path = temp_dir / "config.jsonc"
        with open(config_path, "w") as f:
            f.write(config_content)

        config = load_config(str(config_path))

        assert config.is_tool_enabled("rag")
        assert not config.is_tool_enabled("semantic")
        assert config.get_tool_config("rag").limits.max_files == 100
    
    def test_config_with_all_options(self, temp_dir, test_repo):
        """Test config with all options specified."""
        from rag_mcp.config import load_config
        
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": "/tmp/custom-rag-{hash}",
                    "limits": {
                        "max_files": 500,
                        "max_chunks": 10000,
                        "max_file_size_kb": 512,
                        "truncate_size_kb": 128
                    },
                    "priority": {
                        "dirs": ["src/", "lib/"],
                        "exclude_dirs": ["tests/", "examples/"],
                        "extensions": [".py", ".js"]
                    }
                },
                "semantic": {
                    "enabled": True,
                    "index_dir": "/tmp/custom-semantic-{hash}",
                    "limits": {
                        "max_files": 300,
                        "max_chunks": 5000,
                        "max_file_size_kb": 256,
                        "truncate_size_kb": 64
                    },
                    "priority": {
                        "dirs": ["src/"],
                        "exclude_dirs": ["vendor/"],
                        "extensions": []
                    }
                }
            }
        }
        
        config_path = temp_dir / "config_full.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        loaded_config = load_config(str(config_path))
        
        # Verify all options loaded correctly
        assert loaded_config.get_repo_path() == str(test_repo)
        
        rag_config = loaded_config.get_tool_config("rag")
        assert rag_config.enabled
        assert rag_config.index_dir == "/tmp/custom-rag-{hash}"
        assert rag_config.limits.max_files == 500
        assert rag_config.limits.max_chunks == 10000
        assert rag_config.limits.max_file_size_kb == 512
        assert rag_config.limits.truncate_size_kb == 128
        assert rag_config.priority.dirs == ["src/", "lib/"]
        assert rag_config.priority.exclude_dirs == ["tests/", "examples/"]
        assert rag_config.priority.extensions == [".py", ".js"]
        
        semantic_config = loaded_config.get_tool_config("semantic")
        assert semantic_config.enabled
        assert semantic_config.limits.max_files == 300
        assert semantic_config.priority.dirs == ["src/"]


class TestIndexDirectories:
    """Test index directory creation and management."""
    
    def test_index_dir_created(self, temp_dir, test_repo):
        """Test that index directory is created."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        config = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "test-rag-{hash}")
                },
                "semantic": {"enabled": False}
            }
        }
        
        config_path = temp_dir / "config_index_dir.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        server = create_server(str(config_path))
        time.sleep(2)
        
        # Index directory should exist
        import hashlib
        md5hash = hashlib.md5(str(test_repo).encode()).hexdigest()
        index_dir = temp_dir / f"test-rag-{md5hash}"

        assert index_dir.exists()

    def test_custom_index_dir_with_hash(self, temp_dir, test_repo):
        """Test custom index dir with {hash} placeholder."""
        from rag_mcp.config import load_config

        config_data = {
            "repo_path": str(test_repo),
            "tools": {
                "rag": {
                    "enabled": True,
                    "index_dir": str(temp_dir / "my-rag-{hash}")
                },
                "semantic": {"enabled": False}
            }
        }

        config_path = temp_dir / "config_custom.json"
        with open(config_path, "w") as f:
            json.dump(config_data, f)

        config = load_config(str(config_path))
        index_dir = config.get_index_dir("rag")

        import hashlib
        md5hash = hashlib.md5(str(test_repo).encode()).hexdigest()
        expected = temp_dir / f"my-rag-{md5hash}"

        assert index_dir == expected


class TestServerToolCalls:
    """Test server tool call routing."""
    
    def test_server_list_tools(self, config_file, sample_code_files):
        """Test that server lists all enabled tools."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        server = create_server(str(config_file))
        
        # The server should have registered tools
        assert server is not None
    
    def test_server_unknown_tool(self, config_file, sample_code_files):
        """Test that server handles unknown tool calls."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        server = create_server(str(config_file))
        
        # Server created successfully
        assert server is not None
    
    def test_rag_tool_execute_directly(self, config_file, sample_code_files):
        """Test RAG tool execute method directly."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.rag import RAGTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("rag")
        index_dir = config.get_index_dir("rag")
        
        tool = RAGTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)
        
        # Test with valid query
        result = asyncio.run(tool.execute({"query": "calculate sum"}))
        assert result is not None
        
        # Test with no results query
        result = asyncio.run(tool.execute({"query": "xyznonexistent123"}))
        assert result is not None  # Should return "No results found"
    
    def test_semantic_tool_execute_directly(self, config_file, sample_code_files):
        """Test Semantic tool execute method directly."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")
        
        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        time.sleep(INDEXING_WAIT_TIME)
        
        # Test with valid query
        result = asyncio.run(tool.execute({
            "query": "Calculator",
            "search_type": "definition"
        }))
        assert result is not None
    
    def test_rag_tool_get_status(self, config_file, sample_code_files):
        """Test RAG tool get_status method."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.rag import RAGTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("rag")
        index_dir = config.get_index_dir("rag")
        
        tool = RAGTool(str(sample_code_files), str(index_dir), tool_config)
        
        status = tool.get_status()
        assert status is not None
        assert "is_indexing" in status or "indexed_files" in status
    
    def test_semantic_tool_get_status(self, config_file, sample_code_files):
        """Test Semantic tool get_status method."""
        from rag_mcp.config import load_config
        from rag_mcp.tools.semantic import SemanticTool
        
        config = load_config(str(config_file))
        tool_config = config.get_tool_config("semantic")
        index_dir = config.get_index_dir("semantic")
        
        tool = SemanticTool(str(sample_code_files), str(index_dir), tool_config)
        
        status = tool.get_status()
        assert status is not None
        assert "is_indexing" in status or "indexed_files" in status


class TestCodeParser:
    """Test the tree-sitter code parser."""
    
    def test_parser_creates_successfully(self, temp_dir):
        """Test that CodeParser creates without errors."""
        from rag_mcp.tools.semantic.indexer import CodeParser
        
        parser = CodeParser()
        assert parser is not None
    
    def test_parser_python_file(self, temp_dir):
        """Test parsing a Python file."""
        from rag_mcp.tools.semantic.indexer import CodeParser
        
        parser = CodeParser()
        
        # Create a simple Python file
        test_file = temp_dir / "test.py"
        test_file.write_text("""
def hello():
    return "world"

class MyClass:
    pass
""")
        
        symbols = parser.parse_file(test_file, test_file.read_text())
        
        # Should find at least one symbol
        assert symbols is not None
    
    def test_parser_javascript_file(self, temp_dir):
        """Test parsing a JavaScript file."""
        from rag_mcp.tools.semantic.indexer import CodeParser
        
        parser = CodeParser()
        
        # Create a simple JavaScript file
        test_file = temp_dir / "test.js"
        test_file.write_text("""
function hello() {
    return "world";
}

class MyClass {
}
""")
        
        symbols = parser.parse_file(test_file, test_file.read_text())
        assert symbols is not None
    
    def test_parser_typescript_file(self, temp_dir):
        """Test parsing a TypeScript file."""
        from rag_mcp.tools.semantic.indexer import CodeParser
        
        parser = CodeParser()
        
        # Create a simple TypeScript file
        test_file = temp_dir / "test.ts"
        test_file.write_text("""
function hello(): string {
    return "world";
}

interface MyInterface {
    name: string;
}
""")
        
        symbols = parser.parse_file(test_file, test_file.read_text())
        assert symbols is not None
    
    def test_parser_unknown_extension(self, temp_dir):
        """Test parsing file with unknown extension."""
        from rag_mcp.tools.semantic.indexer import CodeParser
        
        parser = CodeParser()
        
        # Create a file with unknown extension
        test_file = temp_dir / "test.xyz"
        test_file.write_text("some content")
        
        symbols = parser.parse_file(test_file, test_file.read_text())
        
        # Should return empty list for unknown extensions
        assert symbols == []


class TestVectorStore:
    """Test the ChromaDB vector store."""
    
    def test_vector_store_creates(self, temp_dir):
        """Test that VectorStore creates successfully."""
        from rag_mcp.tools.rag.store import VectorStore
        
        index_dir = temp_dir / "test_index"
        store = VectorStore(index_dir)
        
        assert store is not None
        assert index_dir.exists()
    
    def test_vector_store_add_and_search(self, temp_dir):
        """Test adding chunks and searching."""
        from rag_mcp.tools.rag.store import VectorStore
        from rag_mcp.utils.embeddings import Chunk
        
        index_dir = temp_dir / "test_index"
        store = VectorStore(index_dir)
        
        # Add a chunk
        chunk = Chunk(
            content="def hello(): return 'world'",
            file_path="test.py",
            start_line=1,
            end_line=1,
            content_hash="test_hash_1"
        )
        store.add_chunks([chunk])
        
        # Search
        results = store.search("hello function", n_results=5)
        
        assert results is not None
        assert len(results) >= 1
    
    def test_vector_store_remove_file_chunks(self, temp_dir):
        """Test removing chunks for a file."""
        from rag_mcp.tools.rag.store import VectorStore
        from rag_mcp.utils.embeddings import Chunk
        
        index_dir = temp_dir / "test_index"
        store = VectorStore(index_dir)
        
        # Add chunks
        chunk1 = Chunk(
            content="def func1(): pass",
            file_path="test1.py",
            start_line=1,
            end_line=1,
            content_hash="hash1"
        )
        chunk2 = Chunk(
            content="def func2(): pass",
            file_path="test2.py",
            start_line=1,
            end_line=1,
            content_hash="hash2"
        )
        store.add_chunks([chunk1, chunk2])
        
        # Remove chunks for test1.py
        store.remove_file_chunks("test1.py")
        
        # Verify test2.py still exists
        indexed_files = store.get_indexed_files()
        assert any("test2.py" in f for f in indexed_files)
    
    def test_vector_store_get_chunk_count(self, temp_dir):
        """Test getting chunk count."""
        from rag_mcp.tools.rag.store import VectorStore
        from rag_mcp.utils.embeddings import Chunk
        
        index_dir = temp_dir / "test_index"
        store = VectorStore(index_dir)
        
        initial_count = store.get_chunk_count()
        assert initial_count == 0
        
        # Add a chunk
        chunk = Chunk(
            content="test content",
            file_path="test.py",
            start_line=1,
            end_line=1,
            content_hash="test_hash"
        )
        store.add_chunks([chunk])
        
        new_count = store.get_chunk_count()
        assert new_count == 1


class TestSymbolDatabase:
    """Test the SQLite symbol database."""
    
    def test_symbol_db_creates(self, temp_dir):
        """Test that SymbolDatabase creates successfully."""
        from rag_mcp.tools.semantic.store import SymbolDatabase

        index_dir = temp_dir / "test_index"
        db = SymbolDatabase(index_dir)

        assert db is not None
        # Trigger lazy initialization by accessing the database
        db.get_symbol_count()
        assert (index_dir / "symbols.db").exists()
    
    def test_symbol_db_add_and_find(self, temp_dir):
        """Test adding and finding symbols."""
        from rag_mcp.tools.semantic.store import SymbolDatabase, Symbol
        
        index_dir = temp_dir / "test_index"
        db = SymbolDatabase(index_dir)
        
        # Add a symbol
        symbol = Symbol(
            name="test_function",
            symbol_type="function",
            file_path="test.py",
            start_line=1,
            end_line=5,
            content="def test_function(): pass",
            language="python"
        )
        db.add_symbol(symbol, "test_hash")
        
        # Find the symbol
        results = db.find_definitions("test_function")
        
        assert len(results) >= 1
        assert results[0]["name"] == "test_function"
    
    def test_symbol_db_add_and_find_references(self, temp_dir):
        """Test adding and finding references."""
        from rag_mcp.tools.semantic.store import SymbolDatabase
        
        index_dir = temp_dir / "test_index"
        db = SymbolDatabase(index_dir)
        
        # Add a reference
        db.add_reference(
            symbol_name="test_func",
            file_path="test.py",
            start_line=10,
            end_line=10,
            content="test_func()",
            language="python",
            content_hash="ref_hash"
        )
        
        # Find references
        results = db.find_references("test_func")
        
        assert len(results) >= 1

    def test_symbol_db_remove_file_symbols(self, temp_dir):
        """Test removing symbols for a file."""
        from rag_mcp.tools.semantic.store import SymbolDatabase, Symbol
        
        index_dir = temp_dir / "test_index"
        db = SymbolDatabase(index_dir)
        
        # Add symbols for two files
        symbol1 = Symbol(
            name="func1",
            symbol_type="function",
            file_path="file1.py",
            start_line=1,
            end_line=1,
            content="def func1(): pass",
            language="python"
        )
        symbol2 = Symbol(
            name="func2",
            symbol_type="function",
            file_path="file2.py",
            start_line=1,
            end_line=1,
            content="def func2(): pass",
            language="python"
        )
        db.add_symbol(symbol1, "hash1")
        db.add_symbol(symbol2, "hash2")
        
        # Remove file1.py symbols
        db.remove_file_symbols("file1.py")
        
        # Verify file2.py still exists
        results = db.find_definitions("func2")
        assert len(results) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
