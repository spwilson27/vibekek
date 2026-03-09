"""Tests for _collect_task_files and _build_tasks_content in phases.py."""

import os
import sys
import textwrap
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.phases import _collect_task_files, _build_tasks_content


def _make_tasks(tmp_path, file_specs):
    """Create task files under tmp_path/phase_1/epic_1/.

    *file_specs* is a list of (filename, content) tuples.
    """
    tasks_dir = tmp_path / "tasks"
    epic_dir = tasks_dir / "phase_1" / "epic_1"
    epic_dir.mkdir(parents=True)
    for name, content in file_specs:
        (epic_dir / name).write_text(content)
    return str(tasks_dir)


def _make_multi_phase_tasks(tmp_path, phase_specs):
    """Create tasks across multiple phases.

    *phase_specs* is a dict of {phase_id: [(filename, content), ...]}.
    """
    tasks_dir = tmp_path / "tasks"
    for phase_id, files in phase_specs.items():
        epic_dir = tasks_dir / phase_id / "epic_1"
        epic_dir.mkdir(parents=True)
        for name, content in files:
            (epic_dir / name).write_text(content)
    return str(tasks_dir)


class TestCollectTaskFiles:
    def test_collects_all_md_files(self, tmp_path):
        tasks_dir = _make_tasks(tmp_path, [
            ("01_foo.md", "foo content\n"),
            ("02_bar.md", "bar content\n"),
            ("not_a_task.txt", "ignored\n"),
        ])
        result = _collect_task_files(tasks_dir, ["phase_1"])
        ids = [t["task_id"] for t in result]
        assert ids == ["phase_1/epic_1/01_foo.md", "phase_1/epic_1/02_bar.md"]

    def test_empty_phase_dir(self, tmp_path):
        tasks_dir = tmp_path / "tasks" / "phase_1"
        tasks_dir.mkdir(parents=True)
        result = _collect_task_files(str(tmp_path / "tasks"), ["phase_1"])
        assert result == []

    def test_preserves_lines(self, tmp_path):
        content = "line1\nline2\nline3\n"
        tasks_dir = _make_tasks(tmp_path, [("01_t.md", content)])
        result = _collect_task_files(tasks_dir, ["phase_1"])
        assert len(result) == 1
        assert result[0]["lines"] == ["line1\n", "line2\n", "line3\n"]


class TestBuildTasksContent:
    def test_no_truncation_when_under_budget(self, tmp_path):
        tasks_dir = _make_tasks(tmp_path, [
            ("01_a.md", "short\n"),
        ])
        content = _build_tasks_content(tasks_dir, ["phase_1"], max_words=1000)
        assert "..." not in content
        assert "short" in content

    def test_includes_task_id_and_file_path(self, tmp_path):
        tasks_dir = _make_tasks(tmp_path, [
            ("01_a.md", "hello\n"),
        ])
        content = _build_tasks_content(tasks_dir, ["phase_1"], max_words=1000)
        assert "phase_1/epic_1/01_a.md" in content
        assert "File: docs/plan/tasks/phase_1/epic_1/01_a.md" in content

    def test_truncates_when_over_budget(self, tmp_path):
        # Each line has ~10 words. 50 lines = ~500 words per file.
        # 3 files = ~1500 words at full. Budget of 600 should force truncation.
        long_content = "\n".join(
            [f"word1 word2 word3 word4 word5 word6 word7 word8 word9 word10"] * 50
        ) + "\n"
        tasks_dir = _make_tasks(tmp_path, [
            ("01_a.md", long_content),
            ("02_b.md", long_content),
            ("03_c.md", long_content),
        ])
        content = _build_tasks_content(tasks_dir, ["phase_1"], max_words=600)
        assert "..." in content
        assert "use file tools to read full content" in content
        # Verify word count is under budget
        word_count = len(content.split())
        assert word_count <= 600

    def test_returns_empty_for_no_tasks(self, tmp_path):
        tasks_dir = tmp_path / "tasks" / "phase_1"
        tasks_dir.mkdir(parents=True)
        result = _build_tasks_content(str(tmp_path / "tasks"), ["phase_1"])
        assert result == ""

    def test_multiple_phases(self, tmp_path):
        tasks_dir = _make_multi_phase_tasks(tmp_path, {
            "phase_1": [("01_a.md", "phase 1 task\n")],
            "phase_2": [("01_b.md", "phase 2 task\n")],
        })
        content = _build_tasks_content(tasks_dir, ["phase_1", "phase_2"], max_words=5000)
        assert "phase_1/epic_1/01_a.md" in content
        assert "phase_2/epic_1/01_b.md" in content

    def test_full_content_when_budget_allows(self, tmp_path):
        lines = [f"line {i}\n" for i in range(10)]
        content_str = "".join(lines)
        tasks_dir = _make_tasks(tmp_path, [("01_a.md", content_str)])
        content = _build_tasks_content(tasks_dir, ["phase_1"], max_words=50000)
        # All lines should be present, no truncation marker
        assert "..." not in content
        for i in range(10):
            assert f"line {i}" in content

    def test_logs_summary(self, tmp_path, capsys):
        tasks_dir = _make_tasks(tmp_path, [
            ("01_a.md", "hello world\n"),
        ])
        _build_tasks_content(tasks_dir, ["phase_1"], max_words=1000)
        captured = capsys.readouterr()
        assert "1 files" in captured.out
        assert "lines/task" in captured.out
        assert "words" in captured.out

    def test_very_tight_budget_still_includes_one_line(self, tmp_path):
        long_content = "\n".join(["word " * 5] * 100) + "\n"
        tasks_dir = _make_tasks(tmp_path, [("01_a.md", long_content)])
        # Budget big enough for 1 line but not all 100
        content = _build_tasks_content(tasks_dir, ["phase_1"], max_words=50)
        # Should still have some content
        assert "01_a.md" in content
        assert "..." in content

    def test_extra_words_reserves_budget(self, tmp_path):
        """extra_words reduces space available for task content."""
        long_content = "\n".join(
            ["word1 word2 word3 word4 word5 word6 word7 word8 word9 word10"] * 50
        ) + "\n"
        tasks_dir = _make_tasks(tmp_path, [
            ("01_a.md", long_content),
            ("02_b.md", long_content),
        ])
        # With no extra_words, 800 is enough for decent content
        full = _build_tasks_content(tasks_dir, ["phase_1"], max_words=800, extra_words=0)
        # With 400 extra_words reserved, less task content fits
        reduced = _build_tasks_content(tasks_dir, ["phase_1"], max_words=800, extra_words=400)
        assert len(reduced.split()) < len(full.split())

    def test_extra_words_causes_truncation(self, tmp_path):
        """Large extra_words value forces truncation even with generous max_words."""
        long_content = "\n".join(
            ["word1 word2 word3 word4 word5"] * 50
        ) + "\n"
        tasks_dir = _make_tasks(tmp_path, [("01_a.md", long_content)])
        # max_words is big but extra_words eats most of the budget
        content = _build_tasks_content(tasks_dir, ["phase_1"], max_words=5000, extra_words=4800)
        assert "..." in content
        assert len(content.split()) <= 250

    def test_uses_context_limit_from_config(self, tmp_path):
        """When max_words is not passed, it reads context_limit from config."""
        long_content = "\n".join(
            ["word1 word2 word3 word4 word5 word6 word7 word8 word9 word10"] * 50
        ) + "\n"
        tasks_dir = _make_tasks(tmp_path, [
            ("01_a.md", long_content),
            ("02_b.md", long_content),
        ])
        # With a very small config limit, output should be truncated
        with patch("workflow_lib.phases.get_context_limit", return_value=100):
            small = _build_tasks_content(tasks_dir, ["phase_1"])
        # With a large config limit, output should be untruncated
        with patch("workflow_lib.phases.get_context_limit", return_value=50000):
            large = _build_tasks_content(tasks_dir, ["phase_1"])
        assert len(small.split()) < len(large.split())

    def test_explicit_max_words_overrides_config(self, tmp_path):
        """Explicit max_words takes precedence over config."""
        long_content = "\n".join(
            ["word1 word2 word3 word4 word5 word6 word7 word8 word9 word10"] * 50
        ) + "\n"
        tasks_dir = _make_tasks(tmp_path, [
            ("01_a.md", long_content),
        ])
        # Config says 50000, but explicit max_words=100 should win
        with patch("workflow_lib.phases.get_context_limit", return_value=50000):
            content = _build_tasks_content(tasks_dir, ["phase_1"], max_words=100)
        assert "..." in content
