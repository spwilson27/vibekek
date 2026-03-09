"""Test that the remote push path in _execute_dag_inner syncs the local branch
ref from origin before pushing, preventing non-fast-forward failures when
parallel tasks have already pushed to the remote dev branch.

The failure mode this guards against:
    git push origin dev-gemini
    ! [rejected] dev-gemini (non-fast-forward)

The fix: use `git fetch origin branch:branch` (refspec form) to update the
local ref directly without requiring a checkout, so local is always
fast-forward relative to remote and the working tree is never disturbed.

Tests will fail if the fetch-refspec sync is removed from executor.py, or if
a `git rebase <upstream> <branch>` (which implicitly checks out <branch>) is
reintroduced with cwd=root_dir.
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
        """_execute_dag_inner must have a `git fetch` to sync the local ref after
        merge_task pushes, and must NOT contain a `git push` (the push is now
        delegated entirely to merge_task which clones from the remote).

        This invariant ensures the local dev-branch ref stays in sync without
        the local repo ever pushing directly to the remote.
        """
        tree = self._get_ast()

        func = _find_function(tree, "_execute_dag_inner")
        self.assertIsNotNone(func, "Could not find _execute_dag_inner in executor.py")

        fetch_found = any(
            isinstance(node, ast.Call) and bool(_git_subcommand(node) and
                len(_git_subcommand(node)) >= 2 and _git_subcommand(node)[1] == "fetch")
            for node in ast.walk(func)
        )
        self.assertTrue(
            fetch_found,
            "_execute_dag_inner must contain a `git fetch` call to sync the local "
            "dev-branch ref after merge_task has pushed to the remote.",
        )

        # Verify no git push remains in _execute_dag_inner (push lives in merge_task now)
        push_found = any(
            isinstance(node, ast.Call) and bool(_git_subcommand(node) and
                len(_git_subcommand(node)) >= 2 and _git_subcommand(node)[1] == "push")
            for node in ast.walk(func)
        )
        self.assertFalse(
            push_found,
            "_execute_dag_inner must not contain a `git push` call. "
            "The push is now performed inside merge_task (which clones from the "
            "remote), so execute_dag only needs to fetch to sync the local ref.",
        )

    def test_fetch_uses_refspec_form_before_push_in_execute_dag_inner(self):
        """In _execute_dag_inner, the `git fetch` before `git push` must use the
        'branch:branch' refspec form so that the local ref is updated in place
        without requiring a checkout.

        The old approach used `git fetch origin <branch>` (updates only the
        remote-tracking ref) followed by `git rebase origin/<branch> <branch>`
        (which implicitly checks out <branch>). The correct approach is:

            git fetch origin <branch>:<branch>

        which updates the local ref directly, never touching HEAD or the
        working tree.
        """
        tree = self._get_ast()
        func = _find_function(tree, "_execute_dag_inner")
        self.assertIsNotNone(func, "Could not find _execute_dag_inner in executor.py")

        # Find all subprocess.run(["git", "fetch", ...], cwd=root_dir) calls
        # in _execute_dag_inner and verify at least one uses a branch:branch
        # refspec (a token containing ":").
        fetch_with_refspec_found = False
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            tokens = _git_subcommand(node)
            if not (tokens and len(tokens) >= 2 and tokens[1] == "fetch"):
                continue
            # Check cwd=root_dir
            cwd_is_root = any(
                kw.arg == "cwd" and isinstance(kw.value, ast.Name) and kw.value.id == "root_dir"
                for kw in node.keywords
            )
            if not cwd_is_root:
                continue
            # Check that one of the arguments is an f-string or constant containing ":"
            # (the branch:branch refspec).  The 4th token onward is the refspec.
            if len(node.args[0].elts) >= 4:  # type: ignore[union-attr]
                refspec_elt = node.args[0].elts[3]  # type: ignore[union-attr]
                # Accept JoinedStr (f-string like f"{branch}:{branch}") or
                # a plain string constant containing ":".
                if isinstance(refspec_elt, ast.JoinedStr):
                    fetch_with_refspec_found = True
                elif isinstance(refspec_elt, ast.Constant) and ":" in str(refspec_elt.value):
                    fetch_with_refspec_found = True

        self.assertTrue(
            fetch_with_refspec_found,
            "Could not find a `git fetch origin <branch>:<branch>` (refspec form) call "
            "with cwd=root_dir in _execute_dag_inner. The refspec form is required to "
            "update the local branch ref without triggering a checkout. "
            "Do not use `git fetch origin <branch>` + `git rebase` — the rebase "
            "implicitly checks out the branch and disturbs the working tree.",
        )

    def test_no_rebase_with_root_dir_cwd_in_execute_dag_inner(self):
        """_execute_dag_inner must not call `git rebase` with cwd=root_dir.

        The two-argument form `git rebase <upstream> <branch>` implicitly does
        `git checkout <branch>` before rebasing, which changes the working tree
        branch.  All rebase/checkout operations must use an isolated tmpdir
        clone, never the host repo working tree.
        """
        tree = self._get_ast()
        func = _find_function(tree, "_execute_dag_inner")
        self.assertIsNotNone(func, "Could not find _execute_dag_inner in executor.py")

        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            tokens = _git_subcommand(node)
            if not (tokens and len(tokens) >= 2 and tokens[1] == "rebase"):
                continue
            cwd_is_root = any(
                kw.arg == "cwd" and isinstance(kw.value, ast.Name) and kw.value.id == "root_dir"
                for kw in node.keywords
            )
            self.assertFalse(
                cwd_is_root,
                f"Found `git rebase` with cwd=root_dir at line {node.lineno} in "
                f"_execute_dag_inner. The two-argument rebase form implicitly checks "
                f"out the branch, changing the working tree. Use "
                f"`git fetch origin branch:branch` instead to update the local ref "
                f"without a checkout.",
            )


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
