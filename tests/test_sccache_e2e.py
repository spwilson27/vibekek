"""E2E tests for sccache server connectivity from Docker containers.

These tests verify that:
1. Containers are started with --add-host host.docker.internal:host-gateway
2. RUSTC_WRAPPER=sccache env var is set in container
3. SCCACHE_SERVER=host.docker.internal:6301 env var is set in container
4. The sccache server is reachable from inside the container

Tests follow the mocking patterns from test_docker.py and use the
_host_protection fixture from conftest.py to prevent real network calls.
"""

import json
import os
import sys
import socket
import subprocess
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.config import SCCacheConfig, SCCacheDistConfig, get_sccache_config, get_sccache_enabled, get_sccache_dist_config, get_sccache_dist_enabled
from workflow_lib.executor import _start_task_container, _write_container_env_file
from workflow_lib.agent_pool import DockerConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sccache_config_enabled():
    """Return an enabled SCCacheConfig for testing."""
    return SCCacheConfig(
        enabled=True,
        host="host.docker.internal",
        port=6301,
        cache_dir="/tmp/test-sccache",
    )


@pytest.fixture
def sccache_config_disabled():
    """Return a disabled SCCacheConfig for testing."""
    return SCCacheConfig(
        enabled=False,
        host="host.docker.internal",
        port=6301,
        cache_dir="/tmp/test-sccache",
    )


@pytest.fixture
def docker_config():
    """Return a basic DockerConfig for testing."""
    return DockerConfig(
        image="test-image:latest",
        volumes=[],
        copy_files=[],
    )


@pytest.fixture
def tmp_env_file(tmp_path):
    """Create a temporary env file for container testing."""
    env_file = tmp_path / "container.env"
    env_file.write_text("TEST_VAR=test_value\n")
    return str(env_file)


# ---------------------------------------------------------------------------
# SCCache config loader tests
# ---------------------------------------------------------------------------

class TestSCCacheConfigLoader:
    """Tests for get_sccache_config() and get_sccache_enabled()."""

    def test_get_sccache_config_from_workflow_jsonc(self, tmp_path):
        """get_sccache_config should parse .workflow.jsonc sccache section."""
        from workflow_lib import config
        
        # Create a test config file
        config_file = tmp_path / ".workflow.jsonc"
        config_file.write_text("""
        {
            "sccache": {
                "enabled": true,
                "host": "host.docker.internal",
                "port": 6301,
                "cache_dir": "/custom/cache/dir"
            }
        }
        """)
        
        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            cfg = get_sccache_config()
            assert cfg is not None
            assert cfg.enabled is True
            assert cfg.host == "host.docker.internal"
            assert cfg.port == 6301
            assert cfg.cache_dir == "/custom/cache/dir"

    def test_get_sccache_config_missing_section(self, tmp_path):
        """get_sccache_config should return None when sccache section absent."""
        from workflow_lib import config
        
        config_file = tmp_path / ".workflow.jsonc"
        config_file.write_text('{"backend": "gemini"}')
        
        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            cfg = get_sccache_config()
            assert cfg is None

    def test_get_sccache_enabled_true(self, tmp_path):
        """get_sccache_enabled should return True when enabled."""
        from workflow_lib import config
        
        config_file = tmp_path / ".workflow.jsonc"
        config_file.write_text("""
        {
            "sccache": {
                "enabled": true
            }
        }
        """)
        
        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            assert get_sccache_enabled() is True

    def test_get_sccache_enabled_false(self, tmp_path):
        """get_sccache_enabled should return False when disabled or missing."""
        from workflow_lib import config
        
        # Test disabled
        config_file = tmp_path / ".workflow.jsonc"
        config_file.write_text("""
        {
            "sccache": {
                "enabled": false
            }
        }
        """)
        
        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            assert get_sccache_enabled() is False
        
        # Test missing
        config_file.write_text('{"backend": "gemini"}')
        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            assert get_sccache_enabled() is False


# ---------------------------------------------------------------------------
# Container sccache configuration tests
# ---------------------------------------------------------------------------

class TestContainerSCCacheConfig:
    """Tests for sccache configuration in Docker containers."""

    def test_start_container_adds_host_flag_when_enabled(self, docker_config, tmp_env_file, sccache_config_enabled):
        """_start_task_container should add --add-host when sccache enabled."""
        from workflow_lib import executor
        
        run_calls = []
        def mock_run(cmd, **kwargs):
            run_calls.append((cmd, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")
        
        log_calls = []
        def mock_log(msg):
            log_calls.append(msg)
        
        with patch.object(subprocess, "run", side_effect=mock_run):
            _start_task_container(
                container_name="test-container",
                docker_config=docker_config,
                env_file=tmp_env_file,
                log=mock_log,
                sccache_config=sccache_config_enabled,
            )
        
        # Verify docker run was called
        assert len(run_calls) > 0
        docker_cmd = run_calls[0][0]
        
        # Verify --add-host flag was added
        assert "--add-host" in docker_cmd
        host_idx = docker_cmd.index("--add-host")
        assert docker_cmd[host_idx + 1] == "host.docker.internal:host-gateway"
        
        # Verify env vars were added
        assert "-e" in docker_cmd
        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])
        
        assert any("RUSTC_WRAPPER=sccache" in pair for pair in env_pairs)
        assert any("SCCACHE_SERVER=host.docker.internal:6301" in pair for pair in env_pairs)
        
        # Verify log message
        assert any("sccache" in msg.lower() for msg in log_calls)

    def test_start_container_skips_sccache_when_disabled(self, docker_config, tmp_env_file, sccache_config_disabled):
        """_start_task_container should not add sccache flags when disabled."""
        from workflow_lib import executor
        
        run_calls = []
        def mock_run(cmd, **kwargs):
            run_calls.append((cmd, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")
        
        with patch.object(subprocess, "run", side_effect=mock_run):
            _start_task_container(
                container_name="test-container",
                docker_config=docker_config,
                env_file=tmp_env_file,
                log=lambda msg: None,
                sccache_config=sccache_config_disabled,
            )
        
        docker_cmd = run_calls[0][0]
        
        # Verify --add-host was NOT added for sccache
        # (it might exist for other reasons, but not for sccache)
        if "--add-host" in docker_cmd:
            host_idx = docker_cmd.index("--add-host")
            # Should not have host.docker.internal:host-gateway from sccache
            assert docker_cmd[host_idx + 1] != "host.docker.internal:host-gateway"
        
        # Verify no sccache env vars
        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])
        
        assert not any("RUSTC_WRAPPER" in pair for pair in env_pairs)
        assert not any("SCCACHE_SERVER" in pair for pair in env_pairs)

    def test_start_container_with_none_sccache_config(self, docker_config, tmp_env_file):
        """_start_task_container should handle None sccache_config."""
        from workflow_lib import executor
        
        run_calls = []
        def mock_run(cmd, **kwargs):
            run_calls.append((cmd, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")
        
        with patch.object(subprocess, "run", side_effect=mock_run):
            _start_task_container(
                container_name="test-container",
                docker_config=docker_config,
                env_file=tmp_env_file,
                log=lambda msg: None,
                sccache_config=None,
            )
        
        docker_cmd = run_calls[0][0]
        
        # Should not have sccache-related flags
        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])
        
        assert not any("RUSTC_WRAPPER" in pair for pair in env_pairs)
        assert not any("SCCACHE_SERVER" in pair for pair in env_pairs)


# ---------------------------------------------------------------------------
# Integration tests (slow, skip-by-default)
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.skip(reason="Requires real docker and sccache server")
class TestSCCacheIntegration:
    """Integration tests requiring real docker and sccache server."""

    def test_container_can_reach_sccache_server(self, sccache_config_enabled):
        """Verify TCP connectivity from container to host sccache port.
        
        This test:
        1. Starts a real sccache server on the host
        2. Launches a test container with sccache env vars
        3. Attempts TCP connection from container to host sccache port
        4. Verifies connection succeeds
        """
        # Start sccache server
        result = subprocess.run(
            [".tools/start-sccache.sh", "start"],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Failed to start sccache: {result.stderr}"
        
        try:
            # Verify server is listening
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex(("127.0.0.1", 6301))
            sock.close()
            assert result == 0, "sccache server not listening on port 6301"
            
            # Launch test container
            container_name = "test-sccache-connect"
            docker_cmd = [
                "docker", "run", "-d", "--rm",
                "--name", container_name,
                "--add-host", "host.docker.internal:host-gateway",
                "-e", "RUSTC_WRAPPER=sccache",
                "-e", f"SCCACHE_SERVER=host.docker.internal:6301",
                "ubuntu:24.04", "sleep", "infinity"
            ]
            subprocess.run(docker_cmd, check=True, capture_output=True)
            
            try:
                # Test connectivity from inside container
                exec_cmd = [
                    "docker", "exec", container_name,
                    "bash", "-c",
                    "echo 'GET / HTTP/1.0\\r\\n\\r\\n' | timeout 2 nc host.docker.internal 6301 || exit 0"
                ]
                result = subprocess.run(exec_cmd, capture_output=True, text=True)
                
                # Connection test - just verify we can reach the port
                test_cmd = [
                    "docker", "exec", container_name,
                    "bash", "-c",
                    f"echo 'Testing connection to {sccache_config_enabled.host}:{sccache_config_enabled.port}' && (echo > /dev/tcp/host.docker.internal/6301) 2>/dev/null && echo 'SUCCESS' || echo 'FAILED'"
                ]
                result = subprocess.run(test_cmd, capture_output=True, text=True)
                assert "SUCCESS" in result.stdout, f"Connection test failed: {result.stdout}"
                
            finally:
                # Cleanup container
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
                
        finally:
            # Stop sccache server
            subprocess.run([".tools/start-sccache.sh", "stop"], capture_output=True)

    def test_sccache_env_vars_in_container(self, sccache_config_enabled):
        """Verify sccache env vars are correctly set inside container."""
        container_name = "test-sccache-env"
        
        # Start container
        docker_cmd = [
            "docker", "run", "-d", "--rm",
            "--name", container_name,
            "--add-host", "host.docker.internal:host-gateway",
            "-e", "RUSTC_WRAPPER=sccache",
            "-e", f"SCCACHE_SERVER=host.docker.internal:6301",
            "ubuntu:24.04", "sleep", "infinity"
        ]
        subprocess.run(docker_cmd, check=True, capture_output=True)
        
        try:
            # Check env vars
            result = subprocess.run(
                ["docker", "exec", container_name, "env"],
                capture_output=True, text=True, check=True
            )
            
            assert "RUSTC_WRAPPER=sccache" in result.stdout
            assert "SCCACHE_SERVER=host.docker.internal:6301" in result.stdout

        finally:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


# ---------------------------------------------------------------------------
# SCCacheDist config loader tests
# ---------------------------------------------------------------------------

class TestSCCacheDistConfigLoader:
    """Tests for get_sccache_dist_config() and get_sccache_dist_enabled()."""

    def test_get_sccache_dist_config_from_workflow_jsonc(self, tmp_path):
        """get_sccache_dist_config should parse .workflow.jsonc sccache_dist section."""
        from workflow_lib import config

        # Create a test config file (use simple URL without :// to avoid comment stripping)
        config_file = tmp_path / ".workflow.jsonc"
        config_file.write_text('{"sccache_dist":{"enabled":true,"scheduler_url":"h.d.i:10600","auth_token":"test-token-123","config_file":"/custom/sccache-dist.toml"}}')

        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            cfg = get_sccache_dist_config()
            assert cfg is not None
            assert cfg.enabled is True
            assert cfg.scheduler_url == "h.d.i:10600"
            assert cfg.auth_token == "test-token-123"
            assert cfg.config_file == "/custom/sccache-dist.toml"

    def test_get_sccache_dist_config_missing_section(self, tmp_path):
        """get_sccache_dist_config should return None when section absent."""
        from workflow_lib import config

        config_file = tmp_path / ".workflow.jsonc"
        config_file.write_text("{}")

        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            cfg = get_sccache_dist_config()
            assert cfg is None

    def test_get_sccache_dist_enabled_true(self, tmp_path):
        """get_sccache_dist_enabled should return True when enabled."""
        from workflow_lib import config

        config_file = tmp_path / ".workflow.jsonc"
        config_file.write_text('{"sccache_dist": {"enabled": true}}')

        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            assert get_sccache_dist_enabled() is True

    def test_get_sccache_dist_enabled_false(self, tmp_path):
        """get_sccache_dist_enabled should return False when disabled or absent."""
        from workflow_lib import config

        config_file = tmp_path / ".workflow.jsonc"
        config_file.write_text("{}")

        with patch.object(config, "_CONFIG_FILE_ROOT", str(config_file)):
            assert get_sccache_dist_enabled() is False


# ---------------------------------------------------------------------------
# Container sccache-dist config tests
# ---------------------------------------------------------------------------

class TestContainerSCCacheDistConfig:
    """Tests for sccache-dist configuration in containers."""

    @patch("workflow_lib.executor.subprocess.run")
    def test_start_container_adds_sccache_dist_env_vars(self, mock_run, sccache_config_enabled, tmp_env_file, docker_config):
        """Container should have SCCACHE_DIST_SCHEDULER_URL and SCCACHE_AUTH_TOKEN env vars."""
        from workflow_lib.config import SCCacheDistConfig

        sccache_dist_config = SCCacheDistConfig(
            enabled=True,
            scheduler_url="http://host.docker.internal:10600",
            auth_token="test-dist-token",
            config_file="/tmp/sccache-dist.toml",
        )

        mock_run.return_value = MagicMock(returncode=0, stdout="container123")

        _start_task_container(
            "test-container",
            docker_config,
            tmp_env_file,
            lambda msg: None,
            sccache_config=None,  # No local sccache
            sccache_dist_config=sccache_dist_config,
        )

        docker_cmd = mock_run.call_args[0][0]

        # Verify env vars are present
        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])

        assert any("RUSTC_WRAPPER=sccache" in pair for pair in env_pairs)
        assert any("SCCACHE_DIST_SCHEDULER_URL=http://host.docker.internal:10600" in pair for pair in env_pairs)
        assert any("SCCACHE_AUTH_TOKEN=test-dist-token" in pair for pair in env_pairs)

    @patch("workflow_lib.executor.subprocess.run")
    def test_start_container_with_both_sccache_and_dist(self, mock_run, sccache_config_enabled, tmp_env_file, docker_config):
        """When both sccache and sccache-dist are enabled, dist adds its vars."""
        from workflow_lib.config import SCCacheDistConfig

        sccache_dist_config = SCCacheDistConfig(
            enabled=True,
            scheduler_url="http://host.docker.internal:10600",
            auth_token="dist-token",
            config_file="/tmp/sccache-dist.toml",
        )

        mock_run.return_value = MagicMock(returncode=0, stdout="container123")

        _start_task_container(
            "test-container",
            docker_config,
            tmp_env_file,
            lambda msg: None,
            sccache_config=sccache_config_enabled,
            sccache_dist_config=sccache_dist_config,
        )

        docker_cmd = mock_run.call_args[0][0]

        # Verify both configs add their env vars
        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])

        # Local sccache vars
        assert any("SCCACHE_SERVER=host.docker.internal:6301" in pair for pair in env_pairs)
        # Dist scheduler vars
        assert any("SCCACHE_DIST_SCHEDULER_URL=http://host.docker.internal:10600" in pair for pair in env_pairs)
        assert any("SCCACHE_AUTH_TOKEN=dist-token" in pair for pair in env_pairs)

    @patch("workflow_lib.executor.subprocess.run")
    def test_start_container_skips_sccache_dist_when_disabled(self, mock_run, tmp_env_file, docker_config):
        """Container should not have sccache-dist env vars when disabled."""
        from workflow_lib.config import SCCacheDistConfig

        sccache_dist_config = SCCacheDistConfig(
            enabled=False,
            scheduler_url="http://host.docker.internal:10600",
            auth_token="test-token",
            config_file="/tmp/sccache-dist.toml",
        )

        mock_run.return_value = MagicMock(returncode=0, stdout="container123")

        _start_task_container(
            "test-container",
            docker_config,
            tmp_env_file,
            lambda msg: None,
            sccache_config=None,
            sccache_dist_config=sccache_dist_config,
        )

        docker_cmd = mock_run.call_args[0][0]

        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])

        assert not any("SCCACHE_DIST_SCHEDULER_URL" in pair for pair in env_pairs)
        assert not any("SCCACHE_AUTH_TOKEN" in pair for pair in env_pairs)
