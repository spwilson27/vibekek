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
    #"quota",
    #"rate limit",
    #"rate_limit",
    #"ratelimit",
    "RESOURCE_EXHAUSTED",
    "MODEL_CAPACITY_EXHAUSTED",
    #"rateLimitExceeded",
    "usage limit reached",
    #"too many requests",
    "No capacity available for model"
    "exhausted your capacity",
    #"429",
]


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
    """

    name: str
    backend: str
    user: str
    parallel: int
    priority: int
    quota_time: int
    model: Optional[str] = None
    steps: List[str] = field(default_factory=lambda: ["all"])


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
                    self._active[agent.name] += 1
                    return agent
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # Wake up every few seconds to re-check quota expiries.
                self._lock.wait(timeout=min(remaining, 5.0))

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
