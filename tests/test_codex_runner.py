"""Tests for CodexRunner and parse_codex_json_line."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from workflow_lib.runners import parse_codex_json_line, CodexRunner


class TestParseCodexJsonLine:
    """Tests for parse_codex_json_line function."""

    def test_skips_thread_started(self):
        """Metadata event thread.started should be skipped."""
        line = '{"type":"thread.started","thread_id":"019cfcd0-7b14-7631-b6ee-c911f492a1ca"}'
        assert parse_codex_json_line(line) is None

    def test_skips_turn_started(self):
        """Metadata event turn.started should be skipped."""
        line = '{"type":"turn.started"}'
        assert parse_codex_json_line(line) is None

    def test_skips_turn_completed(self):
        """Metadata event turn.completed should be skipped."""
        line = '{"type":"turn.completed","usage":{"input_tokens":100}}'
        assert parse_codex_json_line(line) is None

    def test_skips_item_started(self):
        """Metadata event item.started should be skipped."""
        line = '{"type":"item.started","item":{"id":"item_1"}}'
        assert parse_codex_json_line(line) is None

    def test_parses_agent_message(self):
        """agent_message items should return the text content."""
        line = '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Listing files."}}'
        assert parse_codex_json_line(line) == "Listing files."

    def test_parses_agent_message_strips_whitespace(self):
        """agent_message text should be stripped."""
        line = '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"  Hello World  "}}'
        assert parse_codex_json_line(line) == "Hello World"

    def test_skips_empty_agent_message(self):
        """Empty agent_message text should be skipped."""
        line = '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":""}}'
        assert parse_codex_json_line(line) is None

    def test_parses_command_execution_with_output(self):
        """command_execution with output should return command and result."""
        line = '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"ls","aggregated_output":"file1.txt\\nfile2.txt","exit_code":0,"status":"completed"}}'
        result = parse_codex_json_line(line)
        assert "[command] ls" in result
        assert "[result] file1.txt" in result

    def test_parses_command_execution_completed_no_output(self):
        """command_execution with no output should return ok status."""
        line = '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"mkdir test","aggregated_output":"","exit_code":0,"status":"completed"}}'
        result = parse_codex_json_line(line)
        assert "[command] mkdir test" in result
        assert "[result] ok (exit_code=0)" in result

    def test_parses_command_execution_failed(self):
        """command_execution with failed status should return error."""
        line = '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"invalid_cmd","aggregated_output":"","exit_code":127,"status":"failed"}}'
        result = parse_codex_json_line(line)
        assert "[command] invalid_cmd" in result
        assert "[result] error (exit_code=127)" in result

    def test_returns_none_for_invalid_json(self):
        """Invalid JSON should return None."""
        assert parse_codex_json_line("not json") is None
        assert parse_codex_json_line("{invalid}") is None

    def test_returns_none_for_empty_line(self):
        """Empty lines should return None."""
        assert parse_codex_json_line("") is None
        assert parse_codex_json_line("   ") is None

    def test_returns_none_for_non_dict_json(self):
        """Non-dict JSON should return None."""
        assert parse_codex_json_line("[]") is None
        assert parse_codex_json_line('"string"') is None

    def test_returns_none_for_unknown_type(self):
        """Unknown message types should return None."""
        line = '{"type":"unknown_type","data":{}}'
        assert parse_codex_json_line(line) is None


class TestCodexRunner:
    """Tests for CodexRunner class."""

    def test_get_cmd_includes_json_flag(self):
        """get_cmd should include --json flag."""
        runner = CodexRunner()
        cmd = runner.get_cmd()
        assert "--json" in cmd
        assert "codex" in cmd
        assert "exec" in cmd
        assert "--full-auto" in cmd

    def test_get_cmd_includes_model(self):
        """get_cmd should include model if specified."""
        runner = CodexRunner(model="gpt-5.4")
        cmd = runner.get_cmd()
        assert "-m" in cmd
        assert "gpt-5.4" in cmd

    def test_get_cmd_includes_images(self):
        """get_cmd should include image paths if specified."""
        runner = CodexRunner()
        cmd = runner.get_cmd(image_paths=["/path/to/img1.png", "/path/to/img2.png"])
        assert "-i" in cmd
        assert "/path/to/img1.png" in cmd
        assert "/path/to/img2.png" in cmd

    def test_get_cmd_sandbox_flag(self):
        """get_cmd should include sandbox flag."""
        runner = CodexRunner()
        cmd = runner.get_cmd()
        assert "--sandbox=danger-full-access" in cmd
