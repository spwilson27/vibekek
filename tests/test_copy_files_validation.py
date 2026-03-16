"""Tests for docker copy_files validation before DAG execution.

This module tests that:
1. Missing copy_files sources are detected before any tasks are scheduled
2. The validation uses sudo to check file existence (handles different owners)
3. All missing files are collected and reported
4. The program exits immediately with clear error messages
"""

import os
import sys
import tempfile
import pytest
from unittest.mock import patch, MagicMock, call

from workflow_lib.agent_pool import DockerConfig, DockerCopyFile
from workflow_lib.executor import _execute_dag_inner


class TestCopyFilesValidation:
    """Tests for pre-execution copy_files validation."""

    def test_validation_passes_when_all_files_exist(self, tmp_path):
        """Validation should pass when all copy_files sources exist."""
        # Create existing source files
        src1 = tmp_path / "file1.json"
        src1.write_text("data1")
        src2 = tmp_path / "file2.json"
        src2.write_text("data2")

        docker_cfg = DockerConfig(
            image="test:latest",
            volumes=[],
            copy_files=[
                DockerCopyFile(src=str(src1), dest="/container/file1.json"),
                DockerCopyFile(src=str(src2), dest="/container/file2.json"),
            ]
        )

        def fake_sudo_run(cmd, **kwargs):
            if cmd[:3] == ["sudo", "test", "-e"]:
                # File exists in test
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("workflow_lib.executor.get_docker_config", return_value=docker_cfg), \
             patch("workflow_lib.config.get_agent_pool_configs", return_value=[]), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.subprocess.run", side_effect=fake_sudo_run), \
             patch("workflow_lib.executor.concurrent.futures.ThreadPoolExecutor"):
            
            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            # Should not raise - validation passes
            _execute_dag_inner(
                root_dir=str(tmp_path),
                master_dag={},
                state={"completed_tasks": [], "merged_tasks": []},
                jobs=1,
                presubmit_cmd="test",
                backend="gemini",
                serena_enabled=False,
                cache_lock=MagicMock(),
                dashboard=mock_ctx,
            )

            # Verify validation was logged
            mock_ctx.log.assert_any_call("=> Validating docker copy_files sources...")
            mock_ctx.log.assert_any_call("   All 2 copy_files sources validated across 1 config(s).")

    def test_validation_fails_when_file_missing(self, tmp_path):
        """Validation should fail immediately when a copy_files source is missing."""
        # Create one existing file
        src1 = tmp_path / "file1.json"
        src1.write_text("data1")
        # Missing file: src2 = tmp_path / "file2.json" (not created)

        docker_cfg = DockerConfig(
            image="test:latest",
            volumes=[],
            copy_files=[
                DockerCopyFile(src=str(src1), dest="/container/file1.json"),
                DockerCopyFile(src=str(tmp_path / "missing.json"), dest="/container/file2.json"),
            ]
        )

        def fake_sudo_run(cmd, **kwargs):
            if cmd[:3] == ["sudo", "test", "-e"]:
                if "missing.json" in str(cmd):
                    return MagicMock(returncode=1, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("workflow_lib.executor.get_docker_config", return_value=docker_cfg), \
             patch("workflow_lib.config.get_agent_pool_configs", return_value=[]), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.notify_failure") as mock_notify, \
             patch("workflow_lib.executor.sys.exit") as mock_exit, \
             patch("workflow_lib.executor.subprocess.run", side_effect=fake_sudo_run):
            
            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            # Should call sys.exit(1) due to missing file
            _execute_dag_inner(
                root_dir=str(tmp_path),
                master_dag={},
                state={"completed_tasks": [], "merged_tasks": []},
                jobs=1,
                presubmit_cmd="test",
                backend="gemini",
                serena_enabled=False,
                cache_lock=MagicMock(),
                dashboard=mock_ctx,
            )

            # Verify validation detected the missing file
            mock_ctx.log.assert_any_call("=> Validating docker copy_files sources...")
            mock_ctx.log.assert_called()
            
            # Check that FATAL message was logged
            log_calls = [str(c) for c in mock_ctx.log.call_args_list]
            fatal_msgs = [c for c in log_calls if "FATAL" in c and "copy_files" in c]
            assert len(fatal_msgs) > 0, "Should log FATAL message for missing files"
            
            # Verify missing file path was logged
            missing_log = [c for c in log_calls if "missing.json" in c]
            assert len(missing_log) > 0, "Should log missing file path"
            
            # Verify notification was sent
            mock_notify.assert_called_once()
            
            # Verify exit was called with code 1
            mock_exit.assert_called_once_with(1)

    def test_validation_uses_sudo_test(self, tmp_path):
        """Validation should use 'sudo test -e' to check file existence."""
        missing_src = tmp_path / "missing.json"

        docker_cfg = DockerConfig(
            image="test:latest",
            volumes=[],
            copy_files=[
                DockerCopyFile(src=str(missing_src), dest="/container/file.json"),
            ]
        )

        sudo_test_calls = []

        def fake_run(cmd, **kwargs):
            sudo_test_calls.append(cmd)
            return MagicMock(returncode=1, stdout="", stderr="")

        with patch("workflow_lib.executor.get_docker_config", return_value=docker_cfg), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.sys.exit") as mock_exit, \
             patch("workflow_lib.executor.subprocess.run", side_effect=fake_run):
            
            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            _execute_dag_inner(
                root_dir=str(tmp_path),
                master_dag={},
                state={"completed_tasks": [], "merged_tasks": []},
                jobs=1,
                presubmit_cmd="test",
                backend="gemini",
                serena_enabled=False,
                cache_lock=MagicMock(),
                dashboard=mock_ctx,
            )

            # Verify sudo test -e was called
            assert len(sudo_test_calls) > 0, "Should call sudo test"
            test_call = sudo_test_calls[0]
            assert test_call[0] == "sudo", "First arg should be sudo"
            assert test_call[1] == "test", "Second arg should be test"
            assert test_call[2] == "-e", "Third arg should be -e"
            assert str(missing_src) in test_call, "Should check the missing file path"

    def test_validation_collects_all_missing_files(self, tmp_path):
        """Validation should collect ALL missing files, not just the first."""
        # Create multiple missing files
        missing1 = tmp_path / "missing1.json"
        missing2 = tmp_path / "missing2.json"
        missing3 = tmp_path / "missing3.json"

        docker_cfg = DockerConfig(
            image="test:latest",
            volumes=[],
            copy_files=[
                DockerCopyFile(src=str(missing1), dest="/container/file1.json"),
                DockerCopyFile(src=str(missing2), dest="/container/file2.json"),
                DockerCopyFile(src=str(missing3), dest="/container/file3.json"),
            ]
        )

        def fake_sudo_run(cmd, **kwargs):
            if cmd[:3] == ["sudo", "test", "-e"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("workflow_lib.executor.get_docker_config", return_value=docker_cfg), \
             patch("workflow_lib.config.get_agent_pool_configs", return_value=[]), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.sys.exit") as mock_exit, \
             patch("workflow_lib.executor.subprocess.run", side_effect=fake_sudo_run):
            
            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            _execute_dag_inner(
                root_dir=str(tmp_path),
                master_dag={},
                state={"completed_tasks": [], "merged_tasks": []},
                jobs=1,
                presubmit_cmd="test",
                backend="gemini",
                serena_enabled=False,
                cache_lock=MagicMock(),
                dashboard=mock_ctx,
            )

            # Verify all 3 missing files were logged
            log_calls = [str(c) for c in mock_ctx.log.call_args_list]
            missing_logs = [c for c in log_calls if "missing" in c.lower()]
            assert len(missing_logs) >= 3, f"Should log all 3 missing files, got: {missing_logs}"
            
            # Verify FATAL message mentions 3 files
            fatal_msgs = [c for c in log_calls if "3" in c and "copy_files" in c]
            assert len(fatal_msgs) > 0, "FATAL message should mention 3 missing files"

    def test_validation_includes_per_agent_copy_files(self, tmp_path):
        """Validation should check per-agent docker config copy_files too."""
        missing_src = tmp_path / "agent_missing.json"

        global_docker = DockerConfig(
            image="test:latest",
            volumes=[],
            copy_files=[]  # Global has no copy_files
        )

        agent_docker = DockerConfig(
            image="agent:latest",
            volumes=[],
            copy_files=[
                DockerCopyFile(src=str(missing_src), dest="/container/agent.json"),
            ]
        )

        from workflow_lib.agent_pool import AgentConfig

        agent_config = AgentConfig(
            name="test-agent",
            backend="gemini",
            user="test",
            parallel=1,
            priority=1,
            quota_time=60,
            docker_config=agent_docker,
        )

        validation_logged = []
        exit_called = []

        def track_log(msg, *args, **kwargs):
            validation_logged.append(str(msg))

        def track_exit(code):
            exit_called.append(code)
            raise SystemExit(code)

        with patch("workflow_lib.executor.get_docker_config", return_value=global_docker), \
             patch("workflow_lib.config.get_agent_pool_configs", return_value=[agent_config]), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.sys.exit", side_effect=track_exit), \
             patch("workflow_lib.executor.notify_failure"):

            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock(side_effect=track_log)
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            master_dag = {"phase_0/test_task.md": []}
            state = {"completed_tasks": [], "merged_tasks": []}

            with pytest.raises(SystemExit):
                _execute_dag_inner(
                    root_dir=str(tmp_path),
                    master_dag=master_dag,
                    state=state,
                    jobs=1,
                    presubmit_cmd="test",
                    backend="gemini",
                    serena_enabled=False,
                    cache_lock=MagicMock(),
                    dashboard=mock_ctx,
                )

            # Verify validation detected the agent's missing file
            assert any("Validating docker copy_files" in msg for msg in validation_logged), \
                "Validation should run"

            # Verify the missing file was logged with agent name
            missing_log = [msg for msg in validation_logged if "agent_missing.json" in msg]
            assert len(missing_log) > 0, "Should log missing file"
            assert any("test-agent" in msg for msg in missing_log), "Should mention agent name"

            # Verify exit was called
            assert exit_called == [1], "Should exit with code 1"

    def test_validation_happens_before_task_scheduling(self, tmp_path):
        """Validation must happen BEFORE any tasks are scheduled or run.

        This test verifies that validation runs and sys.exit is called
        before any task processing can occur.
        """
        missing_src = tmp_path / "missing.json"

        docker_cfg = DockerConfig(
            image="test:latest",
            volumes=[],
            copy_files=[
                DockerCopyFile(src=str(missing_src), dest="/container/file.json"),
            ]
        )

        validation_logged = []
        exit_called = []

        def track_log(msg, *args, **kwargs):
            validation_logged.append(str(msg))

        def track_exit(code):
            exit_called.append(code)
            raise SystemExit(code)

        with patch("workflow_lib.executor.get_docker_config", return_value=docker_cfg), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.sys.exit", side_effect=track_exit), \
             patch("workflow_lib.executor.notify_failure"):  # Mock to avoid webhook issues
            
            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock(side_effect=track_log)
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            master_dag = {"phase_0/test_task.md": []}
            state = {"completed_tasks": [], "merged_tasks": []}

            # Should raise SystemExit due to missing file
            with pytest.raises(SystemExit):
                _execute_dag_inner(
                    root_dir=str(tmp_path),
                    master_dag=master_dag,
                    state=state,
                    jobs=1,
                    presubmit_cmd="test",
                    backend="gemini",
                    serena_enabled=False,
                    cache_lock=MagicMock(),
                    dashboard=mock_ctx,
                )

            # Verify validation ran before exit
            assert any("Validating docker copy_files" in msg for msg in validation_logged), \
                "Validation should run before task scheduling"
            
            # Verify exit was called with code 1
            assert exit_called == [1], "Should exit with code 1"

    def test_no_validation_when_docker_config_is_none(self, tmp_path):
        """No validation should occur when docker_config is None."""
        with patch("workflow_lib.executor.get_docker_config", return_value=None), \
             patch("workflow_lib.config.get_agent_pool_configs", return_value=[]), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.concurrent.futures.ThreadPoolExecutor"):
            
            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            _execute_dag_inner(
                root_dir=str(tmp_path),
                master_dag={},
                state={"completed_tasks": [], "merged_tasks": []},
                jobs=1,
                presubmit_cmd="test",
                backend="gemini",
                serena_enabled=False,
                cache_lock=MagicMock(),
                dashboard=mock_ctx,
            )

            # Verify validation was NOT logged
            validation_logs = [c for c in mock_ctx.log.call_args_list 
                             if "Validating docker copy_files" in str(c)]
            assert len(validation_logs) == 0, "Should not validate when docker_config is None"

    def test_no_validation_when_copy_files_is_empty(self, tmp_path):
        """No validation should occur when copy_files list is empty."""
        docker_cfg = DockerConfig(
            image="test:latest",
            volumes=[],
            copy_files=[]  # Empty list
        )

        with patch("workflow_lib.executor.get_docker_config", return_value=docker_cfg), \
             patch("workflow_lib.config.get_agent_pool_configs", return_value=[]), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.concurrent.futures.ThreadPoolExecutor"):
            
            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            _execute_dag_inner(
                root_dir=str(tmp_path),
                master_dag={},
                state={"completed_tasks": [], "merged_tasks": []},
                jobs=1,
                presubmit_cmd="test",
                backend="gemini",
                serena_enabled=False,
                cache_lock=MagicMock(),
                dashboard=mock_ctx,
            )

            # Verify validation was NOT logged (no configs have copy_files)
            validation_logs = [c for c in mock_ctx.log.call_args_list 
                             if "Validating docker copy_files" in str(c)]
            assert len(validation_logs) == 0, "Should not validate when all copy_files are empty"

    def test_file_owned_by_other_user_detected_with_sudo(self, tmp_path):
        """Files owned by other users should be detected via sudo test -e."""
        # This test verifies the sudo approach works even if file exists
        # but is owned by a different user (simulated by mocking sudo test -e)

        existing_src = tmp_path / "existing.json"
        existing_src.write_text("data")

        docker_cfg = DockerConfig(
            image="test:latest",
            volumes=[],
            copy_files=[
                DockerCopyFile(src=str(existing_src), dest="/container/file.json"),
            ]
        )

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["sudo", "test", "-e"]:
                # Simulate file exists and is accessible via sudo
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("workflow_lib.executor.get_docker_config", return_value=docker_cfg), \
             patch("workflow_lib.config.get_agent_pool_configs", return_value=[]), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="origin"), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="https://test.com"), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.make_dashboard") as mock_dashboard, \
             patch("workflow_lib.executor.subprocess.run", side_effect=fake_run), \
             patch("workflow_lib.executor.concurrent.futures.ThreadPoolExecutor"):

            mock_ctx = MagicMock()
            mock_ctx.log = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_dashboard.return_value = mock_ctx

            _execute_dag_inner(
                root_dir=str(tmp_path),
                master_dag={},
                state={"completed_tasks": [], "merged_tasks": []},
                jobs=1,
                presubmit_cmd="test",
                backend="gemini",
                serena_enabled=False,
                cache_lock=MagicMock(),
                dashboard=mock_ctx,
            )

            # Verify validation passed (file detected via sudo)
            mock_ctx.log.assert_any_call("   All 1 copy_files sources validated across 1 config(s).")
