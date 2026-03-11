"""AI CLI runner abstractions.

Each runner wraps a specific AI command-line tool (Gemini, Claude, Copilot)
behind a common interface so that the rest of the workflow can switch
backends without changing its logic.

Runner selection is handled at construction time in :mod:`workflow_lib.cli`
and :mod:`workflow_lib.replan` via ``_make_runner()``.
"""

import os
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Callable, List, Dict, Any, Optional

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg"}

# All backend names accepted by make_runner().
VALID_BACKENDS = {"gemini", "claude", "opencode", "copilot", "cline", "aider", "codex", "qwen"}


def parse_stream_json_line(raw: str) -> Optional[str]:
    """Extract human-readable text from a single Anthropic-style JSONL line.

    Both ``claude`` and ``qwen`` CLIs emit the same JSONL format when
    ``--output-format stream-json`` is used.

    For ``stream_event`` messages (emitted with ``--include-partial-messages``),
    returns the text delta directly so the caller can accumulate tokens into
    lines.
    """
    import json as _json
    stripped = raw.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        obj = _json.loads(stripped)
    except _json.JSONDecodeError:
        return None

    msg_type = obj.get("type")

    # Incremental streaming tokens (--include-partial-messages)
    if msg_type == "stream_event":
        event = obj.get("event", {})
        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                return delta.get("partial_json", "")
        return None

    if msg_type == "assistant":
        content = obj.get("message", {}).get("content", [])
        parts: List[str] = []
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
            elif block.get("type") == "tool_use":
                name = block.get("name", "unknown")
                inp = block.get("input", {})
                if name == "web_fetch":
                    parts.append(f"[tool] {name}: {inp.get('url', '')}")
                elif name in ("read_file", "write_file", "edit"):
                    parts.append(f"[tool] {name}: {inp.get('file_path', inp.get('path', ''))}")
                elif name == "run_shell_command":
                    parts.append(f"[tool] {name}: {inp.get('command', '')}")
                elif name == "grep_search":
                    parts.append(f"[tool] {name}: {inp.get('pattern', '')}")
                else:
                    parts.append(f"[tool] {name}")
        return "\n".join(parts) if parts else None

    if msg_type == "user":
        content = obj.get("message", {}).get("content", [])
        parts = []
        for block in content:
            if block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "")
                text = block.get("content", "")
                if isinstance(text, str) and text.strip():
                    parts.append(f"[result] {text.strip()}")
                elif block.get("is_error"):
                    parts.append(f"[result] error (tool {tool_id})")
                else:
                    parts.append(f"[result] ok (tool {tool_id})")
            elif block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts) if parts else None

    if msg_type == "result":
        result_text = obj.get("result", "").strip()
        return result_text or None

    return None

def _load_resume_prompt() -> str:
    """Load the resume prompt from the prompts directory."""
    prompt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "resume.md")
    with open(prompt_path, "r") as f:
        return f.read().strip()


RESUME_PROMPT = _load_resume_prompt()


class AIRunner:
    """Abstract base class for AI CLI runners.

    Subclasses must implement :meth:`run`.
    """

    def __init__(self, model: Optional[str] = None, user: Optional[str] = None) -> None:
        self.model = model
        self.user = user

    def _env(self) -> Dict[str, str]:
        """Return the environment dict to pass to subprocesses.

        Starts from the current process environment so that API keys,
        PATH, and other variables are available to AI CLI tools.
        """
        return os.environ.copy()

    def _wrap_cmd(self, cmd: List[str]) -> List[str]:
        """Optionally prefix *cmd* with ``sudo su -l <user> -c '...'``.

        When :attr:`user` is set and differs from the current OS user, the
        command is run as that user via a login shell so that their full
        environment (API keys, CLI settings, etc.) is initialised.

        :param cmd: Original command list.
        :returns: Command list, possibly prefixed with ``sudo su``.
        """
        if self.user and self.user != os.getenv("USER", ""):
            path = os.environ.get("PATH", "")
            return ["sudo", "-H", "-u", self.user, "--", "bash", "-l", "-c", f"PATH={shlex.quote(path)} {shlex.join(cmd)}"]
        return cmd

    def _kill_process(self, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        """Kill a timed-out process.  Subclasses may override for graceful shutdown."""
        proc.kill()
        proc.wait()

    def _run_streaming(
        self,
        cmd: List[str],
        prompt: str,
        cwd: str,
        on_line: Callable[[str], None],
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run *cmd* with *prompt* on stdin, calling *on_line* for each output line.

        Uses :class:`subprocess.Popen` to stream stdout in real time.  stderr is
        collected and appended to stdout in the returned object (so callers that
        check ``result.stdout`` still see it).

        :param cmd: Command list to execute.
        :param prompt: Text written to the process stdin.  If empty, stdin is
            connected to ``/dev/null`` instead of a pipe.
        :param cwd: Working directory for the subprocess.
        :param on_line: Callback invoked once per output line (newline stripped).
        :param timeout: Maximum seconds to wait for the process. ``None`` means
            no limit. On timeout the process is killed and a
            :class:`subprocess.TimeoutExpired` is raised.
        :param abort_event: Optional :class:`threading.Event`.  When set (e.g.
            by the *on_line* callback on quota detection), the process is killed
            and the method returns early without raising an exception.
        :returns: Completed process with combined stdout (streamed lines) and
            stderr captured separately.
        :raises subprocess.TimeoutExpired: When the process exceeds *timeout*.
        """
        use_stdin = bool(prompt)
        proc = subprocess.Popen(
            self._wrap_cmd(cmd),
            stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=self._env(),
            start_new_session=True,
        )
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []

        if use_stdin:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()

        def _read_stdout() -> None:
            assert proc.stdout is not None
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                stripped = line.rstrip("\n")
                stdout_lines.append(stripped)
                if stripped.strip():
                    on_line(stripped)
                if abort_event and abort_event.is_set():
                    break

        def _read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                stripped = line.rstrip("\n")
                stderr_lines.append(stripped)
                if stripped.strip():
                    on_line(f"[stderr] {stripped}")
                if abort_event and abort_event.is_set():
                    break

        reader = threading.Thread(target=_read_stdout, daemon=True)
        stderr_reader = threading.Thread(target=_read_stderr, daemon=True)
        reader.start()
        stderr_reader.start()

        deadline = time.monotonic() + (timeout if timeout is not None else float("inf"))
        aborted = False
        try:
            while reader.is_alive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_process(proc)
                    stderr_reader.join(timeout=5.0)
                    raise subprocess.TimeoutExpired(cmd, timeout or 0)
                if abort_event and abort_event.is_set():
                    self._kill_process(proc)
                    stderr_reader.join(timeout=5.0)
                    aborted = True
                    break
                reader.join(timeout=min(remaining, 1.0))
        except KeyboardInterrupt:
            # Let the subprocess finish naturally; the orchestrator's SIGINT
            # handler has already flagged a graceful shutdown.
            reader.join()

        # abort_event may have been set while reader.join() was blocking —
        # the reader exits cleanly but the process is still running.
        if not aborted and abort_event and abort_event.is_set():
            self._kill_process(proc)
            stderr_reader.join(timeout=5.0)
            aborted = True

        if not aborted:
            stderr_reader.join()
        proc.wait()

        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
        )

    def _run_streaming_json(
        self,
        cmd: List[str],
        cwd: str,
        on_line: Callable[[str], None],
        timeout: Optional[int] = None,
        prompt: str = "",
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run *cmd* and parse its stream-json output, calling *on_line* for
        each meaningful extracted line.

        Uses :func:`parse_stream_json_line` to convert Anthropic-style JSONL
        into human-readable text.

        :param prompt: Optional text written to the process stdin.  If empty,
            stdin is connected to ``/dev/null``.
        :param abort_event: Optional :class:`threading.Event`.  When set, the
            process is killed and the method returns early.
        :raises subprocess.TimeoutExpired: When the process exceeds *timeout*.
        """
        use_stdin = bool(prompt)
        proc = subprocess.Popen(
            self._wrap_cmd(cmd),
            stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=self._env(),
            start_new_session=True,
        )
        result_text: List[str] = []

        if use_stdin:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()

        def _read_stdout() -> None:
            assert proc.stdout is not None
            import json as _json
            # Buffer for accumulating streaming token deltas into lines.
            # ``has_deltas`` is set once we see any stream_event and never
            # cleared — it tells us the CLI is sending partial messages so
            # the final ``assistant`` and ``result`` messages are duplicates.
            token_buf = ""
            has_deltas = False
            while True:
                raw_line = proc.stdout.readline()
                if not raw_line:
                    break
                parsed = parse_stream_json_line(raw_line)
                if parsed is None:
                    continue

                # Determine message type from the raw JSON
                stripped = raw_line.strip()
                try:
                    obj = _json.loads(stripped)
                except _json.JSONDecodeError:
                    obj = {}
                msg_type = obj.get("type")

                if msg_type == "stream_event":
                    has_deltas = True
                    token_buf += parsed
                    # Emit complete lines from the buffer
                    while "\n" in token_buf:
                        line, token_buf = token_buf.split("\n", 1)
                        if line.strip():
                            result_text.append(line)
                            on_line(line)
                elif msg_type == "assistant":
                    # Flush any remaining token buffer
                    if token_buf.strip():
                        result_text.append(token_buf.strip())
                        on_line(token_buf.strip())
                    token_buf = ""
                    if has_deltas:
                        # Already streamed via deltas — skip duplicate
                        continue
                    result_text.append(parsed)
                    for sub in parsed.splitlines():
                        if sub.strip():
                            on_line(sub)
                elif msg_type == "result":
                    result_text.append(parsed)
                    if not has_deltas:
                        for sub in parsed.splitlines():
                            if sub.strip():
                                on_line(sub)
                else:
                    # user messages (tool results), etc.
                    result_text.append(parsed)
                    for sub in parsed.splitlines():
                        if sub.strip():
                            on_line(sub)
                if abort_event and abort_event.is_set():
                    break

            # Flush any trailing token buffer
            if token_buf.strip():
                result_text.append(token_buf.strip())
                on_line(token_buf.strip())

        stderr_lines: List[str] = []

        def _read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                stripped = line.rstrip("\n")
                stderr_lines.append(stripped)
                if stripped.strip():
                    on_line(f"[stderr] {stripped}")
                if abort_event and abort_event.is_set():
                    break

        reader = threading.Thread(target=_read_stdout, daemon=True)
        stderr_reader = threading.Thread(target=_read_stderr, daemon=True)
        reader.start()
        stderr_reader.start()

        deadline = time.monotonic() + (timeout if timeout is not None else float("inf"))
        aborted = False
        try:
            while reader.is_alive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_process(proc)
                    stderr_reader.join(timeout=5.0)
                    raise subprocess.TimeoutExpired(cmd, timeout or 0)
                if abort_event and abort_event.is_set():
                    self._kill_process(proc)
                    stderr_reader.join(timeout=5.0)
                    aborted = True
                    break
                reader.join(timeout=min(remaining, 1.0))
        except KeyboardInterrupt:
            reader.join()

        # abort_event may have been set while reader.join() was blocking —
        # the reader exits cleanly but the process is still running.
        if not aborted and abort_event and abort_event.is_set():
            self._kill_process(proc)
            stderr_reader.join(timeout=5.0)
            aborted = True

        if not aborted:
            stderr_reader.join()
        proc.wait()

        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="\n".join(result_text),
            stderr="\n".join(stderr_lines),
        )

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        """Return the CLI command list (without prompt). Subclasses must override."""
        raise NotImplementedError()

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Invoke the AI CLI and return its completed process result.

        :param cwd: Working directory for the subprocess.
        :param full_prompt: Full rendered prompt to pass to the CLI.
        :param image_paths: Optional list of absolute paths to image files to
            attach to the request.  How images are delivered depends on the
            backend — see concrete subclass implementations.
        :param on_line: Optional streaming callback invoked once per output line.
        :param timeout: Maximum seconds to wait for the AI process.
            ``None`` means no limit.
        :param abort_event: Optional :class:`threading.Event`.  When set (e.g.
            on quota detection), the subprocess is killed immediately.
        :raises NotImplementedError: Always — subclasses must override this.
        :returns: The completed subprocess result.
        """
        raise NotImplementedError()


class SessionResumableRunner(AIRunner):
    """Base for runners supporting ``--session-id`` / ``--resume`` soft timeouts.

    When *soft_timeout* is set, each invocation is spawned with a unique
    ``--session-id``.  If the soft timeout elapses the process is killed
    and a new ``--resume <id>`` process is launched with a wrap-up prompt.
    """

    DEFAULT_SOFT_TIMEOUT = 480
    RESUME_HARD_TIMEOUT = 120

    def __init__(self, model: Optional[str] = None, soft_timeout: Optional[int] = DEFAULT_SOFT_TIMEOUT, user: Optional[str] = None) -> None:
        super().__init__(model=model, user=user)
        self.soft_timeout = soft_timeout

    def get_cmd(self, image_paths: Optional[List[str]] = None, session_id: Optional[str] = None, resume: bool = False) -> List[str]:
        raise NotImplementedError()

    def _build_resume_cmd_and_prompt(self, session_id: str) -> tuple:
        """Return ``(cmd_list, prompt_for_stdin)`` for the resume session.

        Subclasses override to control how the resume prompt is delivered
        (positional arg vs stdin).
        """
        raise NotImplementedError()

    def _compress_session(
        self,
        session_id: str,
        cwd: str,
        on_line: Callable[[str], None],
    ) -> None:
        """Optionally compress the session context before resuming.

        Subclasses override to run a compression command (e.g. ``/compress``).
        The default implementation is a no-op.
        """
        pass

    def _run_session(
        self,
        cmd: List[str],
        prompt: str,
        cwd: str,
        on_line: Callable[[str], None],
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run a single session.  Subclasses override for JSON parsing etc."""
        return self._run_streaming(cmd, prompt, cwd, on_line, timeout=timeout, abort_event=abort_event)

    def _run_with_soft_timeout(
        self,
        cmd: List[str],
        prompt: str,
        cwd: str,
        on_line: Callable[[str], None],
        session_id: str,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run with soft timeout.  If it fires, kill and resume."""
        backend_name = self.__class__.__name__.replace("Runner", "").lower()
        try:
            return self._run_session(cmd, prompt, cwd, on_line, timeout=self.soft_timeout, abort_event=abort_event)
        except subprocess.TimeoutExpired:
            if abort_event and abort_event.is_set():
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="quota exceeded")
            on_line(f"[soft-timeout] Interrupting {backend_name} session to resume with finish-up prompt...")
            resume_cmd, resume_prompt = self._build_resume_cmd_and_prompt(session_id)
            hard_timeout = timeout or self.RESUME_HARD_TIMEOUT
            on_line(f"[soft-timeout] Resuming session {session_id} with {hard_timeout}s to finish...")
            try:
                return self._run_session(resume_cmd, resume_prompt, cwd, on_line, timeout=hard_timeout, abort_event=abort_event)
            except subprocess.TimeoutExpired:
                on_line(f"[soft-timeout] Resume session exceeded {hard_timeout}s hard limit, killed.")
                return subprocess.CompletedProcess(
                    args=resume_cmd, returncode=1, stdout="", stderr="soft-timeout: resume exceeded hard limit"
                )


class GeminiRunner(AIRunner):
    """Runner for the ``gemini`` CLI (Google Gemini).

    Passes the prompt via stdin to ``gemini -y`` (auto-confirm mode).
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["gemini", "-y"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``gemini -y`` with *full_prompt* on stdin.

        Images are delivered by appending ``@<absolute_path>`` file-reference
        lines to the prompt, which the Gemini CLI resolves as attachments.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        prompt = full_prompt
        if image_paths:
            refs = "\n".join(f"@{p}" for p in image_paths)
            prompt = f"{prompt}\n\n{refs}"
        cmd = self.get_cmd(image_paths)
        if on_line is not None:
            return self._run_streaming(cmd, prompt, cwd, on_line, timeout=timeout, abort_event=abort_event)
        return subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())


class ClaudeRunner(AIRunner):
    """Runner for the ``claude`` CLI (Anthropic Claude Code).

    Passes the prompt via stdin to ``claude -p --dangerously-skip-permissions``
    and captures stdout/stderr.
    """

    def _env(self) -> Dict[str, str]:
        env = os.environ.copy()
        # Trigger auto-compaction at 75% context capacity (default is 95%) so
        # large tool results (e.g. verbose Rust build output) don't cause a
        # sudden spike past the model's token limit before compaction fires.
        env.setdefault("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "75")
        return env

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "stream-json", "--include-partial-messages", "--verbose"]
        if self.model:
            cmd += ["--model", self.model]
        for path in (image_paths or []):
            cmd += ["--image", path]
        return cmd

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``claude -p --dangerously-skip-permissions --output-format stream-json``.

        Images are delivered via ``--image <path>`` flags appended to the
        command, one flag pair per image.  JSONL output is parsed into
        human-readable text via :func:`parse_stream_json_line`.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        if on_line is not None:
            return self._run_streaming_json(cmd, cwd, on_line, timeout=timeout, prompt=full_prompt, abort_event=abort_event)
        # Non-streaming fallback: parse JSONL after completion
        result = subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())
        parsed_lines: List[str] = []
        for line in result.stdout.splitlines():
            parsed = parse_stream_json_line(line)
            if parsed:
                parsed_lines.append(parsed)
        return subprocess.CompletedProcess(
            args=result.args,
            returncode=result.returncode,
            stdout="\n".join(parsed_lines),
            stderr=result.stderr,
        )


class OpencodeRunner(AIRunner):
    """Runner for the ``opencode`` CLI.

    Passes the prompt via stdin to ``opencode run`` and captures
    stdout/stderr.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["opencode", "run"]
        if self.model:
            cmd += ["--model", self.model]
        for path in (image_paths or []):
            cmd += ["-f", path]
        return cmd

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``opencode run`` with *full_prompt* on stdin.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        if on_line is not None:
            return self._run_streaming(cmd, full_prompt, cwd, on_line, timeout=timeout, abort_event=abort_event)
        return subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())


class ClineRunner(AIRunner):
    """Runner for the ``cline`` CLI.

    Passes the prompt as a positional argument to ``cline --yolo``.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["cline", "--yolo"]
        if self.model:
            cmd += ["-m", self.model]
        return cmd

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``cline --yolo`` with *full_prompt* as the prompt argument.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        cmd.append(full_prompt)
        if on_line is not None:
            return self._run_streaming(cmd, "", cwd, on_line, timeout=timeout, abort_event=abort_event)
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())


class AiderRunner(AIRunner):
    """Runner for the ``aider`` CLI.

    Passes the prompt via ``--message`` to ``aider --yes-always --no-auto-commits``.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["aider", "--yes-always", "--no-auto-commits"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``aider --yes-always --no-auto-commits --message <prompt>``.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        cmd += ["--message", full_prompt]
        if on_line is not None:
            return self._run_streaming(cmd, "", cwd, on_line, timeout=timeout, abort_event=abort_event)
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())


class QwenRunner(SessionResumableRunner):
    """Runner for the ``qwen`` CLI (Qwen Code).

    Uses ``--output-format stream-json`` and parses the JSONL stream to
    extract human-readable text.  Supports soft-timeout via
    ``--session-id`` / ``--resume``.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None, session_id: Optional[str] = None, resume: bool = False) -> List[str]:
        cmd = ["qwen", "-y", "--output-format", "stream-json", "--include-partial-messages", "--chat-recording"]
        if self.model:
            cmd += ["-m", self.model]
        if session_id:
            if resume:
                cmd += ["--resume", session_id]
            else:
                cmd += ["--session-id", session_id]
        return cmd

    def _kill_process(self, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        """Send two SIGINTs to stop the current prompt, wait 1s, then SIGKILL."""
        try:
            proc.send_signal(signal.SIGINT)
            proc.send_signal(signal.SIGINT)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _build_resume_cmd_and_prompt(self, session_id: str) -> tuple:
        """Build the resume command for qwen.

        The resume prompt is passed via stdin (using ``-p -``).
        """
        cmd = self.get_cmd(session_id=session_id, resume=True)
        return cmd, RESUME_PROMPT

    @staticmethod
    def _parse_stream_line(raw: str) -> Optional[str]:
        """Backwards-compatible alias for :func:`parse_stream_json_line`."""
        return parse_stream_json_line(raw)

    def _run_session(
        self,
        cmd: List[str],
        prompt: str,
        cwd: str,
        on_line: Callable[[str], None],
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Override to use JSONL-parsing streaming instead of plain text."""
        return self._run_streaming_json(cmd, cwd, on_line, timeout=timeout, prompt=prompt, abort_event=abort_event)

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``qwen -y --output-format stream-json`` and parse the output.

        When *soft_timeout* is configured, the process is spawned with a
        unique ``--session-id``.  If the soft timeout elapses before it
        finishes, the session is interrupted and resumed with a "wrap up"
        prompt.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        session_id = str(uuid.uuid4()) if self.soft_timeout else None
        cmd = self.get_cmd(image_paths, session_id=session_id)

        if on_line is not None and self.soft_timeout and session_id:
            return self._run_with_soft_timeout(cmd, full_prompt, cwd, on_line, session_id, timeout, abort_event=abort_event)
        if on_line is not None:
            return self._run_streaming_json(cmd, cwd, on_line, timeout=timeout, prompt=full_prompt, abort_event=abort_event)

        # Non-streaming fallback
        result = subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())
        parsed_lines: List[str] = []
        for line in result.stdout.splitlines():
            parsed = parse_stream_json_line(line)
            if parsed:
                parsed_lines.append(parsed)
        return subprocess.CompletedProcess(
            args=result.args,
            returncode=result.returncode,
            stdout="\n".join(parsed_lines),
            stderr=result.stderr,
        )


class CodexRunner(AIRunner):
    """Runner for the ``codex`` CLI (OpenAI Codex).

    Uses ``codex exec --full-auto`` for non-interactive execution.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["codex", "exec", "--full-auto"]
        if self.model:
            cmd += ["-m", self.model]
        for path in (image_paths or []):
            cmd += ["-i", path]
        return cmd

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``codex exec --full-auto`` with *full_prompt* as the prompt argument.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        cmd.append(full_prompt)
        if on_line is not None:
            return self._run_streaming(cmd, "", cwd, on_line, timeout=timeout, abort_event=abort_event)
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())


class CopilotRunner(AIRunner):
    """Runner for the GitHub Copilot CLI.

    Writes the prompt to a temporary file and passes its path as an
    ``@``-reference to ``copilot --yolo``.  Falls back gracefully when the
    binary is not found.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["copilot", "-p", "@<prompt_file>", "--yolo"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run the Copilot CLI with *full_prompt* written to a temp file.

        Tries the ``copilot -p @<tempfile> --yolo`` invocation.  If the
        binary is not found, re-raises the :exc:`FileNotFoundError` or
        raises :exc:`RuntimeError`.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        prompt = full_prompt
        if image_paths:
            refs = "\n".join(f"@{p}" for p in image_paths)
            prompt = f"{prompt}\n\n{refs}"

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=True) as f:
            f.write(prompt)
            prompt_file = f.name
            candidates = [
                ["copilot", "-p", f"Follow the instructions in @{prompt_file}", "--yolo"],
            ]
            last_exc: Optional[Exception] = None
            last_result: Optional[subprocess.CompletedProcess] = None  # type: ignore[type-arg]
            for cmd in candidates:
                try:
                    if on_line is not None:
                        last_result = self._run_streaming(cmd, prompt, cwd, on_line, timeout=timeout, abort_event=abort_event)
                    else:
                        last_result = subprocess.run(
                            cmd, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env()
                        )
                    if last_result.returncode == 0:
                        return last_result
                except FileNotFoundError as e:
                    last_exc = e
                    continue

        if last_result is not None:
            return last_result
        raise last_exc if last_exc is not None else RuntimeError("Failed to invoke copilot CLI")


def make_runner(backend: str, model: Optional[str] = None, soft_timeout: Optional[int] = None, user: Optional[str] = None) -> AIRunner:
    """Instantiate the correct AI runner for the given backend name.

    :param backend: One of ``"gemini"``, ``"claude"``, ``"copilot"``,
        ``"opencode"``, ``"cline"``, ``"aider"``, ``"codex"``, or ``"qwen"``.
        Unknown values default to Gemini.
    :param model: Optional model name to pass through to the CLI via ``--model``.
    :param soft_timeout: For backends that support soft timeout (qwen),
        overrides the default soft timeout.  If ``None``, the runner's
        class default is used.
    :param user: Optional OS username.  When set (and different from the
        current user), the CLI is prefixed with ``sudo -u <user> --set-home --``
        so that the target user's home-directory config is used.
    :returns: An AI runner instance.
    """
    if backend == "claude":
        return ClaudeRunner(model=model, user=user)
    elif backend == "copilot":
        return CopilotRunner(model=model, user=user)
    elif backend == "opencode":
        return OpencodeRunner(model=model, user=user)
    elif backend == "cline":
        return ClineRunner(model=model, user=user)
    elif backend == "aider":
        return AiderRunner(model=model, user=user)
    elif backend == "codex":
        return CodexRunner(model=model, user=user)
    elif backend == "qwen":
        kwargs: Dict[str, Any] = {"model": model, "user": user}
        if soft_timeout is not None:
            kwargs["soft_timeout"] = soft_timeout
        return QwenRunner(**kwargs)
    # default: gemini
    return GeminiRunner(model=model, user=user)
