"""Global test safety net: block real agent CLI invocations and host filesystem writes."""
import os
import builtins
import subprocess
import tempfile
import pytest
from unittest.mock import patch

BLOCKED_COMMANDS = {"gemini", "claude", "copilot", "opencode", "cline", "aider", "codex", "qwen"}

# Save before any mocking so fixtures can call real git regardless of subprocess patches.
_real_subprocess_run = subprocess.run

# Paths that tests must not write to without explicit mocking
_TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
_TOOLS_DIR = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_PROJECT_DIR = os.path.abspath(os.path.join(_TOOLS_DIR, ".."))
_TEMP_DIR = tempfile.gettempdir()

_real_open = builtins.open


def _make_subprocess_guard(original):
    """Wrap a subprocess callable to block agent CLI commands."""
    def guarded(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if cmd:
            executable = cmd[0] if isinstance(cmd, (list, tuple)) else cmd.split()[0]
            if executable in BLOCKED_COMMANDS:
                raise RuntimeError(
                    f"Test tried to invoke agent CLI '{executable}' without mocking! "
                    f"Full command: {cmd}"
                )
        return original(*args, **kwargs)
    return guarded


def _guarded_open(file, mode="r", *args, **kwargs):
    """Block writes to project paths; allow everything else through."""
    if any(c in mode for c in ("w", "a", "x")):
        path = os.path.abspath(str(file))
        if path.startswith(_PROJECT_DIR) and not path.startswith(_TEMP_DIR):
            raise RuntimeError(
                f"Test tried to write to host filesystem without mocking: {path}\n"
                f"Wrap in: with patch('builtins.open', mock_open()): ..."
            )
    return _real_open(file, mode, *args, **kwargs)


@pytest.fixture(autouse=True)
def _host_protection():
    """Prevent tests from writing to the host filesystem or invoking agent CLIs."""
    with patch("subprocess.run", side_effect=_make_subprocess_guard(subprocess.run)), \
         patch("subprocess.Popen", side_effect=_make_subprocess_guard(subprocess.Popen)), \
         patch("builtins.open", new=_guarded_open):
        yield


@pytest.fixture(autouse=True)
def _no_root_branch_change():
    """Assert that no test changes the git branch of the project root directory.

    Uses the saved real subprocess.run reference so it bypasses any mocking
    applied by _host_protection.  This catches regressions like `git rebase
    <upstream> <branch>` or `git checkout <branch>` running against the host
    repo instead of an isolated tmpdir clone.
    """
    res = _real_subprocess_run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=_PROJECT_DIR, capture_output=True, text=True,
    )
    branch_before = res.stdout.strip() if res.returncode == 0 else None
    yield
    if branch_before is None:
        return
    res_after = _real_subprocess_run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=_PROJECT_DIR, capture_output=True, text=True,
    )
    branch_after = res_after.stdout.strip() if res_after.returncode == 0 else None
    assert branch_before == branch_after, (
        f"A test changed the git branch of the project root from "
        f"'{branch_before}' to '{branch_after}'. "
        f"All git operations that touch checkout state must use an isolated tmpdir clone."
    )
