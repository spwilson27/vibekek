"""Parallel task-execution engine for the implementation workflow.

This module drives the ``run`` command: it reads a merged DAG, resolves
dependency order at runtime, and executes tasks concurrently using
:class:`concurrent.futures.ThreadPoolExecutor`.

Key responsibilities:

* **process_task** — clones the repo, runs implementation and review
  AI agents, verifies the result with a presubmit command, and commits.
* **merge_task** — clones the repo into a temp directory, squash-merges the
  task branch into ``dev``, verifies again, and pushes to the local origin.
* **execute_dag** — orchestrates the parallel execution loop, scheduling
  tasks as their prerequisites complete and halting on first failure.
* **Logger** — a thread-safe ``sys.stdout``/``sys.stderr`` wrapper that
  prepends timestamps and mirrors output to a log file.

Signal handling:

* First ``SIGINT`` sets the ``shutdown_requested`` flag so the loop drains
  in-flight tasks gracefully.
* Second ``SIGINT`` calls ``os._exit(1)`` for immediate termination.
"""

import atexit
import os
import shutil
import subprocess
import sys
import json
import re
from typing import Callable, List, Dict, Any, Optional
import threading
import concurrent.futures
import tempfile
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

_PST = ZoneInfo("America/Los_Angeles")

from .constants import TOOLS_DIR, ROOT_DIR, INPUT_DIR, REPLAN_STATE_FILE, STATE_DIR, WORKFLOW_STATE_FILE
from .context import ProjectContext
from .runners import IMAGE_EXTENSIONS, make_runner
from .state import save_workflow_state
from .config import get_serena_enabled, get_dev_branch, get_pivot_remote
from .discord import notify_failure
from .dashboard import make_dashboard
from .agent_pool import AgentPoolManager, QUOTA_RETURN_CODE, QUOTA_PATTERNS

shutdown_requested = False
_active_dashboard: Any = None


def _restore_terminal() -> None:
    """Best-effort restore terminal state (cursor visibility, alternate screen)."""
    try:
        sys.stdout.write("\x1b[?25h")  # show cursor
        sys.stdout.write("\x1b[?1049l")  # leave alternate screen
        sys.stdout.flush()
    except Exception:
        pass


def _compact_task_id(phase_id: str, task_name: str) -> str:
    """Build a compact but unique task identifier for log prefixes.

    Given phase_id="phase_1" and task_name="02_dod_guidelines/03_enforce_rustdoc.md",
    produces "p1/02/03_enforce_ru" — short enough to avoid truncation while
    remaining distinguishable across concurrent tasks.
    """
    # Extract phase number: "phase_1" -> "p1"
    m = re.match(r"phase_(\d+)", phase_id)
    short_phase = f"p{m.group(1)}" if m else phase_id

    parts = task_name.replace(".md", "").split("/")
    if len(parts) >= 2:
        # Extract numeric prefix from sub-epic: "02_dod_guidelines" -> "02"
        sub_match = re.match(r"(\d+)", parts[-2])
        sub_prefix = sub_match.group(1) if sub_match else parts[-2][:4]
        # Leaf task: keep number prefix + truncated name slug
        leaf = parts[-1]
        if len(leaf) > 15:
            leaf = leaf[:15]
        return f"{short_phase}/{sub_prefix}/{leaf}"
    else:
        leaf = parts[0]
        if len(leaf) > 20:
            leaf = leaf[:20]
        return f"{short_phase}/{leaf}"


def _step_for_agent_type(agent_type: str) -> str:
    """Map an agent_type label to the corresponding pool step name.

    :param agent_type: Label passed to :func:`run_agent` (e.g.
        ``"Implementation"``, ``"Review"``, ``"Merge"``).
    :returns: One of ``"develop"``, ``"review"``, ``"merge"``, or ``"all"``.
    """
    if agent_type.startswith("Implementation"):
        return "develop"
    if agent_type.startswith("Review"):
        return "review"
    if agent_type == "Merge":
        return "merge"
    return "all"


def signal_handler(sig: int, frame: Any) -> None:  # type: ignore[type-arg]
    """Handle ``SIGINT`` (Ctrl-C) with a two-stage shutdown policy.

    The first ``SIGINT`` sets :data:`shutdown_requested` so the execution loop
    stops scheduling new tasks and waits for in-flight tasks to finish.  A
    second ``SIGINT`` calls :func:`os._exit` for an immediate, unconditional
    exit.

    :param sig: Signal number received (typically ``signal.SIGINT``).
    :param frame: Current stack frame (unused).
    """
    global shutdown_requested
    if not shutdown_requested:
        shutdown_requested = True
        if _active_dashboard:
            _active_dashboard.set_shutting_down()
        else:
            print("\n[!] Ctrl-C detected. Initiating graceful shutdown...")
            print("    Active agents will finish. No new agents will be spawned.")
    else:
        _restore_terminal()
        print("\n[!] Ctrl-C detected again. Forcing immediate exit...")
        os._exit(1)
def get_gitlab_remote_url(root_dir: str, remote_name: str = "origin") -> str:
    """Return the URL of the pivot remote for *root_dir*.

    Prefers the remote named *remote_name* (default ``"origin"``); falls back
    to the first remote found when no match exists.

    :param root_dir: Absolute path to the git repository root.
    :type root_dir: str
    :param remote_name: Name of the preferred remote, e.g. ``"origin"`` or
        ``"github"``.  Defaults to ``"origin"``.
    :type remote_name: str
    :returns: Remote URL string.
    :rtype: str
    """
    try:
        res = subprocess.run(["git", "remote", "-v"], cwd=root_dir, capture_output=True, text=True, check=True)
        # Prefer the configured pivot remote, fall back to first available
        first_url = None
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                if first_url is None:
                    first_url = parts[1]
                if parts[0] == remote_name:
                    return parts[1]
        if first_url:
            return first_url
    except subprocess.CalledProcessError:
        pass
    raise RuntimeError(
        f"No git remote found. Configure a remote with: git remote add {remote_name} <url>"
    )

class Logger(object):
    """Thread-safe stream wrapper that timestamps output and mirrors it to a log file.

    Replaces ``sys.stdout`` and ``sys.stderr`` in :func:`cmd_run` so that all
    output from concurrent worker threads is serialised under a single lock and
    written to both the terminal and a persistent log file.

    :param terminal: The original terminal stream (``sys.stdout`` or
        ``sys.stderr``).
    :param log_stream: An open file object for the log file.
    :param lock: A :class:`threading.Lock` shared between the ``stdout`` and
        ``stderr`` wrappers to prevent interleaved output.
    """

    def __init__(self, terminal: Any, log_stream: Any, lock: threading.Lock) -> None:  # type: ignore[type-arg]
        """Initialise the logger.

        :param terminal: Original terminal stream.
        :param log_stream: Writable file object for log output.
        :param lock: Shared mutex for serialising writes.
        """
        self.terminal = terminal
        self.log_stream = log_stream
        self.lock = lock
        self._at_line_start = True

    def write(self, message: str) -> None:
        """Write *message* to both terminal and log file, prepending timestamps.

        A ``[YYYY-MM-DD HH:MM:SS]`` prefix is added at the start of each new
        line.  Partial lines (those not ending in ``\\n``) are written without
        a timestamp prefix until a newline is encountered.

        :param message: The string to write.
        :type message: str
        """
        with self.lock:
            # Prepend timestamp at the start of each new line
            parts = message.splitlines(keepends=True)
            out = ""
            for part in parts:
                if self._at_line_start and not part.startswith("\n"):
                    ts = datetime.now(tz=_PST).strftime("%Y-%m-%d %H:%M:%S %Z")
                    out += f"[{ts}] {part}"
                else:
                    out += part
                self._at_line_start = part.endswith("\n")

            self.terminal.write(out)
            self.log_stream.write(out)
            self.log_stream.flush()

    def flush(self) -> None:
        """Flush both the terminal and log-file streams under the shared lock."""
        with self.lock:
            self.terminal.flush()
            self.log_stream.flush()


def run_ai_command(
    prompt: str,
    cwd: str,
    prefix: str = "",
    backend: str = "gemini",
    image_paths: Optional[List[str]] = None,
    on_line: Optional[Callable[[str], None]] = None,
    model: Optional[str] = None,
    user: Optional[str] = None,
) -> tuple:  # type: ignore[type-arg]
    """Launch an AI CLI process and stream its output.

    Delegates to the appropriate :class:`~workflow_lib.runners.AIRunner`
    subclass.  Output lines are printed to ``stdout`` with an optional
    *prefix* so concurrent tasks can be distinguished in the log.

    When a quota-exceeded pattern is detected in the output stream,
    :data:`~workflow_lib.agent_pool.QUOTA_RETURN_CODE` is returned so the
    caller can rotate to a different agent.

    :param prompt: Full prompt text to pass to the AI CLI.
    :param cwd: Working directory for the subprocess.
    :param prefix: String prepended to each output line (e.g. task ID).
    :param backend: AI backend name (``"gemini"``, ``"claude"``, etc.).
    :param image_paths: Optional list of absolute paths to image files.
    :param on_line: Optional callback invoked per output line.
    :param model: Optional model name passed to the CLI.
    :param user: Optional OS user to run the CLI as (via sudo).
    :returns: Tuple of (return_code, stderr_text).
    """
    from .config import get_config_defaults
    cfg = get_config_defaults()
    soft_timeout = cfg.get("soft_timeout")

    runner = make_runner(backend, model=model, soft_timeout=soft_timeout, user=user)

    quota_detected = [False]
    quota_patterns_lower = [p.lower() for p in QUOTA_PATTERNS]

    def output_line(line: str) -> None:
        line_lower = line.lower()
        if any(p in line_lower for p in quota_patterns_lower):
            quota_detected[0] = True
        if on_line:
            on_line(line)
        else:
            print(f"{prefix}{line}")
            sys.stdout.flush()

    try:
        result = runner.run(cwd, prompt, image_paths=image_paths, on_line=output_line)
        if quota_detected[0]:
            return QUOTA_RETURN_CODE, "quota exceeded"
        stderr_text = result.stderr or ""
        if result.returncode != 0 and stderr_text:
            for line in stderr_text.strip().splitlines():
                output_line(f"[stderr] {line}")
        return result.returncode, stderr_text
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except FileNotFoundError:
        return 1, "command not found"


def phase_sort_key(task_id: str) -> tuple:  # type: ignore[type-arg]
    """Parse a task ID into a sortable ``(phase_num, task_num)`` tuple.

    Task IDs have the form ``phase_<N>/<sub_epic>/<NN>_<name>.md``.  The
    function extracts the integer phase number and the leading integer prefix
    of the second path component.  Unknown formats return ``(999, 999)`` so
    they sort last.

    :param task_id: Fully-qualified task ID, e.g. ``"phase_1/api/01_setup.md"``.
    :type task_id: str
    :returns: ``(phase_num, task_num)`` suitable for use as a sort key.
    :rtype: tuple
    """
    parts = task_id.split("/")
    if len(parts) >= 2:
        phase_part = parts[0]
        task_part = parts[1]
        
        phase_num = 0
        if phase_part.startswith("phase_"):
            try:
                phase_num = int(phase_part.split("_")[1])
            except ValueError:
                pass
                
        task_num = 0
        try:
            task_num = int(task_part.split("_")[0])
        except ValueError:
            pass
            
        return (phase_num, task_num)
    return (999, 999)
    
def get_task_details(full_task_id: str) -> str:
    """Read all markdown files for a task and return them as a single context string.

    If *full_task_id* resolves to a file, that file is read.  If it resolves
    to a directory, every ``.md`` file in that directory is concatenated.

    :param full_task_id: Relative task path such as ``"phase_1/api/01_setup.md"``.
    :type full_task_id: str
    :returns: Concatenated file contents, separated by blank lines, or an
        empty string when the path does not exist.
    :rtype: str
    """
    task_path = os.path.join(ROOT_DIR, "docs", "plan", "tasks", full_task_id)
    content = ""
    if os.path.isfile(task_path):
        with open(task_path, "r", encoding="utf-8") as file:
            content += file.read() + "\n\n"
    elif os.path.isdir(task_path):
        for f in os.listdir(task_path):
            if f.endswith(".md"):
                with open(os.path.join(task_path, f), "r", encoding="utf-8") as file:
                    content += file.read() + "\n\n"
    return content


def get_memory_context(root_dir: str) -> str:
    """Return the contents of the agent MEMORY.md file, or an empty string.

    :param root_dir: Absolute path to the project root.
    :type root_dir: str
    :returns: Memory file contents, or ``""`` when not found.
    :rtype: str
    """
    memory_file = os.path.join(root_dir, ".agent", "MEMORY.md")
    if os.path.exists(memory_file):
        with open(memory_file, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def get_project_context(tools_dir: str = "") -> str:
    """Return the concatenated contents of all text files in the ``input/`` directory.

    Every non-image file in ``.tools/input/`` is included, sorted by filename,
    with each file's content preceded by a ``## <filename>`` header.  Image
    files are excluded here; use :func:`get_project_images` to obtain them.

    :param tools_dir: Unused; present for API compatibility.  The actual path
        is always resolved from the package-level :data:`TOOLS_DIR` constant.
    :type tools_dir: str
    :returns: Concatenated text input file contents, or ``""`` when the
        directory does not exist or contains no text files.
    :rtype: str
    """
    input_dir = INPUT_DIR
    if not os.path.isdir(input_dir):
        return ""
    files = sorted(
        f for f in os.listdir(input_dir)
        if os.path.isfile(os.path.join(input_dir, f))
        and os.path.splitext(f)[1].lower() not in IMAGE_EXTENSIONS
    )
    if not files:
        return ""
    parts = []
    for filename in files:
        filepath = os.path.join(input_dir, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            parts.append(f"<file name=\"{filename}\">\n{f.read()}\n</file>")
    return "\n\n".join(parts)


def get_project_images() -> List[str]:
    """Return absolute paths to all image files in the ``input/`` directory.

    :returns: Sorted list of absolute image file paths, or ``[]`` when the
        directory does not exist or contains no image files.
    :rtype: list[str]
    """
    input_dir = INPUT_DIR
    if not os.path.isdir(input_dir):
        return []
    return sorted(
        os.path.join(input_dir, f)
        for f in os.listdir(input_dir)
        if os.path.isfile(os.path.join(input_dir, f))
        and os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS
    )


def run_agent(agent_type: str, prompt_file: str, task_context: Dict[str, Any], cwd: str, backend: str = "gemini", dashboard: Any = None, task_id: str = "", model: Optional[str] = None, agent_pool: Optional[AgentPoolManager] = None) -> bool:
    """Format a prompt template and execute an AI agent subprocess.

    Reads the named prompt template from ``.tools/prompts/``, performs simple
    ``{key}`` substitution using *task_context*, then delegates to
    :func:`run_ai_command`.

    When *agent_pool* is provided, an :class:`~workflow_lib.agent_pool.AgentConfig`
    is acquired before each attempt and released afterwards.  Quota-exceeded
    events are recorded on the pool so that exhausted agents are temporarily
    suppressed and the next attempt picks a different agent.

    :param agent_type: Human-readable label for log output (e.g.
        ``"Implementation"``, ``"Review"``).
    :type agent_type: str
    :param prompt_file: Filename of the prompt template inside
        ``.tools/prompts/`` (e.g. ``"implement_task.md"``).
    :type prompt_file: str
    :param task_context: Key/value substitution map applied to the template.
    :type task_context: dict
    :param cwd: Working directory in which to run the AI subprocess (typically
        the task clone path).
    :type cwd: str
    :param backend: AI backend to use when *agent_pool* is ``None``.
        Passed through to :func:`run_ai_command`.  Defaults to ``"gemini"``.
    :type backend: str
    :param agent_pool: Optional pool manager.  When provided, agents are
        selected and tracked through the pool instead of using *backend* directly.
    :type agent_pool: AgentPoolManager or None
    :returns: ``True`` if the agent exited with code 0, ``False`` otherwise.
    :rtype: bool
    """
    prompt_path = os.path.join(TOOLS_DIR, "prompts", prompt_file)
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt_tmpl = f.read()

    # Simple template replacement
    prompt = prompt_tmpl
    for k, v in task_context.items():
        prompt = prompt.replace(f"{{{k}}}", str(v))

    msg = f"[{agent_type}] Starting agent in {cwd}..."
    if dashboard:
        dashboard.log(msg)
    else:
        print(f"      {msg}")

    phase_id = task_context.get("phase_filename", "phase")
    task_name = task_context.get("task_name", "task")
    short_task = _compact_task_id(phase_id, task_name)
    prefix = f"[{short_task}] "

    on_line: Optional[Callable[[str], None]] = None
    if dashboard and task_id:
        def on_line(line: str, _tid: str = task_id, _stage: str = agent_type) -> None:
            dashboard.log(f"{prefix}{line}")
            dashboard.set_agent(_tid, _stage, "running", line)

    max_capacity_retries = 5
    base_delay = 30  # seconds

    for attempt in range(1, max_capacity_retries + 1):
        # Resolve backend/user/model from pool (if active) or fixed backend.
        agent_cfg = None
        active_backend = backend
        active_model = model
        active_user: Optional[str] = None

        if agent_pool is not None:
            step = _step_for_agent_type(agent_type)
            agent_cfg = agent_pool.acquire(timeout=300.0, step=step)
            if agent_cfg is None:
                err = f"[{agent_type}] FATAL: No agent available for step '{step}' after waiting (all quota-exhausted, at capacity, or none configured)"
                if dashboard:
                    dashboard.log(err)
                else:
                    print(f"      {err}")
                return False
            active_backend = agent_cfg.backend
            active_model = agent_cfg.model or model
            active_user = agent_cfg.user
            if dashboard and task_id:
                dashboard.set_agent(task_id, agent_type, "running", agent_name=agent_cfg.name)

        # Transfer directory ownership to the agent's OS user so it can write freely.
        if active_user and active_user != os.getenv("USER", ""):
            _set_dir_owner(cwd, active_user, print)

        returncode = 1
        stderr_text = ""
        try:
            returncode, stderr_text = run_ai_command(
                prompt, cwd, prefix=prefix, backend=active_backend,
                image_paths=get_project_images(), on_line=on_line,
                model=active_model, user=active_user,
            )
        finally:
            if agent_pool is not None and agent_cfg is not None:
                quota_exhausted = (returncode == QUOTA_RETURN_CODE)
                agent_pool.release(agent_cfg, quota_exhausted=quota_exhausted)
                if quota_exhausted and dashboard:
                    dashboard.log(
                        f"[!] [{agent_cfg.name}] Quota exceeded — suppressed for {agent_cfg.quota_time}s"
                    )
                    if task_id:
                        dashboard.set_agent(task_id, agent_type, "waiting", f"Quota exceeded on {agent_cfg.name}", agent_name="")

        if returncode == 0:
            return True

        # Quota exceeded: retry immediately with a different agent (pool handles rotation).
        if returncode == QUOTA_RETURN_CODE:
            if attempt < max_capacity_retries:
                retry_msg = f"[{agent_type}] Quota exceeded on {agent_cfg.name if agent_cfg else active_backend} (attempt {attempt}/{max_capacity_retries}). Retrying with next agent..."
                if dashboard:
                    dashboard.log(f"{prefix}{retry_msg}")
                else:
                    print(f"      {retry_msg}")
                continue
            err = f"[{agent_type}] FATAL: All agents quota-exhausted after {max_capacity_retries} attempts"
            if dashboard:
                dashboard.log(err)
            else:
                print(f"      {err}")
            return False

        # When no pool is active, fall back to the existing capacity-error retry with backoff.
        if agent_pool is None:
            is_capacity_error = any(s in stderr_text for s in (
                "RESOURCE_EXHAUSTED",
                "MODEL_CAPACITY_EXHAUSTED",
                "No capacity available",
                "rateLimitExceeded",
            ))
            if is_capacity_error and attempt < max_capacity_retries:
                delay = base_delay * (2 ** (attempt - 1))  # 30s, 60s, 120s, 240s
                retry_msg = f"[{agent_type}] Capacity exhausted (attempt {attempt}/{max_capacity_retries}). Retrying in {delay}s..."
                if dashboard:
                    dashboard.log(f"{prefix}{retry_msg}")
                    if task_id:
                        dashboard.set_agent(task_id, agent_type, "waiting", f"Capacity retry {attempt}/{max_capacity_retries}")
                else:
                    print(f"      {retry_msg}")
                time.sleep(delay)
                continue

        err = f"[{agent_type}] FATAL: Agent process failed with exit code {returncode}"
        if dashboard:
            dashboard.log(err)
        else:
            print(f"      {err}")
        return False

    return False


def rebuild_serena_cache(source_dir: str, root_dir: str, cache_lock: threading.Lock, dashboard: Any = None) -> None:
    """Rebuild the Serena code-intelligence cache and copy it to the main repo.

    Starts ``serena-mcp-server`` in *source_dir* (which has ``dev`` checked
    out) to trigger index generation, waits up to 120 seconds for it to
    finish, then atomically replaces ``.serena/cache/`` in *root_dir* with the
    newly generated cache.

    :param source_dir: Path to the directory where Serena should build its
        index.  Usually a temporary merge clone.
    :type source_dir: str
    :param root_dir: Absolute path to the project root that will receive the
        updated cache.
    :type root_dir: str
    :param cache_lock: Lock used to serialise concurrent cache updates across
        worker threads.
    :type cache_lock: threading.Lock
    """
    cache_src = os.path.join(source_dir, ".serena", "cache")
    cache_dst = os.path.join(root_dir, ".serena", "cache")

    _log = dashboard.log if dashboard else print
    _log(f"      [Serena] Re-indexing from {source_dir}...")
    # Serena indexes on MCP server startup. Start it to trigger indexing, then terminate.
    serena_proc = subprocess.Popen(
        ["uvx", "--from", "serena", "serena-mcp-server",
         "--project-from-cwd", "--mode", "no-onboarding"],
        cwd=source_dir,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        serena_proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        serena_proc.terminate()
        serena_proc.wait()

    if not os.path.isdir(cache_src):
        _log(f"      [Serena] Warning: cache not found at {cache_src} after indexing. Skipping.")
        return

    with cache_lock:
        tmp_dst = cache_dst + ".tmp"
        if os.path.isdir(tmp_dst):
            shutil.rmtree(tmp_dst)
        shutil.copytree(cache_src, tmp_dst)
        if os.path.isdir(cache_dst):
            shutil.rmtree(cache_dst)
        os.rename(tmp_dst, cache_dst)
    _log(f"      [Serena] Cache updated at {cache_dst}.")



def process_task(root_dir: str, full_task_id: str, presubmit_cmd: str, backend: str = "gemini", max_retries: int = 3, serena: bool = False, dashboard: Any = None, model: Optional[str] = None, dev_branch: str = "dev", remote_url: Optional[str] = None, agent_pool: Optional[AgentPoolManager] = None) -> bool:
    """Run the full implementation lifecycle for one task.

    Steps performed:

    1. Clone the repo into a temp directory on a dedicated branch.
    2. Optionally seed the Serena cache and copy ``.mcp.json`` (when
       *serena* is ``True``).
    3. Run the **Implementation** AI agent.
    4. Run the **Review** AI agent.
    5. Run the presubmit command up to *max_retries* times, feeding failure
       output back to the Review agent on each retry.
    6. Commit changes if the presubmit passes.

    The clone is removed on success.  On failure it is left in place so the
    developer can inspect or manually fix the state.

    :param root_dir: Absolute path to the project root git repository.
    :type root_dir: str
    :param full_task_id: Fully-qualified task ID, e.g.
        ``"phase_1/api/01_setup.md"``.
    :type full_task_id: str
    :param presubmit_cmd: Shell command string (split on whitespace) used to
        verify the implementation, e.g. ``"./do presubmit"``.
    :type presubmit_cmd: str
    :param backend: AI backend to use (``"gemini"``, ``"claude"``, or
        ``"copilot"``).  Defaults to ``"gemini"``.
    :type backend: str
    :param max_retries: Maximum number of presubmit verification attempts
        before giving up.  Defaults to ``3``.
    :type max_retries: int
    :param serena: When ``True``, seed the Serena cache into the clone and
        copy ``.mcp.json`` so the Claude CLI can discover the Serena MCP
        server.  Defaults to ``False``.
    :type serena: bool
    :returns: ``True`` if the task was implemented, verified, and committed
        successfully; ``False`` otherwise.
    :rtype: bool
    """
    phase_id, task_id = full_task_id.split("/", 1)
    safe_task_id = task_id.replace("/", "_").replace(".md", "")
    branch_name = f"ai-phase-{safe_task_id}"

    def _log(msg: str) -> None:
        if dashboard:
            dashboard.log(msg)
        else:
            print(msg)

    # If the task branch already exists in origin (e.g. from a prior run that
    # succeeded but failed to record state), skip re-running the agent and let
    # merge_task handle it directly.
    try:
        existing = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url or root_dir, branch_name],
            capture_output=True, text=True,
        )
        if "refs/heads/" in existing.stdout:
            _log(f"\n   -> [Implementation] Skipping {full_task_id} — branch {branch_name} already exists in origin.")
            return True
    except Exception:
        pass  # ls-remote failed; proceed with normal implementation flow

    _log(f"\n   -> [Implementation] Starting {full_task_id}")
    if dashboard:
        dashboard.set_agent(full_task_id, "Impl", "queued", "")

    tmpdir = ""
    success = False
    try:
        tmpdir = tempfile.mkdtemp(prefix=f"ai_{safe_task_id}_")
        _log(f"      Cloning repository to {tmpdir} on branch {branch_name}...")
        if dashboard:
            dashboard.set_agent(full_task_id, "Impl", "cloning", "")
        try:
            subprocess.run(["git", "clone", remote_url or root_dir, tmpdir], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            subprocess.run(["git", "checkout", "-B", branch_name, f"origin/{dev_branch}"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            _log(f"      [!] Failed to create clone:\n{e.stderr.decode('utf-8')}")
            if dashboard:
                dashboard.set_agent(full_task_id, "Impl", "failed", "Clone failed")
            return False

        if serena:
            # Seed Serena cache from main repo so agents don't start cold
            serena_cache_src = os.path.join(root_dir, ".serena", "cache")
            serena_cache_dst = os.path.join(tmpdir, ".serena", "cache")
            if os.path.isdir(serena_cache_src) and not os.path.isdir(serena_cache_dst):
                shutil.copytree(serena_cache_src, serena_cache_dst)

            # Copy .mcp.json so Claude CLI picks up Serena in the clone
            mcp_src = os.path.join(root_dir, ".mcp.json")
            mcp_dst = os.path.join(tmpdir, ".mcp.json")
            if os.path.exists(mcp_src) and not os.path.exists(mcp_dst):
                shutil.copy2(mcp_src, mcp_dst)

        task_details = get_task_details(full_task_id)
        description_ctx = get_project_context()
        memory_ctx = get_memory_context(root_dir)
        
        context = {
            "phase_filename": phase_id,
            "task_name": task_id,
            "target_dir": full_task_id,
            "task_details": task_details,
            "description_ctx": description_ctx,
            "memory_ctx": memory_ctx,
            "clone_dir": tmpdir
        }

        # 1. Implementation Agent
        if dashboard:
            dashboard.set_agent(full_task_id, "Impl", "running", "")
        if not run_agent("Implementation", "implement_task.md", context, tmpdir, backend, dashboard=dashboard, task_id=full_task_id, model=model, agent_pool=agent_pool):
            if dashboard:
                dashboard.set_agent(full_task_id, "Impl", "failed", "Implementation agent failed")
            return False

        # 2. Review Agent
        if dashboard:
            dashboard.set_agent(full_task_id, "Review", "running", "")
        if not run_agent("Review", "review_task.md", context, tmpdir, backend, dashboard=dashboard, task_id=full_task_id, model=model, agent_pool=agent_pool):
            if dashboard:
                dashboard.set_agent(full_task_id, "Review", "failed", "Review agent failed")
            return False

        # Reclaim ownership of files written by alternate-user agents before
        # running presubmit and git commands as the current user.
        _reclaim_dir_ownership(tmpdir, _log)

        # 3. Verification Loop
        for attempt in range(1, max_retries + 1):
            _log(f"      [Verification] Running presubmit (Attempt {attempt}/{max_retries})...")
            if dashboard:
                dashboard.set_agent(full_task_id, "Verify", "running", f"Attempt {attempt}/{max_retries}")
            # We split the command string into a list for subprocess
            cmd_list = presubmit_cmd.split()
            presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True, start_new_session=True)

            if presubmit_res.returncode == 0:
                _log(f"      [Verification] Presubmit passed!")

                # Commit the changes
                subprocess.run(["git", "add", "-A"], cwd=tmpdir, check=True)
                # Only commit if there are changes
                status = subprocess.run(["git", "status", "--porcelain"], cwd=tmpdir, capture_output=True, text=True)
                if status.stdout.strip():
                     commit_msg = f"{phase_id}:{task_id}: Standardized Implementation"
                     match = re.search(r'^#\s*Task:\s*(.*?)(?:\s*\(Sub-Epic:.*?\))?$', task_details, re.MULTILINE)
                     if match and match.group(1).strip():
                         commit_msg = f"{phase_id}:{task_id}: {match.group(1).strip()}"
                     subprocess.run(["git", "commit", "--no-verify", "-m", commit_msg], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL)
                else:
                     _log(f"      [Verification] No changes to commit for {full_task_id}.")
                # Push the task branch back to the main repo so merge_task can access it
                push_res = subprocess.run(
                    ["git", "push", "origin", branch_name],
                    cwd=tmpdir, capture_output=True, text=True,
                )
                if push_res.returncode != 0:
                    # Non-fast-forward means the branch already exists from a prior run
                    # that passed presubmit but failed to record state. Force-push our
                    # verified implementation so merge_task can proceed.
                    if "non-fast-forward" in push_res.stderr or "[rejected]" in push_res.stderr:
                        _log(f"      [!] Push rejected (branch exists from prior run); force-pushing verified branch.")
                        force_res = subprocess.run(
                            ["git", "push", "--force-with-lease", "origin", branch_name],
                            cwd=tmpdir, capture_output=True, text=True,
                        )
                        if force_res.returncode != 0:
                            _log(f"      [!] Force-push also failed:\n{force_res.stderr}")
                            return False
                    else:
                        _log(f"      [!] Failed to push task branch {branch_name} to origin:\n{push_res.stderr}")
                        return False

                if dashboard:
                    dashboard.set_agent(full_task_id, "Verify", "done", "Presubmit passed")
                success = True
                return True

            _log(f"      [Verification] Presubmit failed.")
            if attempt < max_retries:
                 # Feed the failure back to the review agent
                 failure_ctx = dict(context)
                 failure_ctx["task_details"] += f"\n\n### PRESUBMIT FAILURE (Attempt {attempt})\nThe presubmit script failed with the following output. Please fix the code.\n\n```\n{presubmit_res.stdout}\n{presubmit_res.stderr}\n```\n"
                 if dashboard:
                     dashboard.set_agent(full_task_id, "Review", "running", f"Retry after presubmit failure")
                 if not run_agent("Review (Retry)", "review_task.md", failure_ctx, tmpdir, backend, dashboard=dashboard, task_id=full_task_id, model=model, agent_pool=agent_pool):
                     return False

        _log(f"   -> [!] Task {full_task_id} failed presubmit {max_retries} times. Aborting task.")
        if dashboard:
            dashboard.set_agent(full_task_id, "Verify", "failed", f"Failed after {max_retries} attempts")
        return False

    finally:
        if success:
            # Cleanup clone
            _log(f"      Cleaning up clone {tmpdir}...")
            shutil.rmtree(tmpdir, ignore_errors=True)
            if dashboard:
                dashboard.remove_agent(full_task_id)
        else:
            _log(f"      [!] Task failed. Leaving clone {tmpdir} and branch {branch_name} for investigation.")
            if dashboard:
                dashboard.set_agent(full_task_id, "failed", "failed", "Task failed")


def _set_dir_owner(path: str, user: str, _log: Any) -> None:
    """Recursively ``chown`` *path* to *user* via ``sudo``.

    Used in two directions:
    - Before spawning an agent: transfer ownership to the agent's OS user so
      it can freely write inside its working directory.
    - After all agents finish: reclaim ownership for the current user so that
      git and presubmit commands (which run as the current user) can access
      the files.
    """
    if not user:
        return
    result = subprocess.run(
        ["sudo", "chown", "-R", user, path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _log(f"      [!] Warning: failed to chown {path} to {user!r}: {result.stderr.strip()}")


def _reclaim_dir_ownership(tmpdir: str, _log: Any) -> None:
    """Reclaim ownership of *tmpdir* for the current OS user.

    Convenience wrapper around :func:`_set_dir_owner` using ``$USER``.
    """
    _set_dir_owner(tmpdir, os.getenv("USER", ""), _log)


def _commit_state_in_clone(tmpdir: str, workflow_state: Optional[Dict], _log: Any) -> None:
    """Stage workflow state files and commit them in tmpdir if anything changed."""
    state_rel_dir = os.path.relpath(STATE_DIR, ROOT_DIR)
    dst_dir = os.path.join(tmpdir, state_rel_dir)
    os.makedirs(dst_dir, exist_ok=True)

    for src_path in [WORKFLOW_STATE_FILE, REPLAN_STATE_FILE]:
        rel = os.path.relpath(src_path, ROOT_DIR)
        dst = os.path.join(tmpdir, rel)
        if workflow_state is not None and src_path == WORKFLOW_STATE_FILE:
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(workflow_state, f, indent=4)
        elif os.path.exists(src_path):
            shutil.copy2(src_path, dst)

    subprocess.run(["git", "add", state_rel_dir], cwd=tmpdir,
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmpdir,
                            capture_output=True, text=True)
    if status.stdout.strip():
        subprocess.run(["git", "commit", "--no-verify", "-m", "Update workflow state"],
                       cwd=tmpdir, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def merge_task(root_dir: str, task_id: str, presubmit_cmd: str, backend: str = "gemini", max_retries: int = 3, cache_lock: Optional[threading.Lock] = None, serena: bool = False, dashboard: Any = None, model: Optional[str] = None, dev_branch: str = "dev", remote_url: Optional[str] = None, workflow_state: Optional[Dict] = None, agent_pool: Optional[AgentPoolManager] = None) -> bool:
    """Squash-merge a task branch into ``dev`` via a temporary clone and verify.

    Steps performed:

    1. Clone the repo into a temp directory.
    2. Add the GitLab remote (for CI push targets).
    3. Attempt a ``git merge --squash`` of the task branch.
    4. If the squash succeeds, run the presubmit command.
    5. If the squash fails (conflicts), attempt a rebase then retry.
    6. If conflicts remain, spawn a **Merge** AI agent up to *max_retries*
       times to resolve them manually.
    7. Push ``dev`` to the local origin on success.
    8. Optionally rebuild the Serena cache (when *serena* is ``True``).
    9. Remove the temporary clone.

    :param root_dir: Absolute path to the project root git repository.
    :type root_dir: str
    :param task_id: Fully-qualified task ID matching the branch name pattern
        ``ai-phase-<safe_task_id>``.
    :type task_id: str
    :param presubmit_cmd: Shell command string used to verify the merged state.
    :type presubmit_cmd: str
    :param backend: AI backend for the merge conflict-resolution agent.
        Defaults to ``"gemini"``.
    :type backend: str
    :param max_retries: Maximum number of merge/verify attempts before giving
        up.  Defaults to ``3``.
    :type max_retries: int
    :param cache_lock: Lock for serialising Serena cache rebuilds across worker
        threads.  Required when *serena* is ``True``.
    :type cache_lock: Optional[threading.Lock]
    :param serena: When ``True`` and the merge push succeeded, trigger a Serena
        cache rebuild so subsequent tasks have an up-to-date code index.
        Defaults to ``False``.
    :type serena: bool
    :returns: ``True`` if the branch was successfully merged into ``dev`` and
        the presubmit passed; ``False`` otherwise.
    :rtype: bool
    """
    phase_part, name_part = task_id.split("/", 1)
    safe_name_part = name_part.replace("/", "_").replace(".md", "")
    branch_name = f"ai-phase-{safe_name_part}"

    _log = dashboard.log if dashboard else print

    if dashboard:
        dashboard.set_agent(task_id, "Merge", "running", "Starting merge...")

    # Extract task title for the commit message
    task_details = get_task_details(task_id)
    commit_msg = f"{phase_part}:{name_part}: Standardized Implementation"
    match = re.search(r'^#\s*Task:\s*(.*?)(?:\s*\(Sub-Epic:.*?\))?$', task_details, re.MULTILINE)
    if match and match.group(1).strip():
        commit_msg = f"{phase_part}:{name_part}: {match.group(1).strip()}"

    # We clone into a new tmpdir to avoid messing with the developer's main working tree
    tmpdir = tempfile.mkdtemp(prefix=f"merge_{safe_name_part}_")

    _log(f"\n   => [Merge] Attempting to squash merge {task_id} into dev...")
    _log(f"      Cloning repository to {tmpdir}...")
    
    # Clone from remote_url (GitHub/GitLab) if provided, otherwise fall back to root_dir
    clone_src = remote_url or root_dir
    subprocess.run(["git", "clone", clone_src, tmpdir], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    subprocess.run(["git", "checkout", dev_branch], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    push_succeeded = False
    try:
        context = {
            "phase_filename": phase_part,
            "task_name": name_part,
            "branches_list": branch_name,
            "description_ctx": get_project_context()
        }
        
        # 1. Verification Loop for Merge
        for attempt in range(1, max_retries + 1):
            failure_output = ""
            if attempt == 1:
                # First attempt: Try a squash merge via git CLI
                _log(f"      [Merge] Attempting squash merge (Attempt 1/{max_retries})...")
                # Checkout branch to fetch it into the clone
                subprocess.run(["git", "fetch", "origin", branch_name], cwd=tmpdir, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                # Perform squash merge
                merge_res = subprocess.run(["git", "merge", "--squash", f"origin/{branch_name}"], cwd=tmpdir, capture_output=True, text=True)
                
                if merge_res.returncode == 0:
                    # Check if there are actually changes to commit
                    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmpdir, capture_output=True, text=True)
                    if status.stdout.strip():
                        # Squash merge staged the changes, now commit them
                        subprocess.run(["git", "commit", "--no-verify", "-m", commit_msg], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL)
                    else:
                        _log(f"      [Merge] No changes to squash merge for {task_id}.")
                    
                    _log(f"      [Merge] Squash successful. Verifying with presubmit...")
                    cmd_list = presubmit_cmd.split()
                    presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True, start_new_session=True)
                    
                    if presubmit_res.returncode == 0:
                        _log(f"      [Merge] Presubmit passed! Pushing to origin.")
                        _commit_state_in_clone(tmpdir, workflow_state, _log)
                        res = subprocess.run(["git", "push", "--force-with-lease", "origin", dev_branch], cwd=tmpdir, capture_output=True, text=True)
                        if res.returncode != 0:
                            _log(f"      [!] Failed to push merge to origin:\n{res.stderr}")
                            return False
                        push_succeeded = True
                        return True
                    else:
                        _log(f"      [Merge] Presubmit failed after squash merge.")
                        failure_output = f"{presubmit_res.stdout}\n{presubmit_res.stderr}"
                else:
                    _log(f"      [Merge] Squash merge failed (conflicts). Attempting rebase...")
                    # Clean up failed squash merge state before rebasing
                    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # Let's try to rebase the task branch onto the current dev to help resolve conflicts
                    rebase_res = subprocess.run(["git", "rebase", dev_branch, f"origin/{branch_name}"], cwd=tmpdir, capture_output=True, text=True)
                    if rebase_res.returncode == 0:
                        _log(f"      [Merge] Rebase successful. Retrying squash merge...")
                        # Now that we rebased origin/{branch_name} locally, let's try to squash it into dev
                        new_task_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmpdir, capture_output=True, text=True).stdout.strip()
                        subprocess.run(["git", "checkout", dev_branch], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                        # Try squash again from the newly rebased head
                        merge_res = subprocess.run(["git", "merge", "--squash", new_task_head], cwd=tmpdir, capture_output=True, text=True)
                        if merge_res.returncode == 0:
                            # Check if there are actually changes to commit
                            status = subprocess.run(["git", "status", "--porcelain"], cwd=tmpdir, capture_output=True, text=True)
                            if status.stdout.strip():
                                subprocess.run(["git", "commit", "--no-verify", "-m", commit_msg], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL)
                            else:
                                _log(f"      [Merge] No changes to squash merge after rebase for {task_id}.")

                            cmd_list = presubmit_cmd.split()
                            presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True, start_new_session=True)
                            if presubmit_res.returncode == 0:
                                _log(f"      [Merge] Presubmit passed after rebase + squash! Pushing to origin.")
                                _commit_state_in_clone(tmpdir, workflow_state, _log)
                                res = subprocess.run(["git", "push", "--force-with-lease", "origin", dev_branch], cwd=tmpdir, capture_output=True, text=True)
                                if res.returncode != 0:
                                    _log(f"      [!] Failed to push merge to origin:\n{res.stderr}")
                                    return False
                                push_succeeded = True
                                return True
                            else:
                                _log(f"      [Merge] Presubmit failed after rebase + squash.")
                                failure_output = f"{presubmit_res.stdout}\n{presubmit_res.stderr}"
                        else:
                            _log(f"      [Merge] Squash merge still failed after rebase.")
                            failure_output = f"{merge_res.stdout}\n{merge_res.stderr}"
                    else:
                        _log(f"      [Merge] Rebase failed to apply cleanly. Aborting rebase.")
                        subprocess.run(["git", "rebase", "--abort"], cwd=tmpdir, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        # Ensure we are back on dev
                        subprocess.run(["git", "checkout", dev_branch], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        failure_output = f"{rebase_res.stdout}\n{rebase_res.stderr}"
            else:
                # Merge Agent Attempt
                _log(f"      [Merge] Spawning Merge Agent to resolve conflicts (Attempt {attempt}/{max_retries})...")
                
                # Reset to clean dev before the agent tries
                subprocess.run(["git", "reset", "--hard", f"origin/{dev_branch}"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "clean", "-fd"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                failure_ctx = dict(context)
                failure_ctx["description_ctx"] += f"\n\n### PREVIOUS ATTEMPT FAILURE\nThe previous squash merge or presubmit failed with:\n```\n{failure_output}\n```\n"
                failure_ctx["description_ctx"] += f"\nPlease resolve the conflicts and ensure the final state is a single commit on the {dev_branch} branch with the message: {commit_msg}"
                
                if not run_agent("Merge", "merge_task.md", failure_ctx, tmpdir, backend, dashboard=dashboard, task_id=task_id, model=model, agent_pool=agent_pool):
                    _log(f"      [!] Merge agent failed to cleanly exit.")
                    continue

                _reclaim_dir_ownership(tmpdir, _log)

                # The agent claims it's done. Let's verify.
                _log(f"      [Merge] Verifying agent's merge...")
                cmd_list = presubmit_cmd.split()
                presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True, start_new_session=True)
                
                if presubmit_res.returncode == 0:
                     _log(f"      [Merge] Presubmit passed! Pushing to origin.")
                     _commit_state_in_clone(tmpdir, workflow_state, _log)
                     res = subprocess.run(["git", "push", "--force-with-lease", "origin", dev_branch], cwd=tmpdir, capture_output=True, text=True)
                     if res.returncode != 0:
                         _log(f"      [!] Failed to push merge to origin:\n{res.stderr}")
                         return False
                     push_succeeded = True
                     return True
                else:
                     failure_output = f"{presubmit_res.stdout}\n{presubmit_res.stderr}"
                     _log(f"      [Merge] Presubmit failed after agent merge.")
                     
        _log(f"   -> [!] Failed to merge {task_id} after {max_retries} attempts.")
        return False
        
    finally:
        if push_succeeded and serena and cache_lock is not None:
            rebuild_serena_cache(tmpdir, root_dir, cache_lock, dashboard=dashboard)
        # Cleanup clone
        _log(f"      Cleaning up merge clone {tmpdir}...")
        subprocess.run(["rm", "-rf", tmpdir])


def load_blocked_tasks() -> set:  # type: ignore[type-arg]
    """Load the set of blocked task IDs from the replan state file.

    Returns an empty set when the file is absent or cannot be parsed.

    :returns: Set of blocked task reference strings.
    :rtype: set
    """
    replan_state_file = REPLAN_STATE_FILE
    if os.path.exists(replan_state_file):
        with open(replan_state_file, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
                return set(state.get("blocked_tasks", {}).keys())
            except (json.JSONDecodeError, AttributeError):
                pass
    return set()


def get_ready_tasks(master_dag: Dict[str, List[str]], completed_tasks: List[str], active_tasks: List[str]) -> List[str]:
    """Return tasks whose prerequisites are met and that are not already active or done.

    Implements a *phase barrier*: only tasks belonging to the lowest-numbered
    incomplete phase are eligible to run.  This ensures phase N is fully merged
    before phase N+1 begins.

    Blocked tasks (from :func:`load_blocked_tasks`) are excluded from both
    eligibility and prerequisite satisfaction checks — a dependency on a
    blocked task is never considered met.

    :param master_dag: Mapping of ``task_id -> [prerequisite_task_ids]`` for
        all tasks across all phases.
    :type master_dag: Dict[str, List[str]]
    :param completed_tasks: List of task IDs that have been successfully
        merged into ``dev``.
    :type completed_tasks: List[str]
    :param active_tasks: List of task IDs currently being processed by worker
        threads.
    :type active_tasks: List[str]
    :returns: Sorted list of task IDs ready to be submitted for execution.
    :rtype: List[str]
    """
    ready = []
    completed_set = set(completed_tasks)
    blocked_set = load_blocked_tasks()

    # 1. First, find all tasks that are ready regardless of phase
    all_ready = []
    for task_id, prereqs in master_dag.items():
        if task_id in completed_set or task_id in active_tasks or task_id in blocked_set:
            continue

        # Check if all prerequisites are in the completed set (blocked prereqs are NOT met)
        if all(prereq in completed_set for prereq in prereqs):
            all_ready.append(task_id)

    if not all_ready:
        return []

    # 2. Find the lowest (earliest) phase among all incomplete tasks to act as a barrier
    incomplete_tasks = [tid for tid in master_dag.keys() if tid not in completed_set]
    if not incomplete_tasks:
         return []
         
    incomplete_tasks.sort(key=phase_sort_key)
    active_phase_num = phase_sort_key(incomplete_tasks[0])[0]

    # 3. Filter ready tasks to only allow tasks from the active phase
    for task_id in all_ready:
         if phase_sort_key(task_id)[0] == active_phase_num:
             ready.append(task_id)
            
    # Sort the final ready list
    ready.sort(key=phase_sort_key)
    return ready


def execute_dag(root_dir: str, master_dag: Dict[str, List[str]], state: Dict[str, Any], jobs: int, presubmit_cmd: str, backend: str = "gemini", log_file: Any = None, model: Optional[str] = None, agent_pool: Optional[AgentPoolManager] = None) -> None:
    """Orchestrate parallel task execution according to the dependency DAG.

    Runs a scheduling loop inside a :class:`~concurrent.futures.ThreadPoolExecutor`
    with *jobs* workers.  On each iteration it resolves ready tasks via
    :func:`get_ready_tasks`, submits them to the pool, waits for at least one
    to finish, and then calls :func:`merge_task` for each successful result.

    Workflow state (completed/merged tasks) is persisted after every successful
    merge so the run can be resumed after an interruption.

    Serena integration:

    * If ``.workflow.jsonc`` has ``"serena": true``, a ``.mcp.json`` is copied
      from the template if missing, and the Serena cache is bootstrapped before
      the first task runs.

    Termination conditions:

    * All tasks completed and merged → clean exit.
    * Any task fails implementation or merging → ``sys.exit(1)`` after draining.
    * DAG deadlock (no tasks running and none ready) → ``os._exit(1)``.
    * ``SIGINT`` received → graceful drain then exit.

    :param root_dir: Absolute path to the project root git repository.
    :type root_dir: str
    :param master_dag: Merged DAG mapping ``task_id -> [prerequisite_ids]``.
    :type master_dag: Dict[str, List[str]]
    :param state: Workflow state dict (from :func:`~workflow_lib.state.load_workflow_state`)
        containing at least ``"completed_tasks"`` and ``"merged_tasks"`` lists.
    :type state: Dict[str, Any]
    :param jobs: Maximum number of concurrent worker threads.
    :type jobs: int
    :param presubmit_cmd: Shell command used to verify each task's
        implementation and merge result.
    :type presubmit_cmd: str
    :param backend: AI backend for implementation and merge agents.  Defaults
        to ``"gemini"``.
    :type backend: str
    :raises SystemExit: When one or more tasks fail.
    """
    serena_enabled = get_serena_enabled()
    dev_branch = get_dev_branch()

    # Ensure dev branch exists
    res = subprocess.run(["git", "rev-parse", "--verify", dev_branch], cwd=root_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        subprocess.run(["git", "branch", dev_branch, "main"], cwd=root_dir, check=True)

    cache_lock = threading.Lock()

    global _active_dashboard
    atexit.register(_restore_terminal)
    with make_dashboard(log_file=log_file) as dashboard:
        _active_dashboard = dashboard
        try:
            _execute_dag_inner(root_dir, master_dag, state, jobs, presubmit_cmd, backend, serena_enabled, cache_lock, dashboard, model=model, dev_branch=dev_branch, agent_pool=agent_pool)
        finally:
            _active_dashboard = None


def _execute_dag_inner(root_dir: str, master_dag: Dict[str, List[str]], state: Dict[str, Any], jobs: int, presubmit_cmd: str, backend: str, serena_enabled: bool, cache_lock: threading.Lock, dashboard: Any, model: Optional[str] = None, dev_branch: str = "dev", agent_pool: Optional[AgentPoolManager] = None) -> None:
    """Inner DAG execution loop run inside the dashboard context manager."""
    pivot_remote = get_pivot_remote()
    try:
        remote_url: Optional[str] = get_gitlab_remote_url(root_dir, remote_name=pivot_remote)
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        remote_url = None

    if serena_enabled:
        # Ensure .mcp.json exists at project root (copy from template if missing)
        mcp_dst = os.path.join(root_dir, ".mcp.json")
        if not os.path.exists(mcp_dst):
            mcp_template = os.path.join(TOOLS_DIR, "templates", ".mcp.json")
            if os.path.exists(mcp_template):
                shutil.copy2(mcp_template, mcp_dst)
                dashboard.log(f"=> [Serena] Copied .mcp.json template to {mcp_dst}")

        # Bootstrap Serena cache from dev if not present
        serena_cache = os.path.join(root_dir, ".serena", "cache")
        if not os.path.isdir(serena_cache):
            dashboard.log(f"=> [Serena] No cache found. Bootstrapping index from {dev_branch} branch...")
            init_clone = tempfile.mkdtemp(prefix="serena_init_")
            try:
                subprocess.run(["git", "clone", root_dir, init_clone],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                subprocess.run(["git", "submodule", "update", "--init", "--recursive"],
                               cwd=init_clone, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                subprocess.run(["git", "checkout", dev_branch],
                               cwd=init_clone, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                rebuild_serena_cache(init_clone, root_dir, cache_lock, dashboard=dashboard)
            finally:
                shutil.rmtree(init_clone, ignore_errors=True)

    active_tasks: set = set()
    failed_tasks: set = set()
    state_lock = threading.Lock()

    dashboard.log("=> Starting Parallel DAG Execution Loop...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        # Dictionary to keep track of futures mapping to task_id
        future_to_task = {}

        while True:
            # Check for newly ready tasks
            ready_tasks = []
            if not shutdown_requested and not failed_tasks:
                with state_lock:
                    ready_tasks = get_ready_tasks(master_dag, state["completed_tasks"], list(active_tasks))

            # Submit ready tasks if we have capacity
            for task_id in ready_tasks:
                if len(active_tasks) >= jobs:
                    break

                with state_lock:
                    active_tasks.add(task_id)

                future = executor.submit(process_task, root_dir, task_id, presubmit_cmd, backend, serena=serena_enabled, dashboard=dashboard, model=model, dev_branch=dev_branch, remote_url=remote_url, agent_pool=agent_pool)
                future_to_task[future] = task_id

            # If no tasks are running and (none are ready or shutdown requested), we are done/deadlocked
            if not future_to_task:
                if shutdown_requested:
                    dashboard.log("=> Graceful shutdown complete. Exiting.")
                    break

                with state_lock:
                    if failed_tasks:
                        break

                    blocked = load_blocked_tasks()
                    non_blocked_total = len([t for t in master_dag if t not in blocked])
                    if len(state["completed_tasks"]) >= non_blocked_total:
                        dashboard.log("=> All implementation tasks completed successfully!")
                        if blocked:
                            dashboard.log(f"   ({len(blocked)} blocked task(s) skipped)")
                        break
                    else:
                        dashboard.log("[!] FATAL: DAG deadlock or unrecoverable error. No tasks running and none ready.")
                        dashboard.log(f"    Completed: {len(state['completed_tasks'])} / {non_blocked_total} (non-blocked)")
                        notify_failure("DAG deadlock or unrecoverable error — no tasks running and none ready.",
                                       context=f"Completed: {len(state['completed_tasks'])} / {non_blocked_total}")
                        _restore_terminal()
                        os._exit(1)

            # Wait for at least one future to complete
            done, not_done = concurrent.futures.wait(
                future_to_task.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED
            )

            for future in done:
                task_id = future_to_task.pop(future)
                with state_lock:
                    active_tasks.remove(task_id)

                try:
                    success = future.result()
                    if success:
                        dashboard.log(f"   -> [Implementation] Task {task_id} completed successfully.")

                        # Build the pending state the merge commit should reflect
                        with state_lock:
                            pending_state = {
                                "completed_tasks": list(state.get("completed_tasks", [])) + [task_id],
                                "merged_tasks":    list(state.get("merged_tasks", []))    + [task_id],
                            }

                        # Trigger DAG Merge Workflow immediately
                        if merge_task(root_dir, task_id, presubmit_cmd, backend, cache_lock=cache_lock, serena=serena_enabled, dashboard=dashboard, model=model, dev_branch=dev_branch, remote_url=remote_url, workflow_state=pending_state, agent_pool=agent_pool):
                            dashboard.set_agent(task_id, "Merge", "done", "Merged successfully")
                            with state_lock:
                                state["completed_tasks"].append(task_id)
                                state["merged_tasks"].append(task_id)
                                save_workflow_state(state)
                            dashboard.log(f"   -> [Success] Task {task_id} fully integrated into {dev_branch}.")
                            # Sync local dev-branch ref from remote (succeeds now: merge_task just pushed there).
                            fetch_res = subprocess.run(
                                ["git", "fetch", pivot_remote, f"+{dev_branch}:{dev_branch}"],
                                cwd=root_dir, capture_output=True, text=True,
                            )
                            if fetch_res.returncode != 0:
                                dashboard.log(f"      [!] Warning: Failed to sync local {dev_branch}: {fetch_res.stderr.strip()}")
                            else:
                                dashboard.log(f"      [Push] Success.")
                        else:
                            dashboard.set_agent(task_id, "Merge", "failed", f"Failed to merge into {dev_branch}")
                            with state_lock:
                                failed_tasks.add(f"Task {task_id} failed merging into {dev_branch}.")
                    else:
                        with state_lock:
                            failed_tasks.add(f"Task {task_id} failed implementation.")
                except Exception as exc:
                    traceback.print_exc()
                    with state_lock:
                        failed_tasks.add(f"Task {task_id} generated an exception.")

    if failed_tasks:
        dashboard.log("\n" + "="*80)
        for err in failed_tasks:
            dashboard.log(f"[!] FATAL: {err} Halting workflow.")
        dashboard.log("="*80 + "\n")
        notify_failure("Run workflow halted due to task failures.",
                       context="\n".join(failed_tasks))
        sys.exit(1)


