"""Mypy static type-checking test.

Verifies that all Python source files in ``workflow_lib/`` pass mypy's type
checker with no errors.  The test is skipped automatically when mypy is not
installed in the current Python environment.
"""

import subprocess
import sys
import os
import pytest


WORKFLOW_LIB_DIR = os.path.join(os.path.dirname(__file__), "..", "workflow_lib")


@pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-m", "mypy", "--version"],
        capture_output=True,
    ).returncode != 0,
    reason="mypy is not installed",
)
def test_mypy_workflow_lib():
    """Run mypy against workflow_lib/ and assert no type errors are found.

    Uses ``--ignore-missing-imports`` so that third-party packages without
    stubs (e.g. the AI CLI runners) do not cause false failures.  The test
    fails if mypy exits with a non-zero return code and prints the full mypy
    output for diagnosis.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            WORKFLOW_LIB_DIR,
            "--ignore-missing-imports",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"mypy found type errors in workflow_lib/:\n"
        f"{result.stdout}\n{result.stderr}"
    )
