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
from .context import ProjectContext, fit_lines_to_budget, _count_tokens
from .runners import IMAGE_EXTENSIONS, make_runner
from .state import save_workflow_state, load_dags, get_tasks_dir
from .config import get_serena_enabled, get_dev_branch, get_pivot_remote, get_docker_config, get_rag_enabled, get_sccache_config, get_sccache_dist_config, get_context_limit, set_agent_context_limit
from .discord import notify_failure
from .dashboard import make_dashboard
from .agent_pool import AgentPoolManager, QUOTA_RETURN_CODE, QUOTA_PATTERNS, QUOTA_TRANSIENT_PATTERNS, parse_quota_reset_seconds
from .rag_integration import get_rag_help_text, start_rag_server

shutdown_requested = False
_active_dashboard: Any = None
_active_containers: set = set()  # container names currently running
_active_containers_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Stage constants for restartable process_task pipeline
# ---------------------------------------------------------------------------

STAGE_IMPL     = "impl"
STAGE_REVIEW   = "review"
STAGE_VALIDATE = "validate"
STAGE_DONE     = "done"
_STAGE_ORDER   = [STAGE_IMPL, STAGE_REVIEW, STAGE_VALIDATE]


def _starting_stage_for(task_id: str, state: Dict[str, Any]) -> str:
    """Return the first stage that still needs to run for *task_id*.

    :param task_id: Fully-qualified task ID, e.g. ``"phase_1/sub/01.md"``.
    :param state: Workflow state dict (must contain ``"task_stages"`` key).
    :returns: One of the ``STAGE_*`` constants, or ``STAGE_DONE`` when all
        stages have already been recorded.
    """
    completed = state.get("task_stages", {}).get(task_id)
    if completed is None:
        return STAGE_IMPL
    try:
        idx = _STAGE_ORDER.index(completed)
    except ValueError:
        return STAGE_IMPL
    return _STAGE_ORDER[idx + 1] if idx + 1 < len(_STAGE_ORDER) else STAGE_DONE


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
            _active_dashboard.log("[!] Graceful shutdown initiated. Current stages will complete before stopping.")
        else:
            print("\n[!] Ctrl-C detected. Initiating graceful shutdown...")
            print("    Active agents will finish their current stage. No new agents will be spawned.")
            print("    Progress will be saved so tasks can resume on next run.")
    else:
        _restore_terminal()
        print("\n[!] Ctrl-C detected again. Forcing immediate exit...")
        # Clean up any active Docker containers before hard exit
        with _active_containers_lock:
            containers = list(_active_containers)
        for name in containers:
            try:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False,
                               timeout=5)
            except Exception:
                pass
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

# Host-identity env vars that must not be forwarded into Docker containers.
# Each AI CLI resolves its config directory from HOME; forwarding a host-side
# HOME that doesn't exist inside the container causes an immediate ENOENT crash.
_DOCKER_ENV_SKIP = frozenset({"HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "PATH"})


def _write_container_env_file(tmpdir: str) -> str:
    """Write current process env (minus identity vars) to *tmpdir*/container.env.

    :param tmpdir: Directory in which to create the env file.
    :returns: Absolute path to the written env file.
    """
    import tempfile as _tempfile
    path = os.path.join(tmpdir, "container.env")
    with open(path, "w", encoding="utf-8") as f:
        for key, val in os.environ.items():
            if key in _DOCKER_ENV_SKIP:
                continue
            # Skip vars with newlines or '=' in the key — invalid env-file format.
            if "\n" in key or "\n" in val or "=" in key:
                continue
            f.write(f"{key}={val}\n")
    return path


def _docker_exec(
    container_name: str,
    cmd: List[str],
    *,
    env_file: str = "",
    capture: bool = True,
    check: bool = False,
    log: Optional[Callable] = None,
) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """Run *cmd* inside *container_name* via ``docker exec``.

    :param container_name: Name of the running Docker container.
    :param cmd: Command list to execute inside the container.
    :param env_file: Path to an env-file on the host passed via ``--env-file``.
    :param capture: When ``True``, capture stdout/stderr (default).
    :param check: When ``True``, raise on non-zero exit.
    :param log: Optional callable for logging stderr on failure.
    :returns: :class:`subprocess.CompletedProcess`.
    """
    import time as _time
    _exec_start = _time.time()
    
    # Log the exec attempt with container name and command
    if log:
        log(f"      [docker exec] BEGIN: container={container_name}, cmd={' '.join(cmd[:3])}...")
    
    exec_cmd = ["docker", "exec", "-i", "--workdir", "/workspace"]
    if env_file:
        exec_cmd += ["--env-file", env_file]
    exec_cmd += [container_name] + cmd
    result = subprocess.run(
        exec_cmd,
        capture_output=capture,
        text=True,
        check=False,
    )
    elapsed = _time.time() - _exec_start
    
    if result.returncode != 0:
        if log:
            log(f"      [docker exec] END: container={container_name}, rc={result.returncode}, elapsed={elapsed:.2f}s")
            if "No such exec instance" in result.stderr:
                log(f"      [docker exec] ERROR: Container {container_name} does not exist or was removed")
            if result.stderr.strip():
                log(f"      [docker exec] stderr: {result.stderr.strip()}")
        if check:
            if log:
                log(f"      [!] docker exec failed (rc={result.returncode}): {' '.join(cmd)}")
                if result.stderr:
                    log(f"          {result.stderr.strip()}")
            raise subprocess.CalledProcessError(result.returncode, exec_cmd, result.stdout, result.stderr)
    else:
        if log:
            log(f"      [docker exec] OK: container={container_name}, rc=0, elapsed={elapsed:.2f}s")
    
    return result


def _start_task_container(
    container_name: str,
    docker_config: Any,
    env_file: str,
    log: Callable,
    sccache_config: Optional[Any] = None,
    sccache_dist_config: Optional[Any] = None,
    configure_containers: bool = True,
) -> None:
    """Start a detached Docker container for the duration of one workflow task.

    Validates that all ``copy_files`` sources exist on the host, then runs
    ``docker run -d --name <name> --env-file <env_file> <volumes> <image> sleep infinity``.
    After the container starts, ``copy_files`` are copied in via ``docker cp`` to
    avoid permission issues with bind-mounted files (e.g. ``.git-credentials``).

    When *sccache_config* is provided and enabled, configures the container to
    connect to the host sccache server for Rust build caching.

    When *sccache_dist_config* is provided and enabled, configures the container
    to connect to the sccache-dist scheduler for distributed compilation.

    :param container_name: Unique name for the container.
    :param docker_config: :class:`~workflow_lib.agent_pool.DockerConfig`.
    :param env_file: Path to the env-file written by :func:`_write_container_env_file`.
    :param log: Callable for status messages.
    :param sccache_config: Optional :class:`~workflow_lib.config.SCCacheConfig`.
        When provided and enabled, adds sccache environment variables and host mapping.
    :param sccache_dist_config: Optional :class:`~workflow_lib.config.SCCacheDistConfig`.
        When provided and enabled, adds sccache-dist environment variables and host mapping.
    :param configure_containers: Whether to configure containers with sccache environment
        variables. Defaults to True. When False, containers run without sccache configuration.
    :raises FileNotFoundError: If a ``copy_files`` src path does not exist.
    """
    import warnings as _warnings
    import time as _time
    dc = docker_config
    
    _start_time = _time.time()
    log(f"      [docker] BEGIN container creation: {container_name}")

    # Build volume flags
    volume_flags: List[str] = []
    for vol in dc.volumes:
        volume_flags += ["-v", vol]

    # Collect existing volume dests to detect duplicates
    mounted_dests: set = set()
    for vol in dc.volumes:
        parts = vol.split(":")
        if len(parts) >= 2:
            mounted_dests.add(parts[1])

    # Validate copy_files sources and track which need docker cp (not bind mount)
    copy_files_to_cp = []
    for cf in dc.copy_files:
        result = subprocess.run(["sudo", "test", "-e", cf.src], check=False)
        if result.returncode != 0:
            log(f"      [!] docker copy_files src does not exist: {cf.src!r}")
            raise FileNotFoundError(f"docker copy_files src does not exist: {cf.src!r}")
        if cf.dest in mounted_dests:
            _warnings.warn(
                f"docker opy_files dest {cf.dest!r} duplicates an existing volume mount — skipping",
                stacklevel=3,
            )
            continue
        # Use docker cp for all copy_files to avoid permission issues with bind mounts
        copy_files_to_cp.append(cf)

    # Build docker run command
    docker_cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--env-file", env_file,
    ] + volume_flags

    # Connect agent to the sccache Redis container via a shared Docker network.
    # Each container runs its own local sccache server backed by Redis.
    if configure_containers and sccache_config is not None and sccache_config.enabled:
        redis_url = f"redis://{sccache_config.redis_container}:{sccache_config.redis_port}"
        docker_cmd += [
            "--network", sccache_config.network,
            "-e", "RUSTC_WRAPPER=sccache",
            "-e", f"SCCACHE_REDIS={redis_url}",
        ]
        log(f"      [sccache] Redis backend: {redis_url} (network: {sccache_config.network})")

    # Add sccache-dist configuration if enabled and configure_containers is True
    if configure_containers and sccache_dist_config is not None and sccache_dist_config.enabled:
        # Add --add-host for host.docker.internal resolution (Linux requires host-gateway)
        docker_cmd += ["--add-host", "host.docker.internal:host-gateway"]
        docker_cmd += [
            "-e", "RUSTC_WRAPPER=sccache",
            "-e", f"SCCACHE_DIST_SCHEDULER_URL={sccache_dist_config.scheduler_url}",
            "-e", f"SCCACHE_AUTH_TOKEN={sccache_dist_config.auth_token}",
        ]
        log(f"      [sccache-dist] Configuring container for scheduler at {sccache_dist_config.scheduler_url}")

    docker_cmd += ["--memory", "20g", "--memory-swap", "20g"]

    docker_cmd += [dc.image, "sleep", "infinity"]

    log(f"      [docker] Starting container {container_name} ({dc.image})...")
    start_result = subprocess.run(docker_cmd, check=True, capture_output=True)
    log(f"      [docker] Container {container_name} started in {_time.time() - _start_time:.2f}s (rc={start_result.returncode})")
    with _active_containers_lock:
        _active_containers.add(container_name)
    log(f"      [docker] Container {container_name} registered in _active_containers")

    # Verify container is running after creation
    verify_res = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        capture_output=True, text=True, check=False
    )
    if verify_res.returncode != 0 or verify_res.stdout.strip() != "true":
        log(f"      [!] Container {container_name} failed to start or exited immediately")
        with _active_containers_lock:
            _active_containers.discard(container_name)
        raise RuntimeError(f"Container {container_name} is not running after creation")
    log(f"      [docker] Container {container_name} verified as running")

    # Copy files into the container after it starts to avoid permission issues
    # with bind-mounted files (e.g. .git-credentials with mode 0600)
    for cf in copy_files_to_cp:
        # Verify container still exists before each copy operation
        check_res = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name=^{container_name}$"],
            capture_output=True, text=True, check=False
        )
        if not check_res.stdout.strip():
            log(f"      [!] Container {container_name} disappeared during copy_files loop")
            with _active_containers_lock:
                _active_containers.discard(container_name)
            raise RuntimeError(f"Container {container_name} disappeared during file copy")
        
        # Ensure parent directory exists
        parent_dir = os.path.dirname(cf.dest)
        if parent_dir:
            mkdir_cmd = ["docker", "exec", "-i", "--workdir", "/workspace",
                        container_name, "sudo", "mkdir", "-p", parent_dir]
            mkdir_result = subprocess.run(mkdir_cmd, capture_output=True, text=True, check=False)
            if mkdir_result.returncode != 0:
                log(f"      [!] docker exec mkdir failed: {mkdir_result.stderr.strip()}")
        # Copy the file (use sudo to read files owned by other users)
        cp_cmd = ["sudo", "docker", "cp", cf.src, f"{container_name}:{cf.dest}"]
        cp_result = subprocess.run(cp_cmd, capture_output=True, text=True, check=False)
        if cp_result.returncode != 0:
            log(f"      [!] docker cp failed for {cf.src} -> {cf.dest}: {cp_result.stderr.strip()}")
        else:
            # Make the file readable and writable by the container user
            chmod_cmd = ["docker", "exec", "-i", "--workdir", "/workspace",
                        container_name, "sudo", "chmod", "644", cf.dest]
            subprocess.run(chmod_cmd, capture_output=True, check=False)
            # Chown file and parent dir to the container's default user.
            # We first query the non-root user, then sudo chown to that user.
            who_cmd = ["docker", "exec", "-i", "--workdir", "/workspace",
                       container_name, "id", "-un"]
            who_result = subprocess.run(who_cmd, capture_output=True, text=True, check=False)
            container_user = who_result.stdout.strip()
            if container_user and container_user != "root":
                chown_cmd = ["docker", "exec", "-i", "--workdir", "/workspace",
                            container_name, "sudo", "chown",
                            f"{container_user}:{container_user}",
                            cf.dest, parent_dir]
                subprocess.run(chown_cmd, capture_output=True, check=False)

    # Final verification before returning to caller
    final_check = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=^{container_name}$"],
        capture_output=True, text=True, check=False
    )
    if not final_check.stdout.strip():
        log(f"      [!] Container {container_name} disappeared after copy_files loop")
        with _active_containers_lock:
            _active_containers.discard(container_name)
        raise RuntimeError(f"Container {container_name} disappeared before git clone")
    log(f"      [docker] Container {container_name} verified after copy_files loop")


def _stop_task_container(container_name: str, log: Callable) -> None:
    """Remove *container_name* (best-effort, errors are logged not raised).

    :param container_name: Name of the container to remove.
    :param log: Callable for status messages.
    """
    if not container_name:
        return
    import time as _time
    _stop_start = _time.time()
    log(f"      [docker] BEGIN container removal: {container_name}")
    
    # Check if container exists before attempting removal
    inspect_result = subprocess.run(
        ["docker", "inspect", container_name],
        capture_output=True, text=True, check=False
    )
    if inspect_result.returncode != 0:
        log(f"      [docker] Container {container_name} does not exist (already removed?)")
        with _active_containers_lock:
            _active_containers.discard(container_name)
        return
    
    stop_result = subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True, text=True, check=False
    )
    elapsed = _time.time() - _stop_start
    if stop_result.returncode == 0:
        log(f"      [docker] Container {container_name} removed in {elapsed:.2f}s")
    else:
        log(f"      [docker] Container {container_name} removal failed in {elapsed:.2f}s: {stop_result.stderr.strip()}")
    with _active_containers_lock:
        _active_containers.discard(container_name)


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
    container_name: Optional[str] = None,
    container_env_file: str = "",
    spawn_rate: float = 0.0,
    agent_env: Optional[Dict[str, str]] = None,
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
    :param container_name: Optional name of a running Docker container.
        When set, the CLI is routed into the container via ``docker exec``.
    :param container_env_file: Path to the env-file for ``docker exec --env-file``.
    :param spawn_rate: Agent spawn-rate in seconds (from :attr:`AgentConfig.spawn_rate`).
        When a quota message advertises a reset time ≤ this value the process is
        *not* killed — the CLI will recover before the next task would be spawned
        anyway.  Defaults to ``0.0`` (only suppress if reset is instantaneous).
    :param agent_env: Optional dict of per-agent environment variables from
        :attr:`~workflow_lib.agent_pool.AgentConfig.env`.
    :returns: Tuple of (return_code, stderr_text).
    """
    from .config import get_config_defaults
    cfg = get_config_defaults()
    soft_timeout = cfg.get("soft_timeout")
    hard_timeout = cfg.get("timeout")

    idle_timeout = cfg.get("idle_timeout", 1200)
    runner = make_runner(backend, model=model, soft_timeout=soft_timeout, user=user, container_name=container_name, env=agent_env, idle_timeout=idle_timeout)
    if container_name and container_env_file:
        runner._container_env_file = container_env_file

    quota_detected = [False]
    # Tracks whether a quota pattern was seen but suppressed because the CLI
    # was handling the retry itself.  If the process still exits non-zero after
    # all its internal retries, we treat it as a quota failure so run_agent can
    # rotate to a different agent.
    quota_seen_transient = [False]
    quota_patterns_lower = [p.lower() for p in QUOTA_PATTERNS]
    quota_transient_lower = [p.lower() for p in QUOTA_TRANSIENT_PATTERNS]
    abort_event = threading.Event()

    # Cross-line window: some CLIs (e.g. Gemini) output "Retrying with backoff"
    # on one line and the quota detail ("No capacity available...") on a later
    # line of the same multi-line error block.  We track how recently a transient
    # retry indicator was seen so quota patterns in subsequent lines are suppressed.
    _transient_lines_remaining = [0]
    _TRANSIENT_WINDOW = 15  # lines after a retry indicator to stay suppressed

    def output_line(line: str) -> None:
        line_lower = line.lower()

        # Detect a transient-retry indicator on this line and open a suppression window.
        if any(t in line_lower for t in quota_transient_lower):
            _transient_lines_remaining[0] = _TRANSIENT_WINDOW
        elif _transient_lines_remaining[0] > 0:
            _transient_lines_remaining[0] -= 1

        if any(p in line_lower for p in quota_patterns_lower):
            # Suppress abort if the CLI signalled a retry on this or a recent line.
            if _transient_lines_remaining[0] > 0:
                quota_seen_transient[0] = True  # quota seen; CLI is handling it internally
                pass  # let the CLI recover on its own
            else:
                # Parse the advertised reset time.  If it's within the agent's
                # spawn window (spawn_rate), the quota will lift before we'd start
                # the next task anyway — no point killing and rotating.
                reset_secs = parse_quota_reset_seconds(line)
                if reset_secs is None or reset_secs > spawn_rate:
                    quota_detected[0] = True
                    abort_event.set()
        if on_line:
            on_line(line)
        else:
            print(f"{prefix}{line}")
            sys.stdout.flush()

    try:
        result = runner.run(cwd, prompt, image_paths=image_paths, on_line=output_line, timeout=hard_timeout, abort_event=abort_event)
        stderr_text = result.stderr or ""
        if quota_detected[0]:
            return QUOTA_RETURN_CODE, "quota exceeded"
        # The CLI handled quota retries internally but ultimately gave up (non-zero exit).
        # Treat this as a quota failure so run_agent can rotate to a different agent.
        if quota_seen_transient[0] and result.returncode != 0:
            return QUOTA_RETURN_CODE, "quota exceeded (CLI retries exhausted)"
        return result.returncode, stderr_text
    except subprocess.TimeoutExpired:
        if quota_detected[0] or quota_seen_transient[0]:
            return QUOTA_RETURN_CODE, "quota exceeded"
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
    """Return combined contents of MEMORY.md and DECISIONS.md from the agent directory.

    MEMORY.md holds ephemeral observations (changelog, brittle areas).
    DECISIONS.md holds durable architectural decisions.  Both are injected
    together so agents have the full picture without separate template slots.

    :param root_dir: Absolute path to the project root.
    :type root_dir: str
    :returns: Concatenated file contents, or ``""`` when neither file exists.
    :rtype: str
    """
    agent_dir = os.path.join(root_dir, ".agent")
    parts = []
    for filename in ("MEMORY.md", "DECISIONS.md"):
        path = os.path.join(agent_dir, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                parts.append(f.read())
    return "\n\n---\n\n".join(parts)


def get_file_tree_context(clone_dir: str) -> str:
    """Return a sorted file tree of the repository clone, excluding .git.

    Gives agents an immediate structural overview of the codebase without
    requiring them to spend tool calls on filesystem exploration.

    :param clone_dir: Absolute path to the cloned repository directory.
    :type clone_dir: str
    :returns: Newline-separated relative file paths, or ``""`` on failure.
    :rtype: str
    """
    try:
        result = subprocess.run(
            ["find", ".", "-type", "f", "-not", "-path", "./.git/*"],
            cwd=clone_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            lines = sorted(result.stdout.splitlines())
            return "\n".join(lines)
    except (subprocess.TimeoutExpired, OSError):
        pass
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


def get_spec_context() -> str:
    """Return PRD and TAS planning documents for implementation/review agent context.

    Prefers summaries (``docs/plan/summaries/``) over full specs to stay within
    token budgets.  Returns an empty string when neither file exists.

    :returns: XML-tagged content for PRD and TAS documents.
    :rtype: str
    """
    plan_dir = os.path.join(ROOT_DIR, "docs", "plan")
    parts = []
    for doc_id, name in [("1_prd", "PRD"), ("2_tas", "TAS")]:
        summary_path = os.path.join(plan_dir, "summaries", f"{doc_id}.md")
        full_path = os.path.join(plan_dir, "specs", f"{doc_id}.md")
        for path in (summary_path, full_path):
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    parts.append(f'<spec name="{name}">\n{f.read().strip()}\n</spec>')
                break
    return "\n\n".join(parts)


def get_shared_components_context() -> str:
    """Return shared components and interface contracts documents if they exist.

    Reads ``docs/plan/shared_components.md`` and
    ``docs/plan/specs/interface_contracts.md``.  Returns an empty string when
    neither file exists.

    :returns: XML-tagged content for each document found.
    :rtype: str
    """
    plan_dir = os.path.join(ROOT_DIR, "docs", "plan")
    candidates = [
        (os.path.join(plan_dir, "shared_components.md"), "Shared Components"),
        (os.path.join(plan_dir, "specs", "interface_contracts.md"), "Interface Contracts"),
    ]
    parts = []
    for path, label in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                parts.append(f'<doc name="{label}">\n{f.read().strip()}\n</doc>')
    return "\n\n".join(parts)


# Keys in task_context whose values are large file content eligible for
# truncation.  Order determines budget priority (earlier = more budget
# when splitting evenly doesn't work).
_TRUNCATABLE_CONTEXT_KEYS = [
    "task_details",
    "spec_ctx",
    "description_ctx",
    "shared_components_ctx",
    "memory_ctx",
]


def truncate_task_context(
    task_context: Dict[str, Any],
    token_budget: int,
    prompt_tmpl: str = "",
) -> Dict[str, Any]:
    """Return a copy of *task_context* with large text values truncated to fit *token_budget*.

    Only keys listed in :data:`_TRUNCATABLE_CONTEXT_KEYS` are candidates for
    truncation.  The budget is first reduced by the token count of the static
    template text (with placeholders removed) and any non-truncatable context
    values.  The remaining budget is split evenly across the truncatable keys
    that have non-empty content.

    Each truncatable value is treated as a single "file" and truncated using
    :func:`~workflow_lib.context.fit_lines_to_budget` — the same binary-search
    algorithm used in the planning phases.  Truncated values receive an
    appended note indicating how many lines were omitted.

    :param task_context: Original key/value substitution map.
    :param token_budget: Maximum total tokens for the fully-substituted prompt.
    :param prompt_tmpl: The raw prompt template (before substitution).  Used to
        account for static text that consumes part of the budget.
    :returns: A shallow copy of *task_context* with truncatable values trimmed.
    """
    if token_budget <= 0:
        return dict(task_context)

    # Count tokens in the static template (strip placeholders).
    static_text = prompt_tmpl
    for k in task_context:
        static_text = static_text.replace(f"{{{k}}}", "")
    static_tokens = _count_tokens(static_text)

    # Count tokens in non-truncatable context values.
    fixed_tokens = static_tokens
    for k, v in task_context.items():
        if k not in _TRUNCATABLE_CONTEXT_KEYS:
            fixed_tokens += _count_tokens(str(v))

    available = max(token_budget - fixed_tokens, 0)

    # Collect truncatable entries that have content.
    trunc_items: List[tuple] = []
    for key in _TRUNCATABLE_CONTEXT_KEYS:
        val = str(task_context.get(key, ""))
        if val.strip():
            trunc_items.append((key, val))

    if not trunc_items:
        return dict(task_context)

    per_key_budget = max(available // len(trunc_items), 1)

    result = dict(task_context)
    for key, val in trunc_items:
        lines = val.splitlines(keepends=True)
        lines_limit = fit_lines_to_budget([lines], per_key_budget, use_tokens=True)
        if lines_limit < len(lines):
            truncated = "".join(lines[:lines_limit]).rstrip()
            omitted = len(lines) - lines_limit
            truncated += (
                f"\n\n... ({omitted} more lines truncated to fit context budget"
                f" — source: {key})\n"
            )
            result[key] = truncated

    return result


def run_agent(agent_type: str, prompt_file: str, task_context: Dict[str, Any], cwd: str, backend: str = "gemini", dashboard: Any = None, task_id: str = "", model: Optional[str] = None, agent_pool: Optional[AgentPoolManager] = None, container_name: Optional[str] = None, container_env_file: str = "", _pre_acquired_agent: Optional[Any] = None) -> bool:
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

    msg = f"[{agent_type}] Starting agent in {cwd}... (backend={backend})"
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

    attempt = 1
    while attempt <= max_capacity_retries:
        # Resolve backend/user/model from pool (if active) or fixed backend.
        agent_cfg = None
        active_backend = backend
        active_model = model
        active_user: Optional[str] = None

        if agent_pool is not None:
            step = _step_for_agent_type(agent_type)
            if _pre_acquired_agent is not None and attempt == 1:
                # Caller already acquired this agent (e.g. to resolve docker config before
                # starting the container); use it directly without going through the pool.
                agent_cfg = _pre_acquired_agent
                _pre_acquired_agent = None  # only skip acquire on first attempt
            else:
                if attempt > 1 and dashboard and task_id:
                    dashboard.set_agent(task_id, agent_type, "waiting",
                                        f"Waiting for available agent (attempt {attempt})...", agent_name="")
                while True:
                    agent_cfg = agent_pool.acquire(timeout=300.0, step=step)
                    if agent_cfg is not None:
                        break
                    # All agents for this step are quota-suppressed or at capacity.
                    # Log a visible waiting status and keep polling — do not treat
                    # this as fatal, as quota windows will eventually expire and
                    # running agents will eventually finish and free their slots.
                    wait_msg = f"[{agent_type}] All agents for step '{step}' are busy or quota-suppressed — waiting for a slot..."
                    if dashboard:
                        dashboard.log(f"{prefix}{wait_msg}")
                        if task_id:
                            dashboard.set_agent(task_id, agent_type, "waiting",
                                                "Waiting for available agent slot...", agent_name="")
            active_backend = agent_cfg.backend
            active_model = agent_cfg.model or model
            active_user = agent_cfg.user
            if dashboard and task_id:
                start_msg = f"Retry attempt {attempt} → {agent_cfg.name}" if attempt > 1 else ""
                dashboard.set_agent(task_id, agent_type, "running", start_msg, agent_name=agent_cfg.name)
            if dashboard:
                if attempt > 1:
                    dashboard.log(f"{prefix}[retry] Attempt {attempt}/{max_capacity_retries} starting with {agent_cfg.name} ({active_backend})")
                else:
                    dashboard.log(f"{prefix}[{agent_type}] Agent selected: {agent_cfg.name} ({active_backend})")

        # Transfer directory ownership to the agent's OS user so it can write freely.
        # Always chown when a pool is active: a previous agent may have run as a
        # different user, leaving files owned by them that this agent can't write.
        # Also chown the cargo target-dir so the agent can build without needing
        # to modify .cargo/config.toml to point at a different path.
        # Skip when docker is configured: the container manages its own permissions.
        if agent_pool is not None and active_user and not container_name:
            _log = (lambda msg: dashboard.log(msg)) if dashboard else (lambda msg: print(f"      {msg}"))
            if agent_cfg is not None and agent_cfg.cargo_target_dir:
                _set_cargo_target_dir(cwd, agent_cfg.cargo_target_dir, _log)
            _set_dir_owner(cwd, active_user, _log)
            cargo_target = _get_cargo_target_dir(cwd)
            if cargo_target and os.path.exists(cargo_target):
                _set_dir_owner(cargo_target, active_user, _log)

        # Apply per-agent context_limit so get_context_limit() returns the
        # right value for this agent (falls back to global config if None).
        if agent_cfg is not None:
            set_agent_context_limit(agent_cfg.context_limit)
        else:
            set_agent_context_limit(None)

        # Build the prompt with budget-aware truncation of large context values.
        effective_ctx = truncate_task_context(
            task_context, get_context_limit(), prompt_tmpl=prompt_tmpl,
        )
        prompt = prompt_tmpl
        for k, v in effective_ctx.items():
            prompt = prompt.replace(f"{{{k}}}", str(v))

        # Inject RAG MCP tool help text into every agent prompt (if enabled)
        if get_rag_enabled():
            rag_help = get_rag_help_text()
            prompt = f"{prompt}\n\n{rag_help}"

        returncode = 1
        stderr_text = ""
        try:
            returncode, stderr_text = run_ai_command(
                prompt, cwd, prefix=prefix, backend=active_backend,
                image_paths=get_project_images(), on_line=on_line,
                model=active_model, user=active_user,
                container_name=container_name,
                container_env_file=container_env_file,
                spawn_rate=agent_cfg.spawn_rate if agent_cfg is not None else 0.0,
                agent_env=agent_cfg.env if agent_cfg is not None else None,
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
            if agent_pool is not None:
                retry_msg = f"[{agent_type}] Quota exceeded on {agent_cfg.name if agent_cfg else active_backend}. Retrying with next available agent..."
                if dashboard:
                    dashboard.log(f"{prefix}{retry_msg}")
                else:
                    print(f"      {retry_msg}")
                # We do not increment attempt when using a pool, as it can rotate infinitely.
                continue

            if attempt < max_capacity_retries:
                retry_msg = f"[{agent_type}] Quota exceeded on {active_backend} (attempt {attempt}/{max_capacity_retries}). Retrying..."
                if dashboard:
                    dashboard.log(f"{prefix}{retry_msg}")
                else:
                    print(f"      {retry_msg}")
                attempt += 1
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
                attempt += 1
                continue

        agent_label = agent_cfg.name if agent_cfg is not None else active_backend
        err = f"[{agent_type}] FATAL: Agent process failed with exit code {returncode} (agent={agent_label})"
        if dashboard:
            dashboard.log(err)
            if stderr_text.strip():
                dashboard.log(f"[{agent_type}] stderr (agent={agent_label}): {stderr_text.strip()}")
        else:
            print(f"      {err}")
            if stderr_text.strip():
                print(f"      [{agent_type}] stderr (agent={agent_label}): {stderr_text.strip()}")
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



def _push_branch_to_origin(
    branch_name: str,
    *,
    cwd: str = "",
    container_name: str = "",
    env_file: str = "",
    _log: Callable,
) -> bool:
    """Push *branch_name* to ``origin``, retrying with ``--force-with-lease`` on rejection.

    Handles both host (plain ``subprocess.run``) and container (``docker exec``)
    push paths.  A plain push is tried first; if it is rejected due to a
    non-fast-forward conflict (race condition from a concurrent run), a
    ``--force-with-lease`` retry is attempted.

    :returns: ``True`` on success, ``False`` if the push ultimately failed.
    """
    def _do_push(extra_flags: List[str]) -> subprocess.CompletedProcess:
        cmd = ["git", "push"] + extra_flags + ["origin", branch_name]
        if container_name:
            return _docker_exec(container_name, cmd, env_file=env_file)
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

    res = _do_push([])
    if res.returncode == 0:
        return True
    if "non-fast-forward" in res.stderr or "[rejected]" in res.stderr:
        _log(f"      [!] Push rejected (branch exists from prior run); force-pushing verified branch.")
        force_res = _do_push(["--force-with-lease"])
        if force_res.returncode == 0:
            return True
        _log(f"      [!] Force-push also failed:\n{force_res.stderr}")
        return False
    _log(f"      [!] Failed to push task branch {branch_name} to origin:\n{res.stderr}")
    return False


def _stage_clone(
    stage_label: str,
    full_task_id: str,
    branch_name: str,
    clone_remote: str,
    checkout_ref: str,
    docker_config: Optional[Any],
    dashboard: Any,
    _log: Callable,
    sccache_config: Optional[Any] = None,
    sccache_dist_config: Optional[Any] = None,
) -> "tuple[str, str, str, str]":
    """Create a fresh tmpdir + optional container, clone the repo, and checkout *checkout_ref*.

    For the impl stage *checkout_ref* is ``origin/{dev_branch}`` (creates the
    task branch).  For review / validate stages it is ``origin/{branch_name}``
    (checks out the existing task branch).

    :param sccache_config: Optional :class:`~workflow_lib.config.SCCacheConfig`.
        When provided and enabled, passes to _start_task_container for sccache setup.
    :param sccache_dist_config: Optional :class:`~workflow_lib.config.SCCacheDistConfig`.
        When provided and enabled, passes to _start_task_container for sccache-dist setup.
    :returns: ``(tmpdir, container_name, env_file, cwd)`` on success, or raises
        ``subprocess.CalledProcessError`` / ``RuntimeError`` on clone failure.
    :raises RuntimeError: When the clone or checkout fails.
    """
    import time as _time
    phase_id, task_id = full_task_id.split("/", 1)
    safe_task_id = task_id.replace("/", "_").replace(".md", "")
    
    _stage_start = _time.time()
    _log(f"      [{stage_label}] BEGIN stage clone for {full_task_id}")

    tmpdir = tempfile.mkdtemp(prefix=f"ai_{safe_task_id}_{stage_label}_")
    os.chmod(tmpdir, 0o755)

    _container_name = ""
    env_file = ""

    if docker_config:
        env_file = _write_container_env_file(tmpdir)
        import uuid as _uuid
        _container_name = f"ai_{stage_label}_{safe_task_id}_{_uuid.uuid4().hex[:8]}"
        # Get sccache_services config for configure_containers setting
        from workflow_lib.config import get_sccache_services_config
        services_cfg = get_sccache_services_config()
        configure_containers = services_cfg.configure_containers if services_cfg else True
        _log(f"      [{stage_label}] Container name will be: {_container_name}")
        _start_task_container(_container_name, docker_config, env_file, _log, sccache_config, sccache_dist_config, configure_containers)
        _log(f"      [{stage_label}] Cloning repository into container...")
        if dashboard:
            dashboard.set_agent(full_task_id, stage_label, "cloning", "")
        
        # Verify container exists before git clone (defensive check)
        container_check = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name=^{_container_name}$"],
            capture_output=True, text=True, check=False
        )
        if not container_check.stdout.strip():
            _log(f"      [!] Container {_container_name} not found before git clone")
            _stop_task_container(_container_name, _log)
            raise RuntimeError(f"Container {_container_name} disappeared before git clone")
        _log(f"      [{stage_label}] Container {_container_name} verified before git clone")
        
        try:
            _docker_exec(_container_name, ["git", "clone", clone_remote, "/workspace"],
                         env_file=env_file, check=True, log=_log)
            _docker_exec(_container_name, ["git", "-C", "/workspace", "submodule", "update",
                                            "--init", "--recursive"],
                         env_file=env_file, check=False, log=_log)
            _docker_exec(_container_name, ["git", "-C", "/workspace", "checkout", "-B",
                                            branch_name, checkout_ref],
                         env_file=env_file, check=True, log=_log)
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf-8") if isinstance(e.stderr, bytes) else str(e.stderr or "")
            _stop_task_container(_container_name, _log)
            raise RuntimeError(f"Clone/checkout failed: {err}") from e
        cwd = "/workspace"
        
        _log(f"      [{stage_label}] Stage clone completed in {_time.time() - _stage_start:.2f}s: container={_container_name}")
        
        # Start RAG MCP server in the container workspace (if enabled)
        if get_rag_enabled():
            start_rag_server("/workspace", verbose=True, container_name=_container_name)
    else:
        _log(f"      [{stage_label}] Cloning repository to {tmpdir} on {branch_name}...")
        if dashboard:
            dashboard.set_agent(full_task_id, stage_label, "cloning", "")
        try:
            subprocess.run(["git", "clone", clone_remote, tmpdir],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            subprocess.run(["git", "submodule", "update", "--init", "--recursive"],
                           cwd=tmpdir, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            subprocess.run(["git", "checkout", "-B", branch_name, checkout_ref],
                           cwd=tmpdir, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf-8") if isinstance(e.stderr, bytes) else str(e.stderr)
            raise RuntimeError(f"Clone/checkout failed: {err}") from e
        cwd = tmpdir
        
        # Start RAG MCP server in the cloned repository (if enabled)
        if get_rag_enabled():
            start_rag_server(tmpdir, verbose=True)

    return tmpdir, _container_name, env_file, cwd


def _stage_commit_and_push(
    branch_name: str,
    commit_msg: str,
    *,
    cwd: str,
    container_name: str,
    env_file: str,
    _log: Callable,
) -> bool:
    """``git add -A``, conditionally commit, then push *branch_name*.

    :returns: ``True`` on success.
    """
    if container_name:
        _docker_exec(container_name, ["git", "add", "-A"], env_file=env_file, log=_log)
        status_res = _docker_exec(container_name, ["git", "status", "--porcelain"],
                                   env_file=env_file)
        if status_res.stdout.strip():
            _docker_exec(container_name, ["git", "commit", "--no-verify", "-m", commit_msg],
                         env_file=env_file, check=True, log=_log)
        else:
            _log(f"      No changes to commit after {commit_msg!r}.")
    else:
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                                 capture_output=True, text=True)
        if status.stdout.strip():
            subprocess.run(["git", "commit", "--no-verify", "-m", commit_msg],
                           cwd=cwd, check=True, stdout=subprocess.DEVNULL)
        else:
            _log(f"      No changes to commit after {commit_msg!r}.")

    return _push_branch_to_origin(
        branch_name, cwd=cwd, container_name=container_name,
        env_file=env_file, _log=_log,
    )


def _stage_cleanup(
    tmpdir: str,
    container_name: str,
    success: bool,
    cleanup: bool,
    branch_name: str,
    _log: Callable,
) -> None:
    """Stop container, reclaim ownership, and optionally delete *tmpdir*."""
    import time as _time
    _cleanup_start = _time.time()
    _log(f"      [_stage_cleanup] BEGIN: container={container_name}, success={success}, cleanup={cleanup}")
    
    if container_name:
        _log(f"      [_stage_cleanup] Calling _stop_task_container for {container_name}")
        _stop_task_container(container_name, _log)
    if tmpdir and not container_name:
        _reclaim_dir_ownership(tmpdir, _log)
    if success or (cleanup and tmpdir):
        if tmpdir:
            _log(f"      [_stage_cleanup] Removing tmpdir: {tmpdir}")
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        _log(f"      [!] Stage failed. Leaving clone {tmpdir} and branch {branch_name} for investigation.")
    
    _log(f"      [_stage_cleanup] END: elapsed={_time.time() - _cleanup_start:.2f}s")


def run_impl_stage(
    root_dir: str,
    full_task_id: str,
    branch_name: str,
    backend: str = "gemini",
    serena: bool = False,
    dashboard: Any = None,
    model: Optional[str] = None,
    dev_branch: str = "dev",
    remote_url: Optional[str] = None,
    agent_pool: Optional[AgentPoolManager] = None,
    cleanup: bool = False,
    docker_config: Optional[Any] = None,
    _log: Callable = print,
) -> bool:
    """Clone from *dev_branch*, run the Implementation agent, and push *branch_name*.

    Each call creates a fresh tmpdir (and optionally a fresh Docker container).
    On success the task branch is pushed to origin for the next stage to clone.

    :returns: ``True`` on success, ``False`` otherwise.
    """
    phase_id, task_id = full_task_id.split("/", 1)

    # When a pool is active and docker is configured, pre-acquire the impl agent
    # before starting the container so we can use its docker override.
    _pre_acquired_impl_agent = None
    if agent_pool is not None and docker_config is not None:
        from .config import merge_docker_configs
        _pre_acquired_impl_agent = agent_pool.acquire(timeout=300.0, step="develop")
        if _pre_acquired_impl_agent is not None and _pre_acquired_impl_agent.docker_config is not None:
            docker_config = merge_docker_configs(docker_config, _pre_acquired_impl_agent.docker_config)

    tmpdir = ""
    _container_name = ""
    success = False
    try:
        clone_remote = remote_url or root_dir
        checkout_ref = f"origin/{dev_branch}"

        # Get sccache and sccache-dist config for container setup
        from .config import get_sccache_config, get_sccache_dist_config
        sccache_config = get_sccache_config()
        sccache_dist_config = get_sccache_dist_config()

        tmpdir, _container_name, env_file, cwd = _stage_clone(
            "Impl", full_task_id, branch_name, clone_remote, checkout_ref,
            docker_config, dashboard, _log, sccache_config, sccache_dist_config,
        )

        if serena:
            serena_cache_src = os.path.join(root_dir, ".serena", "cache")
            serena_cache_dst = os.path.join(tmpdir, ".serena", "cache")
            if os.path.isdir(serena_cache_src) and not os.path.isdir(serena_cache_dst):
                shutil.copytree(serena_cache_src, serena_cache_dst)
            mcp_src = os.path.join(root_dir, ".mcp.json")
            mcp_dst = os.path.join(tmpdir, ".mcp.json")
            if os.path.exists(mcp_src) and not os.path.exists(mcp_dst):
                shutil.copy2(mcp_src, mcp_dst)

        context = {
            "phase_filename": phase_id,
            "task_name": task_id,
            "target_dir": full_task_id,
            "task_details": get_task_details(full_task_id),
            "description_ctx": get_project_context(),
            "memory_ctx": get_memory_context(root_dir),
            "clone_dir": tmpdir,
            "spec_ctx": get_spec_context(),
            "shared_components_ctx": get_shared_components_context(),
        }

        _run_agent_kwargs = dict(
            backend=backend, dashboard=dashboard, task_id=full_task_id, model=model,
            agent_pool=agent_pool, container_name=_container_name, container_env_file=env_file,
        )

        if dashboard:
            dashboard.set_agent(full_task_id, "Impl", "running", "")
        if not run_agent("Implementation", "implement_task.md", context, cwd,
                         _pre_acquired_agent=_pre_acquired_impl_agent, **_run_agent_kwargs):
            if dashboard:
                dashboard.set_agent(full_task_id, "Impl", "failed", "Implementation agent failed")
            return False

        if not _container_name:
            _reclaim_dir_ownership(tmpdir, _log)

        commit_msg = f"{phase_id}:{task_id}: Implementation (WIP)"
        if not _stage_commit_and_push(
            branch_name, commit_msg,
            cwd=cwd, container_name=_container_name, env_file=env_file, _log=_log,
        ):
            return False

        success = True
        return True

    except RuntimeError as e:
        _log(f"      [!] [Impl] {e}")
        if dashboard:
            dashboard.set_agent(full_task_id, "Impl", "failed", str(e))
        return False
    finally:
        if _pre_acquired_impl_agent is not None and agent_pool is not None:
            agent_pool.release(_pre_acquired_impl_agent)
        _stage_cleanup(tmpdir, _container_name, success, cleanup, branch_name, _log)


def run_review_stage(
    root_dir: str,
    full_task_id: str,
    branch_name: str,
    backend: str = "gemini",
    serena: bool = False,
    dashboard: Any = None,
    model: Optional[str] = None,
    dev_branch: str = "dev",
    remote_url: Optional[str] = None,
    agent_pool: Optional[AgentPoolManager] = None,
    cleanup: bool = False,
    docker_config: Optional[Any] = None,
    _log: Callable = print,
) -> bool:
    """Clone from *branch_name* (impl already pushed), run the Review agent, and push.

    Each call creates a fresh tmpdir (and optionally a fresh Docker container).

    :returns: ``True`` on success, ``False`` otherwise.
    """
    phase_id, task_id = full_task_id.split("/", 1)

    tmpdir = ""
    _container_name = ""
    success = False
    try:
        clone_remote = remote_url or root_dir
        checkout_ref = f"origin/{branch_name}"

        # Get sccache and sccache-dist config for container setup
        from .config import get_sccache_config, get_sccache_dist_config
        sccache_config = get_sccache_config()
        sccache_dist_config = get_sccache_dist_config()

        tmpdir, _container_name, env_file, cwd = _stage_clone(
            "Review", full_task_id, branch_name, clone_remote, checkout_ref,
            docker_config, dashboard, _log, sccache_config, sccache_dist_config,
        )

        context = {
            "phase_filename": phase_id,
            "task_name": task_id,
            "target_dir": full_task_id,
            "task_details": get_task_details(full_task_id),
            "description_ctx": get_project_context(),
            "memory_ctx": get_memory_context(root_dir),
            "clone_dir": tmpdir,
            "spec_ctx": get_spec_context(),
            "shared_components_ctx": get_shared_components_context(),
        }

        _run_agent_kwargs = dict(
            backend=backend, dashboard=dashboard, task_id=full_task_id, model=model,
            agent_pool=agent_pool, container_name=_container_name, container_env_file=env_file,
        )

        if dashboard:
            dashboard.set_agent(full_task_id, "Review", "running", "")
        if not run_agent("Review", "review_task.md", context, cwd, **_run_agent_kwargs):
            if dashboard:
                dashboard.set_agent(full_task_id, "Review", "failed", "Review agent failed")
            return False

        if not _container_name:
            _reclaim_dir_ownership(tmpdir, _log)

        commit_msg = f"{phase_id}:{task_id}: Review"
        if not _stage_commit_and_push(
            branch_name, commit_msg,
            cwd=cwd, container_name=_container_name, env_file=env_file, _log=_log,
        ):
            return False

        success = True
        return True

    except RuntimeError as e:
        _log(f"      [!] [Review] {e}")
        if dashboard:
            dashboard.set_agent(full_task_id, "Review", "failed", str(e))
        return False
    finally:
        _stage_cleanup(tmpdir, _container_name, success, cleanup, branch_name, _log)


def run_validate_stage(
    root_dir: str,
    full_task_id: str,
    branch_name: str,
    presubmit_cmd: str,
    backend: str = "gemini",
    serena: bool = False,
    dashboard: Any = None,
    model: Optional[str] = None,
    dev_branch: str = "dev",
    remote_url: Optional[str] = None,
    agent_pool: Optional[AgentPoolManager] = None,
    cleanup: bool = False,
    docker_config: Optional[Any] = None,
    max_retries: int = 3,
    _log: Callable = print,
) -> bool:
    """Clone from *branch_name*, run the presubmit loop, and push on success.

    Mirrors the original verification loop: on failure the Review agent is
    re-invoked with presubmit failure context, then the presubmit is retried.

    :returns: ``True`` when presubmit passes and the branch is pushed.
    """
    phase_id, task_id = full_task_id.split("/", 1)

    tmpdir = ""
    _container_name = ""
    success = False
    try:
        clone_remote = remote_url or root_dir
        checkout_ref = f"origin/{branch_name}"

        # Get sccache and sccache-dist config for container setup
        from .config import get_sccache_config, get_sccache_dist_config
        sccache_config = get_sccache_config()
        sccache_dist_config = get_sccache_dist_config()

        tmpdir, _container_name, env_file, cwd = _stage_clone(
            "Verify", full_task_id, branch_name, clone_remote, checkout_ref,
            docker_config, dashboard, _log, sccache_config, sccache_dist_config,
        )

        task_details = get_task_details(full_task_id)
        context = {
            "phase_filename": phase_id,
            "task_name": task_id,
            "target_dir": full_task_id,
            "task_details": task_details,
            "description_ctx": get_project_context(),
            "memory_ctx": get_memory_context(root_dir),
            "clone_dir": tmpdir,
            "spec_ctx": get_spec_context(),
            "shared_components_ctx": get_shared_components_context(),
        }

        _run_agent_kwargs = dict(
            backend=backend, dashboard=dashboard, task_id=full_task_id, model=model,
            agent_pool=agent_pool, container_name=_container_name, container_env_file=env_file,
        )

        if not _container_name:
            _reclaim_dir_ownership(tmpdir, _log)

        cmd_list = presubmit_cmd.split()
        for attempt in range(1, max_retries + 1):
            _log(f"      [Verification] Running presubmit (Attempt {attempt}/{max_retries})...")
            if dashboard:
                # Verify doesn't spawn an agent, so pass agent_name=None to clear the column
                dashboard.set_agent(full_task_id, "Verify", "running", f"Attempt {attempt}/{max_retries}", agent_name=None)

            if _container_name:
                presubmit_res = _docker_exec(_container_name, cmd_list, env_file=env_file, log=_log)
            else:
                presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True,
                                                text=True, start_new_session=True)

            if presubmit_res.returncode == 0:
                _log(f"      [Verification] Presubmit passed!")

                commit_msg = f"{phase_id}:{task_id}: Standardized Implementation"
                m = re.search(r'^#\s*Task:\s*(.*?)(?:\s*\(Sub-Epic:.*?\))?$',
                               task_details, re.MULTILINE)
                if m and m.group(1).strip():
                    commit_msg = f"{phase_id}:{task_id}: {m.group(1).strip()}"

                if not _stage_commit_and_push(
                    branch_name, commit_msg,
                    cwd=cwd, container_name=_container_name, env_file=env_file, _log=_log,
                ):
                    return False

                if dashboard:
                    # Verify doesn't spawn an agent, so clear agent_name
                    dashboard.set_agent(full_task_id, "Verify", "done", "Presubmit passed", agent_name=None)
                success = True
                return True

            _log(f"      [Verification] Presubmit failed.")
            if attempt < max_retries:
                failure_ctx = dict(context)
                failure_ctx["task_details"] += (
                    f"\n\n### PRESUBMIT FAILURE (Attempt {attempt})\n"
                    f"The presubmit script failed with the following output. "
                    f"Please fix the code.\n\n```\n"
                    f"{presubmit_res.stdout}\n{presubmit_res.stderr}\n```\n"
                )
                if dashboard:
                    # Review retry - clear agent_name since Review agent will run
                    dashboard.set_agent(full_task_id, "Review", "running",
                                        "Retry after presubmit failure", agent_name=None)
                if not run_agent("Review (Retry)", "review_task.md", failure_ctx, cwd,
                                 **_run_agent_kwargs):
                    if dashboard:
                        dashboard.remove_agent(full_task_id)  # Review retry failed, remove agent
                    return False

        _log(f"   -> [!] Task {full_task_id} failed presubmit {max_retries} times. Aborting task.")
        if dashboard:
            # Verify doesn't spawn an agent, so clear agent_name
            dashboard.set_agent(full_task_id, "Verify", "failed",
                                 f"Failed after {max_retries} attempts", agent_name=None)
        return False

    except RuntimeError as e:
        _log(f"      [!] [Verify] {e}")
        if dashboard:
            # Verify doesn't spawn an agent, so clear agent_name
            dashboard.set_agent(full_task_id, "Verify", "failed", str(e), agent_name=None)
        return False
    finally:
        _stage_cleanup(tmpdir, _container_name, success, cleanup, branch_name, _log)


def process_task(
    root_dir: str,
    full_task_id: str,
    presubmit_cmd: str,
    backend: str = "gemini",
    max_retries: int = 3,
    serena: bool = False,
    dashboard: Any = None,
    model: Optional[str] = None,
    dev_branch: str = "dev",
    remote_url: Optional[str] = None,
    agent_pool: Optional[AgentPoolManager] = None,
    cleanup: bool = False,
    docker_config: Optional[Any] = None,
    starting_stage: str = STAGE_IMPL,
    on_stage_complete: Optional[Callable[[str, str], None]] = None,
) -> bool:
    """Orchestrate the full implementation lifecycle for one task.

    Runs three independently restartable stages in sequence:

    1. **impl** — clone from *dev_branch*, run the Implementation agent,
       commit and push to the task branch.
    2. **review** — clone from the task branch, run the Review agent,
       commit and push.
    3. **validate** — clone from the task branch, run the presubmit loop
       (with Review retry on failure), commit and push on success.

    Pass *starting_stage* to resume from a specific stage (e.g. after a
    crash partway through).  *on_stage_complete* is called with
    ``(full_task_id, stage_name)`` after each stage successfully completes,
    allowing the caller to persist progress between stages.

    Each stage creates its own fresh clone and Docker container (when
    *docker_config* is set).

    :param starting_stage: The first stage to run.  One of :data:`STAGE_IMPL`,
        :data:`STAGE_REVIEW`, :data:`STAGE_VALIDATE`, or :data:`STAGE_DONE`.
        Defaults to :data:`STAGE_IMPL`.
    :param on_stage_complete: Optional callback invoked as
        ``on_stage_complete(full_task_id, stage_name)`` after each stage
        succeeds.  Useful for persisting restart state.
    :returns: ``True`` if all stages (from *starting_stage* onward) completed
        successfully; ``False`` otherwise.
    """
    phase_id, task_id = full_task_id.split("/", 1)
    safe_task_id = task_id.replace("/", "_").replace(".md", "")
    branch_name = f"ai-phase-{safe_task_id}"

    def _log(msg: str) -> None:
        if dashboard:
            dashboard.log(msg)
        else:
            print(msg)

    # All stages already done — nothing to run.
    if starting_stage == STAGE_DONE:
        return True

    # For a fresh start (impl stage), check if the task branch already exists
    # in origin (e.g. from a prior run that succeeded but failed to record
    # state).  Skip straight to merge in that case.  Do NOT apply this early-out
    # when resuming at a later stage — the branch is expected to exist.
    if starting_stage == STAGE_IMPL:
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

    _log(f"\n   -> [Implementation] Starting {full_task_id} from stage {starting_stage!r}")
    if dashboard:
        dashboard.set_agent(full_task_id, "Impl", "queued", "")

    _cb: Callable[[str, str], None] = on_stage_complete or (lambda tid, s: None)

    _stage_fns: Dict[str, Callable[..., bool]] = {
        STAGE_IMPL:     run_impl_stage,
        STAGE_REVIEW:   run_review_stage,
        STAGE_VALIDATE: run_validate_stage,
    }
    _stage_extra_kwargs: Dict[str, Any] = {
        STAGE_VALIDATE: {"presubmit_cmd": presubmit_cmd, "max_retries": max_retries},
    }

    stage_kwargs = dict(
        root_dir=root_dir,
        full_task_id=full_task_id,
        branch_name=branch_name,
        backend=backend,
        serena=serena,
        dashboard=dashboard,
        model=model,
        dev_branch=dev_branch,
        remote_url=remote_url,
        agent_pool=agent_pool,
        cleanup=cleanup,
        docker_config=docker_config,
        _log=_log,
    )

    # Load stage-level retry count from config (reuses the same "retries" key).
    from .config import get_config_defaults as _get_cfg_pt
    stage_max_retries: int = _get_cfg_pt().get("retries", 0)

    overall_success = False
    last_completed_stage = None
    task_attempt = 0  # total retries consumed across all stages
    try:
        stages_to_run = _STAGE_ORDER[_STAGE_ORDER.index(starting_stage):]
        stage_idx = 0
        while stage_idx < len(stages_to_run):
            stage = stages_to_run[stage_idx]
            if shutdown_requested:
                # Persist progress from any previously completed stage before shutting down
                if last_completed_stage:
                    _cb(full_task_id, last_completed_stage)
                    _log(f"      [!] Shutdown requested — stage {last_completed_stage!r} completed and saved. Stopping before {stage!r} stage.")
                else:
                    _log(f"      [!] Shutdown requested — stopping before {stage!r} stage.")
                # Return True to indicate graceful shutdown (not failure) when we completed at least one stage
                return last_completed_stage is not None
            extra = _stage_extra_kwargs.get(stage, {})
            if _stage_fns[stage](**stage_kwargs, **extra):
                last_completed_stage = stage
                _cb(full_task_id, stage)
                stage_idx += 1
            else:
                # Stage failed — consume a retry
                task_attempt += 1
                _log(f"      [!] {full_task_id} stage {stage!r} failed (retry {task_attempt}/{stage_max_retries})")
                if task_attempt > stage_max_retries or shutdown_requested:
                    return False
                # Validate failure -> fall back to review (if review is in our run)
                if stage == STAGE_VALIDATE and STAGE_REVIEW in stages_to_run:
                    review_idx = stages_to_run.index(STAGE_REVIEW)
                    stage_idx = review_idx
                    _log(f"      [Stage Retry] {full_task_id} falling back to {STAGE_REVIEW!r} (retry {task_attempt}/{stage_max_retries})")
                    if dashboard:
                        dashboard.set_agent(full_task_id, "Review", "retrying",
                                            f"Retry {task_attempt}/{stage_max_retries}")
                else:
                    _log(f"      [Stage Retry] {full_task_id} retrying {stage!r} (retry {task_attempt}/{stage_max_retries})")
                    if dashboard:
                        dashboard.set_agent(full_task_id, stage.capitalize(), "retrying",
                                            f"Retry {task_attempt}/{stage_max_retries}")
        overall_success = True
        return True
    finally:
        if overall_success or (shutdown_requested and last_completed_stage is not None):
            # Remove agent from dashboard when task fully completes OR when
            # graceful shutdown occurs after completing at least one stage
            if dashboard:
                dashboard.remove_agent(full_task_id)
        elif not shutdown_requested:
            # Only mark as failed if it was a real failure, not graceful shutdown
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
        ["sudo", "-n", "chown", "-R", user, path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        _log(f"      [!] Warning: failed to chown {path} to {user!r}: {result.stderr.strip()}")


def _set_cargo_target_dir(cwd: str, target_dir: str, _log: Any) -> None:
    """Overwrite the ``target-dir`` in *cwd*/.cargo/config.toml with *target_dir*.

    Only modifies the file when it already contains a ``target-dir`` line so
    that projects without a cargo target-dir override are left untouched.
    """
    config_path = os.path.join(cwd, ".cargo", "config.toml")
    if not os.path.exists(config_path):
        return
    try:
        with open(config_path) as f:
            content = f.read()
        new_content = re.sub(
            r'(^\s*target-dir\s*=\s*)"[^"]*"',
            f'\\1"{target_dir}"',
            content,
            flags=re.MULTILINE,
        )
        if new_content != content:
            with open(config_path, "w") as f:
                f.write(new_content)
            _log(f"      [cargo] Set target-dir → {target_dir}")
    except OSError as e:
        _log(f"      [!] Warning: failed to update cargo target-dir in {config_path}: {e}")


def _get_cargo_target_dir(cwd: str) -> Optional[str]:
    """Return the absolute ``target-dir`` from *cwd*/.cargo/config.toml, or ``None``.

    Only returns a value when the path is absolute (relative target dirs stay
    inside the worktree and are covered by the normal worktree chown).
    """
    config_path = os.path.join(cwd, ".cargo", "config.toml")
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path) as f:
            content = f.read()
        m = re.search(r'^\s*target-dir\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if m:
            path = m.group(1)
            if os.path.isabs(path):
                return path
    except OSError:
        pass
    return None


def _reclaim_dir_ownership(tmpdir: str, _log: Any) -> None:
    """Reclaim ownership of *tmpdir* (and its cargo target-dir) for the current OS user.

    Chowns both the worktree clone and any absolute ``target-dir`` referenced
    in ``.cargo/config.toml`` so that git and presubmit commands running as
    the current user can write to build artifacts left by an alternate-user
    agent.
    """
    current_user = os.getenv("USER", "")
    _set_dir_owner(tmpdir, current_user, _log)
    cargo_target = _get_cargo_target_dir(tmpdir)
    if cargo_target and os.path.exists(cargo_target):
        _log(f"      [chown] Reclaiming cargo target-dir {cargo_target} for {current_user!r}")
        _set_dir_owner(cargo_target, current_user, _log)


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


def merge_task(root_dir: str, task_id: str, presubmit_cmd: str, backend: str = "gemini", max_retries: int = 3, cache_lock: Optional[threading.Lock] = None, serena: bool = False, dashboard: Any = None, model: Optional[str] = None, dev_branch: str = "dev", remote_url: Optional[str] = None, workflow_state: Optional[Dict] = None, agent_pool: Optional[AgentPoolManager] = None, cleanup: bool = False, docker_config: Optional[Any] = None) -> bool:
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
    :param cleanup: When ``True``, remove the temporary clone even on failure.
        Defaults to ``False``.
    :type cleanup: bool
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
    tmpdir = ""
    _container_name = ""
    push_succeeded = False
    try:
        tmpdir = tempfile.mkdtemp(prefix=f"merge_{safe_name_part}_")
        os.chmod(tmpdir, 0o755)  # Allow other OS users to traverse (needed for sudo -u <user>)

        _log(f"\n   => [Merge] Attempting to squash merge {task_id} into dev...")

        clone_src = remote_url or root_dir
        env_file = ""
        if docker_config:
            env_file = _write_container_env_file(tmpdir)
            import uuid as _uuid
            _container_name = f"merge_{safe_name_part}_{_uuid.uuid4().hex[:8]}"
            _start_task_container(_container_name, docker_config, env_file, _log)
            _log(f"      Cloning repository into container...")
            _docker_exec(_container_name, ["git", "clone", clone_src, "/workspace"], env_file=env_file, check=True, log=_log)
            _docker_exec(_container_name, ["git", "-C", "/workspace", "submodule", "update", "--init", "--recursive"], env_file=env_file, log=_log)
            _docker_exec(_container_name, ["git", "-C", "/workspace", "checkout", dev_branch], env_file=env_file, check=True, log=_log)
            cwd = "/workspace"
        else:
            _log(f"      Cloning repository to {tmpdir}...")
            subprocess.run(["git", "clone", clone_src, tmpdir], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "checkout", dev_branch], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cwd = tmpdir

        context = {
            "phase_filename": phase_part,
            "task_name": name_part,
            "branches_list": branch_name,
            "description_ctx": get_project_context()
        }

        # Helper: run a git command either on the host or inside the container.
        def _git(cmd: List[str], *, capture: bool = True, check: bool = False) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
            if _container_name:
                return _docker_exec(_container_name, cmd, env_file=env_file, capture=capture, check=check, log=_log)
            kw: Dict[str, Any] = {"capture_output": True, "text": True} if capture else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            return subprocess.run(cmd, cwd=tmpdir, check=check, **kw)

        # 1. Verification Loop for Merge
        for attempt in range(1, max_retries + 1):
            failure_output = ""
            if attempt == 1:
                # First attempt: Try a squash merge via git CLI
                _log(f"      [Merge] Attempting squash merge (Attempt 1/{max_retries})...")
                _git(["git", "fetch", "origin", branch_name], capture=False)

                merge_res = _git(["git", "merge", "--squash", f"origin/{branch_name}"])

                if merge_res.returncode == 0:
                    status = _git(["git", "status", "--porcelain"])
                    if status.stdout.strip():
                        _git(["git", "commit", "--no-verify", "-m", commit_msg], check=True)
                    else:
                        _log(f"      [Merge] No changes to squash merge for {task_id}.")

                    _log(f"      [Merge] Squash successful. Verifying with presubmit...")
                    cmd_list = presubmit_cmd.split()
                    presubmit_res = _git(cmd_list)

                    if presubmit_res.returncode == 0:
                        _log(f"      [Merge] Presubmit passed! Pushing to origin.")
                        if not _container_name:
                            _commit_state_in_clone(tmpdir, workflow_state, _log)
                        res = _git(["git", "push", "--force-with-lease", "origin", dev_branch])
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
                    _git(["git", "reset", "--hard", "HEAD"], capture=False)

                    rebase_res = _git(["git", "rebase", dev_branch, f"origin/{branch_name}"])
                    if rebase_res.returncode == 0:
                        _log(f"      [Merge] Rebase successful. Retrying squash merge...")
                        new_task_head = _git(["git", "rev-parse", "HEAD"]).stdout.strip()
                        _git(["git", "checkout", dev_branch], capture=False)

                        merge_res = _git(["git", "merge", "--squash", new_task_head])
                        if merge_res.returncode == 0:
                            status = _git(["git", "status", "--porcelain"])
                            if status.stdout.strip():
                                _git(["git", "commit", "--no-verify", "-m", commit_msg], check=True)
                            else:
                                _log(f"      [Merge] No changes to squash merge after rebase for {task_id}.")

                            cmd_list = presubmit_cmd.split()
                            presubmit_res = _git(cmd_list)
                            if presubmit_res.returncode == 0:
                                _log(f"      [Merge] Presubmit passed after rebase + squash! Pushing to origin.")
                                if not _container_name:
                                    _commit_state_in_clone(tmpdir, workflow_state, _log)
                                res = _git(["git", "push", "--force-with-lease", "origin", dev_branch])
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
                        _git(["git", "rebase", "--abort"], capture=False)
                        _git(["git", "checkout", dev_branch], capture=False)
                        failure_output = f"{rebase_res.stdout}\n{rebase_res.stderr}"
            else:
                # Merge Agent Attempt
                _log(f"      [Merge] Spawning Merge Agent to resolve conflicts (Attempt {attempt}/{max_retries})...")

                _git(["git", "reset", "--hard", f"origin/{dev_branch}"], capture=False)
                _git(["git", "clean", "-fd"], capture=False)

                failure_ctx = dict(context)
                failure_ctx["description_ctx"] += f"\n\n### PREVIOUS ATTEMPT FAILURE\nThe previous squash merge or presubmit failed with:\n```\n{failure_output}\n```\n"
                failure_ctx["description_ctx"] += f"\nPlease resolve the conflicts and ensure the final state is a single commit on the {dev_branch} branch with the message: {commit_msg}"

                if not run_agent("Merge", "merge_task.md", failure_ctx, cwd, backend, dashboard=dashboard, task_id=task_id, model=model, agent_pool=agent_pool, container_name=_container_name, container_env_file=env_file):
                    _log(f"      [!] Merge agent failed to cleanly exit.")
                    continue

                if not _container_name:
                    _reclaim_dir_ownership(tmpdir, _log)

                _log(f"      [Merge] Verifying agent's merge...")
                cmd_list = presubmit_cmd.split()
                presubmit_res = _git(cmd_list)

                if presubmit_res.returncode == 0:
                    _log(f"      [Merge] Presubmit passed! Pushing to origin.")
                    if not _container_name:
                        _commit_state_in_clone(tmpdir, workflow_state, _log)
                    res = _git(["git", "push", "--force-with-lease", "origin", dev_branch])
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
        if _container_name:
            _stop_task_container(_container_name, _log)

        if push_succeeded and serena and cache_lock is not None:
            rebuild_serena_cache(tmpdir, root_dir, cache_lock, dashboard=dashboard)

        # Reclaim ownership before cleanup (host path only)
        if tmpdir and not _container_name:
            _reclaim_dir_ownership(tmpdir, _log)

        if push_succeeded or (cleanup and tmpdir):
            # Cleanup clone
            if tmpdir:
                _log(f"      Cleaning up merge clone {tmpdir}...")
                shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            _log(f"      [!] Merge failed. Leaving clone {tmpdir} for investigation.")


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


def _get_resumable_tasks(master_dag: Dict[str, List[str]], remote_url: Optional[str], root_dir: str) -> set:
    """Return the set of task IDs that have an existing implementation branch in origin.

    These tasks were partially processed in a prior run.  ``process_task``
    will skip re-implementation for them, so they complete much faster and
    should be scheduled ahead of fresh tasks.

    Makes a single ``git ls-remote --heads`` call and maps each
    ``ai-phase-<safe_task_id>`` branch back to its task ID.  Returns an empty
    set on any git failure so the caller degrades gracefully.

    :param master_dag: Full task-ID → prereqs mapping.
    :param remote_url: Remote URL (or local path) passed to ``git ls-remote``.
    :param root_dir: Project root used when *remote_url* is ``None``.
    :returns: Set of task IDs whose branches already exist on the remote.
    """
    target = remote_url or root_dir
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", target, "refs/heads/ai-phase-*"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return set()

    existing_branches: set = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            existing_branches.add(parts[1].replace("refs/heads/", ""))

    resumable: set = set()
    for task_id in master_dag:
        _, task_part = task_id.split("/", 1)
        safe_task_id = task_part.replace("/", "_").replace(".md", "")
        if f"ai-phase-{safe_task_id}" in existing_branches:
            resumable.add(task_id)
    return resumable


def get_ready_tasks(
    master_dag: Dict[str, List[str]],
    completed_tasks: List[str],
    active_tasks: List[str],
    resumable_tasks: Optional[set] = None,
) -> List[str]:
    """Return tasks whose prerequisites are met and that are not already active or done.

    Implements a *phase barrier*: only tasks belonging to the lowest-numbered
    incomplete phase are eligible to run.  This ensures phase N is fully merged
    before phase N+1 begins.

    Blocked tasks (from :func:`load_blocked_tasks`) are excluded from both
    eligibility and prerequisite satisfaction checks — a dependency on a
    blocked task is never considered met.

    Tasks in *resumable_tasks* (branch already exists in origin) are sorted
    before fresh tasks so in-flight work is completed before new work begins.

    :param master_dag: Mapping of ``task_id -> [prerequisite_task_ids]`` for
        all tasks across all phases.
    :type master_dag: Dict[str, List[str]]
    :param completed_tasks: List of task IDs that have been successfully
        merged into ``dev``.
    :type completed_tasks: List[str]
    :param active_tasks: List of task IDs currently being processed by worker
        threads.
    :type active_tasks: List[str]
    :param resumable_tasks: Optional set of task IDs whose implementation
        branch already exists in origin.  These are sorted first within each
        phase so in-progress work is finished before fresh tasks are started.
    :type resumable_tasks: Optional[set]
    :returns: Sorted list of task IDs ready to be submitted for execution.
    :rtype: List[str]
    """
    resumable = resumable_tasks or set()
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

    # 4. Sort: resumable tasks (branch already exists) first, then by phase/task number.
    #    This finishes in-progress work before starting fresh tasks, unblocking
    #    downstream dependencies sooner.
    def _sort_key(task_id: str) -> tuple:
        is_fresh = 0 if task_id in resumable else 1
        return (is_fresh,) + phase_sort_key(task_id)

    ready.sort(key=_sort_key)
    return ready


def execute_dag(root_dir: str, master_dag: Dict[str, List[str]], state: Dict[str, Any], jobs: int, presubmit_cmd: str, backend: str = "gemini", log_file: Any = None, model: Optional[str] = None, agent_pool: Optional[AgentPoolManager] = None, cleanup: bool = False) -> None:
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
    :param cleanup: When ``True``, remove temporary clones on failure.
    :type cleanup: bool
    :raises SystemExit: When one or more tasks fail.
    """
    # Auto-start sccache services if configured
    from .config import ensure_sccache_services
    sccache_ok, sccache_dist_ok = ensure_sccache_services()
    if not sccache_ok:
        print("[!] Warning: sccache server failed to auto-start")
    if not sccache_dist_ok:
        print("[!] Warning: sccache-dist scheduler failed to auto-start")

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
            _execute_dag_inner(root_dir, master_dag, state, jobs, presubmit_cmd, backend, serena_enabled, cache_lock, dashboard, model=model, dev_branch=dev_branch, agent_pool=agent_pool, cleanup=cleanup)
        finally:
            _active_dashboard = None


def _execute_dag_inner(root_dir: str, master_dag: Dict[str, List[str]], state: Dict[str, Any], jobs: int, presubmit_cmd: str, backend: str, serena_enabled: bool, cache_lock: threading.Lock, dashboard: Any, model: Optional[str] = None, dev_branch: str = "dev", agent_pool: Optional[AgentPoolManager] = None, cleanup: bool = False) -> None:
    """Inner DAG execution loop run inside the dashboard context manager."""
    pivot_remote = get_pivot_remote()
    try:
        remote_url: Optional[str] = get_gitlab_remote_url(root_dir, remote_name=pivot_remote)
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        remote_url = None

    docker_config = get_docker_config()

    # Validate ALL copy_files sources exist on the host BEFORE starting execution
    # This includes global docker config AND per-agent docker config overrides
    # to fail fast if any required files are missing, preventing mid-execution failures
    from .config import get_agent_pool_configs
    all_docker_configs = []
    if docker_config:
        all_docker_configs.append(("global", docker_config))
    
    # Add per-agent docker configs (may override or extend global config)
    try:
        agent_configs = get_agent_pool_configs()
        for agent in agent_configs:
            if agent.docker_config:
                all_docker_configs.append((agent.name, agent.docker_config))
    except Exception:
        pass  # Agents may not be configured
    
    # Only validate if there are copy_files to check
    has_copy_files = any(cfg.copy_files for _, cfg in all_docker_configs)
    
    if has_copy_files:
        dashboard.log("=> Validating docker copy_files sources...")
        missing_files: Dict[str, List[str]] = {}
        checked_files = set()
        
        for cfg_label, cfg in all_docker_configs:
            for cf in cfg.copy_files:
                if cf.src in checked_files:
                    continue  # Already checked this file
                checked_files.add(cf.src)
                
                # Use sudo to check existence (file may exist but be owned by another user)
                result = subprocess.run(["sudo", "test", "-e", cf.src], check=False)
                if result.returncode != 0:
                    if cf.src not in missing_files:
                        missing_files[cf.src] = []
                    missing_files[cf.src].append(cfg_label)
        
        if missing_files:
            dashboard.log(f"[!] FATAL: {len(missing_files)} docker copy_files source(s) do not exist:")
            for path, configs in missing_files.items():
                dashboard.log(f"    - {path} (required by: {', '.join(configs)})")
            dashboard.log("[!] Fix the missing file(s) or update docker configuration, then re-run.")
            notify_failure("DAG execution aborted: docker copy_files sources missing",
                          context="\n".join(f"Missing: {p} (required by: {', '.join(configs)})" 
                                          for p, configs in missing_files.items()))
            sys.exit(1)
        
        total_files = len(checked_files)
        dashboard.log(f"   All {total_files} copy_files sources validated across {len(all_docker_configs)} config(s).")

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
    task_attempts: Dict[str, int] = {}  # task_id -> number of attempts so far
    from .config import get_config_defaults as _get_cfg
    max_task_retries: int = _get_cfg().get("retries", 0)  # 0 = no auto-retry (default)
    state_lock = threading.Lock()

    def _make_stage_callback(task_id: str) -> Callable[[str, str], None]:
        """Return a callback that persists stage completion to state['task_stages']."""
        def on_stage_complete(tid: str, stage: str) -> None:
            with state_lock:
                state.setdefault("task_stages", {})[tid] = stage
                save_workflow_state(state)
        return on_stage_complete

    # Discover tasks that already have an implementation branch in origin so
    # they can be prioritised ahead of fresh tasks in the scheduling loop.
    resumable_tasks = _get_resumable_tasks(master_dag, remote_url, root_dir)
    if resumable_tasks:
        dashboard.log(f"=> Resumable tasks found ({len(resumable_tasks)}): will be scheduled before fresh tasks.")

    dashboard.log("=> Starting Parallel DAG Execution Loop...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor, \
         concurrent.futures.ThreadPoolExecutor(max_workers=1) as merge_executor:
        # Dictionary to keep track of futures mapping to task_id
        future_to_task = {}
        # Merge futures run on a dedicated single-thread executor so they are
        # serialised (no concurrent pushes) but do NOT block the scheduling of
        # new implementation tasks.
        merge_future_to_task = {}   # Future -> (task_id, attempt_num)
        pending_merge = set()       # task_ids awaiting merge (not yet in completed_tasks)

        def _submit_merge(task_id: str, attempt: int = 1) -> None:
            """Submit a merge job to the single-thread merge executor."""
            with state_lock:
                pending_state = {
                    "completed_tasks": list(state.get("completed_tasks", [])) + [task_id],
                    "merged_tasks":    list(state.get("merged_tasks", []))    + [task_id],
                }
            if attempt > 1:
                dashboard.log(f"   -> [Merge Retry] Task {task_id} merge attempt {attempt}/{max_task_retries + 1}")
                dashboard.set_agent(task_id, "Merge", "retrying", f"Attempt {attempt}/{max_task_retries + 1}")
            mf = merge_executor.submit(
                merge_task, root_dir, task_id, presubmit_cmd, backend,
                cache_lock=cache_lock, serena=serena_enabled, dashboard=dashboard,
                model=model, dev_branch=dev_branch, remote_url=remote_url,
                workflow_state=pending_state, agent_pool=agent_pool, cleanup=cleanup,
                docker_config=docker_config,
            )
            merge_future_to_task[mf] = (task_id, attempt)
            pending_merge.add(task_id)

        while True:
            # Check for newly ready tasks
            ready_tasks = []
            if not shutdown_requested and not failed_tasks:
                with state_lock:
                    ready_tasks = get_ready_tasks(master_dag, state["completed_tasks"], list(active_tasks | pending_merge), resumable_tasks=resumable_tasks)

            # Submit ready tasks if we have capacity
            for task_id in ready_tasks:
                if len(active_tasks) + len(pending_merge) >= jobs:
                    break

                with state_lock:
                    active_tasks.add(task_id)
                    task_attempts.setdefault(task_id, 0)
                    task_attempts[task_id] += 1

                with state_lock:
                    _starting = _starting_stage_for(task_id, state)
                attempt_num = task_attempts[task_id]
                if attempt_num > 1:
                    dashboard.log(f"   -> [Retry] Task {task_id} attempt {attempt_num}/{max_task_retries + 1} from stage {_starting!r}")
                future = executor.submit(
                    process_task, root_dir, task_id, presubmit_cmd, backend,
                    serena=serena_enabled, dashboard=dashboard, model=model,
                    dev_branch=dev_branch, remote_url=remote_url,
                    agent_pool=agent_pool, cleanup=cleanup, docker_config=docker_config,
                    starting_stage=_starting,
                    on_stage_complete=_make_stage_callback(task_id),
                )
                future_to_task[future] = task_id

            # If no tasks are running (impl or merge) and none are ready, we are done/deadlocked
            if not future_to_task and not merge_future_to_task:
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

            # Wait for at least one future (implementation or merge) to complete
            all_futures = set(future_to_task.keys()) | set(merge_future_to_task.keys())
            done, not_done = concurrent.futures.wait(
                all_futures,
                return_when=concurrent.futures.FIRST_COMPLETED
            )

            for future in done:
                # --- Handle merge completion ---
                if future in merge_future_to_task:
                    task_id, attempt = merge_future_to_task.pop(future)
                    try:
                        merge_succeeded = future.result()
                    except Exception as exc:
                        traceback.print_exc()
                        merge_succeeded = False

                    if merge_succeeded:
                        pending_merge.discard(task_id)
                        dashboard.set_agent(task_id, "Merge", "done", "Merged successfully")
                        with state_lock:
                            state["completed_tasks"].append(task_id)
                            state["merged_tasks"].append(task_id)
                            state.get("task_stages", {}).pop(task_id, None)
                            save_workflow_state(state)
                        dashboard.log(f"   -> [Success] Task {task_id} fully integrated into {dev_branch}.")
                        fetch_res = subprocess.run(
                            ["git", "fetch", pivot_remote, f"+{dev_branch}:{dev_branch}"],
                            cwd=root_dir, capture_output=True, text=True,
                        )
                        if fetch_res.returncode != 0:
                            dashboard.log(f"      [!] Warning: Failed to sync local {dev_branch}: {fetch_res.stderr.strip()}")
                        else:
                            dashboard.log(f"      [Push] Success.")
                    else:
                        dashboard.log(f"      [!] Task {task_id} merge failed (attempt {attempt}/{max_task_retries + 1})")
                        if attempt <= max_task_retries and not shutdown_requested:
                            _submit_merge(task_id, attempt + 1)
                        else:
                            pending_merge.discard(task_id)
                            dashboard.set_agent(task_id, "Merge", "failed", f"Failed to merge into {dev_branch}")
                            with state_lock:
                                failed_tasks.add(f"Task {task_id} failed merging into {dev_branch} after {max_task_retries + 1} attempt(s).")
                    continue

                # --- Handle implementation completion ---
                task_id = future_to_task.pop(future)
                with state_lock:
                    active_tasks.remove(task_id)

                try:
                    success = future.result()
                    if success:
                        # Check if task fully completed or just partially completed due to shutdown
                        with state_lock:
                            task_stage = state.get("task_stages", {}).get(task_id)

                        if shutdown_requested:
                            if task_stage:
                                # Calculate the next stage to resume from
                                try:
                                    stage_idx = _STAGE_ORDER.index(task_stage)
                                    next_stage = _STAGE_ORDER[stage_idx + 1] if stage_idx + 1 < len(_STAGE_ORDER) else STAGE_DONE
                                except ValueError:
                                    next_stage = STAGE_IMPL
                                dashboard.log(f"   -> [Shutdown] Task {task_id} completed up to {task_stage!r} stage. Will resume from {next_stage!r} on next run.")
                            else:
                                dashboard.log(f"   -> [Shutdown] Task {task_id} completed. Will resume on next run.")
                            # Skip merge during shutdown - task will continue on next run
                            continue

                        dashboard.log(f"   -> [Implementation] Task {task_id} completed successfully.")

                        # Submit merge asynchronously on the dedicated merge executor
                        _submit_merge(task_id)
                    else:
                        # Check if we can retry
                        with state_lock:
                            attempts = task_attempts.get(task_id, 1)
                        if attempts <= max_task_retries:
                            dashboard.log(f"   -> [Retry] Task {task_id} failed (attempt {attempts}/{max_task_retries + 1}). Re-queuing...")
                            # Re-add to ready pool — the next scheduling iteration
                            # will pick it up via get_ready_tasks since it's neither
                            # in active_tasks nor completed_tasks.
                        else:
                            with state_lock:
                                failed_tasks.add(f"Task {task_id} failed implementation after {attempts} attempt(s).")
                except Exception as exc:
                    traceback.print_exc()
                    with state_lock:
                        attempts = task_attempts.get(task_id, 1)
                    if attempts <= max_task_retries:
                        dashboard.log(f"   -> [Retry] Task {task_id} raised exception (attempt {attempts}/{max_task_retries + 1}). Re-queuing...")
                    else:
                        with state_lock:
                            failed_tasks.add(f"Task {task_id} generated an exception after {attempts} attempt(s).")

    if failed_tasks:
        dashboard.log("\n" + "="*80)
        for err in failed_tasks:
            dashboard.log(f"[!] FATAL: {err} Halting workflow.")
        dashboard.log("="*80 + "\n")
        notify_failure("Run workflow halted due to task failures.",
                       context="\n".join(failed_tasks))
        sys.exit(1)

