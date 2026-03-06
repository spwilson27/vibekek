"""AI CLI runner abstractions.

Each runner wraps a specific AI command-line tool (Gemini, Claude, Copilot)
behind a common interface so that the rest of the workflow can switch
backends without changing its logic.

Runner selection is handled at construction time in :mod:`workflow_lib.cli`
and :mod:`workflow_lib.replan` via ``_make_runner()``.
"""

import os
import subprocess
import tempfile
import threading
from typing import Callable, List, Dict, Any, Optional

from .constants import ignore_file_lock

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg"}


class AIRunner:
    """Abstract base class for AI CLI runners.

    Subclasses must implement :meth:`run` and the :attr:`ignore_file_name`
    property.  The shared :meth:`write_ignore_file` helper updates the
    runner's ignore file atomically under :data:`~.constants.ignore_file_lock`
    so that concurrent agent threads do not produce torn writes.
    """

    def write_ignore_file(self, ignore_file: str, ignore_content: str) -> None:
        """Write *ignore_content* to *ignore_file* if the contents differ.

        Acquires :data:`~.constants.ignore_file_lock` before any I/O so that
        concurrent runners sharing a working directory cannot race on the same
        ignore file.

        :param ignore_file: Absolute path to the ignore file (e.g.
            ``/project/.geminiignore``).
        :type ignore_file: str
        :param ignore_content: Full text content to write.
        :type ignore_content: str
        """
        with ignore_file_lock:
            should_write = True
            if os.path.exists(ignore_file):
                with open(ignore_file, "r", encoding="utf-8") as f:
                    if f.read() == ignore_content:
                        should_write = False
            if should_write:
                with open(ignore_file, "w", encoding="utf-8") as f:
                    f.write(ignore_content)

    def _run_streaming(
        self,
        cmd: List[str],
        prompt: str,
        cwd: str,
        on_line: Callable[[str], None],
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run *cmd* with *prompt* on stdin, calling *on_line* for each output line.

        Uses :class:`subprocess.Popen` to stream stdout in real time.  stderr is
        collected and appended to stdout in the returned object (so callers that
        check ``result.stdout`` still see it).

        :param cmd: Command list to execute.
        :param prompt: Text written to the process stdin.
        :param cwd: Working directory for the subprocess.
        :param on_line: Callback invoked once per output line (newline stripped).
        :returns: Completed process with combined stdout (streamed lines) and
            stderr captured separately.
        :rtype: subprocess.CompletedProcess
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

        reader.join()
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
        ignore_content: str,
        ignore_file: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Invoke the AI CLI and return its completed process result.

        :param cwd: Working directory for the subprocess.
        :type cwd: str
        :param full_prompt: Full rendered prompt to pass to the CLI.
        :type full_prompt: str
        :param ignore_content: Content for the runner's ignore file.
        :type ignore_content: str
        :param ignore_file: Path to the ignore file to write before running.
        :type ignore_file: str
        :param image_paths: Optional list of absolute paths to image files to
            attach to the request.  How images are delivered depends on the
            backend — see concrete subclass implementations.
        :type image_paths: list[str] or None
        :raises NotImplementedError: Always — subclasses must override this.
        :returns: The completed subprocess result.
        :rtype: subprocess.CompletedProcess
        """
        raise NotImplementedError()

    @property
    def ignore_file_name(self) -> str:
        """Filename (not path) of the ignore file for this runner.

        :raises NotImplementedError: Always — subclasses must override this.
        :rtype: str
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
        ignore_content: str,
        ignore_file: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``gemini -y`` with *full_prompt* on stdin.

        Images are delivered by appending ``@<absolute_path>`` file-reference
        lines to the prompt, which the Gemini CLI resolves as attachments.

        :param cwd: Working directory for the subprocess.
        :type cwd: str
        :param full_prompt: Prompt text written to stdin.
        :type full_prompt: str
        :param ignore_content: Content written to ``.geminiignore``.
        :type ignore_content: str
        :param ignore_file: Path to the ``.geminiignore`` file.
        :type ignore_file: str
        :param image_paths: Absolute paths to image files to attach.
        :type image_paths: list[str] or None
        :param on_line: Optional callback invoked with each output line for
            real-time streaming.  When provided, uses :class:`subprocess.Popen`
            instead of :func:`subprocess.run`.
        :type on_line: callable or None
        :returns: Completed process with ``returncode``, ``stdout``, ``stderr``.
        :rtype: subprocess.CompletedProcess
        """
        self.write_ignore_file(ignore_file, ignore_content)
        prompt = full_prompt
        if image_paths:
            refs = "\n".join(f"@{p}" for p in image_paths)
            prompt = f"{prompt}\n\n{refs}"
        cmd = ["gemini", "-y"]
        if on_line is not None:
            return self._run_streaming(cmd, prompt, cwd, on_line)
        return subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True)

    @property
    def ignore_file_name(self) -> str:
        """Return ``".geminiignore"``."""
        return ".geminiignore"


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
        ignore_content: str,
        ignore_file: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``claude -p --dangerously-skip-permissions`` with *full_prompt* on stdin.

        Images are delivered via ``--image <path>`` flags appended to the
        command, one flag pair per image.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        self.write_ignore_file(ignore_file, ignore_content)
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
        for path in (image_paths or []):
            cmd += ["--image", path]
        if on_line is not None:
            return self._run_streaming(cmd, full_prompt, cwd, on_line)
        return subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True)

    @property
    def ignore_file_name(self) -> str:
        """Return ``".claudeignore"``."""
        return ".claudeignore"


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
        ignore_content: str,
        ignore_file: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``opencode run`` with *full_prompt* on stdin.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.
        """
        cmd = ["opencode", "run"]
        for path in (image_paths or []):
            cmd += ["-f", path]
        if on_line is not None:
            return self._run_streaming(cmd, full_prompt, cwd, on_line)
        return subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True)

    @property
    def ignore_file_name(self) -> str:
        """Return ``".opencodeignore"``."""
        return ".opencodeignore"


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
        ignore_content: str,
        ignore_file: str,
        image_paths: Optional[List[str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run the Copilot CLI with *full_prompt* written to a temp file.

        Tries the ``copilot -p @<tempfile> --yolo`` invocation.  If the
        binary is not found, moves on and ultimately re-raises the last
        :exc:`FileNotFoundError` or raises :exc:`RuntimeError`.

        :param on_line: Optional streaming callback; see :meth:`AIRunner.run`.

        :param cwd: Working directory for the subprocess.
        :type cwd: str
        :param full_prompt: Prompt text written to the temporary file.
        :type full_prompt: str
        :param ignore_content: Content written to ``.copilotignore``.
        :type ignore_content: str
        :param ignore_file: Path to the ``.copilotignore`` file.
        :type ignore_file: str
        :param image_paths: Absolute paths to image files to attach.  Appended
            as ``@<path>`` references in the prompt file.
        :type image_paths: list[str] or None
        :returns: Completed process with ``returncode``, ``stdout``, ``stderr``.
        :rtype: subprocess.CompletedProcess
        :raises RuntimeError: When no CLI candidate succeeds and no
            :exc:`FileNotFoundError` was captured.
        """
        self.write_ignore_file(ignore_file, ignore_content)
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
                        last_result = self._run_streaming(cmd, prompt, cwd, on_line)
                    else:
                        last_result = subprocess.run(
                            cmd, input=prompt, cwd=cwd, capture_output=True, text=True
                        )
                    if last_result.returncode == 0:
                        return last_result
                except FileNotFoundError as e:
                    last_exc = e
                    continue

        if last_result is not None:
            return last_result
        raise last_exc if last_exc is not None else RuntimeError("Failed to invoke copilot CLI")

    @property
    def ignore_file_name(self) -> str:
        """Return ``".copilotignore"``."""
        return ".copilotignore"


