"""Tests for dashboard active-agents panel line wrapping."""

import io
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".tools"))

from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.text import Text

from workflow_lib.dashboard import Dashboard

_PST = ZoneInfo("America/Los_Angeles")

LONG_TASK_ID = "phase_1/02_definition_of_done_guidelines/05_enforce_static_architectural_violations.md"


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _make_dashboard(width: int = 120, height: int = 40) -> Dashboard:
    """Create a Dashboard with a fixed-size console for deterministic rendering."""
    d = Dashboard(log_file=None)
    d._console = Console(
        file=io.StringIO(),
        width=width,
        height=height,
        highlight=False,
        force_terminal=True,
    )
    return d


def _add_agent(d: Dashboard, task_id: str = LONG_TASK_ID, stage: str = "Implementation"):
    """Add an agent and pin its start time to now for stable rendering."""
    now = datetime.now(tz=_PST)
    d.set_agent(task_id, stage, "running", "doing stuff")
    with d._lock:
        s, st, lines, _ = d._agents[task_id]
        d._agents[task_id] = (s, st, lines, now)


def _render_to_str(d: Dashboard) -> str:
    """Render dashboard to a string via a width-constrained console."""
    group = d._render()
    buf = io.StringIO()
    Console(
        file=buf,
        width=d._console.size.width,
        highlight=False,
        force_terminal=True,
    ).print(group)
    return buf.getvalue()


class TestHeaderColumnWrapping:
    """The agent header (task_id + stage) must fold instead of truncating with ellipsis."""

    def test_long_task_id_is_not_truncated_with_ellipsis(self):
        """The key behavioral change: long headers are folded, not ellipsis-truncated."""
        d = _make_dashboard(width=80)
        _add_agent(d)
        output = _render_to_str(d)
        clean = _strip_ansi(output)
        # Ellipsis truncation must NOT be present
        assert "\u2026" not in clean, (
            "Header was ellipsis-truncated instead of being folded/wrapped"
        )
        # The full task_id must appear (possibly split across lines)
        # Check start and end fragments are both present
        assert "phase_1/02_definition" in clean
        assert "architectural_violations.md" in clean

    def test_full_task_id_preserved_at_narrow_width(self):
        """Even at 60 columns the full task_id must be visible (folded, not cut)."""
        d = _make_dashboard(width=60, height=30)
        _add_agent(d)
        output = _render_to_str(d)
        clean = _strip_ansi(output)
        assert "\u2026" not in clean, "Ellipsis found — content was truncated"
        # Full task_id is present when lines are joined (fold splits mid-word)
        joined = clean.replace("\n", "").replace(" ", "")
        assert "architectural_violations.md" in joined

    def test_no_line_exceeds_terminal_width(self):
        d = _make_dashboard(width=100)
        _add_agent(d)
        output = _render_to_str(d)
        for i, line in enumerate(output.splitlines()):
            clean = _strip_ansi(line)
            assert len(clean) <= 100, (
                f"Line {i} is {len(clean)} chars (max 100): {clean!r}"
            )

    def test_narrow_terminal_no_line_overflow(self):
        d = _make_dashboard(width=40, height=30)
        _add_agent(d)
        output = _render_to_str(d)
        for i, line in enumerate(output.splitlines()):
            clean = _strip_ansi(line)
            assert len(clean) <= 40, (
                f"Line {i} is {len(clean)} chars (max 40): {clean!r}"
            )


class TestAgentOutputLineWrapping:
    """Agent output lines must be pre-wrapped via textwrap before Rich renders them.

    Rich's Text objects wrap correctly in Console.print() but NOT reliably
    inside Live(screen=True).  The fix is to pre-wrap lines with textwrap.wrap
    so the Text content never exceeds content_width.  These tests inspect the
    raw Text objects from _render() to verify pre-wrapping.
    """

    LONG_OUTPUT = (
        "I will read do.py to understand its current implementation "
        "and identify where to add the presubmit command "
        "so that we can run all the checks before submitting."
    )  # 159 chars — exceeds old 120-char ingestion limit, tests wrapping not truncation

    def _get_agent_text_objects(self, d):
        """Extract Text renderables from the dashboard's _render() Group."""
        from rich.console import Group
        group = d._render()
        # Group stores renderables in ._renderables
        texts = []
        for r in group._renderables:
            if isinstance(r, Text):
                texts.append(r)
        return texts

    def test_agent_output_text_is_pre_wrapped(self):
        """Text objects for agent output must have no line exceeding content_width."""
        width = 60
        d = _make_dashboard(width=width, height=40)
        d.set_agent("p1/01/task.md", "Impl", "running", self.LONG_OUTPUT)
        now = datetime.now(tz=_PST)
        with d._lock:
            s, st, lines, _ = d._agents["p1/01/task.md"]
            d._agents["p1/01/task.md"] = (s, st, lines, now)
        texts = self._get_agent_text_objects(d)
        # Find the Text containing our agent output
        agent_texts = [t for t in texts if "read do.py" in t.plain]
        assert agent_texts, f"Agent output Text not found among: {[t.plain[:40] for t in texts]}"
        for t in agent_texts:
            for line in t.plain.splitlines():
                assert len(line) <= width, (
                    f"Pre-wrap failed: Text line is {len(line)} chars (max {width}): {line!r}"
                )

    def test_agent_output_preserves_full_content(self):
        """Pre-wrapped Text must preserve the full agent output, not truncate it."""
        width = 60
        d = _make_dashboard(width=width, height=40)
        d.set_agent("p1/01/task.md", "Impl", "running", self.LONG_OUTPUT)
        now = datetime.now(tz=_PST)
        with d._lock:
            s, st, lines, _ = d._agents["p1/01/task.md"]
            d._agents["p1/01/task.md"] = (s, st, lines, now)
        texts = self._get_agent_text_objects(d)
        agent_texts = [t for t in texts if "read do.py" in t.plain]
        assert agent_texts
        joined = " ".join(agent_texts[0].plain.split())
        assert "before submitting" in joined, (
            f"Pre-wrapped text lost content: {joined}"
        )

    def test_agent_output_no_ellipsis(self):
        """Pre-wrapped agent output must NOT contain truncation ellipsis."""
        width = 60
        d = _make_dashboard(width=width, height=40)
        d.set_agent("p1/01/task.md", "Impl", "running", self.LONG_OUTPUT)
        now = datetime.now(tz=_PST)
        with d._lock:
            s, st, lines, _ = d._agents["p1/01/task.md"]
            d._agents["p1/01/task.md"] = (s, st, lines, now)
        texts = self._get_agent_text_objects(d)
        agent_texts = [t for t in texts if "read do.py" in t.plain]
        assert agent_texts
        assert "…" not in agent_texts[0].plain, (
            f"Agent output was truncated with '…' instead of wrapped"
        )

    def test_rendered_output_fits_within_width(self):
        """End-to-end: rendered output lines must not exceed terminal width."""
        width = 70
        d = _make_dashboard(width=width, height=50)
        d.set_agent("p1/01/task.md", "Impl", "running", "first short line")
        d.update_last_line("p1/01/task.md", self.LONG_OUTPUT)
        d.update_last_line("p1/01/task.md", "Another very long line that should also be wrapped " * 3)
        now = datetime.now(tz=_PST)
        with d._lock:
            s, st, lines, _ = d._agents["p1/01/task.md"]
            d._agents["p1/01/task.md"] = (s, st, lines, now)
        output = _render_to_str(d)
        for i, line in enumerate(output.splitlines()):
            clean = _strip_ansi(line)
            assert len(clean) <= width, (
                f"Line {i} is {len(clean)} chars (max {width}): {clean!r}"
            )


class TestLogLineWrapping:
    """Log lines must be pre-wrapped via textwrap before Rich renders them."""

    def _get_log_text(self, d):
        """Extract the log Text object from _render() Group."""
        from rich.console import Group
        group = d._render()
        # The log text is the large Text object with appended lines
        for r in group._renderables:
            if isinstance(r, Text) and len(r.plain) > 10:
                return r
        return None

    def test_log_text_is_pre_wrapped(self):
        """Log Text object must have no line exceeding content_width."""
        width = 60
        d = _make_dashboard(width=width, height=40)
        long_msg = "Processing task " + "word " * 30 + "END_MARKER"
        d.log(long_msg)
        log_text = self._get_log_text(d)
        assert log_text is not None, "Log Text object not found"
        for line in log_text.plain.splitlines():
            if not line.strip():
                continue
            assert len(line) <= width, (
                f"Pre-wrap failed: log line is {len(line)} chars (max {width}): {line!r}"
            )

    def test_log_preserves_full_content(self):
        """Pre-wrapped log Text must preserve the full message."""
        width = 60
        d = _make_dashboard(width=width, height=40)
        long_msg = "Processing task alpha " + "delta " * 20 + "END_MARKER"
        d.log(long_msg)
        log_text = self._get_log_text(d)
        assert log_text is not None
        joined = " ".join(log_text.plain.split())
        assert "END_MARKER" in joined, (
            f"Pre-wrapped log lost content: {joined[-200:]}"
        )

    def test_log_no_ellipsis(self):
        """Pre-wrapped log lines must NOT contain truncation ellipsis."""
        width = 60
        d = _make_dashboard(width=width, height=40)
        long_msg = "Processing task " + "word " * 40
        d.log(long_msg)
        log_text = self._get_log_text(d)
        assert log_text is not None
        log_lines = [l for l in log_text.plain.splitlines() if "Processing" in l]
        assert log_lines
        assert not any("…" in l for l in log_lines), (
            f"Log was truncated with '…' instead of wrapped"
        )
