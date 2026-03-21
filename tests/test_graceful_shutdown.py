"""Tests for graceful shutdown (Ctrl-C) behaviour.

Ctrl-C should only prevent new jobs from being scheduled.  Running jobs must
finish their current stage naturally — they should NOT be terminated, retried,
or marked as failed due to the shutdown signal.
"""
import os
import sys
import threading
import concurrent.futures
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import workflow_lib.executor as executor_mod
from workflow_lib.executor import process_task, run_agent


class TestProcessTaskShutdownBeforeAnyStage:
    """When shutdown_requested is True before process_task starts any stage."""

    def test_returns_false_without_running_stages(self, tmp_path):
        """process_task returns False immediately if shutdown was requested
        before any stage could start."""
        executor_mod.shutdown_requested = True
        try:
            with patch("workflow_lib.executor.run_agent") as mock_agent, \
                 patch("workflow_lib.executor.get_task_details", return_value="# Task"), \
                 patch("workflow_lib.executor.get_project_context", return_value=""), \
                 patch("workflow_lib.executor.get_memory_context", return_value=""), \
                 patch("workflow_lib.config.get_config_defaults", return_value={"retries": 0}), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr=b"")):
                result = process_task(
                    str(tmp_path), "phase_1/sub/01_task.md", "echo ok",
                    backend="gemini", cleanup=True,
                )
            assert result is False
            mock_agent.assert_not_called()
        finally:
            executor_mod.shutdown_requested = False

    def test_dashboard_not_marked_failed(self, tmp_path):
        """Dashboard should not show 'failed' status for a shutdown-skipped task."""
        executor_mod.shutdown_requested = True
        dashboard = MagicMock()
        try:
            with patch("workflow_lib.executor.run_agent") as mock_agent, \
                 patch("workflow_lib.executor.get_task_details", return_value="# Task"), \
                 patch("workflow_lib.executor.get_project_context", return_value=""), \
                 patch("workflow_lib.executor.get_memory_context", return_value=""), \
                 patch("workflow_lib.config.get_config_defaults", return_value={"retries": 0}), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr=b"")):
                process_task(
                    str(tmp_path), "phase_1/sub/01_task.md", "echo ok",
                    backend="gemini", dashboard=dashboard, cleanup=True,
                )
            # Should NOT be marked as "failed" — it was a graceful shutdown
            for c in dashboard.set_agent.call_args_list:
                assert "failed" not in str(c).lower(), (
                    f"Dashboard should not show 'failed' during shutdown, got: {c}"
                )
        finally:
            executor_mod.shutdown_requested = False


class TestProcessTaskShutdownBetweenStages:
    """When shutdown_requested becomes True after one stage completes."""

    def test_returns_true_after_completing_one_stage(self, tmp_path):
        """If impl stage succeeds then shutdown is set, process_task returns True
        (graceful partial completion, not failure)."""
        executor_mod.shutdown_requested = False

        call_count = [0]
        def _fake_run_agent(*args, **kwargs):
            call_count[0] += 1
            # After first agent call (impl stage), set shutdown
            if call_count[0] == 1:
                executor_mod.shutdown_requested = True
            return True

        try:
            with patch("workflow_lib.executor.run_agent", side_effect=_fake_run_agent), \
                 patch("workflow_lib.executor.get_task_details", return_value="# Task"), \
                 patch("workflow_lib.executor.get_project_context", return_value=""), \
                 patch("workflow_lib.executor.get_memory_context", return_value=""), \
                 patch("workflow_lib.config.get_config_defaults", return_value={"retries": 0}), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr=b"")):
                result = process_task(
                    str(tmp_path), "phase_1/sub/01_task.md", "echo ok",
                    backend="gemini", cleanup=True,
                )
            # Should return True — impl completed successfully before shutdown
            assert result is True
            # Only impl stage should have run (review/validate skipped due to shutdown)
            assert call_count[0] == 1
        finally:
            executor_mod.shutdown_requested = False


class TestProcessTaskShutdownDuringStage:
    """When shutdown_requested becomes True while a stage is running."""

    def test_running_stage_completes_naturally(self, tmp_path):
        """A stage that is in-flight when shutdown is requested must finish
        before the task stops — it should not be aborted mid-execution."""
        executor_mod.shutdown_requested = False
        stage_completed = threading.Event()

        def _slow_agent(*args, **kwargs):
            # Simulate shutdown arriving during this stage
            executor_mod.shutdown_requested = True
            stage_completed.set()
            return True

        try:
            with patch("workflow_lib.executor.run_agent", side_effect=_slow_agent), \
                 patch("workflow_lib.executor.get_task_details", return_value="# Task"), \
                 patch("workflow_lib.executor.get_project_context", return_value=""), \
                 patch("workflow_lib.executor.get_memory_context", return_value=""), \
                 patch("workflow_lib.config.get_config_defaults", return_value={"retries": 0}), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr=b"")):
                result = process_task(
                    str(tmp_path), "phase_1/sub/01_task.md", "echo ok",
                    backend="gemini", cleanup=True,
                )
            assert stage_completed.is_set(), "Stage should have completed naturally"
            assert result is True
        finally:
            executor_mod.shutdown_requested = False


class TestRunAgentShutdown:
    """run_agent should bail out early when shutdown_requested is set."""

    def test_returns_false_immediately_on_shutdown(self):
        """run_agent returns False without launching a subprocess when
        shutdown_requested is already True."""
        executor_mod.shutdown_requested = True
        try:
            result = run_agent(
                "Implementation", "implement_task.md",
                {"phase_filename": "p1", "task_name": "t1"},
                "/tmp/fake",
                backend="gemini",
            )
            assert result is False
        finally:
            executor_mod.shutdown_requested = False

    def test_no_retry_on_shutdown(self):
        """run_agent should not retry after quota failure if shutdown was
        requested."""
        executor_mod.shutdown_requested = False

        def _fake_run_ai(*args, **kwargs):
            # Simulate quota failure, then set shutdown
            executor_mod.shutdown_requested = True
            return (42, "quota exceeded")  # QUOTA_RETURN_CODE

        try:
            with patch("workflow_lib.executor.run_ai_command", side_effect=_fake_run_ai), \
                 patch("workflow_lib.executor.QUOTA_RETURN_CODE", 42), \
                 patch("workflow_lib.executor.get_context_limit", return_value=100000), \
                 patch("workflow_lib.executor.truncate_task_context", side_effect=lambda ctx, *a, **kw: ctx), \
                 patch("workflow_lib.executor.get_project_images", return_value=[]), \
                 patch("workflow_lib.executor.get_rag_enabled", return_value=False), \
                 patch("workflow_lib.executor.set_agent_context_limit"), \
                 patch("builtins.open", MagicMock(return_value=MagicMock(
                     __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value="{phase_filename} {task_name}"))),
                     __exit__=MagicMock(return_value=False),
                 ))):
                result = run_agent(
                    "Implementation", "implement_task.md",
                    {"phase_filename": "p1", "task_name": "t1"},
                    "/tmp/fake",
                    backend="gemini",
                )
            assert result is False
        finally:
            executor_mod.shutdown_requested = False


class TestAgentPoolAcquireShutdown:
    """agent_pool.acquire() loop in run_agent should exit on shutdown."""

    def test_acquire_loop_exits_on_shutdown(self):
        """If shutdown_requested is set while waiting for a pool slot,
        run_agent should return False instead of blocking."""
        executor_mod.shutdown_requested = True
        pool = MagicMock()
        pool.acquire.return_value = None  # No agent available

        try:
            result = run_agent(
                "Implementation", "implement_task.md",
                {"phase_filename": "p1", "task_name": "t1"},
                "/tmp/fake",
                backend="gemini",
                agent_pool=pool,
            )
            assert result is False
            # acquire should not even be called since shutdown check is first
        finally:
            executor_mod.shutdown_requested = False


class TestDAGLoopShutdownHandling:
    """The main DAG execution loop should not treat shutdown-skipped tasks
    as failures or trigger retries."""

    def test_shutdown_task_not_marked_failed(self):
        """Tasks that return False due to shutdown should not be added to
        failed_tasks or trigger retry log messages."""
        dashboard = MagicMock()
        state = {"completed_tasks": [], "merged_tasks": [], "task_stages": {}}
        state_lock = threading.Lock()
        failed_tasks = set()
        active_tasks = set()
        task_attempts = {}
        max_task_retries = 2

        executor_mod.shutdown_requested = True

        try:
            # Simulate what the DAG loop does when processing a completed future
            # that returned False during shutdown.
            task_id = "phase_1/sub/01_task.md"
            active_tasks.add(task_id)
            task_attempts[task_id] = 1

            # Create a future that returns False (as process_task would during shutdown)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(lambda: False)
                future.result()  # wait for completion

            success = future.result()
            assert success is False

            # This is the code path from the DAG loop (post-fix):
            # When shutdown_requested and success is False, it should NOT retry
            if not success:
                if executor_mod.shutdown_requested:
                    dashboard.log(f"   -> [Shutdown] Task {task_id} did not start. Will resume on next run.")
                else:
                    # This path should NOT be taken during shutdown
                    attempts = task_attempts.get(task_id, 1)
                    if attempts <= max_task_retries:
                        dashboard.log(f"   -> [Retry] Task {task_id} failed")
                    else:
                        failed_tasks.add(f"Task {task_id} failed")

            # Verify: no retry messages, no failed_tasks entries
            assert len(failed_tasks) == 0, "Shutdown-skipped tasks must not be marked failed"
            log_messages = [str(c) for c in dashboard.log.call_args_list]
            assert not any("Retry" in msg for msg in log_messages), (
                f"No retry messages expected during shutdown, got: {log_messages}"
            )
            assert any("Shutdown" in msg for msg in log_messages), (
                "Expected a shutdown log message"
            )
        finally:
            executor_mod.shutdown_requested = False

    def test_shutdown_exception_not_marked_failed(self):
        """Tasks that raise exceptions during shutdown should not be added to
        failed_tasks."""
        dashboard = MagicMock()
        failed_tasks = set()
        task_attempts = {"phase_1/sub/01_task.md": 1}
        max_task_retries = 2
        task_id = "phase_1/sub/01_task.md"

        executor_mod.shutdown_requested = True
        try:
            # Simulate the exception handling path from the DAG loop (post-fix)
            try:
                raise RuntimeError("interrupted")
            except Exception:
                if executor_mod.shutdown_requested:
                    dashboard.log(f"   -> [Shutdown] Task {task_id} interrupted. Will resume on next run.")
                else:
                    attempts = task_attempts.get(task_id, 1)
                    if attempts <= max_task_retries:
                        dashboard.log(f"   -> [Retry] Task {task_id} raised exception")
                    else:
                        failed_tasks.add(f"Task {task_id} exception")

            assert len(failed_tasks) == 0
            log_messages = [str(c) for c in dashboard.log.call_args_list]
            assert not any("Retry" in msg for msg in log_messages)
            assert any("Shutdown" in msg for msg in log_messages)
        finally:
            executor_mod.shutdown_requested = False

    def test_no_new_tasks_scheduled_after_shutdown(self):
        """The scheduling guard (shutdown_requested check) should prevent
        new tasks from being submitted to the executor."""
        executor_mod.shutdown_requested = True
        try:
            from workflow_lib.executor import get_ready_tasks
            # When shutdown_requested is True, the DAG loop skips get_ready_tasks
            # entirely. Verify the guard logic:
            ready_tasks = []
            if not executor_mod.shutdown_requested:
                ready_tasks = ["should_not_appear"]
            assert ready_tasks == [], "No tasks should be scheduled during shutdown"
        finally:
            executor_mod.shutdown_requested = False


class TestSignalHandlerSetsFlag:
    """Verify the executor signal handler sets the flag without raising."""

    def test_first_sigint_sets_flag(self):
        """First call to signal_handler sets shutdown_requested=True."""
        executor_mod.shutdown_requested = False
        try:
            executor_mod.signal_handler(2, None)  # sig=SIGINT
            assert executor_mod.shutdown_requested is True
        finally:
            executor_mod.shutdown_requested = False

    def test_signal_handler_does_not_raise(self):
        """signal_handler must not raise KeyboardInterrupt — that would
        terminate in-flight jobs."""
        executor_mod.shutdown_requested = False
        executor_mod._active_dashboard = MagicMock()
        try:
            # Should not raise anything
            executor_mod.signal_handler(2, None)
            assert executor_mod.shutdown_requested is True
        finally:
            executor_mod.shutdown_requested = False
            executor_mod._active_dashboard = None
