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
from typing import Any, Callable, List, Dict, Optional, Union

from .constants import TOOLS_DIR, INPUT_DIR, GEN_STATE_FILE, DOCS
from .config import get_context_limit
from .runners import AIRunner, GeminiRunner, IMAGE_EXTENSIONS


def build_context_block(
    entries: List[Dict[str, Any]],
    word_budget: int,
    label: str = "",
) -> str:
    """Format a list of file entries into a budget-bounded context string.

    Applies :func:`fit_lines_to_budget` to find the maximum lines-per-file
    that keeps the total word count within *word_budget*, then renders each
    entry as ``### {rel}\\n{content}``, appending a truncation hint when lines
    are omitted.

    :param entries: File entries, each a dict with ``"rel"`` (project-root-
        relative path) and ``"lines"`` (list of text lines).
    :type entries: list[dict]
    :param word_budget: Maximum total words for the formatted block.
    :type word_budget: int
    :param label: Optional label used in the progress log line.
    :type label: str
    :returns: Formatted, truncated context string, or ``""`` when *entries*
        is empty.
    :rtype: str
    """
    if not entries:
        return ""
    # ~15 words overhead per file (header line, path hint, newlines)
    header_words = len(entries) * 15
    lines_limit = fit_lines_to_budget(
        [e["lines"] for e in entries], max(word_budget - header_words, 1)
    )
    total = sum(
        len(line.split()) for e in entries for line in e["lines"][:lines_limit]
    ) + header_words
    desc = f"[{label}] " if label else ""
    print(f"   -> Context {desc}{len(entries)} file(s), "
          f"{lines_limit} lines/file, ~{total} words")

    parts = []
    for e in entries:
        preview = "".join(e["lines"][:lines_limit]).rstrip()
        trunc = ""
        if len(e["lines"]) > lines_limit:
            trunc = (f"\n... ({len(e['lines']) - lines_limit} more lines"
                     f" — read full content from: {e['rel']})\n")
        parts.append(f"### {e['rel']}\n{preview}{trunc}")
    return "\n\n".join(parts)


def fit_lines_to_budget(entries_lines: List[List[str]], word_budget: int) -> int:
    """Return the maximum lines-per-entry such that total word count ≤ *word_budget*.

    Uses a binary search over the uniform line limit applied to all entries,
    matching each entry's lines from the start.  All entries are truncated to
    the same limit so no single entry dominates the budget.

    :param entries_lines: Each element is the list of lines for one document or
        file.  Empty entries are allowed and contribute zero words.
    :type entries_lines: list[list[str]]
    :param word_budget: Maximum total words across all entries.
    :type word_budget: int
    :returns: The largest per-entry line limit that keeps the total within
        *word_budget*, or ``0`` when *entries_lines* is empty.
    :rtype: int
    """
    if not entries_lines:
        return 0
    max_lines = max((len(e) for e in entries_lines), default=0)
    if max_lines == 0:
        return 0

    def _count(limit: int) -> int:
        return sum(len(line.split()) for e in entries_lines for line in e[:limit])

    if _count(max_lines) <= word_budget:
        return max_lines

    lo, hi = 1, max_lines
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _count(mid) <= word_budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


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
        self.summaries_dir = os.path.join(self.plan_dir, "summaries")

        self.runner = runner or GeminiRunner()
        self.dashboard = dashboard
        self.ignore_sandbox = False
        self.current_phase: str = ""
        self.agent_timeout: Optional[int] = None

        # Ensures directories exist
        os.makedirs(self.specs_dir, exist_ok=True)
        os.makedirs(self.research_dir, exist_ok=True)
        os.makedirs(self.requirements_dir, exist_ok=True)
        os.makedirs(self.summaries_dir, exist_ok=True)

        self.shared_components_file = os.path.join(self.plan_dir, "shared_components.md")
        self.state = self._load_state()
        self.image_paths = self._load_images()
        self.description_ctx = self._load_description()

    def prompt_input(self, message: str) -> str:
        """Prompt for user input, using the dashboard if available.

        When a dashboard is active this pauses the live display and renders
        a prominent ``INPUT REQUIRED`` banner so the user notices the prompt.

        :param message: The prompt text to display.
        :returns: The user's input string.
        """
        if self.dashboard is not None:
            return self.dashboard.prompt_input(message)
        print("\n" + "!" * 70)
        print("  ⚠  INPUT REQUIRED")
        print("!" * 70)
        print(f"  {message}")
        print("!" * 70)
        return input("> ")

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

    def build_context_strings(
        self,
        context_files: Dict[str, Union[str, List[str]]],
        extra_words: int = 0,
    ) -> Dict[str, str]:
        """Load files for each context placeholder and apply budget-aware truncation.

        The total word budget across all context groups is
        ``context_limit - extra_words``, split equally across groups.  Within
        each group a binary search finds the maximum lines-per-file that fits
        the share, matching the pattern used by :func:`~phases._build_tasks_content`.

        Each value in *context_files* may be:

        * A single file path — read directly.
        * A directory path — all ``.md``, ``.txt``, and ``.json`` files
          found via :func:`os.walk` (sorted).
        * A list of any mix of the above.

        :param context_files: Mapping of template placeholder name to the
            path(s) whose content should fill it.
        :type context_files: dict[str, str | list[str]]
        :param extra_words: Words already consumed by the template text and
            small static params — subtracted from the total budget before
            distributing across groups.
        :type extra_words: int
        :returns: Mapping of placeholder name to formatted, truncated content
            ready for :meth:`format_prompt`.
        :rtype: dict[str, str]
        """
        max_words = get_context_limit()
        available = max(max_words - extra_words, 0)

        # Collect file entries per group
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for key, paths in context_files.items():
            if isinstance(paths, str):
                paths = [paths]
            entries: List[Dict[str, Any]] = []
            for path in paths:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as f:
                        entries.append({
                            "rel": os.path.relpath(path, self.root_dir),
                            "lines": f.readlines(),
                        })
                elif os.path.isdir(path):
                    for dirpath, _, filenames in os.walk(path):
                        for fname in sorted(filenames):
                            if fname.endswith((".md", ".txt", ".json")):
                                fp = os.path.join(dirpath, fname)
                                with open(fp, "r", encoding="utf-8") as f:
                                    entries.append({
                                        "rel": os.path.relpath(fp, self.root_dir),
                                        "lines": f.readlines(),
                                    })
            groups[key] = entries

        nonempty_count = sum(1 for v in groups.values() if v)
        if nonempty_count == 0:
            return {k: "" for k in context_files}

        per_group = max(available // nonempty_count, 1)

        return {
            key: build_context_block(entries, per_group, label=key)
            for key, entries in groups.items()
        }

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

    def get_summary_path(self, doc: Dict[str, Any]) -> str:
        """Return the absolute path for a document's summary file.

        :param doc: Document descriptor from :data:`~.constants.DOCS`.
        :returns: Absolute path under ``docs/plan/summaries/``.
        """
        return os.path.join(self.summaries_dir, f"{doc['id']}.md")

    def get_summary_target_path(self, doc: Dict[str, Any]) -> str:
        """Return the project-root-relative path for a document's summary.

        :param doc: Document descriptor from :data:`~.constants.DOCS`.
        :returns: Relative path such as ``"docs/plan/summaries/1_prd.md"``.
        """
        return f"docs/plan/summaries/{doc['id']}.md"

    def get_accumulated_context(
        self,
        current_doc: Dict[str, Any],
        include_research: bool = True,
        extra_words: int = 0,
    ) -> str:
        """Build an XML-tagged context string from all documents preceding *current_doc*.

        When a summary exists for a preceding document (in ``docs/plan/summaries/``),
        the summary is used instead of the full document to keep the prompt within
        model input limits.

        Documents are truncated to a uniform line limit (found via binary search)
        so the total stays within ``context_limit``.  Truncated documents include
        a file-path hint so the agent can read the full content if needed.

        Research documents can be excluded when generating spec documents to
        prevent hallucinated market data from influencing architectural choices.

        :param current_doc: The document currently being generated.  Documents
            before this one in :data:`~.constants.DOCS` are included.
        :type current_doc: dict
        :param include_research: When ``False``, research-type documents are
            skipped.
        :type include_research: bool
        :param extra_words: Words reserved for the rest of the prompt (template,
            description context, etc.) so the accumulated context leaves room.
        :type extra_words: int
        :returns: Concatenated ``<previous_document>`` XML blocks for each
            existing preceding document.
        :rtype: str
        """
        # Collect raw document entries: (doc, lines[], file_path, is_summary)
        entries: list = []
        for prev_doc in DOCS:
            if prev_doc == current_doc:
                break
            if not include_research and prev_doc["type"] == "research":
                continue
            summary_file = self.get_summary_path(prev_doc)
            full_file = self.get_document_path(prev_doc)
            if os.path.exists(summary_file):
                with open(summary_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                # Store relative path for the hint
                rel = os.path.relpath(summary_file, self.root_dir)
                entries.append((prev_doc, lines, rel, True))
            elif os.path.exists(full_file):
                with open(full_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                rel = os.path.relpath(full_file, self.root_dir)
                entries.append((prev_doc, lines, rel, False))

        if not entries:
            return ""

        # Budget: context_limit minus extra_words minus per-doc header overhead
        max_words = get_context_limit()
        header_words = len(entries) * 20  # XML tags, name attr, etc.
        available_words = max(max_words - extra_words - header_words, 0)

        lines_per_doc = fit_lines_to_budget(
            [e[1] for e in entries], available_words
        )
        total_words = sum(
            len(line.split())
            for _, lines, _, _ in entries
            for line in lines[:lines_per_doc]
        ) + header_words
        print(f"   -> Accumulated context: {len(entries)} docs, "
              f"{lines_per_doc} lines/doc, ~{total_words} words")

        # Build the context string
        parts: list = []
        for prev_doc, lines, rel_path, is_summary in entries:
            doc_type = ' type="summary"' if is_summary else ""
            preview = lines[:lines_per_doc]
            content = "".join(preview)
            truncation_note = ""
            if len(lines) > lines_per_doc:
                truncation_note = (
                    f"\n... ({len(lines) - lines_per_doc} more lines "
                    f"— read full content from: {rel_path})\n"
                )
            parts.append(
                f'\n\n<previous_document name="{prev_doc["name"]}"{doc_type} '
                f'path="{rel_path}">'
                f'\n{content.rstrip()}{truncation_note}\n</previous_document>\n'
            )

        return "".join(parts)

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
        *,
        context_files: Optional[Dict[str, Union[str, List[str]]]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Invoke the configured AI runner and optionally enforce sandbox rules.

        Takes a before-snapshot, runs the AI, then (when *allowed_files* is
        provided) verifies that only the declared paths were touched and strips
        any ``<thinking>`` tags from them.

        When *context_files* or *params* is provided, *full_prompt* is treated
        as a prompt **template filename** (loaded via :meth:`load_prompt`).
        Context files are read from disk and truncated to fit within
        ``context_limit`` via :meth:`build_context_strings`, then substituted
        into the template together with any small static *params*.

        :param full_prompt: Fully rendered prompt string, **or** a prompt
            template filename when *context_files* / *params* are given.
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
        :param context_files: Mapping of template placeholder name to file or
            directory path(s) whose content should fill that placeholder.
            Content is automatically truncated to respect ``context_limit``.
        :type context_files: dict[str, str | list[str]] or None
        :param params: Small static template values (e.g. file paths, phase
            IDs) that do not require truncation.
        :type params: dict[str, Any] or None
        :returns: The completed process result from the underlying runner.
        :rtype: subprocess.CompletedProcess
        """
        if context_files is not None or params is not None:
            tmpl = self.load_prompt(full_prompt)
            # Reserve words for template boilerplate and small static params
            extra = len(tmpl.split()) + sum(
                len(str(v).split()) for v in (params or {}).values()
            )
            ctx_strs = self.build_context_strings(context_files or {}, extra_words=extra)
            full_prompt = self.format_prompt(tmpl, **(params or {}), **ctx_strs)
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
            self._log_failure_summary(result)
            self._write_last_failed_command(full_prompt)
        if allowed_files is not None:
            if sandbox:
                self.verify_changes(before, allowed_files)
            for f in allowed_files:
                self.strip_thinking_tags(os.path.abspath(f))
        return result

    def _log_failure_summary(self, result: subprocess.CompletedProcess) -> None:  # type: ignore[type-arg]
        """Log a concise failure summary from the subprocess result.

        Extracts the last meaningful lines from stderr (or stdout if stderr is
        empty) so the user sees why the agent failed without raw output dumps.
        """
        source = (result.stderr or "").strip() or (result.stdout or "").strip()
        if not source:
            print(f"   -> Agent exited with code {result.returncode} (no output)")
            return
        # Take the last 5 non-empty lines as the error summary
        lines = [l for l in source.splitlines() if l.strip()]
        tail = lines[-5:] if len(lines) > 5 else lines
        print(f"   -> Agent exited with code {result.returncode}. Last output:")
        for line in tail:
            print(f"      {line.rstrip()}")

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
        *,
        context_files: Optional[Dict[str, Union[str, List[str]]]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Alias for :meth:`run_ai` retained for backwards compatibility.

        Accepts the same *context_files* and *params* keyword arguments as
        :meth:`run_ai`.

        :param full_prompt: Fully rendered prompt string, or template filename
            when *context_files* / *params* are given.
        :type full_prompt: str
        :param allowed_files: Paths the AI is permitted to touch.
        :type allowed_files: list[str] or None
        :param sandbox: Enforce sandbox rules when ``True``.
        :type sandbox: bool
        :param timeout: Maximum seconds to wait for the AI process.
        :type timeout: int or None
        :param context_files: See :meth:`run_ai`.
        :param params: See :meth:`run_ai`.
        :returns: The completed process result.
        :rtype: subprocess.CompletedProcess
        """
        return self.run_ai(
            full_prompt, allowed_files, sandbox, timeout=timeout,
            context_files=context_files, params=params,
        )

    def count_task_files(self, directory: str) -> int:
        """Count task files in a directory, excluding non-task files.
        
        :param directory: Directory to scan.
        :type directory: str
        :returns: Count of task files (excluding READMEs, summaries, etc.).
        :rtype: int
        """
        # Non-task files to exclude
        _NON_TASK_FILES = {
            "README.md",
            "SUB_EPIC_SUMMARY.md",
            "REQUIREMENTS_TRACEABILITY.md",
            "review_summary.md",
            "cross_phase_review_summary.md",
            "reorder_tasks_summary.md",
            "cross_phase_review_summary_pass_1.md",
            "cross_phase_review_summary_pass_2.md",
            "reorder_tasks_summary_pass_1.md",
            "reorder_tasks_summary_pass_2.md",
        }
        count = 0
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.endswith(".md") and f not in _NON_TASK_FILES:
                    count += 1
        return count

    def get_headers_path(self, doc: Dict[str, Any]) -> str:
        """Return the path to the headers JSON sidecar for a document.

        :param doc: Document descriptor.
        :type doc: dict
        :returns: Absolute path to the ``_headers.json`` file.
        :rtype: str
        """
        out_folder = "specs" if doc["type"] == "spec" else "research"
        return os.path.join(self.plan_dir, out_folder, f"{doc['id']}_headers.json")

    def save_headers(self, doc: Dict[str, Any], filepath: str) -> List[str]:
        """Extract H1/H2 headers from a markdown file and save to a JSON sidecar.

        Called after Phase 1 generates a spec so that the canonical list of
        headers is captured before any flesh-out passes modify the document.

        :param doc: Document descriptor.
        :param filepath: Path to the markdown file to extract headers from.
        :returns: The list of extracted headers.
        :rtype: list[str]
        """
        headers = self._extract_markdown_headers(filepath)
        headers_path = self.get_headers_path(doc)
        with open(headers_path, 'w', encoding='utf-8') as f:
            json.dump(headers, f, indent=2)
        return headers

    def parse_markdown_headers(self, filepath: str, doc: Optional[Dict[str, Any]] = None) -> List[str]:
        """Return the list of section headers for a document.

        Prefers the ``_headers.json`` sidecar (written by :meth:`save_headers`)
        so that headers embedded in spec content don't pollute the list.
        Falls back to extracting from the markdown if no sidecar exists.

        :param filepath: Path to the markdown file (used as fallback).
        :param doc: Document descriptor; when provided, the sidecar is checked.
        :returns: List of header strings (e.g. ``["# Title", "## Section"]``).
        :rtype: list[str]
        """
        if doc is not None:
            headers_path = self.get_headers_path(doc)
            if os.path.exists(headers_path):
                with open(headers_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        return self._extract_markdown_headers(filepath)

    def _extract_markdown_headers(self, filepath: str) -> List[str]:
        """Extract H1 and H2 headers from a markdown file.

        :param filepath: Path to the markdown file.
        :returns: List of header strings.
        :rtype: list[str]
        """
        headers = []
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    # Only want to capture h1 and h2
                    if re.match(r'^#{1,2}\s+', line):
                        headers.append(line.strip())
        return headers

