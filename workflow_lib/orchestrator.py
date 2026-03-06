"""High-level orchestrator that drives the multi-phase planning workflow.

The :class:`Orchestrator` sequences all planning phases — document generation,
requirement extraction, epic breakdown, task breakdown, cross-phase review, and
DAG generation — in the correct order.  Each phase is executed with automatic
retry logic so that transient AI failures do not abort the entire run.

Typical usage::

    ctx = ProjectContext(root_dir, runner=GeminiRunner())
    Orchestrator(ctx).run()
"""

import sys

from .constants import DOCS
from .context import ProjectContext
from .phases import *


class Orchestrator:
    """Sequences and retries all planning phases for a project.

    :param ctx: Shared project context that carries state, filesystem paths,
        and AI runner configuration across every phase.
    :type ctx: ProjectContext
    """

    def __init__(self, ctx: ProjectContext) -> None:
        """Initialise the orchestrator with a project context.

        :param ctx: The :class:`~workflow_lib.context.ProjectContext` instance
            that phases will read and mutate.
        :type ctx: ProjectContext
        """
        self.ctx = ctx

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
        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                print(f"\n   [Retry {attempt}/{max_retries}] Retrying {phase.__class__.__name__}...")
                
            try:
                phase.execute(self.ctx)
                print(f"   -> Phase {phase.__class__.__name__} completed.")
                return
            except SystemExit as e:
                # Some verify scripts might return 0 through sys.exit() on success
                if e.code == 0:
                    return
                print(f"\n[!] Phase {phase.__class__.__name__} failed on attempt {attempt}.")
                if attempt < max_retries:
                    action = input("Press ENTER to retry, 'c' to continue (if manually resolved), or 'q' to quit: ")
                    if action.lower() == 'q':
                        sys.exit(1)
                    elif action.lower() == 'c':
                        print(f"   -> Continuing (assuming manual resolution).")
                        return
                
                self.ctx.state = self.ctx._load_state()
            except Exception as e:
                print(f"\n[!] Phase {phase.__class__.__name__} encountered an error on attempt {attempt}: {e}")
                if attempt < max_retries:
                    action = input("Press ENTER to retry, 'c' to continue (if manually resolved), or 'q' to quit: ")
                    if action.lower() == 'q':
                        sys.exit(1)
                    elif action.lower() == 'c':
                        print(f"   -> Continuing (assuming manual resolution).")
                        return
                        
                self.ctx.state = self.ctx._load_state()
                
        print(f"\n[!] {phase.__class__.__name__} failed after {max_retries} attempts.")
        sys.exit(1)

    def run(self) -> None:
        """Run the full planning workflow from start to finish.

        Phases are executed in the following order:

        1. **Phase 1** — Generate each planning document (research + specs).
        2. **Phase 2** — Flesh out each spec document section by section.
        3. **Phase 3** — Final holistic review of all documents.
        4. **Phase 3B** — Adversarial review to stress-test the plan.
        5. **Phase 4A** — Extract requirements from each document.
        6. **Phase 4B** — Merge requirements into a master list, then scope gate.
        7. **Phase 4C** — Order requirements by priority/dependency.
        8. **Phase 5** — Generate implementation epics.
        9. **Phase 5B** — Identify and document shared components.
        10. **Phase 6** — Break epics into concrete tasks.
        11. **Phase 6B** — Review tasks for completeness.
        12. **Phase 6C** × 2 — Cross-phase review (two passes).
        13. **Phase 6D** × 2 — Reorder tasks within each phase (two passes).
        14. **Phase 7A** — Generate per-phase dependency DAGs.

        The AI runner ignore file is backed up before the run and restored in a
        ``finally`` block so that the workspace is always returned to a clean
        state regardless of errors.

        :raises SystemExit: Propagated from :meth:`run_phase_with_retry` if any
            phase exhausts its retry budget.
        """
        print("Beginning multi-phase document generation and lifecycle orchestration...")
        self.ctx.backup_ignore_file()
        try:
            # Phase 1 and 2 for each document
            for doc in DOCS:
                self.run_phase_with_retry(Phase1GenerateDoc(doc))
                self.run_phase_with_retry(Phase2FleshOutDoc(doc))

            self.run_phase_with_retry(Phase3FinalReview())
            self.run_phase_with_retry(Phase3BAdversarialReview())

            if not self.ctx.state.get("requirements_extracted", False):
                for doc in DOCS:
                    self.run_phase_with_retry(Phase4AExtractRequirements(doc))
                self.ctx.state["requirements_extracted"] = True
                self.ctx.save_state()
                
            self.run_phase_with_retry(Phase4BMergeRequirements())
            self.run_phase_with_retry(Phase4BScopeGate())
            self.run_phase_with_retry(Phase4COrderRequirements())
            self.run_phase_with_retry(Phase5GenerateEpics())
            self.run_phase_with_retry(Phase5BSharedComponents())
            #self.run_phase_with_retry(Phase6BreakDownTasks())
            self.run_phase_with_retry(Phase6BreakDownTasks())
            self.run_phase_with_retry(Phase6BReviewTasks())
            self.run_phase_with_retry(Phase6CCrossPhaseReview(pass_num=1))
            self.run_phase_with_retry(Phase6DReorderTasks(pass_num=1))
            self.run_phase_with_retry(Phase6CCrossPhaseReview(pass_num=2))
            self.run_phase_with_retry(Phase6DReorderTasks(pass_num=2))
            
            # DAG Generation Steps
            self.run_phase_with_retry(Phase7ADAGGeneration())
            #self.run_phase_with_retry(Phase7BDAGReview())
        finally:
            self.ctx.restore_ignore_file()
        print("\nProject generation orchestration complete.")

