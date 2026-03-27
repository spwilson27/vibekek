"""Persistence helpers for workflow and replan state.

All mutable state that must survive process restarts is stored as JSON on
disk.  This module provides thin read/write wrappers so the rest of the
codebase never needs to know the file paths or serialisation details.

Two independent state files are managed:

* **Workflow state** (``WORKFLOW_STATE_FILE``) – tracks which implementation
  tasks have been completed or merged into ``dev``.
* **Replan state** (``REPLAN_STATE_FILE``) – tracks blocked tasks, removed
  tasks, and an audit log of all replan operations.
"""

import os
import sys
import json
import subprocess
import tempfile
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from .constants import TOOLS_DIR, ROOT_DIR, WORKFLOW_STATE_FILE, REPLAN_STATE_FILE


def load_replan_state() -> Dict[str, Any]:
    """Load the replan state from disk, returning defaults when absent.

    The default skeleton contains empty collections for ``blocked_tasks``,
    ``removed_tasks``, and ``replan_history``.  Any keys present on disk are
    merged into this skeleton, so new keys added in future versions are
    always present in the returned dict.

    :returns: Replan state mapping with at least the keys
        ``blocked_tasks`` (dict), ``removed_tasks`` (list), and
        ``replan_history`` (list).
    :rtype: dict
    """
    state: Dict[str, Any] = {
        "blocked_tasks": {},
        "removed_tasks": [],
        "replan_history": [],
    }
    if os.path.exists(REPLAN_STATE_FILE):
        with open(REPLAN_STATE_FILE, "r", encoding="utf-8") as f:
            try:
                state.update(json.load(f))
            except json.JSONDecodeError:
                pass
    return state


def save_replan_state(state: Dict[str, Any]) -> None:
    """Persist the replan state dict to disk as formatted JSON.

    Creates any missing parent directories before writing.

    :param state: Replan state mapping to serialise.
    :type state: dict
    """
    with open(REPLAN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)


def load_workflow_state() -> Dict[str, Any]:
    """Load the implementation workflow state from disk.

    The default skeleton contains empty lists for ``completed_tasks`` and
    ``merged_tasks``.  Missing or corrupt files silently return the defaults.

    :returns: Workflow state mapping with at least ``completed_tasks`` (list)
        and ``merged_tasks`` (list).
    :rtype: dict
    """
    state: Dict[str, Any] = {"completed_tasks": [], "merged_tasks": [], "task_stages": {}}
    if os.path.exists(WORKFLOW_STATE_FILE):
        with open(WORKFLOW_STATE_FILE, "r", encoding="utf-8") as f:
            try:
                state.update(json.load(f))
            except json.JSONDecodeError:
                pass
    return state


def save_workflow_state(state: Dict[str, Any]) -> None:
    """Persist the workflow state dict to disk as formatted JSON.

    :param state: Workflow state mapping to serialise.
    :type state: dict
    """
    with open(WORKFLOW_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)


def log_action(
    state: Dict[str, Any],
    action: str,
    target: str,
    details: str = "",
) -> None:
    """Append a timestamped audit entry to ``state["replan_history"]``.

    The key is created if it does not exist.

    :param state: Replan state dict to mutate in place.
    :type state: dict
    :param action: Short verb describing the operation (e.g. ``"block"``).
    :type action: str
    :param target: The task reference or artefact affected.
    :type target: str
    :param details: Optional free-text detail string.
    :type details: str
    """
    state.setdefault("replan_history", []).append({
        "action": action,
        "target": target,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def load_dags(tasks_dir: str) -> Dict[str, List[str]]:
    """Load and merge all per-task JSON files into a single master DAG.

    For each ``phase_*/`` subdirectory of *tasks_dir*, the function reads
    individual task ``.json`` sidecar files (which contain a ``depends_on``
    list) and also scans subdirectories for legacy task structures.

    Task IDs in the returned dict are fully-qualified with the phase prefix,
    e.g. ``"phase_1/red_01_session_lifecycle"``.

    :param tasks_dir: Absolute path to the ``docs/plan/tasks/`` directory.
    :type tasks_dir: str
    :returns: Mapping of ``full_task_id -> [prerequisite_full_task_ids]``.
        Returns an empty dict when *tasks_dir* does not exist.
    :rtype: dict
    """
    master_dag: Dict[str, List[str]] = {}
    if not os.path.exists(tasks_dir):
        return master_dag

    # Files that are not tasks and should be skipped
    _NON_TASK_BASENAMES = {
        "dag.json", "dag_reviewed.json",
    }

    for phase_dir in sorted(os.listdir(tasks_dir)):
        phase_path = os.path.join(tasks_dir, phase_dir)
        if not os.path.isdir(phase_path) or not phase_dir.startswith("phase_"):
            continue

        # Scan for task JSON sidecar files directly in the phase directory
        for fname in sorted(os.listdir(phase_path)):
            fpath = os.path.join(phase_path, fname)

            if fname in _NON_TASK_BASENAMES:
                continue

            # Handle per-task .json sidecar files (e.g. green_01_foo.json)
            if fname.endswith(".json") and os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        task_meta = json.load(f)
                except json.JSONDecodeError as exc:
                    print(f"[!] WARNING: Skipping corrupt task file {fpath}: {exc}", file=sys.stderr)
                    continue
                # Use task_id from the JSON if present, otherwise derive from filename
                task_id = task_meta.get("task_id", f"{phase_dir}/{fname[:-5]}")
                prereqs = task_meta.get("depends_on", [])
                master_dag[task_id] = list(prereqs)

            # Handle legacy subdirectory structure (e.g. 00_pre_init/)
            elif os.path.isdir(fpath):
                for sub_fname in sorted(os.listdir(fpath)):
                    sub_fpath = os.path.join(fpath, sub_fname)
                    if sub_fname.endswith(".json") and os.path.isfile(sub_fpath):
                        try:
                            with open(sub_fpath, "r", encoding="utf-8") as f:
                                task_meta = json.load(f)
                        except json.JSONDecodeError as exc:
                            print(f"[!] WARNING: Skipping corrupt task file {sub_fpath}: {exc}", file=sys.stderr)
                            continue
                        fallback_id = f"{phase_dir}/{fname}/{sub_fname[:-5]}"
                        task_id = task_meta.get("task_id", fallback_id)
                        # Normalize: ensure task_id has phase prefix
                        if not task_id.startswith("phase_"):
                            task_id = f"{phase_dir}/{fname}/{task_id}"
                        prereqs = task_meta.get("depends_on", [])
                        master_dag[task_id] = list(prereqs)

    return master_dag


def get_tasks_dir() -> str:
    """Return the absolute path to the canonical tasks directory.

    :returns: ``<ROOT_DIR>/docs/plan/tasks``
    :rtype: str
    """
    return os.path.join(ROOT_DIR, "docs", "plan", "tasks")


def resolve_task_path(task_ref: str) -> str:
    """Resolve a relative task reference to its absolute filesystem path.

    :param task_ref: Relative task path such as ``"phase_1/sub/01_task.md"``.
    :type task_ref: str
    :returns: Absolute path under the tasks directory.
    :rtype: str
    """
    return os.path.join(get_tasks_dir(), task_ref)


def is_completed(task_ref: str, wf_state: Dict[str, Any]) -> bool:
    """Return whether a task is considered done (completed or merged).

    :param task_ref: Relative task reference, e.g. ``"phase_1/sub/01_task.md"``.
    :type task_ref: str
    :param wf_state: Workflow state dict as returned by :func:`load_workflow_state`.
    :type wf_state: dict
    :returns: ``True`` if the task appears in either ``completed_tasks`` or
        ``merged_tasks``.
    :rtype: bool
    """
    completed = set(wf_state.get("completed_tasks", []))
    merged = set(wf_state.get("merged_tasks", []))
    return task_ref in completed or task_ref in merged


def _reconstruct_completed_from_commits(root_dir: str, dev_branch: str) -> List[str]:
    """Extract completed task IDs from commit messages on the dev branch.

    Commit messages produced by the workflow start with the task ref using
    a ``phase_N:subdir/task.md:`` prefix.  This converts the colon-separated
    form back to the slash-separated DAG task ID.

    :param root_dir: Absolute path to the project root git repository.
    :param dev_branch: Name of the dev branch to scan.
    :returns: List of fully-qualified task IDs found in commit messages.
    """
    import re as _re
    res = subprocess.run(
        ["git", "log", dev_branch, "--format=%s"],
        cwd=root_dir, capture_output=True, text=True,
    )
    if res.returncode != 0:
        return []
    completed = []
    for line in res.stdout.splitlines():
        # Match "phase_N:rest/of/task.md: ..." and convert colon to slash
        m = _re.match(r"^(phase_\d+):(.+?\.md):", line)
        if m:
            task_id = f"{m.group(1)}/{m.group(2)}"
            completed.append(task_id)
    return completed


def restore_state_from_branch(root_dir: str, dev_branch: str) -> None:
    """Seed local state files from the dev branch if they don't exist locally.

    For each state file (workflow and replan), if the local file is missing
    but the file exists in the dev branch, extract it via ``git show``.
    As a fallback, if the workflow state file is still missing after checking
    the branch, reconstruct completed tasks from commit messages.

    :param root_dir: Absolute path to the project root git repository.
    :param dev_branch: Name of the dev branch to read state from.
    """
    for filepath in [WORKFLOW_STATE_FILE, REPLAN_STATE_FILE]:
        if os.path.exists(filepath):
            continue
        rel_path = os.path.relpath(filepath, root_dir)
        res = subprocess.run(
            ["git", "show", f"{dev_branch}:{rel_path}"],
            cwd=root_dir, capture_output=True, text=True,
        )
        if res.returncode == 0 and res.stdout.strip():
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(res.stdout)

    # Fallback: reconstruct workflow state from commit history
    if not os.path.exists(WORKFLOW_STATE_FILE):
        completed = _reconstruct_completed_from_commits(root_dir, dev_branch)
        if completed:
            os.makedirs(os.path.dirname(WORKFLOW_STATE_FILE), exist_ok=True)
            state = {
                "completed_tasks": completed,
                "merged_tasks": completed,
            }
            with open(WORKFLOW_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=4)


def commit_state_to_branch(root_dir: str, dev_branch: str) -> bool:
    """Commit workflow and replan state files to the dev branch.

    Uses git plumbing commands to update the branch ref without requiring
    a checkout, so the developer's working tree is never disturbed.

    :param root_dir: Absolute path to the project root git repository.
    :param dev_branch: Name of the dev branch to commit state into.
    :returns: ``True`` on success, ``False`` on any git error.
    """
    state_files = [WORKFLOW_STATE_FILE, REPLAN_STATE_FILE]
    existing = [f for f in state_files if os.path.exists(f)]
    if not existing:
        return True

    tmp_index = None
    try:
        # Verify the branch exists
        res = subprocess.run(
            ["git", "rev-parse", "--verify", dev_branch],
            cwd=root_dir, capture_output=True, text=True,
        )
        if res.returncode != 0:
            return False

        # Create a temporary index file
        fd, tmp_index = tempfile.mkstemp(suffix=".idx")
        os.close(fd)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = tmp_index

        # Seed the temp index with the current dev branch tree
        subprocess.run(
            ["git", "read-tree", dev_branch],
            cwd=root_dir, env=env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Add each state file to the temp index
        for filepath in existing:
            rel_path = os.path.relpath(filepath, root_dir)
            hash_res = subprocess.run(
                ["git", "hash-object", "-w", filepath],
                cwd=root_dir, capture_output=True, text=True, check=True,
            )
            blob_hash = hash_res.stdout.strip()
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo",
                 f"100644,{blob_hash},{rel_path}"],
                cwd=root_dir, env=env, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        # Write the tree and create a commit
        tree_res = subprocess.run(
            ["git", "write-tree"],
            cwd=root_dir, env=env, capture_output=True, text=True, check=True,
        )
        tree_hash = tree_res.stdout.strip()

        commit_res = subprocess.run(
            ["git", "commit-tree", tree_hash, "-p", dev_branch,
             "-m", "Update workflow state"],
            cwd=root_dir, capture_output=True, text=True, check=True,
        )
        commit_hash = commit_res.stdout.strip()

        # Fast-forward the branch ref
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{dev_branch}", commit_hash],
            cwd=root_dir, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True

    except subprocess.CalledProcessError:
        return False
    finally:
        if tmp_index and os.path.exists(tmp_index):
            os.unlink(tmp_index)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

