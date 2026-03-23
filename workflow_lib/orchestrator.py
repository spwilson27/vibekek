"""High-level orchestrator that drives the multi-phase planning workflow.

The :class:`Orchestrator` sequences all planning phases — document generation,
requirement extraction, epic breakdown, task breakdown, cross-phase review, and
DAG generation — in the correct order.  Each phase is executed with automatic
retry logic so that transient AI failures do not abort the entire run.

Typical usage::

    ctx = ProjectContext(root_dir, runner=GeminiRunner())
    Orchestrator(ctx).run()
"""

import concurrent.futures
import os
import signal
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Union

from .constants import DOCS
from .context import ProjectContext
from .config import set_agent_context_limit
from .discord import notify_failure
from .prompt_registry import validate_all_prompts_exist
from .runners import make_runner
from .phases import *


class Orchestrator:
    """Sequences and retries all planning phases for a project.

    :param ctx: Shared project context that carries state, filesystem paths,
        and AI runner configuration across every phase.
    :type ctx: ProjectContext
    :param dashboard: Optional dashboard instance for routing output and
        displaying per-phase status.  When ``None`` all output goes to
        ``sys.stdout`` via ``print``.
    :param agent_pool: Optional :class:`~workflow_lib.agent_pool.AgentPoolManager`
        for distributing parallel phases across configured agents.
    """

    def __init__(self, ctx: ProjectContext, dashboard: Optional[Any] = None,
                 max_retries: int = 3, timeout: int = 600,
                 auto_retries: Optional[int] = None,
                 agent_pool: Optional[Any] = None) -> None:
        self.ctx = ctx
        self.dashboard = dashboard
        self.max_retries = max(max_retries, 1)  # At least 1 attempt
        self.auto_retries = auto_retries or 0
        self.ctx.agent_timeout = timeout if timeout > 0 else None
        self.shutdown_requested = False
        self.agent_pool = agent_pool
        self._prev_sigint_handler: Optional[Union[Callable, int, signal.Handlers]] = None

    def _log(self, message: str) -> None:
        if self.dashboard:
            self.dashboard.log(message)
        else:
            print(message)

    def _set_phase(self, name: str, status: str, stage: str = "") -> None:
        if self.dashboard:
            self.dashboard.set_agent(f"plan/{name}", stage, status)

    def _handle_sigint(self, sig: int, frame: Any) -> None:
        if not self.shutdown_requested:
            self.shutdown_requested = True
            if self.dashboard:
                self.dashboard.set_shutting_down()
            else:
                self._log("\n[!] Ctrl-C detected. Current agent will finish. No new phases will start.")
                self._log("    Press Ctrl-C again to force exit immediately.")
        else:
            self._log("\n[!] Ctrl-C detected again. Forcing immediate exit...")
            os._exit(1)

    def install_signal_handler(self) -> None:
        self._prev_sigint_handler = signal.signal(signal.SIGINT, self._handle_sigint)

    def restore_signal_handler(self) -> None:
        if self._prev_sigint_handler is not None:
            signal.signal(signal.SIGINT, self._prev_sigint_handler)
            self._prev_sigint_handler = None

    def _prompt(self, message: str) -> str:
        if self.dashboard:
            return self.dashboard.prompt_input(message)
        return input(message + ": ")

    def run_phase_with_retry(self, phase: "BasePhase", max_retries: int = -1) -> None:  # type: ignore[name-defined]
        """Execute a phase, retrying on failure up to *max_retries* times.

        On each failure the user is prompted to retry, skip (``c`` — continue,
        treating the phase as manually resolved), or quit (``q``).  After all
        attempts are exhausted the process exits with code 1.

        :param phase: The phase object to execute.  Must implement
            ``execute(ctx: ProjectContext)``.
        :param max_retries: Maximum number of attempts before giving up.
            When ``0`` (default), uses ``self.max_retries``.
        :type max_retries: int
        :raises SystemExit: When the user chooses ``q`` or the phase exhausts
            all retry attempts.
        """
        if self.shutdown_requested:
            self._log("[!] Shutdown requested. Exiting after last completed phase.")
            sys.exit(0)
        if max_retries <= 0:
            max_retries = self.max_retries
        auto_failures = 0
        name = phase.display_name
        stage = phase.operation
        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                self._log(f"[Retry {attempt}/{max_retries}] Retrying {name}...")
                # Reset the agent entry so the timer restarts from now
                if self.dashboard:
                    self.dashboard.remove_agent(f"plan/{name}")

            self.ctx.current_phase = name
            self._set_phase(name, "running", stage)
            try:
                phase.execute(self.ctx)
                self.ctx.current_phase = ""
                self._log(f"-> Phase {name} completed.")
                self._set_phase(name, "done", stage)
                return
            except subprocess.TimeoutExpired:
                self.ctx.current_phase = ""
                timeout_secs = self.ctx.agent_timeout or "?"
                self._log(f"[!] Phase {name} timed out after {timeout_secs}s on attempt {attempt}/{max_retries}.")
                self._set_phase(name, "failed", stage)
                if attempt >= max_retries:
                    break
                self._log(f"    Auto-retrying...")
                self.ctx.state = self.ctx._load_state()
                continue
            except SystemExit as e:
                # Some verify scripts might return 0 through sys.exit() on success
                if e.code == 0:
                    self.ctx.current_phase = ""
                    self._set_phase(name, "done", stage)
                    return
                self.ctx.current_phase = ""
                self._log(f"[!] Phase {name} failed on attempt {attempt} (exit code {e.code}).")
                self._set_phase(name, "failed", stage)
                if attempt < max_retries:
                    if auto_failures < self.auto_retries:
                        auto_failures += 1
                        self._log(f"    Auto-retrying ({auto_failures}/{self.auto_retries})...")
                        self.ctx.state = self.ctx._load_state()
                        continue
                    self._set_phase(name, "waiting", stage)
                    action = self._prompt(
                        f"Phase '{name}' failed (attempt {attempt}/{max_retries}, exit code {e.code}). "
                        "Press ENTER to retry, 'c' to continue (if manually resolved), or 'q' to quit"
                    )
                    if action.lower() == 'q':
                        sys.exit(1)
                    elif action.lower() == 'c':
                        self._log(f"-> Continuing (assuming manual resolution).")
                        return

                self.ctx.state = self.ctx._load_state()
            except Exception as e:
                self.ctx.current_phase = ""
                self._log(f"[!] Phase {name} encountered an error on attempt {attempt}: {e}")
                self._set_phase(name, "failed", stage)
                if attempt < max_retries:
                    if auto_failures < self.auto_retries:
                        auto_failures += 1
                        self._log(f"    Auto-retrying ({auto_failures}/{self.auto_retries})...")
                        self.ctx.state = self.ctx._load_state()
                        continue
                    self._set_phase(name, "waiting", stage)
                    action = self._prompt(
                        f"Phase '{name}' errored (attempt {attempt}/{max_retries}): {e}\n"
                        "Press ENTER to retry, 'c' to continue (if manually resolved), or 'q' to quit"
                    )
                    if action.lower() == 'q':
                        sys.exit(1)
                    elif action.lower() == 'c':
                        self._log(f"-> Continuing (assuming manual resolution).")
                        return

                self.ctx.state = self.ctx._load_state()

        self._log(f"[!] {name} failed after {max_retries} attempts.")
        notify_failure(f"Plan phase '{name}' failed after {max_retries} attempts.")
        sys.exit(1)

    def _validate_artifacts(self, files: list, phase_name: str) -> None:
        """Validate that expected output artifacts exist and are non-empty.

        :param files: List of absolute file paths that should exist.
        :param phase_name: Name of the phase for error messages.
        :raises SystemExit: When any expected artifact is missing or empty.
        """
        for f in files:
            if not os.path.exists(f):
                self._log(f"[!] Artifact validation failed for {phase_name}: missing {f}")
                sys.exit(1)
            if os.path.isfile(f) and os.path.getsize(f) == 0:
                self._log(f"[!] Artifact validation failed for {phase_name}: empty file {f}")
                sys.exit(1)

    def run(self) -> None:
        """Run the full planning workflow from start to finish.

        Phases 1-20 are executed in sequence (see :meth:`_run_phases` for
        the complete ordering).  Signal handlers are installed for graceful
        Ctrl-C handling.

        :raises SystemExit: Propagated from :meth:`run_phase_with_retry` if any
            phase exhausts its retry budget.
        """
        self.install_signal_handler()
        try:
            self._run_phases()
        finally:
            self.restore_signal_handler()

    def _run_parallel_phases(self, phases: List[Any], label: str = "") -> None:
        """Run a batch of phases in parallel, optionally using the agent pool.

        When ``ctx.jobs > 1``, phases are submitted to a
        :class:`~concurrent.futures.ThreadPoolExecutor`.  Each worker sets
        thread-local overrides on the shared :class:`ProjectContext` so that
        ``run_ai()`` uses the correct runner and dashboard identity without
        mutating shared state.

        When an agent pool is configured, each worker acquires an agent from
        the pool.  Without a pool, the default runner is used for all workers.

        Falls back to sequential execution when ``ctx.jobs <= 1``.

        :param phases: List of phase objects to execute.
        :param label: Human-readable label for logging (e.g. "Phase 2").
        """
        if self.ctx.jobs <= 1 or len(phases) <= 1:
            self._log(f"[{label}] Running {len(phases)} phase(s) sequentially "
                      f"(jobs={self.ctx.jobs}, pool={'yes' if self.agent_pool else 'no'}).")
            for phase in phases:
                self.run_phase_with_retry(phase)
            return

        self._log(f"[{label}] Running {len(phases)} phases in parallel "
                  f"(jobs={self.ctx.jobs}, pool={'yes' if self.agent_pool else 'no'})...")
        errors: List[str] = []
        lock = threading.Lock()

        def _run_one(phase: Any) -> None:
            if self.shutdown_requested:
                return
            name = phase.display_name
            stage = phase.operation
            agent_cfg = None
            agent_name = ""

            # Acquire from pool if available; otherwise use default runner
            if self.agent_pool is not None:
                self._log(f"[{label}] Acquiring agent for {name}...")
                agent_cfg = self.agent_pool.acquire(timeout=300.0, step=None)
                if agent_cfg is None:
                    self._log(f"[{label}] TIMEOUT acquiring agent for {name}")
                    with lock:
                        errors.append(f"Timeout acquiring agent for {name}")
                    return
                agent_name = agent_cfg.name
                self._log(f"[{label}] Acquired {agent_name} for {name}")

            try:
                # Build a per-thread runner from pool config or use default
                if agent_cfg is not None:
                    thread_runner = make_runner(
                        agent_cfg.backend,
                        model=agent_cfg.model,
                        user=agent_cfg.user,
                        env=agent_cfg.env,
                    )
                    if agent_cfg.context_limit is not None:
                        set_agent_context_limit(agent_cfg.context_limit)
                else:
                    thread_runner = self.ctx.runner

                # Set thread-local overrides so run_ai() picks them up
                self.ctx._tls.runner = thread_runner
                self.ctx._tls.phase = name

                # Register on dashboard
                task_key = f"plan/{name}"
                self._log(f"[{label}] Dashboard: set_agent({task_key!r}, {stage!r}, 'running', agent_name={agent_name!r})")
                if self.dashboard:
                    self.dashboard.set_agent(
                        task_key, stage, "running",
                        agent_name=agent_name,
                    )

                phase.execute(self.ctx)

                self._log(f"-> Phase {name} completed" +
                          (f" ({agent_name})." if agent_name else "."))
                if self.dashboard:
                    self.dashboard.set_agent(
                        task_key, stage, "done",
                        agent_name=agent_name,
                    )
            except (SystemExit, Exception) as exc:
                if isinstance(exc, SystemExit) and exc.code == 0:
                    if self.dashboard:
                        self.dashboard.set_agent(f"plan/{name}", stage, "done",
                                                 agent_name=agent_name)
                    return
                self._log(f"[!] Phase {name} failed" +
                          (f" ({agent_name}): {exc}" if agent_name else f": {exc}"))
                if self.dashboard:
                    self.dashboard.set_agent(f"plan/{name}", stage, "failed",
                                             agent_name=agent_name)
                with lock:
                    errors.append(f"{name}: {exc}")
            finally:
                # Clean up thread-local overrides
                self.ctx._tls.runner = None
                self.ctx._tls.phase = None
                if agent_cfg is not None:
                    self.agent_pool.release(agent_cfg)
                    if agent_cfg.context_limit is not None:
                        set_agent_context_limit(None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.ctx.jobs) as executor:
            futures = [executor.submit(_run_one, p) for p in phases]
            concurrent.futures.wait(futures)

        if errors:
            for err in errors:
                self._log(f"[!] {err}")
            notify_failure(f"Plan parallel phase '{label}' had {len(errors)} failure(s).")
            sys.exit(1)

    def _run_phases(self) -> None:
        """Internal method that runs all planning phases."""
        self._log("Beginning multi-phase document generation and lifecycle orchestration...")

        # Startup validation: ensure all prompt files exist before running any phase
        missing = validate_all_prompts_exist(self.ctx.prompts_dir)
        if missing:
            for m in missing:
                self._log(f"[!] Missing prompt file: {m}")
            self._log(f"[!] {len(missing)} prompt file(s) missing from {self.ctx.prompts_dir}. Aborting.")
            sys.exit(1)

        # Phase 1: Generate each spec document sequentially (each gets prior docs as context)
        for doc in DOCS:
            self.run_phase_with_retry(Phase1GenerateDoc(doc))
            expected = self.ctx.get_document_path(doc)
            self._validate_artifacts([expected], f"Phase1/{doc['id']}")

        # Phase 2: Flesh out each spec document (parallel)
        self._run_parallel_phases(
            [Phase2FleshOutDoc(doc) for doc in DOCS], "Phase 2: Flesh Out"
        )

        # Phase 3: Summarize each document (parallel)
        self._run_parallel_phases(
            [Phase2BSummarizeDoc(doc) for doc in DOCS], "Phase 3: Summarize"
        )
        for doc in DOCS:
            self._validate_artifacts(
                [self.ctx.get_summary_path(doc)], f"Phase3/Summarize/{doc['id']}"
            )

        # Phase 4: Final holistic review
        self.run_phase_with_retry(Phase3FinalReview())

        # Phase 5: Conflict resolution
        self.run_phase_with_retry(Phase3AConflictResolution())
        self._validate_artifacts(
            [os.path.join(self.ctx.plan_dir, "conflict_resolution.md")],
            "Phase5"
        )

        # Phase 6: Adversarial review
        self.run_phase_with_retry(Phase3BAdversarialReview())
        self._validate_artifacts(
            [os.path.join(self.ctx.plan_dir, "adversarial_review.md")],
            "Phase6"
        )

        # Phase 7: Extract requirements from each doc (parallel, JSON output)
        if not self.ctx.state.get("requirements_extracted", False):
            self._run_parallel_phases(
                [Phase7ExtractRequirements(doc) for doc in DOCS],
                "Phase 7: Extract Requirements"
            )
            self.ctx.state["requirements_extracted"] = True
            self.ctx.save_state()

        # Phase 8: Filter meta requirements (parallel per doc)
        if not self.ctx.state.get("meta_requirements_filtered", False):
            self._run_parallel_phases(
                [Phase8FilterMetaRequirements(doc) for doc in DOCS],
                "Phase 8: Filter Meta Requirements"
            )
            self.ctx.state["meta_requirements_filtered"] = True
            self.ctx.save_state()

        # Phase 9: Merge requirements into requirements.json
        self.run_phase_with_retry(Phase9MergeRequirements())
        self._validate_artifacts(
            [os.path.join(self.ctx.plan_dir, "requirements.json")],
            "Phase9"
        )

        # Phase 10: Deduplicate requirements
        self.run_phase_with_retry(Phase10DeduplicateRequirements())

        # Phase 11: Scope Gate (human review)
        self.run_phase_with_retry(Phase11ScopeGate())

        # Phase 12: Order requirements (E2E-first)
        self.run_phase_with_retry(Phase12OrderRequirements())
        self._validate_artifacts(
            [os.path.join(self.ctx.plan_dir, "requirements_ordered.json")],
            "Phase12"
        )

        # Phase 13: Generate epic/requirement mappings (JSON)
        self.run_phase_with_retry(Phase13GenerateEpics())
        self._validate_artifacts(
            [os.path.join(self.ctx.plan_dir, "epic_mappings.json")],
            "Phase13"
        )

        # Phase 14: E2E interface definitions (single agent)
        self.run_phase_with_retry(Phase14E2EInterfaces())
        self._validate_artifacts(
            [os.path.join(self.ctx.plan_dir, "e2e_interfaces.md")],
            "Phase14"
        )

        # Phase 15: Feature gates (single agent)
        self.run_phase_with_retry(Phase15FeatureGates())
        self._validate_artifacts(
            [os.path.join(self.ctx.plan_dir, "feature_gates.md")],
            "Phase15"
        )

        # Phase 16: Red/Green task breakdown (parallel per phase)
        self.run_phase_with_retry(Phase16RedGreenTasks())
        self._validate_artifacts(
            [os.path.join(self.ctx.plan_dir, "tasks")],
            "Phase16"
        )

        # Phase 17: Review Red/Green tasks (parallel per phase)
        if not self.ctx.state.get("tasks_reviewed", False):
            tasks_dir = os.path.join(self.ctx.plan_dir, "tasks")
            if os.path.isdir(tasks_dir):
                phase_dirs = sorted([
                    d for d in os.listdir(tasks_dir)
                    if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")
                ])
                self._run_parallel_phases(
                    [Phase17ReviewRedGreenTasks(pid) for pid in phase_dirs],
                    "Phase 17: Review Tasks"
                )
            self.ctx.state["tasks_reviewed"] = True
            self.ctx.save_state()

        # Phase 18: Cross-phase review (single pass)
        if not self.ctx.state.get("cross_phase_reviewed", False):
            self.run_phase_with_retry(Phase6CCrossPhaseReview(pass_num=1))
            self.ctx.state["cross_phase_reviewed"] = True
            self.ctx.save_state()

        # Phase 19: Pre-Init task generation
        self.run_phase_with_retry(Phase19PreInitTask())

        # Phase 20: DAG generation
        self.run_phase_with_retry(Phase7ADAGGeneration())

        # Final validation: run validate.py --all to catch any cross-artifact
        # inconsistencies that per-phase validators may have missed.
        self._log("\n=> [Final Validation] Running full plan validation...")
        from .constants import TOOLS_DIR
        validate_script = os.path.join(TOOLS_DIR, "validate.py")
        validate_res = subprocess.run(
            [sys.executable, validate_script, "--all"],
            capture_output=True, text=True, cwd=self.ctx.root_dir
        )
        if validate_res.returncode != 0:
            self._log("[!] Final validation failed:")
            self._log(validate_res.stdout)
            if validate_res.stderr:
                self._log(validate_res.stderr)
            notify_failure("Plan final validation failed.")
            sys.exit(1)
        self._log(validate_res.stdout.strip() if validate_res.stdout.strip() else "All checks passed.")

        self._log("Project generation orchestration complete.")

