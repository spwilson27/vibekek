import os
import shutil
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from workflow_lib.executor import process_task, merge_task
import workflow_lib.executor as executor_mod

def _init_git_repo(root_dir: str):
    import subprocess
    subprocess.run(["git", "init"], cwd=root_dir, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root_dir, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "Initial commit"], cwd=root_dir, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=root_dir, check=True)

class TestProcessTaskCleanup:
    def test_process_task_cleanup_false_on_failure(self, tmp_path):
        """Test process_task leaves temp dir on failure when cleanup=False."""
        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        created_dirs = []
        original_mkdtemp = tempfile.mkdtemp
        def _fake_mkdtemp(**kwargs):
            d = original_mkdtemp(**kwargs)
            created_dirs.append(d)
            return d

        with patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("tempfile.mkdtemp", side_effect=_fake_mkdtemp), \
             patch("subprocess.run") as mock_run:
            
            def _fake_run(cmd, **kwargs):
                res = MagicMock(returncode=0, stdout="", stderr=b"")
                # Fail the presubmit
                if isinstance(cmd, list) and len(cmd) > 0 and "echo ok" in " ".join(cmd):
                    res.returncode = 1
                    res.stdout = "FAIL"
                return res

            mock_run.side_effect = _fake_run

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=1, cleanup=False)
            
            assert result is False
            # Staged architecture: 3 tmpdirs (impl, review, validate), only the
            # failing stage's dir is retained when cleanup=False.
            assert len(created_dirs) == 3
            assert not os.path.exists(created_dirs[0]), "Impl dir should be cleaned (stage succeeded)"
            assert not os.path.exists(created_dirs[1]), "Review dir should be cleaned (stage succeeded)"
            assert os.path.exists(created_dirs[2]), "Validate dir should NOT be cleaned when cleanup=False"
            # Manually clean it up so we don't leak locally during tests
            shutil.rmtree(created_dirs[2], ignore_errors=True)

    def test_process_task_cleanup_true_on_failure(self, tmp_path):
        """Test process_task removes temp dir on failure when cleanup=True."""
        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        created_dirs = []
        original_mkdtemp = tempfile.mkdtemp
        def _fake_mkdtemp(**kwargs):
            d = original_mkdtemp(**kwargs)
            created_dirs.append(d)
            return d

        with patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("tempfile.mkdtemp", side_effect=_fake_mkdtemp), \
             patch("subprocess.run") as mock_run:
            
            def _fake_run(cmd, **kwargs):
                res = MagicMock(returncode=0, stdout="", stderr=b"")
                if isinstance(cmd, list) and len(cmd) > 0 and "echo ok" in " ".join(cmd):
                    res.returncode = 1
                return res
            mock_run.side_effect = _fake_run

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=1, cleanup=True)
            
            assert result is False
            # Staged architecture: 3 tmpdirs (impl, review, validate), all cleaned when cleanup=True.
            assert len(created_dirs) == 3
            for d in created_dirs:
                assert not os.path.exists(d), f"Temp dir {d} SHOULD be cleaned up on failure when cleanup=True"


class TestMergeTaskCleanup:
    def test_merge_task_cleanup_false_on_failure(self, tmp_path):
        """Test merge_task leaves temp dir on failure when cleanup=False."""
        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        created_dirs = []
        original_mkdtemp = tempfile.mkdtemp
        def _fake_mkdtemp(**kwargs):
            d = original_mkdtemp(**kwargs)
            created_dirs.append(d)
            return d

        with patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("tempfile.mkdtemp", side_effect=_fake_mkdtemp), \
             patch("subprocess.run") as mock_run:
            
            def _fake_run(cmd, **kwargs):
                res = MagicMock(returncode=0, stdout="", stderr=b"")
                if isinstance(cmd, list) and "merge" in cmd and "--squash" in cmd:
                    res.returncode = 1
                if isinstance(cmd, list) and "rebase" in cmd:
                    res.returncode = 1
                return res

            mock_run.side_effect = _fake_run

            with patch("workflow_lib.executor.run_agent", return_value=False):
                result = merge_task(root, "phase_1/sub/01_a.md", "echo ok",
                                      backend="gemini", max_retries=1, cleanup=False)
            
            assert result is False
            assert len(created_dirs) == 1
            assert os.path.exists(created_dirs[0]), "Temp dir should NOT be cleaned up on failure when cleanup=False"
            # Cleanup for the test environment
            shutil.rmtree(created_dirs[0], ignore_errors=True)

    def test_merge_task_cleanup_true_on_failure(self, tmp_path):
        """Test merge_task removes temp dir on failure when cleanup=True."""
        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        created_dirs = []
        original_mkdtemp = tempfile.mkdtemp
        def _fake_mkdtemp(**kwargs):
            d = original_mkdtemp(**kwargs)
            created_dirs.append(d)
            return d

        with patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("tempfile.mkdtemp", side_effect=_fake_mkdtemp), \
             patch("subprocess.run") as mock_run:
            
            def _fake_run(cmd, **kwargs):
                res = MagicMock(returncode=0, stdout="", stderr=b"")
                if isinstance(cmd, list) and "merge" in cmd and "--squash" in cmd:
                    res.returncode = 1
                if isinstance(cmd, list) and "rebase" in cmd:
                    res.returncode = 1
                return res

            mock_run.side_effect = _fake_run

            with patch("workflow_lib.executor.run_agent", return_value=False):
                result = merge_task(root, "phase_1/sub/01_a.md", "echo ok",
                                      backend="gemini", max_retries=1, cleanup=True)
            
            assert result is False
            assert len(created_dirs) == 1
            assert not os.path.exists(created_dirs[0]), "Temp dir SHOULD be cleaned up on failure when cleanup=True"
