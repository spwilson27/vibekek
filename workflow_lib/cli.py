"""Command-line interface for the AI project planning and execution workflow.

This module is the entry point for ``workflow.py`` (invoked as
``python workflow.py <command> [options]``).  It defines the argument parser,
dispatches to command handlers, and wires up supporting infrastructure such as
the :class:`~workflow_lib.executor.Logger` and signal handling.

Available commands
------------------

``setup``
    Create a virtual environment, install requirements, and copy project
    templates into the workspace.

``plan``
    Run the multi-phase planning orchestrator to generate all planning
    documents, requirements, epics, tasks, and DAGs.

``run``
    Execute the parallel implementation workflow, processing tasks from the
    generated DAGs and merging results into ``dev``.

``status``
    Show current plan and execution progress.

``validate``
    Run all verification scripts against the plan artefacts.

``block`` / ``unblock``
    Mark or unmark a task as blocked so it is skipped during ``run``.

``remove``
    Delete a task file and update the phase DAG accordingly.

``add``
    AI-generate a new task in a specific phase/sub-epic.

``add-feature``
    Discuss a feature brief with AI, produce a spec, then integrate into plan.

``modify-req``
    Add, remove, or edit requirements interactively.

``regen-dag``
    Rebuild the dependency DAG for a specific phase.

``regen-tasks``
    Regenerate task files for a phase or sub-epic.

``regen-components``
    Regenerate the shared components manifest.

``cascade``
    After manual task edits, rescan tasks, rebuild the DAG, and validate.

``fixup``
    Run validation and automatically fix failures (phase mappings + task coverage).
"""

import os
import sys
import subprocess
import threading
import argparse
import signal
from typing import Optional

from .constants import TOOLS_DIR, ROOT_DIR
from .orchestrator import Orchestrator
from .context import ProjectContext
from .replan import _make_runner, cmd_status, cmd_validate, cmd_block, cmd_unblock, cmd_remove, cmd_add, cmd_add_feature, cmd_modify_req, cmd_regen_dag, cmd_regen_tasks, cmd_regen_components, cmd_cascade, cmd_fixup
from .executor import execute_dag, Logger, signal_handler
from .dashboard import make_dashboard, _DashboardStream
from .config import get_serena_enabled, get_config_defaults, get_dev_branch, get_agent_pool_configs, set_context_limit_override
from .agent_pool import AgentPoolManager
from .state import load_workflow_state, load_dags, get_tasks_dir, restore_state_from_branch
from .runners import GeminiRunner, ClaudeRunner, CopilotRunner, OpencodeRunner, ClineRunner, AiderRunner, CodexRunner, QwenRunner, VALID_BACKENDS


def cmd_setup(args: argparse.Namespace) -> None:
    """Create a virtualenv, install requirements, and copy project templates.

    Steps:

    1. Creates ``.tools/.venv/`` (skipped when it already exists).
    2. Installs packages from ``.tools/requirements.txt`` using the venv pip.
    3. Copies template files (``.agent``, ``do``, ``.workflow.jsonc``) from
       ``.tools/templates/`` to the project root (skipped when already present).

    :param args: Parsed :mod:`argparse` namespace (no relevant attributes).
    :type args: argparse.Namespace
    """
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
    # Copy input directory to project root
    input_template_dir = os.path.join(templates_dir, "input")
    input_dst_dir = os.path.join(ROOT_DIR, "input")
    if os.path.isdir(input_template_dir):
        os.makedirs(input_dst_dir, exist_ok=True)
        for name in os.listdir(input_template_dir):
            src = os.path.join(input_template_dir, name)
            dst = os.path.join(input_dst_dir, name)
            if not os.path.isfile(src):
                continue
            if os.path.exists(dst):
                print(f"Already exists, skipping: {dst}")
                continue
            shutil.copy2(src, dst)
            print(f"Copied: {src} -> {dst}")

    for name in [".agent", "do", ".workflow.jsonc", "tests"]:
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


def cmd_plan(args: argparse.Namespace) -> None:
    """Run the multi-phase planning orchestrator.

    Sets up :class:`~workflow_lib.executor.Logger` on ``stdout``/``stderr``
    mirroring all output to ``plan_workflow.log`` in the project root.

    When ``--phase`` and ``--force`` are both supplied, the state flag for the
    specified phase is reset so it will re-run even if it was previously
    completed.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``backend`` (str) — AI backend to use.
        - ``jobs`` (int) — maximum parallel AI agents.
        - ``phase`` (Optional[str]) — target phase slug for ``--force``.
        - ``force`` (bool) — reset the specified phase's state before running.
    :type args: argparse.Namespace
    """
    log_file = os.path.join(ROOT_DIR, "plan_workflow.log")
    log_stream = open(log_file, "a", encoding="utf-8")

    runner = _make_runner(args.backend, model=args.model)

    with make_dashboard(log_file=log_stream) as dashboard:
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        sys.stdout = _DashboardStream(dashboard, original_stdout)
        sys.stderr = _DashboardStream(dashboard, original_stderr)
        try:
            ctx = ProjectContext(ROOT_DIR, runner=runner, jobs=args.jobs, dashboard=dashboard)
            ctx.ignore_sandbox = args.ignore_sandbox
            if args.phase and args.force:
                phase_state_keys = {
                    "3a-conflicts": ["conflict_resolution_completed"],
                    "3b-adversarial": ["adversarial_review_completed"],
                    "4-merge": ["requirements_merged"],
                    "4-scope": ["scope_gate_passed"],
                    "4-order": ["requirements_ordered"],
                    "5-epics": ["phases_completed"],
                    "5b-components": ["shared_components_completed"],
                    "5c-contracts": ["interface_contracts_completed"],
                    "6-tasks": ["tasks_completed"],
                    "6a-fixup": ["fixup_validation_completed"],
                    "6b-review": ["tasks_reviewed"],
                    "6c-cross-review": ["cross_phase_reviewed_pass_1", "cross_phase_reviewed_pass_2"],
                    "6d-reorder": ["tasks_reordered_pass_1", "tasks_reordered_pass_2"],
                    "6e-integration": ["integration_test_plan_completed"],
                    "7-dag": ["dag_completed"],
                }
                keys = phase_state_keys.get(args.phase)
                if keys:
                    for k in keys:
                        if ctx.state.get(k, False):
                            dashboard.log(f"--force: Resetting state for phase '{args.phase}' ({k}).")
                            ctx.state[k] = False
                    ctx.save_state()
                else:
                    dashboard.log(f"Warning: unknown phase '{args.phase}' for --force, ignoring.")

            orchestrator = Orchestrator(ctx, dashboard=dashboard,
                                       max_retries=args.retries, timeout=args.timeout,
                                       auto_retries=args.auto_retries)
            orchestrator.run()
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

def cmd_run(args: argparse.Namespace) -> None:
    """Execute the parallel implementation workflow.

    Sets up :class:`~workflow_lib.executor.Logger` on ``stdout``/``stderr``,
    installs a ``SIGINT`` handler for graceful shutdown, loads the merged DAG
    and workflow state from disk, prints the Serena integration status, and
    calls :func:`~workflow_lib.executor.execute_dag`.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``jobs`` (int) — number of parallel worker threads.
        - ``presubmit_cmd`` (str) — verification command.
        - ``backend`` (str) — AI backend to use.
    :type args: argparse.Namespace
    """
    dev_branch = get_dev_branch()

    result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, cwd=ROOT_DIR)
    current_branch = result.stdout.strip()
    if current_branch == dev_branch:
        print(f"Error: currently on dev branch '{dev_branch}'. Check out a different branch before running.", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    tasks_dir = get_tasks_dir()
    log_path = os.path.join(ROOT_DIR, "run_workflow.log")

    log_stream = open(log_path, "a", encoding="utf-8")

    restore_state_from_branch(ROOT_DIR, dev_branch)
    master_dag = load_dags(tasks_dir)
    state = load_workflow_state()

    serena_status = "enabled" if get_serena_enabled() else "disabled"

    # Build agent pool when --backend was not explicitly passed on the CLI.
    # args.backend is None here only when the user did not pass --backend;
    # main() layers in the config/hardcoded default only for other commands.
    agent_pool: Optional[AgentPoolManager] = None
    effective_backend: str = "gemini"
    if args.backend is None:
        agent_configs = get_agent_pool_configs()
        if agent_configs:
            agent_pool = AgentPoolManager(agent_configs)
            names = ", ".join(c.name for c in agent_configs)
            print(f"[Agents] Using pool: {names}")
        else:
            # No explicit --backend and no agents config: apply default.
            effective_backend = "gemini"
    else:
        effective_backend = args.backend

    lock = threading.Lock()
    original_stderr = sys.stderr
    sys.stderr = Logger(original_stderr, log_stream, lock)
    try:
        execute_dag(ROOT_DIR, master_dag, state, args.jobs, args.presubmit_cmd, effective_backend, log_file=log_stream, model=args.model, agent_pool=agent_pool, cleanup=args.cleanup)
    finally:
        sys.stderr = original_stderr

def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command handler.

    Builds the top-level :mod:`argparse` argument parser with subparsers for
    every supported command, then calls the matching handler function from
    the ``commands`` dispatch table.
    """
    # Shared flags available to all subcommands
    # Defaults are None so we can distinguish "not passed" from "passed".
    # Actual defaults are layered: hardcoded -> .workflow.jsonc -> CLI.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--backend", choices=sorted(VALID_BACKENDS), default=None, help="AI CLI backend to use (default: gemini)")
    shared.add_argument("--model", default=None, help="Model name to pass through to the AI CLI (e.g. 'claude-sonnet-4-5-20250514')")
    shared.add_argument("--ignore-sandbox", action="store_true", default=None, help="Disable sandbox violation checks")
    shared.add_argument("--context-limit", type=int, default=None, dest="context_limit", help="Override context limit in words (default: 126000)")

    parser = argparse.ArgumentParser(description="AI Project Planning and Execution Workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    sub.add_parser("setup", parents=[shared], help="Create virtualenv, install dependencies, and copy project templates")

    # plan
    p_plan = sub.add_parser("plan", parents=[shared], help="Multi-phase document generation orchestrator")
    p_plan.add_argument("--phase", default=None, help="Start from a specific phase, e.g. '4-merge'")
    p_plan.add_argument("--jobs", type=int, default=1, help="Maximum number of parallel AI agents/jobs")
    p_plan.add_argument("--force", action="store_true", help="Force re-run of the specified phase")
    p_plan.add_argument("--retries", type=int, default=None, help="Max retries per phase on failure (default: 3, use 0 to disable)")
    p_plan.add_argument("--auto-retries", type=int, default=None, help="Auto-retry up to N times before prompting user (default: none)")
    p_plan.add_argument("--timeout", type=int, default=None, help="Timeout in seconds per AI agent invocation (default: 600 = 10m)")

    # run
    p_run = sub.add_parser("run", parents=[shared], help="Parallel development workflow orchestrator")
    p_run.add_argument("--jobs", type=int, default=1, help="Number of parallel implementation agents")
    p_run.add_argument("--presubmit-cmd", type=str, default="./do presubmit", help="Command to evaluate correctness")
    p_run.add_argument("--cleanup", action="store_true", help="Remove temporary clones even on failure")

    # replan commands
    sub.add_parser("status", parents=[shared], help="Show plan and execution status")
    sub.add_parser("validate", parents=[shared], help="Run all verification checks")

    p_block = sub.add_parser("block", parents=[shared], help="Mark a task as blocked")
    p_block.add_argument("task", help="Task path")
    p_block.add_argument("--reason", required=True, help="Reason for blocking")
    p_block.add_argument("--dry-run", action="store_true")

    p_unblock = sub.add_parser("unblock", parents=[shared], help="Unblock a task")
    p_unblock.add_argument("task", help="Task path")
    p_unblock.add_argument("--dry-run", action="store_true")

    p_remove = sub.add_parser("remove", parents=[shared], help="Remove a task and update DAG")
    p_remove.add_argument("task", help="Task path")
    p_remove.add_argument("--dry-run", action="store_true")

    p_add = sub.add_parser("add", parents=[shared], help="AI-generate a new task")
    p_add.add_argument("phase_id", help="Phase (e.g., phase_1)")
    p_add.add_argument("sub_epic", help="Sub-epic directory name")
    p_add.add_argument("--desc", required=True, help="Description of the task to generate")
    p_add.add_argument("--dry-run", action="store_true")

    p_add_feat = sub.add_parser("add-feature", parents=[shared],
        help="Discuss a feature brief with AI, produce a spec, then integrate into plan")
    p_add_feat.add_argument("--brief", help="Path to a filled-in feature brief file")
    p_add_feat.add_argument("--spec", help="Path to existing spec (skip discussion, go straight to execution)")
    p_add_feat.add_argument("--phase", dest="phase_id", help="Target phase (e.g., phase_1)")
    p_add_feat.add_argument("--sub-epic", dest="sub_epic", help="Target sub-epic name")
    p_add_feat.add_argument("--dry-run", action="store_true")

    p_mod_req = sub.add_parser("modify-req", parents=[shared], help="Modify requirements.md")
    mg = p_mod_req.add_mutually_exclusive_group(required=True)
    mg.add_argument("--add", dest="add_req", metavar="DESC", help="Add a requirement (opens editor)")
    mg.add_argument("--remove", dest="remove_req", metavar="REQ_ID", help="Remove a requirement by ID")
    mg.add_argument("--edit", dest="edit_req", action="store_true", help="Open requirements.md in editor")
    p_mod_req.add_argument("--dry-run", action="store_true")

    p_regen_dag = sub.add_parser("regen-dag", parents=[shared], help="Rebuild DAG for all phases (or a single --phase)")
    p_regen_dag.add_argument("--phase", dest="phase_id", default=None, help="Limit to a single phase (e.g., phase_1)")
    p_regen_dag.add_argument("--dry-run", action="store_true")

    p_regen_tasks = sub.add_parser("regen-tasks", parents=[shared], help="Regenerate tasks for a phase/sub-epic")
    p_regen_tasks.add_argument("phase_id", help="Phase (e.g., phase_1)")
    p_regen_tasks.add_argument("--sub-epic", help="Target sub-epic (regenerates only this one)")
    p_regen_tasks.add_argument("--force", action="store_true", help="Override safety checks")
    p_regen_tasks.add_argument("--dry-run", action="store_true")

    p_regen_comp = sub.add_parser("regen-components", parents=[shared], help="Regenerate shared_components.md")
    p_regen_comp.add_argument("--dry-run", action="store_true")

    p_cascade = sub.add_parser("cascade", parents=[shared], help="After manual edits, rescan + rebuild DAG + validate")
    p_cascade.add_argument("phase_id", help="Phase (e.g., phase_1)")
    p_cascade.add_argument("--dry-run", action="store_true")

    p_fixup = sub.add_parser("fixup", parents=[shared], help="Run validation and automatically fix failures (phase mappings + task coverage)")
    p_fixup.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    # Layer defaults: hardcoded -> .workflow.jsonc -> CLI args.
    # For the `run` subcommand, backend is intentionally left as None when the
    # user did not pass --backend, so cmd_run can detect that and use the agent
    # pool from .workflow.jsonc instead. Apply the backend default only for
    # other subcommands.
    _HARDCODED = {
        "backend": "gemini",
        "model": None,
        "ignore_sandbox": False,
        "timeout": 600,
        "retries": 3,
        "auto_retries": None,
    }
    cfg_defaults = get_config_defaults()
    skip_backend_default = (args.command == "run")
    for key, hardcoded in _HARDCODED.items():
        if key == "backend" and skip_backend_default:
            continue
        if getattr(args, key, None) is None:
            setattr(args, key, cfg_defaults.get(key, hardcoded))

    if getattr(args, "context_limit", None) is not None:
        set_context_limit_override(args.context_limit)

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
        "add-feature": cmd_add_feature,
        "modify-req": cmd_modify_req,
        "regen-dag": cmd_regen_dag,
        "regen-tasks": cmd_regen_tasks,
        "regen-components": cmd_regen_components,
        "cascade": cmd_cascade,
        "fixup": cmd_fixup,
    }

    commands[args.command](args)

if __name__ == "__main__":
    main()
