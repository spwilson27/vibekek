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

``docker``
    Start an interactive Docker container with the configured image, copying
    config files and cloning the repository on the dev branch.

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
    Run validation and automatically fix failures (phase mappings, task coverage, depends_on formatting, and DAG references).
"""

import os
import sys
import shlex
import subprocess
import threading
import argparse
import signal
import shutil
import tempfile
import json
from typing import Optional

from .constants import TOOLS_DIR, ROOT_DIR
from .orchestrator import Orchestrator
from .context import ProjectContext
from .replan import _make_runner, cmd_status, cmd_validate, cmd_block, cmd_unblock, cmd_remove, cmd_add, cmd_add_feature, cmd_modify_req, cmd_regen_dag, cmd_regen_tasks, cmd_regen_components, cmd_cascade, cmd_fixup
from .executor import (
    execute_dag,
    Logger,
    signal_handler,
    _docker_exec,
    _start_task_container,
    _stop_task_container,
    _write_container_env_file,
    get_task_details,
    get_project_context,
    get_memory_context,
    get_spec_context,
    get_shared_components_context,
    truncate_task_context,
)
from .dashboard import make_dashboard, _DashboardStream
from .config import get_config_defaults, get_dev_branch, get_agent_pool_configs, set_context_limit_override, set_agent_context_limit, get_docker_config, get_sccache_config, get_sccache_dist_config, get_sccache_services_config, ensure_sccache_services
from .agent_pool import AgentPoolManager, DockerConfig
from .state import load_workflow_state, load_dags, get_tasks_dir, restore_state_from_branch
from .runners import GeminiRunner, ClaudeRunner, CopilotRunner, OpencodeRunner, ClineRunner, AiderRunner, CodexRunner, QwenRunner, VALID_BACKENDS


def cmd_setup(args: argparse.Namespace) -> None:
    """Create a virtualenv, install requirements, and copy project templates.

    Steps:

    1. Creates ``.tools/.venv/`` (skipped when it already exists).
    2. Installs packages from ``.tools/requirements.txt`` using the venv pip.
    3. Copies template files (``.agent``, ``.workflow.jsonc``) from
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

    for name in [".agent", ".workflow.jsonc", "tests"]:
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

    # Build agent pool for parallel phases when pool configs are available.
    # The pool enables multi-agent parallelism in phases 2, 3, 7, 8, 17.
    agent_pool: Optional[AgentPoolManager] = None
    agent_configs = get_agent_pool_configs()
    if agent_configs:
        agent_pool = AgentPoolManager(agent_configs)
        # Auto-set jobs to total pool parallel capacity when user didn't
        # explicitly pass --jobs (default is 1).
        if args.jobs == 1:
            args.jobs = sum(ac.parallel for ac in agent_configs)
        # Apply per-agent context_limit for the default backend
        for _ac in agent_configs:
            if _ac.backend == args.backend and _ac.context_limit is not None:
                set_agent_context_limit(_ac.context_limit)
                break

    with make_dashboard(log_file=log_stream) as dashboard:
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        sys.stdout = _DashboardStream(dashboard, original_stdout)
        sys.stderr = _DashboardStream(dashboard, original_stderr)
        try:
            ctx = ProjectContext(ROOT_DIR, runner=runner, jobs=args.jobs, dashboard=dashboard)
            if args.phase and args.force:
                phase_state_keys = {
                    "5-conflicts": ["conflict_resolution_completed"],
                    "6-adversarial": ["adversarial_review_completed"],
                    "7-extract": ["requirements_extracted"],
                    "8-filter": ["meta_requirements_filtered"],
                    "9-merge": ["requirements_merged"],
                    "10-dedup": ["requirements_deduplicated"],
                    "11-order": ["requirements_ordered"],
                    "12-epics": ["epics_completed"],
                    "13-e2e": ["e2e_interfaces_completed"],
                    "14-gates": ["feature_gates_completed"],
                    "15-tasks": ["tasks_completed"],
                    "16-review": ["tasks_reviewed"],
                    "17-cross-review": ["cross_phase_reviewed"],
                    "18-pre-init": ["pre_init_task_completed"],
                    "19-dag": ["dag_completed"],
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

            if agent_pool:
                names = ", ".join(f"{c.name}({c.backend}, parallel={c.parallel})" for c in agent_configs)
                dashboard.log(f"[Plan] Agent pool: {names}")
                dashboard.log(f"[Plan] Parallel jobs: {args.jobs}")
            else:
                dashboard.log(f"[Plan] No agent pool configured (using single backend: {args.backend})")
                dashboard.log(f"[Plan] Parallel jobs: {args.jobs}")
            orchestrator = Orchestrator(ctx, dashboard=dashboard,
                                       max_retries=args.retries, timeout=args.timeout,
                                       auto_retries=args.auto_retries,
                                       agent_pool=agent_pool)
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

    # No longer an issue now that we use pivot
    # result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, cwd=ROOT_DIR)
    # current_branch = result.stdout.strip()
    #if current_branch == dev_branch:
    #    print(f"Error: currently on dev branch '{dev_branch}'. Check out a different branch before running.", file=sys.stderr)
    #    sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    tasks_dir = get_tasks_dir()
    log_path = os.path.join(ROOT_DIR, "run_workflow.log")

    log_stream = open(log_path, "a", encoding="utf-8")

    restore_state_from_branch(ROOT_DIR, dev_branch)
    master_dag = load_dags(tasks_dir)
    state = load_workflow_state()

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


def cmd_docker(args: argparse.Namespace) -> None:
    """Start a Docker container for debugging or run a command non-interactively.

    This command:

    1. Loads the Docker configuration from ``.workflow.jsonc``.
    2. Creates a temporary directory for the container workspace.
    3. Copies the configured config files into the temp directory.
    4. Starts a Docker container with the configured image.
    5. Uses ``docker cp`` to copy config files into the container.
    6. Inside the container, clones the git repository and checks out the dev branch.

    Configures sccache environment variables if enabled in ``.workflow.jsonc``,
    using the same logic as workflow agent containers.

    If ``--cmd`` is provided, runs the command and exits without git clone.
    If ``--validate-sccache`` is provided, validates sccache connectivity and exits.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``image`` (Optional[str]) — override the Docker image from config.
        - ``cmd`` (Optional[str]) — command to run non-interactively.
        - ``validate_sccache`` (bool) — validate sccache and exit.
    :type args: argparse.Namespace
    """
    docker_config = get_docker_config()
    if docker_config is None:
        print("Error: no 'docker' configuration found in .workflow.jsonc", file=sys.stderr)
        sys.exit(1)

    sccache_config = get_sccache_config()
    sccache_dist_config = get_sccache_dist_config()
    services_cfg = get_sccache_services_config()
    configure_containers = services_cfg.configure_containers if services_cfg else True

    image = args.image if args.image else docker_config.image
    effective_docker_config = DockerConfig(
        image=image,
        pivot_remote=docker_config.pivot_remote,
        volumes=list(docker_config.volumes),
        copy_files=list(docker_config.copy_files),
    )
    pivot_remote = docker_config.pivot_remote
    dev_branch = get_dev_branch()

    result = subprocess.run(
        ["git", "remote", "get-url", pivot_remote],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR
    )
    if result.returncode != 0:
        print(f"Error: could not get URL for remote '{pivot_remote}'", file=sys.stderr)
        sys.exit(1)
    repo_url = result.stdout.strip()

    container_name = f"workflow-docker-{os.getpid()}"

    def log(msg: str) -> None:
        print(msg)

    print(f"Starting Docker container with image: {image}")
    print(f"Git remote: {pivot_remote} -> {repo_url}")
    print(f"Dev branch: {dev_branch}")
    print()

    if configure_containers and (
        (sccache_config is not None and sccache_config.enabled) or
        (sccache_dist_config is not None and sccache_dist_config.enabled)
    ):
        sccache_ok, sccache_dist_ok = ensure_sccache_services()
        if sccache_config is not None and sccache_config.enabled and not sccache_ok:
            print("Warning: failed to auto-start the host sccache server via .tools/start-sccache.sh", file=sys.stderr)
        if sccache_dist_config is not None and sccache_dist_config.enabled and not sccache_dist_ok:
            print("Warning: failed to auto-start the host sccache-dist scheduler via .tools/start-sccache-dist.sh", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="workflow-docker-") as temp_dir:
        env_file = _write_container_env_file(temp_dir)
        _start_task_container(
            container_name,
            effective_docker_config,
            env_file,
            log,
            sccache_config=sccache_config,
            sccache_dist_config=sccache_dist_config,
            configure_containers=configure_containers,
        )
        try:
            sccache_result = _docker_exec(
                container_name,
                ["bash", "-lc", "command -v sccache"],
                env_file=env_file,
                capture=True,
            )
            if sccache_result.returncode == 0 and sccache_result.stdout.strip():
                print(f"      [sccache] Available at {sccache_result.stdout.strip()}")
            else:
                print("Warning: sccache is not available inside the Docker container", file=sys.stderr)

            # Verify Redis-backed sccache works inside container
            if configure_containers and sccache_config is not None and sccache_config.enabled:
                redis_url = f"redis://{sccache_config.redis_container}:{sccache_config.redis_port}"
                verify_cmd = f'test "$SCCACHE_REDIS" = {shlex.quote(redis_url)} && test "$RUSTC_WRAPPER" = "sccache"'
                env_result = _docker_exec(
                    container_name,
                    ["bash", "-lc", verify_cmd],
                    env_file=env_file,
                    capture=True,
                )
                if env_result.returncode == 0:
                    print(f"      [sccache] SCCACHE_REDIS={redis_url}")
                else:
                    print(f"Warning: SCCACHE_REDIS not set correctly inside container", file=sys.stderr)

                # Verify sccache can reach Redis and show stats
                stats_result = _docker_exec(
                    container_name,
                    ["bash", "-lc", "sccache --show-stats 2>&1 | head -5"],
                    env_file=env_file,
                    capture=True,
                )
                if stats_result.returncode == 0:
                    print(f"      [sccache] Local server OK (Redis-backed)")
                else:
                    print("Warning: sccache --show-stats failed inside container", file=sys.stderr)

            if configure_containers and sccache_dist_config is not None and sccache_dist_config.enabled:
                verify_route_cmd = (
                    f'test "$RUSTC_WRAPPER" = "sccache" && '
                    f'test "$SCCACHE_DIST_SCHEDULER_URL" = {shlex.quote(sccache_dist_config.scheduler_url)}'
                )
                route_result = _docker_exec(
                    container_name,
                    ["bash", "-lc", verify_route_cmd],
                    env_file=env_file,
                    capture=True,
                )
                if route_result.returncode == 0:
                    print(f"      [sccache] Routing via SCCACHE_DIST_SCHEDULER_URL={sccache_dist_config.scheduler_url}")
                else:
                    print(
                        f"Warning: expected SCCACHE_DIST_SCHEDULER_URL={sccache_dist_config.scheduler_url} inside container",
                        file=sys.stderr,
                    )

            # Write sccache config file for distributed compilation if enabled
            if configure_containers and sccache_dist_config is not None and sccache_dist_config.enabled:
                # Write config file inside container
                mkdir_result = _docker_exec(
                    container_name,
                    ["bash", "-lc", "mkdir -p ~/.config/sccache"],
                    env_file=env_file,
                    capture=True,
                )
                if mkdir_result.returncode == 0:
                    # Write config using echo commands
                    write_result = _docker_exec(
                        container_name,
                        ["bash", "-lc", f'echo "[dist]" > ~/.config/sccache/config.toml && echo "scheduler_url = {sccache_dist_config.scheduler_url}" >> ~/.config/sccache/config.toml && echo "auth_token = {sccache_dist_config.auth_token}" >> ~/.config/sccache/config.toml'],
                        env_file=env_file,
                        capture=True,
                    )
                    if write_result.returncode == 0:
                        print(f"      [sccache-dist] Wrote config file to ~/.config/sccache/config.toml")

                        # Verify the config was written
                        cat_result = _docker_exec(
                            container_name,
                            ["bash", "-lc", "cat ~/.config/sccache/config.toml"],
                            env_file=env_file,
                            capture=True,
                        )
                        if cat_result.returncode == 0:
                            print(f"              Config content:\n{cat_result.stdout}")

                        # Test dist-status after config is written
                        dist_status_result = _docker_exec(
                            container_name,
                            ["bash", "-lc", "sccache --dist-status"],
                            env_file=env_file,
                            capture=True,
                        )
                        if dist_status_result.returncode == 0:
                            output = dist_status_result.stdout.strip()
                            print(f"      [sccache-dist] --dist-status: {output}")
                            try:
                                status_json = json.loads(output)
                                if "Disabled" in status_json:
                                    print("      [sccache-dist] Note: sccache-dist client is ready but requires build servers to be registered")
                                    print("      [sccache-dist] To enable distributed compilation, run: .tools/install-sccache-dist.sh && .tools/start-sccache-dist.sh start")
                                elif "Scheduler" in status_json:
                                    print("      [sccache-dist] ✓ Connected to scheduler")
                                else:
                                    print("      [sccache-dist] ✓ Distributed compilation configured")
                            except json.JSONDecodeError:
                                pass
                        else:
                            print(f"      [sccache-dist] ✗ --dist-status failed: {dist_status_result.stderr}")
                    else:
                        print(f"      [sccache-dist] Warning: failed to write config file")
                else:
                    print(f"      [sccache-dist] Warning: failed to create config directory")

            # Handle --validate-sccache flag
            if args.validate_sccache:
                print("\n      [sccache] Running validation...")
                # Test sccache --show-stats
                stats_result = _docker_exec(
                    container_name,
                    ["bash", "-lc", "sccache --show-stats"],
                    env_file=env_file,
                    capture=True,
                )
                if stats_result.returncode == 0:
                    print("      [sccache] ✓ --show-stats succeeded")
                    # Parse stats output
                    for line in stats_result.stdout.split('\n'):
                        if 'Cache hits' in line or 'Cache misses' in line:
                            print(f"              {line.strip()}")
                else:
                    print(f"      [sccache] ✗ --show-stats failed: {stats_result.stderr}")

                # Test sccache --dist-status (will show disabled if dist not configured)
                dist_result = _docker_exec(
                    container_name,
                    ["bash", "-lc", "sccache --dist-status"],
                    env_file=env_file,
                    capture=True,
                )
                if dist_result.returncode == 0:
                    output = dist_result.stdout.strip()
                    print(f"      [sccache] --dist-status: {output}")
                    try:
                        status_json = json.loads(output)
                        if "Disabled" in status_json:
                            print("      [sccache] Note: distributed compilation is not configured (expected)")
                        else:
                            print("      [sccache] ✓ Distributed compilation is configured")
                    except json.JSONDecodeError:
                        pass
                else:
                    print(f"      [sccache] ✗ --dist-status failed: {dist_result.stderr}")

                print("\n      [sccache] Validation complete")
                print(f"Stopping container {container_name}...", file=sys.stderr)
                _stop_task_container(container_name, log)
                return

            # Handle --cmd flag (non-interactive, skip git clone)
            if args.cmd:
                print(f"\n      [docker] Running command: {args.cmd}")
                cmd_result = _docker_exec(
                    container_name,
                    ["bash", "-lc", args.cmd],
                    env_file=env_file,
                    capture=False,
                )
                print(f"Stopping container {container_name}...", file=sys.stderr)
                _stop_task_container(container_name, log)
                sys.exit(cmd_result.returncode if hasattr(cmd_result, 'returncode') else 0)

            _docker_exec(
                container_name,
                ["sed", "-i", "s|/home/[^/]*|/home/username|g", "/home/username/.gitconfig"],
                env_file=env_file,
                capture=True,
            )

            clone_and_checkout = [
                f"git clone --branch {shlex.quote(dev_branch)} {shlex.quote(repo_url)} /workspace/gooey",
                "cd /workspace/gooey",
                "git submodule update --init --recursive",
                "exec bash",
            ]
            full_shell_cmd = " && ".join(clone_and_checkout)
            exec_cmd = ["docker", "exec", "-it", container_name, "bash", "-lc", full_shell_cmd]
            subprocess.run(exec_cmd, check=False)
        finally:
            print(f"Stopping container {container_name}...", file=sys.stderr)
            _stop_task_container(container_name, log)


def cmd_task_prompt(args: argparse.Namespace) -> None:
    """Generate the full rendered prompt for a task and print it.

    Builds the same context that ``run`` would pass to an AI agent, renders
    the chosen prompt template with token-aware truncation, and writes the
    result to stdout (or a file with ``--output``).
    """
    from .config import get_context_limit

    task_path = args.task

    # Resolve to a full_task_id relative to docs/plan/tasks/
    tasks_root = os.path.join(ROOT_DIR, "docs", "plan", "tasks")
    if os.path.isabs(task_path) or os.path.exists(task_path):
        # Absolute or cwd-relative path — resolve to relative task id
        abs_path = os.path.abspath(task_path)
        if not os.path.exists(abs_path):
            print(f"Error: file not found: {abs_path}", file=sys.stderr)
            sys.exit(1)
        if abs_path.startswith(tasks_root + os.sep):
            full_task_id = os.path.relpath(abs_path, tasks_root)
        else:
            # File outside tasks dir — use get_task_details with absolute path
            full_task_id = abs_path
    else:
        # Treat as relative to docs/plan/tasks/
        full_task_id = task_path
        if not os.path.exists(os.path.join(tasks_root, full_task_id)):
            print(f"Error: task not found: {os.path.join(tasks_root, full_task_id)}", file=sys.stderr)
            sys.exit(1)

    # Derive phase_filename and task_name from full_task_id
    parts = full_task_id.replace(os.sep, "/").split("/", 1)
    phase_filename = parts[0] if len(parts) > 1 else "unknown"
    task_name = parts[1] if len(parts) > 1 else parts[0]

    # Read task details — handle both relative task IDs and absolute paths
    if os.path.isabs(full_task_id):
        with open(full_task_id, "r", encoding="utf-8") as f:
            task_details = f.read()
    else:
        task_details = get_task_details(full_task_id)

    if not task_details.strip():
        print(f"Error: no content found for task: {full_task_id}", file=sys.stderr)
        sys.exit(1)

    context = {
        "phase_filename": phase_filename,
        "task_name": task_name,
        "target_dir": full_task_id,
        "task_details": task_details,
        "description_ctx": get_project_context(),
        "memory_ctx": get_memory_context(ROOT_DIR),
        "clone_dir": ROOT_DIR,
        "spec_ctx": get_spec_context(),
        "shared_components_ctx": get_shared_components_context(),
    }

    # Load prompt template
    prompt_path = os.path.join(TOOLS_DIR, "prompts", args.prompt)
    if not os.path.exists(prompt_path):
        print(f"Error: prompt template not found: {prompt_path}", file=sys.stderr)
        sys.exit(1)
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt_tmpl = f.read()

    # Truncate and render
    effective_ctx = truncate_task_context(
        context, get_context_limit(), prompt_tmpl=prompt_tmpl,
    )
    prompt = prompt_tmpl
    for k, v in effective_ctx.items():
        prompt = prompt.replace(f"{{{k}}}", str(v))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(prompt)
        print(f"Prompt written to {args.output} ({len(prompt)} chars)", file=sys.stderr)
    else:
        print(prompt)


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
    shared.add_argument("--context-limit", type=int, default=None, dest="context_limit", help="Override context limit in tokens (default: 126000)")

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

    # docker
    p_docker = sub.add_parser("docker", parents=[shared], help="Start a Docker container for debugging")
    p_docker.add_argument("--image", default=None, help="Override the Docker image from .workflow.jsonc")
    p_docker.add_argument("--cmd", default=None, help="Run a command non-interactively (skip git clone)")
    p_docker.add_argument("--validate-sccache", action="store_true", help="Validate sccache connectivity and exit")

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

    p_mod_req = sub.add_parser("modify-req", parents=[shared], help="Modify docs/plan/requirements.json")
    mg = p_mod_req.add_mutually_exclusive_group(required=True)
    mg.add_argument("--add", dest="add_req", metavar="DESC", help="Add a requirement (opens editor)")
    mg.add_argument("--remove", dest="remove_req", metavar="REQ_ID", help="Remove a requirement by ID")
    mg.add_argument("--edit", dest="edit_req", action="store_true", help="Open docs/plan/requirements.json in editor")
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

    # task-prompt
    p_task_prompt = sub.add_parser("task-prompt", parents=[shared], help="Generate full task prompt for manual agent use")
    p_task_prompt.add_argument("task", help="Path to task .md file (absolute or relative to docs/plan/tasks/)")
    p_task_prompt.add_argument("--prompt", default="implement_task.md", help="Prompt template filename from .tools/prompts/ (default: implement_task.md)")
    p_task_prompt.add_argument("--output", "-o", default=None, help="Write prompt to file instead of stdout")

    args = parser.parse_args()

    # Layer defaults: hardcoded -> .workflow.jsonc -> CLI args.
    # For the `run` subcommand, backend is intentionally left as None when the
    # user did not pass --backend, so cmd_run can detect that and use the agent
    # pool from .workflow.jsonc instead. Apply the backend default only for
    # other subcommands.
    _HARDCODED = {
        "backend": "gemini",
        "model": None,
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
        "docker": cmd_docker,
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
        "task-prompt": cmd_task_prompt,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
