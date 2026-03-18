"""Tests for stage-level retry logic in process_task and merge retries in execute_dag.

Retry semantics:
- A single per-task retry counter is shared across all stages.
- impl/review failure → retry the same stage, consuming one retry.
- validate failure → fall back to review stage, consuming one retry.
- Counter exhaustion → return False.
"""

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


_EXEC = "workflow_lib.executor"
_CFG = "workflow_lib.config.get_config_defaults"


def _run_process_task(mock_sp, dashboard=None, starting_stage=STAGE_IMPL, callback=None):
    """Helper to call process_task with standard boilerplate."""
    mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
    if dashboard is None:
        dashboard = _make_dashboard()
    kwargs = dict(
        dashboard=dashboard, starting_stage=starting_stage,
    )
    if callback is not None:
        kwargs["on_stage_complete"] = callback
    return process_task("/tmp/root", "phase_1/task_a.md", "true", **kwargs)


class TestStageRetries:
    """Verify per-task retry counter and stage retry/fallback behavior."""

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage", return_value=True)
    @patch(f"{_EXEC}.run_review_stage", return_value=True)
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_impl_retried_on_failure_then_succeeds(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Impl fails once, retries, succeeds — review+validate still run."""
        mock_impl.side_effect = [False, True]
        assert _run_process_task(mock_sp) is True
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
        mock_review.side_effect = [False, True]
        assert _run_process_task(mock_sp) is True
        assert mock_impl.call_count == 1
        assert mock_review.call_count == 2
        assert mock_validate.call_count == 1

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_validate_failure_falls_back_to_review(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Validate fails → falls back to review, then validate again succeeds."""
        # review passes, validate fails, review passes again, validate passes
        mock_review.side_effect = [True, True]
        mock_validate.side_effect = [False, True]
        assert _run_process_task(mock_sp) is True
        assert mock_impl.call_count == 1
        assert mock_review.call_count == 2  # initial + retry fallback
        assert mock_validate.call_count == 2

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_impl_fails_all_retries_returns_false(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Impl fails 3 times (1 + 2 retries) — returns False, later stages never run."""
        mock_impl.return_value = False
        assert _run_process_task(mock_sp) is False
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
        assert _run_process_task(mock_sp) is False
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
        """Review fails 4 times (1 + 3 retries) — returns False, validate never runs."""
        mock_review.return_value = False
        assert _run_process_task(mock_sp) is False
        assert mock_impl.call_count == 1
        assert mock_review.call_count == 4
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
        assert _run_process_task(mock_sp) is True
        assert mock_impl.call_count == 1
        assert mock_review.call_count == 1
        assert mock_validate.call_count == 1


class TestRetryCounterIsPerTask:
    """The retry counter is shared across stages within a single task."""

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_impl_and_review_failures_share_counter(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Impl fails once (1 retry used), review fails once (2nd retry used), validate passes."""
        mock_impl.side_effect = [False, True]
        mock_review.side_effect = [False, True]
        mock_validate.return_value = True
        assert _run_process_task(mock_sp) is True
        assert mock_impl.call_count == 2
        assert mock_review.call_count == 2
        assert mock_validate.call_count == 1

    @patch(_CFG, return_value={"retries": 3})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_retries_exhausted_across_stages(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Impl uses 1 retry, review uses remaining 2 retries → exhausted on 4th failure."""
        # retries=3: task_attempt can reach 3 before stopping (>3 stops).
        # impl fail → task_attempt=1, retry impl → pass
        # review fail → task_attempt=2, retry review
        # review fail → task_attempt=3, retry review
        # review fail → task_attempt=4, 4>3 → stop
        mock_impl.side_effect = [False, True]
        mock_review.return_value = False
        mock_validate.return_value = True
        assert _run_process_task(mock_sp) is False
        assert mock_impl.call_count == 2
        assert mock_review.call_count == 3  # task_attempt: 2,3,4 → 3 calls before exhausted


class TestValidateFallbackToReview:
    """Validate failure falls back to review, not to validate itself."""

    @patch(_CFG, return_value={"retries": 3})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_validate_fail_reruns_review_then_validate(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """validate fails → review reruns → validate reruns and passes."""
        mock_review.side_effect = [True, True]
        mock_validate.side_effect = [False, True]
        assert _run_process_task(mock_sp) is True
        assert mock_review.call_count == 2
        assert mock_validate.call_count == 2

    @patch(_CFG, return_value={"retries": 3})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_validate_fails_repeatedly_exhausts_retries(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Validate keeps failing, each time falling back to review. Exhausts retries."""
        # Each validate fail consumes a retry and falls back to review.
        # With retries=3: validate fails 3 times (3 retries consumed), then fails once more → exhausted.
        mock_review.return_value = True
        mock_validate.return_value = False
        assert _run_process_task(mock_sp) is False
        # review: 1 initial + 3 fallbacks = 4
        assert mock_review.call_count == 4
        # validate: 1 initial + 3 retries = 4
        assert mock_validate.call_count == 4

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_validate_fallback_review_also_fails(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """Validate fails, falls back to review which keeps failing until retries exhausted."""
        # retries=2: validate fail → task_attempt=1, fallback to review.
        # review fail → task_attempt=2, retry review.
        # review fail → task_attempt=3, 3>2 → stop.
        mock_review.side_effect = [True, False, False]
        mock_validate.side_effect = [False]
        assert _run_process_task(mock_sp) is False
        assert mock_review.call_count == 3  # 1 pass + 2 fails
        assert mock_validate.call_count == 1

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage", return_value=True)
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_starting_from_review_validate_falls_back_to_review(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """When starting from review, validate failure still falls back to review."""
        mock_validate.side_effect = [False, True]
        assert _run_process_task(mock_sp, starting_stage=STAGE_REVIEW) is True
        assert mock_impl.call_count == 0
        assert mock_review.call_count == 2  # initial + fallback
        assert mock_validate.call_count == 2


class TestCallbackBehavior:
    """on_stage_complete callback is called correctly with retries."""

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage", return_value=True)
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage")
    @patch(f"{_EXEC}.subprocess")
    def test_callback_called_on_each_stage_success(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """on_stage_complete fires after each successful stage, including re-runs."""
        mock_impl.side_effect = [False, True]
        mock_review.return_value = True
        callback = MagicMock()
        assert _run_process_task(mock_sp, callback=callback) is True
        callback.assert_any_call("phase_1/task_a.md", STAGE_IMPL)
        callback.assert_any_call("phase_1/task_a.md", STAGE_REVIEW)
        callback.assert_any_call("phase_1/task_a.md", STAGE_VALIDATE)

    @patch(_CFG, return_value={"retries": 2})
    @patch(f"{_EXEC}.run_validate_stage")
    @patch(f"{_EXEC}.run_review_stage")
    @patch(f"{_EXEC}.run_impl_stage", return_value=True)
    @patch(f"{_EXEC}.subprocess")
    def test_callback_for_validate_fallback_to_review(
        self, mock_sp, mock_impl, mock_review, mock_validate, mock_cfg
    ):
        """When validate fails and falls back, review callback fires again."""
        mock_review.side_effect = [True, True]
        mock_validate.side_effect = [False, True]
        callback = MagicMock()
        assert _run_process_task(mock_sp, callback=callback) is True
        # review callback called twice (initial + fallback)
        review_calls = [c for c in callback.call_args_list if c == call("phase_1/task_a.md", STAGE_REVIEW)]
        assert len(review_calls) == 2


class TestShutdownDuringRetry:
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
            assert result is False
            assert mock_impl.call_count == 2
            assert mock_review.call_count == 0
        finally:
            executor_mod.shutdown_requested = orig_shutdown
