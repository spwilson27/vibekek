import os
import sys
import threading
import argparse
import signal

from .constants import TOOLS_DIR, ROOT_DIR
from .orchestrator import Orchestrator
from .context import ProjectContext
from .replan import _make_runner, cmd_status, cmd_validate, cmd_block, cmd_unblock, cmd_remove, cmd_add, cmd_modify_req, cmd_regen_dag, cmd_regen_tasks, cmd_regen_components, cmd_cascade
from .executor import execute_dag, Logger, signal_handler
from .state import load_workflow_state, load_dags, get_tasks_dir
from .runners import GeminiRunner, ClaudeRunner, CopilotRunner
def cmd_setup(args):
    venv_dir = os.path.join(TOOLS_DIR, ".venv")
    requirements = os.path.join(TOOLS_DIR, "requirements.txt")
    templates_dir = os.path.join(TOOLS_DIR, "templates")

    # Create virtualenv
    if os.path.isdir(venv_dir):
        print(f"Virtualenv already exists at {venv_dir}")
    else:
        print(f"Creating virtualenv at {venv_dir} ...")
        import subprocess
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
        print("Virtualenv created.")

    # Install requirements
    if not os.path.isfile(requirements):
        print(f"No requirements.txt found at {requirements}, skipping install.")
    else:
        import subprocess
        pip = os.path.join(venv_dir, "Scripts" if sys.platform == "win32" else "bin", "pip")
        print(f"Installing requirements from {requirements} ...")
        subprocess.run([pip, "install", "-r", requirements], check=True)
        print("Requirements installed.")

    # Copy templates
    import shutil
    for name in [".agent", "do.py", "ci.py"]:
        src = os.path.join(templates_dir, name)
        dst = os.path.join(ROOT_DIR, name)
        if not os.path.exists(src):
            print(f"Template not found, skipping: {src}")
            continue
        if os.path.exists(dst):
            print(f"Already exists, skipping: {dst}")
            continue
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        print(f"Copied: {src} -> {dst}")

    print("\nSetup complete.")


def cmd_plan(args):
    runner = _make_runner(args.backend)
    ctx = ProjectContext(ROOT_DIR, runner=runner, jobs=args.jobs)

    if args.phase and args.force:
        phase_state_keys = {
            "3b-adversarial": ["adversarial_review_completed"],
            "4-merge": ["requirements_merged"],
            "4-scope": ["scope_gate_passed"],
            "4-order": ["requirements_ordered"],
            "5-epics": ["phases_completed"],
            "5b-components": ["shared_components_completed"],
            "6-tasks": ["tasks_completed"],
            "6b-review": ["tasks_reviewed"],
            "6c-cross-review": ["cross_phase_reviewed_pass_1", "cross_phase_reviewed_pass_2"],
            "6d-reorder": ["tasks_reordered_pass_1", "tasks_reordered_pass_2"],
            "7-dag": ["dag_completed"],
        }
        keys = phase_state_keys.get(args.phase)
        if keys:
            for k in keys:
                if ctx.state.get(k, False):
                    print(f"--force: Resetting state for phase '{args.phase}' ({k}).")
                    ctx.state[k] = False
            ctx.save_state()
        else:
            print(f"Warning: unknown phase '{args.phase}' for --force, ignoring.")

    orchestrator = Orchestrator(ctx)
    orchestrator.run()

def cmd_run(args):
    signal.signal(signal.SIGINT, signal_handler)
    tasks_dir = get_tasks_dir()
    log_file = os.path.join(TOOLS_DIR, "run_workflow.log")

    log_stream = open(log_file, "a", encoding="utf-8")
    log_lock = threading.Lock()
    sys.stdout = Logger(sys.stdout, log_stream, log_lock)
    sys.stderr = Logger(sys.stderr, log_stream, log_lock)

    master_dag = load_dags(tasks_dir)
    state = load_workflow_state()
    
    print(f"Loaded {len(master_dag)} tasks across all phases.")
    execute_dag(ROOT_DIR, master_dag, state, args.jobs, args.presubmit_cmd, args.backend)

def main():
    parser = argparse.ArgumentParser(description="AI Project Planning and Execution Workflow")
    parser.add_argument("--backend", choices=["gemini", "claude", "copilot"], default="gemini", help="AI CLI backend to use (default: gemini)")
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    sub.add_parser("setup", help="Create virtualenv, install dependencies, and copy project templates")

    # plan
    p_plan = sub.add_parser("plan", help="Multi-phase document generation orchestrator")
    p_plan.add_argument("--phase", default=None, help="Start from a specific phase, e.g. '4-merge'")
    p_plan.add_argument("--jobs", type=int, default=1, help="Maximum number of parallel AI agents/jobs")
    p_plan.add_argument("--force", action="store_true", help="Force re-run of the specified phase")

    # run
    p_run = sub.add_parser("run", help="Parallel development workflow orchestrator")
    p_run.add_argument("--jobs", type=int, default=1, help="Number of parallel implementation agents")
    p_run.add_argument("--presubmit-cmd", type=str, default="./do presubmit", help="Command to evaluate correctness")

    # replan commands
    sub.add_parser("status", help="Show plan and execution status")
    sub.add_parser("validate", help="Run all verification checks")

    p_block = sub.add_parser("block", help="Mark a task as blocked")
    p_block.add_argument("task", help="Task path")
    p_block.add_argument("--reason", required=True, help="Reason for blocking")
    p_block.add_argument("--dry-run", action="store_true")

    p_unblock = sub.add_parser("unblock", help="Unblock a task")
    p_unblock.add_argument("task", help="Task path")
    p_unblock.add_argument("--dry-run", action="store_true")

    p_remove = sub.add_parser("remove", help="Remove a task and update DAG")
    p_remove.add_argument("task", help="Task path")
    p_remove.add_argument("--dry-run", action="store_true")

    p_add = sub.add_parser("add", help="AI-generate a new task")
    p_add.add_argument("phase_id", help="Phase (e.g., phase_1)")
    p_add.add_argument("sub_epic", help="Sub-epic directory name")
    p_add.add_argument("--desc", required=True, help="Description of the task to generate")
    p_add.add_argument("--dry-run", action="store_true")

    p_mod_req = sub.add_parser("modify-req", help="Modify requirements.md")
    mg = p_mod_req.add_mutually_exclusive_group(required=True)
    mg.add_argument("--add", dest="add_req", metavar="DESC", help="Add a requirement (opens editor)")
    mg.add_argument("--remove", dest="remove_req", metavar="REQ_ID", help="Remove a requirement by ID")
    mg.add_argument("--edit", dest="edit_req", action="store_true", help="Open requirements.md in editor")
    p_mod_req.add_argument("--dry-run", action="store_true")

    p_regen_dag = sub.add_parser("regen-dag", help="Rebuild DAG for a phase")
    p_regen_dag.add_argument("phase_id", help="Phase (e.g., phase_1)")
    p_regen_dag.add_argument("--dry-run", action="store_true")

    p_regen_tasks = sub.add_parser("regen-tasks", help="Regenerate tasks for a phase/sub-epic")
    p_regen_tasks.add_argument("phase_id", help="Phase (e.g., phase_1)")
    p_regen_tasks.add_argument("--sub-epic", help="Target sub-epic (regenerates only this one)")
    p_regen_tasks.add_argument("--force", action="store_true", help="Override safety checks")
    p_regen_tasks.add_argument("--dry-run", action="store_true")

    p_regen_comp = sub.add_parser("regen-components", help="Regenerate shared_components.md")
    p_regen_comp.add_argument("--dry-run", action="store_true")

    p_cascade = sub.add_parser("cascade", help="After manual edits, rescan + rebuild DAG + validate")
    p_cascade.add_argument("phase_id", help="Phase (e.g., phase_1)")
    p_cascade.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    commands = {
        "setup": cmd_setup,
        "plan": cmd_plan,
        "run": cmd_run,
        "status": cmd_status,
        "validate": cmd_validate,
        "block": cmd_block,
        "unblock": cmd_unblock,
        "remove": cmd_remove,
        "add": cmd_add,
        "modify-req": cmd_modify_req,
        "regen-dag": cmd_regen_dag,
        "regen-tasks": cmd_regen_tasks,
        "regen-components": cmd_regen_components,
        "cascade": cmd_cascade,
    }

    commands[args.command](args)

if __name__ == "__main__":
    main()
