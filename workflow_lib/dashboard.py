"""Live terminal dashboard for the parallel implementation workflow.

Displays a two-pane layout using :mod:`rich`:

* **Top pane** — scrolling aggregate log (ring buffer, last 30 lines) from all
  agents and the orchestrator, timestamped in Pacific Time.
* **Bottom pane** — per-agent status table: task name, stage, status indicator,
  and the most recent output line from that agent.

All public methods are thread-safe.  The class also writes every log line to an
optional log file so the full run transcript is preserved on disk.

Typical usage::

    with Dashboard(log_file=open("run.log", "a")) as dash:
        dash.log("Starting workflow...")
        dash.set_agent("phase_1/task.md", "Impl", "running", "Writing tests...")
        dash.remove_agent("phase_1/task.md")
"""

from __future__ import annotations

import io
import time
import threading
import types
from collections import deque
from datetime import datetime
from typing import Deque, Dict, IO, Optional, Tuple, Type
from zoneinfo import ZoneInfo

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

_PST = ZoneInfo("America/Los_Angeles")
_LOG_LINES = 30   # max lines kept in the aggregate log ring buffer
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _now() -> str:
    return datetime.now(tz=_PST).strftime("%Y-%m-%d %H:%M:%S %Z")


def _now_short() -> str:
    return datetime.now(tz=_PST).strftime("%H:%M:%S")


# Status label → (rich style, symbol)
_STATUS_STYLE: Dict[str, Tuple[str, str]] = {
    "queued":   ("dim",           "○"),
    "cloning":  ("cyan",          "⟳"),
    "running":  ("green",         "●"),
    "merging":  ("yellow",        "⟳"),
    "done":     ("bold green",    "✓"),
    "failed":   ("bold red",      "✗"),
    "waiting":  ("bold yellow",   "⏸"),
}


class Dashboard:
    """Live two-pane dashboard for concurrent agent monitoring.

    :param log_file: Optional open file object to mirror all log output to.
        If ``None`` log lines are not persisted to disk.
    :param log_lines: Number of lines to keep in the scrolling log pane.
        Defaults to 30.
    """

    def __init__(
        self,
        log_file: Optional[IO[str]] = None,
        log_lines: int = _LOG_LINES,
    ) -> None:
        self._log_file = log_file
        self._lock = threading.Lock()
        self._ring: Deque[str] = deque(maxlen=log_lines)
        # task_id -> (command, status, lines_deque, start_time)
        # lines_deque holds (timestamp_str, text) pairs for the agent card
        self._agents: Dict[str, Tuple[str, str, Deque[Tuple[str, str]], datetime]] = {}
        self._live: Optional[Live] = None
        self._console = Console(highlight=False)
        self._spinner_idx = 0
        self._last_spinner_time = 0.0

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __rich_console__(self, console: Console, options: "ConsoleOptions") -> "RenderResult":
        """Make Dashboard a live renderable so Rich auto-refreshes us."""
        yield self._render()

    def __enter__(self) -> "Dashboard":
        self._live = Live(
            self,
            console=self._console,
            refresh_per_second=2,
            screen=True,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[types.TracebackType],
    ) -> None:
        if self._live:
            self._live.__exit__(exc_type, exc_val, exc_tb)
            self._live = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(self, message: str) -> None:
        """Append a timestamped line to the log pane and the log file.

        :param message: Plain text message (newlines stripped to one line each).
        """
        for line in message.splitlines():
            line = line.rstrip()
            if not line:
                continue
            entry = f"[{_now()}] {line}"
            with self._lock:
                self._ring.append(entry)
                if self._log_file:
                    try:
                        self._log_file.write(entry + "\n")
                        self._log_file.flush()
                    except Exception:
                        pass
            self._refresh()

    def set_agent(
        self,
        task_id: str,
        stage: str,
        status: str,
        last_line: str = "",
    ) -> None:
        """Upsert an agent row in the status table.

        :param task_id: Fully-qualified task ID, e.g. ``"phase_1/auth.md"``.
        :param stage: Short label for what the agent is doing, e.g. ``"Generate"``.
        :param status: One of ``queued``, ``cloning``, ``running``, ``merging``,
            ``done``, ``failed``.
        :param last_line: Optional initial output line to show in the card.
        """
        with self._lock:
            # Preserve existing log lines and start time when updating
            if task_id in self._agents:
                _, _, lines, started = self._agents[task_id]
            else:
                lines: Deque[Tuple[str, str]] = deque()
                started = datetime.now(tz=_PST)
            short = last_line.strip()[:120] if last_line else ""
            if short:
                lines.append((_now_short(), short))
            self._agents[task_id] = (stage, status, lines, started)
        self._refresh()

    def update_last_line(self, task_id: str, last_line: str) -> None:
        """Append a new output line to an existing agent's card.

        :param task_id: Task ID of the agent to update.
        :param last_line: New output line (appended; older lines scroll off after 4).
        """
        short = last_line.strip()[:120] if last_line else ""
        if not short:
            return
        with self._lock:
            if task_id in self._agents:
                _cmd, _st, lines, _started = self._agents[task_id]
                lines.append((_now_short(), short))
        self._refresh()

    def remove_agent(self, task_id: str) -> None:
        """Remove a task row from the agents table.

        :param task_id: Task ID to remove.
        """
        with self._lock:
            self._agents.pop(task_id, None)
        self._refresh()

    def prompt_input(self, message: str) -> str:
        """Pause the live display, show a prominent prompt, and return user input.

        :param message: The prompt text to display.
        :returns: The user's input string.
        """
        if self._live:
            self._live.stop()
        try:
            self._console.print()
            self._console.print(Rule("[bold yellow]INPUT REQUIRED[/bold yellow]", style="yellow"))
            self._console.print(f"[bold yellow]  {message}[/bold yellow]")
            self._console.print(Rule(style="yellow"))
            response = input("> ")
            self._console.print()
        finally:
            if self._live:
                self._live.start()
        return response

    # ------------------------------------------------------------------
    # Internal rendering
    # ------------------------------------------------------------------

    def _render(self) -> Group:
        """Build the full dashboard as a single Group with dynamic sizing.

        Layout rules:
        - 50/50 split between log and agents panels.
        - If agents need less than their half, log expands into the surplus.
        - Each agent gets an equal share of the agents half.
        - If an agent needs fewer lines than its share, the surplus is
          redistributed evenly among the other agents.
        """
        term_h = self._console.size.height
        # Reserve 2 lines for Live overhead
        usable = max(term_h - 2, 10)
        half = usable // 2

        now = datetime.now(tz=_PST)
        mono = time.monotonic()
        if mono - self._last_spinner_time >= 0.5:
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
            self._last_spinner_time = mono
        spinner = _SPINNER_FRAMES[self._spinner_idx]

        # --- Gather visible agents ---
        with self._lock:
            visible = {
                k: (v[0], v[1], list(v[2]), v[3])
                for k, v in self._agents.items()
                if v[1] in ("running", "failed", "cloning", "merging", "queued", "waiting")
            }

        # --- Compute agent allocations ---
        # Each agent card costs: 1 header + N output lines + 1 separator (except last)
        # Panel border costs 2 lines.
        if visible:
            n = len(visible)
            # Panel border (top + bottom) = 2, separators between agents = n-1
            chrome = 2 + max(n - 1, 0)
            content_budget = max(half - chrome, n)  # at least 1 line per agent
            per_agent = content_budget // n

            # First pass: figure out how many content lines each agent needs
            # (1 for header + output lines, minimum 2: header + at least 1 line)
            agent_data = []
            for task_id in sorted(visible):
                stage, status, output_lines, started = visible[task_id]
                # need = header(1) + output lines (at least 1 for "waiting...")
                need = 1 + max(len(output_lines), 1)
                elapsed = now - started
                agent_data.append((task_id, stage, status, output_lines, need, started, elapsed))

            # Two-pass allocation: give each agent min(need, share), redistribute surplus
            allocs = [min(a[4], per_agent) for a in agent_data]
            surplus = content_budget - sum(allocs)
            # Redistribute surplus to agents that could use more
            if surplus > 0:
                hungry = [i for i, a in enumerate(agent_data) if allocs[i] < a[4]]
                while surplus > 0 and hungry:
                    give = max(surplus // len(hungry), 1)
                    still_hungry = []
                    for i in hungry:
                        can_use = agent_data[i][4] - allocs[i]
                        grant = min(give, can_use, surplus)
                        allocs[i] += grant
                        surplus -= grant
                        if allocs[i] < agent_data[i][4]:
                            still_hungry.append(i)
                    hungry = still_hungry

            # Build agent cards with allocated lines
            cards = []
            for idx, (task_id, stage, status, output_lines, _need, started, elapsed) in enumerate(agent_data):
                style, symbol = _STATUS_STYLE.get(status, ("white", "?"))
                # Use spinner for active statuses
                if status in ("running", "cloning", "merging"):
                    symbol = spinner

                # Format elapsed time
                total_secs = int(elapsed.total_seconds())
                if total_secs >= 3600:
                    elapsed_str = f"{total_secs // 3600}h{(total_secs % 3600) // 60:02d}m"
                elif total_secs >= 60:
                    elapsed_str = f"{total_secs // 60}m{total_secs % 60:02d}s"
                else:
                    elapsed_str = f"{total_secs}s"
                start_str = started.strftime("%H:%M:%S")

                header = Table.grid(expand=True, padding=(0, 1))
                header.add_column(ratio=5)
                header.add_column(ratio=1, justify="right")
                header.add_row(
                    f"[bold]{task_id}[/bold]  [dim]{stage}[/dim]",
                    f"[dim]{start_str}[/dim] [dim]({elapsed_str})[/dim]  [{style}]{symbol} {status}[/{style}]",
                )
                cards.append(header)

                # Output lines: show the most recent that fit (alloc - 1 for header)
                max_lines = max(allocs[idx] - 1, 0)
                if output_lines:
                    for ts, line in output_lines[-max_lines:]:
                        cards.append(Text(f"  [{ts}] {line}", style="dim", no_wrap=True))
                else:
                    cards.append(Text("  waiting for output...", style="dim"))

                if idx < len(agent_data) - 1:
                    cards.append(Rule(style="dim"))

            agents_used = sum(allocs) + chrome
            agents_panel = Panel(
                Group(*cards),
                title="[bold]Active Agents[/bold]",
                border_style="green",
                height=agents_used,
            )
        else:
            agents_used = 3  # border(2) + 1 line of text
            agents_panel = Panel(
                Text("No active agents", style="dim"),
                title="[bold]Active Agents[/bold]",
                border_style="green",
                height=agents_used,
            )

        # --- Build log panel with remaining space ---
        log_height = max(usable - agents_used, 5)
        # Panel border = 2, so content lines = log_height - 2
        log_content_lines = max(log_height - 2, 1)

        text = Text(no_wrap=True, overflow="ellipsis")
        with self._lock:
            lines = list(self._ring)
        # Show newest lines first, limited to available space
        shown = list(reversed(lines))[:log_content_lines]
        for line in shown:
            text.append(line + "\n", style="dim")
        # Pad to fill allocated height so the layout stays stable
        for _ in range(log_content_lines - len(shown)):
            text.append("\n")

        log_panel = Panel(
            text,
            title="[bold]Log[/bold]",
            border_style="blue",
            height=log_height,
        )

        return Group(log_panel, agents_panel)

    def _refresh(self) -> None:
        if self._live is None:
            return
        self._live.refresh()


class _DashboardStream:
    """A ``sys.stdout``-compatible wrapper that routes writes to a dashboard.

    Buffers partial lines and flushes complete lines (terminated by ``\\n``)
    to :meth:`Dashboard.log` (or :meth:`NullDashboard.log`).  This lets the
    existing ``print()`` calls throughout ``phases.py`` and ``orchestrator.py``
    route their output through the dashboard without being modified.

    :param dashboard: The active dashboard instance.
    :param original: The real stdout stream, kept for ``fileno()`` and other
        low-level operations that the rich Console may invoke.
    """

    def __init__(self, dashboard: "Dashboard | NullDashboard", original: IO[str]) -> None:
        self._dashboard = dashboard
        self._original = original
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._dashboard.log(line)
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            self._dashboard.log(self._buf)
            self._buf = ""

    def fileno(self) -> int:
        return self._original.fileno()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str) -> object:
        return getattr(self._original, name)


class NullDashboard:
    """No-op dashboard used when stdout is not a TTY or dashboard is disabled.

    All methods write to *stream* (defaulting to the original ``sys.stdout``)
    and optionally to a log file, mimicking the old
    :class:`~workflow_lib.executor.Logger` behaviour.

    :param log_file: Optional open file object for log output.
    :param stream: The real stdout to write plain-text output to.  Captured
        before any :class:`_DashboardStream` wrapping so that writing to
        ``NullDashboard.log`` does not loop back through itself.
    """

    def __init__(
        self,
        log_file: Optional[IO[str]] = None,
        stream: Optional[IO[str]] = None,
    ) -> None:
        import sys as _sys
        self._log_file = log_file
        self._stream = stream or _sys.stdout
        self._lock = threading.Lock()

    def __enter__(self) -> "NullDashboard":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def log(self, message: str) -> None:
        ts = _now()
        with self._lock:
            for line in message.splitlines():
                line = line.rstrip()
                if not line:
                    continue
                entry = f"[{ts}] {line}"
                self._stream.write(entry + "\n")
                self._stream.flush()
                if self._log_file:
                    try:
                        self._log_file.write(entry + "\n")
                        self._log_file.flush()
                    except Exception:
                        pass

    def set_agent(self, task_id: str, stage: str, status: str, last_line: str = "") -> None:
        pass

    def update_last_line(self, task_id: str, last_line: str) -> None:
        pass

    def remove_agent(self, task_id: str) -> None:
        pass

    def prompt_input(self, message: str) -> str:
        """Show a prominent prompt and return user input."""
        self._stream.write("\n" + "=" * 60 + "\n")
        self._stream.write(f"  INPUT REQUIRED: {message}\n")
        self._stream.write("=" * 60 + "\n")
        self._stream.flush()
        return input("> ")


def make_dashboard(log_file: Optional[IO[str]] = None) -> "Dashboard | NullDashboard":
    """Return a :class:`Dashboard` when stdout is a TTY, else :class:`NullDashboard`.

    Captures the real ``sys.stdout`` *before* any wrapping so that the
    :class:`NullDashboard` can write directly to the terminal without going
    through a :class:`_DashboardStream` loop.

    :param log_file: Optional open file object to mirror log output to.
    :returns: A dashboard instance ready to be used as a context manager.
    """
    import sys
    original_stdout = sys.stdout
    if original_stdout.isatty():
        return Dashboard(log_file=log_file)
    return NullDashboard(log_file=log_file, stream=original_stdout)
