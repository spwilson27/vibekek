"""Global test safety net: block real agent CLI invocations."""
import subprocess
import pytest
from unittest.mock import patch

BLOCKED_COMMANDS = {"gemini", "claude", "copilot"}


def _make_guard(original):
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


@pytest.fixture(autouse=True)
def block_agent_cli_calls():
    """Autouse fixture that prevents any unmocked agent CLI subprocess calls."""
    real_run = subprocess.run
    real_popen = subprocess.Popen

    with patch("subprocess.run", side_effect=_make_guard(real_run)), \
         patch("subprocess.Popen", side_effect=_make_guard(real_popen)):
        yield
