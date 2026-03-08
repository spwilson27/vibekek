"""Test that merge_task uses --force-with-lease when pushing.

The merge agent may rewrite history (e.g. git reset to squash), making the
dev branch non-fast-forward. Using plain `git push` would fail in that case.
All pushes in merge_task must use --force-with-lease to handle this safely.
"""

import ast
import os
import unittest

EXECUTOR_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "workflow_lib", "executor.py",
)


class TestMergeForceWithLease(unittest.TestCase):
    def test_all_push_calls_use_force_with_lease(self):
        """Every git push in executor.py must use --force-with-lease."""
        with open(EXECUTOR_FILE, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source)

        # Find the merge_task function node
        merge_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "merge_task":
                merge_func = node
                break
        self.assertIsNotNone(merge_func, "Could not find merge_task function in executor.py")

        for node in ast.walk(merge_func):
            if not isinstance(node, ast.Call):
                continue
            # Match subprocess.run(["git", "push", ...], ...)
            if not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.List):
                continue
            elts = first_arg.elts
            if len(elts) < 2:
                continue
            strs = []
            for e in elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    strs.append(e.value)
                else:
                    strs.append(None)
            if len(strs) >= 2 and strs[0] == "git" and strs[1] == "push":
                self.assertIn(
                    "--force-with-lease",
                    strs,
                    f"Found `git push` without --force-with-lease at line {node.lineno} in executor.py. "
                    f"All pushes in merge_task must use --force-with-lease to handle non-fast-forward merges.",
                )


if __name__ == "__main__":
    unittest.main()
