"""Test that workflow operations never change the git branch of the project root.

The regression this guards against:

    git rebase origin/<dev_branch> <dev_branch>

When called with cwd=root_dir, the two-argument form of `git rebase` performs
an implicit `git checkout <dev_branch>` before rebasing.  This silently changes
the developer's working branch (e.g. from 'claude' to 'dev-claude') mid-run.

The fix is to use `git fetch origin branch:branch` (the refspec form), which
updates the local branch ref directly using git plumbing — no checkout needed.

These tests verify both the static code structure (AST) and the runtime
behaviour (real git repos).
"""

import ast
import os
import subprocess
import tempfile
import unittest

EXECUTOR_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "workflow_lib", "executor.py",
)


def _git_subcommand(call_node: ast.Call) -> list[str] | None:
    if not call_node.args:
        return None
    first = call_node.args[0]
    if not isinstance(first, ast.List):
        return None
    tokens = []
    for elt in first.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            tokens.append(elt.value)
        else:
            tokens.append(None)
    if not tokens or tokens[0] != "git":
        return None
    return tokens


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _run(cmd, cwd, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# AST-based structural tests
# ---------------------------------------------------------------------------

class TestNoRootCheckoutInExecutor(unittest.TestCase):
    """Verify executor.py never issues git commands that implicitly check out a
    branch when operating on root_dir (the host working tree)."""

    def _ast(self):
        with open(EXECUTOR_FILE, "r", encoding="utf-8") as f:
            return ast.parse(f.read())

    def test_no_git_checkout_with_root_dir_cwd(self):
        """No `subprocess.run(["git", "checkout", ...], cwd=root_dir)` anywhere
        in executor.py.  All checkout operations must use isolated tmpdir clones.
        """
        tree = self._ast()
        for func_node in ast.walk(tree):
            if not isinstance(func_node, ast.FunctionDef):
                continue
            for node in ast.walk(func_node):
                if not isinstance(node, ast.Call):
                    continue
                tokens = _git_subcommand(node)
                if not (tokens and len(tokens) >= 2 and tokens[1] == "checkout"):
                    continue
                cwd_is_root = any(
                    kw.arg == "cwd"
                    and isinstance(kw.value, ast.Name)
                    and kw.value.id == "root_dir"
                    for kw in node.keywords
                )
                self.assertFalse(
                    cwd_is_root,
                    f"Found `git checkout` with cwd=root_dir at line {node.lineno} "
                    f"in function '{func_node.name}'. All checkout operations must "
                    f"use an isolated tmpdir clone, never the host working tree.",
                )

    def test_no_rebase_with_root_dir_cwd(self):
        """No `subprocess.run(["git", "rebase", ...], cwd=root_dir)` anywhere
        in executor.py.  The two-argument rebase form implicitly checks out the
        branch; use `git fetch origin branch:branch` instead.
        """
        tree = self._ast()
        for func_node in ast.walk(tree):
            if not isinstance(func_node, ast.FunctionDef):
                continue
            for node in ast.walk(func_node):
                if not isinstance(node, ast.Call):
                    continue
                tokens = _git_subcommand(node)
                if not (tokens and len(tokens) >= 2 and tokens[1] == "rebase"):
                    continue
                cwd_is_root = any(
                    kw.arg == "cwd"
                    and isinstance(kw.value, ast.Name)
                    and kw.value.id == "root_dir"
                    for kw in node.keywords
                )
                self.assertFalse(
                    cwd_is_root,
                    f"Found `git rebase` with cwd=root_dir at line {node.lineno} "
                    f"in function '{func_node.name}'. The two-argument form "
                    f"`git rebase <upstream> <branch>` implicitly checks out "
                    f"<branch>, disturbing the host working tree. Use "
                    f"`git fetch origin branch:branch` to update the local ref "
                    f"without a checkout.",
                )


# ---------------------------------------------------------------------------
# Functional tests: real git repos
# ---------------------------------------------------------------------------

class TestFetchRefspecDoesNotChangeHead(unittest.TestCase):
    """Demonstrate that `git fetch origin branch:branch` updates the local ref
    without changing HEAD, unlike `git rebase <upstream> <branch>`."""

    def _setup_repos(self, tmp_path: str):
        remote = os.path.join(tmp_path, "remote.git")
        local = os.path.join(tmp_path, "local")
        os.makedirs(remote)
        os.makedirs(local)
        _run(["git", "init", "--bare", remote], cwd=tmp_path)
        _run(["git", "init"], cwd=local)
        _run(["git", "config", "user.email", "test@test.com"], cwd=local)
        _run(["git", "config", "user.name", "Test"], cwd=local)
        with open(os.path.join(local, "README.md"), "w") as f:
            f.write("init\n")
        _run(["git", "add", "README.md"], cwd=local)
        _run(["git", "commit", "-m", "init"], cwd=local)
        _run(["git", "remote", "add", "origin", remote], cwd=local)
        # Create both 'main' and 'dev' branches and push both
        _run(["git", "branch", "-M", "main"], cwd=local)
        _run(["git", "push", "-u", "origin", "main"], cwd=local)
        _run(["git", "branch", "dev"], cwd=local)
        _run(["git", "push", "-u", "origin", "dev"], cwd=local)
        return remote, local

    def _advance_remote_branch(self, remote: str, branch: str):
        """Push a new commit to the remote branch from a separate clone."""
        with tempfile.TemporaryDirectory() as other:
            _run(["git", "clone", remote, other], cwd="/tmp")
            _run(["git", "config", "user.email", "test@test.com"], cwd=other)
            _run(["git", "config", "user.name", "Test"], cwd=other)
            _run(["git", "checkout", branch], cwd=other)
            with open(os.path.join(other, "remote_advance.txt"), "w") as f:
                f.write("remote advance\n")
            _run(["git", "add", "remote_advance.txt"], cwd=other)
            _run(["git", "commit", "-m", "remote advance"], cwd=other)
            _run(["git", "push", "origin", branch], cwd=other)

    def test_rebase_two_arg_changes_head(self):
        """Demonstrate the bug: `git rebase origin/dev dev` with local checked
        out on 'main' causes HEAD to switch to 'dev'."""
        with tempfile.TemporaryDirectory() as tmp:
            remote, local = self._setup_repos(tmp)

            # Ensure we are on 'main', not 'dev'
            head_before = _run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=local
            ).stdout.strip()
            self.assertEqual(head_before, "main")

            # Remote advances on 'dev'
            self._advance_remote_branch(remote, "dev")

            # Fetch the remote-tracking ref (old approach, step 1)
            _run(["git", "fetch", "origin", "dev"], cwd=local)

            # Two-argument rebase (old, broken approach): implicitly checks out 'dev'
            _run(["git", "rebase", "origin/dev", "dev"], cwd=local)

            head_after = _run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=local
            ).stdout.strip()
            self.assertEqual(
                head_after, "dev",
                "Expected two-arg rebase to have changed HEAD to 'dev' "
                "(demonstrating the bug).",
            )

    def test_fetch_refspec_does_not_change_head(self):
        """`git fetch origin dev:dev` updates the local 'dev' ref without
        changing HEAD — the working branch stays on 'main'."""
        with tempfile.TemporaryDirectory() as tmp:
            remote, local = self._setup_repos(tmp)

            head_before = _run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=local
            ).stdout.strip()
            self.assertEqual(head_before, "main")

            # Remote advances on 'dev'
            self._advance_remote_branch(remote, "dev")

            # New approach: refspec fetch updates the local ref in place
            _run(["git", "fetch", "origin", "dev:dev"], cwd=local)

            head_after = _run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=local
            ).stdout.strip()
            self.assertEqual(
                head_after, "main",
                "HEAD should still be 'main' after `git fetch origin dev:dev` — "
                "the refspec form must not change the working branch.",
            )

    def test_fetch_refspec_advances_local_ref(self):
        """`git fetch origin dev:dev` must actually update the local 'dev' ref
        so that a subsequent push is fast-forward."""
        with tempfile.TemporaryDirectory() as tmp:
            remote, local = self._setup_repos(tmp)

            # Record original local dev tip
            dev_before = _run(
                ["git", "rev-parse", "dev"], cwd=local
            ).stdout.strip()

            # Remote advances on 'dev'
            self._advance_remote_branch(remote, "dev")

            # Fetch with refspec
            _run(["git", "fetch", "origin", "dev:dev"], cwd=local)

            dev_after = _run(
                ["git", "rev-parse", "dev"], cwd=local
            ).stdout.strip()

            self.assertNotEqual(
                dev_before, dev_after,
                "The local 'dev' ref should have advanced after `git fetch origin dev:dev`.",
            )

            # A subsequent push should be accepted (fast-forward)
            with open(os.path.join(local, "state.json"), "w") as f:
                f.write("{}\n")
            _run(["git", "add", "state.json"], cwd=local)
            # Commit on top of dev without checking it out, using git plumbing
            _run(["git", "checkout", "main"], cwd=local)  # stay on main
            push_res = _run(["git", "push", "origin", "dev"], cwd=local, check=False)
            # dev is already up-to-date with origin/dev after the refspec fetch, push is a no-op
            self.assertEqual(
                push_res.returncode, 0,
                f"Push failed after fetch refspec: {push_res.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
