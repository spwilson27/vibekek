"""Regression tests for prompt context truncation in executor.run_agent().

Verifies that the context_limit setting from .workflow.jsonc is respected
when building prompts for task execution agents, matching the truncation
behaviour already used in planning phases.
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.executor import truncate_task_context, _TRUNCATABLE_CONTEXT_KEYS
from workflow_lib.context import fit_lines_to_budget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(words_per_key: int = 100) -> dict:
    """Build a task_context dict with controllable content size.

    Content is spread across multiple lines (~10 words each) so that
    line-based truncation can operate meaningfully.
    """
    words_per_line = 10
    num_lines = max(words_per_key // words_per_line, 1)
    content = "\n".join("word " * words_per_line for _ in range(num_lines)) + "\n"
    return {
        "task_details": content,
        "spec_ctx": content,
        "description_ctx": content,
        "shared_components_ctx": content,
        "memory_ctx": content,
        "phase_filename": "phase_0",
        "task_name": "test_task.md",
        "target_dir": "phase_0/test_task.md",
        "clone_dir": "/tmp/test",
    }


def _word_count(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------------
# truncate_task_context
# ---------------------------------------------------------------------------

class TestTruncateTaskContext:
    """Tests for the truncate_task_context function."""

    def test_no_truncation_when_within_budget(self):
        """Content that fits within budget should not be modified."""
        ctx = _make_context(words_per_key=10)  # 50 words total in truncatable keys
        result = truncate_task_context(ctx, word_budget=10000)
        for key in _TRUNCATABLE_CONTEXT_KEYS:
            assert result[key] == ctx[key], f"{key} should not be truncated"

    def test_truncation_when_exceeding_budget(self):
        """Content exceeding budget should be truncated with a note."""
        ctx = _make_context(words_per_key=1000)  # 5000 words in truncatable keys
        result = truncate_task_context(ctx, word_budget=500)
        truncated_any = False
        for key in _TRUNCATABLE_CONTEXT_KEYS:
            if "truncated to fit context budget" in result[key]:
                truncated_any = True
        assert truncated_any, "At least one key should be truncated"

    def test_truncation_note_includes_source(self):
        """Truncation notes should identify the source key."""
        ctx = _make_context(words_per_key=2000)
        result = truncate_task_context(ctx, word_budget=200)
        for key in _TRUNCATABLE_CONTEXT_KEYS:
            val = result[key]
            if "truncated to fit context budget" in val:
                assert f"source: {key}" in val, \
                    f"Truncation note for {key} should include source identifier"

    def test_truncation_note_includes_line_count(self):
        """Truncation note should state how many lines were omitted."""
        lines = "\n".join(f"line {i} " + "word " * 20 for i in range(200))
        ctx = {"task_details": lines, "spec_ctx": "", "description_ctx": "",
               "shared_components_ctx": "", "memory_ctx": "",
               "phase_filename": "p", "task_name": "t"}
        result = truncate_task_context(ctx, word_budget=100)
        assert "more lines" in result["task_details"]

    def test_non_truncatable_keys_preserved(self):
        """Keys not in _TRUNCATABLE_CONTEXT_KEYS should pass through unchanged."""
        ctx = _make_context(words_per_key=2000)
        result = truncate_task_context(ctx, word_budget=200)
        assert result["phase_filename"] == "phase_0"
        assert result["task_name"] == "test_task.md"
        assert result["clone_dir"] == "/tmp/test"

    def test_empty_context_values_skipped(self):
        """Empty truncatable values should not consume budget."""
        ctx = _make_context(words_per_key=500)
        ctx["spec_ctx"] = ""
        ctx["memory_ctx"] = ""
        result = truncate_task_context(ctx, word_budget=1000)
        # Budget should be split among 3 non-empty keys, not 5
        # so each gets more room — task_details should be less truncated
        assert result["spec_ctx"] == ""
        assert result["memory_ctx"] == ""

    def test_template_static_text_counted(self):
        """Static template text should reduce the available budget."""
        ctx = _make_context(words_per_key=100)
        # With a large static template, less budget remains for context
        big_template = "static " * 500 + "{task_details}{spec_ctx}{description_ctx}{shared_components_ctx}{memory_ctx}"
        result_tight = truncate_task_context(ctx, word_budget=600, prompt_tmpl=big_template)
        result_loose = truncate_task_context(ctx, word_budget=600, prompt_tmpl="{task_details}")
        # With more static text, truncation should be more aggressive
        tight_words = sum(_word_count(result_tight[k]) for k in _TRUNCATABLE_CONTEXT_KEYS)
        loose_words = sum(_word_count(result_loose[k]) for k in _TRUNCATABLE_CONTEXT_KEYS)
        assert tight_words <= loose_words

    def test_zero_budget_returns_copy(self):
        """A zero budget should return the context unchanged (defensive)."""
        ctx = _make_context(words_per_key=100)
        result = truncate_task_context(ctx, word_budget=0)
        assert result is not ctx  # should be a copy
        for key in _TRUNCATABLE_CONTEXT_KEYS:
            assert result[key] == ctx[key]

    def test_result_is_a_copy(self):
        """Original context should not be mutated."""
        ctx = _make_context(words_per_key=2000)
        original_details = ctx["task_details"]
        truncate_task_context(ctx, word_budget=200)
        assert ctx["task_details"] == original_details


# ---------------------------------------------------------------------------
# Integration: run_agent applies truncation
# ---------------------------------------------------------------------------

class TestRunAgentTruncation:
    """Verify that run_agent() applies context_limit truncation."""

    @patch("workflow_lib.executor.run_ai_command", return_value=(0, ""))
    @patch("workflow_lib.executor.get_project_images", return_value=[])
    @patch("workflow_lib.executor.get_rag_enabled", return_value=False)
    @patch("workflow_lib.executor.get_context_limit", return_value=200)
    def test_run_agent_truncates_large_prompt(
        self, mock_limit, mock_rag, mock_images, mock_run_ai,
        tmp_path,
    ):
        """run_agent should truncate context when it exceeds context_limit."""
        from workflow_lib.executor import run_agent

        # Create a minimal prompt template
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.md").write_text("Template: {task_details} {spec_ctx}")

        ctx = {
            "task_details": "\n".join("word " * 10 for _ in range(50)),
            "spec_ctx": "\n".join("word " * 10 for _ in range(50)),
            "description_ctx": "",
            "shared_components_ctx": "",
            "memory_ctx": "",
            "phase_filename": "phase_0",
            "task_name": "test.md",
        }

        with patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)):
            run_agent("Test", "test.md", ctx, str(tmp_path))

        # The prompt passed to run_ai_command should be truncated
        actual_prompt = mock_run_ai.call_args[0][0]
        actual_words = _word_count(actual_prompt)
        # Should be well under the original 1000+ words
        assert actual_words < 500, f"Prompt should be truncated, got {actual_words} words"

    @patch("workflow_lib.executor.run_ai_command", return_value=(0, ""))
    @patch("workflow_lib.executor.get_project_images", return_value=[])
    @patch("workflow_lib.executor.get_rag_enabled", return_value=False)
    @patch("workflow_lib.executor.get_context_limit", return_value=100000)
    def test_run_agent_no_truncation_within_limit(
        self, mock_limit, mock_rag, mock_images, mock_run_ai,
        tmp_path,
    ):
        """run_agent should not truncate when content fits within limit."""
        from workflow_lib.executor import run_agent

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.md").write_text("{task_details}")

        original_details = "some content here\n"
        ctx = {
            "task_details": original_details,
            "spec_ctx": "",
            "description_ctx": "",
            "shared_components_ctx": "",
            "memory_ctx": "",
            "phase_filename": "phase_0",
            "task_name": "test.md",
        }

        with patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)):
            run_agent("Test", "test.md", ctx, str(tmp_path))

        actual_prompt = mock_run_ai.call_args[0][0]
        assert original_details.strip() in actual_prompt

    @patch("workflow_lib.executor.run_ai_command", return_value=(0, ""))
    @patch("workflow_lib.executor.get_project_images", return_value=[])
    @patch("workflow_lib.executor.get_rag_enabled", return_value=False)
    @patch("workflow_lib.executor.set_agent_context_limit")
    def test_run_agent_sets_agent_context_limit_from_pool(
        self, mock_set_limit, mock_rag, mock_images, mock_run_ai,
        tmp_path,
    ):
        """run_agent should call set_agent_context_limit with the agent's configured limit."""
        from workflow_lib.executor import run_agent
        from workflow_lib.agent_pool import AgentPoolManager

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.md").write_text("{task_details}")

        ctx = {
            "task_details": "hello",
            "phase_filename": "p",
            "task_name": "t",
        }

        # Create a mock agent config with context_limit
        mock_agent = MagicMock()
        mock_agent.backend = "claude"
        mock_agent.model = "sonnet"
        mock_agent.user = None
        mock_agent.context_limit = 150000
        mock_agent.spawn_rate = 0.0
        mock_agent.env = None
        mock_agent.cargo_target_dir = None
        mock_agent.name = "claude-sonnet"

        mock_pool = MagicMock(spec=AgentPoolManager)
        mock_pool.acquire.return_value = mock_agent

        with patch("workflow_lib.executor.TOOLS_DIR", str(tmp_path)):
            run_agent("Test", "test.md", ctx, str(tmp_path),
                      agent_pool=mock_pool, _pre_acquired_agent=mock_agent)

        mock_set_limit.assert_called_with(150000)
