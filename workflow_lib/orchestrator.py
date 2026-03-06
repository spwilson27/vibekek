from .constants import DOCS
from .context import ProjectContext
from .phases import *
class Orchestrator:
    def __init__(self, ctx: ProjectContext):
        self.ctx = ctx

    def run_phase_with_retry(self, phase, max_retries: int = 3):
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

    def run(self):
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

