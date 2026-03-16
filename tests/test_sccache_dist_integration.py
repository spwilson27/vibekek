#!/usr/bin/env python3
"""Integration test for sccache-dist workflow with agent containers.

This script verifies:
1. sccache-dist scheduler is running and accessible
2. Container can connect to scheduler from inside Docker
3. Workflow config loads sccache-dist settings correctly
4. Container env vars are set correctly for distributed compilation
"""

import sys
import os
import subprocess
import tempfile

# Add tools directory to path
tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, tools_dir)

from workflow_lib.config import get_sccache_dist_config, get_sccache_config, SCCacheDistConfig
from workflow_lib.agent_pool import DockerConfig
from workflow_lib.executor import _start_task_container
from unittest.mock import patch, MagicMock


def test_scheduler_running():
    """Verify sccache-dist scheduler is running."""
    print("[1/4] Checking sccache-dist scheduler...")
    
    result = subprocess.run(["pgrep", "-f", "sccache-dist"], capture_output=True, text=True)
    if result.returncode != 0:
        print("  ✗ sccache-dist scheduler not running")
        return False
    
    pid = result.stdout.strip().split('\n')[0]
    print(f"  ✓ sccache-dist running (PID: {pid})")
    
    # Check port binding
    result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
    if "10600" in result.stdout:
        print("  ✓ Scheduler listening on port 10600")
        return True
    else:
        print("  ✗ Scheduler not listening on expected port")
        return False


def test_container_connectivity():
    """Test container can reach scheduler."""
    print("\n[2/4] Testing container connectivity to scheduler...")
    
    # Run a test container
    docker_cmd = [
        "docker", "run", "--rm",
        "--add-host", "host.docker.internal:host-gateway",
        "-e", "SCCACHE_DIST_SCHEDULER_URL=http://host.docker.internal:10600",
        "-e", "SCCACHE_AUTH_TOKEN=gooey-dist-token-2024",
        "ubuntu:24.04",
        "bash", "-c",
        "apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq curl >/dev/null 2>&1 && " +
        "curl -s -o /dev/null -w '%{http_code}' http://host.docker.internal:10600/api/v1/scheduler/status"
    ]
    
    result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=60)
    
    if result.returncode == 0 and "200" in result.stdout:
        print("  ✓ Container can reach scheduler (HTTP 200)")
        return True
    else:
        print(f"  ✗ Container connectivity failed: {result.stderr}")
        return False


def test_config_loading():
    """Test workflow config loads sccache-dist settings."""
    print("\n[3/4] Testing config loading...")
    
    dist_cfg = get_sccache_dist_config()
    if dist_cfg is None:
        print("  ✗ sccache_dist config not found")
        return False
    
    print(f"  ✓ sccache_dist config loaded:")
    print(f"    - enabled: {dist_cfg.enabled}")
    print(f"    - scheduler_url: {dist_cfg.scheduler_url}")
    print(f"    - auth_token: {dist_cfg.auth_token}")
    
    # Verify required fields
    if not dist_cfg.scheduler_url:
        print("  ✗ scheduler_url missing")
        return False
    if not dist_cfg.auth_token:
        print("  ✗ auth_token missing")
        return False
    
    print("  ✓ All required fields present")
    return True


def test_container_env_vars():
    """Test container env vars are set correctly."""
    print("\n[4/4] Testing container env vars...")
    
    with patch('workflow_lib.executor.subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='test123')
        
        dist_cfg = SCCacheDistConfig(
            enabled=True,
            scheduler_url="http://host.docker.internal:10600",
            auth_token="test-token",
            config_file="/tmp/sccache-dist.toml"
        )
        docker_cfg = DockerConfig(image='test:latest', volumes=[], copy_files=[])
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write('TEST=value\n')
            env_file = f.name
        
        try:
            _start_task_container(
                'test-container',
                docker_cfg,
                env_file,
                lambda m: None,
                sccache_config=None,
                sccache_dist_config=dist_cfg
            )
            
            # Check the docker command
            docker_cmd = mock_run.call_args[0][0]
            env_vars = []
            for i, arg in enumerate(docker_cmd):
                if arg == '-e' and i + 1 < len(docker_cmd):
                    env_vars.append(docker_cmd[i + 1])
            
            # Verify required env vars
            required = {
                'SCCACHE_DIST_SCHEDULER_URL': False,
                'SCCACHE_AUTH_TOKEN': False,
                'RUSTC_WRAPPER': False,
            }
            
            for ev in env_vars:
                if 'SCCACHE_DIST_SCHEDULER_URL' in ev:
                    required['SCCACHE_DIST_SCHEDULER_URL'] = True
                if 'SCCACHE_AUTH_TOKEN' in ev:
                    required['SCCACHE_AUTH_TOKEN'] = True
                if 'RUSTC_WRAPPER' in ev:
                    required['RUSTC_WRAPPER'] = True
            
            all_present = all(required.values())
            
            if all_present:
                print("  ✓ All required env vars present:")
                for key, present in required.items():
                    status = "✓" if present else "✗"
                    print(f"    {status} {key}")
                return True
            else:
                print("  ✗ Missing env vars:")
                for key, present in required.items():
                    if not present:
                        print(f"    ✗ {key}")
                return False
        finally:
            os.unlink(env_file)


def test_auto_start_config():
    """Test auto_start config option is loaded correctly."""
    print("\n[5/5] Testing auto_start configuration...")
    
    from workflow_lib.config import get_sccache_config, get_sccache_dist_config, SCCacheConfig, SCCacheDistConfig
    
    # Test sccache auto_start
    scc_cfg = get_sccache_config()
    if scc_cfg:
        print(f"  sccache auto_start: {scc_cfg.auto_start}")
        print("  ✓ sccache auto_start config loaded")
    else:
        print("  ✗ sccache config not found")
        return False
    
    # Test sccache-dist auto_start
    dist_cfg = get_sccache_dist_config()
    if dist_cfg:
        print(f"  sccache_dist auto_start: {dist_cfg.auto_start}")
        print("  ✓ sccache_dist auto_start config loaded")
    else:
        print("  ✗ sccache_dist config not found")
        return False
    
    return True


def test_ensure_sccache_services():
    """Test ensure_sccache_services function."""
    print("\n[6/6] Testing ensure_sccache_services()...")
    
    from workflow_lib.config import ensure_sccache_services, SCCacheConfig, SCCacheDistConfig
    
    # Mock config with auto_start enabled
    import workflow_lib.config as config
    original_get_sccache = config.get_sccache_config
    original_get_dist = config.get_sccache_dist_config
    
    try:
        # Test with auto_start=False (should not try to start)
        config.get_sccache_config = lambda: SCCacheConfig(enabled=True, auto_start=False)
        config.get_sccache_dist_config = lambda: SCCacheDistConfig(enabled=True, auto_start=False)
        
        scc_ok, dist_ok = ensure_sccache_services()
        
        if scc_ok and dist_ok:
            print("  ✓ ensure_sccache_services() returned success")
            return True
        else:
            print("  ✗ ensure_sccache_services() returned failure")
            return False
    finally:
        config.get_sccache_config = original_get_sccache
        config.get_sccache_dist_config = original_get_dist


def main():
    """Run all integration tests."""
    print("=" * 60)
    print("sccache-dist Integration Tests")
    print("=" * 60)
    
    results = []
    
    results.append(("Scheduler running", test_scheduler_running()))
    results.append(("Container connectivity", test_container_connectivity()))
    results.append(("Config loading", test_config_loading()))
    results.append(("Container env vars", test_container_env_vars()))
    results.append(("Auto-start config", test_auto_start_config()))
    results.append(("Ensure services", test_ensure_sccache_services()))
    
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {status}: {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n✓ All integration tests passed!")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
