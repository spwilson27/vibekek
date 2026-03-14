"""Shared pytest fixtures for MCP Tools tests."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


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

    # Create sample Python files
    (repo_path / "main.py").write_text("""
def main():
    print("Hello, World!")

if __name__ == "__main__":
    main()
""")

    (repo_path / "utils.py").write_text("""
def helper():
    return "I'm a helper function"

def another_helper():
    return "I'm another helper"
""")

    (repo_path / "calculator.py").write_text("""
class Calculator:
    def __init__(self):
        self.value = 0

    def add(self, x):
        self.value += x
        return self.value

    def subtract(self, x):
        self.value -= x
        return self.value
""")

    # Commit files
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    return repo_path


@pytest.fixture
def sample_code_files(temp_dir):
    """Create sample code files for testing."""
    base_dir = temp_dir / "code-samples"
    base_dir.mkdir()

    # Python file with various symbols
    python_file = base_dir / "sample.py"
    python_file.write_text("""
# Sample Python file for testing

CONSTANT = 42

def simple_function():
    return "Hello"

def function_with_args(a, b, c=10):
    return a + b + c

class SampleClass:
    def __init__(self):
        self.value = 0

    def method(self):
        return self.value

    @staticmethod
    def static_method():
        return "static"

@decorator
def decorated_function():
    pass
""")

    # JavaScript file
    js_file = base_dir / "sample.js"
    js_file.write_text("""
// Sample JavaScript file

const PI = 3.14159;

function greet(name) {
    return `Hello, ${name}!`;
}

class Person {
    constructor(name) {
        this.name = name;
    }

    sayHello() {
        return greet(this.name);
    }
}

const arrowFunc = (x) => x * 2;
""")

    return base_dir


@pytest.fixture
def config_file(temp_dir):
    """Create a default config file."""
    config_path = temp_dir / "config.json"
    config_path.write_text("""
{
    "repo_path": null,
    "tools": {
        "rag": {
            "enabled": true,
            "index_dir": "/tmp/rag-test-{hash}",
            "limits": {
                "max_files": 100,
                "max_chunks": 1000
            }
        },
        "semantic": {
            "enabled": true,
            "index_dir": "/tmp/semantic-test-{hash}",
            "limits": {
                "max_files": 100
            }
        }
    }
}
""")
    return config_path
