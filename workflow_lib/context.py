"""Project context — shared state and AI invocation façade for planning phases.

:class:`ProjectContext` is the central object threaded through every planning
phase.  It owns:

* Directory paths derived from the project root.
* The AI runner (Gemini, Claude, or Copilot) used to call external CLI tools.
* Persistent planning state read from / written to ``GEN_STATE_FILE``.
* Helper utilities for prompt formatting and workspace snapshotting.
"""

import os
import subprocess
import sys
import json
import re
from typing import Any, Callable, List, Dict, Optional

from .constants import TOOLS_DIR, INPUT_DIR, GEN_STATE_FILE, DOCS
from .runners import AIRunner, GeminiRunner, IMAGE_EXTENSIONS


class ProjectContext:
    """Shared context object for all planning-phase executions.

    Constructed once per ``workflow plan`` invocation and passed to every
    :class:`~workflow_lib.phases.BasePhase` implementation.

    :param root_dir: Absolute path to the project root directory.
    :type root_dir: str
    :param runner: AI CLI runner to use.  Defaults to
        :class:`~workflow_lib.runners.GeminiRunner` when ``None``.
    :type runner: AIRunner or None
    :param jobs: Maximum number of parallel AI invocations for phases that
        support concurrency (e.g. Phase 6 task breakdown).
    :type jobs: int
    """

    def __init__(self, root_dir: str, runner: Optional[AIRunner] = None, jobs: int = 1, dashboard: Optional[Any] = None):
        self.root_dir = root_dir
        self.jobs = jobs
        self.plan_dir = os.path.join(root_dir, "docs", "plan")
        self.specs_dir = os.path.join(self.plan_dir, "specs")
        self.research_dir = os.path.join(self.plan_dir, "research")
        self.prompts_dir = os.path.join(TOOLS_DIR, "prompts")
        self.state_file = GEN_STATE_FILE
        self.input_dir = INPUT_DIR
        
        self.requirements_dir = os.path.join(self.plan_dir, "requirements")
        
        self.runner = runner or GeminiRunner()
        self.dashboard = dashboard
        self.ignore_sandbox = False
        self.current_phase: str = ""
        self.agent_timeout: Optional[int] = None

        # Ensures directories exist
        os.makedirs(self.specs_dir, exist_ok=True)
        os.makedirs(self.research_dir, exist_ok=True)
        os.makedirs(self.requirements_dir, exist_ok=True)

        self.shared_components_file = os.path.join(self.plan_dir, "shared_components.md")
        self.state = self._load_state()
        self.image_paths = self._load_images()
        self.description_ctx = self._load_description()

    def _load_state(self) -> Dict[str, Any]:
        """Load planning state from disk, merging with safe defaults.

        :returns: State dict with all known keys initialised to falsy defaults,
            updated from the persisted JSON if the state file exists.
        :rtype: dict
        """
        state: Dict[str, Any] = {
            "generated": [],
            "fleshed_out": [],
            "fleshed_out_headers": {},
            "extracted_requirements": [],
            "final_review_completed": False,
            "requirements_extracted": False,
            "requirements_merged": False,
            "requirements_ordered": False,
            "phases_completed": False,
            "tasks_completed": False,
            "tasks_reviewed": False,
            "cross_phase_reviewed": False,
            "tasks_generated": [],
            "dag_completed": False,
            "dag_reviewed": False,
        }
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                try:
                    loaded = json.load(f)
                    state.update(loaded)
                except json.JSONDecodeError:
                    pass
        return state

    def save_state(self) -> None:
        """Persist the current :attr:`state` dict to :attr:`state_file`."""
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=4)

    def _load_images(self) -> List[str]:
        """Return absolute paths to all image files in the ``input/`` directory.

        Files are sorted by name.  Non-existent directories return an empty list.

        :returns: Sorted list of absolute image file paths.
        :rtype: list[str]
        """
        if not os.path.isdir(self.input_dir):
            return []
        return sorted(
            os.path.join(self.input_dir, f)
            for f in os.listdir(self.input_dir)
            if os.path.isfile(os.path.join(self.input_dir, f))
            and os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS
        )

    def _load_description(self) -> str:
        """Read all non-image files from the ``input/`` directory and concatenate them.

        Every text file in ``.tools/input/`` is included, sorted by filename,
        with each file's content preceded by a ``## <filename>`` header so the
        AI can distinguish between multiple input documents.  Image files are
        excluded here and attached separately via :attr:`image_paths`.

        :raises SystemExit: If the ``input/`` directory does not exist or
            contains no text files.
        :returns: Concatenated contents of all text input files.
        :rtype: str
        """
        if not os.path.isdir(self.input_dir):
            print(f"Error: {self.input_dir} not found.")
            sys.exit(1)
        files = sorted(
            f for f in os.listdir(self.input_dir)
            if os.path.isfile(os.path.join(self.input_dir, f))
            and os.path.splitext(f)[1].lower() not in IMAGE_EXTENSIONS
        )
        if not files:
            print(f"Error: No input files found in {self.input_dir}.")
            sys.exit(1)
        parts = []
        for filename in files:
            filepath = os.path.join(self.input_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                parts.append(f"<file name=\"{filename}\">\n{f.read()}\n</file>")
        return "\n\n".join(parts)

    def load_shared_components(self) -> str:
        """Return the contents of ``docs/plan/shared_components.md``.

        :returns: File contents, or an empty string when the file does not
            exist.
        :rtype: str
        """
        if os.path.exists(self.shared_components_file):
            with open(self.shared_components_file, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def load_prompt(self, filename: str) -> str:
        """Load a prompt template by filename from the prompts directory.

        :param filename: Filename relative to ``TOOLS_DIR/prompts/``, e.g.
            ``"spec_prd.md"``.
        :type filename: str
        :raises SystemExit: If the prompt file does not exist.
        :returns: Stripped text content of the prompt file.
        :rtype: str
        """
        prompt_path = os.path.join(self.prompts_dir, filename)
        if not os.path.exists(prompt_path):
            print(f"Error: Prompt template {prompt_path} not found.")
            sys.exit(1)
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def format_prompt(self, tmpl: str, **kwargs: Any) -> str:
        """Perform a single-pass substitution of ``{key}`` placeholders.

        A single regex pass prevents values that themselves contain
        ``{other_key}`` patterns from being double-substituted.

        :param tmpl: Template string containing ``{key}`` placeholders.
        :type tmpl: str
        :param kwargs: Keyword arguments whose names match placeholder keys.
        :returns: Template with all matching placeholders replaced.
        :rtype: str
        """
        pattern = re.compile(r'\{(' + '|'.join(re.escape(k) for k in kwargs) + r')\}')
        result = pattern.sub(lambda m: str(kwargs[m.group(1)]), tmpl)
        return result

    def format_prompt_for(self, prompt_filename: str, tmpl: str, **kwargs: Any) -> str:
        """Like :meth:`format_prompt` but also validates required placeholders.

        Uses the prompt registry to check that all required placeholders for
        *prompt_filename* were provided in *kwargs*.  Missing ones are logged
        as warnings.

        :param prompt_filename: The prompt file name (e.g. ``"spec_prd.md"``).
        :param tmpl: Template string.
        :param kwargs: Substitution values.
        :returns: Formatted prompt string.
        """
        from .prompt_registry import get_required_placeholders
        required = get_required_placeholders(prompt_filename)
        missing = required - set(kwargs.keys())
        if missing:
            print(f"  [WARNING] Missing required placeholders for {prompt_filename}: "
                  f"{', '.join('{' + p + '}' for p in sorted(missing))}")
        return self.format_prompt(tmpl, **kwargs)

    def get_document_path(self, doc: Dict[str, Any]) -> str:
        """Return the absolute filesystem path for a planning document.

        :param doc: Document descriptor from :data:`~.constants.DOCS`.
        :type doc: dict
        :returns: Absolute path under ``docs/plan/specs/`` or
            ``docs/plan/research/``.
        :rtype: str
        """
        out_folder = "specs" if doc["type"] == "spec" else "research"
        return os.path.join(self.plan_dir, out_folder, f"{doc['id']}.md")

    def get_target_path(self, doc: Dict[str, Any]) -> str:
        """Return the project-root-relative path for a planning document.

        Used in prompt templates so the AI knows where to write the file.

        :param doc: Document descriptor from :data:`~.constants.DOCS`.
        :type doc: dict
        :returns: Relative path such as ``"docs/plan/specs/1_prd.md"``.
        :rtype: str
        """
        out_folder = "docs/plan/specs" if doc["type"] == "spec" else "docs/plan/research"
        return f"{out_folder}/{doc['id']}.md"

    def get_accumulated_context(
        self,
        current_doc: Dict[str, Any],
        include_research: bool = True,
    ) -> str:
        """Build an XML-tagged context string from all documents preceding *current_doc*.

        Research documents can be excluded when generating spec documents to
        prevent hallucinated market data from influencing architectural choices.

        :param current_doc: The document currently being generated.  Documents
            before this one in :data:`~.constants.DOCS` are included.
        :type current_doc: dict
        :param include_research: When ``False``, research-type documents are
            skipped.
        :type include_research: bool
        :returns: Concatenated ``<previous_document>`` XML blocks for each
            existing preceding document.
        :rtype: str
        """
        accumulated_context = ""
        for prev_doc in DOCS:
            if prev_doc == current_doc:
                break
            if not include_research and prev_doc["type"] == "research":
                continue
            prev_file = self.get_document_path(prev_doc)
            if os.path.exists(prev_file):
                with open(prev_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    accumulated_context += (
                        f'\n\n<previous_document name="{prev_doc["name"]}">'
                        f'\n{content}\n</previous_document>\n'
                    )
        return accumulated_context

    def get_workspace_snapshot(self) -> Dict[str, float]:
        """Capture a modification-time snapshot of all workspace files.

        Skips ``.git/`` trees and ``.DS_Store`` files.

        :returns: Mapping of absolute file path to ``os.path.getmtime`` value.
        :rtype: dict
        """
        snapshot: Dict[str, float] = {}
        for root, dirs, files in os.walk(self.root_dir):
            if ".git" in root:
                continue
            for file in files:
                if file == ".DS_Store":
                    continue
                filepath = os.path.join(root, file)
                try:
                    snapshot[filepath] = os.path.getmtime(filepath)
                except OSError:
                    pass
        return snapshot

    def stage_changes(self, file_paths: List[str]) -> None:
        """Stage the given paths with ``git add``.

        :param file_paths: List of absolute or relative paths to stage.
            Empty paths and ``None`` values are filtered out.
        :type file_paths: list[str]
        """
        if not file_paths:
            return
        clean_paths = [os.path.abspath(p) for p in file_paths if p]
        subprocess.run(["git", "add"] + clean_paths, cwd=self.root_dir, check=False)

    def verify_changes(self, before: Dict[str, float], allowed_files: List[str]) -> None:
        """Enforce sandbox constraints after an AI invocation.

        Compares the current workspace snapshot against *before* and calls
        :func:`sys.exit(1) <sys.exit>` if any new, modified, or deleted file
        is not in *allowed_files* (or under an allowed directory).

        Internal files (state file, debug files) are always permitted.

        :param before: Snapshot taken before the AI ran, as returned by
            :meth:`get_workspace_snapshot`.
        :type before: dict
        :param allowed_files: Paths that the AI is permitted to create or
            modify.  A path ending with :data:`os.sep` is treated as an
            allowed directory.
        :type allowed_files: list[str]
        :raises SystemExit: On any sandbox violation.
        """
        if self.ignore_sandbox:
            return
        after = self.get_workspace_snapshot()
        allowed_set = set(os.path.abspath(f) for f in allowed_files)
        allowed_dirs = [
            os.path.abspath(f)
            for f in allowed_files
            if os.path.isdir(f) or f.endswith(os.sep)
        ]

        for path in after:
            if path not in before or after[path] > before.get(path, 0):
                abs_path = os.path.abspath(path)
                is_allowed = abs_path in allowed_set
                if not is_allowed:
                    for d in allowed_dirs:
                        if abs_path.startswith(d if d.endswith(os.sep) else d + os.sep):
                            is_allowed = True
                            break
                if not is_allowed:
                    if abs_path in [
                        os.path.abspath(self.state_file),
                        os.path.abspath(os.path.join(self.root_dir, ".last_failed_command.sh")),
                        os.path.abspath(os.path.join(self.root_dir, ".last_failed_prompt.txt")),
                        os.path.abspath(os.path.join(self.root_dir, "plan_workflow.log")),
                        os.path.abspath(os.path.join(self.root_dir, "run_workflow.log")),
                    ]:
                        continue
                    print(f"\n[SANDBOX VIOLATION] Unauthorized change detected: {path}")
                    print(f"The agent was only allowed to modify: {allowed_files}")
                    sys.exit(1)

        for path in before:
            if path not in after:
                abs_path = os.path.abspath(path)
                is_allowed = abs_path in allowed_set
                if not is_allowed:
                    for d in allowed_dirs:
                        if abs_path.startswith(d if d.endswith(os.sep) else d + os.sep):
                            is_allowed = True
                            break
                if not is_allowed:
                    print(f"\n[SANDBOX VIOLATION] Unauthorized deletion detected: {path}")
                    sys.exit(1)

    def strip_thinking_tags(self, filepath: str) -> None:
        """Remove ``<thinking>…</thinking>`` blocks from a file or directory.

        When *filepath* is a directory, all ``*.md`` files within it are
        processed recursively.  The file is only rewritten when the content
        actually changes.

        :param filepath: Absolute path to a file or directory to process.
        :type filepath: str
        """
        if not os.path.exists(filepath):
            return

        if os.path.isdir(filepath):
            for filename in os.listdir(filepath):
                if filename.endswith(".md"):
                    self.strip_thinking_tags(os.path.join(filepath, filename))
            return

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        new_content = re.sub(r'<thinking>.*?</thinking>\s*', '', content, flags=re.DOTALL)

        if new_content != content:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(new_content)

    def run_ai(
        self,
        full_prompt: str,
        allowed_files: Optional[List[str]] = None,
        sandbox: bool = True,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Invoke the configured AI runner and optionally enforce sandbox rules.

        Takes a before-snapshot, runs the AI, then (when *allowed_files* is
        provided) verifies that only the declared paths were touched and strips
        any ``<thinking>`` tags from them.

        :param full_prompt: Fully rendered prompt string passed to the runner.
        :type full_prompt: str
        :param allowed_files: Paths the AI is permitted to create or modify.
            Pass ``None`` to skip both sandbox verification and tag stripping.
        :type allowed_files: list[str] or None
        :param sandbox: When ``True`` (default), :meth:`verify_changes` is
            called after the AI runs.  Set to ``False`` for phases that
            intentionally write to many files.
        :type sandbox: bool
        :param timeout: Maximum seconds to wait for the AI process.
            ``None`` means no limit.
        :type timeout: int or None
        :returns: The completed process result from the underlying runner.
        :rtype: subprocess.CompletedProcess
        """
        before = self.get_workspace_snapshot()
        on_line: Optional[Callable[[str], None]] = None
        if self.dashboard is not None:
            _dash = self.dashboard
            _phase = self.current_phase
            _task_id = f"plan/{_phase}" if _phase else ""
            def on_line(line: str) -> None:
                prefixed = f"[{_phase}] {line}" if _phase else line
                _dash.log(prefixed)
                if _task_id:
                    _dash.update_last_line(_task_id, line)
        effective_timeout = timeout if timeout is not None else self.agent_timeout
        result = self.runner.run(self.root_dir, full_prompt, self.image_paths, on_line=on_line, timeout=effective_timeout)
        if result.returncode != 0:
            self._write_last_failed_command(full_prompt)
        if allowed_files is not None:
            if sandbox:
                self.verify_changes(before, allowed_files)
            for f in allowed_files:
                self.strip_thinking_tags(os.path.abspath(f))
        return result

    def _write_last_failed_command(self, full_prompt: str) -> None:
        """Write .last_failed_prompt.txt and .last_failed_command.sh for debugging."""
        import shlex
        prompt_file = os.path.join(self.root_dir, ".last_failed_prompt.txt")
        script_file = os.path.join(self.root_dir, ".last_failed_command.sh")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(full_prompt)
        cmd = self.runner.get_cmd(self.image_paths)
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        with open(script_file, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write(f"# Last failed workflow command\n")
            f.write(f"cd {shlex.quote(self.root_dir)}\n")
            f.write(f"{cmd_str} < .last_failed_prompt.txt\n")
        os.chmod(script_file, 0o755)
        print(f"   -> Debug: saved .last_failed_command.sh and .last_failed_prompt.txt")

    def run_gemini(
        self,
        full_prompt: str,
        allowed_files: Optional[List[str]] = None,
        sandbox: bool = True,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Alias for :meth:`run_ai` retained for backwards compatibility.

        :param full_prompt: Fully rendered prompt string.
        :type full_prompt: str
        :param allowed_files: Paths the AI is permitted to touch.
        :type allowed_files: list[str] or None
        :param sandbox: Enforce sandbox rules when ``True``.
        :type sandbox: bool
        :param timeout: Maximum seconds to wait for the AI process.
        :type timeout: int or None
        :returns: The completed process result.
        :rtype: subprocess.CompletedProcess
        """
        return self.run_ai(full_prompt, allowed_files, sandbox, timeout=timeout)

    def count_task_files(self, directory: str) -> int:
        count = 0
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.endswith(".md") and f not in ("review_summary.md", "cross_phase_review_summary.md",
                                                     "reorder_tasks_summary.md",
                                                     "cross_phase_review_summary_pass_1.md",
                                                     "cross_phase_review_summary_pass_2.md",
                                                     "reorder_tasks_summary_pass_1.md",
                                                     "reorder_tasks_summary_pass_2.md"):
                    count += 1
        return count

    def parse_markdown_headers(self, filepath: str) -> List[str]:
        headers = []
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    # Only want to capture h1 and h2
                    if re.match(r'^#{1,2}\s+', line):
                        headers.append(line.strip())
        return headers

