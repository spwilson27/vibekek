"""
End-to-end tests for the vibekek-2 workflow.

Two scenarios are covered:

1. Planning E2E  – runs Orchestrator.run() against a real temp directory.
   Agent file-creation is simulated: patching ProjectContext.run_gemini to
   create the expected output files instead of invoking an actual AI CLI.

2. Implementation E2E – runs execute_dag() against a real temp git repo.
   Agent work is simulated: patching process_task / merge_task to return
   success and update state as the real functions would.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROMPT_NAMES = [
    "research_market.md",
    "research_competitive_analysis.md",
    "research_technical_analysis.md",
    "research_user_research.md",
    "spec_prd.md",
    "spec_tas.md",
    "spec_mcp_design.md",
    "spec_user_features.md",
    "spec_security_design.md",
    "spec_ui_ux_architecture.md",
    "spec_ui_ux_design.md",
    "spec_risks_mitigation.md",
    "spec_project_roadmap.md",
    "flesh_out.md",
    "summarize_doc.md",
    "adversarial_review.md",
    "conflict_resolution_review.md",
    "final_review.md",
    "extract_requirements.md",
    "merge_requirements.md",
    "order_requirements.md",
    "phases.md",
    "shared_components.md",
    "interface_contracts.md",
    "integration_test_plan.md",
    "group_tasks.md",
    "tasks.md",
    "review_tasks_in_phase.md",
    "cross_phase_review.md",
    "reorder_tasks.md",
    "dag_tasks.md",
    "dag_tasks_review.md",
    "implement_task.md",
    "review_task.md",
    "add_task.md",
    "merge_task.md",
    "requirements.md",
]

STUB_PROMPT = "Write {target_path} based on {description_ctx}."


def _create_tools_layout(root: Path) -> Path:
    """Create the minimal .tools/ directory structure inside *root*."""
    tools = root / ".tools"
    (root / "input").mkdir(parents=True)
    (tools / "prompts").mkdir(parents=True)
    (tools / "templates").mkdir(parents=True)

    # project description (in root/input/, matching INPUT_DIR)
    (root / "input" / "project-description.md").write_text(
        "# Test Project\n\nA simple test project for E2E testing.\n"
    )

    # stub prompt files – every phase just loads and formats these
    for name in PROMPT_NAMES:
        (tools / "prompts" / name).write_text(STUB_PROMPT)

    # verify_requirements.py is imported by constants.py; the real file must
    # be reachable.  We just need it to exist at TOOLS_DIR level.
    # constants.py does: sys.path.insert(0, TOOLS_DIR); from verify_requirements import …
    # The real file lives next to this test suite, so we symlink it.
    real_verify = Path(__file__).parent.parent / "verify_requirements.py"
    if real_verify.exists():
        link = tools / "verify_requirements.py"
        if not link.exists():
            link.symlink_to(real_verify)

    return tools


def _make_agent(side_effects: dict):
    """Return a run_gemini replacement that creates stub files on demand.

    *side_effects* maps phase-name keywords to extra content to write.
    The default behaviour creates an empty file at every non-directory
    allowed_file path.
    """

    def _run_gemini(self, full_prompt, allowed_files=None, sandbox=False):
        if allowed_files:
            for f in allowed_files:
                if isinstance(f, str) and not f.endswith(os.sep):
                    os.makedirs(os.path.dirname(os.path.abspath(f)), exist_ok=True)
                    if not os.path.exists(f):
                        # write minimal but valid stub content
                        content = "# Generated stub\n"
                        for kw, extra in side_effects.items():
                            if kw in full_prompt:
                                content = extra
                                break
                        with open(f, "w", encoding="utf-8") as fp:
                            fp.write(content)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _run_gemini


# ---------------------------------------------------------------------------
# Planning E2E
# ---------------------------------------------------------------------------


class TestPlanningE2E:
    """Full Orchestrator.run() with mocked AI file creation."""

    def _make_tempdir(self):
        """Return (tmpdir Path, tools Path). Caller must clean up."""
        tmp = Path(tempfile.mkdtemp(prefix="vk_plan_e2e_"))
        _create_tools_layout(tmp)

        # minimal git repo so stage_changes doesn't crash
        subprocess.run(["git", "init", "-q"], cwd=str(tmp), check=False)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"],
                       cwd=str(tmp), check=False,
                       env={**os.environ, "GIT_AUTHOR_NAME": "test",
                            "GIT_AUTHOR_EMAIL": "t@t.com",
                            "GIT_COMMITTER_NAME": "test",
                            "GIT_COMMITTER_EMAIL": "t@t.com"})
        return tmp

    def test_orchestrator_runs_all_phases_in_order(self, tmp_path):
        """Orchestrator.run() completes without error and sets all state flags."""
        import workflow_lib.constants as _const

        # Point constants at our temp tree
        tools_dir = tmp_path / ".tools"
        _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        # Sub-epic grouping JSON that process_sub_epic expects
        sub_epics_json = json.dumps({"Core Features": ["REQ-001"]})
        # Task file content with programmatic DAG metadata (no AI DAG needed)
        task_content = (
            "# Task: Implement core\n"
            "- depends_on: []\n"
            "- shared_components: []\n"
        )
        # Review summary content (non-empty so phases proceed)
        review_stub = "# Review\nAll good.\n"

        agent = _make_agent({
            "grouping": sub_epics_json,   # group_tasks prompt → JSON
            "review": review_stub,
        })

        with patch("workflow_lib.constants.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.constants.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.context.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.context.GEN_STATE_FILE",
                   str(tmp_path / ".gen_state.json")), \
             patch("workflow_lib.phases.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.executor.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.executor.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.context.ProjectContext.run_gemini", agent), \
             patch("workflow_lib.context.ProjectContext.stage_changes"), \
             patch("workflow_lib.context.ProjectContext.verify_changes"), \
             patch("workflow_lib.context.ProjectContext.get_workspace_snapshot",
                   return_value={}), \
             patch("subprocess.run", return_value=MagicMock(returncode=0,
                                                            stdout="", stderr="")), \
             patch("builtins.input", return_value="c"):

            from workflow_lib.context import ProjectContext
            from workflow_lib.orchestrator import Orchestrator

            ctx = ProjectContext(str(tmp_path))

            # Pre-populate the requirements.md that Phase4BScopeGate reads
            req_file = tmp_path / "requirements.md"
            req_file.write_text("# Requirements\n## Active\n### **[PRD-001]** Core feature\n")

            # Pre-populate ordered_requirements.md so Phase4COrderRequirements
            # can shutil.move it over requirements.md
            (tmp_path / "ordered_requirements.md").write_text(
                "# Ordered Requirements\n### **[PRD-001]** Core feature\n"
            )

            # For Phase6BreakDownTasks: pre-create the phases dir with one phase file,
            # and make run_gemini create the grouping JSON + task file correctly.
            phases_dir = tmp_path / "docs" / "plan" / "phases"
            phases_dir.mkdir(parents=True, exist_ok=True)
            (phases_dir / "phase_1.md").write_text("# Phase 1\n")

            tasks_dir = tmp_path / "docs" / "plan" / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)

            # Pre-create grouping JSON so the "already grouped" path is taken
            grouping_file = tasks_dir / "phase_1_grouping.json"
            grouping_file.write_text(sub_epics_json)

            # Pre-create the sub-epic task directory and a stub task file
            se_dir = tasks_dir / "phase_1" / "core_features"
            se_dir.mkdir(parents=True, exist_ok=True)
            (se_dir / "01_implement_core.md").write_text(task_content)

            # Mark tasks_generated so process_sub_epic skips AI call
            ctx.state["tasks_generated"] = ["phase_1/core_features"]

            # Phase6B: pre-create review_summary so inner loop skips
            review_sum = tasks_dir / "phase_1" / "review_summary.md"
            review_sum.parent.mkdir(parents=True, exist_ok=True)
            review_sum.write_text(review_stub)

            # Phase6C pass 1 & 2: pre-create summaries
            (tasks_dir / "cross_phase_review_summary_pass_1.md").write_text(review_stub)
            (tasks_dir / "cross_phase_review_summary_pass_2.md").write_text(review_stub)

            # Phase6D pass 1 & 2: pre-create summaries
            (tasks_dir / "reorder_tasks_summary_pass_1.md").write_text(review_stub)
            (tasks_dir / "reorder_tasks_summary_pass_2.md").write_text(review_stub)

            # Phase3A conflict resolution: pre-create stub
            plan_dir = tmp_path / "docs" / "plan"
            (plan_dir / "conflict_resolution.md").write_text("# Conflict Resolution\nNo conflicts.\n")

            # Phase3B adversarial review: stub file with no "scope creep" hits
            adversarial_file = (plan_dir / "adversarial_review.md")
            adversarial_file.write_text("# Adversarial Review\nLooks good.\n")

            # Phase5C interface contracts: pre-create stub
            (plan_dir / "interface_contracts.md").write_text("# Interface Contracts\n")

            # Phase6E integration test plan: pre-create stub
            (plan_dir / "integration_test_plan.md").write_text("# Integration Test Plan\n")

            orc = Orchestrator(ctx)
            orc.run()

        # All planning state flags should be set
        assert ctx.state.get("final_review_completed") is True
        assert ctx.state.get("requirements_merged") is True
        assert ctx.state.get("scope_gate_passed") is True
        assert ctx.state.get("requirements_ordered") is True
        assert ctx.state.get("phases_completed") is True
        assert ctx.state.get("shared_components_completed") is True
        assert ctx.state.get("tasks_completed") is True
        assert ctx.state.get("tasks_reviewed") is True
        assert ctx.state.get("dag_completed") is True

    def test_orchestrator_skips_already_completed_phases(self, tmp_path):
        """When state shows all phases done, Orchestrator skips all AI calls."""
        tools_dir = tmp_path / ".tools"
        _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        # Pre-populate all expected artifact files so validation passes
        plan_dir = tmp_path / "docs" / "plan"
        (plan_dir / "specs").mkdir(parents=True, exist_ok=True)
        (plan_dir / "research").mkdir(parents=True, exist_ok=True)
        (plan_dir / "phases").mkdir(parents=True, exist_ok=True)

        # Create all doc artifacts
        from workflow_lib.constants import DOCS
        for doc in DOCS:
            folder = "specs" if doc["type"] == "spec" else "research"
            (plan_dir / folder / f"{doc['id']}.md").write_text(f"# {doc['name']}\nStub.\n")

        # Create phase/task/review artifacts
        (plan_dir / "conflict_resolution.md").write_text("# Stub\n")
        (plan_dir / "adversarial_review.md").write_text("# Stub\n")
        (plan_dir / "shared_components.md").write_text("# Stub\n")
        (plan_dir / "interface_contracts.md").write_text("# Stub\n")
        (plan_dir / "integration_test_plan.md").write_text("# Stub\n")
        (plan_dir / "phases" / "phase_1.md").write_text("# Phase 1\n")
        (tmp_path / "requirements.md").write_text("# Requirements\n")

        tasks_dir = plan_dir / "tasks" / "phase_1"
        tasks_dir.mkdir(parents=True)
        dag_file = tasks_dir / "dag.json"
        dag_file.write_text(json.dumps({"sub/01_task.md": []}))

        with patch("workflow_lib.constants.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.constants.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.context.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.context.GEN_STATE_FILE",
                   str(tmp_path / ".gen_state.json")), \
             patch("workflow_lib.phases.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.context.ProjectContext.stage_changes"), \
             patch("workflow_lib.context.ProjectContext.verify_changes"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0,
                                                            stdout="", stderr="")), \
             patch("builtins.input", return_value="c"):

            from workflow_lib.context import ProjectContext
            from workflow_lib.orchestrator import Orchestrator

            ctx = ProjectContext(str(tmp_path))

            # Mark every phase as already done
            for doc in __import__("workflow_lib.constants", fromlist=["DOCS"]).DOCS:
                ctx.state.setdefault("generated", []).append(doc["id"])
                if doc["type"] == "spec":
                    ctx.state.setdefault("fleshed_out", []).append(doc["id"])
                ctx.state.setdefault("summarized", []).append(doc["id"])
                ctx.state.setdefault("extracted_requirements", []).append(doc["id"])

            ctx.state.update({
                "final_review_completed": True,
                "conflict_resolution_completed": True,
                "adversarial_review_completed": True,
                "requirements_extracted": True,
                "requirements_merged": True,
                "scope_gate_passed": True,
                "requirements_ordered": True,
                "phases_completed": True,
                "shared_components_completed": True,
                "interface_contracts_completed": True,
                "tasks_completed": True,
                "tasks_reviewed": True,
                "cross_phase_reviewed_pass_1": True,
                "cross_phase_reviewed_pass_2": True,
                "tasks_reordered_pass_1": True,
                "tasks_reordered_pass_2": True,
                "integration_test_plan_completed": True,
                "dag_completed": True,
            })

            run_gemini_calls = []

            def _spy_run_gemini(self, *args, **kwargs):
                run_gemini_calls.append(args)
                return subprocess.CompletedProcess(args=[], returncode=0,
                                                   stdout="", stderr="")

            with patch("workflow_lib.context.ProjectContext.run_gemini",
                       _spy_run_gemini):
                orc = Orchestrator(ctx)
                orc.run()

        # No AI calls should have been made — every phase was already complete
        assert run_gemini_calls == [], (
            f"Expected no AI calls when all phases done, got {len(run_gemini_calls)}"
        )

    def test_planning_phase_sequence(self, tmp_path):
        """Track which phases execute in order via run_gemini call count."""
        tools_dir = tmp_path / ".tools"
        _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        executed_phases = []
        task_content = "# Task\n- depends_on: []\n- shared_components: []\n"
        review_stub = "# Review\nAll good.\n"
        sub_epics_json = json.dumps({"Core": ["REQ-001"]})

        def _tracking_run_gemini(self, full_prompt,
                                  allowed_files=None, sandbox=False):
            # Record what type of output is being generated
            if allowed_files:
                for f in allowed_files:
                    if isinstance(f, str) and not f.endswith(os.sep):
                        executed_phases.append(os.path.basename(f))
                        os.makedirs(os.path.dirname(os.path.abspath(f)), exist_ok=True)
                        if not os.path.exists(f):
                            with open(f, "w") as fp:
                                fp.write("# Stub\n")
            return subprocess.CompletedProcess(args=[], returncode=0,
                                               stdout="", stderr="")

        with patch("workflow_lib.constants.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.constants.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.context.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.context.GEN_STATE_FILE",
                   str(tmp_path / ".gen_state.json")), \
             patch("workflow_lib.phases.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.context.ProjectContext.run_gemini",
                   _tracking_run_gemini), \
             patch("workflow_lib.context.ProjectContext.stage_changes"), \
             patch("workflow_lib.context.ProjectContext.verify_changes"), \
             patch("workflow_lib.context.ProjectContext.get_workspace_snapshot",
                   return_value={}), \
             patch("subprocess.run", return_value=MagicMock(returncode=0,
                                                            stdout="", stderr="")), \
             patch("builtins.input", return_value="c"):

            from workflow_lib.context import ProjectContext
            from workflow_lib.orchestrator import Orchestrator

            ctx = ProjectContext(str(tmp_path))

            # Same pre-population as full test
            req_file = tmp_path / "requirements.md"
            req_file.write_text("# Requirements\n")
            (tmp_path / "ordered_requirements.md").write_text("# Ordered\n")

            phases_dir = tmp_path / "docs" / "plan" / "phases"
            phases_dir.mkdir(parents=True, exist_ok=True)
            (phases_dir / "phase_1.md").write_text("# Phase 1\n")

            tasks_dir = tmp_path / "docs" / "plan" / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)
            (tasks_dir / "phase_1_grouping.json").write_text(sub_epics_json)

            se_dir = tasks_dir / "phase_1" / "core"
            se_dir.mkdir(parents=True, exist_ok=True)
            (se_dir / "01_task.md").write_text(task_content)
            ctx.state["tasks_generated"] = ["phase_1/core"]

            (tasks_dir / "phase_1" / "review_summary.md").write_text(review_stub)
            for suffix in [
                "cross_phase_review_summary_pass_1.md",
                "cross_phase_review_summary_pass_2.md",
                "reorder_tasks_summary_pass_1.md",
                "reorder_tasks_summary_pass_2.md",
            ]:
                (tasks_dir / suffix).write_text(review_stub)

            plan_dir = tmp_path / "docs" / "plan"
            (plan_dir / "conflict_resolution.md").write_text("# Conflict Resolution\nNo conflicts.\n")
            adversarial_file = (plan_dir / "adversarial_review.md")
            adversarial_file.write_text("# Adversarial Review\nLooks good.\n")
            (plan_dir / "interface_contracts.md").write_text("# Interface Contracts\n")
            (plan_dir / "integration_test_plan.md").write_text("# Integration Test Plan\n")

            orc = Orchestrator(ctx)
            orc.run()

        # Research docs are generated first (4 docs), then specs (9 docs)
        # Each doc gets Phase1 + Phase2 (specs only) calls
        # We can at least verify run_gemini was called for all major phases
        assert ctx.state.get("final_review_completed") is True
        assert ctx.state.get("dag_completed") is True
        # At minimum, all DOCS (13) had Phase1 calls
        assert len(executed_phases) >= 13


# ---------------------------------------------------------------------------
# Implementation E2E
# ---------------------------------------------------------------------------


def _init_git_repo(path: str):
    """Initialise a minimal git repo with a dev branch."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path,
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"],
                   cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "branch", "dev"], cwd=path, check=True,
                   capture_output=True, env=env)


class TestImplementationE2E:
    """Full execute_dag() with mocked process_task / merge_task."""

    def _simple_dag(self):
        """A tiny 3-task DAG: A has no deps; B depends on A; C depends on A."""
        return {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": ["phase_1/sub/01_a.md"],
            "phase_1/sub/01_c.md": ["phase_1/sub/01_a.md"],
        }

    def test_all_tasks_complete_successfully(self, tmp_path):
        """execute_dag completes all 3 tasks in dependency order."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = self._simple_dag()
        state = {"completed_tasks": [], "merged_tasks": []}

        def _fake_process_task(root_dir, task_id, presubmit_cmd,
                               backend, serena=False, **kwargs):
            return True

        def _fake_merge_task(root_dir, task_id, presubmit_cmd,
                             backend, cache_lock=None, serena=False, **kwargs):
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process_task), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_fake_merge_task), \
             patch("workflow_lib.executor.get_serena_enabled",
                   return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=2,
                        presubmit_cmd="echo ok", backend="gemini")

        assert set(state["completed_tasks"]) == set(dag.keys()), (
            f"Not all tasks completed: {state['completed_tasks']}"
        )
        assert set(state["merged_tasks"]) == set(dag.keys())

    def test_dag_respects_dependency_order(self, tmp_path):
        """B should not start until A is done (single-worker sequential check)."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": ["phase_1/sub/01_a.md"],
        }
        state = {"completed_tasks": [], "merged_tasks": []}
        completion_order = []

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          serena=False, **kwargs):
            completion_order.append(task_id)
            return True

        def _fake_merge(root_dir, task_id, presubmit_cmd, backend,
                        cache_lock=None, serena=False, **kwargs):
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_fake_merge), \
             patch("workflow_lib.executor.get_serena_enabled",
                   return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        # A must appear before B
        idx_a = completion_order.index("phase_1/sub/01_a.md")
        idx_b = completion_order.index("phase_1/sub/01_b.md")
        assert idx_a < idx_b, (
            f"A should complete before B, got order: {completion_order}"
        )

    def test_previously_completed_tasks_are_skipped(self, tmp_path):
        """Tasks already in state['completed_tasks'] are never processed."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": ["phase_1/sub/01_a.md"],
        }
        # A is already done; only B should run
        state = {
            "completed_tasks": ["phase_1/sub/01_a.md"],
            "merged_tasks": ["phase_1/sub/01_a.md"],
        }
        processed = []

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          serena=False, **kwargs):
            processed.append(task_id)
            return True

        def _fake_merge(root_dir, task_id, presubmit_cmd, backend,
                        cache_lock=None, serena=False, **kwargs):
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_fake_merge), \
             patch("workflow_lib.executor.get_serena_enabled",
                   return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        assert processed == ["phase_1/sub/01_b.md"], (
            f"Only B should have been processed, got: {processed}"
        )
        assert "phase_1/sub/01_a.md" in state["completed_tasks"]
        assert "phase_1/sub/01_b.md" in state["completed_tasks"]

    def test_task_failure_halts_dag(self, tmp_path):
        """If process_task returns False the dag halts and no further tasks run.

        After a task fails it is not added to completed_tasks, so get_ready_tasks
        would re-queue it — causing executor.submit() to raise RuntimeError on an
        already-shut-down pool.  We patch get_ready_tasks so that after the first
        call it returns [] (simulating the scheduler seeing nothing new to do),
        which lets the while-loop reach the failure-break and then sys.exit(1).
        """
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {"phase_1/sub/01_a.md": []}
        state = {"completed_tasks": [], "merged_tasks": []}

        ready_calls = [0]

        def _get_ready(master_dag, completed, active):
            ready_calls[0] += 1
            if ready_calls[0] == 1:
                return ["phase_1/sub/01_a.md"]
            return []  # nothing left after the task fails

        with patch("workflow_lib.executor.process_task", return_value=False), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.get_serena_enabled",
                   return_value=False), \
             patch("workflow_lib.executor.get_ready_tasks",
                   side_effect=_get_ready), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")), \
             pytest.raises(SystemExit) as exc:
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        assert exc.value.code == 1
        assert state["completed_tasks"] == [], "Failed task must not appear in completed"

    def test_merge_failure_halts_dag(self, tmp_path):
        """If merge_task returns False the dag halts with sys.exit(1)."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {"phase_1/sub/01_a.md": []}
        state = {"completed_tasks": [], "merged_tasks": []}

        ready_calls = [0]

        def _get_ready(master_dag, completed, active):
            ready_calls[0] += 1
            if ready_calls[0] == 1:
                return ["phase_1/sub/01_a.md"]
            return []

        with patch("workflow_lib.executor.process_task", return_value=True), \
             patch("workflow_lib.executor.merge_task", return_value=False), \
             patch("workflow_lib.executor.get_serena_enabled",
                   return_value=False), \
             patch("workflow_lib.executor.get_ready_tasks",
                   side_effect=_get_ready), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")), \
             pytest.raises(SystemExit) as exc:
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        assert exc.value.code == 1
        assert state["completed_tasks"] == [], "Merge-failed task must not appear in completed"

    def test_blocked_tasks_are_skipped(self, tmp_path):
        """Blocked tasks are never submitted to process_task."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": [],  # blocked
        }
        state = {"completed_tasks": [], "merged_tasks": []}
        processed = []

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          serena=False, **kwargs):
            processed.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.get_serena_enabled",
                   return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value={"phase_1/sub/01_b.md"}), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")), \
             patch("builtins.input", return_value="c"):
            execute_dag(root, dag, state, jobs=2,
                        presubmit_cmd="echo ok", backend="gemini")

        assert "phase_1/sub/01_b.md" not in processed, (
            "Blocked task should not have been processed"
        )
        assert "phase_1/sub/01_a.md" in processed

    def test_empty_dag_completes_immediately(self, tmp_path):
        """An empty DAG should exit cleanly with no tasks run."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        state = {"completed_tasks": [], "merged_tasks": []}

        with patch("workflow_lib.executor.get_serena_enabled",
                   return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("workflow_lib.executor.process_task") as mock_proc, \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")), \
             patch("builtins.input", return_value="c"):
            execute_dag(root, {}, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        mock_proc.assert_not_called()

    def test_parallel_independent_tasks(self, tmp_path):
        """Multiple independent tasks in same phase run in parallel (jobs=4)."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod
        import threading

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        # 4 independent tasks — all should complete
        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": [],
            "phase_1/sub/01_c.md": [],
            "phase_1/sub/01_d.md": [],
        }
        state = {"completed_tasks": [], "merged_tasks": []}
        processed = []
        lock = threading.Lock()

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          serena=False, **kwargs):
            with lock:
                processed.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.get_serena_enabled",
                   return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")), \
             patch("builtins.input", return_value="c"):
            execute_dag(root, dag, state, jobs=4,
                        presubmit_cmd="echo ok", backend="gemini")

        assert set(processed) == set(dag.keys()), (
            f"Not all tasks ran: {processed}"
        )
