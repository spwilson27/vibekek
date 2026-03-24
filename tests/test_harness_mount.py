"""Tests for harness.py read-only bind-mount in Docker containers.

Verifies that:
1. harness.py is bind-mounted as :ro at /harness.py in every container
2. A per-container tmpfile is used (host checkout is not directly mounted)
3. The tmpfile is chmod 444 on the host before mounting
4. Missing harness.py on the host raises FileNotFoundError before docker run
5. Each container gets its own independent tmpfile (no cross-container sharing)
6. The tmpfile is deleted when the container stops (_stop_task_container)
7. The tmpfile is deleted even if container removal fails (best-effort cleanup)
8. The :ro flag is present — not just a plain bind mount
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from unittest.mock import patch, MagicMock

from workflow_lib.agent_pool import DockerConfig
from workflow_lib.executor import (
    _start_task_container,
    _stop_task_container,
    _harness_tmpfiles,
    _harness_tmpfiles_lock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _docker_cfg(image="test-image:latest", volumes=None):
    return DockerConfig(image=image, volumes=volumes or [], copy_files=[])


def _write_harness(tools_dir, content="# harness stub"):
    harness_dir = os.path.join(str(tools_dir), "harness")
    os.makedirs(harness_dir, exist_ok=True)
    harness_path = os.path.join(harness_dir, "harness.py")
    with open(harness_path, "w", encoding="utf-8") as f:
        f.write(content)
    return harness_path


def _make_fake_run(inspect_stdout="true\n", ps_stdout="abc123\n"):
    """Return a fake subprocess.run that satisfies all docker lifecycle checks."""
    def fake_run(cmd, **kwargs):
        if not isinstance(cmd, list):
            return MagicMock(returncode=0, stdout="", stderr="")
        if "inspect" in cmd:
            return MagicMock(returncode=0, stdout=inspect_stdout, stderr="")
        if "ps" in cmd:
            return MagicMock(returncode=0, stdout=ps_stdout, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return fake_run


# ---------------------------------------------------------------------------
# Tests: bind-mount presence and shape
# ---------------------------------------------------------------------------

class TestHarnessMountPresence:
    def test_harness_mounted_as_ro_bind(self, tmp_path):
        """harness.py must appear as a readonly bind mount at /harness.py."""
        _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return _make_fake_run()(cmd, **kwargs)

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr_ro", _docker_cfg(), env_file, print)

        docker_run_cmd = run_calls[0]
        mount_args = [docker_run_cmd[i + 1] for i in range(len(docker_run_cmd))
                      if docker_run_cmd[i] == "--mount"]
        harness_mounts = [v for v in mount_args if "target=/harness.py" in v]
        assert harness_mounts, "No --mount entry for /harness.py found in docker run command"
        assert all("readonly" in v for v in harness_mounts), (
            f"harness.py mount must be readonly, got: {harness_mounts}"
        )

    def test_host_harness_py_not_directly_mounted(self, tmp_path):
        """The host harness.py path must NOT be bind-mounted directly.

        A tmpfile copy must be used so the host checkout is not locked open.
        """
        harness = _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return _make_fake_run()(cmd, **kwargs)

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr_notdirect", _docker_cfg(), env_file, print)

        docker_run_cmd = run_calls[0]
        mount_args = [docker_run_cmd[i + 1] for i in range(len(docker_run_cmd))
                      if docker_run_cmd[i] == "--mount"]
        harness_mounts = [v for v in mount_args if "target=/harness.py" in v]
        host_path = str(harness)
        direct_mounts = [v for v in harness_mounts if f"src={host_path}" in v]
        assert not direct_mounts, (
            f"Host harness.py was mounted directly (locks checkout): {direct_mounts}. "
            "A tmpfile copy must be used instead."
        )

    def test_harness_mount_uses_realpath_source(self, tmp_path):
        """The bind mount should use the canonical temp path as its source."""
        _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return _make_fake_run()(cmd, **kwargs)

        fake_tmp_path = str(tmp_path / "tmp" / "harness.py")
        real_tmp_path = str(tmp_path / "canonical" / "harness.py")
        os.makedirs(os.path.dirname(fake_tmp_path), exist_ok=True)
        os.makedirs(os.path.dirname(real_tmp_path), exist_ok=True)
        realpath_orig = os.path.realpath

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("tempfile.NamedTemporaryFile") as mock_tmp, \
             patch("workflow_lib.executor.os.path.realpath", side_effect=lambda p: real_tmp_path if p == fake_tmp_path else realpath_orig(p)), \
             patch("subprocess.run", side_effect=fake_run):
            mock_tmp.return_value.name = fake_tmp_path
            mock_tmp.return_value.close.return_value = None
            _start_task_container("ctr_realpath", _docker_cfg(), env_file, print)

        docker_run_cmd = run_calls[0]
        mount_args = [docker_run_cmd[i + 1] for i in range(len(docker_run_cmd))
                      if docker_run_cmd[i] == "--mount"]
        harness_mounts = [v for v in mount_args if "target=/harness.py" in v]
        assert harness_mounts, "No harness bind mount found"
        assert any(f"src={real_tmp_path}" in v for v in harness_mounts), (
            f"harness.py mount must use canonical realpath source, got: {harness_mounts}"
        )
        assert all(f"src={fake_tmp_path}" not in v for v in harness_mounts), (
            f"harness.py mount should not use raw temp path, got: {harness_mounts}"
        )

    def test_container_verifies_harness_mount_is_regular_file(self, tmp_path):
        """Container startup should check that /harness.py is a regular file."""
        _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            if cmd[:6] == ["docker", "exec", "-i", "ctr_filecheck", "sh", "-lc"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return _make_fake_run()(cmd, **kwargs)

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=fake_run):
            _start_task_container("ctr_filecheck", _docker_cfg(), env_file, print)

        assert ["docker", "exec", "-i", "ctr_filecheck", "sh", "-lc", "test -f /harness.py"] in run_calls

    def test_raises_if_harness_mount_is_not_regular_file(self, tmp_path):
        """Container startup must fail if /harness.py is not mounted as a file."""
        _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        def fake_run(cmd, **kwargs):
            if cmd[:6] == ["docker", "exec", "-i", "ctr_badmount", "sh", "-lc"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            return _make_fake_run()(cmd, **kwargs)

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="invalid harness mount"):
                _start_task_container("ctr_badmount", _docker_cfg(), env_file, print)


# ---------------------------------------------------------------------------
# Tests: tmpfile properties
# ---------------------------------------------------------------------------

class TestHarnessTmpfile:
    def test_tmpfile_is_chmod_444(self, tmp_path):
        """The staged tmpfile must have mode 0o444 (read-only for all) on the host.

        We check the actual file permissions after _start_task_container returns
        rather than spying on os.chmod calls, because shutil.copy2 also invokes
        os.chmod (via copystat) to preserve source permissions — checking only
        that os.chmod was called would conflate the two calls.
        """
        _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=_make_fake_run()):
            _start_task_container("ctr_chmod", _docker_cfg(), env_file, print)

        with _harness_tmpfiles_lock:
            tmp = _harness_tmpfiles.get("ctr_chmod")

        assert tmp is not None, "No tmpfile registered"
        actual_mode = oct(os.stat(tmp).st_mode & 0o777)
        assert actual_mode == oct(0o444), (
            f"Harness tmpfile {tmp!r} must have mode 0o444, got {actual_mode}"
        )

        # Cleanup
        with _harness_tmpfiles_lock:
            _harness_tmpfiles.pop("ctr_chmod", None)
        os.chmod(tmp, 0o644)
        os.unlink(tmp)

    def test_tmpfile_registered_in_harness_tmpfiles(self, tmp_path):
        """After container start the tmpfile path must be in _harness_tmpfiles."""
        _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=_make_fake_run()):
            _start_task_container("ctr_reg", _docker_cfg(), env_file, print)

        with _harness_tmpfiles_lock:
            assert "ctr_reg" in _harness_tmpfiles, (
                "container not found in _harness_tmpfiles after start"
            )
            tmp = _harness_tmpfiles["ctr_reg"]

        assert os.path.exists(tmp), f"Tmpfile {tmp!r} should still exist while container is running"
        # Cleanup
        with _harness_tmpfiles_lock:
            _harness_tmpfiles.pop("ctr_reg", None)
        os.unlink(tmp)

    def test_each_container_gets_distinct_tmpfile(self, tmp_path):
        """Two concurrent containers must receive independent tmpfile copies."""
        _write_harness(tmp_path)

        tmpfiles = {}

        for ctr in ("ctr_a", "ctr_b"):
            env_file = str(tmp_path / f"{ctr}.env")
            open(env_file, "w").close()
            with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
                 patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
                 patch("subprocess.run", side_effect=_make_fake_run()):
                _start_task_container(ctr, _docker_cfg(), env_file, print)

            with _harness_tmpfiles_lock:
                tmpfiles[ctr] = _harness_tmpfiles.get(ctr)

        assert tmpfiles["ctr_a"] != tmpfiles["ctr_b"], (
            "Each container must get its own distinct tmpfile copy of harness.py"
        )

        # Cleanup
        for ctr, path in tmpfiles.items():
            with _harness_tmpfiles_lock:
                _harness_tmpfiles.pop(ctr, None)
            if path and os.path.exists(path):
                os.unlink(path)

    def test_tmpfile_content_matches_harness(self, tmp_path):
        """The staged tmpfile content must be identical to harness.py."""
        sentinel = "# SENTINEL_CONTENT_12345"
        _write_harness(tmp_path, sentinel)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=_make_fake_run()):
            _start_task_container("ctr_content", _docker_cfg(), env_file, print)

        with _harness_tmpfiles_lock:
            tmp = _harness_tmpfiles.get("ctr_content")

        assert tmp is not None, "No tmpfile registered"
        # chmod 444 means we can still read it
        with open(tmp) as f:
            content = f.read()
        assert sentinel in content, (
            f"Tmpfile content does not match harness.py. Got: {content!r}"
        )

        # Cleanup
        with _harness_tmpfiles_lock:
            _harness_tmpfiles.pop("ctr_content", None)
        os.chmod(tmp, 0o644)
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Tests: missing harness.py
# ---------------------------------------------------------------------------

class TestHarnessMissing:
    def test_raises_if_harness_missing(self, tmp_path):
        """FileNotFoundError must be raised before docker run if harness.py is absent."""
        # tmp_path has no harness.py in TOOLS_DIR/harness/
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(FileNotFoundError, match="harness.py"):
                _start_task_container("ctr_missing", _docker_cfg(), env_file, print)

    def test_docker_run_not_called_if_harness_missing(self, tmp_path):
        """docker run must NOT be invoked when harness.py is absent."""
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(FileNotFoundError):
                _start_task_container("ctr_no_docker_run", _docker_cfg(), env_file, print)

        docker_run_calls = [c for c in run_calls
                            if isinstance(c, list) and "run" in c and "-d" in c]
        assert not docker_run_calls, (
            "docker run must not be called when harness.py is missing"
        )


# ---------------------------------------------------------------------------
# Tests: tmpfile cleanup on container stop
# ---------------------------------------------------------------------------

class TestHarnessTmpfileCleanup:
    def test_tmpfile_deleted_on_container_stop(self, tmp_path):
        """_stop_task_container must delete the tmpfile after removing the container."""
        _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=_make_fake_run()):
            _start_task_container("ctr_cleanup", _docker_cfg(), env_file, print)

        with _harness_tmpfiles_lock:
            tmp = _harness_tmpfiles.get("ctr_cleanup")

        assert tmp is not None, "Tmpfile not registered"
        assert os.path.exists(tmp), "Tmpfile should exist before stop"

        with patch("subprocess.run", side_effect=_make_fake_run()):
            _stop_task_container("ctr_cleanup", print)

        assert not os.path.exists(tmp), (
            f"Tmpfile {tmp!r} was not deleted after container stop"
        )
        with _harness_tmpfiles_lock:
            assert "ctr_cleanup" not in _harness_tmpfiles, (
                "Container entry not removed from _harness_tmpfiles after stop"
            )

    def test_tmpfile_deleted_even_if_docker_rm_fails(self, tmp_path):
        """Tmpfile cleanup must happen even when docker rm fails."""
        _write_harness(tmp_path)
        env_file = str(tmp_path / "container.env")
        open(env_file, "w").close()

        with patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)), \
             patch("subprocess.run", side_effect=_make_fake_run()):
            _start_task_container("ctr_rmfail", _docker_cfg(), env_file, print)

        with _harness_tmpfiles_lock:
            tmp = _harness_tmpfiles.get("ctr_rmfail")

        assert tmp and os.path.exists(tmp)

        def fail_rm(cmd, **kwargs):
            if isinstance(cmd, list) and "rm" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="no such container")
            return MagicMock(returncode=0, stdout="ctr_rmfail\n", stderr="")

        with patch("subprocess.run", side_effect=fail_rm):
            _stop_task_container("ctr_rmfail", print)

        assert not os.path.exists(tmp), (
            "Tmpfile must be cleaned up even when docker rm fails"
        )

    def test_stop_noop_for_unknown_container(self):
        """_stop_task_container must not error when container has no registered tmpfile."""
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="")):
            # Should not raise
            _stop_task_container("ctr_unknown_xyz", print)
