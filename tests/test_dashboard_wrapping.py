"""Tests for dashboard active-agents panel line wrapping."""

import io
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".tools"))

from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console

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
