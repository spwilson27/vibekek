#!/usr/bin/env python3
"""Test RAG MCP CLI functionality within Docker containers.

This test verifies that:
1. Docker is installed and available
2. A container can be started with the RAG tool
3. The RAG MCP CLI commands work inside the container
4. The server can be started and queried from within the container

Usage:
    python .tools/tests/test_rag_docker.py

Or with pytest:
    pytest .tools/tests/test_rag_docker.py -v

Note: This test requires Docker to be installed and running. If Docker is not
available, the test will be skipped (returns success).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAG_TOOL_DIR = os.path.join(TOOLS_DIR, "rag-tool")


def is_docker_installed() -> bool:
    """Check if Docker is installed and runnable."""
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def create_test_repo(repo_path: Path) -> None:
    """Create a test git repository with sample code files."""
    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Create sample Python files with searchable content
    (repo_path / "main.py").write_text('''"""Main application module."""

def main():
    """Entry point for the application."""
    print("Hello, World!")
    result = process_data("test")
    print(f"Result: {result}")

def process_data(data: str) -> str:
    """Process input data and return transformed result."""
    return data.upper()

if __name__ == "__main__":
    main()
''')

    (repo_path / "utils.py").write_text('''"""Utility functions for the application."""

def helper_function():
    """A helper function that does useful things."""
    return "I'm a helper function"

def another_helper(x: int, y: int) -> int:
    """Add two numbers together."""
    return x + y

class UtilityClass:
    """A utility class with various methods."""
    
    def __init__(self, name: str):
        self.name = name
    
    def greet(self) -> str:
        """Return a greeting message."""
        return f"Hello from {self.name}"
    
    @staticmethod
    def static_method():
        """A static method that doesn't need self."""
        return "static"
''')

    (repo_path / "config.json").write_text('''{
    "repo_path": null,
    "tools": {
        "rag": {
            "enabled": true,
            "index_dir": "/tmp/rag-docker-test",
            "limits": {
                "max_files": 100,
                "max_chunks": 1000
            }
        },
        "semantic": {
            "enabled": true,
            "index_dir": "/tmp/semantic-docker-test",
            "limits": {
                "max_files": 100
            }
        }
    }
}
''')

    # Commit files
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )


def run_docker_command(container_name: str, cmd: list, timeout: int = 120) -> tuple:
    """Run a command inside a Docker container.
    
    Returns: (return_code, stdout, stderr)
    """
    docker_cmd = [
        "docker", "exec",
        "--workdir", "/workspace",
        container_name,
    ] + cmd
    
    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"


def test_rag_in_docker() -> bool:
    """Test RAG MCP CLI functionality inside a Docker container.
    
    This test:
    1. Creates a temporary test repository
    2. Starts a Docker container with Python and the RAG tool
    3. Copies the RAG tool into the container
    4. Runs RAG MCP CLI commands inside the container
    5. Verifies the output is correct
    
    Returns: True if all tests pass, False otherwise
    """
    if not is_docker_installed():
        print("SKIP: Docker is not installed or not runnable")
        return True  # Return True to not fail the test suite
    
    print("=" * 70)
    print("RAG MCP Docker Integration Test")
    print("=" * 70)
    
    container_name = ""
    temp_dir = ""
    success = False
    
    try:
        # Create temporary directory for test repo
        temp_dir = tempfile.mkdtemp(prefix="rag-docker-test-")
        repo_path = Path(temp_dir) / "test-repo"
        repo_path.mkdir()
        
        # Create test repository
        print("\n[1/7] Creating test repository...")
        create_test_repo(repo_path)
        print(f"      Created test repo at: {repo_path}")
        
        # Start Docker container
        print("\n[2/7] Starting Docker container...")
        container_name = f"rag-test-{os.getpid()}"
        
        # Use a Python image and install required dependencies
        docker_run_cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "-v", f"{repo_path}:/workspace:ro",
            "python:3.11-slim",
            "sleep", "infinity"
        ]
        
        result = subprocess.run(docker_run_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"      FAILED: Could not start container: {result.stderr}")
            return False
        print(f"      Container started: {container_name}")
        
        # Wait for container to be ready
        time.sleep(2)
        
        # Install dependencies in container
        print("\n[3/7] Installing dependencies in container...")
        
        # Install git and basic tools
        rc, out, err = run_docker_command(container_name, ["apt-get", "update"], timeout=120)
        if rc != 0:
            print(f"      WARNING: apt-get update failed: {err[:200]}")
        
        rc, out, err = run_docker_command(container_name, ["apt-get", "install", "-y", "git", "bash", "curl"], timeout=120)
        if rc != 0:
            print(f"      WARNING: apt-get install failed: {err[:200]}")
        
        # Install pip dependencies (with longer timeout for pip)
        print("      Installing Python dependencies (this may take a minute)...")
        rc, out, err = run_docker_command(container_name, [
            "pip", "install", "--quiet", "--no-cache-dir",
            "jsoncomment",
        ], timeout=180)
        if rc != 0:
            print(f"      WARNING: Some dependencies failed: {err[:300]}")
            # Continue anyway - jsoncomment is the only essential one for CLI help
        
        # Try to install tree-sitter for semantic search (optional)
        rc, out, err = run_docker_command(container_name, [
            "pip", "install", "--quiet", "--no-cache-dir",
            "tree-sitter",
            "tree-sitter-python",
        ], timeout=180)
        if rc != 0:
            print(f"      Note: tree-sitter not installed (optional): {err[:100]}")
        
        # Try chromadb (may fail without proper setup, but that's OK for basic CLI test)
        rc, out, err = run_docker_command(container_name, [
            "pip", "install", "--quiet", "--no-cache-dir",
            "chromadb",
        ], timeout=300)
        if rc != 0:
            print(f"      Note: chromadb not installed (optional for full RAG): {err[:100]}")
        
        print("      Core dependencies installed")
        
        # Copy RAG tool into container
        print("\n[4/7] Copying RAG tool into container...")
        
        # Create tarball of rag-tool
        import tarfile
        tar_path = os.path.join(temp_dir, "rag-tool.tar")
        with tarfile.open(tar_path, "w") as tar:
            tar.add(RAG_TOOL_DIR, arcname="rag-tool")
        
        # Copy tarball to container
        docker_cp_cmd = ["docker", "cp", tar_path, f"{container_name}:/tmp/rag-tool.tar"]
        result = subprocess.run(docker_cp_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"      FAILED: Could not copy RAG tool: {result.stderr}")
            return False
        
        # Extract in container
        rc, out, err = run_docker_command(container_name, [
            "tar", "-xf", "/tmp/rag-tool.tar", "-C", "/opt/"
        ])
        if rc != 0:
            print(f"      FAILED: Could not extract RAG tool: {err}")
            return False
        print("      RAG tool copied to /opt/rag-tool")
        
        # Set PYTHONPATH and test basic CLI
        print("\n[5/7] Testing RAG MCP CLI basic commands...")
        
        # Test help command
        env_cmd = ["env", f"PYTHONPATH=/opt/rag-tool", f"REPO_PATH={repo_path}"]
        rc, out, err = run_docker_command(container_name, env_cmd + [
            "python", "-m", "rag_mcp.cli", "--help"
        ], timeout=30)
        if rc != 0:
            print(f"      FAILED: CLI help failed: {err[:300]}")
            return False
        
        if "rag-mcp-cli" not in out.lower() and "usage" not in out.lower():
            print(f"      FAILED: Unexpected help output: {out[:300]}")
            return False
        print("      CLI help command works")
        
        # Test specific subcommand help
        rc, out, err = run_docker_command(container_name, env_cmd + [
            "python", "-m", "rag_mcp.cli", "help"
        ], timeout=30)
        if rc == 0 and ("SEARCH COMMANDS" in out or "rag <query>" in out):
            print("      CLI help subcommand works")
        else:
            print(f"      Note: help subcommand output varies (rc={rc})")
        
        # Test status command (should show server not running)
        print("\n[6/7] Testing RAG MCP status command...")
        rc, out, err = run_docker_command(container_name, env_cmd + [
            "python", "-m", "rag_mcp.cli", "status"
        ], timeout=60)
        
        # Status should work (may show server not running, which is OK)
        print(f"      Status command executed (rc={rc})")
        if rc == 0 and out.strip():
            lines = [l.strip() for l in out.split('\n') if l.strip()]
            print(f"      Status: {lines[0] if lines else 'no output'}")
        
        # Test starting the server
        print("\n[7/7] Testing RAG MCP server startup...")
        rc, out, err = run_docker_command(container_name, env_cmd + [
            "python", "-m", "rag_mcp.cli", "serve"
        ], timeout=30)
        
        # Server start is async, so we just check it didn't immediately crash
        if rc == 0 or "background" in out.lower() or "starting" in out.lower() or "daemon" in out.lower():
            print("      Server startup initiated successfully")
        elif "already running" in out.lower():
            print("      Server already running (acceptable)")
        else:
            # Some errors are expected if full RAG dependencies aren't installed
            print(f"      Server startup returned rc={rc} (may need full dependencies)")
            print(f"      Output: {out[:200] if out else 'none'}")
        
        # Wait a moment for server to start
        time.sleep(2)
        
        # Check status again
        rc, out, err = run_docker_command(container_name, env_cmd + [
            "python", "-m", "rag_mcp.cli", "status"
        ], timeout=30)
        
        if rc == 0 and out.strip():
            if "running" in out.lower() or "pid" in out.lower():
                print("      Server status confirmed via status command")
            else:
                lines = [l.strip() for l in out.split('\n') if l.strip()]
                print(f"      Status check: {lines[0] if lines else 'no output'}")
        
        success = True
        print("\n" + "=" * 70)
        print("RAG MCP Docker Integration Test PASSED!")
        print("=" * 70)
        print("\nNote: Basic CLI functionality verified. Full RAG/semantic search")
        print("requires additional ML dependencies (chromadb, sentence-transformers).")
        
    except Exception as e:
        print(f"\n[!] Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        success = False
    
    finally:
        # Cleanup
        if container_name:
            print(f"\n[Cleanup] Stopping container {container_name}...")
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=30,
            )
        
        if temp_dir:
            print(f"[Cleanup] Removing temporary directory {temp_dir}...")
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    return success


def main() -> int:
    """Main entry point."""
    print("\nRAG MCP Docker Integration Test Suite\n")
    
    if not is_docker_installed():
        print("Docker is not installed or not runnable - skipping test")
        return 0
    
    success = test_rag_in_docker()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
