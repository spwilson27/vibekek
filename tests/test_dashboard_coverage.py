"""Tests for dashboard.py to boost coverage."""
import io
import threading
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from workflow_lib.dashboard import (
    Dashboard,
    NullDashboard,
    _DashboardStream,
    _now,
    _now_short,
    _STATUS_STYLE,
)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

class TestTimeHelpers:
    def test_now_returns_string(self):
        result = _now()
        assert isinstance(result, str)
        assert len(result) > 10

    def test_now_short_returns_string(self):
        result = _now_short()
        assert isinstance(result, str)
        assert ":" in result


# ---------------------------------------------------------------------------
# _DashboardStream
# ---------------------------------------------------------------------------

class TestDashboardStream:
    def _make_stream(self):
        dashboard = MagicMock()
        original = io.StringIO()
        return _DashboardStream(dashboard, original), dashboard, original

    def test_write_complete_line(self):
        stream, dash, _ = self._make_stream()
        stream.write("hello\n")
        dash.log.assert_called_once_with("hello")

    def test_write_partial_then_complete(self):
        stream, dash, _ = self._make_stream()
        stream.write("hel")
        dash.log.assert_not_called()
        stream.write("lo\n")
        dash.log.assert_called_once_with("hello")

    def test_write_multiple_lines(self):
        stream, dash, _ = self._make_stream()
        stream.write("a\nb\nc\n")
        assert dash.log.call_count == 3

    def test_write_returns_length(self):
        stream, dash, _ = self._make_stream()
        assert stream.write("hello\n") == 6

    def test_write_skips_blank_lines(self):
        stream, dash, _ = self._make_stream()
        stream.write("\n\n")
        dash.log.assert_not_called()

    def test_flush_with_buffered_content(self):
        stream, dash, _ = self._make_stream()
        stream.write("partial")
        dash.log.assert_not_called()
        stream.flush()
        dash.log.assert_called_once_with("partial")

    def test_flush_empty_buffer(self):
        stream, dash, _ = self._make_stream()
        stream.flush()
        dash.log.assert_not_called()

    def test_fileno(self):
        dash = MagicMock()
        original = MagicMock()
        original.fileno.return_value = 1
        stream = _DashboardStream(dash, original)
        assert stream.fileno() == 1

    def test_isatty(self):
        stream, _, _ = self._make_stream()
        assert stream.isatty() is False

    def test_getattr_delegates(self):
        dash = MagicMock()
        original = MagicMock()
        original.encoding = "utf-8"
        stream = _DashboardStream(dash, original)
        assert stream.encoding == "utf-8"


# ---------------------------------------------------------------------------
# NullDashboard
# ---------------------------------------------------------------------------

class TestNullDashboard:
    def test_context_manager(self):
        nd = NullDashboard()
        with nd as d:
            assert d is nd

    def test_log_writes_to_stream(self):
        buf = io.StringIO()
        nd = NullDashboard(stream=buf)
        nd.log("hello world")
        output = buf.getvalue()
        assert "hello world" in output

    def test_log_writes_to_log_file(self):
        buf = io.StringIO()
        log_file = io.StringIO()
        nd = NullDashboard(log_file=log_file, stream=buf)
        nd.log("test message")
        assert "test message" in log_file.getvalue()

    def test_log_skips_blank_lines(self):
        buf = io.StringIO()
        nd = NullDashboard(stream=buf)
        nd.log("\n\n")
        assert buf.getvalue() == ""

    def test_log_handles_log_file_error(self):
        buf = io.StringIO()
        log_file = MagicMock()
        log_file.write.side_effect = IOError("disk full")
        nd = NullDashboard(log_file=log_file, stream=buf)
        nd.log("test")  # should not raise

    def test_set_agent_noop(self):
        nd = NullDashboard()
        nd.set_agent("t1", "stage", "running")  # no error

    def test_update_last_line_noop(self):
        nd = NullDashboard()
        nd.update_last_line("t1", "line")

    def test_remove_agent_noop(self):
        nd = NullDashboard()
        nd.remove_agent("t1")

    def test_prompt_input(self):
        buf = io.StringIO()
        nd = NullDashboard(stream=buf)
        with patch("builtins.input", return_value="yes"):
            result = nd.prompt_input("Continue?")
        assert result == "yes"
        assert "INPUT REQUIRED" in buf.getvalue()


# ---------------------------------------------------------------------------
# Dashboard (unit tests without Live)
# ---------------------------------------------------------------------------

class TestDashboard:
    def test_init(self):
        d = Dashboard()
        assert d._live is None
        assert len(d._agents) == 0

    def test_log_appends_to_ring(self):
        d = Dashboard()
        d.log("test line")
        assert len(d._ring) == 1
        assert "test line" in d._ring[0]

    def test_log_multiline(self):
        d = Dashboard()
        d.log("line1\nline2\n\nline3")
        assert len(d._ring) == 3

    def test_log_writes_to_file(self):
        log_file = io.StringIO()
        d = Dashboard(log_file=log_file)
        d.log("file test")
        assert "file test" in log_file.getvalue()

    def test_log_handles_file_error(self):
        log_file = MagicMock()
        log_file.write.side_effect = IOError()
        d = Dashboard(log_file=log_file)
        d.log("test")  # should not raise

    def test_set_agent_new(self):
        d = Dashboard()
        d.set_agent("t1", "implement", "running", "doing stuff")
        assert "t1" in d._agents
        assert d._agents["t1"][0] == "implement"
        assert d._agents["t1"][1] == "running"

    def test_set_agent_update_preserves_lines(self):
        d = Dashboard()
        d.set_agent("t1", "implement", "running", "line1")
        d.set_agent("t1", "implement", "done", "line2")
        assert d._agents["t1"][1] == "done"
        assert len(d._agents["t1"][2]) == 2

    def test_set_agent_empty_last_line(self):
        d = Dashboard()
        d.set_agent("t1", "implement", "running")
        assert len(d._agents["t1"][2]) == 0

    def test_update_last_line(self):
        d = Dashboard()
        d.set_agent("t1", "implement", "running")
        d.update_last_line("t1", "new output")
        assert len(d._agents["t1"][2]) == 1

    def test_update_last_line_empty(self):
        d = Dashboard()
        d.set_agent("t1", "implement", "running")
        d.update_last_line("t1", "")
        assert len(d._agents["t1"][2]) == 0

    def test_update_last_line_unknown_task(self):
        d = Dashboard()
        d.update_last_line("unknown", "output")  # should not raise

    def test_remove_agent(self):
        d = Dashboard()
        d.set_agent("t1", "implement", "running")
        d.remove_agent("t1")
        assert "t1" not in d._agents

    def test_remove_agent_unknown(self):
        d = Dashboard()
        d.remove_agent("unknown")  # should not raise

    def test_refresh_no_live(self):
        d = Dashboard()
        d._refresh()  # should not raise when _live is None

    def test_context_manager(self):
        d = Dashboard()
        with patch.object(d, '_console') as mock_console:
            mock_console.size = MagicMock(height=40, width=80)
            # We can't easily test __enter__/__exit__ without a real terminal
            # but we can test the exit path
            d._live = MagicMock()
            d.__exit__(None, None, None)
            assert d._live is None

    def test_render_no_agents(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        result = d._render()
        assert result is not None

    def test_render_with_agents(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        d.set_agent("t1", "implement", "running", "doing work")
        d.set_agent("t2", "review", "queued", "waiting")
        result = d._render()
        assert result is not None

    def test_render_with_log_lines(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        for i in range(10):
            d.log(f"log line {i}")
        result = d._render()
        assert result is not None

    def test_render_elapsed_formats(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        # Test hour format
        d._start_time = datetime.now(tz=d._start_time.tzinfo) - timedelta(hours=2)
        result = d._render()
        assert result is not None

    def test_render_agent_elapsed_formats(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        # Add agent with long elapsed time
        d.set_agent("t1", "implement", "running", "work")
        # Manually set start time to 2 hours ago
        stage, status, lines, _ = d._agents["t1"]
        old_start = datetime.now(tz=d._start_time.tzinfo) - timedelta(hours=2)
        d._agents["t1"] = (stage, status, lines, old_start)
        result = d._render()
        assert result is not None

    def test_render_failed_status(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        d.set_agent("t1", "implement", "failed", "error occurred")
        result = d._render()
        assert result is not None

    def test_render_no_output_lines(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        d.set_agent("t1", "implement", "running")
        result = d._render()
        assert result is not None

    def test_render_many_agents_surplus_redistribution(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        # Add several agents so surplus redistribution logic is triggered
        for i in range(5):
            d.set_agent(f"t{i}", "implement", "running", f"line {i}")
        result = d._render()
        assert result is not None

    def test_render_minute_elapsed(self):
        d = Dashboard()
        d._console = MagicMock()
        d._console.size = MagicMock(height=40, width=80)
        d._start_time = datetime.now(tz=d._start_time.tzinfo) - timedelta(minutes=5)
        result = d._render()
        assert result is not None


# ---------------------------------------------------------------------------
# prompt_registry coverage
# ---------------------------------------------------------------------------

class TestPromptRegistry:
    def test_validate_missing_prompts(self, tmp_path):
        from workflow_lib.prompt_registry import validate_all_prompts_exist
        missing = validate_all_prompts_exist(str(tmp_path))
        assert len(missing) > 0

    def test_get_required_placeholders_unknown(self):
        from workflow_lib.prompt_registry import get_required_placeholders
        result = get_required_placeholders("nonexistent_prompt.md")
        assert result == set()
