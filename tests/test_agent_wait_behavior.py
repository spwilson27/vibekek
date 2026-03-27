"""E2E tests for the agent-slot waiting behaviour in run_agent.

When every agent in the pool is either at capacity or quota-suppressed,
run_agent must wait indefinitely rather than failing immediately.  These
tests verify:

  - acquire() returning None is never treated as a fatal error.
  - A human-readable "waiting" message is emitted on each failed acquire.
  - The dashboard task status is set to "waiting" while blocked.
  - Execution proceeds normally once a slot becomes available.
  - Multiple consecutive None returns are tolerated.
  - Integration: a slot freed by a concurrent thread unblocks the call.
"""

import sys
import os
import threading
import pytest
from unittest.mock import MagicMock, patch, mock_open

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.executor import run_agent
from workflow_lib.agent_pool import AgentConfig, AgentPoolManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent_cfg(**overrides) -> AgentConfig:
    defaults = dict(
        name="test-agent", backend="gemini", user="mrwilson",
        parallel=1, priority=1, quota_time=60,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _call_run_agent(pool, dashboard=None) -> bool:
    """Invoke run_agent with all real I/O mocked out."""
    context = {"task_name": "01_foo.md", "phase_filename": "phase_4"}
    with patch("builtins.open", mock_open(read_data="prompt {task_name}")), \
         patch("workflow_lib.executor.run_ai_command", return_value=(0, "")), \
         patch("workflow_lib.executor.get_project_images", return_value=[]), \
         patch("workflow_lib.executor._set_dir_owner"), \
         patch("workflow_lib.executor._set_cargo_target_dir"), \
         patch("workflow_lib.executor._get_cargo_target_dir", return_value=None):
        return run_agent(
            "Review", "review_task.md", context,
            "/tmp/fake_cwd", backend="gemini",
            dashboard=dashboard,
            task_id="phase_4/test/01_foo.md",
            agent_pool=pool,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRunAgentWaitsForSlot:
    """run_agent must wait for an available agent slot rather than failing."""

    def test_succeeds_when_slot_available_on_second_acquire(self):
        """A None followed by a valid agent config results in success."""
        agent_cfg = _make_agent_cfg()
        pool = MagicMock()
        pool.acquire.side_effect = [None, agent_cfg]
        pool.release = MagicMock()

        result = _call_run_agent(pool)

        assert result is True
        assert pool.acquire.call_count == 2

    def test_does_not_return_false_on_first_none(self):
        """A single None from acquire must not cause run_agent to return False."""
        agent_cfg = _make_agent_cfg()
        pool = MagicMock()
        pool.acquire.side_effect = [None, agent_cfg]
        pool.release = MagicMock()

        result = _call_run_agent(pool)

        assert result is True

    def test_tolerates_multiple_consecutive_none_cycles(self):
        """run_agent keeps retrying even after several consecutive None returns."""
        agent_cfg = _make_agent_cfg()
        pool = MagicMock()
        pool.acquire.side_effect = [None, None, None, agent_cfg]
        pool.release = MagicMock()

        result = _call_run_agent(pool)

        assert result is True
        assert pool.acquire.call_count == 4

    def test_logs_waiting_message_on_dashboard(self):
        """A human-readable wait message is logged to the dashboard on each None."""
        agent_cfg = _make_agent_cfg()
        pool = MagicMock()
        pool.acquire.side_effect = [None, agent_cfg]
        pool.release = MagicMock()
        dashboard = MagicMock()

        _call_run_agent(pool, dashboard=dashboard)

        logged = [call.args[0] for call in dashboard.log.call_args_list]
        assert any("waiting" in msg.lower() or "busy" in msg.lower() for msg in logged), (
            f"Expected a waiting/busy message in dashboard logs, got: {logged}"
        )

    def test_dashboard_status_set_to_waiting(self):
        """Dashboard set_agent is called with status='waiting' while blocked."""
        agent_cfg = _make_agent_cfg()
        pool = MagicMock()
        pool.acquire.side_effect = [None, agent_cfg]
        pool.release = MagicMock()
        dashboard = MagicMock()

        _call_run_agent(pool, dashboard=dashboard)

        statuses = [call.args[2] for call in dashboard.set_agent.call_args_list]
        assert "waiting" in statuses, (
            f"Expected 'waiting' in dashboard set_agent statuses, got: {statuses}"
        )

    def test_dashboard_waiting_message_names_agent_slot(self):
        """The dashboard waiting detail text mentions agent slot availability."""
        agent_cfg = _make_agent_cfg()
        pool = MagicMock()
        pool.acquire.side_effect = [None, agent_cfg]
        pool.release = MagicMock()
        dashboard = MagicMock()

        _call_run_agent(pool, dashboard=dashboard)

        # Find the set_agent call with status="waiting" triggered by the None acquire
        waiting_details = [
            call.args[3]
            for call in dashboard.set_agent.call_args_list
            if len(call.args) > 2 and call.args[2] == "waiting"
            and "slot" in call.args[3].lower()
        ]
        assert waiting_details, (
            "Expected a set_agent('waiting', ...) call mentioning 'slot'"
        )

    def test_agent_released_after_success(self):
        """The acquired agent config is always released back to the pool."""
        agent_cfg = _make_agent_cfg()
        pool = MagicMock()
        pool.acquire.side_effect = [None, agent_cfg]
        pool.release = MagicMock()

        _call_run_agent(pool)

        pool.release.assert_called_once_with(agent_cfg, quota_exhausted=False)


class TestRunAgentRealPoolIntegration:
    """Integration tests using a real AgentPoolManager."""

    def test_unblocks_when_concurrent_thread_frees_slot(self):
        """run_agent resolves once another thread releases the only pool slot.

        This is the scenario that caused the original failure: all review agents
        were at capacity when implementation finished, so the acquire timed out
        and the task was killed.  Now run_agent must wait for notify_all() from
        pool.release() and then proceed.
        """
        agent_cfg = _make_agent_cfg(parallel=1)
        pool = AgentPoolManager([agent_cfg])

        # Saturate the single slot so the next acquire() blocks.
        held = pool.acquire(timeout=1.0)
        assert held is not None, "Could not acquire the initial slot for setup"

        # Release the slot from a background thread after a short delay —
        # pool.release() calls notify_all() which wakes acquire() immediately.
        def release_after_delay():
            import time
            time.sleep(0.05)
            pool.release(held)

        t = threading.Thread(target=release_after_delay, daemon=True)
        t.start()

        result = _call_run_agent(pool)
        t.join(timeout=5)

        assert result is True
        assert not t.is_alive(), "Release thread did not complete in time"
