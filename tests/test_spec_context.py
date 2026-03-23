"""Tests that spec-editing phases provide all specs as accumulated context.

Verifies that:
1. get_accumulated_context with include_all=True includes docs after current_doc.
2. Phase2FleshOutDoc passes include_all=True.
3. Review phases (FinalReview, ConflictResolution, AdversarialReview) include
   accumulated_context in their prompts.
4. Review prompt templates contain the {accumulated_context} placeholder.
"""

import os
import sys
import tempfile
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.constants import DOCS
from workflow_lib.context import ProjectContext


PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")


# ---------------------------------------------------------------------------
# get_accumulated_context include_all tests
# ---------------------------------------------------------------------------


class TestAccumulatedContextIncludeAll:
    """Verify include_all parameter on get_accumulated_context."""

    @pytest.fixture()
    def ctx_with_specs(self, tmp_path):
        """Create a ProjectContext with fake spec files for all DOCS."""
        root = tmp_path / "project"
        root.mkdir()
        plan_dir = root / "docs" / "plan"
        specs_dir = plan_dir / "specs"
        specs_dir.mkdir(parents=True)
        summaries_dir = plan_dir / "summaries"
        summaries_dir.mkdir(parents=True)
        input_dir = root / ".tools" / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "desc.txt").write_text("project description")

        for doc in DOCS:
            (specs_dir / f"{doc['id']}.md").write_text(
                f"# {doc['name']}\nContent for {doc['id']}.\n"
            )

        with patch("workflow_lib.context.get_context_limit", return_value=500_000):
            ctx = ProjectContext.__new__(ProjectContext)
            ctx.root_dir = str(root)
            ctx.plan_dir = str(plan_dir)
            ctx.summaries_dir = str(summaries_dir)
            ctx.description_ctx = "project description"
            ctx.image_paths = []
            ctx.runner = MagicMock()
            ctx.dashboard = None
            ctx.current_phase = None
            ctx._tls = MagicMock()
            ctx.agent_timeout = None
        return ctx

    def test_default_excludes_later_docs(self, ctx_with_specs):
        """Without include_all, only preceding docs are included."""
        # Use the third doc so there are docs before and after it
        current = DOCS[2]
        with patch("workflow_lib.context.get_context_limit", return_value=500_000):
            result = ctx_with_specs.get_accumulated_context(current, extra_tokens=0)

        # Should include docs 0 and 1 but NOT docs 3+
        assert DOCS[0]["name"] in result
        assert DOCS[1]["name"] in result
        for later_doc in DOCS[3:]:
            assert later_doc["name"] not in result

    def test_include_all_includes_later_docs(self, ctx_with_specs):
        """With include_all=True, docs after current_doc are also included."""
        current = DOCS[2]
        with patch("workflow_lib.context.get_context_limit", return_value=500_000):
            result = ctx_with_specs.get_accumulated_context(
                current, extra_tokens=0, include_all=True
            )

        # Should include docs before AND after, but NOT current
        assert DOCS[0]["name"] in result
        assert DOCS[1]["name"] in result
        assert current["name"] not in result
        for later_doc in DOCS[3:]:
            assert later_doc["name"] in result

    def test_none_current_doc_includes_all(self, ctx_with_specs):
        """With current_doc=None, all docs are included."""
        with patch("workflow_lib.context.get_context_limit", return_value=500_000):
            result = ctx_with_specs.get_accumulated_context(
                None, extra_tokens=0
            )

        for doc in DOCS:
            assert doc["name"] in result


# ---------------------------------------------------------------------------
# Prompt template placeholder tests
# ---------------------------------------------------------------------------


class TestReviewPromptPlaceholders:
    """Review prompts must contain {accumulated_context}."""

    @pytest.mark.parametrize("prompt_file", [
        "final_review.md",
        "conflict_resolution_review.md",
        "adversarial_review.md",
    ])
    def test_accumulated_context_placeholder(self, prompt_file):
        path = os.path.join(PROMPTS_DIR, prompt_file)
        with open(path, "r") as f:
            content = f.read()
        assert "{accumulated_context}" in content, (
            f"{prompt_file} is missing {{accumulated_context}} placeholder"
        )


# ---------------------------------------------------------------------------
# Phase integration tests — verify phases pass accumulated context
# ---------------------------------------------------------------------------


class TestPhase2FleshOutUsesIncludeAll:
    """Phase2FleshOutDoc must pass include_all=True to get_accumulated_context."""

    def test_flesh_out_calls_include_all(self):
        """Verify the source code passes include_all=True."""
        import inspect
        from workflow_lib.phases import Phase2FleshOutDoc

        source = inspect.getsource(Phase2FleshOutDoc.execute)
        assert "include_all=True" in source, (
            "Phase2FleshOutDoc.execute must call get_accumulated_context "
            "with include_all=True"
        )


class TestReviewPhasesIncludeAccumulatedContext:
    """Review phases must build and pass accumulated_context to the prompt."""

    @pytest.mark.parametrize("phase_class_name", [
        "Phase3FinalReview",
        "Phase3AConflictResolution",
        "Phase3BAdversarialReview",
    ])
    def test_review_phase_calls_get_accumulated_context(self, phase_class_name):
        """Verify review phase source code calls get_accumulated_context."""
        import inspect
        from workflow_lib import phases

        cls = getattr(phases, phase_class_name)
        source = inspect.getsource(cls.execute)
        assert "get_accumulated_context" in source, (
            f"{phase_class_name}.execute must call get_accumulated_context"
        )
        assert "accumulated_context" in source, (
            f"{phase_class_name}.execute must pass accumulated_context to the prompt"
        )
