"""Tests for Docker container support (global config, git clone/push lifecycle)."""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import subprocess
import warnings
import json
import pytest
from unittest.mock import patch, MagicMock, call

from workflow_lib.agent_pool import DockerConfig, DockerCopyFile
from workflow_lib.config import merge_docker_configs
from workflow_lib.runners import AIRunner, GeminiRunner, QwenRunner, make_runner
from workflow_lib.executor import (
    _DOCKER_ENV_SKIP,
    _docker_exec,
    _set_cargo_target_dir,
    _write_container_env_file,
    _start_task_container,
    _stop_task_container,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _docker_cfg(image="test-image:latest", volumes=None, copy_files=None, pivot_remote="origin",
                cpu_nice=None, ionice_class=None):
    return DockerConfig(
        image=image,
        pivot_remote=pivot_remote,
        volumes=volumes or [],
        copy_files=copy_files or [],
        cpu_nice=cpu_nice,
        ionice_class=ionice_class,
    )


# ---------------------------------------------------------------------------
# Dockerfile template tests
# ---------------------------------------------------------------------------

class TestDockerfileTemplate:
    """Tests for Dockerfile template and non-root user configuration."""

    def test_dockerfile_template_exists(self):
        """Dockerfile template should exist in .tools/templates/."""
        from workflow_lib.constants import TOOLS_DIR
        template_path = os.path.join(TOOLS_DIR, "templates", "Dockerfile")
        assert os.path.exists(template_path), f"Dockerfile template not found at {template_path}"

    def test_dockerfile_template_has_username_arg(self):
        """Dockerfile template should define USERNAME build argument."""
        from workflow_lib.constants import TOOLS_DIR
        template_path = os.path.join(TOOLS_DIR, "templates", "Dockerfile")
        with open(template_path) as f:
            content = f.read()
        assert "ARG USERNAME=" in content, "Dockerfile template missing ARG USERNAME"

    def test_dockerfile_template_has_user_uid_arg(self):
        """Dockerfile template should define USER_UID build argument."""
        from workflow_lib.constants import TOOLS_DIR
        template_path = os.path.join(TOOLS_DIR, "templates", "Dockerfile")
        with open(template_path) as f:
            content = f.read()
        assert "ARG USER_UID=" in content, "Dockerfile template missing ARG USER_UID"

    def test_dockerfile_template_creates_user(self):
        """Dockerfile template should create non-root user with useradd."""
        from workflow_lib.constants import TOOLS_DIR
        template_path = os.path.join(TOOLS_DIR, "templates", "Dockerfile")
        with open(template_path) as f:
            content = f.read()
        assert "useradd" in content, "Dockerfile template missing useradd command"
        assert "${USERNAME}" in content or "weaver" in content, \
            "Dockerfile template should use USERNAME arg for user creation"

    def test_dockerfile_template_sets_sudo(self):
        """Dockerfile template should configure passwordless sudo for non-root user."""
        from workflow_lib.constants import TOOLS_DIR
        template_path = os.path.join(TOOLS_DIR, "templates", "Dockerfile")
        with open(template_path) as f:
            content = f.read()
        assert "NOPASSWD:ALL" in content, "Dockerfile template missing NOPASSWD sudo config"

    def test_dockerfile_template_switches_to_user(self):
        """Dockerfile template should switch to non-root user with USER directive."""
        from workflow_lib.constants import TOOLS_DIR
        template_path = os.path.join(TOOLS_DIR, "templates", "Dockerfile")
        with open(template_path) as f:
            content = f.read()
        assert "USER ${USERNAME}" in content or "USER weaver" in content, \
            "Dockerfile template missing USER directive to switch to non-root user"

    def test_dockerfile_template_creates_config_dirs(self):
        """Dockerfile template should create AI CLI config directories."""
        from workflow_lib.constants import TOOLS_DIR
        template_path = os.path.join(TOOLS_DIR, "templates", "Dockerfile")
        with open(template_path) as f:
            content = f.read()
        assert ".claude" in content, "Dockerfile template missing .claude directory"
        assert ".gemini" in content, "Dockerfile template missing .gemini directory"
        assert ".qwen" in content, "Dockerfile template missing .qwen directory"

    def test_dockerfile_template_ownership(self):
        """Dockerfile template should set ownership of workspace to non-root user."""
        from workflow_lib.constants import TOOLS_DIR
        template_path = os.path.join(TOOLS_DIR, "templates", "Dockerfile")
        with open(template_path) as f:
            content = f.read()
        assert "chown" in content, "Dockerfile template missing chown command"
        assert "/workspace" in content, "Dockerfile template missing /workspace ownership"


# ---------------------------------------------------------------------------
# Root Dockerfile tests
# ---------------------------------------------------------------------------

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))

@pytest.mark.skipif(
    not os.path.exists(os.path.join(_ROOT_DIR, 'Dockerfile')),
    reason="Dockerfile not yet generated"
)
class TestRootDockerfile:
    """Tests for the root Dockerfile matching template configuration."""

    def test_root_dockerfile_exists(self):
        """Root Dockerfile should exist."""
        from workflow_lib.constants import ROOT_DIR
        dockerfile_path = os.path.join(ROOT_DIR, "Dockerfile")
        assert os.path.exists(dockerfile_path), f"Dockerfile not found at {dockerfile_path}"

    def test_root_dockerfile_has_username_arg(self):
        """Root Dockerfile should define USERNAME build argument."""
        from workflow_lib.constants import ROOT_DIR
        dockerfile_path = os.path.join(ROOT_DIR, "Dockerfile")
        with open(dockerfile_path) as f:
            content = f.read()
        assert "ARG USERNAME=" in content, "Root Dockerfile missing ARG USERNAME"

    def test_root_dockerfile_has_user_uid_arg(self):
        """Root Dockerfile should define USER_UID build argument."""
        from workflow_lib.constants import ROOT_DIR
        dockerfile_path = os.path.join(ROOT_DIR, "Dockerfile")
        with open(dockerfile_path) as f:
            content = f.read()
        assert "ARG USER_UID=" in content, "Root Dockerfile missing ARG USER_UID"

    def test_root_dockerfile_default_username(self):
        """Root Dockerfile should default USERNAME to 'username'."""
        from workflow_lib.constants import ROOT_DIR
        dockerfile_path = os.path.join(ROOT_DIR, "Dockerfile")
        with open(dockerfile_path) as f:
            content = f.read()
        # Check for default value in ARG statement
        assert "ARG USERNAME=username" in content, \
            "Root Dockerfile should default USERNAME to 'username'"

    def test_root_dockerfile_default_uid(self):
        """Root Dockerfile should default USER_UID to 1201."""
        from workflow_lib.constants import ROOT_DIR
        dockerfile_path = os.path.join(ROOT_DIR, "Dockerfile")
        with open(dockerfile_path) as f:
            content = f.read()
        assert "ARG USER_UID=1201" in content, \
            "Root Dockerfile should default USER_UID to 1201"


# ---------------------------------------------------------------------------
# .workflow.jsonc Docker config tests
# ---------------------------------------------------------------------------

class TestWorkflowDockerConfig:
    """Tests for .workflow.jsonc Docker configuration consistency."""

    def test_workflow_config_copy_files_use_home_username(self):
        """.workflow.jsonc copy_files should use /home/username/ destinations."""
        from workflow_lib.constants import ROOT_DIR
        config_path = os.path.join(ROOT_DIR, ".workflow.jsonc")
        with open(config_path) as f:
            content = f.read()
        # Should not have any /root/ destinations
        assert '"/root/' not in content or content.count('"/root/') == 0, \
            ".workflow.jsonc should not use /root/ destinations for copy_files"
        # Should use /home/username/ destinations
        assert '"/home/username/' in content, \
            ".workflow.jsonc should use /home/username/ destinations for copy_files"


# ---------------------------------------------------------------------------
# RAG server in container tests
# ---------------------------------------------------------------------------

class TestRAGServerInContainer:
    """Tests for RAG MCP server startup within Docker containers."""

    def test_start_rag_server_with_container_name(self):
        """RAG server should use docker exec when container_name is provided."""
        from workflow_lib.rag_integration import start_rag_server
        import workflow_lib.rag_integration as rag_mod
        import os

        # Mock all file system checks to pass
        with patch.object(rag_mod, "RAG_TOOL_DIR", "/fake/rag/tool/dir"), \
             patch.object(os.path, "isdir", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", return_value=""), \
             patch("pathlib.Path.unlink"), \
             patch("pathlib.Path.write_text"), \
             patch("subprocess.run") as mock_run:
            start_rag_server("/workspace", verbose=False, container_name="test-ctr")

        # Should call subprocess.run with docker exec
        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert "docker" in call_args
        assert "exec" in call_args
        assert "test-ctr" in call_args

    def test_start_rag_server_without_container_uses_host(self):
        """RAG server should start directly on host when not in Docker mode."""
        from workflow_lib.rag_integration import start_rag_server
        import workflow_lib.rag_integration as rag_mod
        import os

        mock_proc = MagicMock()
        mock_proc.pid = 12345

        # Mock all file system checks to pass
        with patch.object(rag_mod, "RAG_TOOL_DIR", "/fake/rag/tool/dir"), \
             patch.object(os.path, "isdir", return_value=True), \
             patch("pathlib.Path.exists", side_effect=[False, False]), \
             patch("builtins.open", MagicMock()), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            start_rag_server("/tmp/test-workspace", verbose=False)

        # Should call Popen for host execution
        assert mock_popen.called
        call_args = mock_popen.call_args[0][0]
        # Should use python -m rag_mcp.cli directly, not docker
        assert "docker" not in call_args
        # Check that python executable is in the command (may be /usr/bin/python, python, python3, etc.)
        cmd_str = " ".join(call_args)
        assert "python" in cmd_str
        assert "rag_mcp.cli" in cmd_str

    def test_rag_server_command_includes_workspace(self):
        """RAG server command should include workspace path."""
        from workflow_lib.rag_integration import start_rag_server
        import workflow_lib.rag_integration as rag_mod
        import os

        mock_proc = MagicMock()
        workspace = "/workspace"

        with patch.object(rag_mod, "RAG_TOOL_DIR", "/fake/rag/tool/dir"), \
             patch.object(os.path, "isdir", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", return_value=""), \
             patch("pathlib.Path.unlink"), \
             patch("pathlib.Path.write_text"), \
             patch("subprocess.run") as mock_run:
            start_rag_server(workspace, verbose=False, container_name="ctr")

        call_args = mock_run.call_args[0][0]
        # Workspace should be in the command via --workdir
        assert "--workdir" in call_args
        assert workspace in call_args

    def test_rag_server_verbose_flag_passed(self):
        """RAG server verbose setting should print status messages."""
        from workflow_lib.rag_integration import start_rag_server
        import workflow_lib.rag_integration as rag_mod
        import os
        import io

        # Capture stdout to verify verbose output
        captured = io.StringIO()

        with patch.object(rag_mod, "RAG_TOOL_DIR", "/fake/rag/tool/dir"), \
             patch.object(os.path, "isdir", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", return_value=""), \
             patch("pathlib.Path.unlink"), \
             patch("pathlib.Path.write_text"), \
             patch("subprocess.run") as mock_run, \
             patch("sys.stdout", captured):
            start_rag_server("/workspace", verbose=True, container_name="ctr")

        # Verbose output should mention starting the server
        output = captured.getvalue()
        assert "Starting" in output or "RAG" in output


# ---------------------------------------------------------------------------
# process_task with RAG in Docker
# ---------------------------------------------------------------------------
# Note: Full e2e tests for RAG server in Docker require actual Docker daemon
# and are provided in tests/test_docker_integration.py instead.
# The unit tests above verify the correct code paths are taken.


class TestProcessTaskWithRAGAndDocker:
    """Tests for process_task with RAG server in Docker containers.

    These tests verify the code paths are correct. Full e2e testing with
    actual RAG server startup requires Docker daemon and is done separately.
    """

    def test_rag_server_not_started_when_disabled(self, tmp_path):
        """RAG server should not start when RAG is disabled."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo_for_docker(root)

        docker_cfg = DockerConfig(image="test-img:latest")
        rag_started = [False]

        def fake_docker_exec(container_name, cmd, **kwargs):
            result = MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["git", "status"]:
                result.stdout = ""
            return result

        def fake_start_task_container(container_name, *args, **kwargs):
            # Register the container name in the module's active set
            executor_mod._active_containers.add(container_name)

        with patch("workflow_lib.executor._write_container_env_file", return_value="/tmp/test.env"), \
             patch("workflow_lib.executor._start_task_container", side_effect=fake_start_task_container), \
             patch("workflow_lib.executor._stop_task_container"), \
             patch("workflow_lib.executor._docker_exec", side_effect=fake_docker_exec), \
             patch("workflow_lib.executor.subprocess.run", return_value=MagicMock(returncode=0, stdout="running", stderr="")), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("workflow_lib.executor.get_rag_enabled", return_value=False), \
             patch("workflow_lib.rag_integration.start_rag_server",
                   side_effect=lambda *a, **k: rag_started.__setitem__(0, True) or MagicMock()):

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", docker_config=docker_cfg, cleanup=True)

        assert result is True
        assert not rag_started[0], "RAG server should not start when disabled"


# ---------------------------------------------------------------------------
# DockerConfig dataclass
# ---------------------------------------------------------------------------

class TestDockerDataclasses:
    def test_docker_config_defaults(self):
        dc = DockerConfig(image="myimage:latest")
        assert dc.image == "myimage:latest"
        assert dc.pivot_remote == "origin"
        assert dc.volumes == []
        assert dc.copy_files == []

    def test_docker_config_pivot_remote(self):
        dc = DockerConfig(image="img", pivot_remote="upstream")
        assert dc.pivot_remote == "upstream"

    def test_docker_copy_file_fields(self):
        cf = DockerCopyFile(src="/host/file", dest="/container/file")
        assert cf.src == "/host/file"
        assert cf.dest == "/container/file"


# ---------------------------------------------------------------------------
# AIRunner._build_exec_cmd
# ---------------------------------------------------------------------------

class TestBuildExecCmd:
    def test_no_container_name_returns_cmd_unchanged(self):
        runner = GeminiRunner()
        cmd = ["gemini", "--model", "pro"]
        assert runner._build_exec_cmd(cmd) == cmd

    def test_with_container_name_wraps_with_docker_exec(self):
        runner = GeminiRunner(container_name="my-ctr")
        cmd = runner._build_exec_cmd(["gemini", "-y"])
        assert cmd[:3] == ["docker", "exec", "-i"]
        assert "--workdir" in cmd
        assert "/workspace" in cmd
        assert "my-ctr" in cmd
        assert cmd[-2:] == ["gemini", "-y"]

    def test_env_file_included_when_set(self):
        runner = GeminiRunner(container_name="my-ctr")
        runner._container_env_file = "/tmp/test.env"
        cmd = runner._build_exec_cmd(["gemini"])
        assert "--env-file" in cmd
        assert "/tmp/test.env" in cmd

    def test_no_env_file_when_not_set(self):
        runner = GeminiRunner(container_name="my-ctr")
        cmd = runner._build_exec_cmd(["gemini"])
        assert "--env-file" not in cmd

    def test_original_cmd_appended_at_end(self):
        runner = GeminiRunner(container_name="ctr")
        orig = ["qwen", "-y", "--output-format", "stream-json"]
        result = runner._build_exec_cmd(orig)
        assert result[-len(orig):] == orig


# ---------------------------------------------------------------------------
# AIRunner._wrap_cmd skips sudo when container_name is set
# ---------------------------------------------------------------------------

class TestWrapCmdWithDocker:
    def test_wrap_cmd_skips_sudo_when_container_name_set(self):
        runner = GeminiRunner(user="otheruser", container_name="my-ctr")
        cmd = ["gemini", "-y"]
        assert runner._wrap_cmd(cmd) == cmd  # no sudo wrapping

    def test_wrap_cmd_applies_sudo_when_no_container(self):
        runner = GeminiRunner(user="otheruser")
        with patch.dict(os.environ, {"USER": "currentuser"}):
            cmd = runner._wrap_cmd(["gemini", "-y"])
        assert "sudo" in cmd


# ---------------------------------------------------------------------------
# make_runner passes container_name
# ---------------------------------------------------------------------------

class TestMakeRunnerContainerName:
    def test_make_runner_passes_container_name_gemini(self):
        runner = make_runner("gemini", container_name="ctr-1")
        assert runner.container_name == "ctr-1"

    def test_make_runner_passes_container_name_claude(self):
        runner = make_runner("claude", container_name="ctr-2")
        assert runner.container_name == "ctr-2"

    def test_make_runner_passes_container_name_qwen(self):
        runner = make_runner("qwen", container_name="ctr-3")
        assert runner.container_name == "ctr-3"

    def test_make_runner_no_container_name(self):
        runner = make_runner("gemini")
        assert runner.container_name is None

    def test_qwen_soft_timeout_still_enabled_with_container(self):
        runner = make_runner("qwen", container_name="ctr")
        assert runner.soft_timeout == QwenRunner.DEFAULT_SOFT_TIMEOUT


# ---------------------------------------------------------------------------
# _write_container_env_file
# ---------------------------------------------------------------------------

class TestWriteContainerEnvFile:
    def test_creates_file(self, tmp_path):
        path = _write_container_env_file(str(tmp_path))
        assert os.path.exists(path)

    def test_contains_env_vars(self, tmp_path):
        with patch.dict(os.environ, {"MY_TEST_VAR": "hello_world"}, clear=False):
            path = _write_container_env_file(str(tmp_path))
        with open(path) as f:
            content = f.read()
        assert "MY_TEST_VAR=hello_world" in content

    def test_skips_identity_vars(self, tmp_path):
        with patch.dict(os.environ, {
            "HOME": "/home/mrwilson",
            "USER": "mrwilson",
            "LOGNAME": "mrwilson",
            "SHELL": "/bin/bash",
            "PWD": "/home/mrwilson/projects",
            "OLDPWD": "/tmp",
            "PATH": "/home/mrwilson/.cargo/bin:/usr/bin:/bin",
            "TMPDIR": "/var/folders/lq/abc123/T/",
            "TEMP": "C:\\Users\\user\\AppData\\Local\\Temp",
            "TMP": "/tmp",
        }, clear=False):
            path = _write_container_env_file(str(tmp_path))
        with open(path) as f:
            lines = f.readlines()
        for var in _DOCKER_ENV_SKIP:
            assert not any(line.startswith(f"{var}=") for line in lines), \
                f"{var} must not appear in container env-file"

    def test_skips_vars_with_newlines_in_value(self, tmp_path):
        with patch.dict(os.environ, {"BAD_VAR": "line1\nline2"}, clear=False):
            path = _write_container_env_file(str(tmp_path))
        with open(path) as f:
            content = f.read()
        assert "BAD_VAR" not in content


# ---------------------------------------------------------------------------
# _start_task_container
# ---------------------------------------------------------------------------

class TestStartTaskContainer:
    @pytest.fixture(autouse=True)
    def _mock_harness(self):
        """Mock harness.py existence check and copy for all container tests."""
        with patch("os.path.exists", return_value=True), \
             patch("shutil.copy2"):
            yield

    def test_calls_docker_run_d(self, tmp_path):
        dc = _docker_cfg(image="test-img:latest")
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            # Make files exist for copy_files validation
            if cmd[:3] == ["sudo", "test", "-e"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ai_test_ctr", dc, env_file, print)

        assert calls, "subprocess.run not called"
        start_cmd = calls[0]
        assert "docker" in start_cmd
        assert "run" in start_cmd
        assert "-d" in start_cmd
        assert "--name" in start_cmd
        assert "ai_test_ctr" in start_cmd
        assert "test-img:latest" in start_cmd
        assert "sleep" in start_cmd
        assert "infinity" in start_cmd

    def test_volumes_added(self, tmp_path):
        dc = _docker_cfg(volumes=["/data:/data:ro"])
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr", dc, env_file, print)

        start_cmd = calls[0]
        v_args = [start_cmd[i+1] for i in range(len(start_cmd)) if start_cmd[i] == "-v"]
        assert "/data:/data:ro" in v_args

    def test_copy_files_mounted_readonly(self, tmp_path):
        src = tmp_path / "creds"
        src.write_text("secret")
        dc = _docker_cfg(copy_files=[DockerCopyFile(src=str(src), dest="/root/.creds")])
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            # Return "username" for the `id -un` query so chown logic triggers
            stdout = "username\n" if "id" in cmd and "-un" in cmd else ""
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr", dc, env_file, print)

        # First call should be docker run (without -v for copy_files)
        start_cmd = calls[0]
        v_args = [start_cmd[i+1] for i in range(len(start_cmd)) if start_cmd[i] == "-v"]
        # copy_files should NOT be in volume mounts anymore (uses docker cp instead)
        assert f"{src}:/root/.creds:ro" not in v_args

        # Subsequent calls should be docker cp, chmod, and chown for the copy_file
        assert any("docker" in str(cmd) and "cp" in str(cmd) and str(src) in str(cmd) for cmd in calls)
        assert any("chmod" in str(cmd) and "/root/.creds" in str(cmd) for cmd in calls)
        assert any("chown" in str(cmd) for cmd in calls), \
            "copy_files must be chown'd to the container user so CLIs can write back (e.g. oauth token refresh)"

    def test_copy_file_missing_src_raises(self, tmp_path):
        dc = _docker_cfg(copy_files=[DockerCopyFile(src="/nonexistent/file.txt", dest="/dest")])
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        with pytest.raises(FileNotFoundError, match="docker copy_files src does not exist"):
            _start_task_container("ctr", dc, env_file, print)

    def test_duplicate_dest_warns_and_skips(self, tmp_path):
        src = tmp_path / "file.txt"
        src.write_text("data")
        dc = _docker_cfg(
            volumes=["/host/path:/shared/path"],
            copy_files=[DockerCopyFile(src=str(src), dest="/shared/path")]
        )
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with patch("subprocess.run", side_effect=fake_run):
                _start_task_container("ctr", dc, env_file, print)

        assert any("/shared/path" in str(w.message) or "duplicate" in str(w.message).lower() for w in caught)
        start_cmd = calls[0]
        v_args = [start_cmd[i+1] for i in range(len(start_cmd)) if start_cmd[i] == "-v"]
        ro_mounts = [v for v in v_args if ":ro" in v and "/shared/path" in v]
        assert not ro_mounts, f"Duplicate copy_file was not skipped: {ro_mounts}"

    def test_env_file_passed_to_docker_run(self, tmp_path):
        dc = _docker_cfg()
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr", dc, env_file, print)

        start_cmd = calls[0]
        assert "--env-file" in start_cmd
        idx = start_cmd.index("--env-file")
        assert start_cmd[idx + 1] == env_file


    def test_cpu_nice_sets_cpu_shares(self, tmp_path):
        """cpu_nice value is mapped to --cpu-shares in docker run."""
        dc = _docker_cfg(cpu_nice=14)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr", dc, env_file, print)

        start_cmd = calls[0]
        assert "--cpu-shares" in start_cmd
        idx = start_cmd.index("--cpu-shares")
        # nice 14 → shares = max(2, int(1024 / (1 + 14))) = 68
        assert start_cmd[idx + 1] == "68"

    def test_ionice_class_sets_blkio_weight(self, tmp_path):
        """ionice_class is mapped to --blkio-weight in docker run."""
        dc = _docker_cfg(ionice_class=3)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr", dc, env_file, print)

        start_cmd = calls[0]
        assert "--blkio-weight" in start_cmd
        idx = start_cmd.index("--blkio-weight")
        assert start_cmd[idx + 1] == "10"  # class 3 (idle) → weight 10

    def test_merge_boost_cpu_nice(self, tmp_path):
        """Merge containers get halved cpu_nice for higher priority."""
        dc = _docker_cfg(cpu_nice=14)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr", dc, env_file, print, is_merge=True)

        start_cmd = calls[0]
        idx = start_cmd.index("--cpu-shares")
        # merge: effective_nice = 14 // 2 = 7 → shares = int(1024 / 8) = 128
        assert start_cmd[idx + 1] == "128"

    def test_merge_boost_ionice(self, tmp_path):
        """Merge containers bump ionice up one class."""
        dc = _docker_cfg(ionice_class=3)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr", dc, env_file, print, is_merge=True)

        start_cmd = calls[0]
        idx = start_cmd.index("--blkio-weight")
        # merge: class 3 → class 2 (best-effort) → weight 500
        assert start_cmd[idx + 1] == "500"

    def test_no_nice_flags_when_unset(self, tmp_path):
        """No --cpu-shares or --blkio-weight when nice values are None."""
        dc = _docker_cfg()
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and ("inspect" in cmd or "ps" in cmd):
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr", dc, env_file, print)

        start_cmd = calls[0]
        assert "--cpu-shares" not in start_cmd
        assert "--blkio-weight" not in start_cmd


# ---------------------------------------------------------------------------
# _stop_task_container
# ---------------------------------------------------------------------------

class TestStopTaskContainer:
    def test_calls_docker_rm_f(self):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", side_effect=fake_run):
            _stop_task_container("my-ctr", print)
        assert any("docker" in c and "rm" in c and "-f" in c and "my-ctr" in c for c in calls)

    def test_noop_for_empty_name(self):
        with patch("subprocess.run") as mock_run:
            _stop_task_container("", print)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# merge_docker_configs
# ---------------------------------------------------------------------------

class TestMergeDockerConfigs:
    def _cfg(self, image="img:latest", pivot_remote="origin", volumes=None, copy_files=None):
        return DockerConfig(
            image=image,
            pivot_remote=pivot_remote,
            volumes=volumes or [],
            copy_files=copy_files or [],
        )

    def test_none_base_returns_override(self):
        override = self._cfg(image="override:latest")
        assert merge_docker_configs(None, override) is override

    def test_none_override_returns_base(self):
        base = self._cfg(image="base:latest")
        assert merge_docker_configs(base, None) is base

    def test_both_none_returns_none(self):
        assert merge_docker_configs(None, None) is None

    def test_override_image_wins(self):
        base = self._cfg(image="base:latest")
        override = self._cfg(image="new:1.0")
        result = merge_docker_configs(base, override)
        assert result.image == "new:1.0"

    def test_empty_override_image_falls_back_to_base(self):
        base = self._cfg(image="base:latest")
        override = self._cfg(image="")
        result = merge_docker_configs(base, override)
        assert result.image == "base:latest"

    def test_override_copy_files_replaces_base(self):
        base_cf = [DockerCopyFile(src="/host/base.json", dest="/root/base.json")]
        override_cf = [DockerCopyFile(src="/host/agent.json", dest="/root/agent.json")]
        base = self._cfg(copy_files=base_cf)
        override = self._cfg(copy_files=override_cf)
        result = merge_docker_configs(base, override)
        assert len(result.copy_files) == 1
        assert result.copy_files[0].src == "/host/agent.json"

    def test_empty_override_copy_files_falls_back_to_base(self):
        base_cf = [DockerCopyFile(src="/host/base.json", dest="/root/base.json")]
        base = self._cfg(copy_files=base_cf)
        override = self._cfg(copy_files=[])
        result = merge_docker_configs(base, override)
        assert result.copy_files[0].src == "/host/base.json"

    def test_override_volumes_replaces_base(self):
        base = self._cfg(volumes=["/data:/data"])
        override = self._cfg(volumes=["/other:/other"])
        result = merge_docker_configs(base, override)
        assert result.volumes == ["/other:/other"]

    def test_empty_override_volumes_falls_back_to_base(self):
        base = self._cfg(volumes=["/data:/data"])
        override = self._cfg(volumes=[])
        result = merge_docker_configs(base, override)
        assert result.volumes == ["/data:/data"]

    def test_override_pivot_remote_wins_when_non_default(self):
        base = self._cfg(pivot_remote="origin")
        override = self._cfg(pivot_remote="upstream")
        result = merge_docker_configs(base, override)
        assert result.pivot_remote == "upstream"

    def test_override_pivot_remote_falls_back_to_base_when_default(self):
        base = self._cfg(pivot_remote="custom-remote")
        override = self._cfg(pivot_remote="origin")  # "origin" == default, treated as unset
        result = merge_docker_configs(base, override)
        assert result.pivot_remote == "custom-remote"

    def test_mixed_override_partial_fields(self):
        """Override only copy_files; image and pivot_remote fall back to base."""
        base_cf = [DockerCopyFile(src="/host/mrwilson.json", dest="/root/creds.json")]
        sub_cf = [DockerCopyFile(src="/host/sub.json", dest="/root/creds.json")]
        base = self._cfg(image="axel-de:latest", pivot_remote="origin", copy_files=base_cf)
        override = self._cfg(image="axel-de:latest", copy_files=sub_cf)
        result = merge_docker_configs(base, override)
        assert result.image == "axel-de:latest"
        assert result.copy_files[0].src == "/host/sub.json"


# ---------------------------------------------------------------------------
# config.py — get_docker_config()
# ---------------------------------------------------------------------------

class TestConfigParsing:
    def test_parse_global_docker_block(self, tmp_path, monkeypatch):
        config = {
            "docker": {
                "image": "ubuntu:24.04",
                "pivot_remote": "upstream",
                "volumes": ["/data:/data"],
                "copy_files": [{"src": "/host/creds", "dest": "/container/creds"}]
            }
        }
        cfg_file = tmp_path / ".workflow.jsonc"
        cfg_file.write_text(json.dumps(config))

        import workflow_lib.config as config_mod
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_ROOT", str(cfg_file))
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_TOOLS", str(cfg_file))

        dc = config_mod.get_docker_config()
        assert dc is not None
        assert dc.image == "ubuntu:24.04"
        assert dc.pivot_remote == "upstream"
        assert dc.volumes == ["/data:/data"]
        assert len(dc.copy_files) == 1
        assert dc.copy_files[0].src == "/host/creds"
        assert dc.copy_files[0].dest == "/container/creds"

    def test_docker_block_missing_image_raises(self, tmp_path, monkeypatch):
        config = {"docker": {"volumes": ["/data:/data"]}}
        cfg_file = tmp_path / ".workflow.jsonc"
        cfg_file.write_text(json.dumps(config))

        import workflow_lib.config as config_mod
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_ROOT", str(cfg_file))
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_TOOLS", str(cfg_file))

        with pytest.raises(ValueError, match="missing required 'image' field"):
            config_mod.get_docker_config()

    def test_no_docker_block_returns_none(self, tmp_path, monkeypatch):
        config = {"agents": [{"name": "a", "backend": "gemini"}]}
        cfg_file = tmp_path / ".workflow.jsonc"
        cfg_file.write_text(json.dumps(config))

        import workflow_lib.config as config_mod
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_ROOT", str(cfg_file))
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_TOOLS", str(cfg_file))

        assert config_mod.get_docker_config() is None

    def test_pivot_remote_defaults_to_origin(self, tmp_path, monkeypatch):
        config = {"docker": {"image": "img:latest"}}
        cfg_file = tmp_path / ".workflow.jsonc"
        cfg_file.write_text(json.dumps(config))

        import workflow_lib.config as config_mod
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_ROOT", str(cfg_file))
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_TOOLS", str(cfg_file))

        dc = config_mod.get_docker_config()
        assert dc.pivot_remote == "origin"

    def test_copy_files_missing_src_raises(self, tmp_path, monkeypatch):
        config = {
            "docker": {
                "image": "ubuntu:24.04",
                "copy_files": [{"dest": "/container/file"}]
            }
        }
        cfg_file = tmp_path / ".workflow.jsonc"
        cfg_file.write_text(json.dumps(config))

        import workflow_lib.config as config_mod
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_ROOT", str(cfg_file))
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_TOOLS", str(cfg_file))

        with pytest.raises(ValueError, match="missing required field 'src'"):
            config_mod.get_docker_config()

    def test_agent_inherits_global_docker_config(self, tmp_path, monkeypatch):
        """Agent with no docker block inherits the global docker config."""
        config = {
            "docker": {"image": "base:latest"},
            "agents": [{"name": "a", "backend": "gemini"}],
        }
        cfg_file = tmp_path / ".workflow.jsonc"
        cfg_file.write_text(json.dumps(config))

        import workflow_lib.config as config_mod
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_ROOT", str(cfg_file))
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_TOOLS", str(cfg_file))

        agents = config_mod.get_agent_pool_configs()
        assert agents[0].docker_config is not None
        assert agents[0].docker_config.image == "base:latest"

    def test_agent_docker_override_replaces_copy_files(self, tmp_path, monkeypatch):
        """Agent docker block's copy_files replaces the global copy_files."""
        config = {
            "docker": {
                "image": "base:latest",
                "copy_files": [{"src": "/host/global.json", "dest": "/root/global.json"}],
            },
            "agents": [
                {
                    "name": "a", "backend": "gemini",
                    "docker": {
                        "image": "base:latest",
                        "copy_files": [{"src": "/host/agent.json", "dest": "/root/agent.json"}],
                    },
                }
            ],
        }
        cfg_file = tmp_path / ".workflow.jsonc"
        cfg_file.write_text(json.dumps(config))

        import workflow_lib.config as config_mod
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_ROOT", str(cfg_file))
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_TOOLS", str(cfg_file))

        agents = config_mod.get_agent_pool_configs()
        dc = agents[0].docker_config
        assert len(dc.copy_files) == 1
        assert dc.copy_files[0].src == "/host/agent.json"

    def test_agent_without_docker_override_has_none_when_no_global(self, tmp_path, monkeypatch):
        """Agent with no docker block and no global docker has docker_config=None."""
        config = {"agents": [{"name": "a", "backend": "gemini"}]}
        cfg_file = tmp_path / ".workflow.jsonc"
        cfg_file.write_text(json.dumps(config))

        import workflow_lib.config as config_mod
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_ROOT", str(cfg_file))
        monkeypatch.setattr(config_mod, "_CONFIG_FILE_TOOLS", str(cfg_file))

        agents = config_mod.get_agent_pool_configs()
        assert agents[0].docker_config is None


# ---------------------------------------------------------------------------
# _docker_exec
# ---------------------------------------------------------------------------

class TestDockerExec:
    def test_basic_exec_command(self):
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _docker_exec("my-ctr", ["echo", "hello"])

        assert calls[0][:3] == ["docker", "exec", "-i"]
        assert "--workdir" in calls[0]
        assert "/workspace" in calls[0]
        assert "my-ctr" in calls[0]
        assert calls[0][-2:] == ["echo", "hello"]

    def test_env_file_included_when_set(self):
        calls = []
        with patch("subprocess.run", side_effect=lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0, stdout="", stderr="")):
            _docker_exec("ctr", ["ls"], env_file="/tmp/test.env")
        assert "--env-file" in calls[0]
        assert "/tmp/test.env" in calls[0]

    def test_check_raises_on_nonzero(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="fail")):
            with pytest.raises(subprocess.CalledProcessError):
                _docker_exec("ctr", ["false"], check=True)

    def test_check_false_does_not_raise(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="")):
            result = _docker_exec("ctr", ["false"], check=False)
        assert result.returncode == 1

    def test_log_called_on_failure_with_stderr(self):
        """log callback is invoked with error details and stderr when check=True fails."""
        log_msgs = []
        with patch("subprocess.run", return_value=MagicMock(returncode=2, stdout="", stderr="some error")):
            with pytest.raises(subprocess.CalledProcessError):
                _docker_exec("ctr", ["bad", "cmd"], check=True, log=log_msgs.append)
        # Verify key messages are present in log output
        assert any("bad cmd" in m for m in log_msgs)
        assert any("some error" in m for m in log_msgs)


# ---------------------------------------------------------------------------
# process_task with docker_config
# ---------------------------------------------------------------------------

def _init_git_repo_for_docker(path: str):
    """Create a minimal git repo with a dev branch and a task file."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "branch", "dev"], cwd=path, check=True, capture_output=True, env=env)
    # Create task file
    phase_dir = os.path.join(path, "phase_1", "sub")
    os.makedirs(phase_dir, exist_ok=True)
    with open(os.path.join(phase_dir, "01_a.md"), "w") as f:
        f.write("# Task: Docker Test\n")


class TestProcessTaskWithDocker:
    """process_task uses docker when docker_config is provided."""

    def test_process_task_starts_and_stops_container(self, tmp_path):
        """Container is started at task start and stopped in finally."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod
        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo_for_docker(root)

        docker_cfg = DockerConfig(image="test-img:latest")
        docker_exec_calls = []

        def fake_docker_exec(container_name, cmd, **kwargs):
            docker_exec_calls.append(cmd)
            result = MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["git", "status"]:
                result.stdout = "M file.py"
            return result

        with patch("workflow_lib.executor._write_container_env_file", return_value="/tmp/test.env"), \
             patch("workflow_lib.executor._start_task_container") as mock_start, \
             patch("workflow_lib.executor._stop_task_container") as mock_stop, \
             patch("workflow_lib.executor._docker_exec", side_effect=fake_docker_exec), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("workflow_lib.executor.subprocess.run", return_value=MagicMock(returncode=0, stdout="running\n", stderr="")):

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", docker_config=docker_cfg, cleanup=True)

        assert result is True
        # Staged architecture: each stage starts and stops its own container (3 stages total)
        assert mock_start.call_count == 3
        assert mock_stop.call_count == 3

    def test_process_task_clone_failure_returns_false(self, tmp_path):
        """If git clone inside container fails, process_task returns False."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod
        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo_for_docker(root)

        docker_cfg = DockerConfig(image="test-img:latest")
        dashboard = MagicMock()

        def fake_docker_exec(container_name, cmd, **kwargs):
            if "clone" in cmd:
                raise subprocess.CalledProcessError(1, cmd, "", "clone failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("workflow_lib.executor._write_container_env_file", return_value="/tmp/test.env"), \
             patch("workflow_lib.executor._start_task_container"), \
             patch("workflow_lib.executor._stop_task_container") as mock_stop, \
             patch("workflow_lib.executor._docker_exec", side_effect=fake_docker_exec), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("workflow_lib.config.get_config_defaults", return_value={"retries": 0}), \
             patch("workflow_lib.executor.subprocess.run", return_value=MagicMock(returncode=0, stdout="running\n", stderr="")):

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", docker_config=docker_cfg,
                                  dashboard=dashboard, cleanup=True)

        assert result is False
        mock_stop.assert_called_once()  # container always cleaned up in finally

    def test_process_task_docker_push_rejected_force_pushes(self, tmp_path):
        """When git push is rejected in docker mode, force-with-lease is attempted."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod
        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo_for_docker(root)

        docker_cfg = DockerConfig(image="test-img:latest")
        push_count = [0]

        def fake_docker_exec(container_name, cmd, **kwargs):
            result = MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["git", "status"]:
                result.stdout = "M file.py"
            elif cmd[:2] == ["git", "push"] and "--force-with-lease" not in cmd:
                push_count[0] += 1
                result.returncode = 1
                result.stderr = "[rejected] non-fast-forward"
            return result

        with patch("workflow_lib.executor._write_container_env_file", return_value="/tmp/test.env"), \
             patch("workflow_lib.executor._start_task_container"), \
             patch("workflow_lib.executor._stop_task_container"), \
             patch("workflow_lib.executor._docker_exec", side_effect=fake_docker_exec), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("workflow_lib.executor.subprocess.run", return_value=MagicMock(returncode=0, stdout="running\n", stderr="")):

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", docker_config=docker_cfg, cleanup=True)

        assert result is True  # force-with-lease succeeded
        assert push_count[0] == 3  # one initial push rejected per stage (impl, review, validate)

    def test_process_task_docker_presubmit_uses_docker_exec(self, tmp_path):
        """Presubmit is run via _docker_exec (not subprocess.run) when docker is configured."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod
        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo_for_docker(root)

        docker_cfg = DockerConfig(image="test-img:latest")
        docker_exec_cmds = []

        def fake_docker_exec(container_name, cmd, **kwargs):
            docker_exec_cmds.append(cmd)
            result = MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["git", "status"]:
                result.stdout = ""  # no changes
            return result

        with patch("workflow_lib.executor._write_container_env_file", return_value="/tmp/test.env"), \
             patch("workflow_lib.executor._start_task_container"), \
             patch("workflow_lib.executor._stop_task_container"), \
             patch("workflow_lib.executor._docker_exec", side_effect=fake_docker_exec), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("workflow_lib.executor.subprocess.run", return_value=MagicMock(returncode=0, stdout="running\n", stderr="")):

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", docker_config=docker_cfg, cleanup=True)

        assert result is True
        # Presubmit "echo ok" was run via docker exec
        assert ["echo", "ok"] in docker_exec_cmds
        # git add -A was run via docker exec
        assert ["git", "add", "-A"] in docker_exec_cmds


# ---------------------------------------------------------------------------
# _set_cargo_target_dir
# ---------------------------------------------------------------------------

class TestSetCargoTargetDir:
    def test_no_config_file_is_noop(self, tmp_path):
        """No .cargo/config.toml means the function exits early without error."""
        logs = []
        _set_cargo_target_dir(str(tmp_path), "/new/target", logs.append)
        assert logs == []

    def test_updates_target_dir_in_config(self, tmp_path):
        """target-dir line is rewritten when config file is present."""
        cargo_dir = tmp_path / ".cargo"
        cargo_dir.mkdir()
        config = cargo_dir / "config.toml"
        config.write_text('[build]\ntarget-dir = "/old/path"\n')
        logs = []
        _set_cargo_target_dir(str(tmp_path), "/new/target", logs.append)
        assert '"/new/target"' in config.read_text()
        assert logs  # log message emitted

    def test_no_target_dir_line_leaves_file_unchanged(self, tmp_path):
        """File without target-dir is left untouched."""
        cargo_dir = tmp_path / ".cargo"
        cargo_dir.mkdir()
        config = cargo_dir / "config.toml"
        original = "[build]\nincremental = true\n"
        config.write_text(original)
        logs = []
        _set_cargo_target_dir(str(tmp_path), "/new/target", logs.append)
        assert config.read_text() == original
        assert logs == []

    def test_oserror_logs_warning(self, tmp_path):
        """OSError during file read is caught and logged as a warning."""
        cargo_dir = tmp_path / ".cargo"
        cargo_dir.mkdir()
        config = cargo_dir / "config.toml"
        config.write_text('target-dir = "/old"\n')
        logs = []
        with patch("builtins.open", side_effect=OSError("disk full")):
            _set_cargo_target_dir(str(tmp_path), "/new/target", logs.append)
        assert any("Warning" in m or "disk full" in m for m in logs)


# ---------------------------------------------------------------------------
# cmd_docker tests
# ---------------------------------------------------------------------------

class TestCmdDocker:
    """Tests for the docker subcommand."""

    def test_docker_config_not_found_exits(self, capsys):
        """cmd_docker should exit with error if no docker config found."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse

        with patch.object(cli_mod, "get_docker_config", return_value=None):
            args = argparse.Namespace(image=None, validate_sccache=False, cmd=None)

            with pytest.raises(SystemExit) as exc_info:
                cmd_docker(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "no 'docker' configuration found" in captured.err

    def test_git_remote_not_found_exits(self, capsys, tmp_path):
        """cmd_docker should exit with error if git remote URL cannot be retrieved."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse
        from workflow_lib.agent_pool import DockerConfig, DockerCopyFile

        docker_cfg = DockerConfig(image="test:latest")

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=1, stdout="", stderr="error")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "get_docker_config", return_value=docker_cfg), \
             patch.object(cli_mod, "get_dev_branch", return_value="dev"), \
             patch.object(cli_mod, "ROOT_DIR", str(tmp_path)), \
             patch.object(cli_mod.subprocess, "run", side_effect=fake_run):

            args = argparse.Namespace(image=None, validate_sccache=False, cmd=None)

            with pytest.raises(SystemExit) as exc_info:
                cmd_docker(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "could not get URL for remote" in captured.err

    def test_uses_shared_container_startup(self, tmp_path):
        """cmd_docker should delegate container startup to the shared helper."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse
        from workflow_lib.agent_pool import DockerConfig
        from workflow_lib.config import SCCacheConfig

        docker_cfg = DockerConfig(image="test:latest", volumes=["/tmp:/tmp"])
        sccache_cfg = SCCacheConfig(enabled=True)

        docker_exec_results = [
            MagicMock(returncode=0, stdout="/usr/local/cargo/bin/sccache\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout="https://github.com/test/repo.git", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "get_docker_config", return_value=docker_cfg), \
             patch.object(cli_mod, "get_dev_branch", return_value="dev"), \
             patch.object(cli_mod, "get_sccache_config", return_value=sccache_cfg), \
             patch.object(cli_mod, "get_sccache_dist_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_services_config", return_value=MagicMock(configure_containers=True)), \
             patch.object(cli_mod, "ensure_sccache_services", return_value=(True, True)) as mock_ensure, \
             patch.object(cli_mod, "ROOT_DIR", str(tmp_path)), \
             patch.object(cli_mod, "_write_container_env_file", return_value="/tmp/container.env"), \
             patch.object(cli_mod, "_start_task_container") as mock_start, \
             patch.object(cli_mod, "_stop_task_container") as mock_stop, \
             patch.object(cli_mod, "_docker_exec", side_effect=docker_exec_results), \
             patch.object(cli_mod.subprocess, "run", side_effect=fake_run):

            args = argparse.Namespace(image=None, validate_sccache=False, cmd=None)
            cmd_docker(args)

        mock_start.assert_called_once()
        mock_ensure.assert_called_once()
        _, kwargs = mock_start.call_args
        assert kwargs["sccache_config"] == sccache_cfg
        assert kwargs["configure_containers"] is True
        assert mock_start.call_args.args[1].image == "test:latest"
        assert mock_start.call_args.args[1].volumes == ["/tmp:/tmp"]
        mock_stop.assert_called_once()

    def test_image_override_from_args(self, tmp_path):
        """cmd_docker should use --image argument if provided."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse
        from workflow_lib.agent_pool import DockerConfig

        docker_cfg = DockerConfig(image="base:latest")

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout="https://github.com/test/repo.git", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "get_docker_config", return_value=docker_cfg), \
             patch.object(cli_mod, "get_dev_branch", return_value="dev"), \
             patch.object(cli_mod, "ROOT_DIR", str(tmp_path)), \
             patch.object(cli_mod, "get_sccache_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_dist_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_services_config", return_value=None), \
             patch.object(cli_mod, "_write_container_env_file", return_value="/tmp/container.env"), \
             patch.object(cli_mod, "_start_task_container") as mock_start, \
             patch.object(cli_mod, "_stop_task_container"), \
             patch.object(cli_mod, "_docker_exec", side_effect=[
                 MagicMock(returncode=0, stdout="/usr/local/cargo/bin/sccache\n", stderr=""),
                 MagicMock(returncode=0, stdout="", stderr=""),
             ]), \
             patch.object(cli_mod.subprocess, "run", side_effect=fake_run), \
             patch.object(cli_mod.os, "getpid", return_value=1234):

            args = argparse.Namespace(image="override:custom", validate_sccache=False, cmd=None)
            cmd_docker(args)

        assert mock_start.call_args.args[1].image == "override:custom"

    def test_uses_pivot_remote_from_config(self, tmp_path):
        """cmd_docker should use pivot_remote from docker config."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse
        from workflow_lib.agent_pool import DockerConfig

        docker_cfg = DockerConfig(image="test:latest", pivot_remote="upstream")

        remote_queries = []
        def fake_run(cmd, **kwargs):
            remote_queries.append(cmd)
            if cmd[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout="https://github.com/test/repo.git", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "get_docker_config", return_value=docker_cfg), \
             patch.object(cli_mod, "get_dev_branch", return_value="dev"), \
             patch.object(cli_mod, "ROOT_DIR", str(tmp_path)), \
             patch.object(cli_mod, "get_sccache_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_dist_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_services_config", return_value=None), \
             patch.object(cli_mod, "_write_container_env_file", return_value="/tmp/container.env"), \
             patch.object(cli_mod, "_start_task_container"), \
             patch.object(cli_mod, "_stop_task_container"), \
             patch.object(cli_mod, "_docker_exec", side_effect=[
                 MagicMock(returncode=0, stdout="/usr/local/cargo/bin/sccache\n", stderr=""),
                 MagicMock(returncode=0, stdout="", stderr=""),
             ]), \
             patch.object(cli_mod.subprocess, "run", side_effect=fake_run), \
             patch.object(cli_mod.os, "getpid", return_value=1234):

            args = argparse.Namespace(image=None, validate_sccache=False, cmd=None)
            cmd_docker(args)

        # Verify git remote query used configured pivot_remote
        assert any("upstream" in cmd for cmd in remote_queries), \
            "Should query git remote using configured pivot_remote"

    def test_verifies_sccache_routing_in_container(self, tmp_path):
        """cmd_docker should verify sccache availability and routing after startup."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse
        from workflow_lib.agent_pool import DockerConfig
        from workflow_lib.config import SCCacheConfig

        docker_cfg = DockerConfig(image="test:latest")
        sccache_cfg = SCCacheConfig(enabled=True)

        docker_exec_calls = []
        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout="https://github.com/test/repo.git", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        def fake_docker_exec(*args, **kwargs):
            docker_exec_calls.append((args, kwargs))
            cmd = args[1]
            if cmd == ["bash", "-lc", "command -v sccache"]:
                return MagicMock(returncode=0, stdout="/usr/local/cargo/bin/sccache\n", stderr="")
            if isinstance(cmd, list) and len(cmd) == 3 and "sccache --show-stats" in cmd[2]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "get_docker_config", return_value=docker_cfg), \
             patch.object(cli_mod, "get_dev_branch", return_value="dev"), \
             patch.object(cli_mod, "ROOT_DIR", str(tmp_path)), \
             patch.object(cli_mod, "get_sccache_config", return_value=sccache_cfg), \
             patch.object(cli_mod, "get_sccache_dist_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_services_config", return_value=MagicMock(configure_containers=True)), \
             patch.object(cli_mod, "ensure_sccache_services", return_value=(True, True)), \
             patch.object(cli_mod, "_write_container_env_file", return_value="/tmp/container.env"), \
             patch.object(cli_mod, "_start_task_container"), \
             patch.object(cli_mod, "_stop_task_container"), \
             patch.object(cli_mod, "_docker_exec", side_effect=fake_docker_exec), \
             patch.object(cli_mod.subprocess, "run", side_effect=fake_run), \
             patch.object(cli_mod.os, "getpid", return_value=1234):

            args = argparse.Namespace(image=None, validate_sccache=False, cmd=None)
            cmd_docker(args)

        route_checks = [
            args[1] for args, _kwargs in docker_exec_calls
            if args[1][:2] == ["bash", "-lc"] and "SCCACHE_REDIS" in args[1][2]
        ]
        assert route_checks
        assert 'test "$RUSTC_WRAPPER" = "sccache"' in route_checks[0][2]
        assert 'SCCACHE_REDIS' in route_checks[0][2]
        assert any(isinstance(args[1], list) and len(args[1]) == 3 and "sccache --show-stats" in args[1][2] for args, _kwargs in docker_exec_calls)

    def test_volumes_from_config_added(self, tmp_path):
        """cmd_docker should pass configured volumes into the shared startup config."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse
        from workflow_lib.agent_pool import DockerConfig

        docker_cfg = DockerConfig(
            image="test:latest",
            volumes=["/host/data:/container/data:ro", "/tmp:/tmp"]
        )

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout="https://github.com/test/repo.git", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "get_docker_config", return_value=docker_cfg), \
             patch.object(cli_mod, "get_dev_branch", return_value="dev"), \
             patch.object(cli_mod, "ROOT_DIR", str(tmp_path)), \
             patch.object(cli_mod, "get_sccache_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_dist_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_services_config", return_value=None), \
             patch.object(cli_mod, "_write_container_env_file", return_value="/tmp/container.env"), \
             patch.object(cli_mod, "_start_task_container") as mock_start, \
             patch.object(cli_mod, "_stop_task_container"), \
             patch.object(cli_mod, "_docker_exec", side_effect=[
                 MagicMock(returncode=0, stdout="/usr/local/cargo/bin/sccache\n", stderr=""),
                 MagicMock(returncode=0, stdout="", stderr=""),
             ]), \
             patch.object(cli_mod.subprocess, "run", side_effect=fake_run), \
             patch.object(cli_mod.os, "getpid", return_value=1234):

            args = argparse.Namespace(image=None, validate_sccache=False, cmd=None)
            cmd_docker(args)

        effective_cfg = mock_start.call_args.args[1]
        assert effective_cfg.volumes == ["/host/data:/container/data:ro", "/tmp:/tmp"]

    def test_clones_dev_branch(self, tmp_path):
        """cmd_docker should clone and checkout the configured dev branch."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse
        from workflow_lib.agent_pool import DockerConfig

        docker_cfg = DockerConfig(image="test:latest")

        docker_exec_calls = []
        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout="https://github.com/test/repo.git", stderr="")
            if len(cmd) >= 2 and cmd[0] == "docker" and cmd[1] == "exec":
                docker_exec_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "get_docker_config", return_value=docker_cfg), \
             patch.object(cli_mod, "get_dev_branch", return_value="feature-branch"), \
             patch.object(cli_mod, "ROOT_DIR", str(tmp_path)), \
             patch.object(cli_mod, "get_sccache_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_dist_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_services_config", return_value=None), \
             patch.object(cli_mod, "_write_container_env_file", return_value="/tmp/container.env"), \
             patch.object(cli_mod, "_start_task_container"), \
             patch.object(cli_mod, "_stop_task_container"), \
             patch.object(cli_mod, "_docker_exec", side_effect=[
                 MagicMock(returncode=0, stdout="/usr/local/cargo/bin/sccache\n", stderr=""),
                 MagicMock(returncode=0, stdout="", stderr=""),
             ]), \
             patch.object(cli_mod.os, "getpid", return_value=1234), \
             patch.object(cli_mod.subprocess, "run", side_effect=fake_run):

            args = argparse.Namespace(image=None, validate_sccache=False, cmd=None)
            cmd_docker(args)

        # Verify docker exec was called with the clone command
        assert docker_exec_calls, "docker exec should be called"
        # Find the exec call with git clone
        clone_cmd = None
        for call in docker_exec_calls:
            call_str = " ".join(call)
            if "git clone" in call_str:
                clone_cmd = call_str
                break
        
        assert clone_cmd is not None, "Should have git clone in docker exec command"
        assert "git clone --branch feature-branch" in clone_cmd
        assert "git submodule update --init --recursive" in clone_cmd

    def test_missing_copy_file_matches_shared_startup_failure(self, tmp_path):
        """cmd_docker should surface shared startup copy-file failures."""
        from workflow_lib.cli import cmd_docker
        import workflow_lib.cli as cli_mod
        import argparse
        from workflow_lib.agent_pool import DockerConfig

        docker_cfg = DockerConfig(image="test:latest")

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout="https://github.com/test/repo.git", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "get_docker_config", return_value=docker_cfg), \
             patch.object(cli_mod, "get_dev_branch", return_value="dev"), \
             patch.object(cli_mod, "ROOT_DIR", str(tmp_path)), \
             patch.object(cli_mod, "get_sccache_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_dist_config", return_value=None), \
             patch.object(cli_mod, "get_sccache_services_config", return_value=None), \
             patch.object(cli_mod, "_write_container_env_file", return_value="/tmp/container.env"), \
             patch.object(cli_mod, "_start_task_container", side_effect=FileNotFoundError("missing copy file")), \
             patch.object(cli_mod.subprocess, "run", side_effect=fake_run), \
             patch.object(cli_mod.os, "getpid", return_value=1234):

            args = argparse.Namespace(image=None, validate_sccache=False, cmd=None)
            with pytest.raises(FileNotFoundError, match="missing copy file"):
                cmd_docker(args)
