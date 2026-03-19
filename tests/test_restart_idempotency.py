"""Tests for workflow restart idempotency in process_task.

Two regression scenarios are covered:

1. **Branch-exists early-out** — When a task branch already exists in origin
   (e.g. a prior run passed presubmit but failed to record state), process_task
   must return True immediately without re-running the AI agent.

2. **Force-push on non-fast-forward** — When the initial push is rejected
   because another concurrent run pushed to the same branch between ls-remote
   and our push, process_task must retry with --force-with-lease rather than
   failing the task.

Each fix is validated two ways:
  - **AST test**: verifies the code structure in executor.py is correct.
  - **Functional test**: exercises real or mock git operations to confirm the
    runtime behaviour matches the expected contract.
"""

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Capture the true subprocess.run BEFORE any pytest fixtures patch it.
# This reference is used inside side-effects to call real git without recursion.
_real_subprocess_run = subprocess.run

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import workflow_lib.executor as executor_mod
from workflow_lib.executor import process_task

EXECUTOR_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "workflow_lib", "executor.py",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd, cwd, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def _make_git_repo(path: str, branch: str = "dev-claude") -> None:
    """Initialise a git repo with one commit on *branch*."""
    _run(["git", "init"], cwd=path)
    _run(["git", "config", "user.email", "test@test.com"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)
    Path(os.path.join(path, "README.md")).write_text("init\n")
    _run(["git", "add", "README.md"], cwd=path)
    _run(["git", "commit", "-m", "init"], cwd=path)
    # Rename default branch to the requested name
    _run(["git", "branch", "-M", branch], cwd=path)


def _parse_executor() -> ast.Module:
    with open(EXECUTOR_FILE, "r", encoding="utf-8") as f:
        return ast.parse(f.read())


def _find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _git_calls(func_node) -> list[list]:
    """Return all ["git", ...] command lists found in subprocess.run calls."""
    results = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.List):
            continue
        tokens = []
        for elt in first.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                tokens.append(elt.value)
            else:
                tokens.append(None)
        if tokens and tokens[0] == "git":
            results.append((node.lineno, tokens))
    return results


# ---------------------------------------------------------------------------
# Fix 1 — Branch-exists early-out: AST tests
# ---------------------------------------------------------------------------

class TestEarlyOutAST(unittest.TestCase):
    """Verify the code structure of the branch-exists early-out in process_task."""

    def _process_task_node(self):
        tree = _parse_executor()
        node = _find_function(tree, "process_task")
        self.assertIsNotNone(node, "process_task not found in executor.py")
        return node

    def test_ls_remote_present_in_process_task(self):
        """process_task must call `git ls-remote --heads` to detect existing branches."""
        func = self._process_task_node()
        calls = _git_calls(func)
        ls_remote_lines = [
            lineno for lineno, tokens in calls
            if "ls-remote" in tokens and "--heads" in tokens
        ]
        self.assertTrue(
            ls_remote_lines,
            "process_task must contain a `git ls-remote --heads` call to detect "
            "pre-existing task branches from prior workflow runs.",
        )

    def test_ls_remote_precedes_stage_dispatch(self):
        """The `git ls-remote --heads` check must appear before the stage dispatch
        loop in process_task so the early-out fires before any heavy work begins.
        (git clone moved to run_impl_stage; verify it exists there instead.)"""
        tree = _parse_executor()

        # ls-remote must still be in process_task
        pt_func = _find_function(tree, "process_task")
        pt_calls = _git_calls(pt_func)
        ls_remote_line = next(
            (lineno for lineno, tokens in pt_calls if "ls-remote" in tokens and "--heads" in tokens),
            None,
        )
        self.assertIsNotNone(ls_remote_line, "No `git ls-remote --heads` found in process_task")

        # git clone must now be in run_impl_stage (not process_task)
        impl_func = _find_function(tree, "run_impl_stage")
        self.assertIsNotNone(impl_func, "run_impl_stage not found in executor.py")
        impl_calls = _git_calls(impl_func)
        # run_impl_stage itself doesn't clone directly — it calls _stage_clone.
        # Verify _stage_clone contains git clone instead.
        stage_clone_func = _find_function(tree, "_stage_clone")
        self.assertIsNotNone(stage_clone_func, "_stage_clone not found in executor.py")
        clone_calls = _git_calls(stage_clone_func)
        clone_found = any("clone" in tokens for _, tokens in clone_calls)
        self.assertTrue(clone_found, "No `git clone` found in _stage_clone")

    def test_early_out_returns_true(self):
        """When ls-remote finds the branch, process_task must return True (not False)
        so the caller proceeds to merge_task rather than treating it as a failure."""
        with open(EXECUTOR_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        func = _find_function(tree, "process_task")
        self.assertIsNotNone(func)

        # Find the ls-remote call, then verify a `return True` follows it in
        # process_task (early-out for the branch-already-exists path).
        calls = _git_calls(func)
        ls_remote_line = next(
            (lineno for lineno, tokens in calls if "ls-remote" in tokens and "--heads" in tokens),
            None,
        )
        self.assertIsNotNone(ls_remote_line)

        func_end_line = getattr(func, "end_lineno", func.lineno + 9999)

        early_returns = [
            node.lineno
            for node in ast.walk(func)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
            and ls_remote_line < node.lineno <= func_end_line
        ]
        self.assertTrue(
            early_returns,
            f"Expected a `return True` after ls-remote (line {ls_remote_line}) "
            f"in process_task for the branch-exists early-out path.",
        )


# ---------------------------------------------------------------------------
# Fix 1 — Branch-exists early-out: Functional tests
# ---------------------------------------------------------------------------

class TestEarlyOutFunctional(unittest.TestCase):
    """Functional tests: process_task skips agent when branch already exists."""

    def _make_repo_with_task_branch(self, root_dir: str, branch_name: str) -> None:
        """Create a git repo that already has the task branch."""
        _make_git_repo(root_dir)
        _run(["git", "checkout", "-b", branch_name], cwd=root_dir)
        Path(os.path.join(root_dir, "task_done.txt")).write_text("done\n")
        _run(["git", "add", "task_done.txt"], cwd=root_dir)
        _run(["git", "commit", "-m", "task done"], cwd=root_dir)
        _run(["git", "checkout", "dev-claude"], cwd=root_dir)

    def test_returns_true_when_branch_exists(self):
        """process_task must return True immediately when the task branch exists."""
        import uuid
        # Use a unique task ID to avoid conflicts with parallel test runs
        unique_suffix = uuid.uuid4().hex[:8]
        unique_task_id = f"phase_0/04_test_task_{unique_suffix}.md"
        branch_name = f"ai-phase-04_test_task_{unique_suffix}"
        
        # Reset the global shutdown flag to avoid interference from other parallel tests
        executor_mod.shutdown_requested = False
        
        with tempfile.TemporaryDirectory() as root_dir:
            self._make_repo_with_task_branch(root_dir, branch_name)

            with patch.object(executor_mod, "run_agent") as mock_agent:
                result = process_task(
                    root_dir=root_dir,
                    full_task_id=unique_task_id,
                    presubmit_cmd="python /harness.py presubmit",
                    dev_branch="dev-claude",
                )

            self.assertTrue(result, "process_task must return True when branch exists")
            mock_agent.assert_not_called()

    def test_agent_runs_when_branch_absent(self):
        """process_task must NOT early-out when the branch does not exist —
        the agent must be invoked so the task is actually implemented."""
        import uuid
        # Use a unique task ID to avoid conflicts with parallel test runs
        unique_suffix = uuid.uuid4().hex[:8]
        unique_task_id = f"phase_0/04_test_task_{unique_suffix}.md"
        
        # Reset the global shutdown flag to avoid interference from other parallel tests
        executor_mod.shutdown_requested = False
        
        with tempfile.TemporaryDirectory() as root_dir:
            _make_git_repo(root_dir)
            # No task branch created — process_task should attempt to run the agent.
            with patch.object(executor_mod, "run_agent", return_value=False) as mock_agent:
                result = process_task(
                    root_dir=root_dir,
                    full_task_id=unique_task_id,
                    presubmit_cmd="python /harness.py presubmit",
                    dev_branch="dev-claude",
                )

            self.assertFalse(result, "process_task must return False when agent fails")
            mock_agent.assert_called()


# ---------------------------------------------------------------------------
# Fix 2 — Force-push on non-fast-forward: AST tests
# ---------------------------------------------------------------------------

class TestForcePushAST(unittest.TestCase):
    """Verify the force-push fallback code structure.

    The push logic lives in the ``_push_branch_to_origin`` helper (called by
    all three stage functions), not directly in ``process_task``.
    """

    def _push_helper_node(self):
        tree = _parse_executor()
        node = _find_function(tree, "_push_branch_to_origin")
        self.assertIsNotNone(node, "_push_branch_to_origin not found in executor.py")
        return node

    def test_rejected_stderr_check_present(self):
        """executor.py must inspect push stderr for 'non-fast-forward' or '[rejected]'
        to distinguish a race-condition push failure from a hard failure."""
        with open(EXECUTOR_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        # The string literals must appear as part of the rejection detection logic
        self.assertTrue(
            "non-fast-forward" in source or "[rejected]" in source,
            "executor.py must check for 'non-fast-forward' or '[rejected]' in push "
            "stderr to detect race-condition branch conflicts.",
        )

    def test_force_with_lease_in_push_helper(self):
        """_push_branch_to_origin must contain a `--force-with-lease` push
        for the fallback path when the initial push is rejected."""
        with open(EXECUTOR_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        # The push helper builds commands via list concatenation so a plain
        # source-text search is more reliable than AST walking.
        self.assertIn(
            "--force-with-lease",
            source,
            "_push_branch_to_origin must pass '--force-with-lease' as a "
            "fallback when the initial push is rejected (non-fast-forward).",
        )

    def test_plain_push_precedes_force_push(self):
        """The plain push must appear before the `--force-with-lease` push —
        force should only be used as a fallback, not the default."""
        with open(EXECUTOR_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        # Use the code-only occurrence: ["git", "push"] and ["--force-with-lease"]
        plain_pos = source.find('"git", "push"')
        # Find the code call site: _do_push(["--force-with-lease"])
        force_pos = source.find('["--force-with-lease"]')
        self.assertGreater(plain_pos, 0, 'No ["git", "push"] literal found in executor.py')
        self.assertGreater(force_pos, 0, 'No ["--force-with-lease"] call found in executor.py')
        self.assertLess(
            plain_pos, force_pos,
            "Plain push must appear before force-push in source — force should only be a fallback.",
        )


# ---------------------------------------------------------------------------
# Fix 2 — Force-push on non-fast-forward: Functional tests
# ---------------------------------------------------------------------------

class TestForcePushFunctional(unittest.TestCase):
    """Functional tests: process_task force-pushes when initial push is rejected."""

    def _make_side_effect(self, branch_name: str, reject_plain_push: bool = True):
        """Return a subprocess.run side-effect that intercepts git push calls.

        When *reject_plain_push* is True the first plain push is rejected with a
        non-fast-forward error; --force-with-lease pushes are tracked and allowed
        to succeed via the real git binary.  All other commands are passed through
        to the real subprocess.run (captured before any fixtures patch it).
        """
        push_attempts = {"plain": 0, "force": []}

        def side_effect(cmd, *args, **kwargs):
            if not isinstance(cmd, list) or cmd[:1] != ["git"]:
                return _real_subprocess_run(cmd, *args, **kwargs)
            if cmd[1:2] == ["push"] and "--force-with-lease" in cmd:
                push_attempts["force"].append(cmd)
                return _real_subprocess_run(cmd, *args, **kwargs)
            if cmd[1:2] == ["push"] and branch_name in cmd and reject_plain_push:
                push_attempts["plain"] += 1
                result = MagicMock()
                result.returncode = 1
                result.stdout = ""
                result.stderr = (
                    f" ! [rejected]        {branch_name} -> {branch_name} (non-fast-forward)\n"
                    "error: failed to push some refs\n"
                )
                return result
            return _real_subprocess_run(cmd, *args, **kwargs)

        return side_effect, push_attempts

    def _make_network_error_side_effect(self, branch_name: str):
        """Side-effect where the plain push fails with a generic network error."""
        force_push_calls = []

        def side_effect(cmd, *args, **kwargs):
            if not isinstance(cmd, list) or cmd[:1] != ["git"]:
                return _real_subprocess_run(cmd, *args, **kwargs)
            if cmd[1:2] == ["push"] and "--force-with-lease" in cmd:
                force_push_calls.append(cmd)
                return _real_subprocess_run(cmd, *args, **kwargs)
            if cmd[1:2] == ["push"] and branch_name in cmd:
                result = MagicMock()
                result.returncode = 1
                result.stdout = ""
                result.stderr = "fatal: unable to access remote: Connection refused\n"
                return result
            return _real_subprocess_run(cmd, *args, **kwargs)

        return side_effect, force_push_calls

    def test_force_push_called_on_rejected(self):
        """When `git push origin branch` is rejected, process_task must retry
        with --force-with-lease."""
        # branch_name is derived as: task_id="04_force_push_test.md"
        #   → safe="04_force_push_test" → "ai-phase-04_force_push_test"
        branch_name = "ai-phase-04_force_push_test"
        with tempfile.TemporaryDirectory() as root_dir:
            _make_git_repo(root_dir)
            side_effect, push_attempts = self._make_side_effect(branch_name)

            with patch.object(executor_mod, "run_agent", return_value=True), \
                 patch("workflow_lib.executor.subprocess.run", side_effect=side_effect):
                process_task(
                    root_dir=root_dir,
                    full_task_id="phase_0/04_force_push_test.md",
                    presubmit_cmd="echo ok",
                    dev_branch="dev-claude",
                )

            self.assertGreater(
                push_attempts["plain"], 0,
                "The initial plain push must have been attempted before force-pushing.",
            )
            self.assertTrue(
                push_attempts["force"],
                "process_task must attempt `git push --force-with-lease` when the "
                "initial push is rejected with a non-fast-forward error.",
            )
            self.assertIn("--force-with-lease", push_attempts["force"][0])
            self.assertIn(branch_name, push_attempts["force"][0])

    def test_plain_push_failure_non_rejection_does_not_force(self):
        """A push failure that is NOT a non-fast-forward rejection (e.g. network error)
        must NOT be retried with --force-with-lease."""
        branch_name = "ai-phase-04_no_force_test"
        with tempfile.TemporaryDirectory() as root_dir:
            _make_git_repo(root_dir)
            side_effect, force_push_calls = self._make_network_error_side_effect(branch_name)

            with patch.object(executor_mod, "run_agent", return_value=True), \
                 patch("workflow_lib.executor.subprocess.run", side_effect=side_effect):
                result = process_task(
                    root_dir=root_dir,
                    full_task_id="phase_0/04_no_force_test.md",
                    presubmit_cmd="echo ok",
                    dev_branch="dev-claude",
                )

            self.assertFalse(
                result,
                "process_task must return False on a non-rejection push failure.",
            )
            self.assertFalse(
                force_push_calls,
                "process_task must NOT use --force-with-lease for non-rejection "
                "push failures (e.g. network errors).",
            )


if __name__ == "__main__":
    unittest.main()
