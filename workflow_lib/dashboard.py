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
import threading
import types
from collections import deque
from datetime import datetime
from typing import Deque, Dict, IO, Optional, Tuple, Type
from zoneinfo import ZoneInfo

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

_PST = ZoneInfo("America/Los_Angeles")
_LOG_LINES = 30   # max lines kept in the aggregate log ring buffer
_AGENT_LINES = 4  # max recent output lines shown per agent card


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
        # task_id -> (command, status, lines_deque)
        # lines_deque holds (timestamp_str, text) pairs for the agent card
        self._agents: Dict[str, Tuple[str, str, Deque[Tuple[str, str]]]] = {}
        self._live: Optional[Live] = None
        self._console = Console(highlight=False)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "Dashboard":
        layout = self._build_layout()
        self._live = Live(
            layout,
            console=self._console,
            refresh_per_second=4,
            screen=False,
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
            # Preserve existing log lines when updating status/stage
            if task_id in self._agents:
                _, _, lines = self._agents[task_id]
            else:
                lines: Deque[Tuple[str, str]] = deque(maxlen=_AGENT_LINES)
            short = last_line.strip()[:120] if last_line else ""
            if short:
                lines.append((_now_short(), short))
            self._agents[task_id] = (stage, status, lines)
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
                command, status, lines = self._agents[task_id]
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

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="log",    ratio=2),
            Layout(name="agents", ratio=1),
        )
        return layout

    def _render_log_panel(self) -> Panel:
        text = Text()
        with self._lock:
            lines = list(self._ring)
        # Render newest lines first so they are always visible when the panel
        # is shorter than the full ring buffer.
        for line in reversed(lines):
            text.append(line + "\n", style="dim")
        return Panel(text, title="[bold]Log[/bold]", border_style="blue")

    def _render_agents_panel(self) -> Panel:
        with self._lock:
            visible = {
                k: v for k, v in self._agents.items()
                if v[1] in ("running", "failed", "cloning", "merging", "queued", "waiting")
            }

        if not visible:
            return Panel(
                Text("No active agents", style="dim"),
                title="[bold]Active Agents[/bold]",
                border_style="green",
            )

        cards = []
        for i, task_id in enumerate(sorted(visible)):
            stage, status, lines = visible[task_id]
            style, symbol = _STATUS_STYLE.get(status, ("white", "?"))

            # Header: task_id left, status right
            header = Table.grid(expand=True, padding=(0, 1))
            header.add_column(ratio=5)
            header.add_column(ratio=1, justify="right")
            header.add_row(
                f"[bold]{task_id}[/bold]  [dim]{stage}[/dim]",
                f"[{style}]{symbol} {status}[/{style}]",
            )
            cards.append(header)

            with self._lock:
                output_lines = list(lines)

            if output_lines:
                for ts, line in output_lines:
                    cards.append(Text(f"  [{ts}] {line}", style="dim", no_wrap=True))
            else:
                cards.append(Text("  [dim]waiting for output...[/dim]"))

            if i < len(visible) - 1:
                cards.append(Rule(style="dim"))

        return Panel(
            Group(*cards),
            title="[bold]Active Agents[/bold]",
            border_style="green",
        )

    def _refresh(self) -> None:
        if self._live is None:
            return
        layout = self._build_layout()
        layout["log"].update(self._render_log_panel())
        layout["agents"].update(self._render_agents_panel())
        self._live.update(layout)


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
