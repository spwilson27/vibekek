"""Tests for the dev-branch guard in cmd_run.

cmd_run should exit with code 1 (and print an error) when the current git
branch matches the configured dev_branch, because pushing to a checked-out
branch is not allowed.
"""

import argparse
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.cli import cmd_run


def _make_args(**kwargs):
    defaults = dict(jobs=1, presubmit_cmd="python /harness.py presubmit", backend="gemini", model=None, cleanup=False)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _git_branch_result(branch: str) -> MagicMock:
    r = MagicMock()
    r.stdout = branch + "\n"
    return r


class TestCmdRunDevBranchGuard(unittest.TestCase):

    @patch("workflow_lib.cli.get_dev_branch", return_value="dev-claude")
    @patch("workflow_lib.cli.subprocess.run")
    def test_exits_when_on_dev_branch(self, mock_subproc, _mock_dev_branch):
        """cmd_run should sys.exit(1) when the current branch is the dev branch."""
        mock_subproc.return_value = _git_branch_result("dev-claude")

        with self.assertRaises(SystemExit) as cm:
            cmd_run(_make_args())

        self.assertEqual(cm.exception.code, 1)

    @patch("workflow_lib.cli.get_dev_branch", return_value="dev-claude")
    @patch("workflow_lib.cli.subprocess.run")
    def test_error_message_mentions_branch(self, mock_subproc, _mock_dev_branch):
        """The error message should include the dev branch name."""
        mock_subproc.return_value = _git_branch_result("dev-claude")

        import io
        fake_stderr = io.StringIO()
        with patch("sys.stderr", fake_stderr):
            with self.assertRaises(SystemExit):
                cmd_run(_make_args())

        self.assertIn("dev-claude", fake_stderr.getvalue())

    @patch("workflow_lib.cli.restore_state_from_branch")
    @patch("workflow_lib.cli.load_workflow_state", return_value={"completed_tasks": [], "merged_tasks": []})
    @patch("workflow_lib.cli.load_dags", return_value={})
    @patch("workflow_lib.cli.execute_dag")
    @patch("workflow_lib.cli.get_serena_enabled", return_value=False)
    @patch("workflow_lib.cli.get_tasks_dir", return_value="/fake/tasks")
    @patch("workflow_lib.cli.get_dev_branch", return_value="dev-claude")
    @patch("workflow_lib.cli.subprocess.run")
    def test_does_not_exit_when_on_different_branch(
        self, mock_subproc, _mock_dev_branch, _mock_tasks_dir,
        _mock_serena, mock_execute_dag, _mock_load_dags,
        _mock_load_state, _mock_restore,
    ):
        """cmd_run should not exit early when on a branch other than dev_branch."""
        from unittest.mock import mock_open
        mock_subproc.return_value = _git_branch_result("feature-branch")

        with patch("builtins.open", mock_open()):
            cmd_run(_make_args())

        mock_execute_dag.assert_called_once()


if __name__ == "__main__":
    unittest.main()
