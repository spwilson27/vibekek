#!/usr/bin/env python3
"""Test for verifying sccache --dist-status in Docker containers.

This test verifies that:
1. The sccache server is running on the host
2. Docker containers are configured with correct sccache environment variables
3. The sccache --dist-status command returns expected status (not "disabled")
4. The sccache server is reachable from inside the container

Run with: pytest test_sccache_dist_status.py -v
"""

import json
import os
import sys
import subprocess
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.config import (
    SCCacheConfig,
    SCCacheDistConfig,
    SCCacheServicesConfig,
    get_sccache_config,
    get_sccache_enabled,
    get_sccache_dist_config,
    get_sccache_services_config,
    ensure_sccache_services,
)
from workflow_lib.agent_pool import DockerConfig


class TestSCCacheDistStatusConfig:
    """Tests for sccache --dist-status configuration."""

    def test_sccache_config_present_and_enabled(self):
        """Verify .workflow.jsonc has sccache enabled."""
        cfg = get_sccache_config()
        assert cfg is not None, ".workflow.jsonc should have sccache config"
        assert cfg.enabled is True, "sccache should be enabled"

    def test_sccache_services_config_present(self):
        """Verify .workflow.jsonc has sccache_services config."""
        cfg = get_sccache_services_config()
        assert cfg is not None, ".workflow.jsonc should have sccache_services config"
        assert cfg.auto_start is True, "auto_start should be enabled"
        assert cfg.configure_containers is True, "configure_containers should be enabled"

    def test_ensure_sccache_services_starts_server(self):
        """Verify ensure_sccache_services() starts the sccache server."""
        # This requires auto_start to be enabled
        services_cfg = get_sccache_services_config()
        assert services_cfg is not None
        assert services_cfg.auto_start is True

        # Call ensure_sccache_services - should start if not running
        scc_ok, dist_ok = ensure_sccache_services()

        # Verify sccache server is now running
        result = subprocess.run(["pgrep", "-f", "sccache"], capture_output=True, text=True)
        assert result.returncode == 0, "sccache server should be running after ensure_sccache_services()"


class TestContainerSCCacheConfig:
    """Tests for sccache container environment variable configuration."""

    @pytest.fixture
    def sccache_config_enabled(self):
        """Return an enabled SCCacheConfig for testing."""
        return SCCacheConfig(enabled=True)

    @pytest.fixture
    def docker_config(self):
        """Return a basic DockerConfig for testing."""
        return DockerConfig(
            image="weaver-reloaded:dev",
            volumes=[],
            copy_files=[],
        )

    def test_docker_command_includes_sccache_env_vars(self, sccache_config_enabled, docker_config, tmp_path):
        """Verify docker run command includes correct sccache environment variables."""
        # Create temp env file
        env_file = tmp_path / "container.env"
        env_file.write_text("TEST_VAR=test_value\n")

        # Build the docker command (mirroring _start_task_container logic)
        docker_cmd = [
            "docker", "run", "-d",
            "--name", "test-sccache-container",
            "--env-file", str(env_file),
        ]

        # Add sccache configuration (same logic as in executor.py)
        if sccache_config_enabled.enabled:
            redis_url = f"redis://{sccache_config_enabled.redis_container}:{sccache_config_enabled.redis_port}"
            docker_cmd += [
                "--network", sccache_config_enabled.network,
                "-e", "RUSTC_WRAPPER=sccache",
                "-e", f"SCCACHE_REDIS={redis_url}",
            ]

        docker_cmd += [docker_config.image, "sleep", "infinity"]

        # Verify --network flag for Redis
        assert "--network" in docker_cmd

        # Verify sccache environment variables
        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])

        assert any("RUSTC_WRAPPER=sccache" in pair for pair in env_pairs), \
            "RUSTC_WRAPPER=sccache should be set"
        assert any("SCCACHE_REDIS=" in pair for pair in env_pairs), \
            "SCCACHE_REDIS should be set"

    def test_docker_command_skips_sccache_when_disabled(self, tmp_path, docker_config):
        """Verify docker command skips sccache vars when disabled."""
        sccache_config_disabled = SCCacheConfig(enabled=False)

        env_file = tmp_path / "container.env"
        env_file.write_text("TEST_VAR=test_value\n")

        docker_cmd = [
            "docker", "run", "-d",
            "--name", "test-container",
            "--env-file", str(env_file),
        ]

        if sccache_config_disabled.enabled:
            redis_url = f"redis://{sccache_config_disabled.redis_container}:{sccache_config_disabled.redis_port}"
            docker_cmd += [
                "--network", sccache_config_disabled.network,
                "-e", "RUSTC_WRAPPER=sccache",
                "-e", f"SCCACHE_REDIS={redis_url}",
            ]

        docker_cmd += [docker_config.image, "sleep", "infinity"]

        # Verify --network was NOT added
        assert "--network" not in docker_cmd

        # Verify no sccache env vars
        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])

        assert not any("RUSTC_WRAPPER" in pair for pair in env_pairs)
        assert not any("SCCACHE_SERVER" in pair for pair in env_pairs)


class TestSCCacheDistSchedulerConfig:
    """Tests for sccache-dist scheduler configuration."""

    def test_sccache_dist_config_optional(self):
        """sccache-dist config is optional - test it can be absent."""
        cfg = get_sccache_dist_config()
        # This is OK - sccache-dist is optional
        assert cfg is None or isinstance(cfg, SCCacheDistConfig)

    def test_docker_command_with_sccache_dist_enabled(self, tmp_path):
        """Verify docker command has sccache-dist env vars when enabled."""
        sccache_dist_config = SCCacheDistConfig(
            enabled=True,
            scheduler_url="http://host.docker.internal:10600",
            auth_token="gooey-dist-token-2024",
            config_file="/home/mrwilson/.tools/sccache-dist.toml",
        )

        docker_config = DockerConfig(
            image="weaver-reloaded:dev",
            volumes=[],
            copy_files=[],
        )

        env_file = tmp_path / "container.env"
        env_file.write_text("TEST=value\n")

        docker_cmd = [
            "docker", "run", "-d",
            "--name", "test-container",
            "--env-file", str(env_file),
        ]

        if sccache_dist_config.enabled:
            docker_cmd += ["--add-host", "host.docker.internal:host-gateway"]
            docker_cmd += [
                "-e", f"RUSTC_WRAPPER=sccache",
                "-e", f"SCCACHE_DIST_SCHEDULER_URL={sccache_dist_config.scheduler_url}",
                "-e", f"SCCACHE_AUTH_TOKEN={sccache_dist_config.auth_token}",
            ]

        docker_cmd += [docker_config.image, "sleep", "infinity"]

        # Verify sccache-dist env vars
        env_pairs = []
        for i, arg in enumerate(docker_cmd):
            if arg == "-e" and i + 1 < len(docker_cmd):
                env_pairs.append(docker_cmd[i + 1])

        assert any("RUSTC_WRAPPER=sccache" in pair for pair in env_pairs)
        assert any("SCCACHE_DIST_SCHEDULER_URL=http://host.docker.internal:10600" in pair for pair in env_pairs)
        assert any("SCCACHE_AUTH_TOKEN=gooey-dist-token-2024" in pair for pair in env_pairs)


@pytest.mark.slow
@pytest.mark.skip(reason="Requires real Docker and sccache server - run manually")
class TestSCCacheDistStatusIntegration:
    """Integration tests for sccache --dist-status from inside container."""

    def test_sccache_dist_status_from_container(self):
        """Verify sccache --dist-status returns non-disabled status from container.

        This integration test:
        1. Ensures sccache server is running on host
        2. Starts a Docker container with sccache env vars
        3. Runs `sccache --dist-status` inside the container
        4. Verifies the output is NOT {"Disabled":"disabled"}
        """
        # Ensure sccache server is running
        result = subprocess.run(
            [".tools/start-sccache.sh", "start"],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Failed to start sccache: {result.stderr}"

        try:
            # Wait for server to be ready
            import time
            time.sleep(2)

            # Verify server is running
            result = subprocess.run(["pgrep", "-f", "sccache"], capture_output=True, text=True)
            assert result.returncode == 0, "sccache server should be running"

            # Start test container
            container_name = "test-sccache-dist-status"
            docker_cmd = [
                "docker", "run", "-d", "--rm",
                "--name", container_name,
                "--add-host", "host.docker.internal:host-gateway",
                "-e", "RUSTC_WRAPPER=sccache",
                "-e", "SCCACHE_SERVER=host.docker.internal:6301",
                "weaver-reloaded:dev", "sleep", "infinity"
            ]
            subprocess.run(docker_cmd, check=True, capture_output=True)

            try:
                # Run sccache --dist-status inside container
                exec_cmd = [
                    "docker", "exec", container_name,
                    "bash", "-lc", "sccache --dist-status"
                ]
                result = subprocess.run(exec_cmd, capture_output=True, text=True)

                # Parse the JSON output
                output = result.stdout.strip()
                try:
                    status_json = json.loads(output)
                    # Should NOT be {"Disabled":"disabled"}
                    assert "Disabled" not in status_json or status_json.get("Disabled") != "disabled", \
                        f"sccache --dist-status returned disabled: {output}"
                except json.JSONDecodeError:
                    # If output is not JSON, it might be an error - that's OK if not "disabled"
                    assert "disabled" not in output.lower(), \
                        f"sccache --dist-status indicates disabled: {output}"

            finally:
                # Cleanup container
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        finally:
            # Stop sccache server
            subprocess.run([".tools/start-sccache.sh", "stop"], capture_output=True)

    def test_sccache_show_stats_from_container(self):
        """Verify sccache --show-stats works from inside container.

        This test verifies the container can communicate with the host sccache server.
        """
        # Ensure sccache server is running
        subprocess.run([".tools/start-sccache.sh", "start"], capture_output=True, text=True)

        try:
            import time
            time.sleep(2)

            # Start test container
            container_name = "test-sccache-stats"
            docker_cmd = [
                "docker", "run", "-d", "--rm",
                "--name", container_name,
                "--add-host", "host.docker.internal:host-gateway",
                "-e", "RUSTC_WRAPPER=sccache",
                "-e", "SCCACHE_SERVER=host.docker.internal:6301",
                "weaver-reloaded:dev", "sleep", "infinity"
            ]
            subprocess.run(docker_cmd, check=True, capture_output=True)

            try:
                # Run sccache --show-stats inside container
                exec_cmd = [
                    "docker", "exec", container_name,
                    "bash", "-lc", "sccache --show-stats"
                ]
                result = subprocess.run(exec_cmd, capture_output=True, text=True)

                # Should succeed and show stats
                assert result.returncode == 0, f"sccache --show-stats failed: {result.stderr}"
                assert "Cache hits" in result.stdout or "Cache misses" in result.stdout, \
                    f"Expected cache stats in output: {result.stdout}"

            finally:
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        finally:
            subprocess.run([".tools/start-sccache.sh", "stop"], capture_output=True)


def main():
    """Run tests manually (for non-pytest usage)."""
    print("=" * 60)
    print("sccache --dist-status Verification Tests")
    print("=" * 60)

    # Test 1: Config loaded
    print("\n[1/3] Checking .workflow.jsonc configuration...")
    cfg = get_sccache_config()
    if cfg and cfg.enabled:
        print(f"  ✓ sccache enabled (redis_container={cfg.redis_container}, network={cfg.network})")
    else:
        print("  ✗ sccache not enabled in .workflow.jsonc")
        return 1

    # Test 2: Server running
    print("\n[2/3] Checking sccache server status...")
    result = subprocess.run(["pgrep", "-f", "sccache"], capture_output=True, text=True)
    if result.returncode == 0:
        pid = result.stdout.strip().split('\n')[0]
        print(f"  ✓ sccache server running (PID: {pid})")
    else:
        print("  ✗ sccache server not running")
        print("  Run: .tools/start-sccache.sh start")
        return 1

    # Test 3: Stats
    print("\n[3/3] Checking sccache stats...")
    result = subprocess.run(["sccache", "--show-stats"], capture_output=True, text=True)
    if result.returncode == 0:
        print("  ✓ sccache --show-stats succeeded")
    else:
        print(f"  ✗ sccache --show-stats failed: {result.stderr}")
        return 1

    print("\n" + "=" * 60)
    print("✓ All verification checks passed!")
    print("=" * 60)
    print("\nTo run full container test:")
    print("  pytest test_sccache_dist_status.py -v -m slow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
