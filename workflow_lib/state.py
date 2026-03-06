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
import json
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
    os.makedirs(os.path.dirname(REPLAN_STATE_FILE), exist_ok=True)
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
    state: Dict[str, Any] = {"completed_tasks": [], "merged_tasks": []}
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
    """Load and merge all per-phase DAG files into a single master DAG.

    For each ``phase_*/`` subdirectory of *tasks_dir*, the function prefers
    ``dag_reviewed.json`` (human-reviewed) over ``dag.json`` (AI-generated).
    Task IDs in the returned dict are fully-qualified with the phase prefix,
    e.g. ``"phase_1/sub/01_task.md"``.

    :param tasks_dir: Absolute path to the ``docs/plan/tasks/`` directory.
    :type tasks_dir: str
    :returns: Mapping of ``full_task_id -> [prerequisite_full_task_ids]``.
        Returns an empty dict when *tasks_dir* does not exist.
    :rtype: dict
    """
    master_dag: Dict[str, List[str]] = {}
    if not os.path.exists(tasks_dir):
        return master_dag
    for phase_dir in sorted(os.listdir(tasks_dir)):
        phase_path = os.path.join(tasks_dir, phase_dir)
        if not os.path.isdir(phase_path) or not phase_dir.startswith("phase_"):
            continue
        dag_file = os.path.join(phase_path, "dag_reviewed.json")
        if not os.path.exists(dag_file):
            dag_file = os.path.join(phase_path, "dag.json")
        if os.path.exists(dag_file):
            with open(dag_file, "r", encoding="utf-8") as f:
                try:
                    phase_dag = json.load(f)
                    for task_id, prereqs in phase_dag.items():
                        full_id = f"{phase_dir}/{task_id}"
                        master_dag[full_id] = [f"{phase_dir}/{p}" for p in prereqs]
                except json.JSONDecodeError:
                    pass
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


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

