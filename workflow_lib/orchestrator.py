"""High-level orchestrator that drives the multi-phase planning workflow.

The :class:`Orchestrator` sequences all planning phases — document generation,
requirement extraction, epic breakdown, task breakdown, cross-phase review, and
DAG generation — in the correct order.  Each phase is executed with automatic
retry logic so that transient AI failures do not abort the entire run.

Typical usage::

    ctx = ProjectContext(root_dir, runner=GeminiRunner())
    Orchestrator(ctx).run()
"""

import os
import signal
import subprocess
import sys
from typing import Any, Callable, Optional, Union

from .constants import DOCS
from .context import ProjectContext
from .discord import notify_failure
from .prompt_registry import validate_all_prompts_exist
from .phases import *


class Orchestrator:
    """Sequences and retries all planning phases for a project.

    :param ctx: Shared project context that carries state, filesystem paths,
        and AI runner configuration across every phase.
    :type ctx: ProjectContext
    :param dashboard: Optional dashboard instance for routing output and
        displaying per-phase status.  When ``None`` all output goes to
        ``sys.stdout`` via ``print``.
    """

    def __init__(self, ctx: ProjectContext, dashboard: Optional[Any] = None,
                 max_retries: int = 3, timeout: int = 600,
                 auto_retries: Optional[int] = None) -> None:
        """Initialise the orchestrator with a project context.

        :param ctx: The :class:`~workflow_lib.context.ProjectContext` instance
            that phases will read and mutate.
        :type ctx: ProjectContext
        :param dashboard: Optional dashboard.
        :param max_retries: Maximum retry attempts per phase (0 = no retries).
        :param timeout: Timeout in seconds per AI agent invocation.
        :param auto_retries: Number of automatic retries before prompting
            the user.  ``None`` means always prompt immediately.
        """
        self.ctx = ctx
        self.dashboard = dashboard
        self.max_retries = max(max_retries, 1)  # At least 1 attempt
        self.auto_retries = auto_retries or 0
        self.ctx.agent_timeout = timeout if timeout > 0 else None
        self.shutdown_requested = False
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

    def _run_phases(self) -> None:
        """Internal method that runs all planning phases sequentially."""
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
        for doc in DOCS:
            self.run_phase_with_retry(Phase2FleshOutDoc(doc))

        # Phase 3: Summarize each document (parallel)
        for doc in DOCS:
            self.run_phase_with_retry(Phase2BSummarizeDoc(doc))

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
            for doc in DOCS:
                self.run_phase_with_retry(Phase7ExtractRequirements(doc))
            self.ctx.state["requirements_extracted"] = True
            self.ctx.save_state()

        # Phase 8: Filter meta requirements (parallel per doc)
        if not self.ctx.state.get("meta_requirements_filtered", False):
            for doc in DOCS:
                self.run_phase_with_retry(Phase8FilterMetaRequirements(doc))
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
                for phase_id in phase_dirs:
                    self.run_phase_with_retry(Phase17ReviewRedGreenTasks(phase_id))
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

        self._log("Project generation orchestration complete.")

