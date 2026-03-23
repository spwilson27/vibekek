"""Unit tests for ensure_sccache_services and related config functions."""

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.config import (
    ensure_sccache_services,
    get_sccache_services_config,
    get_sccache_dist_config,
    SCCacheConfig,
    SCCacheServicesConfig,
    SCCacheDistConfig,
)


def test_ensure_sccache_services_no_config():
    """Returns (True, True) when no sccache_services config."""
    result = ensure_sccache_services()
    assert result == (True, True)


def test_get_sccache_services_config_present():
    """Returns SCCacheServicesConfig when config is present."""
    fake_cfg = {
        "sccache_services": {
            "auto_start": True,
            "configure_containers": True,
        }
    }
    with patch("workflow_lib.config.load_config", return_value=fake_cfg):
        result = get_sccache_services_config()
    assert result is not None
    assert result.auto_start is True
    assert result.configure_containers is True


def test_get_sccache_dist_config_present():
    """Returns SCCacheDistConfig when config is present."""
    fake_cfg = {
        "sccache_dist": {
            "enabled": True,
            "scheduler_url": "http://localhost:10600",
            "auth_token": "test-token",
            "config_file": "/tmp/sccache-dist.toml",
        }
    }
    with patch("workflow_lib.config.load_config", return_value=fake_cfg):
        result = get_sccache_dist_config()
    assert result is not None
    assert result.enabled is True
    assert result.scheduler_url == "http://localhost:10600"


def test_ensure_sccache_services_with_sccache_enabled():
    """Test ensure_sccache_services with sccache enabled - redis already running."""
    svc_cfg = SCCacheServicesConfig(auto_start=True, configure_containers=True)
    scc_cfg = SCCacheConfig(
        enabled=True,
        redis_container="test-redis",
        network="test-net",
        redis_port=6379,
        redis_maxmemory="1gb",
        redis_image="redis:7-alpine",
    )

    mock_run = MagicMock()
    # Network inspect succeeds (network exists)
    # Container inspect succeeds (running)
    mock_run.return_value = MagicMock(returncode=0, stdout="true\n", stderr="")

    with patch("workflow_lib.config.get_sccache_services_config", return_value=svc_cfg), \
         patch("workflow_lib.config.get_sccache_config", return_value=scc_cfg), \
         patch("workflow_lib.config.get_sccache_dist_config", return_value=None), \
         patch("subprocess.run", mock_run):
        result = ensure_sccache_services()
    assert result[0] is True


def test_ensure_sccache_services_redis_not_running():
    """Test ensure_sccache_services starts redis when not running."""
    svc_cfg = SCCacheServicesConfig(auto_start=True, configure_containers=True)
    scc_cfg = SCCacheConfig(
        enabled=True,
        redis_container="test-redis",
        network="test-net",
        redis_port=6379,
        redis_maxmemory="1gb",
        redis_image="redis:7-alpine",
    )

    call_count = [0]

    def fake_run(cmd, **kwargs):
        call_count[0] += 1
        if "network" in cmd and "inspect" in cmd:
            return MagicMock(returncode=0)  # Network exists
        if "inspect" in cmd and "-f" in cmd:
            return MagicMock(returncode=1, stdout="", stderr="")  # Container not running
        if "rm" in cmd:
            return MagicMock(returncode=0)
        if "run" in cmd:
            return MagicMock(returncode=0)  # Docker run succeeds
        return MagicMock(returncode=0)

    with patch("workflow_lib.config.get_sccache_services_config", return_value=svc_cfg), \
         patch("workflow_lib.config.get_sccache_config", return_value=scc_cfg), \
         patch("workflow_lib.config.get_sccache_dist_config", return_value=None), \
         patch("subprocess.run", side_effect=fake_run):
        result = ensure_sccache_services()
    assert result[0] is True


def test_ensure_sccache_services_network_create():
    """Test ensure_sccache_services creates network when missing."""
    svc_cfg = SCCacheServicesConfig(auto_start=True, configure_containers=True)
    scc_cfg = SCCacheConfig(
        enabled=True,
        redis_container="test-redis",
        network="test-net",
        redis_port=6379,
        redis_maxmemory="1gb",
        redis_image="redis:7-alpine",
    )

    def fake_run(cmd, **kwargs):
        if "network" in cmd and "inspect" in cmd:
            return MagicMock(returncode=1)  # Network doesn't exist
        if "network" in cmd and "create" in cmd:
            return MagicMock(returncode=0)
        if "inspect" in cmd:
            return MagicMock(returncode=0, stdout="true\n")  # Container running
        return MagicMock(returncode=0)

    with patch("workflow_lib.config.get_sccache_services_config", return_value=svc_cfg), \
         patch("workflow_lib.config.get_sccache_config", return_value=scc_cfg), \
         patch("workflow_lib.config.get_sccache_dist_config", return_value=None), \
         patch("subprocess.run", side_effect=fake_run):
        result = ensure_sccache_services()
    assert result[0] is True


def test_ensure_sccache_services_with_dist_enabled():
    """Test ensure_sccache_services with sccache-dist enabled."""
    svc_cfg = SCCacheServicesConfig(auto_start=True, configure_containers=True)
    dist_cfg = SCCacheDistConfig(
        enabled=True,
        scheduler_url="http://localhost:10600",
        auth_token="test-token",
        config_file="/tmp/sccache-dist.toml",
    )

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and "pgrep" in cmd:
            return MagicMock(returncode=0, stdout="12345\n")  # Already running
        return MagicMock(returncode=0)

    with patch("workflow_lib.config.get_sccache_services_config", return_value=svc_cfg), \
         patch("workflow_lib.config.get_sccache_config", return_value=None), \
         patch("workflow_lib.config.get_sccache_dist_config", return_value=dist_cfg), \
         patch("subprocess.run", side_effect=fake_run):
        result = ensure_sccache_services()
    assert result == (True, True)
