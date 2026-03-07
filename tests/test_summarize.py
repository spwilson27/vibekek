"""Tests for the Phase2B summarization workflow.

Covers:
- Phase2BSummarizeDoc phase execution (success, skip, failure, missing source)
- ProjectContext summary path helpers
- get_accumulated_context preference for summaries over full documents
- Orchestrator integration (Phase2B runs after Phase2 for each doc)
- Prompt registry inclusion
- Summarize prompt file existence and placeholder coverage
"""
import json
import os
import sys
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.constants import DOCS
from workflow_lib.context import ProjectContext
from workflow_lib.phases import Phase2BSummarizeDoc
from workflow_lib.prompt_registry import PROMPT_PLACEHOLDERS, validate_all_prompts_exist


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_ctx(state=None):
    """Create a MagicMock ProjectContext with sensible defaults."""
    ctx = MagicMock(spec=ProjectContext)
    ctx.state = state or {}
    ctx.root_dir = "/fake/root"
    ctx.plan_dir = "/fake/root/docs/plan"
    ctx.summaries_dir = "/fake/root/docs/plan/summaries"
    ctx.description_ctx = "Project description"
    ctx.run_gemini.return_value = MagicMock(returncode=0, stdout="", stderr="")
    ctx.load_prompt.return_value = "Summarize {document_name} to {summary_path}: {document_content}"
    ctx.format_prompt.return_value = "formatted summarize prompt"
    ctx.get_document_path.return_value = "/fake/root/docs/plan/specs/doc.md"
    ctx.get_summary_path.return_value = "/fake/root/docs/plan/summaries/doc.md"
    ctx.get_summary_target_path.return_value = "docs/plan/summaries/doc.md"
    return ctx


def _real_ctx(**kwargs):
    """Create a real ProjectContext.__new__ with mocked filesystem."""
    with patch("os.makedirs"), \
         patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data="description")), \
         patch("workflow_lib.context.GeminiRunner"):
        ctx = ProjectContext.__new__(ProjectContext)
        ctx.root_dir = "/fake/root"
        ctx.plan_dir = "/fake/root/docs/plan"
        ctx.specs_dir = "/fake/root/docs/plan/specs"
        ctx.research_dir = "/fake/root/docs/plan/research"
        ctx.requirements_dir = "/fake/root/docs/plan/requirements"
        ctx.summaries_dir = "/fake/root/docs/plan/summaries"
        ctx.prompts_dir = "/fake/.tools/prompts"
        ctx.state_file = "/fake/.gen_state.json"
        ctx.input_dir = "/fake/.tools/input"
        ctx.shared_components_file = "/fake/root/docs/plan/shared_components.md"
        ctx.state = kwargs.get("state", {})
        ctx.description_ctx = "project desc"
        ctx.runner = MagicMock()
        ctx.ignore_sandbox = False
        return ctx


# ---------------------------------------------------------------------------
# Phase2BSummarizeDoc – unit tests
# ---------------------------------------------------------------------------

class TestPhase2BSummarizeDoc:
    """Unit tests for the summarization phase."""

    def test_skip_when_already_summarized(self):
        ctx = _mock_ctx(state={"summarized": ["my_doc"]})
        doc = {"id": "my_doc", "type": "spec", "name": "My Doc"}
        Phase2BSummarizeDoc(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_skip_when_source_missing(self):
        ctx = _mock_ctx()
        doc = {"id": "my_doc", "type": "spec", "name": "My Doc"}
        with patch("os.path.exists", return_value=False):
            Phase2BSummarizeDoc(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_success_marks_state_and_stages(self):
        ctx = _mock_ctx()
        doc = {"id": "my_doc", "type": "spec", "name": "My Doc"}
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="# Full document content\nLots of text.")):
            Phase2BSummarizeDoc(doc).execute(ctx)

        ctx.load_prompt.assert_called_once_with("summarize_doc.md")
        ctx.run_gemini.assert_called_once()
        ctx.stage_changes.assert_called_once()
        assert "my_doc" in ctx.state["summarized"]
        ctx.save_state.assert_called()

    def test_failure_exits_nonzero(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="error", stderr="details")
        doc = {"id": "my_doc", "type": "spec", "name": "My Doc"}
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="content")), \
             pytest.raises(SystemExit):
            Phase2BSummarizeDoc(doc).execute(ctx)

    def test_allowed_files_is_summary_path(self):
        """run_gemini should receive the summary file as the only allowed file."""
        ctx = _mock_ctx()
        summary_abs = "/fake/root/docs/plan/summaries/my_doc.md"
        ctx.get_summary_path.return_value = summary_abs
        doc = {"id": "my_doc", "type": "spec", "name": "My Doc"}
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="content")):
            Phase2BSummarizeDoc(doc).execute(ctx)
        _, kwargs = ctx.run_gemini.call_args
        assert kwargs.get("allowed_files") == [summary_abs] or \
               ctx.run_gemini.call_args[0] == ("formatted summarize prompt",) and \
               kwargs.get("allowed_files") == [summary_abs]

    def test_prompt_receives_document_content(self):
        """format_prompt should be called with the full document content."""
        ctx = _mock_ctx()
        doc = {"id": "my_doc", "type": "spec", "name": "My Doc"}
        doc_text = "# Architecture\n\nDetailed architecture here."
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=doc_text)):
            Phase2BSummarizeDoc(doc).execute(ctx)
        _, kwargs = ctx.format_prompt.call_args
        assert kwargs["document_content"] == doc_text
        assert kwargs["document_name"] == "My Doc"

    def test_multiple_docs_tracked_independently(self):
        ctx = _mock_ctx(state={"summarized": ["doc_a"]})
        doc_b = {"id": "doc_b", "type": "spec", "name": "Doc B"}
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="content")):
            Phase2BSummarizeDoc(doc_b).execute(ctx)
        assert "doc_a" in ctx.state["summarized"]
        assert "doc_b" in ctx.state["summarized"]

    def test_operation_property(self):
        phase = Phase2BSummarizeDoc({"id": "x", "name": "X"})
        assert phase.operation == "Summarize"

    def test_display_name_includes_doc_name(self):
        phase = Phase2BSummarizeDoc({"id": "x", "name": "My Document"})
        assert "My Document" in phase.display_name
        assert "Phase2B" in phase.display_name

    def test_research_docs_are_also_summarized(self):
        """Summarization should work for research docs too, not just specs."""
        ctx = _mock_ctx()
        doc = {"id": "market_research", "type": "research", "name": "Market Research"}
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="Research content")):
            Phase2BSummarizeDoc(doc).execute(ctx)
        ctx.run_gemini.assert_called_once()
        assert "market_research" in ctx.state["summarized"]


# ---------------------------------------------------------------------------
# ProjectContext – summary path helpers
# ---------------------------------------------------------------------------

class TestContextSummaryPaths:
    """Tests for get_summary_path and get_summary_target_path."""

    def test_get_summary_path_spec(self):
        ctx = _real_ctx()
        doc = {"id": "2_tas", "type": "spec", "name": "TAS"}
        result = ctx.get_summary_path(doc)
        assert result == "/fake/root/docs/plan/summaries/2_tas.md"

    def test_get_summary_path_research(self):
        ctx = _real_ctx()
        doc = {"id": "market_research", "type": "research", "name": "Market"}
        result = ctx.get_summary_path(doc)
        assert result == "/fake/root/docs/plan/summaries/market_research.md"

    def test_get_summary_target_path(self):
        ctx = _real_ctx()
        doc = {"id": "3_mcp_design", "type": "spec", "name": "MCP"}
        result = ctx.get_summary_target_path(doc)
        assert result == "docs/plan/summaries/3_mcp_design.md"

    def test_summaries_dir_created_on_init(self, tmp_path):
        """ProjectContext.__init__ should create the summaries directory."""
        tools_dir = tmp_path / ".tools"
        tools_dir.mkdir()
        (tools_dir / "input").mkdir()
        (tools_dir / "input" / "desc.md").write_text("# Project\nDesc.")
        with patch("workflow_lib.context.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.context.GEN_STATE_FILE",
                   str(tmp_path / ".gen_state.json")), \
             patch("workflow_lib.context.GeminiRunner"):
            ctx = ProjectContext(str(tmp_path))
        assert os.path.isdir(ctx.summaries_dir)
        assert ctx.summaries_dir.endswith("summaries")


# ---------------------------------------------------------------------------
# get_accumulated_context – summary preference logic
# ---------------------------------------------------------------------------

class TestAccumulatedContextSummaryPreference:
    """Tests for get_accumulated_context preferring summaries."""

    def test_uses_summary_when_available(self):
        ctx = _real_ctx()
        prev = {"id": "prd", "type": "spec", "name": "PRD"}
        current = {"id": "tas", "type": "spec", "name": "TAS"}

        def exists_side(path):
            return "summaries" in path

        with patch("workflow_lib.context.DOCS", [prev, current]), \
             patch("os.path.exists", side_effect=exists_side), \
             patch("builtins.open", mock_open(read_data="PRD summary")):
            result = ctx.get_accumulated_context(current)

        assert 'type="summary"' in result
        assert "PRD summary" in result

    def test_falls_back_to_full_doc_when_no_summary(self):
        ctx = _real_ctx()
        prev = {"id": "prd", "type": "spec", "name": "PRD"}
        current = {"id": "tas", "type": "spec", "name": "TAS"}

        def exists_side(path):
            # No summary exists, only the full doc
            return "summaries" not in path

        with patch("workflow_lib.context.DOCS", [prev, current]), \
             patch("os.path.exists", side_effect=exists_side), \
             patch("builtins.open", mock_open(read_data="Full PRD content")):
            result = ctx.get_accumulated_context(current)

        assert 'type="summary"' not in result
        assert "Full PRD content" in result

    def test_mixed_summary_and_full(self):
        """When some docs have summaries and others don't, use the appropriate version."""
        ctx = _real_ctx()
        doc_a = {"id": "doc_a", "type": "spec", "name": "Doc A"}
        doc_b = {"id": "doc_b", "type": "spec", "name": "Doc B"}
        current = {"id": "doc_c", "type": "spec", "name": "Doc C"}

        call_count = {"n": 0}
        file_contents = {
            "/fake/root/docs/plan/summaries/doc_a.md": "Summary of A",
            "/fake/root/docs/plan/specs/doc_b.md": "Full content of B",
        }

        def exists_side(path):
            return path in file_contents

        def open_side(path, *args, **kwargs):
            m = mock_open(read_data=file_contents.get(path, ""))()
            return m

        with patch("workflow_lib.context.DOCS", [doc_a, doc_b, current]), \
             patch("os.path.exists", side_effect=exists_side), \
             patch("builtins.open", side_effect=open_side):
            result = ctx.get_accumulated_context(current)

        assert "Doc A" in result
        assert "Doc B" in result
        assert 'type="summary"' in result  # doc_a uses summary

    def test_no_docs_returns_empty(self):
        ctx = _real_ctx()
        current = {"id": "first", "type": "spec", "name": "First"}
        with patch("workflow_lib.context.DOCS", [current]):
            result = ctx.get_accumulated_context(current)
        assert result == ""

    def test_research_excluded_even_with_summary(self):
        """include_research=False should skip research summaries too."""
        ctx = _real_ctx()
        research = {"id": "market", "type": "research", "name": "Market"}
        current = {"id": "prd", "type": "spec", "name": "PRD"}

        with patch("workflow_lib.context.DOCS", [research, current]), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="market summary")):
            result = ctx.get_accumulated_context(current, include_research=False)

        assert result == ""


# ---------------------------------------------------------------------------
# Prompt registry & file
# ---------------------------------------------------------------------------

class TestSummarizePromptRegistry:
    def test_registered_in_prompt_placeholders(self):
        assert "summarize_doc.md" in PROMPT_PLACEHOLDERS

    def test_required_placeholders(self):
        required = PROMPT_PLACEHOLDERS["summarize_doc.md"]
        assert "document_name" in required
        assert "document_content" in required
        assert "summary_path" in required

    def test_prompt_file_exists(self):
        prompts_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts"
        )
        assert os.path.exists(os.path.join(prompts_dir, "summarize_doc.md"))

    def test_prompt_file_validates(self):
        prompts_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts"
        )
        missing = validate_all_prompts_exist(prompts_dir)
        assert "summarize_doc.md" not in missing

    def test_prompt_contains_placeholders(self):
        prompts_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts"
        )
        with open(os.path.join(prompts_dir, "summarize_doc.md")) as f:
            content = f.read()
        for placeholder in PROMPT_PLACEHOLDERS["summarize_doc.md"]:
            assert f"{{{placeholder}}}" in content, \
                f"Placeholder {{{placeholder}}} missing from summarize_doc.md"


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------

class TestOrchestratorSummarizeIntegration:
    """Verify Phase2BSummarizeDoc is wired into the orchestrator loop."""

    def test_summarize_runs_after_flesh_out(self, tmp_path):
        """Phase2B should run for each doc after Phase2 in the orchestrator."""
        from workflow_lib.orchestrator import Orchestrator

        tools_dir = tmp_path / ".tools"
        tools_dir.mkdir()
        (tools_dir / "prompts").mkdir()
        (tools_dir / "input").mkdir()
        (tools_dir / "input" / "desc.md").write_text("# Proj\nDesc.")

        # Create all prompt stubs
        for name in PROMPT_PLACEHOLDERS:
            (tools_dir / "prompts" / name).write_text("stub prompt")

        # Track phase execution order
        executed_phases = []

        def tracking_agent(self_ctx, full_prompt, allowed_files=None, sandbox=False):
            if allowed_files:
                for f in allowed_files:
                    if isinstance(f, str) and not f.endswith(os.sep):
                        os.makedirs(os.path.dirname(os.path.abspath(f)), exist_ok=True)
                        if not os.path.exists(f):
                            with open(f, "w") as fp:
                                fp.write("# Stub\n")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        # We only need to test that Phase2B appears in the right spot.
        # Run the first 3 phases (Phase1 + Phase2 + Phase2B) for the first doc, then abort.
        original_run_phase = Orchestrator.run_phase_with_retry

        def tracking_run_phase(orc_self, phase):
            executed_phases.append(type(phase).__name__)
            # After we've seen Phase2BSummarizeDoc for the first doc, abort
            if type(phase).__name__ == "Phase2BSummarizeDoc":
                # Let it run but then stop after the first doc
                original_run_phase(orc_self, phase)
                raise _StopEarly()
                return
            original_run_phase(orc_self, phase)

        class _StopEarly(Exception):
            pass

        with patch("workflow_lib.constants.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.constants.ROOT_DIR", str(tmp_path)), \
             patch("workflow_lib.context.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.context.GEN_STATE_FILE",
                   str(tmp_path / ".gen_state.json")), \
             patch("workflow_lib.phases.TOOLS_DIR", str(tools_dir)), \
             patch("workflow_lib.context.ProjectContext.run_gemini", tracking_agent), \
             patch("workflow_lib.context.ProjectContext.stage_changes"), \
             patch("workflow_lib.context.ProjectContext.verify_changes"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0,
                                                            stdout="", stderr="")):

            ctx = ProjectContext(str(tmp_path))
            orc = Orchestrator(ctx)

            with patch.object(Orchestrator, "run_phase_with_retry", tracking_run_phase):
                with pytest.raises(_StopEarly):
                    orc._run_phases()

        # The first doc should go: Phase1 -> Phase2 -> Phase2B
        assert executed_phases[:3] == [
            "Phase1GenerateDoc",
            "Phase2FleshOutDoc",
            "Phase2BSummarizeDoc",
        ]

    def test_skip_all_summarized_no_ai_calls(self):
        """When all docs are already summarized, no AI calls are made for Phase2B."""
        ctx = _mock_ctx()
        ctx.state = {"summarized": [d["id"] for d in DOCS]}

        for doc in DOCS:
            phase = Phase2BSummarizeDoc(doc)
            phase.execute(ctx)

        ctx.run_gemini.assert_not_called()
