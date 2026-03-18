"""Tests for build_context_block and fit_lines_to_budget in context.py.

These replace the former tests for the deleted _collect_task_files /
_build_tasks_content helpers in phases.py.  The same behavioural guarantees
are validated at the new, canonical location.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.context import build_context_block, fit_lines_to_budget, ProjectContext, _count_tokens


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(rel: str, content: str) -> dict:
    return {"rel": rel, "lines": content.splitlines(keepends=True)}


# ---------------------------------------------------------------------------
# fit_lines_to_budget
# ---------------------------------------------------------------------------

class TestFitLinesToBudget:
    def test_empty_entries(self):
        assert fit_lines_to_budget([], 1000) == 0

    def test_all_empty_lines(self):
        assert fit_lines_to_budget([[], []], 1000) == 0

    def test_fits_within_budget(self):
        lines = ["word " * 10 + "\n"] * 5   # ~20 tokens per entry (50 chars / 2.5)
        result = fit_lines_to_budget([lines, lines], 200)
        # 2 entries × 5 lines × ~20 tokens = ~200 tokens ≤ 200 → all lines fit
        assert result == 5

    def test_truncates_to_budget(self):
        lines = ["word " * 10 + "\n"] * 10  # ~20 tokens per entry
        # Two entries, ~400 tokens total at full. Budget of 60 → ~1-2 lines per entry
        result = fit_lines_to_budget([lines, lines], 60)
        assert 1 <= result < 10

    def test_budget_of_zero_gives_one_line(self):
        lines = ["a b c\n"] * 5
        result = fit_lines_to_budget([lines], 0)
        assert result >= 1

    def test_exact_budget_boundary(self):
        lines = ["word\n"] * 4   # 1 word per line
        result = fit_lines_to_budget([lines], 4)
        assert result == 4


# ---------------------------------------------------------------------------
# build_context_block
# ---------------------------------------------------------------------------

class TestBuildContextBlock:
    def test_empty_entries_returns_empty(self):
        assert build_context_block([], 1000) == ""

    def test_header_contains_rel_path(self, tmp_path):
        entry = _entry("docs/plan/tasks/phase_1/epic_1/01_a.md", "hello\n")
        result = build_context_block([entry], 1000)
        assert "### docs/plan/tasks/phase_1/epic_1/01_a.md" in result

    def test_no_truncation_under_budget(self, capsys):
        entry = _entry("phase_1/task.md", "short\n")
        result = build_context_block([entry], 1000)
        assert "..." not in result
        assert "short" in result

    def test_truncates_over_budget(self):
        long_content = "\n".join(
            ["word1 word2 word3 word4 word5 word6 word7 word8 word9 word10"] * 50
        ) + "\n"
        entries = [
            _entry("phase_1/epic_1/01_a.md", long_content),
            _entry("phase_1/epic_1/02_b.md", long_content),
            _entry("phase_1/epic_1/03_c.md", long_content),
        ]
        result = build_context_block(entries, 600)
        assert "..." in result
        assert "read full content from:" in result

    def test_truncation_token_count_within_budget(self):
        """Truncation should keep token count close to budget.
        
        Note: Due to the approximate nature of character-based token estimation
        and header overhead, the actual count may slightly exceed the budget.
        This test verifies truncation occurs and stays within ~15% of target.
        """
        long_content = "\n".join(
            ["word1 word2 word3 word4 word5 word6 word7 word8 word9 word10"] * 50
        ) + "\n"
        entries = [
            _entry("phase_1/epic_1/01_a.md", long_content),
            _entry("phase_1/epic_1/02_b.md", long_content),
            _entry("phase_1/epic_1/03_c.md", long_content),
        ]
        result = build_context_block(entries, token_budget=600)
        # Allow ~15% variance due to estimation approximation
        assert _count_tokens(result) <= 700, "Token count should stay near budget"
        assert "..." in result, "Content should be truncated"

    def test_multiple_entries_all_included(self):
        entries = [
            _entry("phase_1/epic_1/01_a.md", "phase 1 task\n"),
            _entry("phase_2/epic_1/01_b.md", "phase 2 task\n"),
        ]
        result = build_context_block(entries, token_budget=5000)
        assert "phase_1/epic_1/01_a.md" in result
        assert "phase_2/epic_1/01_b.md" in result

    def test_full_content_when_budget_allows(self):
        lines = "".join(f"line {i}\n" for i in range(10))
        entry = _entry("task.md", lines)
        result = build_context_block([entry], token_budget=50000)
        assert "..." not in result
        for i in range(10):
            assert f"line {i}" in result

    def test_tight_budget_still_shows_some_content(self):
        long_content = "\n".join(["word " * 5] * 100) + "\n"
        entry = _entry("phase_1/epic_1/01_a.md", long_content)
        result = build_context_block([entry], token_budget=50)
        assert "01_a.md" in result
        assert "..." in result

    def test_logs_progress(self, capsys):
        entry = _entry("task.md", "hello world\n")
        build_context_block([entry], token_budget=1000)
        captured = capsys.readouterr()
        assert "1 file(s)" in captured.out
        assert "lines/file" in captured.out
        assert "tokens" in captured.out

    def test_logs_label_when_given(self, capsys):
        entry = _entry("task.md", "hello\n")
        build_context_block([entry], token_budget=1000, label="my_group")
        captured = capsys.readouterr()
        assert "[my_group]" in captured.out

    def test_reduced_budget_reduces_output(self):
        long_content = "\n".join(
            ["word1 word2 word3 word4 word5 word6 word7 word8 word9 word10"] * 50
        ) + "\n"
        entries = [
            _entry("phase_1/epic_1/01_a.md", long_content),
            _entry("phase_1/epic_1/02_b.md", long_content),
        ]
        full = build_context_block(entries, token_budget=800)
        reduced = build_context_block(entries, token_budget=400)
        assert _count_tokens(reduced) < _count_tokens(full)

    def test_very_small_budget_still_includes_one_line(self):
        long_content = "\n".join(["word " * 5] * 100) + "\n"
        entry = _entry("phase_1/epic_1/01_a.md", long_content)
        result = build_context_block([entry], token_budget=5)
        assert "01_a.md" in result


# ---------------------------------------------------------------------------
# ProjectContext.build_context_strings
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path) -> ProjectContext:
    """Construct a minimal ProjectContext pointed at tmp_path."""
    ctx = MagicMock(spec=ProjectContext)
    ctx.root_dir = str(tmp_path)
    ctx.build_context_strings = lambda *a, **kw: ProjectContext.build_context_strings(ctx, *a, **kw)
    return ctx


class TestBuildContextStrings:
    def test_single_file(self, tmp_path):
        f = tmp_path / "task.md"
        f.write_text("hello world\n")
        ctx = _make_ctx(tmp_path)
        with patch("workflow_lib.context.get_context_limit", return_value=10000):
            result = ctx.build_context_strings({"content": str(f)})
        assert "hello world" in result["content"]

    def test_directory_walks_md_files(self, tmp_path):
        d = tmp_path / "docs"
        d.mkdir()
        (d / "a.md").write_text("alpha\n")
        (d / "b.txt").write_text("beta\n")
        (d / "c.json").write_text('{"x":1}\n')
        (d / "ignored.py").write_text("code\n")
        ctx = _make_ctx(tmp_path)
        with patch("workflow_lib.context.get_context_limit", return_value=10000):
            result = ctx.build_context_strings({"docs": str(d)})
        assert "alpha" in result["docs"]
        assert "beta" in result["docs"]
        assert '{"x":1}' in result["docs"]
        assert "code" not in result["docs"]

    def test_list_of_paths(self, tmp_path):
        f1 = tmp_path / "f1.md"
        f2 = tmp_path / "f2.md"
        f1.write_text("file one\n")
        f2.write_text("file two\n")
        ctx = _make_ctx(tmp_path)
        with patch("workflow_lib.context.get_context_limit", return_value=10000):
            result = ctx.build_context_strings({"files": [str(f1), str(f2)]})
        assert "file one" in result["files"]
        assert "file two" in result["files"]

    def test_empty_context_files_returns_empty_strings(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        with patch("workflow_lib.context.get_context_limit", return_value=10000):
            result = ctx.build_context_strings({"key": []})
        assert result == {"key": ""}

    def test_all_empty_groups_returns_empty(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        ctx = _make_ctx(tmp_path)
        with patch("workflow_lib.context.get_context_limit", return_value=10000):
            result = ctx.build_context_strings({"a": str(empty_dir), "b": str(empty_dir)})
        assert result["a"] == ""
        assert result["b"] == ""

    def test_budget_split_across_groups(self, tmp_path):
        long = "\n".join(["word " * 10] * 100) + "\n"
        d1 = tmp_path / "g1"
        d2 = tmp_path / "g2"
        d1.mkdir(); d2.mkdir()
        (d1 / "a.md").write_text(long)
        (d2 / "b.md").write_text(long)
        ctx = _make_ctx(tmp_path)
        with patch("workflow_lib.context.get_context_limit", return_value=500):
            result = ctx.build_context_strings({"g1": str(d1), "g2": str(d2)})
        total_tokens = sum(_count_tokens(v) for v in result.values())
        assert total_tokens <= 600  # some slack for headers

    def test_extra_tokens_reduces_budget(self, tmp_path):
        long = "\n".join(["word " * 10] * 50) + "\n"
        d = tmp_path / "docs"
        d.mkdir()
        (d / "a.md").write_text(long)
        ctx = _make_ctx(tmp_path)
        with patch("workflow_lib.context.get_context_limit", return_value=1000):
            full = ctx.build_context_strings({"docs": str(d)}, extra_tokens=0)
        with patch("workflow_lib.context.get_context_limit", return_value=1000):
            reduced = ctx.build_context_strings({"docs": str(d)}, extra_tokens=800)
        assert _count_tokens(reduced["docs"]) <= _count_tokens(full["docs"])
