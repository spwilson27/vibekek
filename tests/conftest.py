"""Global test safety net: block real agent CLI invocations and host filesystem writes."""
import os
import builtins
import subprocess
import tempfile
import pytest
from unittest.mock import patch

BLOCKED_COMMANDS = {"gemini", "claude", "copilot"}

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
