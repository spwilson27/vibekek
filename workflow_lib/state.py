import os
import json
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from .constants import TOOLS_DIR, ROOT_DIR, WORKFLOW_STATE_FILE, REPLAN_STATE_FILE

def load_replan_state() -> Dict[str, Any]:
    state = {"blocked_tasks": {}, "removed_tasks": [], "replan_history": []}
    if os.path.exists(REPLAN_STATE_FILE):
        with open(REPLAN_STATE_FILE, "r", encoding="utf-8") as f:
            try:
                state.update(json.load(f))
            except json.JSONDecodeError:
                pass
    return state


def save_replan_state(state: Dict[str, Any]):
    os.makedirs(os.path.dirname(REPLAN_STATE_FILE), exist_ok=True)
    with open(REPLAN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)



def load_workflow_state() -> Dict[str, Any]:
    state = {"completed_tasks": [], "merged_tasks": []}
    if os.path.exists(WORKFLOW_STATE_FILE):
        with open(WORKFLOW_STATE_FILE, "r", encoding="utf-8") as f:
            try:
                state.update(json.load(f))
            except json.JSONDecodeError:
                pass
    return state

def save_workflow_state(state: Dict[str, Any]):
    with open(WORKFLOW_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)
def log_action(state: Dict[str, Any], action: str, target: str, details: str = ""):
    state.setdefault("replan_history", []).append({
        "action": action,
        "target": target,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def load_dags(tasks_dir: str) -> Dict[str, List[str]]:
    """Load all DAGs from task directories, matching run_workflow.py logic."""
    master_dag = {}
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
    return os.path.join(ROOT_DIR, "docs", "plan", "tasks")


def resolve_task_path(task_ref: str) -> str:
    """Resolve a task reference like phase_1/sub_epic/task.md to full path."""
    return os.path.join(get_tasks_dir(), task_ref)


def is_completed(task_ref: str, wf_state: Dict) -> bool:
    completed = set(wf_state.get("completed_tasks", []))
    merged = set(wf_state.get("merged_tasks", []))
    return task_ref in completed or task_ref in merged


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

