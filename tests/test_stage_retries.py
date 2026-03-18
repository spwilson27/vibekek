"""Tests for stage-level retry logic in process_task and merge retries in execute_dag."""

import sys
import os
import pytest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from workflow_lib.executor import (
    process_task,
    STAGE_IMPL, STAGE_REVIEW, STAGE_VALIDATE, STAGE_DONE,
)


def _make_dashboard():
    d = MagicMock()
    d.log = MagicMock()
    d.set_agent = MagicMock()
    d.remove_agent = MagicMock()
    return d


# Patch targets inside executor module
_EXEC = "workflow_lib.executor"
_CFG = "workflow_lib.config.get_config_defaults"


class TestProcessTaskStageRetries:
    """Verify that process_task retries individual stages on failure."""

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage", return_value=True)
    @patch(f"{_EXEC}.run_review_stage", return_value=True)
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_impl_retried_on_failure_then_succeeds(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Impl fails once, retries, succeeds — review+validate still run."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        mock_impl.side_effect = [False, True]
        dashboard = _make_dashboard()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_IMPL,
        )
        assert result is True
        assert mock_impl.call_count == 2
        assert mock_review.call_count == 1
        assert mock_validate.call_count == 1

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage", return_value=True)
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_review_retried_on_failure_then_succeeds(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Review fails once, retries, succeeds — validate still runs."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        mock_review.side_effect = [False, True]
        dashboard = _make_dashboard()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_IMPL,
        )
        assert result is True
        assert mock_impl.call_count == 1
        assert mock_review.call_count == 2
        assert mock_validate.call_count == 1

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage", return_value=True)
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_validate_retried_on_failure_then_succeeds(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Validate fails once, retries, succeeds."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        mock_validate.side_effect = [False, True]
        dashboard = _make_dashboard()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_IMPL,
        )
        assert result is True
        assert mock_validate.call_count == 2

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_impl_fails_all_retries_returns_false(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Impl fails all 3 attempts (1 + 2 retries) — returns False, later stages never run."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        mock_impl.return_value = False
        dashboard = _make_dashboard()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_IMPL,
        )
        assert result is False
        assert mock_impl.call_count == 3  # 1 original + 2 retries
        assert mock_review.call_count == 0
        assert mock_validate.call_count == 0

    @patch(_CFG, return_value={"retries": 0})
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage", return_value=False)
    @patch(f"{_EXEC}.subprocess")
    def test_zero_retries_no_retry(
        self, mock_sp, mock_impl, mock_review, mock_cfg
    ):
        """With retries=0, a single failure returns False immediately."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        dashboard = _make_dashboard()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_IMPL,
        )
        assert result is False
        assert mock_impl.call_count == 1
        assert mock_review.call_count == 0

    @patch(_CFG, return_value={"retries": 3})
    @patch(f"{_EXEC}.run_validate_stage", return_value=True)
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_review_fails_all_retries_returns_false(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Review fails all 4 attempts — returns False, validate never runs."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        mock_review.return_value = False
        dashboard = _make_dashboard()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_IMPL,
        )
        assert result is False
        assert mock_impl.call_count == 1
        assert mock_review.call_count == 4  # 1 original + 3 retries
        assert mock_validate.call_count == 0

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage", return_value=True)
    @patch(f"{_EXEC}.run_review_stage", return_value=True)
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_all_stages_pass_first_try(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """When all stages pass on first try, each runs exactly once."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        dashboard = _make_dashboard()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_IMPL,
        )
        assert result is True
        assert mock_impl.call_count == 1
        assert mock_review.call_count == 1
        assert mock_validate.call_count == 1

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage", return_value=True)
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_stage_callback_only_after_success(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """on_stage_complete is called only after a stage succeeds, not on retries."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        mock_impl.side_effect = [False, True]
        mock_review.return_value = True
        dashboard = _make_dashboard()
        callback = MagicMock()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_IMPL,
            on_stage_complete=callback,
        )
        assert result is True
        # Callback should be called 3 times: once per stage success (impl, review, validate)
        assert callback.call_count == 3
        callback.assert_any_call("phase_1/task_a.md", STAGE_IMPL)
        callback.assert_any_call("phase_1/task_a.md", STAGE_REVIEW)
        callback.assert_any_call("phase_1/task_a.md", STAGE_VALIDATE)

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage", return_value=True)
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_starting_from_review_skips_impl(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """When starting from review stage, impl is not run."""
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        mock_validate.side_effect = [False, True]
        dashboard = _make_dashboard()

        result = process_task(
            "/tmp/root", "phase_1/task_a.md", "true",
            dashboard=dashboard, starting_stage=STAGE_REVIEW,
        )
        assert result is True
        assert mock_impl.call_count == 0
        assert mock_review.call_count == 1
        assert mock_validate.call_count == 2


class TestProcessTaskShutdownDuringRetry:
    """Verify shutdown_requested is respected during stage retries."""

    @patch(_CFG, return_value={"retries": 5})
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_shutdown_stops_retries(
        self, mock_sp, mock_impl, mock_review, mock_cfg
    ):
        """If shutdown_requested is set during retries, retries stop."""
        import workflow_lib.executor as executor_mod
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        orig_shutdown = executor_mod.shutdown_requested

        call_count = 0
        def impl_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                executor_mod.shutdown_requested = True
            return False

        mock_impl.side_effect = impl_side_effect
        dashboard = _make_dashboard()

        try:
            result = process_task(
                "/tmp/root", "phase_1/task_a.md", "true",
                dashboard=dashboard, starting_stage=STAGE_IMPL,
            )
            # Should fail since impl never succeeded
            assert result is False
            # Should have stopped after 2 attempts (shutdown set on 2nd)
            assert mock_impl.call_count == 2
            assert mock_review.call_count == 0
        finally:
            executor_mod.shutdown_requested = orig_shutdown
