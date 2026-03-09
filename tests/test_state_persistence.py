"""
End-to-end tests for workflow state persistence to the dev branch.

Validates that:
1. commit_state_to_branch() writes state files into a git branch without
   disturbing the working tree.
2. restore_state_from_branch() recovers state files from a branch when
   local copies are missing.
3. Sequential commit_state_to_branch() calls produce fast-forward-compatible
   history (no merge conflicts when a clone pushes back).
4. State round-trips correctly (save → commit → delete local → restore → load).
5. State files must live outside any git submodule path — placing them inside
   a submodule (e.g. .tools/.state/) causes commit_state_to_branch() to fail
   because the parent repo treats the submodule as a gitlink, not a directory.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _run(cmd, cwd, **kwargs):
    """Run a git command, raising on failure."""
    return subprocess.run(
        cmd, cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **kwargs,
    )


def _git_show(repo, branch, rel_path):
    """Return file contents from a branch, or None if missing."""
    res = subprocess.run(
        ["git", "show", f"{branch}:{rel_path}"],
        cwd=repo, capture_output=True, text=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout


@pytest.fixture()
def temp_repo(tmp_path):
    """Create a bare-bones git repo with a dev branch and return paths.

    Yields a dict with:
        root_dir: path to the repo
        tools_dir: path to .tools/ inside the repo
        workflow_state_file: absolute path matching WORKFLOW_STATE_FILE layout
        replan_state_file: absolute path matching REPLAN_STATE_FILE layout
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "-c", "init.defaultBranch=main", "init"], cwd=repo)
    _run(["git", "config", "user.email", "test@test.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)

    # Initial commit on main
    (repo / "README.md").write_text("# test\n")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)

    # Create dev branch
    _run(["git", "branch", "dev"], cwd=repo)

    # Create .tools/.state dir
    tools = repo / ".tools"
    tools.mkdir()
    state_dir = tools / ".state"
    state_dir.mkdir()

    yield {
        "root_dir": str(repo),
        "tools_dir": str(tools),
        "workflow_state_file": str(state_dir / "workflow_state.json"),
        "replan_state_file": str(state_dir / "replan_state.json"),
    }


class TestCommitStateToBranch:
    """Tests for commit_state_to_branch()."""

    def test_commits_workflow_state_to_dev(self, temp_repo, monkeypatch):
        """State file appears in the dev branch after commit."""
        from workflow_lib.state import commit_state_to_branch
        from workflow_lib import constants

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        # Re-import to pick up patched constants
        from workflow_lib import state as state_mod
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        state_data = {"completed_tasks": ["phase_1/task_a"], "merged_tasks": ["phase_1/task_a"]}
        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump(state_data, f)

        result = commit_state_to_branch(temp_repo["root_dir"], "dev")
        assert result is True

        content = _git_show(temp_repo["root_dir"], "dev", ".tools/.state/workflow_state.json")
        assert content is not None
        assert json.loads(content) == state_data

    def test_commits_replan_state_to_dev(self, temp_repo, monkeypatch):
        """Replan state file appears in the dev branch after commit."""
        from workflow_lib.state import commit_state_to_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        replan_data = {"blocked_tasks": {"t1": "reason"}, "removed_tasks": [], "replan_history": []}
        with open(temp_repo["replan_state_file"], "w") as f:
            json.dump(replan_data, f)

        result = commit_state_to_branch(temp_repo["root_dir"], "dev")
        assert result is True

        content = _git_show(temp_repo["root_dir"], "dev", ".tools/.state/replan_state.json")
        assert content is not None
        assert json.loads(content) == replan_data

    def test_does_not_disturb_working_tree(self, temp_repo, monkeypatch):
        """Working tree and current branch are unchanged after commit."""
        from workflow_lib.state import commit_state_to_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump({"completed_tasks": [], "merged_tasks": []}, f)

        # Check current branch before
        branch_before = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=temp_repo["root_dir"], capture_output=True, text=True,
        ).stdout.strip()

        commit_state_to_branch(temp_repo["root_dir"], "dev")

        # Check current branch after
        branch_after = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=temp_repo["root_dir"], capture_output=True, text=True,
        ).stdout.strip()

        assert branch_before == branch_after

    def test_returns_false_for_nonexistent_branch(self, temp_repo, monkeypatch):
        """Returns False when the target branch doesn't exist."""
        from workflow_lib.state import commit_state_to_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump({"completed_tasks": [], "merged_tasks": []}, f)

        result = commit_state_to_branch(temp_repo["root_dir"], "nonexistent-branch")
        assert result is False

    def test_sequential_commits_are_fast_forward(self, temp_repo, monkeypatch):
        """Two sequential state commits produce a linear (fast-forward) history."""
        from workflow_lib.state import commit_state_to_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        # First commit
        state1 = {"completed_tasks": ["t1"], "merged_tasks": ["t1"]}
        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump(state1, f)
        commit_state_to_branch(temp_repo["root_dir"], "dev")

        rev1 = subprocess.run(
            ["git", "rev-parse", "dev"],
            cwd=temp_repo["root_dir"], capture_output=True, text=True,
        ).stdout.strip()

        # Second commit
        state2 = {"completed_tasks": ["t1", "t2"], "merged_tasks": ["t1", "t2"]}
        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump(state2, f)
        commit_state_to_branch(temp_repo["root_dir"], "dev")

        # Verify rev1 is an ancestor of current dev
        res = subprocess.run(
            ["git", "merge-base", "--is-ancestor", rev1, "dev"],
            cwd=temp_repo["root_dir"],
        )
        assert res.returncode == 0, "Second state commit is not a fast-forward from the first"

    def test_clone_can_push_after_state_commit(self, temp_repo, monkeypatch):
        """A clone that merges code can still push after a state commit on origin."""
        from workflow_lib.state import commit_state_to_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        root = temp_repo["root_dir"]

        # Commit state to dev
        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump({"completed_tasks": ["t1"], "merged_tasks": ["t1"]}, f)
        commit_state_to_branch(root, "dev")

        # Now simulate what merge_task does: clone, checkout dev, make changes, push
        clone_dir = tempfile.mkdtemp()
        try:
            _run(["git", "clone", root, clone_dir], cwd=root)
            _run(["git", "checkout", "dev"], cwd=clone_dir)

            # Make a code change in the clone
            Path(clone_dir, "new_file.py").write_text("print('hello')\n")
            _run(["git", "add", "new_file.py"], cwd=clone_dir)
            _run(["git", "commit", "-m", "add new_file"], cwd=clone_dir)

            # Push should succeed (fast-forward)
            res = subprocess.run(
                ["git", "push", "origin", "dev"],
                cwd=clone_dir, capture_output=True, text=True,
            )
            assert res.returncode == 0, f"Push failed: {res.stderr}"
        finally:
            import shutil
            shutil.rmtree(clone_dir, ignore_errors=True)


class TestRestoreStateFromBranch:
    """Tests for restore_state_from_branch()."""

    def test_restores_missing_local_state(self, temp_repo, monkeypatch):
        """Local state files are restored from the dev branch when missing."""
        from workflow_lib.state import commit_state_to_branch, restore_state_from_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        # Write and commit state
        state_data = {"completed_tasks": ["t1", "t2"], "merged_tasks": ["t1", "t2"]}
        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump(state_data, f)
        commit_state_to_branch(temp_repo["root_dir"], "dev")

        # Delete local file
        os.unlink(temp_repo["workflow_state_file"])
        assert not os.path.exists(temp_repo["workflow_state_file"])

        # Restore
        restore_state_from_branch(temp_repo["root_dir"], "dev")

        assert os.path.exists(temp_repo["workflow_state_file"])
        with open(temp_repo["workflow_state_file"]) as f:
            restored = json.load(f)
        assert restored == state_data

    def test_does_not_overwrite_existing_local_state(self, temp_repo, monkeypatch):
        """If local state exists, restore_state_from_branch() leaves it alone."""
        from workflow_lib.state import commit_state_to_branch, restore_state_from_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        # Commit old state to branch
        old_state = {"completed_tasks": ["t1"], "merged_tasks": ["t1"]}
        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump(old_state, f)
        commit_state_to_branch(temp_repo["root_dir"], "dev")

        # Update local state to something newer
        new_state = {"completed_tasks": ["t1", "t2", "t3"], "merged_tasks": ["t1", "t2", "t3"]}
        with open(temp_repo["workflow_state_file"], "w") as f:
            json.dump(new_state, f)

        # Restore should not overwrite
        restore_state_from_branch(temp_repo["root_dir"], "dev")

        with open(temp_repo["workflow_state_file"]) as f:
            current = json.load(f)
        assert current == new_state

    def test_no_error_when_branch_has_no_state_and_no_commits(self, temp_repo, monkeypatch):
        """No error when dev branch has no state files and no task commits."""
        from workflow_lib.state import restore_state_from_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        # Should not raise
        restore_state_from_branch(temp_repo["root_dir"], "dev")
        assert not os.path.exists(temp_repo["workflow_state_file"])

    def test_reconstructs_state_from_commit_messages(self, temp_repo, monkeypatch):
        """When no state file exists, completed tasks are reconstructed from commit history."""
        from workflow_lib.state import restore_state_from_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        root = temp_repo["root_dir"]

        # Add task-style commits to the dev branch
        _run(["git", "checkout", "dev"], cwd=root)
        Path(root, "a.py").write_text("a\n")
        _run(["git", "add", "a.py"], cwd=root)
        _run(["git", "commit", "-m", "phase_1:01_setup/01_init.md: Initial setup"], cwd=root)

        Path(root, "b.py").write_text("b\n")
        _run(["git", "add", "b.py"], cwd=root)
        _run(["git", "commit", "-m", "phase_1:02_core/03_api.md: Add API endpoint"], cwd=root)

        _run(["git", "checkout", "main"], cwd=root)

        # Ensure no local state file exists
        assert not os.path.exists(temp_repo["workflow_state_file"])

        restore_state_from_branch(root, "dev")

        assert os.path.exists(temp_repo["workflow_state_file"])
        with open(temp_repo["workflow_state_file"]) as f:
            state = json.load(f)

        assert "phase_1/01_setup/01_init.md" in state["completed_tasks"]
        assert "phase_1/02_core/03_api.md" in state["completed_tasks"]
        assert len(state["completed_tasks"]) == 2

    def test_reconstruct_ignores_non_task_commits(self, temp_repo, monkeypatch):
        """Commit messages that don't match the task pattern are ignored."""
        from workflow_lib.state import restore_state_from_branch
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        root = temp_repo["root_dir"]

        _run(["git", "checkout", "dev"], cwd=root)
        Path(root, "a.py").write_text("a\n")
        _run(["git", "add", "a.py"], cwd=root)
        _run(["git", "commit", "-m", "Initial commit after setup"], cwd=root)

        Path(root, "b.py").write_text("b\n")
        _run(["git", "add", "b.py"], cwd=root)
        _run(["git", "commit", "-m", "phase_1:01_setup/01_init.md: Real task"], cwd=root)

        Path(root, "c.py").write_text("c\n")
        _run(["git", "add", "c.py"], cwd=root)
        _run(["git", "commit", "-m", "Update workflow state"], cwd=root)

        _run(["git", "checkout", "main"], cwd=root)

        restore_state_from_branch(root, "dev")

        with open(temp_repo["workflow_state_file"]) as f:
            state = json.load(f)

        assert state["completed_tasks"] == ["phase_1/01_setup/01_init.md"]


class TestStateRoundTrip:
    """Full round-trip: save → commit → delete → restore → load."""

    def test_full_round_trip(self, temp_repo, monkeypatch):
        from workflow_lib.state import (
            save_workflow_state, load_workflow_state,
            save_replan_state, load_replan_state,
            commit_state_to_branch, restore_state_from_branch,
        )
        from workflow_lib import constants, state as state_mod

        monkeypatch.setattr(constants, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(constants, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])
        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", temp_repo["workflow_state_file"])
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", temp_repo["replan_state_file"])

        # Save state locally
        wf_state = {"completed_tasks": ["p1/t1", "p1/t2"], "merged_tasks": ["p1/t1", "p1/t2"]}
        rp_state = {"blocked_tasks": {"p2/t1": "depends on external API"}, "removed_tasks": ["p3/t1"], "replan_history": []}
        save_workflow_state(wf_state)
        save_replan_state(rp_state)

        # Commit to branch
        assert commit_state_to_branch(temp_repo["root_dir"], "dev")

        # Delete local copies
        os.unlink(temp_repo["workflow_state_file"])
        os.unlink(temp_repo["replan_state_file"])

        # Restore from branch
        restore_state_from_branch(temp_repo["root_dir"], "dev")

        # Load and verify
        restored_wf = load_workflow_state()
        restored_rp = load_replan_state()

        assert restored_wf["completed_tasks"] == wf_state["completed_tasks"]
        assert restored_wf["merged_tasks"] == wf_state["merged_tasks"]
        assert restored_rp["blocked_tasks"] == rp_state["blocked_tasks"]
        assert restored_rp["removed_tasks"] == rp_state["removed_tasks"]


def _make_repo_with_tools_submodule(tmp_path):
    """Return a repo path where .tools is a real git submodule.

    Creates a minimal upstream repo for the submodule, then creates the main
    repo and adds it as .tools.  Both repos are fully initialised with an
    initial commit so the submodule reference is valid.
    """
    # Upstream repo that will become the .tools submodule
    tools_src = tmp_path / "tools_src"
    tools_src.mkdir()
    _run(["git", "-c", "init.defaultBranch=main", "init"], cwd=tools_src)
    _run(["git", "config", "user.email", "t@t.com"], cwd=tools_src)
    _run(["git", "config", "user.name", "T"], cwd=tools_src)
    (tools_src / "README.md").write_text("tools\n")
    _run(["git", "add", "README.md"], cwd=tools_src)
    _run(["git", "commit", "-m", "init tools"], cwd=tools_src)

    # Main repo
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "-c", "init.defaultBranch=main", "init"], cwd=repo)
    _run(["git", "config", "user.email", "t@t.com"], cwd=repo)
    _run(["git", "config", "user.name", "T"], cwd=repo)
    _run(["git", "config", "protocol.file.allow", "always"], cwd=repo)
    (repo / "README.md").write_text("# test\n")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)

    # Add .tools as a submodule
    _run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add",
         str(tools_src), ".tools"],
        cwd=repo,
    )
    _run(["git", "commit", "-m", "add .tools submodule"], cwd=repo)

    # Create dev branch
    _run(["git", "branch", "dev"], cwd=repo)

    return repo


class TestSubmoduleSubtlety:
    """Regression tests for the .tools-submodule state-commit bug.

    When .tools is a git submodule, the parent repo records it as a gitlink
    (mode 160000) in its tree.  git update-index --cacheinfo cannot add a
    regular file under a gitlink path, so commit_state_to_branch() silently
    fails whenever STATE_DIR was inside .tools/.state/.

    These tests verify:
    * Placing state files inside the submodule path causes the commit to fail.
    * Placing state files outside the submodule (ROOT_DIR/.workflow_state/)
      causes the commit to succeed — the behaviour guaranteed by the fix that
      moved STATE_DIR from TOOLS_DIR/.state to ROOT_DIR/.workflow_state.

    The second test directly uses the relative path derived from
    constants.WORKFLOW_STATE_FILE so it fails if the constant is ever
    accidentally moved back inside a submodule directory.
    """

    def test_state_inside_submodule_path_fails(self, tmp_path, monkeypatch):
        """commit_state_to_branch returns False when state files live inside .tools/."""
        from workflow_lib.state import commit_state_to_branch
        from workflow_lib import state as state_mod

        repo = _make_repo_with_tools_submodule(tmp_path)

        # Simulate the old (broken) layout: state inside the .tools submodule
        old_state_dir = repo / ".tools" / ".state"
        old_state_dir.mkdir(parents=True, exist_ok=True)
        wf_file = old_state_dir / "workflow_state.json"
        rp_file = old_state_dir / "replan_state.json"
        wf_file.write_text('{"completed_tasks": ["t1"], "merged_tasks": ["t1"]}')

        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", str(wf_file))
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", str(rp_file))

        result = commit_state_to_branch(str(repo), "dev")
        assert result is False, (
            "Expected commit_state_to_branch to fail when state files are "
            "inside the .tools git submodule path"
        )

    def test_state_dir_outside_submodule_succeeds(self, tmp_path, monkeypatch):
        """commit_state_to_branch succeeds when STATE_DIR is in the root repo.

        This test derives the state-file location from constants.WORKFLOW_STATE_FILE
        (relative to constants.ROOT_DIR) so it will fail if that constant is ever
        moved back inside a submodule directory.
        """
        from workflow_lib.state import commit_state_to_branch
        from workflow_lib import constants, state as state_mod

        repo = _make_repo_with_tools_submodule(tmp_path)

        # Mirror the relative path of the real WORKFLOW_STATE_FILE under our
        # temp repo.  If constants still points inside .tools/, this test fails.
        wf_rel = os.path.relpath(constants.WORKFLOW_STATE_FILE, constants.ROOT_DIR)
        rp_rel = os.path.relpath(constants.REPLAN_STATE_FILE, constants.ROOT_DIR)

        wf_file = repo / wf_rel
        rp_file = repo / rp_rel
        wf_file.parent.mkdir(parents=True, exist_ok=True)

        state_data = {"completed_tasks": ["phase_1/t1.md"], "merged_tasks": ["phase_1/t1.md"]}
        wf_file.write_text(json.dumps(state_data))

        monkeypatch.setattr(state_mod, "WORKFLOW_STATE_FILE", str(wf_file))
        monkeypatch.setattr(state_mod, "REPLAN_STATE_FILE", str(rp_file))

        result = commit_state_to_branch(str(repo), "dev")
        assert result is True, (
            f"commit_state_to_branch failed with state at '{wf_rel}'. "
            f"STATE_DIR must be outside any git submodule (not under .tools/)."
        )

        content = _git_show(str(repo), "dev", wf_rel)
        assert content is not None, f"State file not found in dev branch at {wf_rel}"
        assert json.loads(content) == state_data
