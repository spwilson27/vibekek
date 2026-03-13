"""End-to-end tests for quota detection in run_ai_command.

These tests drive the full output_line → quota_detected pipeline by patching
the runner so it emits controlled stderr lines, then asserting whether
run_ai_command returns QUOTA_RETURN_CODE or passes through cleanly.
"""
import subprocess
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.executor import run_ai_command
from workflow_lib.agent_pool import QUOTA_RETURN_CODE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner_emitting(lines):
    """Return a mock runner whose .run() feeds *lines* through on_line."""
    mock_runner = MagicMock()

    def fake_run(cwd, prompt, image_paths=None, on_line=None, timeout=None, abort_event=None):
        for line in lines:
            if on_line:
                on_line(line)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    mock_runner.run.side_effect = fake_run
    return mock_runner


def _run(lines, spawn_rate=0.0):
    """Call run_ai_command with a mocked runner emitting *lines*."""
    mock_runner = _make_runner_emitting(lines)
    with patch("workflow_lib.executor.make_runner", return_value=mock_runner), \
         patch("workflow_lib.config.get_config_defaults", return_value={}):
        return run_ai_command("prompt", "/tmp", spawn_rate=spawn_rate)


def _run_capture_abort(lines, spawn_rate=0.0):
    """Like _run but also returns whether abort_event was set during the run."""
    abort_was_set = [False]
    mock_runner = MagicMock()

    def fake_run(cwd, prompt, image_paths=None, on_line=None, timeout=None, abort_event=None):
        for line in lines:
            if on_line:
                on_line(line)
        if abort_event and abort_event.is_set():
            abort_was_set[0] = True
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    mock_runner.run.side_effect = fake_run
    with patch("workflow_lib.executor.make_runner", return_value=mock_runner), \
         patch("workflow_lib.config.get_config_defaults", return_value={}):
        rc, _ = run_ai_command("prompt", "/tmp", spawn_rate=spawn_rate)
    return rc, abort_was_set[0]


# ---------------------------------------------------------------------------
# Hard quota — should always kill and rotate
# ---------------------------------------------------------------------------

class TestHardQuota:
    def test_resource_exhausted_kills(self):
        rc, _ = _run(["[stderr] RESOURCE_EXHAUSTED: quota exceeded"])
        assert rc == QUOTA_RETURN_CODE

    def test_model_capacity_exhausted_kills(self):
        rc, _ = _run(["[stderr] MODEL_CAPACITY_EXHAUSTED"])
        assert rc == QUOTA_RETURN_CODE

    def test_exhausted_your_capacity_no_reset_kills(self):
        rc, _ = _run(["[stderr] You have exhausted your capacity on this model."])
        assert rc == QUOTA_RETURN_CODE

    def test_reset_12h_exceeds_spawn_rate_kills(self):
        line = "[stderr] You have exhausted your capacity on this model. Your quota will reset after 12h."
        rc, _ = _run([line], spawn_rate=120.0)
        assert rc == QUOTA_RETURN_CODE

    def test_reset_30m_exceeds_spawn_rate_kills(self):
        line = "[stderr] exhausted your capacity. Your quota will reset after 30m."
        rc, _ = _run([line], spawn_rate=60.0)
        assert rc == QUOTA_RETURN_CODE

    def test_non_quota_line_passes_through(self):
        rc, _ = _run(["[stderr] some unrelated error message"])
        assert rc != QUOTA_RETURN_CODE


# ---------------------------------------------------------------------------
# Transient quota — CLI is retrying, should NOT kill
# ---------------------------------------------------------------------------

class TestTransientQuota:
    def test_retrying_after_suppresses_kill(self):
        """Exact line from the Gemini CLI retry log."""
        line = (
            "[stderr] Attempt 1 failed: You have exhausted your capacity on this model. "
            "Your quota will reset after 0s.. Retrying after 5306ms..."
        )
        rc, _ = _run([line], spawn_rate=0.0)
        assert rc != QUOTA_RETURN_CODE

    def test_retry_after_variant_suppresses_kill(self):
        rc, _ = _run(["[stderr] exhausted your capacity. Retry after 2000ms."])
        assert rc != QUOTA_RETURN_CODE

    def test_retrying_with_backoff_suppresses_kill(self):
        """'Retrying with backoff' on same line as quota pattern."""
        line = "[stderr] Attempt 1 failed with status 429. Retrying with backoff... No capacity available for model gemini-flash"
        rc, _ = _run([line])
        assert rc != QUOTA_RETURN_CODE

    def test_multiline_gemini_retry_block_suppresses_kill(self):
        """
        Real Gemini CLI output: 'Retrying with backoff' on line 1,
        'No capacity available' on line 4. Cross-line window must suppress.
        """
        lines = [
            '[stderr] Attempt 1 failed with status 429. Retrying with backoff... GaxiosError: [{',
            '[stderr]   "error": {',
            '[stderr]     "code": 429,',
            '[stderr]     "message": "No capacity available for model gemini-3-flash-preview on the server",',
            '[stderr]   }',
            '[stderr] }]',
        ]
        rc, _ = _run(lines)
        assert rc != QUOTA_RETURN_CODE

    def test_quota_after_transient_window_expires_kills(self):
        """After 15+ normal lines past the retry indicator, a new quota error IS fatal."""
        lines = (
            ["[stderr] Retrying with backoff... something"]
            + ["[stdout] normal output line"] * 16  # exhaust the 15-line window
            + ["[stderr] No capacity available for model gemini-flash"]
        )
        rc, _ = _run(lines)
        assert rc == QUOTA_RETURN_CODE

    def test_will_retry_suppresses_kill(self):
        rc, _ = _run(["[stderr] exhausted your capacity on this model. Will retry in 3s."])
        assert rc != QUOTA_RETURN_CODE


# ---------------------------------------------------------------------------
# Reset-time within spawn window — should NOT kill
# ---------------------------------------------------------------------------

class TestResetWithinSpawnWindow:
    def test_reset_0s_within_any_spawn_rate(self):
        line = "[stderr] exhausted your capacity. Your quota will reset after 0s."
        rc, _ = _run([line], spawn_rate=0.0)
        assert rc != QUOTA_RETURN_CODE

    def test_reset_45s_within_60s_spawn_rate(self):
        line = "[stderr] exhausted your capacity. Your quota will reset after 45s."
        rc, _ = _run([line], spawn_rate=60.0)
        assert rc != QUOTA_RETURN_CODE

    def test_reset_equal_to_spawn_rate(self):
        line = "[stderr] exhausted your capacity. Your quota will reset after 60s."
        rc, _ = _run([line], spawn_rate=60.0)
        assert rc != QUOTA_RETURN_CODE

    def test_reset_0s_default_spawn_rate_zero(self):
        """With default spawn_rate=0.0, only 0s reset is within window."""
        line = "[stderr] exhausted your capacity. Your quota will reset after 0s."
        rc, _ = _run([line])  # spawn_rate defaults to 0.0
        assert rc != QUOTA_RETURN_CODE

    def test_reset_1s_outside_default_spawn_rate_zero_kills(self):
        """1s > 0.0 spawn_rate, so should kill."""
        line = "[stderr] exhausted your capacity. Your quota will reset after 1s."
        rc, _ = _run([line])  # spawn_rate=0.0
        assert rc == QUOTA_RETURN_CODE

    def test_reset_30m_within_2h_spawn_rate(self):
        line = "[stderr] exhausted your capacity. Your quota will reset after 30m."
        rc, _ = _run([line], spawn_rate=7200.0)
        assert rc != QUOTA_RETURN_CODE

    def test_combined_2h32m_exceeds_spawn_rate(self):
        """2h32m = 9120s > spawn_rate=120 → kill."""
        line = "[stderr] exhausted your capacity. Your quota will reset after 2h32m."
        rc, _ = _run([line], spawn_rate=120.0)
        assert rc == QUOTA_RETURN_CODE

    def test_combined_1h5m30s_within_large_spawn_rate(self):
        """1h5m30s = 3930s ≤ spawn_rate=7200 → continue."""
        line = "[stderr] exhausted your capacity. Your quota will reset after 1h5m30s."
        rc, _ = _run([line], spawn_rate=7200.0)
        assert rc != QUOTA_RETURN_CODE


# ---------------------------------------------------------------------------
# abort_event behaviour — long reset terminates, short reset continues
# ---------------------------------------------------------------------------

class TestAbortEvent:
    def test_long_reset_sets_abort_event(self):
        """12h reset > spawn_rate=120 → abort_event is set, process is killed."""
        line = "[stderr] exhausted your capacity. Your quota will reset after 12h."
        rc, aborted = _run_capture_abort([line], spawn_rate=120.0)
        assert rc == QUOTA_RETURN_CODE
        assert aborted is True

    def test_short_reset_does_not_set_abort_event(self):
        """30s reset ≤ spawn_rate=60 → abort_event stays clear, process continues."""
        line = "[stderr] exhausted your capacity. Your quota will reset after 30s."
        rc, aborted = _run_capture_abort([line], spawn_rate=60.0)
        assert rc != QUOTA_RETURN_CODE
        assert aborted is False

    def test_retrying_line_does_not_set_abort_event(self):
        """CLI-managed retry → abort_event stays clear."""
        line = (
            "[stderr] Attempt 1 failed: You have exhausted your capacity on this model. "
            "Your quota will reset after 0s.. Retrying after 5306ms..."
        )
        rc, aborted = _run_capture_abort([line])
        assert rc != QUOTA_RETURN_CODE
        assert aborted is False

    def test_no_reset_time_sets_abort_event(self):
        """Quota with no parseable reset → abort immediately."""
        line = "[stderr] You have exhausted your capacity on this model."
        rc, aborted = _run_capture_abort([line], spawn_rate=60.0)
        assert rc == QUOTA_RETURN_CODE
        assert aborted is True


# ---------------------------------------------------------------------------
# Multiple lines — quota on one, normal on others
# ---------------------------------------------------------------------------

class TestMixedLines:
    def test_quota_line_among_normal_lines_kills(self):
        lines = [
            "Running task...",
            "[stderr] RESOURCE_EXHAUSTED",
            "More output",
        ]
        rc, _ = _run(lines)
        assert rc == QUOTA_RETURN_CODE

    def test_no_quota_lines_passes_through(self):
        lines = [
            "Running task...",
            "[stderr] warning: unused variable",
            "Task complete.",
        ]
        rc, _ = _run(lines)
        assert rc != QUOTA_RETURN_CODE
