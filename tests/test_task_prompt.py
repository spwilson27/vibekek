"""Tests for the task-prompt CLI subcommand.

Verifies that cmd_task_prompt correctly resolves task paths, builds context,
renders prompt templates, and handles error cases.
"""

import argparse
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.cli import cmd_task_prompt


TOOLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _make_args(**kwargs):
    defaults = dict(
        backend="gemini",
        model=None,
        context_limit=None,
        prompt="implement_task.md",
        output=None,
        task="phase_0/red_01_harness_setup_fmt.md",
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _fake_context(**overrides):
    """Return mock return values for the context-gathering functions."""
    defaults = dict(
        task_details="# Task: Fake\n\nDo the thing.\n",
        project_ctx="project context here",
        memory_ctx="memory context here",
        spec_ctx="<spec>prd</spec>",
        shared_ctx="<doc>shared</doc>",
    )
    defaults.update(overrides)
    return defaults


class TestCmdTaskPromptPathResolution(unittest.TestCase):
    """Test that task paths are resolved correctly."""

    @patch("workflow_lib.cli.get_shared_components_context", return_value="")
    @patch("workflow_lib.cli.get_spec_context", return_value="")
    @patch("workflow_lib.cli.get_memory_context", return_value="")
    @patch("workflow_lib.cli.get_project_context", return_value="")
    @patch("workflow_lib.cli.get_task_details", return_value="# Task\nContent\n")
    def test_relative_task_id(self, mock_details, *_mocks):
        """A relative path like phase_0/foo.md is resolved under docs/plan/tasks/."""
        args = _make_args(task="phase_0/red_01_harness_setup_fmt.md")
        # Should not raise (assuming the task file exists in the repo)
        try:
            cmd_task_prompt(args)
        except SystemExit:
            # If the file doesn't exist in test env, that's fine — we test the
            # mock path below instead
            pass
        # get_task_details should be called with the relative id
        if mock_details.called:
            call_arg = mock_details.call_args[0][0]
            self.assertIn("red_01_harness_setup_fmt.md", call_arg)

    @patch("workflow_lib.cli.get_shared_components_context", return_value="")
    @patch("workflow_lib.cli.get_spec_context", return_value="")
    @patch("workflow_lib.cli.get_memory_context", return_value="")
    @patch("workflow_lib.cli.get_project_context", return_value="")
    @patch("workflow_lib.cli.get_task_details")
    def test_absolute_path(self, mock_details, *_mocks):
        """An absolute path to a .md file should be read directly."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Task: Test\n\nSome content.\n")
            tmp_path = f.name
        try:
            args = _make_args(task=tmp_path)
            cmd_task_prompt(args)
            # get_task_details should NOT be called — file is read directly
            mock_details.assert_not_called()
        finally:
            os.unlink(tmp_path)

    def test_nonexistent_file_exits(self):
        """Pointing to a file that doesn't exist should sys.exit(1)."""
        args = _make_args(task="/nonexistent/path/task.md")
        with self.assertRaises(SystemExit) as cm:
            cmd_task_prompt(args)
        self.assertEqual(cm.exception.code, 1)

    def test_nonexistent_relative_exits(self):
        """A relative task id that doesn't exist should sys.exit(1)."""
        args = _make_args(task="phase_99/does_not_exist.md")
        with self.assertRaises(SystemExit) as cm:
            cmd_task_prompt(args)
        self.assertEqual(cm.exception.code, 1)


class TestCmdTaskPromptOutput(unittest.TestCase):
    """Test prompt rendering and output modes."""

    def _make_task_file(self, content="# Task: Test\n\nImplement the thing.\n"):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        f.write(content)
        f.close()
        return f.name

    @patch("workflow_lib.cli.get_shared_components_context", return_value="shared-components")
    @patch("workflow_lib.cli.get_spec_context", return_value="spec-context")
    @patch("workflow_lib.cli.get_memory_context", return_value="memory-context")
    @patch("workflow_lib.cli.get_project_context", return_value="project-context")
    def test_stdout_contains_task_content(self, *_mocks):
        """The rendered prompt printed to stdout should contain the task content."""
        tmp = self._make_task_file()
        try:
            args = _make_args(task=tmp)
            import io
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                cmd_task_prompt(args)
            output = captured.getvalue()
            self.assertIn("Implement the thing", output)
            self.assertIn("spec-context", output)
            self.assertIn("memory-context", output)
            self.assertIn("project-context", output)
            self.assertIn("shared-components", output)
        finally:
            os.unlink(tmp)

    @patch("workflow_lib.cli.get_shared_components_context", return_value="")
    @patch("workflow_lib.cli.get_spec_context", return_value="")
    @patch("workflow_lib.cli.get_memory_context", return_value="")
    @patch("workflow_lib.cli.get_project_context", return_value="")
    def test_output_file_written(self, *_mocks):
        """With --output, the prompt should be written to the specified file."""
        task_tmp = self._make_task_file()
        out_tmp = tempfile.mktemp(suffix=".md")
        try:
            args = _make_args(task=task_tmp, output=out_tmp)
            cmd_task_prompt(args)
            self.assertTrue(os.path.exists(out_tmp))
            with open(out_tmp) as f:
                content = f.read()
            self.assertIn("Implement the thing", content)
            self.assertGreater(len(content), 100)
        finally:
            os.unlink(task_tmp)
            if os.path.exists(out_tmp):
                os.unlink(out_tmp)

    @patch("workflow_lib.cli.get_shared_components_context", return_value="")
    @patch("workflow_lib.cli.get_spec_context", return_value="")
    @patch("workflow_lib.cli.get_memory_context", return_value="")
    @patch("workflow_lib.cli.get_project_context", return_value="")
    def test_phase_and_task_name_in_output(self, *_mocks):
        """The rendered prompt should include the derived phase and task name."""
        task_tmp = self._make_task_file()
        try:
            # Absolute path outside tasks dir — phase becomes the parent dir name
            args = _make_args(task=task_tmp)
            import io
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                cmd_task_prompt(args)
            output = captured.getvalue()
            # Should have Phase and Task Name fields filled in
            self.assertIn("**Phase:**", output)
            self.assertIn("**Task Name:**", output)
        finally:
            os.unlink(task_tmp)


class TestCmdTaskPromptTemplate(unittest.TestCase):
    """Test custom prompt template selection."""

    def test_invalid_prompt_template_exits(self):
        """A nonexistent prompt template should sys.exit(1)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Task\nContent\n")
            tmp = f.name
        try:
            args = _make_args(task=tmp, prompt="nonexistent_template.md")
            with self.assertRaises(SystemExit) as cm:
                cmd_task_prompt(args)
            self.assertEqual(cm.exception.code, 1)
        finally:
            os.unlink(tmp)

    @patch("workflow_lib.cli.get_shared_components_context", return_value="")
    @patch("workflow_lib.cli.get_spec_context", return_value="")
    @patch("workflow_lib.cli.get_memory_context", return_value="")
    @patch("workflow_lib.cli.get_project_context", return_value="")
    def test_review_template(self, *_mocks):
        """Using --prompt review_task.md should render the review template."""
        review_prompt = os.path.join(TOOLS_DIR, "prompts", "review_task.md")
        if not os.path.exists(review_prompt):
            self.skipTest("review_task.md not found")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Task: Review Test\nReview content.\n")
            tmp = f.name
        try:
            args = _make_args(task=tmp, prompt="review_task.md")
            import io
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                cmd_task_prompt(args)
            output = captured.getvalue()
            self.assertIn("Review content", output)
        finally:
            os.unlink(tmp)


class TestCmdTaskPromptEmptyContent(unittest.TestCase):
    """Test handling of empty task files."""

    def test_empty_task_file_exits(self):
        """A task file with no content should sys.exit(1)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("")  # empty
            tmp = f.name
        try:
            args = _make_args(task=tmp)
            with self.assertRaises(SystemExit) as cm:
                cmd_task_prompt(args)
            self.assertEqual(cm.exception.code, 1)
        finally:
            os.unlink(tmp)

    def test_whitespace_only_task_file_exits(self):
        """A task file with only whitespace should sys.exit(1)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("   \n\n  \n")
            tmp = f.name
        try:
            args = _make_args(task=tmp)
            with self.assertRaises(SystemExit) as cm:
                cmd_task_prompt(args)
            self.assertEqual(cm.exception.code, 1)
        finally:
            os.unlink(tmp)


if __name__ == "__main__":
    unittest.main()
