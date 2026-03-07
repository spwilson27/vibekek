"""AI CLI runner abstractions.

Each runner wraps a specific AI command-line tool (Gemini, Claude, Copilot)
behind a common interface so that the rest of the workflow can switch
backends without changing its logic.

Runner selection is handled at construction time in :mod:`workflow_lib.cli`
and :mod:`workflow_lib.replan` via ``_make_runner()``.
"""

import subprocess
import tempfile
import threading
from typing import Callable, List, Dict, Any, Optional

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg"}


class AIRunner:
    """Abstract base class for AI CLI runners.

    Subclasses must implement :meth:`run`.
    """

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
        :param prompt: Text written to the process stdin.
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
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
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
        return ["gemini", "-y"]

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
        cmd = ["gemini", "-y"]
        if on_line is not None:
            return self._run_streaming(cmd, prompt, cwd, on_line, timeout=timeout)
        return subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout)


class ClaudeRunner(AIRunner):
    """Runner for the ``claude`` CLI (Anthropic Claude Code).

    Passes the prompt via stdin to ``claude -p --dangerously-skip-permissions``
    and captures stdout/stderr.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
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
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
        for path in (image_paths or []):
            cmd += ["--image", path]
        if on_line is not None:
            return self._run_streaming(cmd, full_prompt, cwd, on_line, timeout=timeout)
        return subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout)


class OpencodeRunner(AIRunner):
    """Runner for the ``opencode`` CLI.

    Passes the prompt via stdin to ``opencode --print --yes`` and captures
    stdout/stderr.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        cmd = ["opencode", "run"]
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
        cmd = ["opencode", "run"]
        for path in (image_paths or []):
            cmd += ["-f", path]
        if on_line is not None:
            return self._run_streaming(cmd, full_prompt, cwd, on_line, timeout=timeout)
        return subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout)


class CopilotRunner(AIRunner):
    """Runner for the GitHub Copilot CLI.

    Writes the prompt to a temporary file and passes its path as an
    ``@``-reference to ``copilot --yolo``.  Falls back gracefully when the
    binary is not found.
    """

    def get_cmd(self, image_paths: Optional[List[str]] = None) -> List[str]:
        return ["copilot", "-p", "@<prompt_file>", "--yolo"]

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
                            cmd, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout
                        )
                    if last_result.returncode == 0:
                        return last_result
                except FileNotFoundError as e:
                    last_exc = e
                    continue

        if last_result is not None:
            return last_result
        raise last_exc if last_exc is not None else RuntimeError("Failed to invoke copilot CLI")


