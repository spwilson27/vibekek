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
        s, st, lines, _, an = d._agents[task_id]
        d._agents[task_id] = (s, st, lines, now, an)


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
        "I will read harness.py to understand its current implementation "
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
            s, st, lines, _, an = d._agents["p1/01/task.md"]
            d._agents["p1/01/task.md"] = (s, st, lines, now, an)
        texts = self._get_agent_text_objects(d)
        # Find the Text containing our agent output
        agent_texts = [t for t in texts if "read harness.py" in t.plain]
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
            s, st, lines, _, an = d._agents["p1/01/task.md"]
            d._agents["p1/01/task.md"] = (s, st, lines, now, an)
        texts = self._get_agent_text_objects(d)
        agent_texts = [t for t in texts if "read harness.py" in t.plain]
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
            s, st, lines, _, an = d._agents["p1/01/task.md"]
            d._agents["p1/01/task.md"] = (s, st, lines, now, an)
        texts = self._get_agent_text_objects(d)
        agent_texts = [t for t in texts if "read harness.py" in t.plain]
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
            s, st, lines, _, an = d._agents["p1/01/task.md"]
            d._agents["p1/01/task.md"] = (s, st, lines, now, an)
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


class TestShutdownBannerPlacement:
    """Regression tests: SHUTTING DOWN banner must be rendered before log/agents.

    Before the fix the render order was Group(*log_parts, *parts, *shutdown_parts),
    placing the banner at the end of the renderable list.  When agents or log
    content exceeded the terminal height the banner was pushed off the bottom of
    the screen.

    The fix changed the order to Group(*shutdown_parts, *log_parts, *parts) so
    the banner is always the first thing rendered and can never be displaced.

    test_shutdown_banner_is_first_renderable fails without the fix because the
    first renderable is the log-header Rule, not the shutdown Rule.

    test_shutdown_banner_renders_before_log fails without the fix because the
    log text appears before "SHUTTING DOWN" in the output string.
    """

    def test_shutdown_banner_is_first_renderable(self):
        """The SHUTTING DOWN Rule must be the very first item in the render group."""
        from rich.rule import Rule
        d = _make_dashboard()
        d.set_shutting_down()
        group = d._render()
        first = group._renderables[0]
        assert isinstance(first, Rule), (
            f"Expected Rule as first renderable (shutdown banner), got "
            f"{type(first).__name__}. "
            "The banner must come before log and agents so it cannot be pushed "
            "off the bottom of the screen by overflowing content."
        )
        # Confirm it is the shutdown rule and not the log-header rule
        assert "SHUTTING DOWN" in str(first.title), (
            f"First renderable is a Rule but not the shutdown banner: {first.title!r}"
        )

    def test_shutdown_banner_renders_before_log(self):
        """SHUTTING DOWN text appears before any log content in the rendered output."""
        d = _make_dashboard(width=120, height=40)
        d.log("sentinel_log_message")
        d.set_shutting_down()
        output = _render_to_str(d)
        clean = _strip_ansi(output)
        lines = clean.splitlines()

        shutdown_line = next((i for i, l in enumerate(lines) if "SHUTTING DOWN" in l), None)
        log_line = next((i for i, l in enumerate(lines) if "sentinel_log_message" in l), None)

        assert shutdown_line is not None, "SHUTTING DOWN not found in rendered output"
        assert log_line is not None, "sentinel_log_message not found in rendered output"
        assert shutdown_line < log_line, (
            f"SHUTTING DOWN banner (line {shutdown_line}) appears AFTER log content "
            f"(line {log_line}). Banner must come first to avoid being pushed off screen."
        )

    def test_shutdown_banner_visible_when_agents_overflow(self):
        """Banner stays in the first two lines even when many agents fill the terminal."""
        d = _make_dashboard(width=80, height=20)
        for i in range(6):
            d.set_agent(
                f"phase_1/epic_{i}/task_{i:02d}_long_name.md",
                "Implementation", "running",
                f"agent {i} doing work processing files and writing code",
            )
        d.set_shutting_down()
        output = _render_to_str(d)
        clean = _strip_ansi(output)
        first_two = "\n".join(clean.splitlines()[:2])
        assert "SHUTTING DOWN" in first_two, (
            "SHUTTING DOWN not found in first 2 rendered lines with many agents. "
            f"First 5 lines:\n" + "\n".join(clean.splitlines()[:5])
        )


# ---------------------------------------------------------------------------
# Agent name column in the active-agents panel
# ---------------------------------------------------------------------------

class TestAgentNameColumn:
    """The agent_name field is stored, preserved, and rendered correctly."""

    # ------------------------------------------------------------------
    # Storage and preservation
    # ------------------------------------------------------------------

    def test_agent_name_stored_on_set(self):
        d = _make_dashboard()
        d.set_agent("t1", "Impl", "running", agent_name="claude-dev")
        assert d._agents["t1"][4] == "claude-dev"

    def test_agent_name_defaults_to_empty_string(self):
        d = _make_dashboard()
        d.set_agent("t1", "Impl", "running")
        assert d._agents["t1"][4] == ""

    def test_empty_agent_name_preserves_existing(self):
        """Calling set_agent with agent_name="" must not overwrite an existing name."""
        d = _make_dashboard()
        d.set_agent("t1", "Impl", "running", agent_name="gemini-flash")
        d.set_agent("t1", "Impl", "running", "some output line")  # no agent_name
        assert d._agents["t1"][4] == "gemini-flash"

    def test_non_empty_agent_name_overwrites_existing(self):
        """A new non-empty name replaces the previous one (agent rotated on quota)."""
        d = _make_dashboard()
        d.set_agent("t1", "Impl", "running", agent_name="agent-a")
        d.set_agent("t1", "Impl", "running", agent_name="agent-b")
        assert d._agents["t1"][4] == "agent-b"

    def test_agent_name_preserved_across_status_updates(self):
        d = _make_dashboard()
        d.set_agent("t1", "Impl", "running", agent_name="my-agent")
        d.set_agent("t1", "Verify", "waiting", "quota exceeded")
        assert d._agents["t1"][4] == "my-agent"

    # ------------------------------------------------------------------
    # Render: agent name appears in output when set
    # ------------------------------------------------------------------

    def test_agent_name_appears_in_rendered_output(self):
        d = _make_dashboard(width=120)
        d.set_agent("phase_1/01_setup.md", "Impl", "running", agent_name="claude-dev")
        output = _strip_ansi(_render_to_str(d))
        assert "claude-dev" in output, (
            f"Agent name 'claude-dev' not found in rendered output:\n{output}"
        )

    def test_agent_name_absent_when_not_set(self):
        d = _make_dashboard(width=120)
        d.set_agent("phase_1/01_setup.md", "Impl", "running")
        # There's no agent name to check — just confirm render doesn't crash
        # and the task ID is present
        output = _strip_ansi(_render_to_str(d))
        assert "phase_1/01_setup.md" in output

    def test_multiple_agents_show_their_respective_names(self):
        d = _make_dashboard(width=160, height=60)
        d.set_agent("phase_1/task_a.md", "Impl", "running", agent_name="claude-dev")
        d.set_agent("phase_1/task_b.md", "Review", "running", agent_name="gemini-flash")
        output = _strip_ansi(_render_to_str(d))
        assert "claude-dev" in output
        assert "gemini-flash" in output

    def test_agent_with_no_name_does_not_show_other_agents_name(self):
        d = _make_dashboard(width=160, height=60)
        d.set_agent("phase_1/named.md", "Impl", "running", agent_name="pool-agent")
        d.set_agent("phase_1/unnamed.md", "Impl", "running")
        output = _strip_ansi(_render_to_str(d))
        # "pool-agent" must appear exactly once (only for the named task)
        assert output.count("pool-agent") == 1

    # ------------------------------------------------------------------
    # NullDashboard accepts agent_name without error
    # ------------------------------------------------------------------

    def test_null_dashboard_accepts_agent_name(self):
        import io as _io
        from workflow_lib.dashboard import NullDashboard
        nd = NullDashboard(log_file=None, stream=_io.StringIO())
        nd.set_agent("t1", "Impl", "running", agent_name="test-agent")  # must not raise

    # ------------------------------------------------------------------
    # Executor: run_agent calls set_agent with agent_name from pool
    # ------------------------------------------------------------------

    def test_run_agent_sets_agent_name_from_pool(self):
        """When a pool agent is acquired, the dashboard row shows its name."""
        import subprocess as sp
        from unittest.mock import patch, MagicMock, mock_open
        from workflow_lib.executor import run_agent
        from workflow_lib.agent_pool import AgentPoolManager, AgentConfig

        cfg = AgentConfig("my-pool-agent", "gemini", "u", parallel=1, priority=1, quota_time=60)
        pool = AgentPoolManager([cfg])

        mock_runner = MagicMock()
        mock_runner.run.return_value = sp.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")

        set_agent_calls = []
        mock_dashboard = MagicMock()
        mock_dashboard.set_agent.side_effect = lambda *a, **kw: set_agent_calls.append((a, kw))

        with patch("workflow_lib.executor.make_runner", return_value=mock_runner), \
             patch("workflow_lib.executor.get_project_images", return_value=[]), \
             patch("workflow_lib.config.get_config_defaults", return_value={}), \
             patch("builtins.open", mock_open(read_data="prompt {task_name}")):
            run_agent("Implementation", "implement_task.md",
                      {"task_name": "t", "phase_filename": "p"},
                      "/tmp", dashboard=mock_dashboard, task_id="phase_1/t.md",
                      agent_pool=pool)

        # At least one call must supply agent_name="my-pool-agent"
        names_supplied = [
            kw.get("agent_name") or next((v for v in a if isinstance(v, str) and v == "my-pool-agent"), None)
            for a, kw in set_agent_calls
        ]
        assert "my-pool-agent" in names_supplied, (
            f"Expected set_agent to be called with agent_name='my-pool-agent'. "
            f"Calls: {set_agent_calls}"
        )

    def test_agent_name_changes_between_steps_with_different_pool_agents(self):
        """When develop and review steps are assigned to different pool agents,
        the dashboard row must show each agent's name for its respective step."""
        import subprocess as sp
        from unittest.mock import patch, MagicMock, mock_open
        from workflow_lib.executor import run_agent
        from workflow_lib.agent_pool import AgentPoolManager, AgentConfig

        # Two agents, each restricted to a single step.
        dev_cfg = AgentConfig("dev-agent", "claude", "u", parallel=1, priority=1, quota_time=60,
                              steps=["develop"])
        rev_cfg = AgentConfig("rev-agent", "gemini", "u", parallel=1, priority=1, quota_time=60,
                              steps=["review"])
        pool = AgentPoolManager([dev_cfg, rev_cfg])

        mock_runner = MagicMock()
        mock_runner.run.return_value = sp.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        # Use a real Dashboard so set_agent actually updates _agents.
        dashboard = _make_dashboard(width=120)

        common_patches = dict(
            make_runner=patch("workflow_lib.executor.make_runner", return_value=mock_runner),
            images=patch("workflow_lib.executor.get_project_images", return_value=[]),
            cfg=patch("workflow_lib.config.get_config_defaults", return_value={}),
            open=patch("builtins.open", mock_open(read_data="hello {task_name}")),
        )

        task_id = "phase_1/api/01_setup.md"
        ctx = {"task_name": "01_setup", "phase_filename": "phase_1"}

        with common_patches["make_runner"], common_patches["images"], \
             common_patches["cfg"], common_patches["open"]:
            # Step 1: Implementation (→ develop step → dev-agent)
            run_agent("Implementation", "implement_task.md", ctx, "/tmp",
                      dashboard=dashboard, task_id=task_id, agent_pool=pool)
            name_after_develop = dashboard._agents[task_id][4]

            # Step 2: Review (→ review step → rev-agent)
            run_agent("Review", "review_task.md", ctx, "/tmp",
                      dashboard=dashboard, task_id=task_id, agent_pool=pool)
            name_after_review = dashboard._agents[task_id][4]

        assert name_after_develop == "dev-agent", (
            f"After Implementation step, expected agent 'dev-agent', got {name_after_develop!r}"
        )
        assert name_after_review == "rev-agent", (
            f"After Review step, expected agent 'rev-agent', got {name_after_review!r}"
        )
        assert name_after_develop != name_after_review, (
            "Agent name must change between steps when different pool agents are used"
        )
