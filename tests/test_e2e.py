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
    "spec_prd.md",
    "spec_tas.md",
    "spec_mcp_design.md",
    "spec_user_features.md",
    "spec_security_design.md",
    "spec_ui_ux_architecture.md",
    "spec_ui_ux_design.md",
    "spec_risks_mitigation.md",
    "spec_performance_spec.md",
    "spec_project_roadmap.md",
    "flesh_out.md",
    "summarize_doc.md",
    "adversarial_review.md",
    "conflict_resolution_review.md",
    "final_review.md",
    "extract_requirements.md",
    "filter_meta_requirements.md",
    "merge_requirements.md",
    "deduplicate_requirements.md",
    "order_requirements.md",
    "phases.md",
    "e2e_interfaces.md",
    "feature_gates.md",
    "red_green_tasks.md",
    "review_red_green_tasks.md",
    "cross_phase_review.md",
    "pre_init_task.md",
    "group_tasks.md",
    "tasks.md",
    "review_tasks_in_phase.md",
    "dag_tasks.md",
    "dag_tasks_review.md",
    "implement_task.md",
    "review_task.md",
    "add_task.md",
    "fix_requirements.md",
    "merge_task.md",
    "requirements.md",
    "feature_discuss.md",
    "feature_execute.md",
    "feature_spec.md",
    "fix_description_length.md",
    "fix_phase_mappings.md",
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

    return tools


def _make_agent(side_effects: dict, json_content: str = "{}"):
    """Return a run_gemini replacement that creates stub files on demand.

    *side_effects* maps phase-name keywords to extra content to write.
    The default behaviour creates an empty file at every non-directory
    allowed_file path.  Files ending in ``.json`` get *json_content*
    instead of a markdown stub.
    """

    def _run_gemini(self, full_prompt, allowed_files=None):
        if allowed_files:
            for f in allowed_files:
                if isinstance(f, str) and not f.endswith(os.sep):
                    os.makedirs(os.path.dirname(os.path.abspath(f)), exist_ok=True)
                    if not os.path.exists(f):
                        # write minimal but valid stub content
                        if f.endswith(".json"):
                            content = json_content
                        else:
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

        req_json_content = json.dumps({"requirements": [{"id": "PRD-001", "title": "Core feature", "status": "active"}]})
        agent = _make_agent({
            "grouping": sub_epics_json,   # group_tasks prompt → JSON
            "review": review_stub,
        }, json_content=req_json_content)

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
             patch("subprocess.run", return_value=MagicMock(returncode=0,
                                                            stdout="", stderr="")), \
             patch("builtins.input", return_value="c"):

            from workflow_lib.context import ProjectContext
            from workflow_lib.orchestrator import Orchestrator

            ctx = ProjectContext(str(tmp_path))

            # Pre-populate the requirements.json that Phase11ScopeGate reads
            plan_dir = tmp_path / "docs" / "plan"
            plan_dir.mkdir(parents=True, exist_ok=True)
            req_json = {"requirements": [{"id": "PRD-001", "title": "Core feature", "status": "active"}]}
            (plan_dir / "requirements.json").write_text(json.dumps(req_json))

            # Pre-populate requirements_ordered.json for Phase12OrderRequirements
            (plan_dir / "requirements_ordered.json").write_text(json.dumps(req_json))

            # Pre-populate epic_mappings.json for Phase13GenerateEpics
            (plan_dir / "epic_mappings.json").write_text(json.dumps({"Phase 1": {"Core": ["PRD-001"]}}))

            # Pre-populate e2e_interfaces.md for Phase14E2EInterfaces
            (plan_dir / "e2e_interfaces.md").write_text("# E2E Interfaces\n")

            # Pre-populate feature_gates.md for Phase15FeatureGates
            (plan_dir / "feature_gates.md").write_text("# Feature Gates\n")

            # For Phase16RedGreenTasks: pre-create the tasks dir with one phase,
            # and task files with JSON sidecars.
            tasks_dir = tmp_path / "docs" / "plan" / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)

            # Pre-create phase task directory and stub task files
            se_dir = tasks_dir / "phase_1" / "core_features"
            se_dir.mkdir(parents=True, exist_ok=True)
            (se_dir / "01_implement_core.md").write_text(task_content)
            (se_dir / "01_implement_core.json").write_text(json.dumps({"id": "01_implement_core", "depends_on": []}))

            # Mark tasks as completed so Phase16 skips
            ctx.state["tasks_completed"] = True

            # Phase17: pre-create review_summary so inner loop skips
            review_sum = tasks_dir / "phase_1" / "review_summary.md"
            review_sum.parent.mkdir(parents=True, exist_ok=True)
            review_sum.write_text(review_stub)

            # Phase18 (cross-phase review): pre-create summary
            (tasks_dir / "cross_phase_review_summary_pass_1.md").write_text(review_stub)

            # Phase3A conflict resolution: pre-create stub
            (plan_dir / "conflict_resolution.md").write_text("# Conflict Resolution\nNo conflicts.\n")

            # Phase3B adversarial review: stub file with no "scope creep" hits
            adversarial_file = (plan_dir / "adversarial_review.md")
            adversarial_file.write_text("# Adversarial Review\nLooks good.\n")

            orc = Orchestrator(ctx)
            orc.run()

        # All planning state flags should be set
        assert ctx.state.get("final_review_completed") is True
        assert ctx.state.get("requirements_merged") is True
        assert ctx.state.get("meta_requirements_filtered") is True
        assert ctx.state.get("requirements_deduplicated") is True
        assert ctx.state.get("scope_gate_passed") is True
        assert ctx.state.get("requirements_ordered") is True
        assert ctx.state.get("epics_completed") is True
        assert ctx.state.get("e2e_interfaces_completed") is True
        assert ctx.state.get("feature_gates_completed") is True
        assert ctx.state.get("tasks_completed") is True
        assert ctx.state.get("tasks_reviewed") is True
        assert ctx.state.get("cross_phase_reviewed") is True
        assert ctx.state.get("pre_init_task_completed") is True
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

        # Create all doc artifacts
        from workflow_lib.constants import DOCS
        for doc in DOCS:
            (plan_dir / "specs" / f"{doc['id']}.md").write_text(f"# {doc['name']}\nStub.\n")

        # Create phase/task/review artifacts
        (plan_dir / "conflict_resolution.md").write_text("# Stub\n")
        (plan_dir / "adversarial_review.md").write_text("# Stub\n")
        req_json = {"requirements": [{"id": "PRD-001", "title": "Core feature", "status": "active"}]}
        (plan_dir / "requirements.json").write_text(json.dumps(req_json))
        (plan_dir / "requirements_ordered.json").write_text(json.dumps(req_json))
        (plan_dir / "epic_mappings.json").write_text(json.dumps({"Phase 1": {"Core": ["PRD-001"]}}))
        (plan_dir / "e2e_interfaces.md").write_text("# E2E Interfaces\n")
        (plan_dir / "feature_gates.md").write_text("# Feature Gates\n")

        # Create summary artifacts
        summaries_dir = plan_dir / "summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)
        for doc in DOCS:
            (summaries_dir / f"{doc['id']}.md").write_text(f"# Summary: {doc['name']}\nStub.\n")

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
             patch("subprocess.run", return_value=MagicMock(returncode=0,
                                                            stdout="", stderr="")), \
             patch("builtins.input", return_value="c"):

            from workflow_lib.context import ProjectContext
            from workflow_lib.orchestrator import Orchestrator

            ctx = ProjectContext(str(tmp_path))

            # Mark every phase as already done
            for doc in __import__("workflow_lib.constants", fromlist=["DOCS"]).DOCS:
                ctx.state.setdefault("generated", []).append(doc["id"])
                ctx.state.setdefault("fleshed_out", []).append(doc["id"])
                ctx.state.setdefault("summarized", []).append(doc["id"])
                ctx.state.setdefault("extracted_requirements", []).append(doc["id"])

            ctx.state.update({
                "final_review_completed": True,
                "conflict_resolution_completed": True,
                "adversarial_review_completed": True,
                "requirements_extracted": True,
                "meta_requirements_filtered": True,
                "requirements_merged": True,
                "requirements_deduplicated": True,
                "scope_gate_passed": True,
                "requirements_ordered": True,
                "epics_completed": True,
                "e2e_interfaces_completed": True,
                "feature_gates_completed": True,
                "tasks_completed": True,
                "tasks_reviewed": True,
                "cross_phase_reviewed": True,
                "pre_init_task_completed": True,
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
                                  allowed_files=None):
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
             patch("subprocess.run", return_value=MagicMock(returncode=0,
                                                            stdout="", stderr="")), \
             patch("builtins.input", return_value="c"):

            from workflow_lib.context import ProjectContext
            from workflow_lib.orchestrator import Orchestrator

            ctx = ProjectContext(str(tmp_path))

            # Same pre-population as full test
            plan_dir = tmp_path / "docs" / "plan"
            plan_dir.mkdir(parents=True, exist_ok=True)
            req_json = {"requirements": [{"id": "PRD-001", "title": "Core feature", "status": "active"}]}
            (plan_dir / "requirements.json").write_text(json.dumps(req_json))
            (plan_dir / "requirements_ordered.json").write_text(json.dumps(req_json))
            (plan_dir / "epic_mappings.json").write_text(json.dumps({"Phase 1": {"Core": ["PRD-001"]}}))
            (plan_dir / "e2e_interfaces.md").write_text("# E2E Interfaces\n")
            (plan_dir / "feature_gates.md").write_text("# Feature Gates\n")

            tasks_dir = tmp_path / "docs" / "plan" / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)

            se_dir = tasks_dir / "phase_1" / "core"
            se_dir.mkdir(parents=True, exist_ok=True)
            (se_dir / "01_task.md").write_text(task_content)
            (se_dir / "01_task.json").write_text(json.dumps({"id": "01_task", "depends_on": []}))
            ctx.state["tasks_completed"] = True

            (tasks_dir / "phase_1" / "review_summary.md").write_text(review_stub)
            (tasks_dir / "cross_phase_review_summary_pass_1.md").write_text(review_stub)

            (plan_dir / "conflict_resolution.md").write_text("# Conflict Resolution\nNo conflicts.\n")
            adversarial_file = (plan_dir / "adversarial_review.md")
            adversarial_file.write_text("# Adversarial Review\nLooks good.\n")

            orc = Orchestrator(ctx)
            orc.run()

        # All docs are specs (10 docs), each gets Phase1 + Phase2 calls
        # We can at least verify run_gemini was called for all major phases
        assert ctx.state.get("final_review_completed") is True
        assert ctx.state.get("dag_completed") is True
        # At minimum, all DOCS (10) had Phase1 calls
        assert len(executed_phases) >= 10


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
                               backend, **kwargs):
            return True

        def _fake_merge_task(root_dir, task_id, presubmit_cmd,
                             backend, cache_lock=None, **kwargs):
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process_task), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_fake_merge_task), \
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
                          **kwargs):
            completion_order.append(task_id)
            return True

        def _fake_merge(root_dir, task_id, presubmit_cmd, backend,
                        cache_lock=None, **kwargs):
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_fake_merge), \
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
                          **kwargs):
            processed.append(task_id)
            return True

        def _fake_merge(root_dir, task_id, presubmit_cmd, backend,
                        cache_lock=None, **kwargs):
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_fake_merge), \
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

        def _get_ready(master_dag, completed, active, **kwargs):
            ready_calls[0] += 1
            if ready_calls[0] == 1:
                return ["phase_1/sub/01_a.md"]
            return []  # nothing left after the task fails

        with patch("workflow_lib.executor.process_task", return_value=False), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.get_ready_tasks",
                   side_effect=_get_ready), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("workflow_lib.executor.notify_failure"), \
             patch("os._exit", side_effect=SystemExit), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")), \
             pytest.raises(SystemExit):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")
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

        def _get_ready(master_dag, completed, active, **kwargs):
            ready_calls[0] += 1
            if ready_calls[0] == 1:
                return ["phase_1/sub/01_a.md"]
            return []

        with patch("workflow_lib.executor.process_task", return_value=True), \
             patch("workflow_lib.executor.merge_task", return_value=False), \
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
                          **kwargs):
            processed.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
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

        with patch("workflow_lib.executor.load_blocked_tasks",
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
                          **kwargs):
            with lock:
                processed.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
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


# ---------------------------------------------------------------------------
# Custom dev_branch E2E
# ---------------------------------------------------------------------------


def _init_git_repo_custom_branch(path: str, branch_name: str):
    """Initialise a minimal git repo with a custom integration branch."""
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
    subprocess.run(["git", "branch", branch_name], cwd=path, check=True,
                   capture_output=True, env=env)


class TestCustomDevBranchE2E:
    """E2E tests verifying that a custom dev_branch config is respected."""

    def test_custom_dev_branch_tasks_complete(self, tmp_path):
        """execute_dag with custom dev_branch completes all tasks."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        custom_branch = "integration"
        _init_git_repo_custom_branch(root, custom_branch)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": ["phase_1/sub/01_a.md"],
        }
        state = {"completed_tasks": [], "merged_tasks": []}

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            # Verify the custom dev_branch is passed through
            assert kwargs.get("dev_branch") == custom_branch, (
                f"Expected dev_branch={custom_branch!r}, got {kwargs.get('dev_branch')!r}"
            )
            return True

        def _fake_merge(root_dir, task_id, presubmit_cmd, backend,
                        cache_lock=None, **kwargs):
            assert kwargs.get("dev_branch") == custom_branch
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_fake_merge), \
             patch("workflow_lib.executor.get_dev_branch",
                   return_value=custom_branch), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        assert set(state["completed_tasks"]) == set(dag.keys())

    def test_custom_dev_branch_created_if_missing(self, tmp_path):
        """When the custom branch doesn't exist, execute_dag creates it."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        custom_branch = "my-dev"

        # Init repo WITHOUT the custom branch
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root,
                       check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"],
                       cwd=root, check=True, capture_output=True, env=env)

        # Verify branch doesn't exist yet
        res = subprocess.run(["git", "rev-parse", "--verify", custom_branch],
                             cwd=root, capture_output=True)
        assert res.returncode != 0, "Branch should not exist before test"

        dag = {}
        state = {"completed_tasks": [], "merged_tasks": []}

        mock_docker = MagicMock()
        mock_docker.copy_files = []
        with patch("workflow_lib.executor.get_dev_branch",
                   return_value=custom_branch), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("workflow_lib.executor.get_docker_config", return_value=mock_docker), \
             patch("workflow_lib.config.get_agent_pool_configs", return_value=[]):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        # Verify the custom branch was created
        res = subprocess.run(["git", "rev-parse", "--verify", custom_branch],
                             cwd=root, capture_output=True)
        assert res.returncode == 0, f"Branch {custom_branch!r} should have been created"

    def test_push_uses_custom_dev_branch(self, tmp_path):
        """After merge, the push targets the custom dev branch name."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        custom_branch = "staging"
        _init_git_repo_custom_branch(root, custom_branch)

        dag = {"phase_1/sub/01_a.md": []}
        state = {"completed_tasks": [], "merged_tasks": []}
        fetch_calls = []

        def _tracking_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "fetch" in cmd:
                fetch_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            return True

        def _fake_merge(root_dir, task_id, presubmit_cmd, backend,
                        cache_lock=None, **kwargs):
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_fake_merge), \
             patch("workflow_lib.executor.get_dev_branch",
                   return_value=custom_branch), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run", side_effect=_tracking_run):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        # Verify fetch used the custom branch name (execute_dag syncs local ref after merge_task pushes)
        assert any(custom_branch in " ".join(cmd) for cmd in fetch_calls), (
            f"Expected fetch with {custom_branch!r}, got: {fetch_calls}"
        )


# ---------------------------------------------------------------------------
# Graceful Shutdown E2E
# ---------------------------------------------------------------------------


class TestGracefulShutdownExecutorE2E:
    """E2E tests verifying CTRL-C graceful shutdown during execute_dag().

    Uses a slow mock agent (time.sleep) to simulate real work, then sets
    shutdown_requested mid-flight and verifies:
    - In-flight tasks complete
    - No new tasks are spawned after shutdown
    - State reflects completed work
    """

    def test_inflight_task_completes_on_shutdown(self, tmp_path):
        """A running task finishes even after shutdown_requested is set."""
        import time
        import threading
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        # Two independent tasks: A will be slow, B should never start
        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": [],
        }
        state = {"completed_tasks": [], "merged_tasks": []}
        processed = []
        lock = threading.Lock()

        def _slow_process(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            """Simulate a slow agent. Sets shutdown after starting."""
            if task_id == "phase_1/sub/01_a.md":
                # Signal shutdown while this task is running
                executor_mod.shutdown_requested = True
                time.sleep(0.3)  # Simulate work continuing
            with lock:
                processed.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_slow_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        # A must have had process_task run (it was in-flight when shutdown was set)
        assert "phase_1/sub/01_a.md" in processed, (
            "In-flight task A should have completed its process_task call"
        )
        # Merge is skipped on shutdown — task resumes on next run from last stage
        assert "phase_1/sub/01_a.md" not in state["completed_tasks"], (
            "Merge skipped on shutdown; task A not in completed_tasks until resumed"
        )

        # B should NOT have been processed (shutdown prevents new spawns)
        assert "phase_1/sub/01_b.md" not in processed, (
            "Task B should not have started after shutdown"
        )

    def test_multiple_inflight_tasks_all_complete(self, tmp_path):
        """All in-flight tasks complete even after shutdown is requested."""
        import time
        import threading
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        # 3 independent tasks + 1 dependent; with jobs=3, first 3 start together
        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": [],
            "phase_1/sub/01_c.md": [],
            "phase_1/sub/01_d.md": [],  # should never start
        }
        state = {"completed_tasks": [], "merged_tasks": []}
        processed = []
        started = threading.Event()
        lock = threading.Lock()

        def _slow_process(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            if task_id == "phase_1/sub/01_a.md":
                # Let all 3 tasks get scheduled, then trigger shutdown
                time.sleep(0.1)
                executor_mod.shutdown_requested = True
            time.sleep(0.3)
            with lock:
                processed.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_slow_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=3,
                        presubmit_cmd="echo ok", backend="gemini")

        # A, B, C were all in-flight and should all have process_task run
        for task in ["phase_1/sub/01_a.md", "phase_1/sub/01_b.md",
                     "phase_1/sub/01_c.md"]:
            assert task in processed, f"{task} should have completed process_task"
        # Merge is skipped on shutdown — none are in completed_tasks yet
        for task in ["phase_1/sub/01_a.md", "phase_1/sub/01_b.md",
                     "phase_1/sub/01_c.md"]:
            assert task not in state["completed_tasks"], (
                f"{task} should not be in completed_tasks (merge skipped on shutdown)"
            )

        # D should never have started
        assert "phase_1/sub/01_d.md" not in processed, (
            "Task D should not start after shutdown"
        )

    def test_merge_skipped_on_shutdown_leaves_completed_tasks_empty(self, tmp_path):
        """When shutdown fires during process_task, merge is skipped.

        completed_tasks is only updated on merge success, so after a shutdown
        the task is not in completed_tasks — it will resume from the last saved
        stage on the next run.
        """
        import time
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

        def _process(root_dir, task_id, presubmit_cmd, backend,
                     **kwargs):
            executor_mod.shutdown_requested = True
            time.sleep(0.1)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        # Merge was skipped on shutdown — task A not in completed_tasks
        assert "phase_1/sub/01_a.md" not in state["completed_tasks"]
        # B never ran (depends on A completing + merging)
        assert "phase_1/sub/01_b.md" not in state["completed_tasks"]


class TestGracefulShutdownOrchestratorE2E:
    """E2E tests verifying CTRL-C graceful shutdown during Orchestrator.run().

    Injects mock phases that simulate slow work, sets shutdown_requested
    mid-phase, and verifies:
    - Current phase completes
    - Subsequent phases do not execute
    """

    def test_current_phase_completes_next_skipped(self):
        """Phase in progress finishes; next phase never starts."""
        import time
        from workflow_lib.orchestrator import Orchestrator

        ctx = MagicMock()
        ctx._load_state.return_value = {}
        ctx.state = {}
        orc = Orchestrator(ctx)

        phase_a_executed = []
        phase_b_executed = []

        phase_a = MagicMock()
        phase_a.display_name = "SlowPhaseA"
        phase_a.operation = "test"

        def _slow_execute_a(_ctx):
            """Simulate slow agent work, then request shutdown."""
            time.sleep(0.2)
            phase_a_executed.append(True)
            orc.shutdown_requested = True

        phase_a.execute.side_effect = _slow_execute_a

        phase_b = MagicMock()
        phase_b.display_name = "PhaseB"
        phase_b.operation = "test"
        phase_b.execute.side_effect = lambda _ctx: phase_b_executed.append(True)

        # Run phase A, then try phase B
        orc.run_phase_with_retry(phase_a)
        assert phase_a_executed == [True], "Phase A should have completed"

        with pytest.raises(SystemExit) as exc_info:
            orc.run_phase_with_retry(phase_b)

        assert exc_info.value.code == 0
        assert phase_b_executed == [], "Phase B should not have executed"

    def test_signal_during_phase_allows_completion(self):
        """Sending SIGINT during a phase lets it finish, blocks next phase."""
        import signal
        import time
        import threading
        from workflow_lib.orchestrator import Orchestrator

        ctx = MagicMock()
        ctx._load_state.return_value = {}
        ctx.state = {}
        orc = Orchestrator(ctx)
        orc.install_signal_handler()

        phase_completed = []

        phase_a = MagicMock()
        phase_a.display_name = "LongPhaseA"
        phase_a.operation = "test"

        def _execute_with_signal(_ctx):
            """Simulate work, then send ourselves SIGINT mid-execution."""
            time.sleep(0.1)
            os.kill(os.getpid(), signal.SIGINT)
            # Continue working after signal — should NOT be interrupted
            time.sleep(0.2)
            phase_completed.append(True)

        phase_a.execute.side_effect = _execute_with_signal

        try:
            orc.run_phase_with_retry(phase_a)
        finally:
            orc.restore_signal_handler()

        assert phase_completed == [True], (
            "Phase should complete even after SIGINT"
        )
        assert orc.shutdown_requested, "shutdown_requested should be set"

        # Next phase should be blocked
        phase_b = MagicMock()
        phase_b.display_name = "PhaseB"
        phase_b.operation = "test"

        with pytest.raises(SystemExit) as exc_info:
            orc.run_phase_with_retry(phase_b)

        assert exc_info.value.code == 0
        phase_b.execute.assert_not_called()


class TestSIGINTNotForwardedToChild:
    """Verify that CTRL-C (SIGINT) is not forwarded to child subprocesses.

    Spawns a real child process (a Python while loop that writes to a file
    on completion) and sends SIGINT to the parent. The child should NOT
    receive the signal because it runs in its own session (start_new_session=True).
    """

    def test_child_process_survives_parent_sigint(self, tmp_path):
        """Child subprocess in its own session is not killed by parent SIGINT."""
        import subprocess
        import signal
        import time

        marker = tmp_path / "child_done.txt"

        # Child script: loops briefly, writes a marker file on completion
        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import time, sys\n"
            f"time.sleep(1)\n"
            f"open('{marker}', 'w').write('done')\n"
        )

        # Launch child in its own session (as runners.py now does)
        proc = subprocess.Popen(
            [sys.executable, str(child_script)],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Send SIGINT to the parent (this test process) — child should be unaffected
        time.sleep(0.1)
        # Save and restore handler so the test process doesn't die
        old_handler = signal.getsignal(signal.SIGINT)
        caught = []
        signal.signal(signal.SIGINT, lambda s, f: caught.append(True))
        try:
            os.kill(os.getpid(), signal.SIGINT)
        finally:
            signal.signal(signal.SIGINT, old_handler)

        assert caught, "Parent should have caught SIGINT"

        # Wait for child to finish naturally
        proc.wait(timeout=5)
        assert proc.returncode == 0, f"Child exited with {proc.returncode}"
        assert marker.exists(), "Child should have written marker file"
        assert marker.read_text() == "done"

    def test_child_without_new_session_receives_sigint(self, tmp_path):
        """Baseline: child WITHOUT start_new_session gets killed by process-group SIGINT.

        We run the entire scenario inside a wrapper subprocess (in its own
        session) so that the os.killpg does not affect pytest workers.
        """
        import subprocess

        wrapper_script = tmp_path / "wrapper.py"
        marker = tmp_path / "child_done.txt"
        wrapper_script.write_text(
            "import subprocess, signal, time, os, sys\n"
            f"marker = '{marker}'\n"
            "child_script = sys.argv[1]\n"
            "proc = subprocess.Popen(\n"
            "    [sys.executable, child_script],\n"
            "    stdout=subprocess.PIPE, stderr=subprocess.PIPE,\n"
            ")\n"
            "time.sleep(0.1)\n"
            "signal.signal(signal.SIGINT, lambda s, f: None)\n"
            "os.killpg(os.getpgid(proc.pid), signal.SIGINT)\n"
            "proc.wait(timeout=5)\n"
            "sys.exit(0 if proc.returncode != 0 else 1)\n"
        )

        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import time\n"
            f"time.sleep(1)\n"
            f"open('{marker}', 'w').write('done')\n"
        )

        # Run wrapper in its own session so killpg doesn't affect pytest
        result = subprocess.run(
            [sys.executable, str(wrapper_script), str(child_script)],
            start_new_session=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Child should have been killed by SIGINT (wrapper rc={result.returncode})"
        )
        assert not marker.exists(), "Child should NOT have written marker file"


# ---------------------------------------------------------------------------
# Timeout Handling E2E
# ---------------------------------------------------------------------------


class TestTimeoutHandlingOrchestratorE2E:
    """E2E tests verifying TimeoutExpired handling during planning phases."""

    def test_timeout_retries_then_fails(self):
        """Phase that always times out is retried up to max_retries, then exits."""
        import subprocess as sp
        from workflow_lib.orchestrator import Orchestrator

        ctx = MagicMock()
        ctx._load_state.return_value = {}
        ctx.state = {}
        ctx.agent_timeout = 10
        orc = Orchestrator(ctx, max_retries=3)

        phase = MagicMock()
        phase.display_name = "SlowPhase"
        phase.operation = "test"
        phase.execute.side_effect = sp.TimeoutExpired(cmd="test", timeout=10)

        with pytest.raises(SystemExit) as exc_info:
            orc.run_phase_with_retry(phase)

        assert exc_info.value.code == 1
        assert phase.execute.call_count == 3

    def test_timeout_then_success_on_retry(self):
        """Phase times out once, then succeeds on retry."""
        import subprocess as sp
        from workflow_lib.orchestrator import Orchestrator

        ctx = MagicMock()
        ctx._load_state.return_value = {}
        ctx.state = {}
        ctx.agent_timeout = 10
        orc = Orchestrator(ctx, max_retries=3)

        phase = MagicMock()
        phase.display_name = "FlakyPhase"
        phase.operation = "test"
        phase.execute.side_effect = [
            sp.TimeoutExpired(cmd="test", timeout=10),
            None,  # succeeds on retry
        ]

        orc.run_phase_with_retry(phase)
        assert phase.execute.call_count == 2

    def test_timeout_state_reloaded_between_retries(self):
        """State is reloaded from disk between timeout retries."""
        import subprocess as sp
        from workflow_lib.orchestrator import Orchestrator

        fresh_state = {"reloaded": True}
        ctx = MagicMock()
        ctx._load_state.return_value = fresh_state
        ctx.state = {}
        ctx.agent_timeout = 10
        orc = Orchestrator(ctx, max_retries=2)

        phase = MagicMock()
        phase.display_name = "TimeoutPhase"
        phase.operation = "test"
        phase.execute.side_effect = [
            sp.TimeoutExpired(cmd="test", timeout=10),
            None,
        ]

        orc.run_phase_with_retry(phase)
        # State should have been reloaded after the timeout
        ctx._load_state.assert_called()
        assert ctx.state == fresh_state


# ---------------------------------------------------------------------------
# Process Task Retry E2E
# ---------------------------------------------------------------------------


class TestProcessTaskRetryE2E:
    """E2E tests for process_task internal retry logic.

    process_task runs impl agent, review agent, then verification loop
    (presubmit up to max_retries times, calling review agent between retries).
    """

    def test_presubmit_passes_first_try(self, tmp_path):
        """Task succeeds when presubmit passes on first attempt."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)


        with patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("subprocess.run") as mock_run:

            # Configure subprocess.run responses
            def _fake_run(cmd, **kwargs):
                result = MagicMock(returncode=0, stdout="", stderr=b"")
                if isinstance(cmd, list) and "status" in cmd and "--porcelain" in cmd:
                    result.stdout = "M file.py"
                    result.text = True
                return result

            mock_run.side_effect = _fake_run

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=3)

        assert result is True

    def test_presubmit_fails_all_retries(self, tmp_path):
        """Task fails when presubmit fails max_retries times."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)


        presubmit_call_count = [0]

        with patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("workflow_lib.config.get_config_defaults", return_value={"retries": 0}), \
             patch("subprocess.run") as mock_run:

            def _fake_run(cmd, **kwargs):
                result = MagicMock(returncode=0, stdout="", stderr=b"")
                if isinstance(cmd, list):
                    cmd_str = " ".join(cmd)
                    if cmd_str == "echo ok":
                        presubmit_call_count[0] += 1
                        result.returncode = 1
                        result.stdout = "FAIL"
                        result.stderr = "error"
                return result

            mock_run.side_effect = _fake_run

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=3)

        assert result is False
        assert presubmit_call_count[0] == 3

    def test_presubmit_fails_then_succeeds_on_retry(self, tmp_path):
        """Task succeeds after presubmit fails once then passes on retry."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)


        presubmit_attempts = [0]

        with patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("subprocess.run") as mock_run:

            def _fake_run(cmd, **kwargs):
                result = MagicMock(returncode=0, stdout="", stderr=b"")
                if isinstance(cmd, list):
                    cmd_str = " ".join(cmd)
                    if cmd_str == "echo ok":
                        presubmit_attempts[0] += 1
                        if presubmit_attempts[0] == 1:
                            result.returncode = 1
                            result.stdout = "FAIL"
                            result.stderr = "error"
                    elif "status" in cmd and "--porcelain" in cmd:
                        result.stdout = "M file.py"
                return result

            mock_run.side_effect = _fake_run

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=3)

        assert result is True
        assert presubmit_attempts[0] == 2

    def test_impl_agent_failure_aborts_task(self, tmp_path):
        """If the implementation agent fails, task returns False immediately."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)


        agent_calls = []

        def _fake_run_agent(agent_type, prompt_file, context, cwd, backend,
                            dashboard=None, task_id="", model=None, agent_pool=None,
                            container_name=None, container_env_file="", _pre_acquired_agent=None):
            agent_calls.append(agent_type)
            if agent_type == "Implementation":
                return False  # impl fails
            return True

        with patch("workflow_lib.executor.run_agent", side_effect=_fake_run_agent), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("workflow_lib.config.get_config_defaults", return_value={"retries": 0}), \
             patch("subprocess.run") as mock_run:

            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=3)

        assert result is False
        assert agent_calls == ["Implementation"]

    def test_review_agent_failure_aborts_task(self, tmp_path):
        """If the review agent fails, task returns False (no verification)."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)


        agent_calls = []

        def _fake_run_agent(agent_type, prompt_file, context, cwd, backend,
                            dashboard=None, task_id="", model=None, agent_pool=None,
                            container_name=None, container_env_file="", _pre_acquired_agent=None):
            agent_calls.append(agent_type)
            if agent_type == "Review":
                return False
            return True

        with patch("workflow_lib.executor.run_agent", side_effect=_fake_run_agent), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("workflow_lib.config.get_config_defaults", return_value={"retries": 0}), \
             patch("subprocess.run") as mock_run:

            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=3)

        assert result is False
        assert agent_calls == ["Implementation", "Review"]

    def test_shutdown_during_verification_exhausts_retries(self, tmp_path):
        """Shutdown during verification no longer skips remaining attempts.

        Verification runs to completion even after shutdown_requested is set —
        all max_retries attempts are exhausted before the task fails.
        """
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        def _fake_run_agent(agent_type, prompt_file, context, cwd, backend,
                            dashboard=None, task_id="", model=None, agent_pool=None,
                            container_name=None, container_env_file="", _pre_acquired_agent=None):
            if "Retry" in agent_type:
                # Set shutdown after first presubmit failure's review retry
                executor_mod.shutdown_requested = True
            return True

        presubmit_calls = [0]

        with patch("workflow_lib.executor.run_agent", side_effect=_fake_run_agent), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("subprocess.run") as mock_run:

            def _fake_run(cmd, **kwargs):
                result = MagicMock(returncode=0, stdout="", stderr=b"")
                if isinstance(cmd, list):
                    cmd_str = " ".join(cmd)
                    if cmd_str == "echo ok":
                        presubmit_calls[0] += 1
                        result.returncode = 1
                        result.stdout = "FAIL"
                        result.stderr = "error"
                return result

            mock_run.side_effect = _fake_run

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=3)

        assert result is False
        # All 3 presubmit attempts ran — shutdown no longer short-circuits
        # the verification loop; tasks must exhaust retries before failing.
        assert presubmit_calls[0] == 3


# ---------------------------------------------------------------------------
# process_task pushes branch to origin
# ---------------------------------------------------------------------------


class TestProcessTaskPushesBranch:
    """Verify process_task pushes the task branch back to the main repo.

    Without this push, merge_task cannot find the task branch because the
    clone (where the work was done) is cleaned up after process_task returns.
    """

    def test_task_branch_pushed_to_origin_on_success(self, tmp_path):
        """After process_task succeeds, the task branch must exist in root_dir."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        push_calls = []

        with patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("subprocess.run") as mock_run:

            def _fake_run(cmd, **kwargs):
                result = MagicMock(returncode=0, stdout="", stderr=b"")
                if isinstance(cmd, list):
                    if "status" in cmd and "--porcelain" in cmd:
                        result.stdout = "M file.py"
                    if "push" in cmd and "origin" in cmd:
                        push_calls.append(cmd)
                    # Ensure stderr is a string for text-mode callers
                    if kwargs.get("text") or kwargs.get("capture_output"):
                        result.stderr = ""
                return result

            mock_run.side_effect = _fake_run

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=3)

        assert result is True
        # Verify git push was called with the correct task branch name.
        # Staged architecture: impl, review, and validate each push once.
        assert len(push_calls) == 3, (
            f"Expected exactly 3 push calls (one per stage), got {len(push_calls)}"
        )
        for push_cmd in push_calls:
            assert push_cmd == ["git", "push", "origin", "ai-phase-sub_01_a"], (
                f"Push called with unexpected args: {push_cmd}"
            )

    def test_task_fails_if_push_fails(self, tmp_path):
        """process_task returns False when the branch push fails."""
        from workflow_lib.executor import process_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        with patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: Test"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("subprocess.run") as mock_run:

            def _fake_run(cmd, **kwargs):
                result = MagicMock(returncode=0, stdout="", stderr="")
                if isinstance(cmd, list):
                    if "status" in cmd and "--porcelain" in cmd:
                        result.stdout = "M file.py"
                    if "push" in cmd and "origin" in cmd:
                        result.returncode = 1
                        result.stderr = "error: failed to push"
                    if isinstance(result.stderr, bytes) and (kwargs.get("text") or kwargs.get("capture_output")):
                        result.stderr = ""
                return result

            mock_run.side_effect = _fake_run

            result = process_task(root, "phase_1/sub/01_a.md", "echo ok",
                                  backend="gemini", max_retries=3)

        assert result is False


# ---------------------------------------------------------------------------
# Resume from Partial State E2E
# ---------------------------------------------------------------------------


class TestResumeFromPartialStateE2E:
    """E2E tests verifying execute_dag correctly resumes from partial state.

    Simulates a previous interrupted run by pre-populating completed_tasks,
    then verifying only remaining tasks are processed.
    """

    def test_resume_skips_completed_runs_remaining(self, tmp_path):
        """Tasks in completed_tasks are skipped; remaining run normally."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": ["phase_1/sub/01_a.md"],
            "phase_1/sub/01_c.md": ["phase_1/sub/01_b.md"],
        }
        # Simulate: A and B completed in a prior run
        state = {
            "completed_tasks": ["phase_1/sub/01_a.md", "phase_1/sub/01_b.md"],
            "merged_tasks": ["phase_1/sub/01_a.md", "phase_1/sub/01_b.md"],
        }
        processed = []

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            processed.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        # Only C should have been processed
        assert processed == ["phase_1/sub/01_c.md"]
        assert set(state["completed_tasks"]) == set(dag.keys())

    def test_resume_all_completed_exits_immediately(self, tmp_path):
        """When all tasks already completed, execute_dag exits with no work."""
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": [],
        }
        state = {
            "completed_tasks": ["phase_1/sub/01_a.md", "phase_1/sub/01_b.md"],
            "merged_tasks": ["phase_1/sub/01_a.md", "phase_1/sub/01_b.md"],
        }

        with patch("workflow_lib.executor.process_task") as mock_proc, \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        mock_proc.assert_not_called()

    def test_resume_respects_dependencies_of_remaining(self, tmp_path):
        """Resumed tasks still respect dependency ordering."""
        import threading
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": ["phase_1/sub/01_a.md"],
            "phase_1/sub/01_c.md": ["phase_1/sub/01_b.md"],
            "phase_1/sub/01_d.md": ["phase_1/sub/01_c.md"],
        }
        # A already done; B, C, D remain
        state = {
            "completed_tasks": ["phase_1/sub/01_a.md"],
            "merged_tasks": ["phase_1/sub/01_a.md"],
        }
        completion_order = []
        lock = threading.Lock()

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            with lock:
                completion_order.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        # B must come before C, C before D
        assert completion_order.index("phase_1/sub/01_b.md") < \
               completion_order.index("phase_1/sub/01_c.md")
        assert completion_order.index("phase_1/sub/01_c.md") < \
               completion_order.index("phase_1/sub/01_d.md")
        assert "phase_1/sub/01_a.md" not in completion_order

    def test_resume_after_shutdown_continues_from_saved_state(self, tmp_path):
        """Simulates: run1 processes A then shuts down; run2 resumes from saved state."""
        import time
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": ["phase_1/sub/01_a.md"],
            "phase_1/sub/01_c.md": ["phase_1/sub/01_a.md"],
        }

        # --- Run 1: process A, merge A, then shutdown (before B/C scheduled) ---
        executor_mod.shutdown_requested = False
        state_run1 = {"completed_tasks": [], "merged_tasks": []}
        processed_run1 = []

        def _process_run1(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            processed_run1.append(task_id)
            return True

        def _merge_run1(root_dir, task_id, *args, **kwargs):
            # Shutdown after A merges — prevents B/C from being scheduled
            executor_mod.shutdown_requested = True
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_process_run1), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_merge_run1), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state_run1, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        assert processed_run1 == ["phase_1/sub/01_a.md"]
        assert state_run1["completed_tasks"] == ["phase_1/sub/01_a.md"]

        # --- Run 2: resume with state from run 1 ---
        executor_mod.shutdown_requested = False
        state_run2 = dict(state_run1)  # carry forward
        processed_run2 = []

        def _process_run2(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            processed_run2.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_process_run2), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state_run2, jobs=2,
                        presubmit_cmd="echo ok", backend="gemini")

        # B and C should have been processed (not A again)
        assert "phase_1/sub/01_a.md" not in processed_run2
        assert set(processed_run2) == {"phase_1/sub/01_b.md", "phase_1/sub/01_c.md"}
        assert set(state_run2["completed_tasks"]) == set(dag.keys())


class TestMergeDuringShutdownE2E:
    """E2E tests verifying merge behavior during graceful shutdown.

    When Ctrl-C triggers shutdown_requested, merges are SKIPPED for tasks whose
    process_task completed while shutdown was set. Each stage pushes its work to
    the remote branch, so the task resumes from the last completed stage on the
    next run — nothing is lost.
    """

    def test_merge_skipped_when_shutdown_fires_during_process_task(self, tmp_path):
        """process_task returns True but merge is skipped when shutdown was set."""
        import time
        import threading
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": [],
        }
        state = {"completed_tasks": [], "merged_tasks": []}
        processed = []
        merged = []
        lock = threading.Lock()

        def _slow_process(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            if task_id == "phase_1/sub/01_a.md":
                executor_mod.shutdown_requested = True
                time.sleep(0.2)
            with lock:
                processed.append(task_id)
            return True

        def _track_merge(root_dir, task_id, presubmit_cmd, backend,
                         max_retries=3, cache_lock=None,
                         dashboard=None, model=None, dev_branch="dev", **kwargs):
            with lock:
                merged.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_slow_process), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_track_merge), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        # A ran process_task but merge was skipped (shutdown set during process_task)
        assert "phase_1/sub/01_a.md" in processed
        assert "phase_1/sub/01_a.md" not in merged, (
            "Merge should be skipped when shutdown fires during process_task"
        )
        assert "phase_1/sub/01_a.md" not in state["completed_tasks"]

        # B should not have been processed at all
        assert "phase_1/sub/01_b.md" not in processed
        assert "phase_1/sub/01_b.md" not in merged

    def test_multiple_inflight_tasks_all_skip_merge_on_shutdown(self, tmp_path):
        """All in-flight tasks that complete with shutdown set skip merge."""
        import time
        import threading
        from workflow_lib.executor import execute_dag
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        root = str(tmp_path)
        _init_git_repo(root)

        dag = {
            "phase_1/sub/01_a.md": [],
            "phase_1/sub/01_b.md": [],
            "phase_1/sub/01_c.md": [],
            "phase_1/sub/01_d.md": [],  # should never start
        }
        state = {"completed_tasks": [], "merged_tasks": []}
        processed = []
        merged = []
        lock = threading.Lock()

        def _slow_process(root_dir, task_id, presubmit_cmd, backend,
                          **kwargs):
            if task_id == "phase_1/sub/01_a.md":
                time.sleep(0.1)
                executor_mod.shutdown_requested = True
            time.sleep(0.3)
            with lock:
                processed.append(task_id)
            return True

        def _track_merge(root_dir, task_id, presubmit_cmd, backend,
                         max_retries=3, cache_lock=None,
                         dashboard=None, model=None, dev_branch="dev", **kwargs):
            with lock:
                merged.append(task_id)
            return True

        with patch("workflow_lib.executor.process_task",
                   side_effect=_slow_process), \
             patch("workflow_lib.executor.merge_task",
                   side_effect=_track_merge), \
             patch("workflow_lib.executor.load_blocked_tasks",
                   return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="",
                                          stderr="")):
            execute_dag(root, dag, state, jobs=3,
                        presubmit_cmd="echo ok", backend="gemini")

        # A, B, C all ran process_task but had merges skipped
        for task in ["phase_1/sub/01_a.md", "phase_1/sub/01_b.md",
                     "phase_1/sub/01_c.md"]:
            assert task in processed, f"{task} should have completed process_task"
            assert task not in merged, f"{task} merge should be skipped on shutdown"
        assert state["completed_tasks"] == []

        # D should never have started
        assert "phase_1/sub/01_d.md" not in processed
        assert "phase_1/sub/01_d.md" not in merged

    def test_run_agent_skips_all_types_during_shutdown(self, tmp_path):
        """run_agent returns False for all agent types during shutdown,
        preventing new work from starting."""
        import workflow_lib.executor as executor_mod
        from workflow_lib.executor import run_agent

        executor_mod.shutdown_requested = True
        root = str(tmp_path)

        # Create a minimal prompt file
        prompts_dir = os.path.join(root, ".tools", "prompts")
        os.makedirs(prompts_dir, exist_ok=True)
        with open(os.path.join(prompts_dir, "implement_task.md"), "w") as f:
            f.write("Implement {task_name}")

        try:
            with patch("workflow_lib.executor.TOOLS_DIR", root + "/.tools"), \
                 patch("workflow_lib.executor.run_ai_command",
                       return_value=(0, "")) as mock_ai, \
                 patch("workflow_lib.executor.get_project_images",
                       return_value=[]):
                # All agent types should be skipped during shutdown
                for agent_type in ("Implementation", "Review", "Merge"):
                    result = run_agent(agent_type, "implement_task.md",
                                       {"task_name": "test"}, root, "gemini")
                    assert result is False, (
                        f"{agent_type} agent should be skipped during shutdown"
                    )
                mock_ai.assert_not_called()
        finally:
            executor_mod.shutdown_requested = False

    def test_merge_verification_loop_not_skipped_during_shutdown(self, tmp_path):
        """The merge verification loop inside merge_task does not bail out
        when shutdown_requested is True."""
        import threading
        from workflow_lib.executor import merge_task
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = True
        root = str(tmp_path)
        _init_git_repo(root)

        # Create task file so get_task_details can read it
        task_dir = os.path.join(root, "docs", "plan", "tasks", "phase_1",
                                "sub")
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, "01_a.md"), "w") as f:
            f.write("# Task: Test Task\nDo something.\n")

        # Create the feature branch with a commit
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }
        subprocess.run(["git", "checkout", "-b", "ai-phase-sub_01_a"],
                       cwd=root, check=True, capture_output=True, env=env)
        with open(os.path.join(root, "feature.txt"), "w") as f:
            f.write("feature work\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True,
                       capture_output=True, env=env)
        subprocess.run(["git", "commit", "-m", "feature work"],
                       cwd=root, check=True, capture_output=True, env=env)
        subprocess.run(["git", "checkout", "main"], cwd=root, check=True,
                       capture_output=True, env=env)

        try:
            with patch("workflow_lib.executor.TOOLS_DIR",
                       os.path.join(root, ".tools")), \
                 patch("workflow_lib.executor.get_project_context",
                       return_value="test context"), \
                 patch("workflow_lib.executor.get_gitlab_remote_url",
                       return_value="https://example.com/repo.git"), \
                 patch("subprocess.run") as mock_run:

                # subprocess.run: succeed for git commands, succeed for
                # presubmit
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="", stderr=""
                )

                result = merge_task(
                    root, "phase_1/sub/01_a.md", "echo ok", "gemini",
                    max_retries=1, dev_branch="dev"
                )

            # merge_task should have proceeded (not skipped due to shutdown)
            assert result is True, (
                "merge_task should not skip during shutdown"
            )
        finally:
            executor_mod.shutdown_requested = False


class TestDashboardShutdownNotice:
    """Tests verifying the dashboard displays a shutdown notice."""

    def test_dashboard_set_shutting_down_flag(self):
        """Dashboard.set_shutting_down() sets the internal flag."""
        from workflow_lib.dashboard import Dashboard
        dash = Dashboard()
        assert dash._shutting_down is False
        dash.set_shutting_down()
        assert dash._shutting_down is True

    def test_dashboard_render_includes_shutdown_banner(self):
        """When shutting down, the rendered output includes a shutdown notice."""
        from workflow_lib.dashboard import Dashboard
        from rich.text import Text
        from rich.rule import Rule

        dash = Dashboard()
        dash.set_shutting_down()
        group = dash._render()

        # Flatten the group renderables and look for shutdown text
        renderables = list(group.renderables)
        found_shutdown_rule = False
        found_shutdown_text = False
        for r in renderables:
            if isinstance(r, Rule) and "SHUTTING DOWN" in str(r.title):
                found_shutdown_rule = True
            if isinstance(r, Text) and "shutdown in progress" in r.plain.lower():
                found_shutdown_text = True

        assert found_shutdown_rule, "Shutdown rule/banner not found in render"
        assert found_shutdown_text, "Shutdown message text not found in render"

    def test_dashboard_render_no_banner_when_not_shutting_down(self):
        """No shutdown banner when not shutting down."""
        from workflow_lib.dashboard import Dashboard
        from rich.rule import Rule

        dash = Dashboard()
        group = dash._render()
        renderables = list(group.renderables)
        for r in renderables:
            if isinstance(r, Rule) and r.title and "SHUTTING DOWN" in str(r.title):
                pytest.fail("Shutdown banner should not appear when not shutting down")

    def test_null_dashboard_set_shutting_down_logs(self):
        """NullDashboard.set_shutting_down() logs a message."""
        import io
        from workflow_lib.dashboard import NullDashboard

        stream = io.StringIO()
        dash = NullDashboard(stream=stream)
        dash.set_shutting_down()
        output = stream.getvalue()
        assert "shutdown" in output.lower()

    def test_signal_handler_notifies_dashboard(self):
        """The executor signal handler calls dashboard.set_shutting_down()."""
        import workflow_lib.executor as executor_mod
        from workflow_lib.executor import signal_handler

        executor_mod.shutdown_requested = False
        mock_dash = MagicMock()
        executor_mod._active_dashboard = mock_dash

        try:
            signal_handler(2, None)  # SIGINT = 2
            assert executor_mod.shutdown_requested is True
            mock_dash.set_shutting_down.assert_called_once()
        finally:
            executor_mod.shutdown_requested = False
            executor_mod._active_dashboard = None
