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
import sys
from typing import Any, Optional

from .constants import DOCS
from .context import ProjectContext
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

    def __init__(self, ctx: ProjectContext, dashboard: Optional[Any] = None) -> None:
        """Initialise the orchestrator with a project context.

        :param ctx: The :class:`~workflow_lib.context.ProjectContext` instance
            that phases will read and mutate.
        :type ctx: ProjectContext
        :param dashboard: Optional dashboard.
        """
        self.ctx = ctx
        self.dashboard = dashboard

    def _log(self, message: str) -> None:
        if self.dashboard:
            self.dashboard.log(message)
        else:
            print(message)

    def _set_phase(self, name: str, status: str, command: str = "") -> None:
        if self.dashboard:
            self.dashboard.set_agent(f"plan/{name}", command, status)

    def run_phase_with_retry(self, phase: "BasePhase", max_retries: int = 3) -> None:  # type: ignore[name-defined]
        """Execute a phase, retrying on failure up to *max_retries* times.

        On each failure the user is prompted to retry, skip (``c`` — continue,
        treating the phase as manually resolved), or quit (``q``).  After all
        attempts are exhausted the process exits with code 1.

        :param phase: The phase object to execute.  Must implement
            ``execute(ctx: ProjectContext)``.
        :param max_retries: Maximum number of attempts before giving up.
            Defaults to ``3``.
        :type max_retries: int
        :raises SystemExit: When the user chooses ``q`` or the phase exhausts
            all retry attempts.
        """
        name = phase.display_name
        command = phase.operation
        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                self._log(f"[Retry {attempt}/{max_retries}] Retrying {name}...")

            self.ctx.current_phase = name
            self._set_phase(name, "running", command)
            try:
                phase.execute(self.ctx)
                self.ctx.current_phase = ""
                self._log(f"-> Phase {name} completed.")
                self._set_phase(name, "done", command)
                return
            except SystemExit as e:
                # Some verify scripts might return 0 through sys.exit() on success
                if e.code == 0:
                    self.ctx.current_phase = ""
                    self._set_phase(name, "done", command)
                    return
                self.ctx.current_phase = ""
                self._log(f"[!] Phase {name} failed on attempt {attempt}.")
                self._set_phase(name, "failed", command)
                if attempt < max_retries:
                    action = input("Press ENTER to retry, 'c' to continue (if manually resolved), or 'q' to quit: ")
                    if action.lower() == 'q':
                        sys.exit(1)
                    elif action.lower() == 'c':
                        self._log(f"-> Continuing (assuming manual resolution).")
                        return

                self.ctx.state = self.ctx._load_state()
            except Exception as e:
                self.ctx.current_phase = ""
                self._log(f"[!] Phase {name} encountered an error on attempt {attempt}: {e}")
                self._set_phase(name, "failed", command)
                if attempt < max_retries:
                    action = input("Press ENTER to retry, 'c' to continue (if manually resolved), or 'q' to quit: ")
                    if action.lower() == 'q':
                        sys.exit(1)
                    elif action.lower() == 'c':
                        self._log(f"-> Continuing (assuming manual resolution).")
                        return

                self.ctx.state = self.ctx._load_state()

        self._log(f"[!] {name} failed after {max_retries} attempts.")
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

        Phases are executed in the following order:

        1. **Phase 1** — Generate each planning document (research + specs).
        2. **Phase 2** — Flesh out each spec document section by section.
        3. **Phase 3** — Final holistic review of all documents.
        4. **Phase 3A** — Conflict resolution between documents.
        5. **Phase 3B** — Adversarial review to stress-test the plan.
        6. **Phase 4A** — Extract requirements from each document.
        7. **Phase 4B** — Merge requirements into a master list, then scope gate.
        8. **Phase 4C** — Order requirements by priority/dependency.
        9. **Phase 5** — Generate implementation epics.
        10. **Phase 5B** — Identify and document shared components.
        11. **Phase 5C** — Define interface contracts for shared components.
        12. **Phase 6** — Break epics into concrete tasks.
        13. **Phase 6B** — Review tasks for completeness.
        14. **Phase 6C** × 2 — Cross-phase review (two passes).
        15. **Phase 6D** × 2 — Validate task ordering (two passes).
        16. **Phase 6E** — Generate integration test plan.
        17. **Phase 7A** — Generate per-phase dependency DAGs.

        The AI runner ignore file is backed up before the run and restored in a
        ``finally`` block so that the workspace is always returned to a clean
        state regardless of errors.

        :raises SystemExit: Propagated from :meth:`run_phase_with_retry` if any
            phase exhausts its retry budget.
        """
        self._log("Beginning multi-phase document generation and lifecycle orchestration...")
        self.ctx.backup_ignore_file()
        try:
            # Phase 1 and 2 for each document
            for doc in DOCS:
                self.run_phase_with_retry(Phase1GenerateDoc(doc))
                expected = self.ctx.get_document_path(doc)
                self._validate_artifacts([expected], f"Phase1/{doc['id']}")

                self.run_phase_with_retry(Phase2FleshOutDoc(doc))

            self.run_phase_with_retry(Phase3FinalReview())
            self.run_phase_with_retry(Phase3AConflictResolution())
            self._validate_artifacts(
                [os.path.join(self.ctx.plan_dir, "conflict_resolution.md")],
                "Phase3A"
            )
            self.run_phase_with_retry(Phase3BAdversarialReview())
            self._validate_artifacts(
                [os.path.join(self.ctx.plan_dir, "adversarial_review.md")],
                "Phase3B"
            )

            if not self.ctx.state.get("requirements_extracted", False):
                for doc in DOCS:
                    self.run_phase_with_retry(Phase4AExtractRequirements(doc))
                self.ctx.state["requirements_extracted"] = True
                self.ctx.save_state()

            self.run_phase_with_retry(Phase4BMergeRequirements())
            self._validate_artifacts(
                [os.path.join(self.ctx.root_dir, "requirements.md")],
                "Phase4B"
            )
            self.run_phase_with_retry(Phase4BScopeGate())
            self.run_phase_with_retry(Phase4COrderRequirements())
            self.run_phase_with_retry(Phase5GenerateEpics())
            self._validate_artifacts(
                [os.path.join(self.ctx.plan_dir, "phases")],
                "Phase5"
            )
            self.run_phase_with_retry(Phase5BSharedComponents())
            self._validate_artifacts(
                [os.path.join(self.ctx.plan_dir, "shared_components.md")],
                "Phase5B"
            )
            self.run_phase_with_retry(Phase5CInterfaceContracts())
            self._validate_artifacts(
                [os.path.join(self.ctx.plan_dir, "interface_contracts.md")],
                "Phase5C"
            )
            self.run_phase_with_retry(Phase6BreakDownTasks())
            self._validate_artifacts(
                [os.path.join(self.ctx.plan_dir, "tasks")],
                "Phase6"
            )
            self.run_phase_with_retry(Phase6BReviewTasks())
            self.run_phase_with_retry(Phase6CCrossPhaseReview(pass_num=1))
            self.run_phase_with_retry(Phase6DReorderTasks(pass_num=1))
            self.run_phase_with_retry(Phase6CCrossPhaseReview(pass_num=2))
            self.run_phase_with_retry(Phase6DReorderTasks(pass_num=2))
            self.run_phase_with_retry(Phase6EIntegrationTestPlan())
            self._validate_artifacts(
                [os.path.join(self.ctx.plan_dir, "integration_test_plan.md")],
                "Phase6E"
            )

            # DAG Generation
            self.run_phase_with_retry(Phase7ADAGGeneration())
        finally:
            self.ctx.restore_ignore_file()
        self._log("Project generation orchestration complete.")

