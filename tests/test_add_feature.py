"""End-to-end tests for the ``add-feature`` workflow.

Covers all three stages:

1. **Template** – running without ``--brief`` prints the template.
2. **Discuss** – running with ``--brief`` enters the discussion loop, then
   generates a spec when the user types ``done``.
3. **Execute** – running with ``--spec`` (or continuing from discuss) updates
   requirements, creates tasks, and rebuilds the DAG.

Agent file-creation is simulated by patching ``ProjectContext.run_ai`` to
write stub files in the expected locations.
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.replan import cmd_add_feature, _load_requirements_ctx, _load_phases_ctx
from workflow_lib.state import load_replan_state, save_replan_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FEATURE_BRIEF = textwrap.dedent("""\
    # Feature Brief

    ## Feature Name
    Widget Export

    ## Problem Statement
    Users cannot export widgets to PDF.

    ## Proposed Solution
    Add a PDF export button to the widget detail view.

    ## Requirements
    - Export single widget to PDF
    - Include widget metadata in export

    ## Acceptance Criteria
    - [ ] Clicking export produces a valid PDF

    ## Constraints / Dependencies
    - Must work with existing widget renderer

    ## Scope Boundaries
    - **In scope**: Single widget export
    - **Out of scope**: Bulk export
""")

FEATURE_SPEC = textwrap.dedent("""\
    # Feature Spec: Widget Export

    ## Summary
    Add PDF export capability for individual widgets.

    ## Requirements
    - [REQ_NEW_001]: Export single widget to PDF
    - [REQ_NEW_002]: Include widget metadata in export

    ## Acceptance Criteria
    - [ ] Clicking export produces a valid PDF

    ## Technical Design
    ### Architecture
    Uses existing widget renderer with a PDF output adapter.

    ### Components Affected
    - widget_renderer
    - export_service (new)

    ## Scope
    ### In Scope
    - Single widget export

    ### Out of Scope
    - Bulk export
""")

TASK_CONTENT = textwrap.dedent("""\
    # Task: Implement PDF export (Sub-Epic: widget_export)

    ## Covered Requirements
    - [REQ-042]

    ## Dependencies
    - depends_on: []
    - shared_components: [widget_renderer]

    ## 1. Initial Test Written
    - [ ] Write test for PDF export endpoint

    ## 2. Task Implementation
    - [ ] Implement PDF export service

    ## 3. Code Review
    - [ ] Review implementation

    ## 4. Run Automated Tests to Verify
    - [ ] Run test suite

    ## 5. Update Documentation
    - [ ] Update API docs

    ## 6. Automated Verification
    - [ ] Run linter
""")


def _setup_project(tmp_path: Path) -> Path:
    """Create a minimal project layout in *tmp_path* and return it."""
    tools = tmp_path / ".tools"
    (tools / "prompts").mkdir(parents=True)
    (tmp_path / "input").mkdir(parents=True)

    # Project description
    (tmp_path / "input" / "project-description.md").write_text(
        "# Test Project\nA simple project.\n"
    )

    # Copy real prompt templates for add-feature
    real_prompts = Path(__file__).parent.parent / "prompts"
    for name in [
        "feature_brief_template.md",
        "feature_discuss.md",
        "feature_spec.md",
        "feature_execute.md",
    ]:
        src = real_prompts / name
        if src.exists():
            (tools / "prompts" / name).write_text(src.read_text())
        else:
            (tools / "prompts" / name).write_text(f"# stub for {name}\n{{description_ctx}}")

    # Requirements
    (tmp_path / "requirements.md").write_text(
        "# Requirements\n## Active\n### **[REQ-001]** Core feature\n"
    )

    # Phases
    phases_dir = tmp_path / "docs" / "plan" / "phases"
    phases_dir.mkdir(parents=True, exist_ok=True)
    (phases_dir / "phase_1.md").write_text("# Phase 1: Core\n## Requirements Covered\n- REQ-001\n")
    (phases_dir / "phase_2.md").write_text("# Phase 2: Extensions\n## Requirements Covered\n")

    # Tasks dir
    tasks_dir = tmp_path / "docs" / "plan" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Shared components
    (tmp_path / "docs" / "plan" / "shared_components.md").write_text(
        "# Shared Components\n- widget_renderer\n"
    )

    return tmp_path


def _make_args(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace with add-feature defaults."""
    defaults = {
        "brief": None,
        "spec": None,
        "phase_id": None,
        "sub_epic": None,
        "dry_run": False,
        "backend": "gemini",
        "model": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _patch_constants(tmp_path):
    """Return a context-manager stack patching TOOLS_DIR/ROOT_DIR everywhere."""
    tools_dir = str(tmp_path / ".tools")
    root_dir = str(tmp_path)
    return [
        patch("workflow_lib.replan.TOOLS_DIR", tools_dir),
        patch("workflow_lib.replan.ROOT_DIR", root_dir),
        patch("workflow_lib.constants.TOOLS_DIR", tools_dir),
        patch("workflow_lib.constants.ROOT_DIR", root_dir),
        patch("workflow_lib.context.TOOLS_DIR", tools_dir),
        patch("workflow_lib.context.GEN_STATE_FILE", str(tmp_path / ".gen_state.json")),
        patch("workflow_lib.state.ROOT_DIR", root_dir),
    ]


# ---------------------------------------------------------------------------
# Stage 1: Template
# ---------------------------------------------------------------------------


class TestAddFeatureTemplate:
    """When called without --brief or --spec, prints the template."""

    def test_prints_template_and_returns(self, tmp_path, capsys):
        root = _setup_project(tmp_path)
        args = _make_args()

        patches = _patch_constants(root)
        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        captured = capsys.readouterr()
        assert "# Feature Brief" in captured.out
        assert "## Feature Name" in captured.out
        assert "workflow.py add-feature --brief" in captured.out

    def test_template_does_not_invoke_ai(self, tmp_path):
        root = _setup_project(tmp_path)
        args = _make_args()

        mock_runner = MagicMock()
        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=mock_runner))
        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        # _make_runner should not even be called in template mode
        # (no backend needed)
        mock_runner.run.assert_not_called()


# ---------------------------------------------------------------------------
# Stage 2: Discuss
# ---------------------------------------------------------------------------


class TestAddFeatureDiscuss:
    """With --brief, enters a discussion loop and generates a spec."""

    def _make_discuss_agent(self, spec_output_dir: Path):
        """Return mocks for runner.run (discussion) and ctx.run_ai (spec gen)."""
        call_count = [0]

        def _runner_run(cwd, full_prompt, image_paths=None, on_line=None, timeout=None):
            """Simulates the runner.run() call used in discussion rounds."""
            call_count[0] += 1
            response = "## Assessment\nLooks good.\n\n## Questions\n1. None."
            if on_line:
                for line in response.splitlines():
                    on_line(line)
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=response, stderr="",
            )

        def _run_ai(full_prompt, allowed_files=None, sandbox=True, timeout=None):
            """Simulates ctx.run_ai() for spec generation."""
            call_count[0] += 1
            if allowed_files:
                for f in allowed_files:
                    if isinstance(f, str) and not f.endswith(os.sep):
                        os.makedirs(os.path.dirname(os.path.abspath(f)), exist_ok=True)
                        with open(f, "w", encoding="utf-8") as fp:
                            fp.write(FEATURE_SPEC)

            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",
            )

        return _runner_run, _run_ai, call_count

    def test_discuss_then_generate_spec(self, tmp_path):
        """User says 'done' after one round; spec file is created."""
        root = _setup_project(tmp_path)
        brief_file = root / "widget_export.md"
        brief_file.write_text(FEATURE_BRIEF)

        args = _make_args(brief=str(brief_file))

        runner_fn, run_ai_fn, call_count = self._make_discuss_agent(root / "docs" / "plan" / "features")

        mock_runner = MagicMock()
        mock_runner.run.side_effect = runner_fn

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project description"
        mock_ctx.image_paths = []
        mock_ctx.agent_timeout = 600
        mock_ctx.runner = mock_runner
        mock_ctx.load_shared_components.return_value = "# Shared Components\n- widget_renderer"
        mock_ctx.load_prompt.side_effect = lambda name: (
            Path(__file__).parent.parent / "prompts" / name
        ).read_text() if (Path(__file__).parent.parent / "prompts" / name).exists() else f"# {name}"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.run_ai.side_effect = run_ai_fn
        mock_ctx.get_workspace_snapshot.return_value = {}

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        # User types 'done' on first prompt, then presses Enter to continue,
        # but we raise KeyboardInterrupt to stop before execution
        patches.append(patch("builtins.input", side_effect=["done", KeyboardInterrupt()]))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        # runner.run called once for discussion, run_ai called once for spec
        assert call_count[0] >= 2
        # Spec file should exist
        features_dir = root / "docs" / "plan" / "features"
        specs = list(features_dir.glob("spec_*.md"))
        assert len(specs) == 1
        assert "Widget Export" in specs[0].read_text()

    def test_discuss_quit_aborts(self, tmp_path, capsys):
        """User types 'quit' during discussion; no spec is generated."""
        root = _setup_project(tmp_path)
        brief_file = root / "widget_export.md"
        brief_file.write_text(FEATURE_BRIEF)

        args = _make_args(brief=str(brief_file))

        mock_runner = MagicMock()
        mock_runner.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="## Questions\n1. Why?\n", stderr="",
        )

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.image_paths = []
        mock_ctx.agent_timeout = 600
        mock_ctx.runner = mock_runner
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.get_workspace_snapshot.return_value = {}

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        patches.append(patch("builtins.input", return_value="quit"))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        captured = capsys.readouterr()
        assert "Aborted" in captured.out

        # No spec file created
        features_dir = root / "docs" / "plan" / "features"
        assert not features_dir.exists() or len(list(features_dir.glob("*.md"))) == 0

    def test_discuss_multiple_rounds(self, tmp_path):
        """User has two discussion rounds before typing 'done'."""
        root = _setup_project(tmp_path)
        brief_file = root / "widget_export.md"
        brief_file.write_text(FEATURE_BRIEF)

        args = _make_args(brief=str(brief_file))

        runner_fn, run_ai_fn, call_count = self._make_discuss_agent(root / "docs" / "plan" / "features")

        mock_runner = MagicMock()
        mock_runner.run.side_effect = runner_fn

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.image_paths = []
        mock_ctx.agent_timeout = 600
        mock_ctx.runner = mock_runner
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.run_ai.side_effect = run_ai_fn
        mock_ctx.get_workspace_snapshot.return_value = {}

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        # Round 1: user responds, Round 2: user says done, then KeyboardInterrupt to skip execution
        patches.append(patch("builtins.input", side_effect=[
            "What about error handling?",
            "done",
            KeyboardInterrupt(),
        ]))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        # 2 discussion rounds (runner.run) + 1 spec generation (run_ai) = 3 calls
        assert call_count[0] == 3

    def test_discuss_streams_agent_output(self, tmp_path, capsys):
        """Agent response is printed to stdout so the user can see it."""
        root = _setup_project(tmp_path)
        brief_file = root / "widget_export.md"
        brief_file.write_text(FEATURE_BRIEF)

        args = _make_args(brief=str(brief_file))

        agent_text = "## Assessment\nThis feature looks well-defined.\n\n## Questions\n1. What PDF library?"

        def _runner_run(cwd, full_prompt, image_paths=None, on_line=None, timeout=None):
            if on_line:
                for line in agent_text.splitlines():
                    on_line(line)
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=agent_text, stderr="",
            )

        mock_runner = MagicMock()
        mock_runner.run.side_effect = _runner_run

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.image_paths = []
        mock_ctx.agent_timeout = 600
        mock_ctx.runner = mock_runner
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.get_workspace_snapshot.return_value = {}

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        patches.append(patch("builtins.input", return_value="quit"))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        captured = capsys.readouterr()
        assert "This feature looks well-defined" in captured.out
        assert "What PDF library?" in captured.out

    def test_discuss_brief_not_found_exits(self, tmp_path):
        """Missing brief file causes sys.exit(1)."""
        root = _setup_project(tmp_path)
        args = _make_args(brief="/nonexistent/brief.md")

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()))
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc_info:
                cmd_add_feature(args)
            assert exc_info.value.code == 1
        finally:
            for p in patches:
                p.stop()

    def test_discuss_dry_run_does_not_write_spec(self, tmp_path, capsys):
        """With --dry-run, prints intended path but does not generate."""
        root = _setup_project(tmp_path)
        brief_file = root / "widget_export.md"
        brief_file.write_text(FEATURE_BRIEF)

        args = _make_args(brief=str(brief_file), dry_run=True)

        mock_runner = MagicMock()
        mock_runner.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="## Assessment\nGood.\n", stderr="",
        )

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.image_paths = []
        mock_ctx.agent_timeout = 600
        mock_ctx.runner = mock_runner
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.get_workspace_snapshot.return_value = {}

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        patches.append(patch("builtins.input", return_value="done"))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        captured = capsys.readouterr()
        assert "[dry-run]" in captured.out
        features_dir = root / "docs" / "plan" / "features"
        assert not features_dir.exists() or len(list(features_dir.glob("*.md"))) == 0


# ---------------------------------------------------------------------------
# Stage 3: Execute
# ---------------------------------------------------------------------------


class TestAddFeatureExecute:
    """With --spec, integrates the feature into the plan."""

    def _make_execute_agent(self, se_dir: Path, task_content: str = TASK_CONTENT):
        """Return a run_ai replacement that creates task files and updates requirements."""

        def _run_ai(full_prompt, allowed_files=None, sandbox=True, timeout=None):
            if allowed_files:
                for f in allowed_files:
                    if isinstance(f, str) and f.endswith(os.sep):
                        # Directory — create a task file in it
                        os.makedirs(f, exist_ok=True)
                        task_path = os.path.join(f, "01_implement_pdf_export.md")
                        if not os.path.exists(task_path):
                            with open(task_path, "w", encoding="utf-8") as fp:
                                fp.write(task_content)

            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",
            )

        return _run_ai

    def test_execute_creates_tasks_and_rebuilds_dag(self, tmp_path):
        """Spec integration creates task files and rebuilds the phase DAG."""
        root = _setup_project(tmp_path)
        spec_file = root / "docs" / "plan" / "features" / "spec_widget_export.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(FEATURE_SPEC)

        se_dir = root / "docs" / "plan" / "tasks" / "phase_2" / "widget_export"
        agent_fn = self._make_execute_agent(se_dir)

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.load_shared_components.return_value = "# Shared Components"
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.run_ai.side_effect = agent_fn

        args = _make_args(
            spec=str(spec_file),
            phase_id="phase_2",
            sub_epic="widget_export",
        )

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        patches.append(patch("workflow_lib.replan._rebuild_phase_dag"))
        patches.append(patch("workflow_lib.replan.load_replan_state", return_value={
            "blocked_tasks": {}, "removed_tasks": [], "replan_history": [],
        }))
        patches.append(patch("workflow_lib.replan.save_replan_state"))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        # Task file was created
        assert se_dir.exists()
        tasks = list(se_dir.glob("*.md"))
        assert len(tasks) == 1
        assert "PDF export" in tasks[0].read_text()

    def test_execute_calls_rebuild_dag(self, tmp_path):
        """After creating tasks, the phase DAG is rebuilt."""
        root = _setup_project(tmp_path)
        spec_file = root / "docs" / "plan" / "features" / "spec_widget_export.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(FEATURE_SPEC)

        se_dir = root / "docs" / "plan" / "tasks" / "phase_2" / "widget_export"
        agent_fn = self._make_execute_agent(se_dir)

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.run_ai.side_effect = agent_fn

        mock_rebuild = MagicMock()

        args = _make_args(
            spec=str(spec_file),
            phase_id="phase_2",
            sub_epic="widget_export",
        )

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        patches.append(patch("workflow_lib.replan._rebuild_phase_dag", mock_rebuild))
        patches.append(patch("workflow_lib.replan.load_replan_state", return_value={
            "blocked_tasks": {}, "removed_tasks": [], "replan_history": [],
        }))
        patches.append(patch("workflow_lib.replan.save_replan_state"))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        mock_rebuild.assert_called_once()
        call_args = mock_rebuild.call_args
        assert "phase_2" in call_args[0][0]

    def test_execute_logs_replan_action(self, tmp_path):
        """The add-feature action is logged in the replan audit trail."""
        root = _setup_project(tmp_path)
        spec_file = root / "docs" / "plan" / "features" / "spec_widget_export.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(FEATURE_SPEC)

        se_dir = root / "docs" / "plan" / "tasks" / "phase_2" / "widget_export"
        agent_fn = self._make_execute_agent(se_dir)

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.run_ai.side_effect = agent_fn

        rp_state = {"blocked_tasks": {}, "removed_tasks": [], "replan_history": []}
        mock_save = MagicMock()

        args = _make_args(
            spec=str(spec_file),
            phase_id="phase_2",
            sub_epic="widget_export",
        )

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        patches.append(patch("workflow_lib.replan._rebuild_phase_dag"))
        patches.append(patch("workflow_lib.replan.load_replan_state", return_value=rp_state))
        patches.append(patch("workflow_lib.replan.save_replan_state", mock_save))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert len(saved["replan_history"]) == 1
        assert saved["replan_history"][0]["action"] == "add-feature"
        assert "phase_2/widget_export" in saved["replan_history"][0]["target"]

    def test_execute_spec_not_found_exits(self, tmp_path):
        """Missing spec file causes sys.exit(1)."""
        root = _setup_project(tmp_path)
        args = _make_args(
            spec="/nonexistent/spec.md",
            phase_id="phase_2",
            sub_epic="widget_export",
        )

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()))
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc_info:
                cmd_add_feature(args)
            assert exc_info.value.code == 1
        finally:
            for p in patches:
                p.stop()

    def test_execute_dry_run(self, tmp_path, capsys):
        """With --dry-run, prints plan but does not create files."""
        root = _setup_project(tmp_path)
        spec_file = root / "docs" / "plan" / "features" / "spec_widget_export.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(FEATURE_SPEC)

        args = _make_args(
            spec=str(spec_file),
            phase_id="phase_2",
            sub_epic="widget_export",
            dry_run=True,
        )

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.load_shared_components.return_value = ""

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        captured = capsys.readouterr()
        assert "[dry-run]" in captured.out
        assert "phase_2/widget_export" in captured.out

        # No task files created
        se_dir = root / "docs" / "plan" / "tasks" / "phase_2" / "widget_export"
        assert not se_dir.exists()

    def test_execute_prompts_for_phase_and_sub_epic(self, tmp_path, capsys):
        """When --phase and --sub-epic are missing, prompts interactively."""
        root = _setup_project(tmp_path)
        spec_file = root / "docs" / "plan" / "features" / "spec_widget_export.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(FEATURE_SPEC)

        se_dir = root / "docs" / "plan" / "tasks" / "phase_2" / "widget_export"
        agent_fn = self._make_execute_agent(se_dir)

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.run_ai.side_effect = agent_fn

        args = _make_args(spec=str(spec_file))  # no phase_id or sub_epic

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        patches.append(patch("workflow_lib.replan._rebuild_phase_dag"))
        patches.append(patch("workflow_lib.replan.load_replan_state", return_value={
            "blocked_tasks": {}, "removed_tasks": [], "replan_history": [],
        }))
        patches.append(patch("workflow_lib.replan.save_replan_state"))
        # User provides phase and sub-epic via input()
        patches.append(patch("builtins.input", side_effect=["phase_2", "widget_export"]))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        captured = capsys.readouterr()
        assert "Available phases:" in captured.out
        assert se_dir.exists()

    def test_execute_appends_to_existing_tasks(self, tmp_path):
        """When sub-epic already has tasks, new ones start at the next number."""
        root = _setup_project(tmp_path)
        spec_file = root / "docs" / "plan" / "features" / "spec_widget_export.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(FEATURE_SPEC)

        # Pre-create an existing task
        se_dir = root / "docs" / "plan" / "tasks" / "phase_2" / "widget_export"
        se_dir.mkdir(parents=True, exist_ok=True)
        (se_dir / "01_existing_task.md").write_text("# Task: Existing\n")

        new_task = textwrap.dedent("""\
            # Task: New task (Sub-Epic: widget_export)

            ## Dependencies
            - depends_on: [01_existing_task.md]
            - shared_components: []
        """)

        def _run_ai(full_prompt, allowed_files=None, sandbox=True, timeout=None):
            if allowed_files:
                for f in allowed_files:
                    if isinstance(f, str) and f.endswith(os.sep):
                        task_path = os.path.join(f, "02_new_task.md")
                        if not os.path.exists(task_path):
                            with open(task_path, "w", encoding="utf-8") as fp:
                                fp.write(new_task)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.run_ai.side_effect = _run_ai

        args = _make_args(
            spec=str(spec_file),
            phase_id="phase_2",
            sub_epic="widget_export",
        )

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        patches.append(patch("workflow_lib.replan._rebuild_phase_dag"))
        patches.append(patch("workflow_lib.replan.load_replan_state", return_value={
            "blocked_tasks": {}, "removed_tasks": [], "replan_history": [],
        }))
        patches.append(patch("workflow_lib.replan.save_replan_state"))

        for p in patches:
            p.start()
        try:
            cmd_add_feature(args)
        finally:
            for p in patches:
                p.stop()

        tasks = sorted(se_dir.glob("*.md"))
        assert len(tasks) == 2
        assert tasks[0].name == "01_existing_task.md"
        assert tasks[1].name == "02_new_task.md"

    def test_execute_ai_failure_exits(self, tmp_path):
        """When the AI agent fails, sys.exit(1) is raised."""
        root = _setup_project(tmp_path)
        spec_file = root / "docs" / "plan" / "features" / "spec_widget_export.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(FEATURE_SPEC)

        mock_ctx = MagicMock()
        mock_ctx.description_ctx = "Test project"
        mock_ctx.load_shared_components.return_value = ""
        mock_ctx.load_prompt.return_value = "# stub"
        mock_ctx.format_prompt.side_effect = lambda tmpl, **kw: tmpl
        mock_ctx.run_ai.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="error output", stderr="error details",
        )

        args = _make_args(
            spec=str(spec_file),
            phase_id="phase_2",
            sub_epic="widget_export",
        )

        patches = _patch_constants(root)
        patches.append(patch("workflow_lib.replan._make_runner", return_value=MagicMock()))
        patches.append(patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx))
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc_info:
                cmd_add_feature(args)
            assert exc_info.value.code == 1
        finally:
            for p in patches:
                p.stop()


# ---------------------------------------------------------------------------
# Helpers unit tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """Unit tests for _load_requirements_ctx and _load_phases_ctx."""

    def test_load_requirements_ctx_reads_file(self, tmp_path):
        req_content = "# Requirements\n- [REQ-001] Test\n"
        (tmp_path / "requirements.md").write_text(req_content)
        with patch("workflow_lib.replan.ROOT_DIR", str(tmp_path)):
            result = _load_requirements_ctx()
        assert "REQ-001" in result

    def test_load_requirements_ctx_missing_file(self, tmp_path):
        with patch("workflow_lib.replan.ROOT_DIR", str(tmp_path)):
            result = _load_requirements_ctx()
        assert "no requirements.md" in result

    def test_load_phases_ctx_reads_files(self, tmp_path):
        phases_dir = tmp_path / "docs" / "plan" / "phases"
        phases_dir.mkdir(parents=True)
        (phases_dir / "phase_1.md").write_text("# Phase 1: Core\n")
        (phases_dir / "phase_2.md").write_text("# Phase 2: Extensions\n")

        with patch("workflow_lib.replan.ROOT_DIR", str(tmp_path)):
            result = _load_phases_ctx()
        assert "phase_1.md" in result
        assert "Phase 1: Core" in result
        assert "Phase 2: Extensions" in result

    def test_load_phases_ctx_missing_dir(self, tmp_path):
        with patch("workflow_lib.replan.ROOT_DIR", str(tmp_path)):
            result = _load_phases_ctx()
        assert "no phases found" in result
