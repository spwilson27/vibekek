"""RAG MCP tool integration for workflow agents.

This module provides utilities for:
1. Injecting RAG tool help text into agent prompts
2. Automatically starting/stopping the RAG MCP server in cloned repositories
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .constants import TOOLS_DIR, ROOT_DIR

# RAG tool CLI path
RAG_TOOL_DIR = os.path.join(TOOLS_DIR, "rag-tool")
RAG_CLI_MODULE = "rag_mcp.cli"

# RAG help text to inject into agent prompts
_RAG_HELP_TEXT = """
# Code Search Tools (RAG MCP)

You have access to powerful code search tools via the RAG MCP CLI. Use these tools to understand the codebase, find symbols, and trace code flow.

## Available Commands

**SEARCH COMMANDS:**
- `rag-mcp-cli rag <query>` - Natural language search ("how does auth work?")
- `rag-mcp-cli semantic <name>` - Symbol search (definitions, references)
- `rag-mcp-cli list --by-type <type>` - List all symbols of a type
- `rag-mcp-cli list --file <path>` - List symbols in a file
- `rag-mcp-cli hierarchy --callers X` - What functions call X
- `rag-mcp-cli hierarchy --callees X` - What functions does X call
- `rag-mcp-cli hierarchy --class X` - Class inheritance

**SERVER COMMANDS:**
- `rag-mcp-cli serve` - Start background MCP server
- `rag-mcp-cli stop` - Stop background server
- `rag-mcp-cli status` - Show indexing status

## Common Workflows

1. **UNDERSTAND A FEATURE:**
   ```
   rag-mcp-cli rag "how is authentication implemented"
   rag-mcp-cli rag "password hashing and validation"
   ```

2. **FIND AND ANALYZE A CLASS:**
   ```
   rag-mcp-cli semantic UserService --symbol-type class
   rag-mcp-cli hierarchy --callers UserService
   rag-mcp-cli list --file src/services/user.py
   ```

3. **TRACE A BUG:**
   ```
   rag-mcp-cli semantic process_payment --search-type references
   rag-mcp-cli hierarchy --callers process_payment
   rag-mcp-cli hierarchy --callees validate_card
   ```

4. **EXPLORE A CODEBASE:**
   ```
   rag-mcp-cli list --by-type class
   rag-mcp-cli list --by-type function -n 50
   rag-mcp-cli rag "API endpoints" -n 20
   ```

5. **CODE REVIEW PREP:**
   ```
   rag-mcp-cli status
   rag-mcp-cli semantic main --search-type callers
   rag-mcp-cli hierarchy --class BaseController
   ```

## Tips

- Always check `rag-mcp-cli status` before searching to ensure indexing is complete
- Use `rag` for conceptual queries, `semantic` for exact symbol names
- Combine commands: semantic to find, hierarchy to trace, list to explore
- Use `-L` to limit output verbosity in large codebases
- Use `--verbose` during indexing to show progress

The RAG MCP server is automatically started for you in the working directory.
""".strip()


def get_rag_help_text() -> str:
    """Return the RAG MCP help text to inject into agent prompts.
    
    :returns: Formatted RAG help text string.
    :rtype: str
    """
    return _RAG_HELP_TEXT


def start_rag_server(repo_path: str, verbose: bool = False, container_name: str = "") -> Optional[int]:
    """Start the RAG MCP server in the background for a given repository.

    The server will automatically index the repository on startup.
    PID and log files are stored in the repository root directory.

    :param repo_path: Absolute path to the git repository root (or "/workspace" in Docker).
    :param verbose: When True, log progress messages to stdout.
    :param container_name: If set, start the server inside this Docker container.
    :returns: Server PID if started successfully, None if already running or failed.
    :rtype: int or None
    """
    if not os.path.isdir(RAG_TOOL_DIR):
        if verbose:
            print(f"      [RAG] RAG tool directory not found: {RAG_TOOL_DIR}")
        return None
    
    # Determine the actual path for PID file
    if container_name:
        # In Docker, we use /workspace as the repo path
        pid_file_path = "/workspace/.rag-mcp-server.pid"
    else:
        pid_file_path = str(Path(repo_path) / ".rag-mcp-server.pid")
    
    # Check if server is already running for this repo
    pid_file = Path(pid_file_path)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            # For Docker, we can't easily check if process exists, so assume running
            if not container_name:
                os.kill(pid, 0)
            if verbose:
                print(f"      [RAG] Server already running (PID: {pid})")
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            pid_file.unlink(missing_ok=True)
    
    if verbose:
        print(f"      [RAG] Starting MCP server in {repo_path}...")
    
    try:
        # Start server in background
        if container_name:
            # In Docker, start via docker exec
            log_file = "/workspace/.rag-mcp-server.log"
            docker_cmd = [
                "docker", "exec", "-d",
                "--workdir", "/workspace",
                container_name,
                sys.executable, "-m", RAG_CLI_MODULE, "serve"
            ]
            subprocess.run(docker_cmd, check=True, capture_output=True)
            # We can't get the actual PID inside the container easily, so use a placeholder
            pid = -1
        else:
            # Host execution
            log_file = str(Path(repo_path) / ".rag-mcp-server.log")
            with open(log_file, "w") as log_fh:
                proc = subprocess.Popen(
                    [sys.executable, "-m", RAG_CLI_MODULE, "serve"],
                    cwd=repo_path,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            pid = proc.pid
        
        # Write PID file
        if container_name:
            # Write PID file inside container
            docker_write_pid = [
                "docker", "exec",
                "--workdir", "/workspace",
                container_name,
                "bash", "-c", f"echo {pid} > {pid_file_path}"
            ]
            subprocess.run(docker_write_pid, check=True, capture_output=True)
        else:
            pid_file.write_text(str(pid))
        
        if verbose:
            print(f"      [RAG] Server started (PID: {pid})")
        
        return pid
    except Exception as e:
        if verbose:
            print(f"      [RAG] Failed to start server: {e}")
        return None


def stop_rag_server(repo_path: str, verbose: bool = False) -> bool:
    """Stop the RAG MCP server for a given repository.
    
    :param repo_path: Absolute path to the git repository root.
    :param verbose: When True, log progress messages to stdout.
    :returns: True if server was stopped, False if not running or failed.
    :rtype: bool
    """
    pid_file = Path(repo_path) / ".rag-mcp-server.pid"
    if not pid_file.exists():
        if verbose:
            print(f"      [RAG] No server running (PID file not found)")
        return False
    
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        if verbose:
            print(f"      [RAG] Stopping server (PID: {pid})...")
        os.kill(pid, 15)  # SIGTERM
        pid_file.unlink(missing_ok=True)
        if verbose:
            print(f"      [RAG] Server stopped")
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        pid_file.unlink(missing_ok=True)
        if verbose:
            print(f"      [RAG] Server not running (cleaned up stale PID file)")
        return False
    except Exception as e:
        if verbose:
            print(f"      [RAG] Failed to stop server: {e}")
        return False


def wait_for_rag_indexing(repo_path: str, timeout: int = 120, verbose: bool = False) -> bool:
    """Wait for RAG MCP server to complete indexing.
    
    Polls the server status until indexing is complete or timeout is reached.
    
    :param repo_path: Absolute path to the git repository root.
    :param timeout: Maximum seconds to wait for indexing.
    :param verbose: When True, log progress messages to stdout.
    :returns: True if indexing completed, False if timeout or failed.
    :rtype: bool
    """
    import time
    
    if verbose:
        print(f"      [RAG] Waiting for indexing to complete...")
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = subprocess.run(
                [sys.executable, "-m", RAG_CLI_MODULE, "status"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if "Running" in result.stdout and "is_indexing: false" in result.stdout.lower():
                if verbose:
                    print(f"      [RAG] Indexing complete")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        time.sleep(2)
    
    if verbose:
        print(f"      [RAG] Indexing timeout after {timeout}s")
    return False
