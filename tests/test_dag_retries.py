"""Tests for auto-retry of failed tasks in the DAG executor."""

import sys
import os
import threading
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _make_dashboard():
    dashboard = MagicMock()
    dashboard.log = MagicMock()
    dashboard.set_agent = MagicMock()
    dashboard.remove_agent = MagicMock()
    return dashboard


class TestDagRetryLogic:
    """Test that failed tasks are retried up to `retries` times in _execute_dag_inner."""

    def _run_dag(self, process_task_side_effects, retries=1, task_id="phase_1/01_test/01_task.md"):
        """Helper to run a single-task DAG with mocked process_task."""
        from workflow_lib.executor import _execute_dag_inner

        master_dag = {task_id: []}
        state = {"completed_tasks": [], "merged_tasks": [], "task_stages": {}}
        dashboard = _make_dashboard()

        call_count = [0]

        def fake_process_task(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(process_task_side_effects):
                result = process_task_side_effects[idx]
                if isinstance(result, Exception):
                    raise result
                return result
            return True

        def fake_merge_task(*args, **kwargs):
            return True

        patches = [
            patch('workflow_lib.executor.process_task', side_effect=fake_process_task),
            patch('workflow_lib.executor.merge_task', side_effect=fake_merge_task),
            patch('workflow_lib.config.get_config_defaults', return_value={"retries": retries}),
            patch('workflow_lib.executor.get_serena_enabled', return_value=False),
            patch('workflow_lib.executor.get_dev_branch', return_value="dev"),
            patch('workflow_lib.executor.get_pivot_remote', return_value="origin"),
            patch('workflow_lib.executor.get_gitlab_remote_url', return_value=None),
            patch('workflow_lib.executor.get_docker_config', return_value=None),
            patch('workflow_lib.executor._get_resumable_tasks', return_value=set()),
            patch('workflow_lib.executor.load_blocked_tasks', return_value=set()),
            patch('workflow_lib.executor.notify_failure'),
            patch('os._exit', side_effect=SystemExit(1)),
            patch('subprocess.run', return_value=MagicMock(returncode=0, stdout="", stderr="")),
        ]

        for p in patches:
            p.start()

        try:
            try:
                _execute_dag_inner(
                    root_dir="/tmp/test",
                    master_dag=master_dag,
                    state=state,
                    jobs=1,
                    presubmit_cmd="./do presubmit",
                    backend="claude",
                    serena_enabled=False,
                    cache_lock=threading.Lock(),
                    dashboard=dashboard,
                )
            except SystemExit:
                pass
        finally:
            for p in patches:
                p.stop()

        return call_count[0], state, dashboard

    def test_no_retry_when_retries_zero(self):
        """With retries=0, a failed task is not retried."""
        count, state, dashboard = self._run_dag(
            process_task_side_effects=[False],
            retries=0,
        )
        assert count == 1

    def test_retry_on_failure(self):
        """With retries=1, a failed task is retried once."""
        count, state, dashboard = self._run_dag(
            process_task_side_effects=[False, True],
            retries=1,
        )
        assert count == 2
        assert "phase_1/01_test/01_task.md" in state["completed_tasks"]

    def test_retry_on_exception(self):
        """Exceptions also trigger retry."""
        count, state, dashboard = self._run_dag(
            process_task_side_effects=[RuntimeError("boom"), True],
            retries=1,
        )
        assert count == 2
        assert "phase_1/01_test/01_task.md" in state["completed_tasks"]

    def test_exhausted_retries_fails(self):
        """After exhausting retries, the task is marked as failed."""
        count, state, dashboard = self._run_dag(
            process_task_side_effects=[False, False, False],
            retries=1,  # 1 retry = 2 total attempts
        )
        assert count == 2
        assert "phase_1/01_test/01_task.md" not in state["completed_tasks"]

    def test_retry_logs_message(self):
        """Retry produces a log message."""
        _, _, dashboard = self._run_dag(
            process_task_side_effects=[False, True],
            retries=1,
        )
        log_calls = [str(c) for c in dashboard.log.call_args_list]
        assert any("Retry" in c for c in log_calls)

    def test_success_no_retry(self):
        """A successful task is not retried."""
        count, state, _ = self._run_dag(
            process_task_side_effects=[True],
            retries=2,
        )
        assert count == 1
        assert "phase_1/01_test/01_task.md" in state["completed_tasks"]


class TestMergeRetryDuringShutdown:
    """Merge retries must not be attempted when shutdown_requested is True."""

    def test_merge_retry_skipped_during_shutdown(self):
        """When a merge fails and shutdown_requested is set, no retry is submitted."""
        import workflow_lib.executor as executor_mod
        from workflow_lib.executor import _execute_dag_inner

        task_id = "phase_1/01_test/01_task.md"
        master_dag = {task_id: []}
        state = {"completed_tasks": [], "merged_tasks": [], "task_stages": {}}
        dashboard = _make_dashboard()

        merge_calls = [0]

        def fake_merge_task(*args, **kwargs):
            merge_calls[0] += 1
            # First merge fails; set shutdown before returning
            executor_mod.shutdown_requested = True
            return False

        patches = [
            patch('workflow_lib.executor.process_task', return_value=True),
            patch('workflow_lib.executor.merge_task', side_effect=fake_merge_task),
            patch('workflow_lib.config.get_config_defaults', return_value={"retries": 3}),
            patch('workflow_lib.executor.get_serena_enabled', return_value=False),
            patch('workflow_lib.executor.get_dev_branch', return_value="dev"),
            patch('workflow_lib.executor.get_pivot_remote', return_value="origin"),
            patch('workflow_lib.executor.get_gitlab_remote_url', return_value=None),
            patch('workflow_lib.executor.get_docker_config', return_value=None),
            patch('workflow_lib.executor._get_resumable_tasks', return_value=set()),
            patch('workflow_lib.executor.load_blocked_tasks', return_value=set()),
            patch('workflow_lib.executor.notify_failure'),
            patch('os._exit', side_effect=SystemExit(1)),
            patch('subprocess.run', return_value=MagicMock(returncode=0, stdout="", stderr="")),
        ]

        executor_mod.shutdown_requested = False
        for p in patches:
            p.start()
        try:
            try:
                _execute_dag_inner(
                    root_dir="/tmp/test",
                    master_dag=master_dag,
                    state=state,
                    jobs=1,
                    presubmit_cmd="./do presubmit",
                    backend="claude",
                    serena_enabled=False,
                    cache_lock=threading.Lock(),
                    dashboard=dashboard,
                )
            except SystemExit:
                pass
        finally:
            executor_mod.shutdown_requested = False
            for p in patches:
                p.stop()

        assert merge_calls[0] == 1, (
            f"Merge should be called exactly once (no retry during shutdown), "
            f"but was called {merge_calls[0]} times"
        )
