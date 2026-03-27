"""E2E tests for agent name inclusion in failure log messages.

When an agent process fails (non-zero exit, prompt too long, etc.), the log
output must include the agent name so operators can identify which backend
was responsible.  These tests exercise `run_agent` with mocked runners that
simulate failures and verify the logged messages contain agent identifiers.
"""
import os
import subprocess
import sys
import pytest
from unittest.mock import MagicMock, patch, ANY

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.executor import run_agent
from workflow_lib.agent_pool import AgentConfig, AgentPoolManager, QUOTA_RETURN_CODE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal valid prompt template — run_agent reads from .tools/prompts/
_PROMPT_TEMPLATE = "Do the task: {task_name}"

_TASK_CONTEXT = {
    "phase_filename": "phase_0",
    "task_name": "test_task",
    "target_dir": "phase_0/test_task",
}


def _mock_runner(returncode=1, stderr=""):
    """Return a mock runner whose .run() returns *returncode* with *stderr*."""
    runner = MagicMock()

    def fake_run(cwd, prompt, image_paths=None, on_line=None, timeout=None, abort_event=None):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)

    runner.run.side_effect = fake_run
    return runner


def _run_agent_with_pool(agent_name, returncode=1, stderr="", dashboard=None):
    """Call run_agent with a single-agent pool and return (result, dashboard)."""
    cfg = AgentConfig(agent_name, "gemini", "testuser", parallel=1, priority=1, quota_time=60)
    pool = AgentPoolManager([cfg])

    if dashboard is None:
        dashboard = MagicMock()
        dashboard.log = MagicMock()
        dashboard.set_agent = MagicMock()

    runner = _mock_runner(returncode=returncode, stderr=stderr)

    with patch("workflow_lib.executor.make_runner", return_value=runner), \
         patch("workflow_lib.executor.TOOLS_DIR", os.path.dirname(__file__)), \
         patch("workflow_lib.executor.get_project_images", return_value=[]), \
         patch("workflow_lib.config.get_config_defaults", return_value={}), \
         patch("builtins.open", MagicMock(return_value=MagicMock(
             __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=_PROMPT_TEMPLATE))),
             __exit__=MagicMock(return_value=False),
         ))):
        result = run_agent(
            "Implementation", "implement_task.md", _TASK_CONTEXT, "/tmp",
            dashboard=dashboard, task_id="phase_0/test_task",
            agent_pool=pool,
        )

    return result, dashboard


def _run_agent_no_pool(backend="gemini", returncode=1, stderr="", dashboard=None):
    """Call run_agent without a pool (fixed backend) and return (result, dashboard)."""
    if dashboard is None:
        dashboard = MagicMock()
        dashboard.log = MagicMock()
        dashboard.set_agent = MagicMock()

    runner = _mock_runner(returncode=returncode, stderr=stderr)

    with patch("workflow_lib.executor.make_runner", return_value=runner), \
         patch("workflow_lib.executor.TOOLS_DIR", os.path.dirname(__file__)), \
         patch("workflow_lib.executor.get_project_images", return_value=[]), \
         patch("workflow_lib.config.get_config_defaults", return_value={}), \
         patch("builtins.open", MagicMock(return_value=MagicMock(
             __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=_PROMPT_TEMPLATE))),
             __exit__=MagicMock(return_value=False),
         ))):
        result = run_agent(
            "Implementation", "implement_task.md", _TASK_CONTEXT, "/tmp",
            backend=backend, dashboard=dashboard, task_id="phase_0/test_task",
        )

    return result, dashboard


def _all_log_text(dashboard):
    """Concatenate all dashboard.log() call args into one string."""
    return "\n".join(
        call.args[0] for call in dashboard.log.call_args_list if call.args
    )


# ---------------------------------------------------------------------------
# Tests: agent name in failure logs (with pool)
# ---------------------------------------------------------------------------

class TestAgentNameInFailureLogs:
    def test_fatal_message_includes_agent_name(self):
        """When agent fails, FATAL log line must include agent=<name>."""
        result, dashboard = _run_agent_with_pool("my-gemini-flash", returncode=1)
        assert result is False
        log_text = _all_log_text(dashboard)
        assert "agent=my-gemini-flash" in log_text
        assert "FATAL" in log_text

    def test_stderr_message_includes_agent_name(self):
        """When agent fails with stderr, stderr log line must include agent=<name>."""
        result, dashboard = _run_agent_with_pool(
            "my-claude-sonnet", returncode=1, stderr="prompt too long"
        )
        assert result is False
        log_text = _all_log_text(dashboard)
        assert "agent=my-claude-sonnet" in log_text
        assert "prompt too long" in log_text

    def test_agent_selected_message_logged(self):
        """On first attempt, an 'Agent selected' log line with the name must be emitted."""
        result, dashboard = _run_agent_with_pool("pool-agent-1", returncode=1)
        log_text = _all_log_text(dashboard)
        assert "pool-agent-1" in log_text
        assert "Agent selected" in log_text


# ---------------------------------------------------------------------------
# Tests: backend in failure logs (no pool)
# ---------------------------------------------------------------------------

class TestBackendInFailureLogs:
    def test_fatal_message_includes_backend_when_no_pool(self):
        """Without a pool, FATAL log line must include agent=<backend>."""
        result, dashboard = _run_agent_no_pool(backend="claude", returncode=1)
        assert result is False
        log_text = _all_log_text(dashboard)
        assert "agent=claude" in log_text
        assert "FATAL" in log_text

    def test_starting_message_includes_backend(self):
        """The 'Starting agent' line must include backend= info."""
        result, dashboard = _run_agent_no_pool(backend="codex", returncode=0)
        log_text = _all_log_text(dashboard)
        assert "backend=codex" in log_text


# ---------------------------------------------------------------------------
# Tests: success case still works
# ---------------------------------------------------------------------------

class TestSuccessCase:
    def test_success_returns_true_with_pool(self):
        result, _ = _run_agent_with_pool("good-agent", returncode=0)
        assert result is True

    def test_success_returns_true_no_pool(self):
        result, _ = _run_agent_no_pool(returncode=0)
        assert result is True
