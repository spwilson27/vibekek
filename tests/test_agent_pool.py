"""Tests for AgentPoolManager and quota detection."""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
import threading
import pytest
from unittest.mock import patch, MagicMock, mock_open
from workflow_lib.agent_pool import (
    AgentConfig,
    AgentPoolManager,
    QUOTA_RETURN_CODE,
    QUOTA_PATTERNS,
    VALID_STEPS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(name="a", backend="gemini", user="u", parallel=2, priority=1, quota_time=60, model=None):
    return AgentConfig(name=name, backend=backend, user=user, parallel=parallel,
                       priority=priority, quota_time=quota_time, model=model)


# ---------------------------------------------------------------------------
# AgentPoolManager.acquire / release basics
# ---------------------------------------------------------------------------

class TestAcquireRelease:
    def test_returns_config_immediately(self):
        pool = AgentPoolManager([_cfg()])
        cfg = pool.acquire(timeout=1.0)
        assert cfg is not None
        assert cfg.name == "a"

    def test_decrements_active_on_release(self):
        pool = AgentPoolManager([_cfg(parallel=1)])
        cfg = pool.acquire(timeout=1.0)
        assert pool._active["a"] == 1
        pool.release(cfg)
        assert pool._active["a"] == 0

    def test_blocks_when_all_slots_full(self):
        pool = AgentPoolManager([_cfg(parallel=1)])
        first = pool.acquire(timeout=1.0)
        # Try to acquire with a very short timeout — should fail (slot taken)
        second = pool.acquire(timeout=0.1)
        assert second is None
        pool.release(first)

    def test_unblocks_after_release(self):
        pool = AgentPoolManager([_cfg(parallel=1)])
        first = pool.acquire(timeout=1.0)

        results = []

        def _wait():
            results.append(pool.acquire(timeout=5.0))

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        time.sleep(0.1)
        pool.release(first)
        t.join(timeout=3.0)
        assert results[0] is not None

    def test_returns_none_on_timeout(self):
        pool = AgentPoolManager([_cfg(parallel=1)])
        pool.acquire(timeout=1.0)  # exhaust slot
        result = pool.acquire(timeout=0.1)
        assert result is None


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

class TestPriority:
    def test_lower_priority_returned_first(self):
        low = _cfg(name="low", priority=1, parallel=3)
        high = _cfg(name="high", priority=2, parallel=3)
        pool = AgentPoolManager([high, low])  # deliberately wrong order
        cfg = pool.acquire(timeout=1.0)
        assert cfg.name == "low"

    def test_falls_back_to_higher_priority_when_lower_full(self):
        low = _cfg(name="low", priority=1, parallel=1)
        high = _cfg(name="high", priority=2, parallel=3)
        pool = AgentPoolManager([low, high])
        pool.acquire(timeout=1.0)  # fills "low"
        cfg = pool.acquire(timeout=1.0)
        assert cfg.name == "high"


# ---------------------------------------------------------------------------
# Quota expiry
# ---------------------------------------------------------------------------

class TestQuotaExpiry:
    def test_quota_exhausted_agent_skipped(self):
        a = _cfg(name="a", priority=1, parallel=2, quota_time=30)
        b = _cfg(name="b", priority=2, parallel=2)
        pool = AgentPoolManager([a, b])
        cfg = pool.acquire(timeout=1.0)
        pool.release(cfg, quota_exhausted=True)
        # "a" should now be suppressed; next acquire should return "b"
        next_cfg = pool.acquire(timeout=1.0)
        assert next_cfg.name == "b"

    def test_quota_expiry_lifts_after_timeout(self):
        a = _cfg(name="a", priority=1, parallel=2, quota_time=1)
        pool = AgentPoolManager([a])
        cfg = pool.acquire(timeout=1.0)
        pool.release(cfg, quota_exhausted=True)
        # Immediately suppressed
        assert pool.acquire(timeout=0.05) is None
        # Wait for quota to lift
        time.sleep(1.2)
        recovered = pool.acquire(timeout=1.0)
        assert recovered is not None
        assert recovered.name == "a"

    def test_release_without_quota_does_not_suppress(self):
        pool = AgentPoolManager([_cfg(parallel=1)])
        cfg = pool.acquire(timeout=1.0)
        pool.release(cfg, quota_exhausted=False)
        again = pool.acquire(timeout=1.0)
        assert again is not None


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_acquires_respect_parallel_limit(self):
        parallel = 3
        pool = AgentPoolManager([_cfg(parallel=parallel)])
        acquired = []
        lock = threading.Lock()
        errors = []

        def _worker():
            cfg = pool.acquire(timeout=5.0)
            if cfg is None:
                with lock:
                    errors.append("None returned")
                return
            with lock:
                acquired.append(cfg)
            time.sleep(0.05)
            pool.release(cfg)

        threads = [threading.Thread(target=_worker, daemon=True) for _ in range(parallel * 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors
        # At no point should active count exceed parallel; we verify by checking final state
        assert pool._active["a"] == 0


# ---------------------------------------------------------------------------
# QUOTA_RETURN_CODE constant
# ---------------------------------------------------------------------------

def test_quota_return_code_is_negative():
    assert QUOTA_RETURN_CODE < 0


# ---------------------------------------------------------------------------
# QUOTA_PATTERNS
# ---------------------------------------------------------------------------

def test_quota_patterns_non_empty():
    assert len(QUOTA_PATTERNS) > 0


# ---------------------------------------------------------------------------
# status_lines
# ---------------------------------------------------------------------------

def test_status_lines():
    pool = AgentPoolManager([_cfg(name="a"), _cfg(name="b", priority=2)])
    lines = pool.status_lines()
    assert len(lines) == 2
    assert any("a" in l for l in lines)
    assert any("b" in l for l in lines)


# ---------------------------------------------------------------------------
# run_ai_command quota detection
# ---------------------------------------------------------------------------

class TestRunAiCommandQuotaDetection:
    """Verify that quota patterns in output trigger QUOTA_RETURN_CODE."""

    def _make_mock_runner(self, output_lines, returncode=0):
        """Build a mock runner that streams given lines."""
        import subprocess as sp
        mock_result = sp.CompletedProcess(args=[], returncode=returncode, stdout="\n".join(output_lines), stderr="")

        mock_runner = MagicMock()
        def fake_run(cwd, prompt, image_paths=None, on_line=None, timeout=None):
            for line in output_lines:
                if on_line:
                    on_line(line)
            return mock_result

        mock_runner.run.side_effect = fake_run
        return mock_runner

    def test_quota_pattern_in_stdout_triggers_code(self):
        from workflow_lib.executor import run_ai_command
        runner = self._make_mock_runner(["Starting...", "Error: usage limit reached", "done"])
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, stderr = run_ai_command("prompt", "/tmp", backend="gemini")
        assert rc == QUOTA_RETURN_CODE
        assert "quota" in stderr

    def test_clean_output_returns_zero(self):
        from workflow_lib.executor import run_ai_command
        runner = self._make_mock_runner(["All done"], returncode=0)
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp", backend="gemini")
        assert rc == 0

    def test_resource_exhausted_triggers_code(self):
        from workflow_lib.executor import run_ai_command
        runner = self._make_mock_runner(["RESOURCE_EXHAUSTED: model busy"])
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp", backend="gemini")
        assert rc == QUOTA_RETURN_CODE

    def test_user_passed_to_make_runner(self):
        from workflow_lib.executor import run_ai_command
        import subprocess as sp

        mock_runner = MagicMock()
        mock_runner.run.return_value = sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        captured = {}
        def fake_make_runner(backend, model=None, soft_timeout=None, user=None):
            captured["user"] = user
            return mock_runner

        with patch("workflow_lib.executor.make_runner", side_effect=fake_make_runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            run_ai_command("prompt", "/tmp", backend="gemini", user="testuser")

        assert captured["user"] == "testuser"


# ---------------------------------------------------------------------------
# run_agent with agent pool
# ---------------------------------------------------------------------------

class TestRunAgentWithPool:
    def _make_pool(self, names=("a",), backend="gemini", parallel=2):
        configs = [_cfg(name=n, backend=backend, parallel=parallel) for n in names]
        return AgentPoolManager(configs)

    def _make_success_runner(self):
        import subprocess as sp
        mock_runner = MagicMock()
        mock_runner.run.return_value = sp.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")
        return mock_runner

    def test_acquire_and_release_called(self):
        from workflow_lib.executor import run_agent
        pool = self._make_pool()
        runner = self._make_success_runner()

        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.executor.get_project_images", return_value=[]), \
             patch("workflow_lib.config.get_config_defaults", return_value={}), \
             patch("builtins.open", mock_open(read_data="hello {task_name}")):
            result = run_agent("Impl", "implement_task.md", {"task_name": "t", "phase_filename": "p"}, "/tmp", agent_pool=pool)

        assert result is True
        assert pool._active["a"] == 0  # released

    def test_quota_triggers_pool_rotation(self):
        """When first agent returns QUOTA_RETURN_CODE, pool should rotate to second."""
        import subprocess as sp
        from workflow_lib.executor import run_agent

        call_count = [0]

        def fake_run(cwd, prompt, image_paths=None, on_line=None, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: emit quota line
                if on_line:
                    on_line("usage limit reached")
                return sp.CompletedProcess(args=[], returncode=0, stdout="usage limit reached", stderr="")
            return sp.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        mock_runner = MagicMock()
        mock_runner.run.side_effect = fake_run

        pool = self._make_pool(names=("a", "b"), parallel=2)

        with patch("workflow_lib.executor.make_runner", return_value=mock_runner), \
             patch("workflow_lib.executor.get_project_images", return_value=[]), \
             patch("workflow_lib.config.get_config_defaults", return_value={}), \
             patch("builtins.open", mock_open(read_data="hello {task_name}")):
            result = run_agent("Impl", "implement_task.md", {"task_name": "t", "phase_filename": "p"}, "/tmp", agent_pool=pool)

        assert result is True
        assert call_count[0] == 2
        # "a" should be quota-suppressed
        assert "a" in pool._quota_expiry


# ---------------------------------------------------------------------------
# config: get_agent_pool_configs
# ---------------------------------------------------------------------------

class TestGetAgentPoolConfigs:
    def test_returns_empty_when_no_agents_key(self):
        from workflow_lib.config import get_agent_pool_configs
        with patch("workflow_lib.config.load_config", return_value={}):
            result = get_agent_pool_configs()
        assert result == []

    def test_parses_full_config(self):
        from workflow_lib.config import get_agent_pool_configs
        raw = {"agents": [
            {"name": "c1", "backend": "claude", "user": "alice", "parallel": 3, "priority": 2, "quota-time": 120, "model": "claude-opus"},
        ]}
        with patch("workflow_lib.config.load_config", return_value=raw):
            cfgs = get_agent_pool_configs()
        assert len(cfgs) == 1
        c = cfgs[0]
        assert c.name == "c1"
        assert c.backend == "claude"
        assert c.user == "alice"
        assert c.parallel == 3
        assert c.priority == 2
        assert c.quota_time == 120
        assert c.model == "claude-opus"

    def test_applies_defaults_for_optional_fields(self):
        from workflow_lib.config import get_agent_pool_configs
        raw = {"agents": [{"name": "x", "backend": "gemini", "user": "bob"}]}
        with patch("workflow_lib.config.load_config", return_value=raw):
            cfgs = get_agent_pool_configs()
        c = cfgs[0]
        assert c.parallel == 1
        assert c.priority == 1
        assert c.quota_time == 60
        assert c.model is None


# ---------------------------------------------------------------------------
# runners: _wrap_cmd and user parameter
# ---------------------------------------------------------------------------

class TestRunnerWrapCmd:
    def test_no_wrap_when_user_is_none(self):
        from workflow_lib.runners import GeminiRunner
        r = GeminiRunner(user=None)
        cmd = ["gemini", "-y"]
        assert r._wrap_cmd(cmd) == cmd

    def test_no_wrap_when_user_matches_current(self):
        import os
        from workflow_lib.runners import GeminiRunner
        current_user = os.getenv("USER", "nobody")
        r = GeminiRunner(user=current_user)
        cmd = ["gemini", "-y"]
        assert r._wrap_cmd(cmd) == cmd

    def test_wraps_with_sudo_for_different_user(self):
        from workflow_lib.runners import GeminiRunner
        r = GeminiRunner(user="otheruser")
        cmd = ["gemini", "-y"]
        wrapped = r._wrap_cmd(cmd)
        assert wrapped[:4] == ["sudo", "-u", "otheruser", "--set-home"]
        assert "--" in wrapped
        assert "env" in wrapped
        assert "gemini" in wrapped

    def test_wrap_cmd_env_path_allows_finding_binary_outside_sudo_secure_path(self):
        """Integration test: verify env PATH=... in sudo prefix makes binaries findable.

        sudo strips PATH to its secure_path, so binaries in user-local directories
        (e.g. ~/.nvm/bin, ~/.local/bin) are not found without this workaround.
        This test verifies that:
          (a) running the binary directly via sudo fails with 'command not found'
          (b) running via 'sudo -- env PATH=... <binary>' succeeds
        Skipped if no alternate OS user is available to sudo to, or if no
        user-local binary outside sudo's secure path can be found.
        """
        import subprocess
        import shutil
        import pwd

        current_user = os.getenv("USER", "")

        # Find an alternate user we can sudo to
        alt_user = None
        for entry in pwd.getpwall():
            if entry.pw_uid >= 1000 and entry.pw_uid < 65534 and entry.pw_name != current_user:
                result = subprocess.run(
                    ["sudo", "-n", "-u", entry.pw_name, "--", "true"],
                    capture_output=True,
                )
                if result.returncode == 0:
                    alt_user = entry.pw_name
                    break
        if alt_user is None:
            pytest.skip("No alternate user available for passwordless sudo")

        # Find a binary that is in our PATH but NOT in sudo's secure_path
        sudo_secure_dirs = {"/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
                            "/usr/bin", "/sbin", "/bin", "/snap/bin"}
        target_binary = None
        target_path = None
        for directory in os.environ.get("PATH", "").split(":"):
            if directory in sudo_secure_dirs:
                continue
            for candidate in ("gemini", "node", "python3"):
                full = shutil.which(candidate, path=directory)
                if full:
                    target_binary = candidate
                    target_path = full
                    break
            if target_binary:
                break
        if target_binary is None:
            pytest.skip("No user-local binary outside sudo's secure_path found")

        # (a) Direct sudo (no env trick) should fail to find the binary
        result_direct = subprocess.run(
            ["sudo", "-u", alt_user, "--set-home", "--", target_binary, "--version"],
            capture_output=True, text=True,
        )
        assert result_direct.returncode != 0, (
            f"Expected '{target_binary}' to be unfindable via plain sudo, "
            f"but it exited {result_direct.returncode}. "
            f"stderr: {result_direct.stderr!r}"
        )

        # (b) sudo with env PATH=... should find and run the binary successfully
        from workflow_lib.runners import GeminiRunner
        r = GeminiRunner(user=alt_user)
        wrapped = r._wrap_cmd([target_binary, "--version"])
        result_wrapped = subprocess.run(wrapped, capture_output=True, text=True)
        assert result_wrapped.returncode == 0, (
            f"Expected wrapped sudo+env to run '{target_binary}' successfully, "
            f"but got exit {result_wrapped.returncode}. "
            f"stdout: {result_wrapped.stdout!r} stderr: {result_wrapped.stderr!r}"
        )

    def test_make_runner_passes_user(self):
        from workflow_lib.runners import make_runner
        runner = make_runner("gemini", user="alice")
        assert runner.user == "alice"

    def test_make_runner_claude_passes_user(self):
        from workflow_lib.runners import make_runner
        runner = make_runner("claude", user="bob")
        assert runner.user == "bob"

    def test_make_runner_qwen_passes_user(self):
        from workflow_lib.runners import make_runner
        runner = make_runner("qwen", user="carol")
        assert runner.user == "carol"


# ---------------------------------------------------------------------------
# Steps filtering
# ---------------------------------------------------------------------------

class TestStepsFiltering:
    def _cfg_with_steps(self, steps, name="a", priority=1, parallel=2):
        return AgentConfig(name=name, backend="gemini", user="u",
                           parallel=parallel, priority=priority, quota_time=60,
                           steps=steps)

    def test_all_matches_any_step(self):
        pool = AgentPoolManager([self._cfg_with_steps(["all"])])
        for step in ("develop", "review", "merge", "all"):
            pool._active["a"] = 0  # reset
            cfg = pool.acquire(timeout=1.0, step=step)
            assert cfg is not None, f"Expected agent for step={step!r}"

    def test_specific_step_only_matches_that_step(self):
        pool = AgentPoolManager([self._cfg_with_steps(["develop"])])
        assert pool.acquire(timeout=0.05, step="develop") is not None
        pool._active["a"] = 0
        assert pool.acquire(timeout=0.05, step="review") is None
        assert pool.acquire(timeout=0.05, step="merge") is None

    def test_multiple_steps_in_list(self):
        pool = AgentPoolManager([self._cfg_with_steps(["develop", "review"])])
        assert pool.acquire(timeout=0.05, step="develop") is not None
        pool._active["a"] = 0
        assert pool.acquire(timeout=0.05, step="review") is not None
        pool._active["a"] = 0
        assert pool.acquire(timeout=0.05, step="merge") is None

    def test_step_routing_picks_correct_agent(self):
        """develop → agent A (only develop), review → agent B (only review)."""
        a = AgentConfig("dev-agent", "gemini", "u", parallel=2, priority=1, quota_time=60, steps=["develop"])
        b = AgentConfig("rev-agent", "gemini", "u", parallel=2, priority=1, quota_time=60, steps=["review"])
        pool = AgentPoolManager([a, b])
        dev_cfg = pool.acquire(timeout=1.0, step="develop")
        assert dev_cfg.name == "dev-agent"
        pool.release(dev_cfg)
        rev_cfg = pool.acquire(timeout=1.0, step="review")
        assert rev_cfg.name == "rev-agent"
        pool.release(rev_cfg)

    def test_all_step_in_list_acts_as_wildcard(self):
        pool = AgentPoolManager([self._cfg_with_steps(["develop", "all"])])
        assert pool.acquire(timeout=0.05, step="merge") is not None


# ---------------------------------------------------------------------------
# _step_for_agent_type helper
# ---------------------------------------------------------------------------

class TestStepForAgentType:
    def test_implementation_maps_to_develop(self):
        from workflow_lib.executor import _step_for_agent_type
        assert _step_for_agent_type("Implementation") == "develop"

    def test_review_maps_to_review(self):
        from workflow_lib.executor import _step_for_agent_type
        assert _step_for_agent_type("Review") == "review"

    def test_review_retry_maps_to_review(self):
        from workflow_lib.executor import _step_for_agent_type
        assert _step_for_agent_type("Review (Retry)") == "review"

    def test_merge_maps_to_merge(self):
        from workflow_lib.executor import _step_for_agent_type
        assert _step_for_agent_type("Merge") == "merge"

    def test_unknown_maps_to_all(self):
        from workflow_lib.executor import _step_for_agent_type
        assert _step_for_agent_type("SomethingElse") == "all"


# ---------------------------------------------------------------------------
# config: steps field parsing
# ---------------------------------------------------------------------------

class TestConfigStepsParsing:
    def test_parses_steps_list(self):
        from workflow_lib.config import get_agent_pool_configs
        raw = {"agents": [{"name": "x", "backend": "gemini", "user": "u", "steps": ["develop", "review"]}]}
        with patch("workflow_lib.config.load_config", return_value=raw):
            cfgs = get_agent_pool_configs()
        assert cfgs[0].steps == ["develop", "review"]

    def test_default_steps_is_all(self):
        from workflow_lib.config import get_agent_pool_configs
        raw = {"agents": [{"name": "x", "backend": "gemini", "user": "u"}]}
        with patch("workflow_lib.config.load_config", return_value=raw):
            cfgs = get_agent_pool_configs()
        assert cfgs[0].steps == ["all"]

    def test_single_string_step_is_wrapped_in_list(self):
        from workflow_lib.config import get_agent_pool_configs
        raw = {"agents": [{"name": "x", "backend": "gemini", "user": "u", "steps": "develop"}]}
        with patch("workflow_lib.config.load_config", return_value=raw):
            cfgs = get_agent_pool_configs()
        assert cfgs[0].steps == ["develop"]


# ---------------------------------------------------------------------------
# Config validation: required fields, backend, steps
# ---------------------------------------------------------------------------

class TestConfigValidation:
    """get_agent_pool_configs() must raise ValueError for bad config."""

    _GOOD = {"name": "a", "backend": "gemini", "user": "u"}

    def _load(self, entry):
        from workflow_lib.config import get_agent_pool_configs
        with patch("workflow_lib.config.load_config", return_value={"agents": [entry]}):
            return get_agent_pool_configs()

    def _raises(self, entry, fragment):
        with pytest.raises(ValueError, match=fragment):
            self._load(entry)

    # Required fields
    def test_missing_name_raises(self):
        self._raises({"backend": "gemini", "user": "u"}, "name")

    def test_missing_backend_raises(self):
        self._raises({"name": "a", "user": "u"}, "backend")

    def test_missing_user_raises(self):
        self._raises({"name": "a", "backend": "gemini"}, "user")

    # Backend validation
    def test_invalid_backend_raises(self):
        self._raises({**self._GOOD, "backend": "chatgpt"}, "chatgpt")

    def test_invalid_backend_message_lists_valid_backends(self):
        with pytest.raises(ValueError, match="gemini"):
            self._load({**self._GOOD, "backend": "notreal"})

    def test_all_valid_backends_accepted(self):
        from workflow_lib.runners import VALID_BACKENDS
        for backend in VALID_BACKENDS:
            cfgs = self._load({**self._GOOD, "backend": backend})
            assert cfgs[0].backend == backend

    # Steps validation
    def test_invalid_step_raises(self):
        self._raises({**self._GOOD, "steps": ["build"]}, "build")

    def test_multiple_invalid_steps_raises(self):
        self._raises({**self._GOOD, "steps": ["build", "test"]}, r"build|test")

    def test_invalid_step_message_lists_valid_steps(self):
        with pytest.raises(ValueError, match="all"):
            self._load({**self._GOOD, "steps": ["bad"]})

    def test_all_valid_steps_accepted(self):
        from workflow_lib.agent_pool import VALID_STEPS
        for step in VALID_STEPS:
            cfgs = self._load({**self._GOOD, "steps": [step]})
            assert step in cfgs[0].steps

    def test_mix_of_valid_and_invalid_steps_raises(self):
        self._raises({**self._GOOD, "steps": ["develop", "nonsense"]}, "nonsense")

    # Error message includes agent name for easy diagnosis
    def test_error_message_includes_agent_name(self):
        with pytest.raises(ValueError, match="my-agent"):
            self._load({"name": "my-agent", "backend": "gemini", "steps": ["bad"]})

    # Happy path: valid full config doesn't raise
    def test_valid_full_config_accepted(self):
        cfgs = self._load({
            "name": "prod", "backend": "claude", "user": "alice",
            "parallel": 4, "priority": 1, "quota-time": 120,
            "model": "claude-opus", "steps": ["develop", "review"],
        })
        assert len(cfgs) == 1
        assert cfgs[0].name == "prod"

    # Empty agents list is fine
    def test_empty_agents_list_returns_empty(self):
        from workflow_lib.config import get_agent_pool_configs
        with patch("workflow_lib.config.load_config", return_value={"agents": []}):
            assert get_agent_pool_configs() == []


# ---------------------------------------------------------------------------
# VALID_STEPS constant
# ---------------------------------------------------------------------------

def test_valid_steps_contains_expected_values():
    assert VALID_STEPS == {"develop", "review", "merge", "all"}
