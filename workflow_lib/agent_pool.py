"""Multi-agent pool manager for parallel DAG execution.

Provides :class:`AgentPoolManager`, a thread-safe scheduler that distributes
tasks across a set of named agent configurations read from ``.workflow.jsonc``.
Each configuration specifies the backend CLI, the OS user to run it as, its
concurrency limit, scheduling priority, and how long to suppress it after a
quota-exceeded event.

The special return code :data:`QUOTA_RETURN_CODE` is returned by
:func:`~workflow_lib.executor.run_ai_command` when a quota-exceeded pattern is
detected in the agent's output stream, signalling the pool to rotate to the
next available agent.
"""

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# Valid step names accepted in agent config and acquire() calls.
VALID_STEPS: Set[str] = {"develop", "review", "merge", "all"}


# Sentinel return code used by run_ai_command when quota is detected in output.
QUOTA_RETURN_CODE: int = -2

# Substrings (case-insensitive) that indicate a quota / rate-limit error in
# an agent's stdout or stderr stream.
QUOTA_PATTERNS: List[str] = [
    "RESOURCE_EXHAUSTED",
    "MODEL_CAPACITY_EXHAUSTED",
    "usage limit reached",
    "No capacity available for model",
    "exhausted your capacity",
    "ModelNotFoundError: Requested entity was not found",
    "You've hit your limit · resets", # Claude
]

# Substrings (case-insensitive) that indicate the CLI is already handling
# the quota error by retrying internally.  When a QUOTA_PATTERNS match also
# contains one of these, the abort is suppressed — we let the CLI recover.
QUOTA_TRANSIENT_PATTERNS: List[str] = [
    "retrying after",
    "retry after",
    "will retry",
    "retrying with backoff",
    "with backoff",
]

# Regex that matches "reset after <duration>" in a quota message.
# Duration tokens: 12h, 30m, 45s, or combinations like 1h30m.
_RESET_AFTER_RE = re.compile(
    r"reset\s+after\s+((?:\d+\s*h\s*)?(?:\d+\s*m\s*)?(?:\d+\s*s)?)",
    re.IGNORECASE,
)
_DURATION_TOKEN_RE = re.compile(r"(\d+)\s*([hms])", re.IGNORECASE)


def parse_quota_reset_seconds(line: str) -> Optional[float]:
    """Return the quota reset duration in seconds parsed from *line*, or ``None``.

    Recognises patterns like::

        Your quota will reset after 12h.
        Your quota will reset after 30m.
        Your quota will reset after 0s.
        Your quota will reset after 1h30m.

    :param line: A single output line from an AI CLI.
    :returns: Total seconds as a float, or ``None`` if no reset time is found.
    """
    m = _RESET_AFTER_RE.search(line)
    if not m:
        return None
    duration_str = m.group(1).strip()
    tokens = _DURATION_TOKEN_RE.findall(duration_str)
    if not tokens:
        return None
    total = 0.0
    for num, unit in tokens:
        n = int(num)
        if unit.lower() == "h":
            total += n * 3600
        elif unit.lower() == "m":
            total += n * 60
        else:
            total += n
    return total


@dataclass
class DockerCopyFile:
    """A single file to make available inside a Docker container.

    :param src: Absolute path on the host filesystem.
    :param dest: Absolute path inside the container where the file will appear
        (mounted read-only).
    """

    src: str
    dest: str


@dataclass
class DockerConfig:
    """Global Docker container configuration for the workflow.

    When set at the top level of ``.workflow.jsonc``, all workflow steps
    (implementation, review, presubmit, commit, merge) run inside a Docker
    container.  The container is started once per task, git-clones the repo
    from the pivot remote, and pushes changes back after validation.

    :param image: Docker image name (e.g. ``"ubuntu:24.04"``).
    :param pivot_remote: Git remote name to clone from and push to inside the
        container.  Defaults to ``"origin"``.
    :param volumes: List of bind-mount strings in standard Docker format
        (``"src:dest"`` or ``"src:dest:options"``).
    :param copy_files: Individual files to make available inside the container.
        Each entry specifies a host ``src`` path and a container ``dest`` path.
        Files are always mounted read-only.
    """

    image: str
    pivot_remote: str = "origin"
    volumes: List[str] = field(default_factory=list)
    copy_files: List[DockerCopyFile] = field(default_factory=list)


@dataclass
class AgentConfig:
    """Configuration for a single named agent pool entry.

    :param name: Human-readable label used in logs and the dashboard.
    :param backend: AI CLI backend (``"gemini"``, ``"claude"``, etc.).
    :param user: OS username to run the CLI as via ``sudo -u <user> --set-home --``.
    :param parallel: Maximum number of concurrent jobs from this agent.
    :param priority: Scheduling priority — lower values are preferred first.
    :param quota_time: Seconds to suppress this agent after a quota error.
    :param model: Optional model name passed to the CLI (e.g. ``"gemini-flash"``).
    :param steps: List of workflow steps this agent may perform.  Allowed values
        are ``"develop"``, ``"review"``, ``"merge"``, and ``"all"`` (the default,
        which matches any step).
    :param context_limit: Optional per-agent context limit in words.  When set,
        overrides the global ``"context_limit"`` from ``.workflow.jsonc`` but is
        itself overridden by the CLI ``--context-limit`` flag.
    :param env: Optional dict of environment variables to set when spawning this
        agent. These are merged into the current process environment, allowing
        per-agent configuration such as API keys or feature flags.
    """

    name: str
    backend: str
    user: str
    parallel: int
    priority: int
    quota_time: int
    spawn_rate: float = 0.0
    model: Optional[str] = None
    steps: List[str] = field(default_factory=lambda: ["all"])
    cargo_target_dir: Optional[str] = None
    docker_config: Optional[DockerConfig] = None
    context_limit: Optional[int] = None
    env: Dict[str, str] = field(default_factory=dict)


class AgentPoolManager:
    """Thread-safe pool of :class:`AgentConfig` entries with quota tracking.

    Tasks call :meth:`acquire` to obtain an agent configuration before
    spawning a subprocess, then :meth:`release` when the subprocess exits.
    Passing ``quota_exhausted=True`` to :meth:`release` suppresses that
    agent for ``agent.quota_time`` seconds.

    :param configs: List of agent configurations (order does not matter;
        they are sorted internally by priority).
    """

    def __init__(self, configs: List[AgentConfig]) -> None:
        self._lock: threading.Condition = threading.Condition(threading.Lock())
        # Sort by priority ascending (lower priority value = preferred).
        self._configs: List[AgentConfig] = sorted(configs, key=lambda a: a.priority)
        self._active: Dict[str, int] = {c.name: 0 for c in configs}
        # Maps agent name -> time.time() value after which quota has lifted.
        self._quota_expiry: Dict[str, float] = {}
        # Maps agent name -> time.time() value of the last spawn.
        self._last_spawn: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, timeout: float = 300.0, step: str = "all") -> Optional[AgentConfig]:
        """Block until an agent slot is available and return it.

        Walks agents in priority order, skipping any that are at their
        concurrency limit, within their quota-suppression window, or not
        permitted to perform *step*.  Waits up to *timeout* seconds before
        returning ``None``.

        :param timeout: Maximum seconds to wait.  Defaults to 300 (5 min).
        :param step: Workflow step being requested — one of ``"develop"``,
            ``"review"``, ``"merge"``, or ``"all"``.  Only agents whose
            :attr:`~AgentConfig.steps` list contains *step* or ``"all"``
            are eligible.
        :returns: An :class:`AgentConfig` whose slot has been reserved, or
            ``None`` if no agent became available within *timeout*.
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                agent = self._pick(step)
                if agent is not None:
                    now = time.time()
                    last = self._last_spawn.get(agent.name, 0)
                    
                    if agent.spawn_rate > 0.0 and last > 0:
                        target = last + agent.spawn_rate
                        if now < target:
                            # We want this agent, but need to wait for its spawn_rate cooldown.
                            # We wait inside the lock (which unlocks while sleeping) so that 
                            # if quota is exhausted during our wait, we are woken up via notify_all()
                            # and will loop again to potentially pick a different agent.
                            wait_for_spawn = target - now
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                return None
                            self._lock.wait(timeout=min(remaining, wait_for_spawn))
                            continue # Re-evaluate everything (including quota) after waking up
                    
                    # Ready to spawn
                    self._active[agent.name] += 1
                    self._last_spawn[agent.name] = time.time()
                    return agent
                    
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                
                # Calculate wait time for the next available agent slot
                now = time.time()
                next_wakeup = now + 5.0
                for cfg in self._configs:
                    if "all" not in cfg.steps and step not in cfg.steps:
                        continue
                    if self._active[cfg.name] < cfg.parallel:
                        qe = self._quota_expiry.get(cfg.name, 0)
                        if qe > now:
                            next_wakeup = min(next_wakeup, qe)
                
                wait_time = max(0.1, next_wakeup - now)
                self._lock.wait(timeout=min(remaining, wait_time))

    def release(self, agent: AgentConfig, quota_exhausted: bool = False) -> None:
        """Release a previously acquired agent slot.

        :param agent: The :class:`AgentConfig` returned by :meth:`acquire`.
        :param quota_exhausted: When ``True``, suppress this agent for
            ``agent.quota_time`` seconds.
        """
        with self._lock:
            self._active[agent.name] = max(0, self._active[agent.name] - 1)
            if quota_exhausted:
                self._quota_expiry[agent.name] = time.time() + agent.quota_time
            self._lock.notify_all()

    def status_lines(self) -> List[str]:
        """Return human-readable status lines for dashboard logging.

        :returns: One line per agent showing its active count and quota state.
        """
        lines = []
        now = time.time()
        with self._lock:
            for cfg in self._configs:
                expiry = self._quota_expiry.get(cfg.name, 0)
                if now < expiry:
                    remaining = int(expiry - now)
                    lines.append(f"  {cfg.name}: quota suppressed for {remaining}s")
                else:
                    active = self._active[cfg.name]
                    lines.append(f"  {cfg.name}: {active}/{cfg.parallel} slots active")
        return lines

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pick(self, step: str = "all") -> Optional[AgentConfig]:
        """Return the best available agent for *step* or ``None`` (must hold *_lock*).

        An agent is eligible when:

        * Its :attr:`~AgentConfig.steps` list contains *step* or ``"all"``.
        * Its quota-suppression window has not elapsed.
        * Its active job count is below :attr:`~AgentConfig.parallel`.
        """
        now = time.time()
        for cfg in self._configs:  # already sorted by priority
            # Step filter: agent must allow this step or be configured for "all".
            if "all" not in cfg.steps and step not in cfg.steps:
                continue
            if now < self._quota_expiry.get(cfg.name, 0):
                continue  # quota suppressed
            if self._active[cfg.name] < cfg.parallel:
                return cfg
        return None
