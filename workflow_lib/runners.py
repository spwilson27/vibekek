import os
import subprocess
import tempfile
from typing import List, Dict, Any, Optional

from .constants import ignore_file_lock
class AIRunner:
    """Abstract base for AI CLI runners."""
    def write_ignore_file(self, ignore_file: str, ignore_content: str):
        with ignore_file_lock:
            should_write = True
            if os.path.exists(ignore_file):
                with open(ignore_file, "r", encoding="utf-8") as f:
                    if f.read() == ignore_content:
                        should_write = False
            if should_write:
                with open(ignore_file, "w", encoding="utf-8") as f:
                    f.write(ignore_content)

    def run(self, cwd: str, full_prompt: str, ignore_content: str, ignore_file: str) -> subprocess.CompletedProcess:
        raise NotImplementedError()

    @property
    def ignore_file_name(self) -> str:
        raise NotImplementedError()


class GeminiRunner(AIRunner):
    """Wraps the gemini CLI subprocess call."""
    def run(self, cwd: str, full_prompt: str, ignore_content: str, ignore_file: str) -> subprocess.CompletedProcess:
        self.write_ignore_file(ignore_file, ignore_content)
        return subprocess.run(
            ["gemini", "-y"],
            input=full_prompt,
            cwd=cwd,
            capture_output=True,
            text=True
        )

    @property
    def ignore_file_name(self) -> str:
        return ".geminiignore"


class ClaudeRunner(AIRunner):
    """Wraps the claude CLI subprocess call."""
    def run(self, cwd: str, full_prompt: str, ignore_content: str, ignore_file: str) -> subprocess.CompletedProcess:
        self.write_ignore_file(ignore_file, ignore_content)
        return subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions"],
            input=full_prompt,
            cwd=cwd,
            capture_output=True,
            text=True
        )

    @property
    def ignore_file_name(self) -> str:
        return ".claudeignore"


class CopilotRunner(AIRunner):
    """Wraps the GitHub Copilot CLI subprocess call."""
    def run(self, cwd: str, full_prompt: str, ignore_content: str, ignore_file: str) -> subprocess.CompletedProcess:
        self.write_ignore_file(ignore_file, ignore_content)

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=True) as f:
            f.write(full_prompt)
            #print(full_prompt)
            prompt_file = f.name
            candidates = [
                ["copilot", "-p", f"Follow the instructions in @{prompt_file}", "--yolo"],
            ]
            last_exc = None
            last_result = None
            for cmd in candidates:
                try:
                    last_result = subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True)
                    # If the command ran (found) and returned 0, return immediately.
                    if last_result.returncode == 0:
                        return last_result
                except FileNotFoundError as e:
                    last_exc = e
                    continue

        # If none succeeded, return the last result if available, else raise the FileNotFoundError
        if last_result is not None:
            return last_result
        raise last_exc if last_exc is not None else RuntimeError("Failed to invoke copilot CLI")

    @property
    def ignore_file_name(self) -> str:
        return ".copilotignore"


