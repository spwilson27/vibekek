"""AI CLI runner abstractions.

Each runner wraps a specific AI command-line tool (Gemini, Claude, Copilot)
behind a common interface so that the rest of the workflow can switch
backends without changing its logic.

Runner selection is handled at construction time in :mod:`workflow_lib.cli`
and :mod:`workflow_lib.replan` via ``_make_runner()``.
"""

import os
import signal
import subprocess
import tempfile
import threading
import uuid
from typing import Callable, List, Dict, Any, Optional

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg"}


class AIRunner:
    """Abstract base class for AI CLI runners.

    Subclasses must implement :meth:`run`.
    """

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model

    def _env(self) -> Dict[str, str]:
        """Return the environment dict to pass to subprocesses.

        Starts from the current process environment so that API keys,
        PATH, and other variables are available to AI CLI tools.
        """
        return os.environ.copy()

    def _run_streaming(
        self,
        cmd: List[str],
        prompt: str,
        cwd: str,
        on_line: Callable[[str], None],
        timeout: Optional[int] = None,
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
        :returns: Completed process with combined stdout (streamed lines) and
            stderr captured separately.
        :rtype: subprocess.CompletedProcess
        :raises subprocess.TimeoutExpired: When the process exceeds *timeout*.
        """
        use_stdin = bool(prompt)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=self._env(),
        )
        stdout_lines: List[str] = []

        def _read_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                stripped = line.rstrip("\n")
                stdout_lines.append(stripped)
                if stripped.strip():
                    on_line(stripped)

        reader = threading.Thread(target=_read_stdout, daemon=True)
        reader.start()

        if use_stdin:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()

        reader.join(timeout=timeout)
        if reader.is_alive():
            # Timeout: kill the process
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(cmd, timeout or 0)

        stderr_raw = proc.stderr.read() if proc.stderr else ""
        proc.wait()

        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="\n".join(stdout_lines),
            stderr=stderr_raw,
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
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Invoke the AI CLI and return its completed process result.

        :param cwd: Working directory for the subprocess.
        :type cwd: str
        :param full_prompt: Full rendered prompt to pass to the CLI.
        :type full_prompt: str
        :param image_paths: Optional list of absolute paths to image files to
            attach to the request.  How images are delivered depends on the
            backend — see concrete subclass implementations.
        :type image_paths: list[str] or None
        :param timeout: Maximum seconds to wait for the AI process.
            ``None`` means no limit.
        :type timeout: int or None
        :raises NotImplementedError: Always — subclasses must override this.
        :returns: The completed subprocess result.
        :rtype: subprocess.CompletedProcess
        """
        raise NotImplementedError()


class GeminiRunner(AIRunner):
    """Runner for the ``gemini`` CLI (Google Gemini).

    Passes the prompt via stdin to ``gemini -y`` (auto-confirm mode) and
    captures stdout/stderr.
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
            return self._run_streaming(cmd, prompt, cwd, on_line, timeout=timeout)
        return subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())


class ClaudeRunner(AIRunner):
    """Runner for the ``claude`` CLI (Anthropic Claude Code).

    Passes the prompt via stdin to ``claude -p --dangerously-skip-permissions``
    and captures stdout/stderr.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
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
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``claude -p --dangerously-skip-permissions`` with *full_prompt* on stdin.

        Images are delivered via ``--image <path>`` flags appended to the
        command, one flag pair per image.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        if on_line is not None:
            return self._run_streaming(cmd, full_prompt, cwd, on_line, timeout=timeout)
        return subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())


class OpencodeRunner(AIRunner):
    """Runner for the ``opencode`` CLI.

    Passes the prompt via stdin to ``opencode --print --yes`` and captures
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
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``opencode run`` with *full_prompt* on stdin.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        if on_line is not None:
            return self._run_streaming(cmd, full_prompt, cwd, on_line, timeout=timeout)
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
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``cline --yolo`` with *full_prompt* as the prompt argument.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        cmd.append(full_prompt)
        if on_line is not None:
            return self._run_streaming(cmd, "", cwd, on_line, timeout=timeout)
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
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``aider --yes-always --no-auto-commits --message <prompt>``.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        cmd += ["--message", full_prompt]
        if on_line is not None:
            return self._run_streaming(cmd, "", cwd, on_line, timeout=timeout)
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())


class QwenRunner(AIRunner):
    """Runner for the ``qwen`` CLI (Qwen Code).

    Uses ``--output-format stream-json`` and parses the JSONL stream to
    extract human-readable text from ``assistant`` and ``result`` messages,
    filtering out the raw JSON noise.

    Supports a *soft_timeout* (seconds).  When set, each invocation is
    spawned with a unique ``--session-id``.  If the soft timeout elapses
    the process is interrupted (SIGINT) and a new ``qwen --resume <id>``
    process is launched with a prompt telling the agent to wrap up.
    """

    # Default soft timeout: 8 minutes (480s).  ``None`` disables the feature.
    DEFAULT_SOFT_TIMEOUT = 480

    def __init__(self, model: Optional[str] = None, soft_timeout: Optional[int] = DEFAULT_SOFT_TIMEOUT) -> None:
        super().__init__(model=model)
        self.soft_timeout = soft_timeout

    def get_cmd(self, image_paths: Optional[List[str]] = None, session_id: Optional[str] = None, resume: bool = False) -> List[str]:
        cmd = ["qwen", "-y", "--output-format", "stream-json"]
        if self.model:
            cmd += ["-m", self.model]
        if session_id:
            if resume:
                cmd += ["--resume", session_id]
            else:
                cmd += ["--session-id", session_id]
        return cmd

    @staticmethod
    def _parse_stream_line(raw: str) -> Optional[str]:
        """Extract human-readable text from a single JSONL line.

        Returns a trimmed string for lines worth displaying, or ``None``
        to suppress the line entirely.
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
                    # Show a concise summary of the tool call
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
            parts: List[str] = []
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

        # system and other types are suppressed
        return None

    def _run_streaming_json(
        self,
        cmd: List[str],
        cwd: str,
        on_line: Callable[[str], None],
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run *cmd* and parse its stream-json output, calling *on_line* for
        each meaningful extracted line."""
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=self._env(),
        )
        stdout_lines: List[str] = []
        result_text: List[str] = []

        def _read_stdout() -> None:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                stdout_lines.append(raw_line.rstrip("\n"))
                parsed = self._parse_stream_line(raw_line)
                if parsed:
                    result_text.append(parsed)
                    for sub in parsed.splitlines():
                        if sub.strip():
                            on_line(sub)

        reader = threading.Thread(target=_read_stdout, daemon=True)
        reader.start()

        reader.join(timeout=timeout)
        if reader.is_alive():
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(cmd, timeout or 0)

        stderr_raw = proc.stderr.read() if proc.stderr else ""
        proc.wait()

        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="\n".join(result_text),
            stderr=stderr_raw,
        )

    def _interrupt_and_resume(
        self,
        proc: subprocess.Popen,  # type: ignore[type-arg]
        reader: threading.Thread,
        session_id: str,
        cwd: str,
        on_line: Callable[[str], None],
        result_text: List[str],
        hard_timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Send SIGINT to *proc*, then launch a ``--resume`` session.

        The resumed session gets a 2-minute hard timeout to wrap up.
        """
        on_line("[soft-timeout] Interrupting qwen session to resume with finish-up prompt...")
        proc.kill()
        proc.wait()
        reader.join(timeout=5)

        # Launch resume session
        resume_prompt = (
            "You have run out of time. Immediately finish up your current work: "
            "complete any in-progress file edits, ensure the code compiles/runs, "
            "and stop. Do not start any new tasks."
        )
        resume_cmd = self.get_cmd(session_id=session_id, resume=True)
        resume_cmd.append(resume_prompt)

        resume_timeout = hard_timeout or 120  # 2 min default for resume
        on_line(f"[soft-timeout] Resuming session {session_id} with {resume_timeout}s to finish...")

        return self._run_streaming_json(resume_cmd, cwd, on_line, timeout=resume_timeout)

    def run(
        self,
        cwd: str,
        full_prompt: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
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
        cmd.append(full_prompt)

        if on_line is not None and self.soft_timeout and session_id:
            # Streaming with soft timeout: manually manage the process
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=self._env(),
            )
            result_text: List[str] = []

            def _read_stdout() -> None:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    parsed = self._parse_stream_line(raw_line)
                    if parsed:
                        result_text.append(parsed)
                        for sub in parsed.splitlines():
                            if sub.strip():
                                on_line(sub)

            reader = threading.Thread(target=_read_stdout, daemon=True)
            reader.start()

            reader.join(timeout=self.soft_timeout)
            if reader.is_alive():
                # Soft timeout reached — interrupt and resume
                resume_result = self._interrupt_and_resume(
                    proc, reader, session_id, cwd, on_line, result_text,
                    hard_timeout=timeout,
                )
                # Combine output from both sessions
                combined_stdout = "\n".join(result_text) + "\n" + resume_result.stdout
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=resume_result.returncode,
                    stdout=combined_stdout,
                    stderr=resume_result.stderr,
                )

            # Finished within soft timeout
            stderr_raw = proc.stderr.read() if proc.stderr else ""
            proc.wait()
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=proc.returncode,
                stdout="\n".join(result_text),
                stderr=stderr_raw,
            )

        if on_line is not None:
            return self._run_streaming_json(cmd, cwd, on_line, timeout=timeout)

        # Non-streaming: still use stream-json and parse it
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._env())
        parsed_lines: List[str] = []
        for line in result.stdout.splitlines():
            parsed = self._parse_stream_line(line)
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
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``codex exec --full-auto`` with *full_prompt* as the prompt argument.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = self.get_cmd(image_paths)
        cmd.append(full_prompt)
        if on_line is not None:
            return self._run_streaming(cmd, "", cwd, on_line, timeout=timeout)
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
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run the Copilot CLI with *full_prompt* written to a temp file.

        Tries the ``copilot -p @<tempfile> --yolo`` invocation.  If the
        binary is not found, moves on and ultimately re-raises the last
        :exc:`FileNotFoundError` or raises :exc:`RuntimeError`.

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
                        last_result = self._run_streaming(cmd, prompt, cwd, on_line, timeout=timeout)
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


