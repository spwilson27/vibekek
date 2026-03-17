"""Tests for idle timeout support in _run_streaming_json."""

import json
import subprocess
import sys
import os
import threading
import time
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from workflow_lib.runners import (
    AIRunner,
    ClaudeRunner,
    GeminiRunner,
    make_runner,
)


# ---------------------------------------------------------------------------
# make_runner passes idle_timeout
# ---------------------------------------------------------------------------

class TestMakeRunnerIdleTimeout:
    def test_idle_timeout_passed_to_claude(self):
        r = make_runner("claude", idle_timeout=600)
        assert isinstance(r, ClaudeRunner)
        assert r.idle_timeout == 600

    def test_idle_timeout_passed_to_gemini(self):
        r = make_runner("gemini", idle_timeout=300)
        assert isinstance(r, GeminiRunner)
        assert r.idle_timeout == 300

    def test_idle_timeout_default_none(self):
        r = make_runner("claude")
        assert r.idle_timeout is None

    def test_all_backends_accept_idle_timeout(self):
        for backend in ("gemini", "claude", "copilot", "opencode", "cline", "aider", "codex", "qwen"):
            r = make_runner(backend, idle_timeout=999)
            assert r.idle_timeout == 999


# ---------------------------------------------------------------------------
# run_ai_command passes idle_timeout from config
# ---------------------------------------------------------------------------

class TestRunAiCommandIdleTimeout:
    def test_passes_idle_timeout_from_config(self):
        from workflow_lib.executor import run_ai_command
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={"idle_timeout": 900}):
            mock_runner = MagicMock()
            mock_runner.run.return_value = expected
            mock_make.return_value = mock_runner

            run_ai_command("prompt", "/tmp", backend="claude")

        call_kwargs = mock_make.call_args
        assert call_kwargs.kwargs.get("idle_timeout") == 900 or \
               (len(call_kwargs.args) > 6 and call_kwargs.args[6] == 900)

    def test_default_idle_timeout_1200(self):
        from workflow_lib.executor import run_ai_command
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={}):
            mock_runner = MagicMock()
            mock_runner.run.return_value = expected
            mock_make.return_value = mock_runner

            run_ai_command("prompt", "/tmp", backend="claude")

        call_kwargs = mock_make.call_args
        assert call_kwargs.kwargs.get("idle_timeout") == 1200 or \
               (len(call_kwargs.args) > 6 and call_kwargs.args[6] == 1200)


# ---------------------------------------------------------------------------
# _run_streaming_json idle timeout behavior
# ---------------------------------------------------------------------------

class TestStreamingJsonIdleTimeout:
    """Test idle timeout in _run_streaming_json using a real subprocess."""

    def test_idle_timeout_kills_claude_with_success(self):
        """ClaudeRunner: idle timeout kills process and returns exit code 0."""
        r = ClaudeRunner(idle_timeout=1)  # 1 second for fast test

        # Use a subprocess that outputs one line then sleeps forever
        script = (
            'import sys, time, json; '
            'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}), flush=True); '
            'time.sleep(60)'
        )

        lines = []
        result = r._run_streaming_json(
            ["python3", "-c", script],
            os.getcwd(),
            lines.append,
            timeout=30,
            prompt="",
        )

        assert result.returncode == 0  # Claude idle-kill -> success
        assert any("hello" in l for l in lines)

    def test_idle_timeout_kills_non_claude_with_failure(self):
        """GeminiRunner: idle timeout kills process and returns non-zero."""
        r = GeminiRunner(idle_timeout=1)

        script = (
            'import sys, time, json; '
            'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}), flush=True); '
            'time.sleep(60)'
        )

        lines = []
        result = r._run_streaming_json(
            ["python3", "-c", script],
            os.getcwd(),
            lines.append,
            timeout=30,
            prompt="",
        )

        # Non-Claude runner: idle kill preserves the actual (non-zero) returncode
        assert result.returncode != 0

    def test_no_idle_timeout_when_none(self):
        """When idle_timeout is None, process runs normally until completion."""
        r = ClaudeRunner(idle_timeout=None)

        script = (
            'import sys, json; '
            'print(json.dumps({"type":"result","result":"done"}), flush=True)'
        )

        lines = []
        result = r._run_streaming_json(
            ["python3", "-c", script],
            os.getcwd(),
            lines.append,
            timeout=10,
            prompt="",
        )

        assert result.returncode == 0

    def test_activity_resets_idle_timer(self):
        """Continuous output prevents idle timeout from firing."""
        r = ClaudeRunner(idle_timeout=2)

        # Output a line every 0.5s for 3s, then exit — should NOT be idle-killed
        script = (
            'import sys, time, json; '
            '[('
            'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"tick"}]}}), flush=True),'
            'time.sleep(0.5)'
            ') for _ in range(6)]'
        )

        lines = []
        result = r._run_streaming_json(
            ["python3", "-c", script],
            os.getcwd(),
            lines.append,
            timeout=30,
            prompt="",
        )

        assert result.returncode == 0
        assert len([l for l in lines if "tick" in l]) == 6
