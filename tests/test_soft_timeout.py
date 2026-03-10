"""Tests for soft-timeout support in SessionResumableRunner and QwenRunner."""

import subprocess
import sys
import os
import uuid
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from workflow_lib.runners import (
    AIRunner,
    SessionResumableRunner,
    GeminiRunner,
    ClaudeRunner,
    QwenRunner,
    make_runner,
    parse_stream_json_line,
    RESUME_PROMPT,
)


# ---------------------------------------------------------------------------
# make_runner factory
# ---------------------------------------------------------------------------

class TestMakeRunner:
    def test_gemini_default(self):
        r = make_runner("gemini")
        assert isinstance(r, GeminiRunner)
        assert not isinstance(r, SessionResumableRunner)

    def test_qwen_default(self):
        r = make_runner("qwen")
        assert isinstance(r, QwenRunner)
        assert r.soft_timeout == QwenRunner.DEFAULT_SOFT_TIMEOUT

    def test_qwen_custom_soft_timeout(self):
        r = make_runner("qwen", soft_timeout=120)
        assert isinstance(r, QwenRunner)
        assert r.soft_timeout == 120

    def test_qwen_with_model(self):
        r = make_runner("qwen", model="qwen-coder")
        assert r.model == "qwen-coder"

    def test_non_resumable_backends(self):
        for backend in ("gemini", "claude", "copilot", "opencode", "cline", "aider", "codex"):
            r = make_runner(backend)
            assert not isinstance(r, SessionResumableRunner)

    def test_unknown_backend_defaults_to_gemini(self):
        r = make_runner("unknown_backend")
        assert isinstance(r, GeminiRunner)


# ---------------------------------------------------------------------------
# GeminiRunner (no soft timeout)
# ---------------------------------------------------------------------------

class TestGeminiGetCmd:
    def test_basic(self):
        r = GeminiRunner()
        assert r.get_cmd() == ["gemini", "-y"]

    def test_with_model(self):
        r = GeminiRunner(model="gemini-2.5-pro")
        assert r.get_cmd() == ["gemini", "-y", "--model", "gemini-2.5-pro"]


# ---------------------------------------------------------------------------
# QwenRunner.get_cmd
# ---------------------------------------------------------------------------

class TestQwenGetCmd:
    def test_basic(self):
        r = QwenRunner()
        cmd = r.get_cmd()
        assert cmd[:4] == ["qwen", "-y", "--output-format", "stream-json"]

    def test_with_model(self):
        r = QwenRunner(model="qwen-coder")
        cmd = r.get_cmd()
        assert "-m" in cmd
        assert "qwen-coder" in cmd

    def test_with_session_id(self):
        r = QwenRunner()
        cmd = r.get_cmd(session_id="xyz-789")
        assert "--session-id" in cmd
        assert "xyz-789" in cmd

    def test_with_resume(self):
        r = QwenRunner()
        cmd = r.get_cmd(session_id="xyz-789", resume=True)
        assert "--resume" in cmd
        assert "xyz-789" in cmd
        assert "--session-id" not in cmd

    def test_no_session_flags_without_id(self):
        r = QwenRunner()
        cmd = r.get_cmd()
        assert "--session-id" not in cmd
        assert "--resume" not in cmd


# ---------------------------------------------------------------------------
# QwenRunner._build_resume_cmd_and_prompt
# ---------------------------------------------------------------------------

class TestQwenBuildResume:
    def test_resume_uses_stdin(self):
        r = QwenRunner()
        cmd, prompt = r._build_resume_cmd_and_prompt("sess-456")
        assert "--resume" in cmd
        assert "sess-456" in cmd
        assert RESUME_PROMPT not in cmd  # not appended to cmd
        assert prompt == RESUME_PROMPT  # passed via stdin


# ---------------------------------------------------------------------------
# _run_with_soft_timeout (via QwenRunner)
# ---------------------------------------------------------------------------

class TestRunWithSoftTimeout:
    def test_completes_within_timeout(self):
        """When the session finishes before soft timeout, returns normally."""
        r = QwenRunner(soft_timeout=300)
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        lines = []
        with patch.object(r, '_run_session', return_value=expected) as mock_run:
            result = r._run_with_soft_timeout(
                ["qwen", "-y"], "", "/tmp", lines.append, "sess-1"
            )
        assert result.returncode == 0
        mock_run.assert_called_once_with(["qwen", "-y"], "", "/tmp", lines.append, timeout=300, abort_event=None)

    def test_soft_timeout_triggers_resume(self):
        """When soft timeout fires, kills and resumes."""
        r = QwenRunner(soft_timeout=10)
        resume_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="resumed", stderr="")
        lines = []

        def side_effect(cmd, prompt, cwd, on_line, timeout=None, abort_event=None):
            if timeout == 10:
                raise subprocess.TimeoutExpired(cmd, 10)
            return resume_result

        with patch.object(r, '_run_session', side_effect=side_effect), \
             patch.object(r, '_build_resume_cmd_and_prompt', return_value=(["qwen", "--resume", "s1"], "")):
            result = r._run_with_soft_timeout(
                ["qwen", "-y"], "", "/tmp", lines.append, "s1"
            )

        assert result.returncode == 0
        assert any("[soft-timeout]" in l for l in lines)

    def test_resume_also_times_out(self):
        """When both initial and resume sessions time out, returns error."""
        r = QwenRunner(soft_timeout=10)
        lines = []

        def side_effect(cmd, prompt, cwd, on_line, timeout=None, abort_event=None):
            raise subprocess.TimeoutExpired(cmd, timeout or 0)

        with patch.object(r, '_run_session', side_effect=side_effect), \
             patch.object(r, '_build_resume_cmd_and_prompt', return_value=(["qwen", "--resume", "s1"], "")):
            result = r._run_with_soft_timeout(
                ["qwen", "-y"], "", "/tmp", lines.append, "s1"
            )

        assert result.returncode == 1
        assert any("hard limit" in l for l in lines)

    def test_hard_timeout_uses_caller_timeout(self):
        """When caller provides timeout, it's used as resume hard timeout."""
        r = QwenRunner(soft_timeout=10)
        resume_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        calls = []
        def side_effect(cmd, prompt, cwd, on_line, timeout=None, abort_event=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, 10)
            return resume_result

        with patch.object(r, '_run_session', side_effect=side_effect), \
             patch.object(r, '_build_resume_cmd_and_prompt', return_value=(["qwen", "--resume", "s1"], "")):
            r._run_with_soft_timeout(
                ["qwen", "-y"], "", "/tmp", lambda l: None, "s1", timeout=60
            )

        assert calls == [10, 60]

    def test_default_hard_timeout(self):
        """When caller doesn't provide timeout, RESUME_HARD_TIMEOUT is used."""
        r = QwenRunner(soft_timeout=10)
        resume_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        calls = []
        def side_effect(cmd, prompt, cwd, on_line, timeout=None, abort_event=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, 10)
            return resume_result

        with patch.object(r, '_run_session', side_effect=side_effect), \
             patch.object(r, '_build_resume_cmd_and_prompt', return_value=(["qwen", "--resume", "s1"], "")):
            r._run_with_soft_timeout(
                ["qwen", "-y"], "", "/tmp", lambda l: None, "s1"
            )

        assert calls == [10, SessionResumableRunner.RESUME_HARD_TIMEOUT]


# ---------------------------------------------------------------------------
# QwenRunner.run integration
# ---------------------------------------------------------------------------

class TestQwenRunnerRun:
    def test_soft_timeout_enabled_uses_session_id(self):
        r = QwenRunner(soft_timeout=60)
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch.object(r, '_run_with_soft_timeout', return_value=expected) as mock_st:
            r.run("/tmp", "hello", on_line=lambda l: None)

        mock_st.assert_called_once()
        session_id = mock_st.call_args[0][4]
        uuid.UUID(session_id)  # raises if invalid

    def test_soft_timeout_disabled(self):
        r = QwenRunner(soft_timeout=None)
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch.object(r, '_run_streaming_json', return_value=expected) as mock_json, \
             patch.object(r, '_run_with_soft_timeout') as mock_st:
            r.run("/tmp", "hello", on_line=lambda l: None)

        mock_st.assert_not_called()
        mock_json.assert_called_once()

    def test_prompt_is_passed_via_stdin(self):
        r = QwenRunner(soft_timeout=None)
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with patch.object(r, '_run_streaming_json', return_value=expected) as mock_json:
            r.run("/tmp", "my prompt", on_line=lambda l: None)

        cmd_arg = mock_json.call_args[0][0]
        assert "my prompt" not in cmd_arg  # not a positional arg
        assert mock_json.call_args[1].get("prompt") == "my prompt"  # passed via stdin

    def test_no_on_line_uses_subprocess_run(self):
        r = QwenRunner(soft_timeout=60)
        expected = subprocess.CompletedProcess(args=["qwen"], returncode=0, stdout='{"type":"result","result":"ok"}', stderr="")

        with patch('subprocess.run', return_value=expected):
            result = r.run("/tmp", "hello")

        assert result.returncode == 0


# ---------------------------------------------------------------------------
# QwenRunner._parse_stream_line
# ---------------------------------------------------------------------------

class TestQwenParseStreamLine:
    def test_empty_line(self):
        assert QwenRunner._parse_stream_line("") is None

    def test_non_json(self):
        assert QwenRunner._parse_stream_line("some text") is None

    def test_invalid_json(self):
        assert QwenRunner._parse_stream_line("{invalid}") is None

    def test_assistant_text(self):
        import json
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello world"}]}
        })
        assert QwenRunner._parse_stream_line(line) == "Hello world"

    def test_assistant_tool_use(self):
        import json
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "read_file", "input": {"file_path": "/foo.py"}}]}
        })
        result = QwenRunner._parse_stream_line(line)
        assert "[tool] read_file: /foo.py" in result

    def test_result_type(self):
        import json
        line = json.dumps({"type": "result", "result": "Done!"})
        assert QwenRunner._parse_stream_line(line) == "Done!"

    def test_system_type_suppressed(self):
        import json
        line = json.dumps({"type": "system", "message": "init"})
        assert QwenRunner._parse_stream_line(line) is None

    def test_user_tool_result(self):
        import json
        line = json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}]}
        })
        result = QwenRunner._parse_stream_line(line)
        assert "[result] file contents" in result


# ---------------------------------------------------------------------------
# run_ai_command integration (executor.py)
# ---------------------------------------------------------------------------

class TestRunAiCommand:
    def test_delegates_to_runner(self):
        from workflow_lib.executor import run_ai_command
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={}):
            mock_runner = MagicMock()
            mock_runner.run.return_value = expected
            mock_make.return_value = mock_runner

            rc, _ = run_ai_command("prompt", "/tmp", backend="gemini")

        assert rc == 0
        mock_make.assert_called_once()
        mock_runner.run.assert_called_once()

    def test_passes_soft_timeout_from_config(self):
        from workflow_lib.executor import run_ai_command
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={"soft_timeout": 300}):
            mock_runner = MagicMock()
            mock_runner.run.return_value = expected
            mock_make.return_value = mock_runner

            run_ai_command("prompt", "/tmp", backend="qwen")

        mock_make.assert_called_once_with("qwen", model=None, soft_timeout=300, user=None)

    def test_handles_timeout_expired(self):
        from workflow_lib.executor import run_ai_command

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={}):
            mock_runner = MagicMock()
            mock_runner.run.side_effect = subprocess.TimeoutExpired(["cmd"], 10)
            mock_make.return_value = mock_runner

            rc, _ = run_ai_command("prompt", "/tmp")

        assert rc == 1

    def test_handles_file_not_found(self):
        from workflow_lib.executor import run_ai_command

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={}):
            mock_runner = MagicMock()
            mock_runner.run.side_effect = FileNotFoundError("gemini not found")
            mock_make.return_value = mock_runner

            rc, _ = run_ai_command("prompt", "/tmp")

        assert rc == 1

    def test_prefix_output(self):
        """When no on_line is given, output_line wrapper prints with prefix."""
        from workflow_lib.executor import run_ai_command
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={}):
            mock_runner = MagicMock()
            mock_runner.run.return_value = expected
            mock_make.return_value = mock_runner

            run_ai_command("prompt", "/tmp", prefix="[task] ")

        # Verify an on_line callback was passed (wrapping the prefix)
        run_call = mock_runner.run.call_args
        assert run_call.kwargs.get("on_line") is not None

    def test_stderr_logged_on_failure(self):
        """When the agent process fails, stderr lines are forwarded via on_line.

        With streaming stderr, the runner calls on_line for each stderr line
        as it arrives; the executor sees it regardless of exit code.
        """
        from workflow_lib.executor import run_ai_command
        collected: list = []

        def fake_run(cwd, prompt, image_paths=None, on_line=None, timeout=None, abort_event=None):
            if on_line:
                on_line("[stderr] PermissionError: cannot write to workspace")
                on_line("[stderr] Details: disk full")
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="",
                stderr="PermissionError: cannot write to workspace\nDetails: disk full",
            )

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={}):
            mock_runner = MagicMock()
            mock_runner.run.side_effect = fake_run
            mock_make.return_value = mock_runner

            rc, _ = run_ai_command("prompt", "/tmp", on_line=collected.append)

        assert rc == 1
        assert any("PermissionError" in line for line in collected)
        assert any("Details:" in line for line in collected)

    def test_stderr_logged_even_on_success(self):
        """stderr lines are forwarded via on_line even when the agent succeeds.

        With streaming stderr, the runner calls on_line for each stderr line
        as it arrives; the executor sees it in real time regardless of exit code.
        """
        from workflow_lib.executor import run_ai_command
        collected: list = []

        def fake_run(cwd, prompt, image_paths=None, on_line=None, timeout=None, abort_event=None):
            # Simulate the streaming stderr reader calling on_line.
            if on_line:
                on_line("[stderr] some warning")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="some warning")

        with patch('workflow_lib.executor.make_runner') as mock_make, \
             patch('workflow_lib.config.get_config_defaults', return_value={}):
            mock_runner = MagicMock()
            mock_runner.run.side_effect = fake_run
            mock_make.return_value = mock_runner

            rc, _ = run_ai_command("prompt", "/tmp", on_line=collected.append)

        assert rc == 0
        assert any("[stderr] some warning" in line for line in collected)


# ---------------------------------------------------------------------------
# Module-level parse_stream_json_line
# ---------------------------------------------------------------------------

class TestParseStreamJsonLine:
    def test_empty_line(self):
        assert parse_stream_json_line("") is None

    def test_non_json(self):
        assert parse_stream_json_line("some text") is None

    def test_invalid_json(self):
        assert parse_stream_json_line("{invalid}") is None

    def test_assistant_text(self):
        import json
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello world"}]}
        })
        assert parse_stream_json_line(line) == "Hello world"

    def test_result_type(self):
        import json
        line = json.dumps({"type": "result", "result": "Done!"})
        assert parse_stream_json_line(line) == "Done!"

    def test_qwen_alias_delegates(self):
        """QwenRunner._parse_stream_line delegates to module-level function."""
        import json
        line = json.dumps({"type": "result", "result": "ok"})
        assert QwenRunner._parse_stream_line(line) == parse_stream_json_line(line)


# ---------------------------------------------------------------------------
# ClaudeRunner
# ---------------------------------------------------------------------------

class TestClaudeGetCmd:
    def test_basic(self):
        r = ClaudeRunner()
        cmd = r.get_cmd()
        assert cmd == ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "stream-json", "--include-partial-messages", "--verbose"]

    def test_with_model(self):
        r = ClaudeRunner(model="claude-sonnet-4-6")
        cmd = r.get_cmd()
        assert "--model" in cmd
        assert "claude-sonnet-4-6" in cmd

    def test_with_images(self):
        r = ClaudeRunner()
        cmd = r.get_cmd(image_paths=["/tmp/img.png"])
        assert "--image" in cmd
        assert "/tmp/img.png" in cmd

    def test_stream_json_flag(self):
        r = ClaudeRunner()
        cmd = r.get_cmd()
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "stream-json"


class TestClaudeRunnerRun:
    def test_streaming_uses_json_parser(self):
        r = ClaudeRunner()
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch.object(r, '_run_streaming_json', return_value=expected) as mock_json, \
             patch.object(r, '_run_streaming') as mock_plain:
            r.run("/tmp", "hello", on_line=lambda l: None)

        mock_json.assert_called_once()
        mock_plain.assert_not_called()

    def test_prompt_passed_via_stdin(self):
        r = ClaudeRunner()
        expected = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with patch.object(r, '_run_streaming_json', return_value=expected) as mock_json:
            r.run("/tmp", "my prompt", on_line=lambda l: None)

        cmd_arg = mock_json.call_args[0][0]
        # Prompt should NOT be in the command args (passed via stdin instead)
        assert "my prompt" not in cmd_arg
        # Prompt should be passed as the 'prompt' kwarg
        assert mock_json.call_args[1].get("prompt") == "my prompt"

    def test_no_on_line_parses_json(self):
        """Non-streaming mode still parses JSONL output."""
        import json
        r = ClaudeRunner()
        jsonl_output = json.dumps({"type": "result", "result": "final answer"})
        expected = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=jsonl_output, stderr="")

        with patch('subprocess.run', return_value=expected):
            result = r.run("/tmp", "hello")

        assert result.returncode == 0
        assert "final answer" in result.stdout
