"""Test that the remote push path in _execute_dag_inner fetches and rebases
before pushing, preventing non-fast-forward failures when parallel tasks have
already pushed to the remote dev branch.

The failure mode this guards against:
    git push origin dev-gemini
    ! [rejected] dev-gemini (non-fast-forward)

The fix: fetch + rebase onto origin/dev before pushing so local is always
fast-forward relative to remote.

Tests will fail if the fetch+rebase sequence is removed from executor.py.
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


# ---------------------------------------------------------------------------
# Helpers to classify subprocess.run(["git", ...]) calls in the AST
# ---------------------------------------------------------------------------

def _git_subcommand(call_node: ast.Call) -> list[str] | None:
    """Return the git sub-command tokens for a subprocess.run(["git", ...]) call,
    or None if the call is not a git subprocess invocation."""
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
            tokens.append(None)  # dynamic value
    if not tokens or tokens[0] != "git":
        return None
    return tokens


def _stmt_is_git_call(stmt: ast.stmt, subcommand: str) -> bool:
    """Return True if `stmt` is a direct assignment or expression whose value is
    a subprocess.run(["git", subcommand, ...]) call — i.e. the call is at the
    top level of the statement, not buried inside a nested block."""
    # Match:  result = subprocess.run(["git", subcommand, ...], ...)
    #     or: subprocess.run(["git", subcommand, ...], ...)  (Expr statement)
    call: ast.Call | None = None
    if isinstance(stmt, ast.Assign):
        if isinstance(stmt.value, ast.Call):
            call = stmt.value
    elif isinstance(stmt, ast.Expr):
        if isinstance(stmt.value, ast.Call):
            call = stmt.value
    if call is None:
        return False
    tokens = _git_subcommand(call)
    return bool(tokens and len(tokens) >= 2 and tokens[1] == subcommand)


def _stmt_contains_git_call(stmt: ast.stmt, subcommand: str) -> bool:
    """Return True if `stmt` (or any node directly nested in it, including
    through if/else/with blocks but NOT through function defs) contains a
    subprocess.run(["git", subcommand, ...]) call.

    We use ast.walk here intentionally for the rebase check, where the rebase
    lives inside an `if/else` block that is a sibling of the push statement."""
    for node in ast.walk(stmt):
        if not isinstance(node, ast.Call):
            continue
        tokens = _git_subcommand(node)
        if tokens and len(tokens) >= 2 and tokens[1] == subcommand:
            return True
    return False


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------------------
# AST-based structural test
# ---------------------------------------------------------------------------

class TestFetchRebaseBeforeRemotePush(unittest.TestCase):

    def _get_ast(self):
        with open(EXECUTOR_FILE, "r", encoding="utf-8") as f:
            return ast.parse(f.read())

    def test_fetch_precedes_push_in_execute_dag_inner(self):
        """In _execute_dag_inner, every `git push origin` must be preceded by a
        `git fetch origin` in the same statement block.

        This test fails on the old code that used a bare `git push origin`
        without a prior fetch, and passes once the fetch+rebase is in place.
        """
        tree = self._get_ast()

        func = _find_function(tree, "_execute_dag_inner")
        self.assertIsNotNone(func, "Could not find _execute_dag_inner in executor.py")

        # Collect every statement-list (body) reachable from _execute_dag_inner.
        bodies: list[list[ast.stmt]] = []
        for node in ast.walk(func):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                bodies.append(body)
            orelse = getattr(node, "orelse", None)
            if isinstance(orelse, list) and orelse:
                bodies.append(orelse)
            handlers = getattr(node, "handlers", None)
            if isinstance(handlers, list):
                for h in handlers:
                    if isinstance(getattr(h, "body", None), list):
                        bodies.append(h.body)

        # For each body, find direct `git push` statements and verify a direct
        # `git fetch` statement precedes them in the same block.
        push_bodies_found = 0
        for body in bodies:
            push_indices = [
                i for i, stmt in enumerate(body)
                if _stmt_is_git_call(stmt, "push")
            ]
            if not push_indices:
                continue

            for push_idx in push_indices:
                push_bodies_found += 1
                # Is there a direct fetch anywhere before the push in this body?
                fetch_before = any(
                    _stmt_is_git_call(body[j], "fetch")
                    for j in range(push_idx)
                )
                self.assertTrue(
                    fetch_before,
                    f"Found `git push` at line {body[push_idx].lineno} in "
                    f"_execute_dag_inner without a preceding `git fetch` in the "
                    f"same block. Add `git fetch origin <branch>` before pushing "
                    f"to prevent non-fast-forward failures from concurrent merges.",
                )

        self.assertGreater(
            push_bodies_found, 0,
            "No `git push` calls found in _execute_dag_inner — test may be stale.",
        )

    def test_rebase_precedes_push_in_execute_dag_inner(self):
        """In _execute_dag_inner, every `git push origin` must also have a
        `git rebase` nearby to integrate remote changes before pushing.

        This test fails on the old code that used a bare `git push origin`
        without a prior rebase, and passes once the fetch+rebase is in place.
        """
        tree = self._get_ast()
        func = _find_function(tree, "_execute_dag_inner")
        self.assertIsNotNone(func, "Could not find _execute_dag_inner in executor.py")

        # Collect all statement bodies reachable from _execute_dag_inner.
        bodies: list[list[ast.stmt]] = []
        for node in ast.walk(func):
            for attr in ("body", "orelse"):
                block = getattr(node, attr, None)
                if isinstance(block, list) and block:
                    bodies.append(block)
            for h in getattr(node, "handlers", []):
                if isinstance(getattr(h, "body", None), list):
                    bodies.append(h.body)

        # For each body containing a direct `git push`, check that somewhere
        # before the push (including inside sibling if/else blocks) a `git
        # rebase` appears.  We use the deep-walk checker here because the
        # rebase lives inside an if/else that is a sibling of the push.
        push_bodies_found = 0
        for body in bodies:
            push_indices = [
                i for i, stmt in enumerate(body)
                if _stmt_is_git_call(stmt, "push")
            ]
            if not push_indices:
                continue

            for push_idx in push_indices:
                push_bodies_found += 1
                # Search the preceding statements (including their sub-trees)
                # for any rebase call.
                rebase_before = any(
                    _stmt_contains_git_call(body[j], "rebase")
                    for j in range(push_idx)
                )
                self.assertTrue(
                    rebase_before,
                    f"Found `git push` at line {body[push_idx].lineno} in "
                    f"_execute_dag_inner without a preceding `git rebase` in the "
                    f"same or enclosing block. Add `git rebase origin/<branch>` "
                    f"before pushing to ensure local is fast-forward.",
                )

        self.assertGreater(push_bodies_found, 0, "No `git push` calls found — test may be stale.")


# ---------------------------------------------------------------------------
# Functional test: real git repos
# ---------------------------------------------------------------------------

def _run(cmd, cwd, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


class TestPushAfterConcurrentRemoteAdvance(unittest.TestCase):
    """Demonstrate the failure mode and verify the fix using real git repos."""

    def _setup_repos(self, tmp_path):
        """Create a bare remote and a local clone with a shared dev branch."""
        remote = os.path.join(tmp_path, "remote.git")
        local = os.path.join(tmp_path, "local")

        os.makedirs(remote)
        os.makedirs(local)

        _run(["git", "init", "--bare", remote], cwd=tmp_path)

        # Create a local repo, make an initial commit, push to remote
        _run(["git", "init"], cwd=local)
        _run(["git", "config", "user.email", "test@test.com"], cwd=local)
        _run(["git", "config", "user.name", "Test"], cwd=local)
        (open(os.path.join(local, "README.md"), "w")).write("init\n")
        _run(["git", "add", "README.md"], cwd=local)
        _run(["git", "commit", "-m", "init"], cwd=local)
        _run(["git", "remote", "add", "origin", remote], cwd=local)
        _run(["git", "branch", "-M", "dev-gemini"], cwd=local)
        _run(["git", "push", "-u", "origin", "dev-gemini"], cwd=local)

        return remote, local

    def _advance_remote(self, remote, local, branch="dev-gemini"):
        """Simulate a concurrent task pushing to the remote ahead of local."""
        # Make a second clone, commit to it, push to remote
        with tempfile.TemporaryDirectory() as other:
            _run(["git", "clone", remote, other], cwd="/tmp")
            _run(["git", "config", "user.email", "test@test.com"], cwd=other)
            _run(["git", "config", "user.name", "Test"], cwd=other)
            _run(["git", "checkout", branch], cwd=other)
            (open(os.path.join(other, "concurrent.txt"), "w")).write("concurrent\n")
            _run(["git", "add", "concurrent.txt"], cwd=other)
            _run(["git", "commit", "-m", "concurrent task merge"], cwd=other)
            _run(["git", "push", "origin", branch], cwd=other)

    def test_plain_push_fails_when_remote_advanced(self):
        """Without fetch+rebase, a plain git push fails if remote has moved ahead."""
        with tempfile.TemporaryDirectory() as tmp:
            remote, local = self._setup_repos(tmp)
            branch = "dev-gemini"

            # Add a local commit (simulating our merge completing)
            (open(os.path.join(local, "our_task.txt"), "w")).write("our task\n")
            _run(["git", "add", "our_task.txt"], cwd=local)
            _run(["git", "commit", "-m", "our task merge"], cwd=local)

            # Remote advances concurrently
            self._advance_remote(remote, local, branch)

            # Plain push (old code) should fail
            res = _run(["git", "push", "origin", branch], cwd=local, check=False)
            self.assertNotEqual(
                res.returncode, 0,
                "Expected plain git push to fail when remote has advanced, "
                "but it succeeded. The test setup may be incorrect.",
            )

    def test_fetch_rebase_push_succeeds_when_remote_advanced(self):
        """With fetch+rebase before push, the push succeeds even when remote advanced."""
        with tempfile.TemporaryDirectory() as tmp:
            remote, local = self._setup_repos(tmp)
            branch = "dev-gemini"

            # Add a local commit (simulating our merge completing)
            (open(os.path.join(local, "our_task.txt"), "w")).write("our task\n")
            _run(["git", "add", "our_task.txt"], cwd=local)
            _run(["git", "commit", "-m", "our task merge"], cwd=local)

            # Remote advances concurrently
            self._advance_remote(remote, local, branch)

            # New code: fetch + rebase + push
            fetch_res = _run(["git", "fetch", "origin", branch], cwd=local, check=False)
            self.assertEqual(fetch_res.returncode, 0, f"git fetch failed: {fetch_res.stderr}")

            rebase_res = _run(["git", "rebase", f"origin/{branch}", branch], cwd=local, check=False)
            self.assertEqual(rebase_res.returncode, 0, f"git rebase failed: {rebase_res.stderr}")

            push_res = _run(["git", "push", "origin", branch], cwd=local, check=False)
            self.assertEqual(
                push_res.returncode, 0,
                f"git push failed after fetch+rebase: {push_res.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
