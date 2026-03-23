"""
End-to-end tests for the rewritten planning workflow (Phases 1-20).

Tests exercise the full Orchestrator.run() pipeline against a real temp
directory, with AI file-creation simulated by patching ProjectContext.run_gemini
to create expected output files.

Coverage includes:
- Full pipeline happy-path (all 20 phases)
- Skip-when-complete (idempotency)
- Validation failure edge cases
- Phase-specific artifact checks
- JSON schema conformance for all requirement/epic/task outputs
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

from workflow_lib.prompt_registry import PROMPT_PLACEHOLDERS

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

STUB_PROMPT = "Write {target_path} based on {description_ctx}."

EXTRACTED_REQS = {
    "source_document": "1_prd",
    "requirements": [
        {
            "id": "1_PRD-REQ-001",
            "title": "User authentication system",
            "description": "The system must support email-based user authentication with secure sessions",
            "category": "functional",
            "priority": "must",
            "source_section": "## Authentication"
        },
        {
            "id": "1_PRD-REQ-002",
            "title": "User profile management",
            "description": "Users must be able to view and edit their profile information including name and avatar",
            "category": "functional",
            "priority": "should",
            "source_section": "## User Management"
        },
    ]
}

MERGED_REQS = {
    "version": 1,
    "total_count": 2,
    "requirements": [
        {
            "id": "1_PRD-REQ-001",
            "title": "User authentication system",
            "description": "The system must support email-based user authentication with secure sessions",
            "category": "functional",
            "priority": "must",
            "source_documents": ["1_prd"],
            "source_section": "## Authentication"
        },
        {
            "id": "1_PRD-REQ-002",
            "title": "User profile management",
            "description": "Users must be able to view and edit their profile information including name and avatar",
            "category": "functional",
            "priority": "should",
            "source_documents": ["1_prd"],
            "source_section": "## User Management"
        },
    ]
}

DEDUPED_REQS = {
    "version": 1,
    "total_remaining": 2,
    "total_removed": 0,
    "removed_requirements": []
}

ORDERED_REQS = {
    "version": 1,
    "ordering_strategy": "E2E-first topological",
    "requirements": [
        {
            "id": "1_PRD-REQ-001",
            "title": "User authentication system",
            "description": "The system must support email-based user authentication with secure sessions",
            "category": "functional",
            "priority": "must",
            "source_documents": ["1_prd"],
            "order": 1,
            "depends_on_requirements": [],
            "e2e_testable": True
        },
        {
            "id": "1_PRD-REQ-002",
            "title": "User profile management",
            "description": "Users must be able to view and edit their profile information including name and avatar",
            "category": "functional",
            "priority": "should",
            "source_documents": ["1_prd"],
            "order": 2,
            "depends_on_requirements": ["1_PRD-REQ-001"],
            "e2e_testable": True
        },
    ]
}

EPIC_MAPPINGS = {
    "epics": [
        {
            "epic_id": "auth",
            "name": "Authentication",
            "description": "User auth system",
            "phase_number": 1,
            "requirement_ids": ["1_PRD-REQ-001", "1_PRD-REQ-002"],
            "features": [
                {"name": "Basic Login", "requirement_ids": ["1_PRD-REQ-001"]},
                {"name": "Profile", "requirement_ids": ["1_PRD-REQ-002"]},
            ],
            "shared_components": {
                "owns": [
                    {"name": "AuthService", "contract": "dreamer-core::auth::AuthService"}
                ],
                "consumes": []
            }
        }
    ]
}

TASK_SIDECAR = {
    "task_id": "phase_1/auth/01_setup_api.md",
    "phase": "phase_1",
    "type": "red",
    "depends_on": [],
    "feature_gates": ["features/auth_basic_login"],
    "requirement_mappings": ["1_PRD-REQ-001"],
    "epic_id": "auth"
}

GREEN_TASK_SIDECAR = {
    "task_id": "phase_1/auth/02_impl_login.md",
    "phase": "phase_1",
    "type": "green",
    "depends_on": ["phase_1/auth/01_setup_api.md"],
    "feature_gates": ["features/auth_basic_login"],
    "requirement_mappings": ["1_PRD-REQ-001"],
    "epic_id": "auth"
}

DAG = {
    "auth/01_setup_api.md": [],
    "auth/02_impl_login.md": ["auth/01_setup_api.md"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_tools_layout(root: Path) -> Path:
    """Create the minimal .tools/ directory structure inside *root*."""
    tools = root / ".tools"
    (root / "input").mkdir(parents=True)
    (tools / "prompts").mkdir(parents=True)
    (tools / "templates").mkdir(parents=True)
    (tools / "schemas").mkdir(parents=True)

    # project description
    (root / "input" / "project-description.md").write_text(
        "# Test Project\n\nA simple test project for E2E testing.\n"
    )

    # stub prompt files
    for name in PROMPT_PLACEHOLDERS:
        (tools / "prompts" / name).write_text(STUB_PROMPT)

    # Copy real schemas
    real_schemas = Path(__file__).parent.parent / "schemas"
    if real_schemas.exists():
        for schema_file in real_schemas.iterdir():
            if schema_file.suffix == ".json":
                (tools / "schemas" / schema_file.name).write_text(
                    schema_file.read_text()
                )

    # Copy real validate.py
    real_validate = Path(__file__).parent.parent / "validate.py"
    if real_validate.exists():
        (tools / "validate.py").write_text(real_validate.read_text())

    return tools


def _make_agent(side_effects: dict):
    """Return a run_gemini replacement that creates stub files on demand.

    *side_effects* maps keywords (matched against the prompt text) to
    content strings.  When a keyword matches, the content is written
    to the first non-directory allowed_file path.
    """

    def _run_gemini(self, full_prompt, allowed_files=None, **kwargs):
        if allowed_files:
            for f in allowed_files:
                if isinstance(f, str) and not f.endswith(os.sep):
                    os.makedirs(os.path.dirname(os.path.abspath(f)), exist_ok=True)
                    if not os.path.exists(f):
                        content = "# Generated stub\n"
                        for kw, extra in side_effects.items():
                            if kw in full_prompt:
                                content = extra if isinstance(extra, str) else json.dumps(extra, indent=2)
                                break
                        with open(f, "w", encoding="utf-8") as fp:
                            fp.write(content)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _run_gemini


def _standard_patches(tmp_path, tools_dir):
    """Return a combined context manager with all standard patches."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("workflow_lib.constants.TOOLS_DIR", str(tools_dir)))
    stack.enter_context(patch("workflow_lib.constants.ROOT_DIR", str(tmp_path)))
    stack.enter_context(patch("workflow_lib.constants.SCHEMAS_DIR", str(tools_dir / "schemas")))
    stack.enter_context(patch("workflow_lib.context.TOOLS_DIR", str(tools_dir)))
    stack.enter_context(patch("workflow_lib.context.GEN_STATE_FILE", str(tmp_path / ".gen_state.json")))
    stack.enter_context(patch("workflow_lib.context.INPUT_DIR", str(tmp_path / "input")))
    stack.enter_context(patch("workflow_lib.phases.TOOLS_DIR", str(tools_dir)))
    stack.enter_context(patch("workflow_lib.context.ProjectContext.stage_changes"))
    stack.enter_context(patch("builtins.input", return_value="c"))
    return stack


def _pre_populate_plan_artifacts(tmp_path):
    """Create all artifacts the orchestrator expects to find during the pipeline."""
    plan_dir = tmp_path / "docs" / "plan"
    tasks_dir = plan_dir / "tasks"
    phase_dir = tasks_dir / "phase_1" / "auth"
    phase_dir.mkdir(parents=True, exist_ok=True)

    # Spec documents and summaries
    from workflow_lib.constants import DOCS
    summaries_dir = plan_dir / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    for doc in DOCS:
        (plan_dir / "specs" / f"{doc['id']}.md").parent.mkdir(parents=True, exist_ok=True)
        (plan_dir / "specs" / f"{doc['id']}.md").write_text(f"# {doc['name']}\nStub.\n")
        (summaries_dir / f"{doc['id']}.md").write_text(f"# Summary: {doc['name']}\nKey decisions.\n")

    # Reviews
    (plan_dir / "conflict_resolution.md").write_text("# Conflict Resolution\nNo conflicts.\n")
    (plan_dir / "adversarial_review.md").write_text("# Adversarial Review\nLooks good.\n")

    # Per-doc requirement extractions
    req_dir = plan_dir / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)
    for doc in DOCS:
        extracted = dict(EXTRACTED_REQS, source_document=doc["id"])
        prefix = doc["id"].upper().replace(" ", "_")
        for i, req in enumerate(extracted["requirements"]):
            extracted["requirements"][i] = dict(req, id=f"{prefix}-REQ-{i+1:03d}")
        (req_dir / f"{doc['id']}.json").write_text(json.dumps(extracted, indent=2))

    # Merged requirements
    (plan_dir / "requirements.json").write_text(json.dumps(MERGED_REQS, indent=2))

    # Deduped requirements
    (plan_dir / "requirements_deduped.json").write_text(json.dumps(DEDUPED_REQS, indent=2))

    # Ordered requirements
    (plan_dir / "requirements_ordered.json").write_text(json.dumps(ORDERED_REQS, indent=2))

    # Epic mappings
    (plan_dir / "epic_mappings.json").write_text(json.dumps(EPIC_MAPPINGS, indent=2))

    # E2E interfaces and feature gates
    (plan_dir / "e2e_interfaces.md").write_text("# E2E Interfaces\n## Phase 1\n```\ninterface AuthAPI {}\n```\n")
    (plan_dir / "feature_gates.md").write_text("# Feature Gates\n- `features/auth_basic_login`\n")

    # Task files with sidecars
    (phase_dir / "01_setup_api.md").write_text("# Task: Setup API\nImplement auth API stubs.\n")
    (phase_dir / "01_setup_api.json").write_text(json.dumps(TASK_SIDECAR, indent=2))
    (phase_dir / "02_impl_login.md").write_text("# Task: Implement Login\nReal auth implementation.\n")
    (phase_dir / "02_impl_login.json").write_text(json.dumps(GREEN_TASK_SIDECAR, indent=2))

    # Pre-init task
    pre_init = tasks_dir / "phase_0"
    pre_init.mkdir(parents=True, exist_ok=True)
    (pre_init / "00_pre_init.md").write_text("# Pre-Init Task\nSetup project.\n")
    pre_init_sidecar = {
        "task_id": "phase_0/00_pre_init.md",
        "phase": "phase_0",
        "type": "red",
        "depends_on": [],
        "feature_gates": [],
        "requirement_mappings": [],
        "epic_id": "bootstrap"
    }
    (pre_init / "00_pre_init.json").write_text(json.dumps(pre_init_sidecar, indent=2))

    # Cross-phase review summary
    (tasks_dir / "cross_phase_review_summary_pass_1.md").write_text("# Review\nAll good.\n")

    # DAG files
    (tasks_dir / "phase_1" / "dag.json").write_text(json.dumps(DAG, indent=2))
    (tasks_dir / "phase_0" / "dag.json").write_text(json.dumps({"00_pre_init.md": []}, indent=2))

    return plan_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlanningE2EHappyPath:
    """Full Orchestrator.run() with mocked AI file creation."""

    def test_orchestrator_runs_all_phases(self, tmp_path):
        """Orchestrator.run() completes without error and sets all state flags."""
        tools_dir = _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        agent = _make_agent({
            # Phase 7: Extract requirements → JSON
            "Extract": json.dumps(EXTRACTED_REQS),
            # Phase 9: Merge requirements
            "Merge": json.dumps(MERGED_REQS),
            # Phase 10: Deduplicate
            "Dedup": json.dumps(DEDUPED_REQS),
            # Phase 12: Order requirements
            "Order": json.dumps(ORDERED_REQS),
            # Phase 13: Epic mappings
            "epic": json.dumps(EPIC_MAPPINGS),
            # Phase 16: Red/Green tasks produce sidecars
            "Red": json.dumps(TASK_SIDECAR),
        })

        with _standard_patches(tmp_path, tools_dir):
            with patch("workflow_lib.context.ProjectContext.run_gemini", agent), \
                 patch("workflow_lib.context.ProjectContext.run_ai", agent), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):

                from workflow_lib.context import ProjectContext
                from workflow_lib.orchestrator import Orchestrator

                ctx = ProjectContext(str(tmp_path))

                # Pre-populate artifacts that the mock agent alone can't create
                # (phases that need specific multi-file setups)
                _pre_populate_plan_artifacts(tmp_path)

                orc = Orchestrator(ctx)
                orc.run()

            # Verify all phase flags are set
            assert ctx.state.get("final_review_completed") is True
            assert ctx.state.get("conflict_resolution_completed") is True
            assert ctx.state.get("adversarial_review_completed") is True
            assert ctx.state.get("requirements_extracted") is True
            assert ctx.state.get("meta_requirements_filtered") is True
            assert ctx.state.get("requirements_merged") is True
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


class TestPlanningIdempotency:
    """Verify phases are skipped when already completed."""

    def test_skips_all_when_complete(self, tmp_path):
        """When state shows all phases done, no AI calls are made."""
        tools_dir = _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        _pre_populate_plan_artifacts(tmp_path)

        run_gemini_calls = []

        def _spy(self, *args, **kwargs):
            run_gemini_calls.append(args)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with _standard_patches(tmp_path, tools_dir):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.orchestrator import Orchestrator
                from workflow_lib.constants import DOCS

                ctx = ProjectContext(str(tmp_path))

                # Mark every phase as done
                for doc in DOCS:
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

                with patch("workflow_lib.context.ProjectContext.run_gemini", _spy), \
                     patch("workflow_lib.context.ProjectContext.run_ai", _spy):
                    orc = Orchestrator(ctx)
                    orc.run()

            assert run_gemini_calls == [], (
                f"Expected no AI calls, got {len(run_gemini_calls)}"
            )


class TestStaleStateReExtraction:
    """Verify phases re-run when state is stale (files missing on disk)."""

    def test_phase7_re_extracts_when_json_missing(self, tmp_path):
        """Phase 7 re-extracts when state says extracted but .json file is missing.

        Regression test: old pipeline produced .md files, new pipeline needs
        .json. Stale state with doc IDs in extracted_requirements but no .json
        on disk must trigger re-extraction, not skip.
        """
        tools_dir = _create_tools_layout(tmp_path)

        # Track whether run_gemini was called (extraction happened)
        extraction_ran = []

        def tracking_agent(self, full_prompt, allowed_files=None, **kwargs):
            extraction_ran.append(True)
            if allowed_files:
                for f in allowed_files:
                    if isinstance(f, str) and not f.endswith(os.sep):
                        os.makedirs(os.path.dirname(os.path.abspath(f)), exist_ok=True)
                        if f.endswith(".json") and not os.path.exists(f):
                            with open(f, "w") as fp:
                                fp.write(json.dumps(EXTRACTED_REQS, indent=2))
                        elif not os.path.exists(f):
                            with open(f, "w") as fp:
                                fp.write("# stub\n")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with _standard_patches(tmp_path, tools_dir):
            with patch("workflow_lib.context.ProjectContext.run_gemini", tracking_agent), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.phases import Phase7ExtractRequirements
                from workflow_lib.constants import DOCS

                ctx = ProjectContext(str(tmp_path))

                # Create the spec file so Phase7 doesn't skip due to missing source
                doc = DOCS[0]  # 1_prd
                spec_path = ctx.get_document_path(doc)
                os.makedirs(os.path.dirname(spec_path), exist_ok=True)
                with open(spec_path, "w") as f:
                    f.write("# PRD\nSome content.\n")

                # Simulate stale state: doc ID in extracted list, but NO .json file
                ctx.state["extracted_requirements"] = [doc["id"]]
                json_path = os.path.join(ctx.requirements_dir, f"{doc['id']}.json")
                assert not os.path.exists(json_path), "JSON should not exist yet"

                phase = Phase7ExtractRequirements(doc)
                phase.execute(ctx)

        # Extraction MUST have run despite stale state
        assert len(extraction_ran) > 0, (
            "Phase 7 skipped extraction due to stale state — "
            "the .json file was missing but state said 'extracted'"
        )
        assert os.path.exists(json_path), "Phase 7 should have created the .json file"

    def test_phase7_skips_when_json_exists(self, tmp_path):
        """Phase 7 correctly skips when state says extracted AND .json exists."""
        tools_dir = _create_tools_layout(tmp_path)

        extraction_ran = []

        def tracking_agent(self, full_prompt, allowed_files=None, **kwargs):
            extraction_ran.append(True)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with _standard_patches(tmp_path, tools_dir):
            with patch("workflow_lib.context.ProjectContext.run_gemini", tracking_agent), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.phases import Phase7ExtractRequirements
                from workflow_lib.constants import DOCS

                ctx = ProjectContext(str(tmp_path))

                doc = DOCS[0]
                # State says extracted AND .json exists → should skip
                ctx.state["extracted_requirements"] = [doc["id"]]
                json_path = os.path.join(ctx.requirements_dir, f"{doc['id']}.json")
                with open(json_path, "w") as f:
                    f.write(json.dumps(EXTRACTED_REQS, indent=2))

                phase = Phase7ExtractRequirements(doc)
                phase.execute(ctx)

        assert len(extraction_ran) == 0, (
            "Phase 7 should have skipped — state and .json file both present"
        )

    def test_phase8_fails_when_json_missing(self, tmp_path):
        """Phase 8 exits with error when the extracted .json file doesn't exist."""
        tools_dir = _create_tools_layout(tmp_path)

        with _standard_patches(tmp_path, tools_dir):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.phases import Phase8FilterMetaRequirements
                from workflow_lib.constants import DOCS

                ctx = ProjectContext(str(tmp_path))
                doc = DOCS[0]

                # No .json file exists — Phase 8 should fail, not silently skip
                phase = Phase8FilterMetaRequirements(doc)
                with pytest.raises(SystemExit) as exc_info:
                    phase.execute(ctx)
                assert exc_info.value.code == 1


class TestParallelAgentPool:
    """Verify parallel phases use the agent pool and dashboard correctly."""

    def test_parallel_phases_acquire_from_pool_and_show_in_dashboard(self, tmp_path):
        """Phase 2 (flesh out) runs in parallel, acquires agents, updates dashboard."""
        tools_dir = _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        from workflow_lib.agent_pool import AgentConfig, AgentPoolManager
        from workflow_lib.constants import DOCS

        # Create a pool with 2 agents
        pool = AgentPoolManager([
            AgentConfig(name="agent-alpha", backend="gemini", user="",
                        parallel=2, priority=1, quota_time=60),
            AgentConfig(name="agent-beta", backend="gemini", user="",
                        parallel=2, priority=2, quota_time=60),
        ])

        import threading

        # Track which agents are acquired and dashboard calls
        acquired_agents = []  # (thread_id, agent_name)
        lock = threading.Lock()

        original_acquire = pool.acquire
        def spy_acquire(*args, **kwargs):
            cfg = original_acquire(*args, **kwargs)
            if cfg:
                with lock:
                    acquired_agents.append((threading.current_thread().ident, cfg.name))
            return cfg

        pool.acquire = spy_acquire

        # Track dashboard set_agent calls
        real_set_agent_calls = []
        def capture_set_agent(task_id, stage, status, last_line="", agent_name=""):
            with lock:
                real_set_agent_calls.append({
                    "task_id": task_id,
                    "stage": stage,
                    "status": status,
                    "agent_name": agent_name,
                })

        mock_dashboard = MagicMock()
        mock_dashboard.set_agent = capture_set_agent
        mock_dashboard.log = MagicMock()
        mock_dashboard.update_last_line = MagicMock()
        mock_dashboard.prompt_input = MagicMock(return_value="c")

        # Mock make_runner to return a mock runner that tracks calls
        runners_created = []
        def mock_make_runner(backend, model=None, **kwargs):
            mock_runner = MagicMock()
            mock_runner.run = MagicMock(return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""))
            with lock:
                runners_created.append((threading.current_thread().ident, backend, model))
            return mock_runner

        agent = _make_agent({})

        with _standard_patches(tmp_path, tools_dir):
            with patch("workflow_lib.context.ProjectContext.run_gemini", agent), \
                 patch("workflow_lib.orchestrator.make_runner", mock_make_runner), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.orchestrator import Orchestrator

                ctx = ProjectContext(str(tmp_path), jobs=3, dashboard=mock_dashboard)

                # Pre-populate all Phase 1 outputs so Phase 2 has docs to flesh out
                for doc in DOCS:
                    ctx.state.setdefault("generated", []).append(doc["id"])
                ctx.save_state()
                _pre_populate_plan_artifacts(tmp_path)

                orc = Orchestrator(ctx, dashboard=mock_dashboard, agent_pool=pool)
                from workflow_lib.phases import Phase2FleshOutDoc
                phases = [Phase2FleshOutDoc(doc) for doc in DOCS]
                orc._run_parallel_phases(phases, "Phase 2: Flesh Out")

        # --- Assertions ---

        # 1. Agents were acquired from the pool (one per phase)
        assert len(acquired_agents) == len(DOCS), (
            f"Expected {len(DOCS)} pool.acquire calls, got {len(acquired_agents)}"
        )

        # 2. Multiple threads were used
        unique_threads = {t for t, _ in acquired_agents}
        assert len(unique_threads) > 1, (
            f"Expected multiple threads, got {len(unique_threads)}"
        )

        # 3. make_runner was called for each phase (one runner per agent)
        assert len(runners_created) == len(DOCS), (
            f"Expected {len(DOCS)} make_runner calls, got {len(runners_created)}"
        )

        # 4. Dashboard set_agent was called with agent names from the pool
        running_calls = [c for c in real_set_agent_calls if c["status"] == "running"]
        done_calls = [c for c in real_set_agent_calls if c["status"] == "done"]

        assert len(running_calls) > 0, "No 'running' dashboard calls were made"
        assert len(done_calls) > 0, "No 'done' dashboard calls were made"

        pool_names = {"agent-alpha", "agent-beta"}
        for call in running_calls:
            assert call["agent_name"] in pool_names, (
                f"Dashboard call had agent_name={call['agent_name']!r}, "
                f"expected one of {pool_names}"
            )

        # 5. Dashboard task_ids should be plan/{phase_display_name}
        for call in running_calls:
            assert call["task_id"].startswith("plan/"), (
                f"Expected task_id starting with 'plan/', got {call['task_id']!r}"
            )

    def test_parallel_phases_without_pool_still_parallelizes(self, tmp_path):
        """With --jobs > 1 but no pool, phases still run in parallel with default runner."""
        tools_dir = _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        import threading
        threads_used = set()
        lock = threading.Lock()

        agent = _make_agent({})
        original_agent = agent

        def tracking_agent(self, full_prompt, allowed_files=None, **kwargs):
            with lock:
                threads_used.add(threading.current_thread().ident)
            return original_agent(self, full_prompt, allowed_files=allowed_files, **kwargs)

        with _standard_patches(tmp_path, tools_dir):
            with patch("workflow_lib.context.ProjectContext.run_gemini", tracking_agent), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.orchestrator import Orchestrator
                from workflow_lib.constants import DOCS

                ctx = ProjectContext(str(tmp_path), jobs=3)

                # Pre-populate Phase 1 outputs
                for doc in DOCS:
                    ctx.state.setdefault("generated", []).append(doc["id"])
                ctx.save_state()
                _pre_populate_plan_artifacts(tmp_path)

                orc = Orchestrator(ctx, agent_pool=None)  # No pool
                from workflow_lib.phases import Phase2FleshOutDoc
                phases = [Phase2FleshOutDoc(doc) for doc in DOCS]
                orc._run_parallel_phases(phases, "Phase 2: Flesh Out")

        assert len(threads_used) > 1, (
            f"Expected parallel execution across threads, only got {len(threads_used)} thread(s)"
        )

    def test_parallel_phases_sequential_with_jobs_1(self, tmp_path):
        """With --jobs 1, phases run sequentially even with a pool configured."""
        tools_dir = _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        from workflow_lib.agent_pool import AgentConfig, AgentPoolManager

        pool = AgentPoolManager([
            AgentConfig(name="agent-alpha", backend="gemini", user="",
                        parallel=2, priority=1, quota_time=60),
        ])

        acquire_calls = []
        original_acquire = pool.acquire
        def spy_acquire(*args, **kwargs):
            acquire_calls.append(1)
            return original_acquire(*args, **kwargs)
        pool.acquire = spy_acquire

        agent = _make_agent({})

        with _standard_patches(tmp_path, tools_dir):
            with patch("workflow_lib.context.ProjectContext.run_gemini", agent), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.orchestrator import Orchestrator
                from workflow_lib.constants import DOCS

                ctx = ProjectContext(str(tmp_path), jobs=1)  # jobs=1

                for doc in DOCS:
                    ctx.state.setdefault("generated", []).append(doc["id"])
                ctx.save_state()
                _pre_populate_plan_artifacts(tmp_path)

                orc = Orchestrator(ctx, agent_pool=pool)
                from workflow_lib.phases import Phase2FleshOutDoc
                phases = [Phase2FleshOutDoc(doc) for doc in DOCS]
                orc._run_parallel_phases(phases, "Phase 2: Flesh Out")

        # With jobs=1, pool.acquire should never be called (sequential path)
        assert len(acquire_calls) == 0, (
            f"Expected no pool.acquire calls with jobs=1, got {len(acquire_calls)}"
        )

    def test_pool_acquires_agents_with_specific_steps(self, tmp_path):
        """Agents with steps=["review"] or steps=["develop"] are still acquired for planning.

        This is the regression test for the bug where acquire(step="all")
        would skip agents that didn't have "all" in their steps list,
        causing the pool to block indefinitely.
        """
        tools_dir = _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        from workflow_lib.agent_pool import AgentConfig, AgentPoolManager
        from workflow_lib.constants import DOCS

        # Create agents with SPECIFIC steps — no "all" anywhere.
        # This matches real-world configs like steps=["review"].
        pool = AgentPoolManager([
            AgentConfig(name="review-only", backend="claude", user="",
                        parallel=2, priority=1, quota_time=60,
                        steps=["review"]),
            AgentConfig(name="develop-only", backend="gemini", user="",
                        parallel=2, priority=2, quota_time=60,
                        steps=["develop"]),
        ])

        import threading
        lock = threading.Lock()
        acquired_agents = []

        original_acquire = pool.acquire
        def spy_acquire(*args, **kwargs):
            cfg = original_acquire(*args, **kwargs)
            if cfg:
                with lock:
                    acquired_agents.append(cfg.name)
            return cfg
        pool.acquire = spy_acquire

        agent = _make_agent({})
        mock_make_runner = MagicMock(return_value=MagicMock(
            run=MagicMock(return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""))))

        with _standard_patches(tmp_path, tools_dir):
            with patch("workflow_lib.context.ProjectContext.run_gemini", agent), \
                 patch("workflow_lib.orchestrator.make_runner", mock_make_runner), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.orchestrator import Orchestrator

                ctx = ProjectContext(str(tmp_path), jobs=3)
                for doc in DOCS:
                    ctx.state.setdefault("generated", []).append(doc["id"])
                ctx.save_state()
                _pre_populate_plan_artifacts(tmp_path)

                orc = Orchestrator(ctx, agent_pool=pool)
                from workflow_lib.phases import Phase2FleshOutDoc
                # Use just 2 phases to keep the test fast
                phases = [Phase2FleshOutDoc(DOCS[0]), Phase2FleshOutDoc(DOCS[1])]
                orc._run_parallel_phases(phases, "Phase 2: Flesh Out")

        # Both phases must have acquired an agent — if the step filter
        # blocked them, acquired_agents would be empty and acquire()
        # would have timed out (causing an error).
        assert len(acquired_agents) == 2, (
            f"Expected 2 acquire calls, got {len(acquired_agents)}. "
            f"Agents with specific steps were not matched for planning."
        )
        # All acquired agents must come from our pool
        valid_names = {"review-only", "develop-only"}
        for name in acquired_agents:
            assert name in valid_names, (
                f"Unexpected agent {name!r}, expected one of {valid_names}"
            )

    def test_pool_acquire_step_none_matches_any(self, tmp_path):
        """acquire(step=None) matches agents regardless of their steps config.

        Direct unit test for the _pick fix — verifies that step=None
        bypasses the step filter entirely.
        """
        from workflow_lib.agent_pool import AgentConfig, AgentPoolManager

        pool = AgentPoolManager([
            AgentConfig(name="review-only", backend="claude", user="",
                        parallel=1, priority=1, quota_time=60,
                        steps=["review"]),
        ])

        # step=None should match the review-only agent
        agent = pool.acquire(timeout=1.0, step=None)
        assert agent is not None, (
            "acquire(step=None) failed to match agent with steps=['review']"
        )
        assert agent.name == "review-only"
        pool.release(agent)

        # step="all" should NOT match (agent doesn't have "all" in steps)
        agent2 = pool.acquire(timeout=0.1, step="all")
        assert agent2 is None, (
            "acquire(step='all') should not match agent with steps=['review']"
        )

        # step="review" should match
        agent3 = pool.acquire(timeout=1.0, step="review")
        assert agent3 is not None
        assert agent3.name == "review-only"
        pool.release(agent3)

        # step="develop" should NOT match
        agent4 = pool.acquire(timeout=0.1, step="develop")
        assert agent4 is None, (
            "acquire(step='develop') should not match agent with steps=['review']"
        )

    def test_thread_local_runner_not_shared(self, tmp_path):
        """Each parallel thread gets its own runner via thread-local, not by mutating ctx.runner."""
        tools_dir = _create_tools_layout(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=False,
                       capture_output=True)

        from workflow_lib.agent_pool import AgentConfig, AgentPoolManager

        pool = AgentPoolManager([
            AgentConfig(name="agent-alpha", backend="gemini", user="",
                        parallel=5, priority=1, quota_time=60),
        ])

        import threading

        # Track that make_runner is called (meaning new runners are created per-thread)
        lock = threading.Lock()
        runners_created = []

        def mock_make_runner(backend, model=None, **kwargs):
            mock_runner = MagicMock()
            mock_runner.run = MagicMock(return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""))
            with lock:
                runners_created.append(id(mock_runner))
            return mock_runner

        agent = _make_agent({})

        with _standard_patches(tmp_path, tools_dir):
            with patch("workflow_lib.context.ProjectContext.run_gemini", agent), \
                 patch("workflow_lib.orchestrator.make_runner", mock_make_runner), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                from workflow_lib.orchestrator import Orchestrator
                from workflow_lib.constants import DOCS

                ctx = ProjectContext(str(tmp_path), jobs=4)
                original_runner_id = id(ctx.runner)

                for doc in DOCS:
                    ctx.state.setdefault("generated", []).append(doc["id"])
                ctx.save_state()
                _pre_populate_plan_artifacts(tmp_path)

                orc = Orchestrator(ctx, agent_pool=pool)
                from workflow_lib.phases import Phase2FleshOutDoc
                phases = [Phase2FleshOutDoc(doc) for doc in DOCS]
                orc._run_parallel_phases(phases, "Phase 2: Flesh Out")

        # ctx.runner should not have been permanently changed
        assert id(ctx.runner) == original_runner_id, (
            "ctx.runner was permanently mutated by parallel execution"
        )

        # Each phase should have gotten its own runner (not sharing the default)
        assert len(runners_created) == len(DOCS), (
            f"Expected {len(DOCS)} runners created, got {len(runners_created)}"
        )
        # All runner IDs should be unique (different objects)
        assert len(set(runners_created)) == len(runners_created), (
            "Expected unique runner per thread, but some runners were reused"
        )


class TestStatePrePopulation:
    """Verify .gen_state.json is pre-populated with all phase keys."""

    def test_state_file_created_on_init(self, tmp_path):
        """ProjectContext writes .gen_state.json with all keys on first init."""
        tools_dir = _create_tools_layout(tmp_path)

        state_file = tmp_path / ".gen_state.json"
        assert not state_file.exists()

        with _standard_patches(tmp_path, tools_dir):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                from workflow_lib.context import ProjectContext
                ctx = ProjectContext(str(tmp_path))

        assert state_file.exists()
        state = json.loads(state_file.read_text())

        # All phase flags should be present and False
        expected_flags = [
            "final_review_completed", "conflict_resolution_completed",
            "adversarial_review_completed", "requirements_extracted",
            "meta_requirements_filtered", "requirements_merged",
            "requirements_deduplicated", "scope_gate_passed",
            "requirements_ordered", "epics_completed",
            "e2e_interfaces_completed", "feature_gates_completed",
            "tasks_completed", "tasks_reviewed", "cross_phase_reviewed",
            "pre_init_task_completed", "dag_completed",
        ]
        for flag in expected_flags:
            assert flag in state, f"Missing state key: {flag}"
            assert state[flag] is False, f"State key {flag} should be False, got {state[flag]}"


class TestValidation:
    """Test validate.py catches schema violations and invariant breaks."""

    def _run_validate(self, tmp_path, phase=None, all_flag=False):
        """Run validate.py and return (returncode, stdout)."""
        validate_py = Path(__file__).parent.parent / "validate.py"
        schemas_dir = Path(__file__).parent.parent / "schemas"
        cmd = [sys.executable, str(validate_py)]
        if phase is not None:
            cmd += ["--phase", str(phase)]
        if all_flag:
            cmd += ["--all"]
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "VALIDATE_ROOT_DIR": str(tmp_path),
            "VALIDATE_SCHEMAS_DIR": str(schemas_dir),
        }
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path), env=env)
        return result.returncode, result.stdout

    def test_extracted_requirements_valid(self, tmp_path):
        """Valid extracted requirements JSON passes validation."""
        plan_dir = tmp_path / "docs" / "plan"
        req_dir = plan_dir / "requirements"
        req_dir.mkdir(parents=True)

        (req_dir / "1_prd.json").write_text(json.dumps(EXTRACTED_REQS, indent=2))

        state = {"requirements_extracted": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=7)
        assert rc == 0, f"Validation failed: {out}"

    def test_extracted_requirements_invalid_category(self, tmp_path):
        """Invalid category enum value is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        req_dir = plan_dir / "requirements"
        req_dir.mkdir(parents=True)

        bad_reqs = dict(EXTRACTED_REQS)
        bad_reqs["requirements"] = [
            {
                "id": "1_PRD-REQ-001",
                "title": "Bad requirement",
                "description": "This has an invalid category that should fail schema validation",
                "category": "INVALID_CATEGORY",
                "priority": "must",
                "source_section": "## Test"
            }
        ]
        (req_dir / "1_prd.json").write_text(json.dumps(bad_reqs, indent=2))

        state = {"requirements_extracted": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=7)
        assert rc != 0, f"Expected validation failure for bad category"
        assert "INVALID_CATEGORY" in out

    def test_merged_requirements_missing_field(self, tmp_path):
        """Missing required field in merged requirements is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        bad_merged = {"version": 1, "requirements": []}  # missing total_count
        (plan_dir / "requirements.json").write_text(json.dumps(bad_merged))

        state = {"requirements_merged": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=9)
        assert rc != 0, f"Expected validation failure for missing total_count"
        assert "total_count" in out

    def test_dedup_invariant_violation(self, tmp_path):
        """Dedup total_remaining mismatch is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        # requirements.json has 2 requirements
        (plan_dir / "requirements.json").write_text(json.dumps(MERGED_REQS, indent=2))

        # But dedup claims 5 remaining
        bad_dedup = dict(DEDUPED_REQS, total_remaining=5)
        (plan_dir / "requirements_deduped.json").write_text(json.dumps(bad_dedup))

        state = {"requirements_deduplicated": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=10)
        assert rc != 0, f"Expected validation failure for count mismatch"
        assert "total_remaining" in out

    def test_ordered_requirements_missing_ids(self, tmp_path):
        """Ordered requirements missing IDs from merged is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        (plan_dir / "requirements.json").write_text(json.dumps(MERGED_REQS, indent=2))

        # Ordered has only 1 of the 2 requirements
        partial_ordered = dict(ORDERED_REQS)
        partial_ordered["requirements"] = [ORDERED_REQS["requirements"][0]]
        (plan_dir / "requirements_ordered.json").write_text(json.dumps(partial_ordered))

        state = {"requirements_ordered": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=12)
        assert rc != 0, f"Expected validation failure for missing requirements"
        assert "missing" in out.lower()

    def test_epic_mappings_unmapped_requirement(self, tmp_path):
        """Requirements not mapped to any epic are caught."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        (plan_dir / "requirements_ordered.json").write_text(json.dumps(ORDERED_REQS, indent=2))

        # Epic only maps one requirement
        partial_epic = {"epics": [{
            "epic_id": "auth",
            "name": "Auth",
            "phase_number": 1,
            "requirement_ids": ["1_PRD-REQ-001"],  # missing REQ-002
            "features": [{"name": "Login", "requirement_ids": ["1_PRD-REQ-001"]}],
            "shared_components": {"owns": [], "consumes": []}
        }]}
        (plan_dir / "epic_mappings.json").write_text(json.dumps(partial_epic))

        state = {"epics_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=13)
        assert rc != 0, f"Expected validation failure for unmapped requirement"
        assert "not mapped" in out.lower() or "unmapped" in out.lower()

    def test_task_sidecar_invalid_type(self, tmp_path):
        """Invalid task type (not red/green) is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        bad_sidecar = dict(TASK_SIDECAR, type="blue")  # invalid type
        (task_dir / "01_setup_api.json").write_text(json.dumps(bad_sidecar))
        (task_dir / "01_setup_api.md").write_text("# Task\n")

        state = {"tasks_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=16)
        assert rc != 0, f"Expected validation failure for invalid type"
        assert "blue" in out

    def test_dag_cycle_detection(self, tmp_path):
        """DAG with a cycle is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        phase_dir = plan_dir / "tasks" / "phase_1" / "auth"
        phase_dir.mkdir(parents=True)

        # Create task files
        (phase_dir / "01_a.md").write_text("# Task A\n")
        (phase_dir / "02_b.md").write_text("# Task B\n")

        # Cyclic DAG: A -> B -> A
        cyclic_dag = {
            "auth/01_a.md": ["auth/02_b.md"],
            "auth/02_b.md": ["auth/01_a.md"],
        }
        (plan_dir / "tasks" / "phase_1" / "dag.json").write_text(json.dumps(cyclic_dag))

        state = {"dag_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=20)
        assert rc != 0, f"Expected validation failure for cycle"
        assert "cycle" in out.lower()

    def test_cross_phase_duplicate_task_ids(self, tmp_path):
        """Duplicate task_id across phases is caught."""
        plan_dir = tmp_path / "docs" / "plan"

        for phase in ["phase_1", "phase_2"]:
            task_dir = plan_dir / "tasks" / phase / "auth"
            task_dir.mkdir(parents=True)

            sidecar = dict(TASK_SIDECAR, task_id="DUPLICATE_ID", phase=phase)
            (task_dir / "01_task.json").write_text(json.dumps(sidecar))
            (task_dir / "01_task.md").write_text("# Task\n")

        state = {"cross_phase_reviewed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=18)
        assert rc != 0, f"Expected validation failure for duplicate task_id"
        assert "Duplicate" in out or "duplicate" in out

    def test_feature_gate_no_producer(self, tmp_path):
        """Feature gate consumed by red task but no green task produces it."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        # Red task consumes a gate
        red = dict(TASK_SIDECAR, type="red", feature_gates=["features/nonexistent_gate"])
        (task_dir / "01_red.json").write_text(json.dumps(red))
        (task_dir / "01_red.md").write_text("# Red Task\n")

        state = {"cross_phase_reviewed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=18)
        assert rc != 0, f"Expected validation failure for missing gate producer"
        assert "nonexistent_gate" in out

    # --- Shared component ownership tests (Phase 13) ---

    def test_epic_shared_components_valid(self, tmp_path):
        """Epic with valid shared_components passes validation."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        (plan_dir / "requirements_ordered.json").write_text(json.dumps(ORDERED_REQS, indent=2))
        (plan_dir / "epic_mappings.json").write_text(json.dumps(EPIC_MAPPINGS, indent=2))

        state = {"epics_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=13)
        assert rc == 0, f"Validation failed: {out}"

    def test_epic_shared_components_missing(self, tmp_path):
        """Epic missing shared_components field is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        (plan_dir / "requirements_ordered.json").write_text(json.dumps(ORDERED_REQS, indent=2))

        epic_no_sc = {"epics": [{
            "epic_id": "auth",
            "name": "Auth",
            "phase_number": 1,
            "requirement_ids": ["1_PRD-REQ-001", "1_PRD-REQ-002"],
            "features": [{"name": "Login", "requirement_ids": ["1_PRD-REQ-001"]}]
        }]}
        (plan_dir / "epic_mappings.json").write_text(json.dumps(epic_no_sc))

        state = {"epics_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=13)
        assert rc != 0, f"Expected failure for missing shared_components"
        assert "shared_components" in out

    def test_epic_shared_components_dual_ownership(self, tmp_path):
        """Component owned by two epics is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        (plan_dir / "requirements_ordered.json").write_text(json.dumps(ORDERED_REQS, indent=2))

        dual_owner = {"epics": [
            {
                "epic_id": "EPIC-000",
                "name": "Bootstrap",
                "phase_number": 0,
                "requirement_ids": ["1_PRD-REQ-001"],
                "features": [{"name": "Setup", "requirement_ids": ["1_PRD-REQ-001"]}],
                "shared_components": {
                    "owns": [{"name": "CommandBus", "contract": "core::CommandBus"}],
                    "consumes": []
                }
            },
            {
                "epic_id": "EPIC-001",
                "name": "Core",
                "phase_number": 1,
                "requirement_ids": ["1_PRD-REQ-002"],
                "features": [{"name": "Core", "requirement_ids": ["1_PRD-REQ-002"]}],
                "shared_components": {
                    "owns": [{"name": "CommandBus", "contract": "core::CommandBus"}],
                    "consumes": []
                }
            }
        ]}
        (plan_dir / "epic_mappings.json").write_text(json.dumps(dual_owner))

        state = {"epics_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=13)
        assert rc != 0, f"Expected failure for dual ownership"
        assert "CommandBus" in out
        assert "owned by both" in out

    def test_epic_shared_components_consume_from_later_phase(self, tmp_path):
        """Consuming a component from a later phase is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        (plan_dir / "requirements_ordered.json").write_text(json.dumps(ORDERED_REQS, indent=2))

        bad_consume = {"epics": [
            {
                "epic_id": "EPIC-000",
                "name": "Bootstrap",
                "phase_number": 0,
                "requirement_ids": ["1_PRD-REQ-001"],
                "features": [{"name": "Setup", "requirement_ids": ["1_PRD-REQ-001"]}],
                "shared_components": {
                    "owns": [],
                    "consumes": [{"name": "AuthService", "from_epic": "EPIC-001"}]
                }
            },
            {
                "epic_id": "EPIC-001",
                "name": "Auth",
                "phase_number": 1,
                "requirement_ids": ["1_PRD-REQ-002"],
                "features": [{"name": "Auth", "requirement_ids": ["1_PRD-REQ-002"]}],
                "shared_components": {
                    "owns": [{"name": "AuthService", "contract": "core::AuthService"}],
                    "consumes": []
                }
            }
        ]}
        (plan_dir / "epic_mappings.json").write_text(json.dumps(bad_consume))

        state = {"epics_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=13)
        assert rc != 0, f"Expected failure for consuming from later phase"
        assert "earlier phases" in out

    def test_epic_shared_components_consume_unknown_epic(self, tmp_path):
        """Consuming from a nonexistent epic is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)

        (plan_dir / "requirements_ordered.json").write_text(json.dumps(ORDERED_REQS, indent=2))

        bad_ref = {"epics": [{
            "epic_id": "EPIC-001",
            "name": "Auth",
            "phase_number": 1,
            "requirement_ids": ["1_PRD-REQ-001", "1_PRD-REQ-002"],
            "features": [{"name": "Auth", "requirement_ids": ["1_PRD-REQ-001"]}],
            "shared_components": {
                "owns": [],
                "consumes": [{"name": "CommandBus", "from_epic": "EPIC-NONEXISTENT"}]
            }
        }]}
        (plan_dir / "epic_mappings.json").write_text(json.dumps(bad_ref))

        state = {"epics_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=13)
        assert rc != 0, f"Expected failure for unknown epic reference"
        assert "EPIC-NONEXISTENT" in out

    # --- Requirement traceability tests (Phase 18) ---

    def test_traceability_all_requirements_tested(self, tmp_path):
        """All requirements covered by task requirement_mappings passes."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        (plan_dir / "requirements.json").write_text(json.dumps(MERGED_REQS, indent=2))

        # Task covers both requirements in requirement_mappings
        sidecar = dict(TASK_SIDECAR, requirement_mappings=["1_PRD-REQ-001", "1_PRD-REQ-002"])
        (task_dir / "01_task.json").write_text(json.dumps(sidecar))
        (task_dir / "01_task.md").write_text("# Task\n")

        # Green task produces the gate
        green = dict(GREEN_TASK_SIDECAR, requirement_mappings=[])
        (task_dir / "02_green.json").write_text(json.dumps(green))
        (task_dir / "02_green.md").write_text("# Green Task\n")

        state = {"cross_phase_reviewed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=18)
        assert rc == 0, f"Validation failed: {out}"

    def test_traceability_untested_requirement(self, tmp_path):
        """Requirement not in any task's requirement_mappings is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        (plan_dir / "requirements.json").write_text(json.dumps(MERGED_REQS, indent=2))

        # Task only covers REQ-001, REQ-002 is missing
        sidecar = dict(TASK_SIDECAR, requirement_mappings=["1_PRD-REQ-001"])
        (task_dir / "01_task.json").write_text(json.dumps(sidecar))
        (task_dir / "01_task.md").write_text("# Task\n")

        green = dict(GREEN_TASK_SIDECAR, requirement_mappings=[])
        (task_dir / "02_green.json").write_text(json.dumps(green))
        (task_dir / "02_green.md").write_text("# Green Task\n")

        state = {"cross_phase_reviewed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=18)
        assert rc != 0, f"Expected failure for untested requirement"
        assert "not directly tested" in out
        assert "1_PRD-REQ-002" in out

    def test_traceability_contributes_to_not_counted(self, tmp_path):
        """Requirement only in contributes_to (not requirement_mappings) is flagged."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        (plan_dir / "requirements.json").write_text(json.dumps(MERGED_REQS, indent=2))

        # REQ-001 in requirement_mappings, REQ-002 only in contributes_to
        sidecar = dict(TASK_SIDECAR,
                       requirement_mappings=["1_PRD-REQ-001"],
                       contributes_to=["1_PRD-REQ-002"])
        (task_dir / "01_task.json").write_text(json.dumps(sidecar))
        (task_dir / "01_task.md").write_text("# Task\n")

        green = dict(GREEN_TASK_SIDECAR,
                     requirement_mappings=[],
                     contributes_to=[])
        (task_dir / "02_green.json").write_text(json.dumps(green))
        (task_dir / "02_green.md").write_text("# Green Task\n")

        state = {"cross_phase_reviewed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=18)
        assert rc != 0, f"Expected failure — contributes_to should not count as tested"
        assert "1_PRD-REQ-002" in out

    # --- Task sidecar contributes_to schema tests (Phase 16) ---

    def test_task_sidecar_with_contributes_to_valid(self, tmp_path):
        """Task sidecar with valid contributes_to field passes validation."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        sidecar = dict(TASK_SIDECAR, contributes_to=["1_PRD-REQ-002"])
        (task_dir / "01_task.json").write_text(json.dumps(sidecar))
        (task_dir / "01_task.md").write_text("# Task\n")

        state = {"tasks_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=16)
        assert rc == 0, f"Validation failed: {out}"

    def test_task_sidecar_without_contributes_to_valid(self, tmp_path):
        """Task sidecar without contributes_to still passes (field is optional)."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        sidecar = dict(TASK_SIDECAR)  # No contributes_to
        assert "contributes_to" not in sidecar
        (task_dir / "01_task.json").write_text(json.dumps(sidecar))
        (task_dir / "01_task.md").write_text("# Task\n")

        state = {"tasks_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=16)
        assert rc == 0, f"Validation failed: {out}"

    def test_task_sidecar_contributes_to_invalid_pattern(self, tmp_path):
        """contributes_to with invalid requirement ID pattern is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        sidecar = dict(TASK_SIDECAR, contributes_to=["not-a-valid-id"])
        (task_dir / "01_task.json").write_text(json.dumps(sidecar))
        (task_dir / "01_task.md").write_text("# Task\n")

        state = {"tasks_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=16)
        assert rc != 0, f"Expected failure for invalid contributes_to pattern"
        assert "not-a-valid-id" in out

    # --- Requirement mappings cap tests (Phase 16) ---

    def test_task_requirement_mappings_within_cap(self, tmp_path):
        """Task with 5 or fewer requirement_mappings passes."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        sidecar = dict(TASK_SIDECAR, requirement_mappings=[
            "1_PRD-REQ-001", "1_PRD-REQ-002", "1_PRD-REQ-003",
            "1_PRD-REQ-004", "1_PRD-REQ-005"
        ])
        (task_dir / "01_task.json").write_text(json.dumps(sidecar))
        (task_dir / "01_task.md").write_text("# Task\n")

        state = {"tasks_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=16)
        assert rc == 0, f"Validation failed: {out}"

    def test_task_requirement_mappings_exceeds_cap(self, tmp_path):
        """Task with more than 5 requirement_mappings is caught."""
        plan_dir = tmp_path / "docs" / "plan"
        task_dir = plan_dir / "tasks" / "phase_1" / "auth"
        task_dir.mkdir(parents=True)

        sidecar = dict(TASK_SIDECAR, requirement_mappings=[
            "1_PRD-REQ-001", "1_PRD-REQ-002", "1_PRD-REQ-003",
            "1_PRD-REQ-004", "1_PRD-REQ-005", "1_PRD-REQ-006"
        ])
        (task_dir / "01_task.json").write_text(json.dumps(sidecar))
        (task_dir / "01_task.md").write_text("# Task\n")

        state = {"tasks_completed": True}
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        rc, out = self._run_validate(tmp_path, phase=16)
        assert rc != 0, f"Expected failure for exceeding requirement_mappings cap"
        assert "6 entries" in out
        assert "max 5" in out


class TestValidationNoPhaseRun:
    """Validate that validators only run for completed phases."""

    def test_no_validators_when_nothing_complete(self, tmp_path):
        """No validators run when gen_state has all flags False."""
        state = {
            "requirements_extracted": False,
            "requirements_merged": False,
            "dag_completed": False,
        }
        (tmp_path / ".gen_state.json").write_text(json.dumps(state))

        validate_py = Path(__file__).parent.parent / "validate.py"
        schemas_dir = Path(__file__).parent.parent / "schemas"
        result = subprocess.run(
            [sys.executable, str(validate_py)],
            capture_output=True, text=True, cwd=str(tmp_path),
            env={**os.environ, "VALIDATE_ROOT_DIR": str(tmp_path), "VALIDATE_SCHEMAS_DIR": str(schemas_dir)},
        )
        assert result.returncode == 0
        assert "No validators to run" in result.stdout
