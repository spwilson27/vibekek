"""Replan command implementations for mid-execution plan adjustments.

This module provides the CLI command handlers that allow developers to modify
the implementation plan after execution has started — blocking/unblocking tasks,
removing tasks, adding new ones, modifying requirements, regenerating DAGs or
task files, and cascading changes.

All write operations are logged to the replan audit trail via
:func:`~workflow_lib.state.log_action` and persisted with
:func:`~workflow_lib.state.save_replan_state`.

Command summary
---------------

+-------------------+----------------------------------------------------------+
| Function          | Purpose                                                  |
+===================+==========================================================+
| cmd_status        | Display plan progress grouped by phase.                  |
+-------------------+----------------------------------------------------------+
| cmd_validate      | Run all verification scripts against plan artefacts.     |
+-------------------+----------------------------------------------------------+
| cmd_block         | Mark a task as blocked (excluded from ``run``).          |
+-------------------+----------------------------------------------------------+
| cmd_unblock       | Remove a blocked status from a task.                     |
+-------------------+----------------------------------------------------------+
| cmd_remove        | Delete a task file and update the phase DAG.             |
+-------------------+----------------------------------------------------------+
| cmd_add           | AI-generate a new task file and rebuild the DAG.         |
+-------------------+----------------------------------------------------------+
| cmd_add_feature   | Discuss a feature brief, produce spec, integrate plan.   |
+-------------------+----------------------------------------------------------+
| cmd_modify_req    | Add, remove, or edit requirements interactively.         |
+-------------------+----------------------------------------------------------+
| cmd_regen_dag     | Rebuild the dependency DAG for one phase.                |
+-------------------+----------------------------------------------------------+
| cmd_regen_tasks   | Regenerate task files for a phase or sub-epic.           |
+-------------------+----------------------------------------------------------+
| cmd_regen_components | Regenerate the shared components manifest.            |
+-------------------+----------------------------------------------------------+
| cmd_cascade       | Rescan tasks, rebuild DAG, and validate after manual edits. |
+-------------------+----------------------------------------------------------+
| cmd_fixup         | Run validation and auto-fix phase/task mapping gaps.     |
+-------------------+----------------------------------------------------------+
"""

import os
import subprocess
import sys
import json
import re
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone

from .constants import TOOLS_DIR, ROOT_DIR

# Regex to match requirements like [REQ-123], [TAS-001], [REQ-SEC-001], etc.
_REQ_REGEX = re.compile(r"\[([A-Z0-9_]+-[A-Z0-9\._-]+)\]")


def parse_requirements(file_path: str):
    """Extract all requirement IDs from a file."""
    if not os.path.exists(file_path):
        return set()
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return set(_REQ_REGEX.findall(content))
from .state import *
from .executor import phase_sort_key
from .context import ProjectContext
from .runners import make_runner as _make_runner_from_runners
from .phases import Phase7ADAGGeneration


def cmd_status(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Show current plan and execution status grouped by phase.

    Prints a summary line with overall counts (completed, blocked, pending),
    then lists each task with an icon:

    * ``[x]`` — merged into ``dev``
    * ``[~]`` — completed but not yet merged
    * ``[B]`` — blocked (with reason)
    * ``[ ]`` — ready (all prerequisites met)
    * ``[.]`` — waiting on prerequisites

    Also prints any task files found on disk that are not in any DAG
    ("orphan" tasks).

    :param args: Parsed :mod:`argparse` namespace (no relevant attributes).
    :type args: argparse.Namespace
    """
    tasks_dir = get_tasks_dir()
    master_dag = load_dags(tasks_dir)
    wf_state = load_workflow_state()
    rp_state = load_replan_state()

    completed = set(wf_state.get("completed_tasks", []))
    merged = set(wf_state.get("merged_tasks", []))
    blocked = set(rp_state.get("blocked_tasks", {}).keys())

    # Group by phase
    phases: Dict[str, List[str]] = {}
    for task_id in sorted(master_dag.keys()):
        phase = task_id.split("/")[0]
        phases.setdefault(phase, []).append(task_id)

    # Also find tasks on disk not in DAG
    on_disk = set()
    if os.path.exists(tasks_dir):
        # Skip non-task files
        _NON_TASK_FILES = {
            "README.md",
            "SUB_EPIC_SUMMARY.md",
            "REQUIREMENTS_TRACEABILITY.md",
            "REQUIREMENTS_COVERAGE_MAP.md",
            "review_summary.md",
            "cross_phase_review_summary.md",
            "00_index.md",
        }
        _NON_TASK_JSON = {"dag.json", "dag_reviewed.json"}
        for phase_dir in sorted(os.listdir(tasks_dir)):
            phase_path = os.path.join(tasks_dir, phase_dir)
            if not os.path.isdir(phase_path) or not phase_dir.startswith("phase_"):
                continue
            for entry in sorted(os.listdir(phase_path)):
                entry_path = os.path.join(phase_path, entry)
                # Flat task .json sidecar files (the canonical source)
                if entry.endswith(".json") and os.path.isfile(entry_path) and entry not in _NON_TASK_JSON:
                    # Use task_id from JSON if present (matches load_dags behavior)
                    try:
                        with open(entry_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        task_ref = meta.get("task_id", f"{phase_dir}/{entry[:-5]}")
                    except (json.JSONDecodeError, OSError):
                        task_ref = f"{phase_dir}/{entry[:-5]}"
                    on_disk.add(task_ref)
                # Legacy subdirectory structure
                elif os.path.isdir(entry_path):
                    for sub in sorted(os.listdir(entry_path)):
                        sub_path = os.path.join(entry_path, sub)
                        if sub.endswith(".json") and os.path.isfile(sub_path):
                            fallback_ref = f"{phase_dir}/{entry}/{sub[:-5]}"
                            try:
                                with open(sub_path, "r", encoding="utf-8") as f:
                                    meta = json.load(f)
                                task_ref = meta.get("task_id", fallback_ref)
                                # Normalize: match load_dags behavior
                                if not task_ref.startswith("phase_"):
                                    task_ref = f"{phase_dir}/{entry}/{task_ref}"
                            except (json.JSONDecodeError, OSError):
                                task_ref = fallback_ref
                            on_disk.add(task_ref)

    total = len(master_dag)
    n_completed = len(completed)
    n_blocked = len(blocked)
    n_pending = total - n_completed - n_blocked

    print(f"\nPlan Status: {n_completed}/{total} completed, {n_blocked} blocked, {n_pending} pending\n")

    for phase in sorted(phases.keys()):
        tasks = phases[phase]
        phase_done = sum(1 for t in tasks if t in completed)
        print(f"  {phase} ({phase_done}/{len(tasks)})")
        for task_id in tasks:
            prereqs = master_dag.get(task_id, [])
            prereqs_met = all(p in completed for p in prereqs)
            if task_id in merged:
                icon = "  [x]"
            elif task_id in completed:
                icon = "  [~]"
            elif task_id in blocked:
                reason = rp_state["blocked_tasks"].get(task_id, {}).get("reason", "")
                print(f"    [B] {task_id}  -- blocked: {reason}")
                continue
            elif prereqs_met:
                icon = "  [ ]"  # ready
            else:
                icon = "  [.]"  # waiting
            print(f"  {icon} {task_id}")

    # Orphan tasks (on disk but not in DAG)
    orphans = on_disk - set(master_dag.keys())
    if orphans:
        print(f"\n  Orphan tasks (not in any DAG):")
        for o in sorted(orphans):
            print(f"    ? {o}")

    print()


def _run_all_checks(quiet: bool = False) -> Dict[str, Any]:
    """Run all validation checks via ``validate.py --all`` and return results.

    :param quiet: When ``True``, suppress printed output.
    :returns: Dict with ``"all_pass"`` bool and ``"output"`` str.
    :rtype: Dict[str, Any]
    """
    validate_script = os.path.join(TOOLS_DIR, "validate.py")

    results: Dict[str, Any] = {"all_pass": True, "output": ""}

    res = subprocess.run(
        [sys.executable, validate_script, "--all"],
        capture_output=True, text=True, cwd=ROOT_DIR
    )

    results["all_pass"] = res.returncode == 0
    results["output"] = res.stdout.strip()

    if not quiet:
        if results["output"]:
            print(results["output"])
        if res.stderr.strip():
            print(res.stderr.strip())

    return results


def cmd_validate(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Run all validation checks via ``validate.py --all``.

    :param args: Parsed :mod:`argparse` namespace (no relevant attributes).
    :type args: argparse.Namespace
    :raises SystemExit: Exits ``0`` on all-pass or ``1`` on any failure.
    """
    results = _run_all_checks()
    sys.exit(0 if results["all_pass"] else 1)


def cmd_block(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Mark a task as blocked so it is skipped by the ``run`` command.

    Validates that the task exists on disk and has not already been completed
    or merged.  Supports ``--dry-run`` to preview the operation without
    writing state.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``task`` (str) — relative task ref, e.g. ``"phase_1/api/01_setup.md"``.
        - ``reason`` (str) — human-readable reason for blocking.
        - ``dry_run`` (bool) — preview mode.
    :type args: argparse.Namespace
    :raises SystemExit: When the task is already completed or not found.
    """
    task_ref = args.task
    reason = args.reason
    wf_state = load_workflow_state()

    if is_completed(task_ref, wf_state):
        print(f"Error: {task_ref} is already completed/merged. Cannot block.")
        sys.exit(1)

    full_path = resolve_task_path(task_ref)
    if not os.path.exists(full_path):
        print(f"Error: Task file not found: {full_path}")
        sys.exit(1)

    rp_state = load_replan_state()

    if args.dry_run:
        print(f"[dry-run] Would block: {task_ref}")
        print(f"[dry-run] Reason: {reason}")
        return

    rp_state.setdefault("blocked_tasks", {})[task_ref] = {
        "reason": reason,
        "blocked_at": datetime.now(timezone.utc).isoformat(),
        "blocked_by": "user",
    }
    log_action(rp_state, "block", task_ref, reason)
    save_replan_state(rp_state)
    print(f"Blocked: {task_ref}")


def cmd_unblock(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Remove a blocked status from a task.

    If the task is not currently blocked, prints a message and returns without
    modifying state.  Supports ``--dry-run``.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``task`` (str) — relative task ref.
        - ``dry_run`` (bool) — preview mode.
    :type args: argparse.Namespace
    """
    task_ref = args.task
    rp_state = load_replan_state()

    if task_ref not in rp_state.get("blocked_tasks", {}):
        print(f"Task {task_ref} is not blocked.")
        return

    if args.dry_run:
        print(f"[dry-run] Would unblock: {task_ref}")
        return

    del rp_state["blocked_tasks"][task_ref]
    log_action(rp_state, "unblock", task_ref)
    save_replan_state(rp_state)
    print(f"Unblocked: {task_ref}")


def cmd_remove(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Remove a task file, update the phase DAG, and log orphaned requirements.

    Steps:

    1. Validate the task is not completed/merged and exists on disk.
    2. Parse requirement IDs from the task file.
    3. Delete the file.
    4. Remove the task entry and all references to it from ``dag.json``
       (or ``dag_reviewed.json``).
    5. Remove the task from the blocked list if present.
    6. Log the removal and orphaned requirements to the replan audit trail.
    7. Print a warning for any requirements that are now uncovered.

    Supports ``--dry-run``.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``task`` (str) — relative task ref.
        - ``dry_run`` (bool) — preview mode.
    :type args: argparse.Namespace
    :raises SystemExit: When the task is already completed or not found.
    """
    task_ref = args.task
    wf_state = load_workflow_state()

    if is_completed(task_ref, wf_state):
        print(f"Error: {task_ref} is already completed/merged. Cannot remove.")
        sys.exit(1)

    full_path = resolve_task_path(task_ref)
    if not os.path.exists(full_path):
        print(f"Error: Task file not found: {full_path}")
        sys.exit(1)

    # Parse requirement IDs from the task
    task_reqs = parse_requirements(full_path)

    # Determine phase
    parts = task_ref.split("/")
    phase_id = parts[0]
    # The DAG task key is sub_epic/file (without phase prefix)
    dag_task_key = "/".join(parts[1:])

    phase_dir = os.path.join(get_tasks_dir(), phase_id)
    dag_file = os.path.join(phase_dir, "dag_reviewed.json")
    if not os.path.exists(dag_file):
        dag_file = os.path.join(phase_dir, "dag.json")

    if args.dry_run:
        print(f"[dry-run] Would remove: {task_ref}")
        print(f"[dry-run] Orphaned requirements: {sorted(task_reqs)}")
        return

    # Delete the file
    os.remove(full_path)
    print(f"Deleted: {full_path}")

    # Update DAG
    if os.path.exists(dag_file):
        with open(dag_file, "r", encoding="utf-8") as f:
            dag = json.load(f)

        # Remove the task entry
        dag.pop(dag_task_key, None)

        # Remove from other tasks' dependency lists
        for tid in dag:
            if dag_task_key in dag[tid]:
                dag[tid].remove(dag_task_key)

        with open(dag_file, "w", encoding="utf-8") as f:
            json.dump(dag, f, indent=2)
        print(f"Updated DAG: {dag_file}")

    # Remove from blocked list if present
    rp_state = load_replan_state()
    if task_ref in rp_state.get("blocked_tasks", {}):
        del rp_state["blocked_tasks"][task_ref]

    rp_state.setdefault("removed_tasks", []).append({
        "task_id": task_ref,
        "removed_at": datetime.now(timezone.utc).isoformat(),
        "orphaned_reqs": sorted(task_reqs),
    })
    log_action(rp_state, "remove", task_ref, f"orphaned_reqs={sorted(task_reqs)}")
    save_replan_state(rp_state)

    if task_reqs:
        print(f"\nWARNING: The following requirements are no longer covered by any task in this phase:")
        for r in sorted(task_reqs):
            print(f"  - [{r}]")
        print("Consider: replan.py add, or replan.py modify-req --remove")


def cmd_add(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """AI-generate a new task file in a phase/sub-epic and rebuild the DAG.

    Determines the next sequential task number, gathers existing task content
    as context, renders the ``add_task.md`` prompt, and runs the AI agent to
    create the file.  Then calls :func:`_rebuild_phase_dag`.

    Supports ``--dry-run`` (prints the intended path without generating).

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``phase_id`` (str) — phase directory name, e.g. ``"phase_1"``.
        - ``sub_epic`` (str) — sub-epic directory name.
        - ``desc`` (str) — natural-language description of the new task.
        - ``dry_run`` (bool) — preview mode.
        - ``backend`` (str) — AI backend to use.
    :type args: argparse.Namespace
    :raises SystemExit: When the phase directory is not found, the AI runner
        fails, or no file is created by the agent.
    """
    phase_id = args.phase_id
    sub_epic = args.sub_epic
    description = args.desc
    backend = args.backend

    runner = _make_runner(backend, model=getattr(args, 'model', None))
    ctx = ProjectContext(ROOT_DIR, runner=runner)

    phase_dir = os.path.join(get_tasks_dir(), phase_id)
    se_dir = os.path.join(phase_dir, sub_epic)

    if not os.path.isdir(phase_dir):
        print(f"Error: Phase directory not found: {phase_dir}")
        sys.exit(1)

    os.makedirs(se_dir, exist_ok=True)

    # Determine next task number
    existing = sorted([f for f in os.listdir(se_dir) if f.endswith(".md")]) if os.path.isdir(se_dir) else []
    next_num = len(existing) + 1
    task_filename = f"{next_num:02d}_new_task.md"

    # Gather existing tasks content
    existing_content = ""
    for md_file in existing:
        with open(os.path.join(se_dir, md_file), "r", encoding="utf-8") as f:
            existing_content += f"### {md_file}\n{f.read()}\n\n"

    shared_components_ctx = ctx.load_shared_components()
    target_dir = f"{phase_id}/{sub_epic}"
    phase_filename = f"{phase_id}.md"

    prompt_tmpl = ctx.load_prompt("add_task.md")
    prompt = ctx.format_prompt(prompt_tmpl,
        description_ctx=ctx.description_ctx,
        shared_components_ctx=shared_components_ctx,
        existing_tasks_content=existing_content or "(none)",
        phase_filename=phase_filename,
        target_dir=target_dir,
        task_filename=task_filename,
        sub_epic_name=sub_epic,
        user_description=description,
    )

    if args.dry_run:
        print(f"[dry-run] Would generate task at: docs/plan/tasks/{target_dir}/{task_filename}")
        print(f"[dry-run] Description: {description}")
        return

    expected_file = os.path.join(se_dir, task_filename)
    allowed_files = [se_dir + os.sep]
    result = ctx.run_ai(prompt, allowed_files=allowed_files)

    if result.returncode != 0:
        print(f"\n[!] Error generating task.")
        print(result.stdout)
        print(result.stderr)
        sys.exit(1)

    # Find what file was actually created (agent may choose a different name)
    new_files = sorted(set(os.listdir(se_dir)) - set(existing) if os.path.isdir(se_dir) else [])
    if new_files:
        print(f"Created: docs/plan/tasks/{target_dir}/{new_files[0]}")
    else:
        print("Warning: No new task file was created by the agent.")
        sys.exit(1)

    # Rebuild DAG
    print("Rebuilding DAG...")
    _rebuild_phase_dag(phase_dir, ctx)

    rp_state = load_replan_state()
    log_action(rp_state, "add", f"{target_dir}/{new_files[0]}", description)
    save_replan_state(rp_state)


def cmd_modify_req(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Add, remove, or interactively edit ``docs/plan/requirements.json``.

    Exactly one of the mutually exclusive flags must be set:

    * ``--add`` — opens ``$EDITOR`` for the user to append a requirement, then
      runs ``verify-master``.
    * ``--remove <REQ_ID>`` — moves the requirement block to a
      "Removed or Modified Requirements" section and shows affected tasks.
    * ``--edit`` — opens ``$EDITOR`` directly on ``docs/plan/requirements.json``, then
      runs ``verify-master``.

    Supports ``--dry-run`` for ``--remove`` (shows affected tasks without
    writing).

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``add_req`` (Optional[str]) — description for ``--add``.
        - ``remove_req`` (Optional[str]) — requirement ID for ``--remove``.
        - ``edit_req`` (bool) — flag for ``--edit``.
        - ``dry_run`` (bool) — preview mode.
    :type args: argparse.Namespace
    :raises SystemExit: When ``docs/plan/requirements.json`` is not found or the
        requirement ID is not present.
    """
    req_file = os.path.join(ROOT_DIR, "docs/plan/requirements.json")
    if not os.path.exists(req_file):
        print(f"Error: {req_file} not found.")
        sys.exit(1)

    if args.edit_req:
        # Open editor
        editor = os.environ.get("EDITOR", "vim")
        subprocess.run([editor, req_file])
        # Verify
        _run_validate(phase=9)
        return

    if args.remove_req:
        req_id = args.remove_req
        with open(req_file, "r", encoding="utf-8") as f:
            content = f.read()

        if f"[{req_id}]" not in content:
            print(f"Requirement [{req_id}] not found in requirements.json")
            sys.exit(1)

        if args.dry_run:
            # Show affected tasks
            _show_affected_tasks(req_id)
            return

        # Move to removed section
        # Find the requirement block (### **[ID]** ... up to next ### or end)
        pattern = rf'(### \*\*\[{re.escape(req_id)}\]\*\*.*?)(?=\n### |\Z)'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            req_block = match.group(1).strip()
            content = content[:match.start()] + content[match.end():]

            removed_section = "\n## Removed or Modified Requirements\n"
            if "## Removed or Modified Requirements" in content:
                content += f"\n### **[{req_id}]** (Removed via replan)\n- **Action:** Removed\n- **Rationale:** Removed during replanning\n"
            else:
                content += f"\n{removed_section}\n### **[{req_id}]** (Removed via replan)\n- **Action:** Removed\n- **Rationale:** Removed during replanning\n"

            with open(req_file, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"Removed [{req_id}] from active requirements.")

        _show_affected_tasks(req_id)

        rp_state = load_replan_state()
        log_action(rp_state, "remove-req", req_id)
        save_replan_state(rp_state)

    if args.add_req:
        print("Adding a requirement interactively...")
        editor = os.environ.get("EDITOR", "vim")
        subprocess.run([editor, req_file])
        _run_validate(phase=9)

        rp_state = load_replan_state()
        log_action(rp_state, "add-req", args.add_req)
        save_replan_state(rp_state)


def cmd_regen_dag(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Rebuild the dependency DAG for all phases, or a single ``--phase``.

    Delegates to :func:`_rebuild_phase_dag` which first attempts a
    programmatic build from task metadata, then falls back to AI inference.

    Supports ``--dry-run``.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``phase_id`` (str | None) — optional phase directory name.
        - ``dry_run`` (bool) — preview mode.
        - ``backend`` (str) — AI backend for fallback DAG generation.
    :type args: argparse.Namespace
    :raises SystemExit: When a specified phase directory is not found.
    """
    tasks_dir = get_tasks_dir()
    phase_id = getattr(args, "phase_id", None)

    if phase_id:
        phase_ids = [phase_id]
    else:
        if not os.path.isdir(tasks_dir):
            print(f"Error: Tasks directory not found: {tasks_dir}")
            sys.exit(1)
        phase_ids = sorted(
            d for d in os.listdir(tasks_dir)
            if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")
        )
        if not phase_ids:
            print("No phase directories found.")
            return

    for pid in phase_ids:
        phase_dir = os.path.join(tasks_dir, pid)
        if not os.path.isdir(phase_dir):
            print(f"Error: Phase directory not found: {phase_dir}")
            sys.exit(1)

        if args.dry_run:
            print(f"[dry-run] Would rebuild DAG for {pid}")
            continue

        runner = _make_runner(args.backend, model=getattr(args, 'model', None))
        ctx = ProjectContext(ROOT_DIR, runner=runner)
        _rebuild_phase_dag(phase_dir, ctx)

        rp_state = load_replan_state()
        log_action(rp_state, "regen-dag", pid)
        save_replan_state(rp_state)


def cmd_regen_tasks(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Regenerate task files for a phase or a specific sub-epic.

    When ``--sub-epic`` is given, clears existing ``.md`` files in that
    directory, regenerates them using the ``tasks.md`` prompt (reading
    requirement IDs from the phase grouping JSON), then rebuilds the phase
    DAG.  Full-phase regeneration (without ``--sub-epic``) is not yet
    implemented.

    Safety: refuses to overwrite completed tasks unless ``--force`` is passed.
    Supports ``--dry-run``.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``phase_id`` (str) — phase directory name.
        - ``sub_epic`` (Optional[str]) — sub-epic name to target.
        - ``force`` (bool) — override completed-task safety check.
        - ``dry_run`` (bool) — preview mode.
        - ``backend`` (str) — AI backend.
    :type args: argparse.Namespace
    :raises SystemExit: On missing directories, completed tasks (without
        ``--force``), missing grouping JSON, or AI runner failure.
    """
    phase_id = args.phase_id
    sub_epic = args.sub_epic
    backend = args.backend

    runner = _make_runner(backend, model=getattr(args, 'model', None))
    ctx = ProjectContext(ROOT_DIR, runner=runner)
    wf_state = load_workflow_state()

    phase_dir = os.path.join(get_tasks_dir(), phase_id)
    if not os.path.isdir(phase_dir):
        print(f"Error: Phase directory not found: {phase_dir}")
        sys.exit(1)

    if sub_epic:
        se_dir = os.path.join(phase_dir, sub_epic)
        if not os.path.isdir(se_dir):
            print(f"Error: Sub-epic directory not found: {se_dir}")
            sys.exit(1)

        # Check for completed tasks
        completed = set(wf_state.get("completed_tasks", []))
        for md in os.listdir(se_dir):
            if md.endswith(".md"):
                full_ref = f"{phase_id}/{sub_epic}/{md}"
                if full_ref in completed:
                    print(f"Error: {full_ref} is already completed. Use --force to override.")
                    if not args.force:
                        sys.exit(1)

        if args.dry_run:
            print(f"[dry-run] Would regenerate tasks for {phase_id}/{sub_epic}")
            return

        # Find the grouping JSON to get requirement IDs
        grouping_file = os.path.join(get_tasks_dir(), f"{phase_id}_grouping.json")
        if not os.path.exists(grouping_file):
            print(f"Error: Grouping file not found: {grouping_file}")
            sys.exit(1)

        with open(grouping_file, "r", encoding="utf-8") as f:
            sub_epics = json.load(f)

        # Find matching sub-epic (keys may have prefixes like "01_")
        matching_key = None
        matching_reqs = None
        for key, reqs in sub_epics.items():
            safe_name = re.sub(r'[^a-zA-Z0-9_\-]+', '_', key.lower())
            if safe_name == sub_epic:
                matching_key = key
                matching_reqs = reqs
                break

        if not matching_key:
            print(f"Error: Sub-epic '{sub_epic}' not found in grouping file.")
            print(f"Available: {list(sub_epics.keys())}")
            sys.exit(1)

        # Clear existing tasks
        for md in os.listdir(se_dir):
            if md.endswith(".md"):
                os.remove(os.path.join(se_dir, md))

        # Regenerate using tasks.md prompt
        tasks_prompt_tmpl = ctx.load_prompt("tasks.md")
        shared_components_ctx = ctx.load_shared_components()
        target_dir = f"{phase_id}/{sub_epic}"
        reqs_str = json.dumps(matching_reqs)

        prompt = ctx.format_prompt(tasks_prompt_tmpl,
            description_ctx=ctx.description_ctx,
            phase_filename=f"{phase_id}.md",
            sub_epic_name=matching_key,
            sub_epic_reqs=reqs_str,
            target_dir=target_dir,
            shared_components_ctx=shared_components_ctx,
        )
        allowed_files = [se_dir + os.sep]
        result = ctx.run_ai(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print(f"\n[!] Error regenerating tasks for {target_dir}.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)

        print(f"Regenerated tasks for {target_dir}")
    else:
        if args.dry_run:
            print(f"[dry-run] Would regenerate all tasks for {phase_id}")
            return
        print("Full phase regeneration not yet implemented. Use --sub-epic to target a specific sub-epic.")
        sys.exit(1)

    # Rebuild DAG
    print("Rebuilding DAG...")
    _rebuild_phase_dag(phase_dir, ctx)

    rp_state = load_replan_state()
    log_action(rp_state, "regen-tasks", f"{phase_id}/{sub_epic or 'all'}")
    save_replan_state(rp_state)


def cmd_regen_components(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Deprecated: shared components phase has been replaced by E2E interfaces.

    :param args: Parsed :mod:`argparse` namespace.
    :type args: argparse.Namespace
    """
    print("The regen-components command has been removed. "
          "Shared components are now handled via E2E interfaces (Phase 14) "
          "and feature gates (Phase 15).")
    sys.exit(1)


def cmd_cascade(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Rescan tasks, rebuild the phase DAG, and run validation after manual edits.

    Steps:

    1. Walk all task files in the phase directory and aggregate requirement IDs.
    2. Compare coverage against the phase epic document and warn about orphaned
       requirements.
    3. Rebuild the phase DAG via :func:`_rebuild_phase_dag`.
    4. Run ``verify-dags`` validation.

    Supports ``--dry-run``.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``phase_id`` (str) — phase directory name.
        - ``dry_run`` (bool) — preview mode.
        - ``backend`` (str) — AI backend.
    :type args: argparse.Namespace
    :raises SystemExit: When the phase directory is not found.
    """
    phase_id = args.phase_id
    phase_dir = os.path.join(get_tasks_dir(), phase_id)

    if not os.path.isdir(phase_dir):
        print(f"Error: Phase directory not found: {phase_dir}")
        sys.exit(1)

    if args.dry_run:
        print(f"[dry-run] Would cascade changes for {phase_id}")
        return

    runner = _make_runner(args.backend, model=getattr(args, 'model', None))
    ctx = ProjectContext(ROOT_DIR, runner=runner)

    # Scan tasks and collect requirement coverage
    print(f"Scanning tasks in {phase_id}...")
    task_reqs = set()
    task_count = 0
    # Skip non-task files (READMEs, summaries, etc.)
    _NON_TASK_FILES = {
        "README.md",
        "SUB_EPIC_SUMMARY.md",
        "REQUIREMENTS_TRACEABILITY.md",
        "review_summary.md",
    }
    for sub_epic in sorted(os.listdir(phase_dir)):
        se_path = os.path.join(phase_dir, sub_epic)
        if not os.path.isdir(se_path):
            continue
        for md in sorted(os.listdir(se_path)):
            if md.endswith(".md") and md not in _NON_TASK_FILES:
                reqs = parse_requirements(os.path.join(se_path, md))
                task_reqs.update(reqs)
                task_count += 1

    # Check against phase requirements
    phases_dir = os.path.join(ROOT_DIR, "docs", "plan", "phases")
    phase_file = os.path.join(phases_dir, f"{phase_id}.md")
    if os.path.exists(phase_file):
        phase_reqs = parse_requirements(phase_file)
        orphaned = phase_reqs - task_reqs
        if orphaned:
            print(f"\nWARNING: {len(orphaned)} requirements in {phase_id}.md not covered by any task:")
            for r in sorted(orphaned):
                print(f"  - [{r}]")

    print(f"Found {task_count} tasks covering {len(task_reqs)} requirements.")

    # Rebuild DAG
    print("Rebuilding DAG...")
    _rebuild_phase_dag(phase_dir, ctx)

    # Validate
    print("Running validation...")
    _run_validate(phase=20)

    rp_state = load_replan_state()
    log_action(rp_state, "cascade", phase_id)
    save_replan_state(rp_state)


def _fix_phase_mappings(unmapped_reqs: List[str], ctx: ProjectContext, dry_run: bool = False) -> bool:
    """Fix verify-phases failures by assigning unmapped requirements to phases.

    :param unmapped_reqs: List of requirement IDs missing from all phase files.
    :param ctx: Project context with AI runner.
    :param dry_run: Preview mode — don't actually run AI.
    :returns: ``True`` if fix was attempted (or dry-run shown), ``False`` if nothing to do.
    """
    if not unmapped_reqs:
        return False

    plan_dir = os.path.join(ROOT_DIR, "docs", "plan")
    phases_dir = os.path.join(plan_dir, "phases")
    req_file = os.path.join(ROOT_DIR, "docs/plan/requirements.json")

    print(f"\n=> Fixing {len(unmapped_reqs)} requirement(s) not mapped to any phase:")
    for r in sorted(unmapped_reqs):
        print(f"  - [{r}]")

    if dry_run:
        print("\n[dry-run] Would assign the above requirements to phases.")
        return True

    # Build phases content summary
    phases_content = ""
    for filename in sorted(os.listdir(phases_dir)):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(phases_dir, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            # Include just the first ~30 lines (objective + some reqs)
            lines = f.readlines()
            phases_content += f"### {filename}\n"
            phases_content += "".join(lines[:30]) + "\n...\n\n"

    # Build requirements context for the unmapped IDs
    requirements_context = ""
    if os.path.exists(req_file):
        with open(req_file, "r", encoding="utf-8") as f:
            req_content = f.read()
        for req_id in unmapped_reqs:
            # Find the line(s) for this requirement
            for line in req_content.splitlines():
                if f"[{req_id}]" in line:
                    requirements_context += f"- {line.strip()}\n"
                    break

    unmapped_reqs_list = "\n".join(f"- [{r}]" for r in sorted(unmapped_reqs))

    prompt_tmpl = ctx.load_prompt("fix_phase_mappings.md")
    prompt = ctx.format_prompt(prompt_tmpl,
        description_ctx=ctx.description_ctx,
        phases_content=phases_content,
        unmapped_reqs_list=unmapped_reqs_list,
        requirements_context=requirements_context or "(no context found)",
    )

    allowed_files = [phases_dir + os.sep]
    result = ctx.run_ai(prompt, allowed_files=allowed_files)

    if result.returncode != 0:
        print(f"\n[!] Error fixing phase mappings.")
        print(result.stdout)
        print(result.stderr)
        return False

    print("  Phase mappings updated.")
    return True


def _fix_task_mappings(unmapped_reqs: List[str], ctx: ProjectContext, dry_run: bool = False) -> bool:
    """Fix verify-tasks failures by generating tasks for unmapped requirements.

    This is the logic formerly in ``cmd_fix_requirements``.

    :param unmapped_reqs: List of requirement IDs in phases but not in tasks.
    :param ctx: Project context with AI runner.
    :param dry_run: Preview mode.
    :returns: ``True`` if fix was attempted, ``False`` if nothing to do.
    """
    if not unmapped_reqs:
        return False

    plan_dir = os.path.join(ROOT_DIR, "docs", "plan")
    phases_dir = os.path.join(plan_dir, "phases")
    tasks_dir = get_tasks_dir()

    if not os.path.isdir(phases_dir) or not os.path.isdir(tasks_dir):
        print("Error: phases or tasks directory not found.")
        return False

    # Build req_id -> phase_ids mapping
    phases_reqs: Dict[str, set] = {}
    for filename in os.listdir(phases_dir):
        if not filename.endswith(".md") or filename == "phase_removed.md":
            continue
        phase_id = filename.replace(".md", "")
        file_path = os.path.join(phases_dir, filename)
        reqs = parse_requirements(file_path)
        for r in reqs:
            phases_reqs.setdefault(r, set()).add(phase_id)

    # Filter to only the unmapped ones
    unmapped = set(unmapped_reqs) & set(phases_reqs.keys())
    if not unmapped:
        return False

    print(f"\n=> Fixing {len(unmapped)} requirement(s) not mapped to any task:")
    for r in sorted(unmapped):
        phase_list = ", ".join(sorted(phases_reqs.get(r, set())))
        print(f"  - [{r}] (in {phase_list})")

    if dry_run:
        print("\n[dry-run] Would generate tasks to cover the above requirements.")
        return True

    # Group by phase
    by_phase: Dict[str, List[str]] = {}
    for req_id in unmapped:
        for phase_id in phases_reqs[req_id]:
            by_phase.setdefault(phase_id, []).append(req_id)

    affected_phase_dirs = set()

    for phase_id, req_ids in sorted(by_phase.items()):
        phase_filename = f"{phase_id}.md"
        phase_task_dir = os.path.join(tasks_dir, phase_id)

        if not os.path.isdir(phase_task_dir):
            os.makedirs(phase_task_dir, exist_ok=True)

        grouping_file = os.path.join(tasks_dir, f"{phase_id}_grouping.json")

        target_sub_epic = None
        sub_epic_name = None
        if os.path.exists(grouping_file):
            with open(grouping_file, "r", encoding="utf-8") as f:
                sub_epics = json.load(f)
            for key, reqs in sub_epics.items():
                if isinstance(reqs, list):
                    overlap = set(req_ids) & set(reqs)
                    if overlap:
                        safe_name = re.sub(r'[^a-zA-Z0-9_\-]+', '_', key.lower())
                        target_sub_epic = safe_name
                        sub_epic_name = key
                        break

        if not target_sub_epic:
            existing_sub_epics = [
                d for d in os.listdir(phase_task_dir)
                if os.path.isdir(os.path.join(phase_task_dir, d))
            ] if os.path.isdir(phase_task_dir) else []

            if existing_sub_epics:
                target_sub_epic = sorted(existing_sub_epics)[0]
                sub_epic_name = target_sub_epic
            else:
                target_sub_epic = "unmapped_requirements"
                sub_epic_name = "Unmapped Requirements"

        target_dir = f"{phase_id}/{target_sub_epic}"
        se_dir = os.path.join(tasks_dir, target_dir)
        os.makedirs(se_dir, exist_ok=True)

        existing = sorted([f for f in os.listdir(se_dir) if f.endswith(".md")]) if os.path.isdir(se_dir) else []
        next_num = len(existing) + 1

        existing_content = ""
        for md_file in existing:
            with open(os.path.join(se_dir, md_file), "r", encoding="utf-8") as f:
                existing_content += f"### {md_file}\n{f.read()}\n\n"

        unmapped_reqs_list = "\n".join(f"- [{r}]" for r in sorted(req_ids))
        shared_components_ctx = ctx.load_shared_components()

        prompt_tmpl = ctx.load_prompt("fix_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            shared_components_ctx=shared_components_ctx,
            existing_tasks_content=existing_content or "(none)",
            phase_filename=phase_filename,
            target_dir=target_dir,
            sub_epic_name=sub_epic_name,
            unmapped_reqs_list=unmapped_reqs_list,
            next_task_num=f"{next_num:02d}",
        )

        print(f"\nGenerating tasks for {len(req_ids)} unmapped requirement(s) in {target_dir}...")
        allowed_files = [se_dir + os.sep]
        result = ctx.run_ai(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print(f"\n[!] Error generating fix tasks for {target_dir}.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)

        new_files = sorted(set(os.listdir(se_dir)) - set(existing))
        if new_files:
            for nf in new_files:
                print(f"  Created: docs/plan/tasks/{target_dir}/{nf}")
        else:
            print(f"  Warning: No new task files were created for {target_dir}.")

        affected_phase_dirs.add(os.path.join(tasks_dir, phase_id))

    # Rebuild DAGs for affected phases
    for phase_dir in sorted(affected_phase_dirs):
        phase_id = os.path.basename(phase_dir)
        print(f"\nRebuilding DAG for {phase_id}...")
        _rebuild_phase_dag(phase_dir, ctx)

    return True


def cmd_fixup(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Run validation and automatically fix any failures.

    Runs all verification checks, then for each failure category:

    - **verify-desc-length**: Expands short requirement descriptions.
    - **verify-master**: Appends missing requirement definitions from extracted
      docs to ``requirements.json``.
    - **verify-phases**: Assigns unmapped requirements to the best-fit phase
      using AI.
    - **verify-tasks**: Generates new task files to cover unmapped requirements
      (formerly ``fix-requirements``).
    - **verify-depends-on**: Fixes depends_on metadata formatting issues.
    - **verify-dags**: Fixes broken task references in DAG files.

    After fixes, re-runs validation to confirm resolution. Rebuilds DAGs for
    any phases whose tasks were modified.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``backend`` (str) — AI backend to use.
        - ``dry_run`` (bool) — preview mode.
    :type args: argparse.Namespace
    :raises SystemExit: On unrecoverable errors.
    """
    print("Running validation checks...")
    results = _run_all_checks()

    if results["all_pass"]:
        print("\nAll checks passed. Nothing to fix.")
        return

    dry_run = getattr(args, 'dry_run', False)
    runner = _make_runner(args.backend, model=getattr(args, 'model', None))
    ctx = ProjectContext(ROOT_DIR, runner=runner)

    fixed_anything = False

    # Fix short descriptions
    if _fix_description_length(ctx, dry_run=dry_run):
        fixed_anything = True

    # Fix DAG reference issues
    dag_fixes = _fix_dag_references(dry_run=dry_run, ctx=ctx)
    if dag_fixes > 0:
        fixed_anything = True

    if not fixed_anything:
        print("\nNo automatic fixes available for the remaining failures.")
        sys.exit(1)

    if dry_run:
        return

    # Re-verify
    print("\n=> Re-running validation...")
    final = _run_all_checks()

    rp_state = load_replan_state()
    log_action(rp_state, "fixup", "ran automatic fixups")
    save_replan_state(rp_state)

    if not final["all_pass"]:
        print("\nSome checks still failing after fixup.")
        sys.exit(1)
    else:
        print("\nAll checks passing.")


def _fix_master_list(missing_reqs: List[str], dry_run: bool = False) -> bool:
    """Append missing requirement definitions to ``docs/plan/requirements.json``.

    Scans ``docs/plan/requirements/*.md`` for heading-level definitions of
    the missing IDs and appends the full requirement blocks to the master
    file.

    :param missing_reqs: List of requirement IDs missing from docs/plan/requirements.json.
    :param dry_run: If ``True``, print what would be added without writing.
    :returns: ``True`` if any requirements were added.
    """
    req_dir = os.path.join(ROOT_DIR, "docs", "plan", "requirements")
    master_file = os.path.join(ROOT_DIR, "docs/plan/requirements.json")
    missing_set = set(missing_reqs)

    if not missing_set:
        return False

    # Build a regex that matches heading definitions for any of the missing IDs
    # Pattern: ### **[ID]** Title\n followed by metadata lines until the next
    # heading or --- separator or end of file.
    block_pattern = re.compile(
        r"(###\s+\*\*\[(" + "|".join(re.escape(r) for r in missing_set) +
        r")\]\*\*.*?)(?=\n###\s|\n---|\Z)",
        re.DOTALL,
    )

    found_blocks: Dict[str, str] = {}

    for filename in sorted(os.listdir(req_dir)):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(req_dir, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        for match in block_pattern.finditer(content):
            req_id = match.group(2)
            block = match.group(1).strip()
            if req_id in missing_set and req_id not in found_blocks:
                found_blocks[req_id] = block

    if not found_blocks:
        print("  Could not find definition blocks for missing requirements.")
        return False

    not_found = missing_set - set(found_blocks.keys())
    if not_found:
        print(f"  WARNING: Could not find definitions for: {sorted(not_found)}")

    print(f"  Adding {len(found_blocks)} requirement(s) to requirements.json:")
    for req_id in sorted(found_blocks):
        print(f"    + [{req_id}]")

    if dry_run:
        return True

    # Append to requirements.json
    with open(master_file, "a", encoding="utf-8") as f:
        f.write("\n")
        for req_id in sorted(found_blocks):
            f.write(f"\n{found_blocks[req_id]}\n")

    return True


def _fix_dag_references(dry_run: bool = False, ctx: Optional[ProjectContext] = None) -> int:
    """Fix broken task references in DAG files.

    Handles four categories of broken references, and also generates missing
    DAG files for phases that have tasks but no ``dag.json``:

    1. **Relative ``../`` prefixes** — e.g. ``../14_foo/bar.md`` when the task
       lives in the same phase.  Fixed by stripping the ``../`` prefix.
    2. **Redundant phase prefixes** — e.g. ``phase_2/02_css/foo.md`` appearing
       in ``phase_2/dag.json``.  Fixed by stripping the ``phase_N/`` prefix.
    3. **Full project-relative prefixes** — e.g.
       ``docs/plan/tasks/phase_3/foo.md`` in ``phase_3/dag.json``.  Fixed by
       stripping the ``docs/plan/tasks/phase_N/`` prefix.
    4. **Cross-phase references** — e.g. ``phase_1/foo/bar.md`` in
       ``phase_3/dag.json``.  Removed entirely since DAGs are per-phase.

    :param dry_run: If ``True``, print what would change without writing.
    :returns: Number of references fixed.
    :rtype: int
    """
    tasks_dir = get_tasks_dir()
    total_fixes = 0

    def _iter_phase_dags():
        """Yield (phase_dir_name, phase_path, dag_file) for each phase."""
        for name in sorted(os.listdir(tasks_dir)):
            path = os.path.join(tasks_dir, name)
            if not os.path.isdir(path) or not name.startswith("phase_"):
                continue
            df = os.path.join(path, "dag_reviewed.json")
            if not os.path.exists(df):
                df = os.path.join(path, "dag.json")
            if os.path.exists(df):
                yield name, path, df

    # Pass 0: generate missing DAGs for phases that have task sub-epics but
    # no dag.json / dag_reviewed.json at all.
    for name in sorted(os.listdir(tasks_dir)):
        path = os.path.join(tasks_dir, name)
        if not os.path.isdir(path) or not name.startswith("phase_"):
            continue
        if os.path.exists(os.path.join(path, "dag_reviewed.json")) or \
                os.path.exists(os.path.join(path, "dag.json")):
            continue
        has_tasks = any(
            os.path.isdir(os.path.join(path, d))
            for d in os.listdir(path)
        )
        if has_tasks:
            print(f"  Generating missing DAG for {name}...")
            if not dry_run:
                _rebuild_phase_dag(path, ctx=ctx)
            total_fixes += 1

    # Pass 1: rebuild DAGs for phases with orphan tasks (tasks on disk but
    # not in the DAG).  This must happen before reference fixing because
    # the programmatic DAG builder reads depends_on metadata from task
    # files and may re-introduce cross-phase refs that pass 2 will clean.
    for phase_dir_name, phase_path, dag_file in _iter_phase_dags():
        with open(dag_file, "r", encoding="utf-8") as f:
            dag = json.load(f)

        # Use the same 2-level discovery as _validate_dag: only sub_epic/*.md
        from .phases import _NON_TASK_FILES
        on_disk = set()
        for sub_epic in sorted(os.listdir(phase_path)):
            se_path = os.path.join(phase_path, sub_epic)
            if not os.path.isdir(se_path):
                continue
            for fname in sorted(os.listdir(se_path)):
                if fname.endswith(".md") and fname not in _NON_TASK_FILES:
                    on_disk.add(f"{sub_epic}/{fname}")

        orphans = on_disk - set(dag.keys())
        if orphans:
            print(f"  {len(orphans)} orphan task(s) in {phase_dir_name}, rebuilding DAG...")
            if not dry_run:
                _rebuild_phase_dag(phase_path, ctx=ctx)
            total_fixes += len(orphans)

    # Pass 2: fix broken references (../ prefixes, redundant phase prefixes,
    # cross-phase refs).
    for phase_dir_name, phase_path, dag_file in _iter_phase_dags():
        with open(dag_file, "r", encoding="utf-8") as f:
            dag = json.load(f)

        modified = False
        new_dag: Dict[str, List[str]] = {}

        for task_id, deps in dag.items():
            fixed_task_id = _fix_single_dag_ref(task_id, phase_path, phase_dir_name)
            if fixed_task_id is None:
                print(f"  Removing cross-phase DAG key: {task_id} in {phase_dir_name}")
                modified = True
                total_fixes += 1
                continue
            if fixed_task_id != task_id:
                modified = True
                total_fixes += 1

            fixed_deps = []
            for dep in deps:
                fixed_dep = _fix_single_dag_ref(dep, phase_path, phase_dir_name)
                if fixed_dep is None:
                    print(f"  Removing cross-phase dep: {dep} from {task_id} in {phase_dir_name}")
                    modified = True
                    total_fixes += 1
                    continue
                if fixed_dep != dep:
                    print(f"  Fixing ref: {dep} -> {fixed_dep} in {phase_dir_name}")
                    modified = True
                    total_fixes += 1
                fixed_deps.append(fixed_dep)

            new_dag[fixed_task_id] = fixed_deps

        if modified and not dry_run:
            with open(dag_file, "w", encoding="utf-8") as f:
                json.dump(new_dag, f, indent=2)
                f.write("\n")
            print(f"  Updated {dag_file}")

    # Pass 3: remove phantom references — DAG keys or dependencies that point
    # to task files which no longer exist on disk.
    from .phases import _NON_TASK_FILES as _NTF
    for phase_dir_name, phase_path, dag_file in _iter_phase_dags():
        with open(dag_file, "r", encoding="utf-8") as f:
            dag = json.load(f)

        on_disk = set()
        for sub_epic in sorted(os.listdir(phase_path)):
            se_path = os.path.join(phase_path, sub_epic)
            if not os.path.isdir(se_path):
                continue
            for fname in sorted(os.listdir(se_path)):
                if fname.endswith(".md") and fname not in _NTF:
                    on_disk.add(f"{sub_epic}/{fname}")

        dag_keys = set(dag.keys())
        phantom_keys = dag_keys - on_disk

        # Also find phantom deps: dependencies referencing non-existent files
        all_deps = set()
        for deps in dag.values():
            all_deps.update(deps)
        phantom_deps = all_deps - on_disk
        all_phantoms = phantom_keys | phantom_deps

        if all_phantoms:
            for p in sorted(phantom_keys):
                print(f"  Removing phantom DAG key: {p} in {phase_dir_name}")
            for p in sorted(phantom_deps - phantom_keys):
                print(f"  Removing phantom DAG dep: {p} in {phase_dir_name}")
            new_dag = {k: v for k, v in dag.items() if k not in phantom_keys}
            for task_id in new_dag:
                new_dag[task_id] = [d for d in new_dag[task_id] if d not in all_phantoms]
            if not dry_run:
                with open(dag_file, "w", encoding="utf-8") as f:
                    json.dump(new_dag, f, indent=2)
                    f.write("\n")
                print(f"  Updated {dag_file}")
            total_fixes += len(all_phantoms)

    if total_fixes > 0:
        print(f"\n=> Fixed {total_fixes} DAG reference(s)")
    return total_fixes


def _fix_single_dag_ref(
    ref: str, phase_path: str, phase_dir_name: str
) -> Optional[str]:
    """Attempt to fix a single DAG reference.

    :param ref: The task reference string from the DAG.
    :param phase_path: Absolute path to the phase directory.
    :param phase_dir_name: The phase directory name (e.g. ``phase_2``).
    :returns: The corrected reference, or ``None`` if the reference is a
        cross-phase dependency that should be removed.
    :rtype: Optional[str]
    """
    # Already valid?
    if os.path.exists(os.path.join(phase_path, ref)):
        return ref

    # Strip ../ prefix (same-phase ref with unnecessary relative path)
    if ref.startswith("../"):
        candidate = ref.lstrip("../")
        # Need to re-strip since lstrip removes chars not prefix
        candidate = ref[3:]  # strip exactly one ../
        while candidate.startswith("../"):
            candidate = candidate[3:]
        if os.path.exists(os.path.join(phase_path, candidate)):
            return candidate

    # Strip redundant same-phase prefix (e.g. phase_2/foo in phase_2's DAG)
    if ref.startswith(phase_dir_name + "/"):
        candidate = ref[len(phase_dir_name) + 1:]
        if os.path.exists(os.path.join(phase_path, candidate)):
            return candidate

    # Strip full project-relative path prefix
    # (e.g. docs/plan/tasks/phase_3/foo in phase_3's DAG)
    tasks_prefix = f"docs/plan/tasks/{phase_dir_name}/"
    if ref.startswith(tasks_prefix):
        candidate = ref[len(tasks_prefix):]
        if os.path.exists(os.path.join(phase_path, candidate)):
            return candidate
        # Cross-phase full path — remove it
        return None

    # Cross-phase reference (starts with phase_N/ but different phase)
    if re.match(r"^phase_\d+/", ref):
        return None

    # Full project-relative path pointing to a different phase
    # (e.g. docs/plan/tasks/phase_1/foo in phase_5's DAG)
    if re.match(r"^docs/plan/tasks/phase_\d+/", ref):
        return None

    # Unknown broken ref — return as-is, validation will still catch it
    return ref


def _fix_description_length(ctx: ProjectContext, dry_run: bool = False) -> bool:
    """Fix short description failures by expanding descriptions under 10 words.

    Spawns an AI agent to review docs/plan/requirements.json and expand all descriptions
    that are shorter than 10 words to meet the minimum length requirement.

    :param ctx: Project context with AI runner.
    :param dry_run: Preview mode — don't actually run AI.
    :returns: ``True`` if fix was attempted (or dry-run shown), ``False`` if nothing to do.
    """
    req_file = os.path.join(ROOT_DIR, "docs/plan/requirements.json")

    if not os.path.exists(req_file):
        print("Error: docs/plan/requirements.json not found.")
        return False

    # Detect short descriptions directly from the JSON
    import json as _json
    try:
        with open(req_file, "r", encoding="utf-8") as f:
            req_data = _json.load(f)
    except (_json.JSONDecodeError, OSError):
        print("Error: could not parse docs/plan/requirements.json")
        return False

    short_reqs = []
    for req in req_data.get("requirements", []):
        desc = req.get("description", "")
        word_count = len(desc.split())
        if word_count < 10:
            short_reqs.append((req.get("id", "?"), word_count))

    if not short_reqs:
        print("No description length issues found in output.")
        return False

    print(f"\n=> Fixing {len(short_reqs)} requirement(s) with descriptions shorter than 10 words:")
    for req_id, word_count in sorted(short_reqs)[:20]:  # Show first 20
        print(f"  - [{req_id}] ({word_count} words)")
    if len(short_reqs) > 20:
        print(f"  ... and {len(short_reqs) - 20} more")

    if dry_run:
        print("\n[dry-run] Would expand short descriptions to meet 10-word minimum.")
        return True

    # Load requirements context
    with open(req_file, "r", encoding="utf-8") as f:
        req_content = f.read()

    # Build context for the short requirements
    requirements_context = ""
    for req_id, _ in short_reqs:
        # Find the requirement block in the file
        pattern = re.compile(
            rf'###\s*\*\*\[{re.escape(req_id)}\]\*\*\s*.*?(?=\n###\s*\*\*\[|\Z)',
            re.DOTALL
        )
        match = pattern.search(req_content)
        if match:
            requirements_context += f"### **[{req_id}]**\n{match.group(0)}\n\n"

    prompt_tmpl = ctx.load_prompt("fix_description_length.md")
    prompt = ctx.format_prompt(
        prompt_tmpl,
        description_ctx=ctx.description_ctx,
        requirements_context=requirements_context,
        short_reqs_list="\n".join(f"- [{r[0]}] ({r[1]} words)" for r in sorted(short_reqs)),
    )

    allowed_files = [req_file]
    result = ctx.run_ai(prompt, allowed_files=allowed_files)

    if result.returncode != 0:
        print(f"\n[!] Error fixing description length.")
        print(result.stdout)
        print(result.stderr)
        return False

    print("  Description length issues addressed.")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(backend: str, model: Optional[str] = None) -> "AIRunner":  # type: ignore[name-defined]
    """Instantiate the correct AI runner for the given backend name.

    Delegates to :func:`~workflow_lib.runners.make_runner`, reading
    ``soft_timeout`` from the project config for backends that support it.
    """
    from .config import get_config_defaults as _get_cfg
    soft_timeout = _get_cfg().get("soft_timeout")
    return _make_runner_from_runners(backend, model=model, soft_timeout=soft_timeout)


def _rebuild_phase_dag(phase_dir: str, ctx: Optional[ProjectContext]) -> None:
    """Rebuild the DAG for a phase: programmatic first, AI fallback.

    Removes any existing ``dag_reviewed.json`` (the new DAG is authoritative
    after a replan), then calls
    :meth:`~workflow_lib.phases.Phase7ADAGGeneration._build_programmatic_dag`.
    If all tasks have ``depends_on`` metadata, writes the result to
    ``dag.json``.  Otherwise falls back to an AI-generated DAG using the
    ``dag_tasks.md`` prompt.  When *ctx* is ``None``, the AI fallback is
    skipped.

    :param phase_dir: Absolute path to the phase task directory, e.g.
        ``docs/plan/tasks/phase_1/``.
    :type phase_dir: str
    :param ctx: Shared project context providing AI runner access and the
        project description.  May be ``None`` when called without a context,
        in which case the AI fallback is skipped.
    :type ctx: Optional[ProjectContext]
    """
    dag_file = os.path.join(phase_dir, "dag.json")
    dag_reviewed = os.path.join(phase_dir, "dag_reviewed.json")

    # Remove reviewed DAG — after replanning, the new DAG is authoritative
    if os.path.exists(dag_reviewed):
        os.remove(dag_reviewed)

    phase_id = os.path.basename(phase_dir)

    # Try programmatic build
    programmatic_dag = Phase7ADAGGeneration._build_programmatic_dag(phase_dir)
    if programmatic_dag is not None:
        errors = Phase7ADAGGeneration._validate_dag(phase_dir, programmatic_dag)
        if not errors:
            with open(dag_file, "w", encoding="utf-8") as f:
                json.dump(programmatic_dag, f, indent=2)
            print(f"Built DAG programmatically ({len(programmatic_dag)} tasks): {dag_file}")
            return
        print(f"\n[!] WARNING: Programmatic DAG for {phase_id} has {len(errors)} consistency issues:")
        for e in errors:
            print(f"      - {e}")
        print(f"   -> Falling back to AI DAG inference for {phase_id}...")

    # Fallback to AI
    if ctx is None:
        print(f"Some tasks lack depends_on metadata but no ctx provided; skipping AI DAG generation.")
        return
    print(f"Some tasks lack depends_on metadata. Using AI to generate DAG for {phase_id}...")

    for attempt in range(1, 4):
        result = ctx.run_ai(
            "dag_tasks.md",
            allowed_files=[dag_file],
            context_files={
                "description_ctx": ctx.input_dir,
                "tasks_content": phase_dir,
            },
            params={
                "phase_filename": phase_id,
                "target_path": f"docs/plan/tasks/{phase_id}/dag.json",
            },
        )

        if result.returncode == 0 and os.path.exists(dag_file):
            try:
                with open(dag_file, "r", encoding="utf-8") as f:
                    ai_dag = json.load(f)
                errors = Phase7ADAGGeneration._validate_dag(phase_dir, ai_dag)
                if errors:
                    print(f"\n[!] WARNING: AI-generated DAG for {phase_id} has {len(errors)} consistency issues (attempt {attempt}/3):")
                    for e in errors[:10]:
                        print(f"      - {e}")
                    if len(errors) > 10:
                        print(f"      ... and {len(errors) - 10} more")
                    os.remove(dag_file)
                    continue
            except (json.JSONDecodeError, OSError) as exc:
                print(f"\n[!] Invalid DAG JSON for {phase_id} (attempt {attempt}/3): {exc}")
                os.remove(dag_file)
                continue
            print(f"AI-generated DAG: {dag_file}")
            return

        print(f"\n[!] Error generating DAG for {phase_id} (attempt {attempt}/3).")
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
        elif not os.path.exists(dag_file):
            print(f"\n[!] Error: Agent failed to generate DAG JSON file {dag_file}.")

    print(f"[!] Failed to generate DAG for {phase_id} after 3 attempts.")


def _show_affected_tasks(req_id: str) -> None:
    """Print the list of tasks that reference a given requirement ID.

    Walks all ``.md`` files under the tasks directory and parses requirement
    IDs using :func:`~workflow_lib.constants.parse_requirements`.  Tasks where
    *req_id* is the only requirement are flagged as candidates for removal.

    :param req_id: Requirement identifier to search for (without brackets),
        e.g. ``"AUTH-001"``.
    :type req_id: str
    """
    tasks_dir = get_tasks_dir()
    if not os.path.isdir(tasks_dir):
        return

    affected = []
    for root, dirs, files in os.walk(tasks_dir):
        for f in files:
            if f.endswith(".md"):
                filepath = os.path.join(root, f)
                reqs = parse_requirements(filepath)
                if req_id in reqs:
                    rel = os.path.relpath(filepath, tasks_dir)
                    # Check if this is the ONLY requirement
                    only_req = len(reqs) == 1
                    affected.append((rel, only_req))

    if affected:
        print(f"\nTasks referencing [{req_id}]:")
        for rel, only in affected:
            suffix = " (ONLY req — consider removing task)" if only else ""
            print(f"  - {rel}{suffix}")
    else:
        print(f"\nNo tasks reference [{req_id}].")


def _run_validate(phase: Optional[int] = None) -> None:
    """Run ``validate.py`` and print the result.

    :param phase: When provided, validate only this phase number.
        When ``None``, runs ``--all``.
    :type phase: Optional[int]
    """
    validate_script = os.path.join(TOOLS_DIR, "validate.py")
    cmd = [sys.executable, validate_script]
    if phase is not None:
        cmd.extend(["--phase", str(phase)])
    else:
        cmd.append("--all")
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT_DIR)
    if res.returncode == 0:
        label = f"phase {phase}" if phase else "all"
        print(f"  PASS  {label}")
    else:
        label = f"phase {phase}" if phase else "all"
        print(f"  FAIL  {label}")
        if res.stdout.strip():
            print(res.stdout.strip())


# ---------------------------------------------------------------------------
# Add Feature
# ---------------------------------------------------------------------------


def _load_requirements_ctx() -> str:
    """Load docs/plan/requirements.json content, or empty string if missing."""
    req_file = os.path.join(ROOT_DIR, "docs/plan/requirements.json")
    if os.path.exists(req_file):
        with open(req_file, "r", encoding="utf-8") as f:
            return f.read()
    return "(no docs/plan/requirements.json found)"


def _load_phases_ctx() -> str:
    """Load all phase documents as context."""
    phases_dir = os.path.join(ROOT_DIR, "docs", "plan", "phases")
    if not os.path.isdir(phases_dir):
        return "(no phases found)"
    content = ""
    for md in sorted(os.listdir(phases_dir)):
        if md.endswith(".md"):
            with open(os.path.join(phases_dir, md), "r", encoding="utf-8") as f:
                content += f"### {md}\n{f.read()}\n\n"
    return content or "(no phases found)"


def cmd_add_feature(args: "argparse.Namespace") -> None:  # type: ignore[name-defined]
    """Interactive feature addition: discuss a feature brief, produce a spec,
    then integrate it into the project plan.

    **Stage 1 — Template**: When called without ``--brief``, prints the feature
    brief template to stdout so the user can fill it in and save to a file.

    **Stage 2 — Discuss**: When called with ``--brief <file>``, runs an
    interactive discussion loop where an AI agent reviews the brief, asks
    questions, and raises concerns.  The user responds until satisfied, then
    the agent produces a formal feature spec.

    **Stage 3 — Execute**: When the user confirms (or calls with ``--spec``
    directly), runs an automatic workflow that consumes the spec to update
    requirements, create tasks, build DAGs, and update documentation.

    :param args: Parsed :mod:`argparse` namespace with attributes:

        - ``brief`` (Optional[str]) — path to the filled-in feature brief.
        - ``spec`` (Optional[str]) — path to a pre-existing spec (skip discuss).
        - ``phase_id`` (Optional[str]) — target phase for task generation.
        - ``sub_epic`` (Optional[str]) — target sub-epic name.
        - ``dry_run`` (bool) — preview mode.
        - ``backend`` (str) — AI backend to use.
    :type args: argparse.Namespace
    """
    brief_path = getattr(args, "brief", None)
    spec_path = getattr(args, "spec", None)

    # Stage 1: No brief provided — print template
    if not brief_path and not spec_path:
        template_file = os.path.join(TOOLS_DIR, "prompts", "feature_brief_template.md")
        with open(template_file, "r", encoding="utf-8") as f:
            print(f.read())
        print("\n---")
        print("Save the above template to a file, fill it in, then re-run:")
        print("  workflow.py add-feature --brief <your_brief.md>")
        return

    backend = args.backend
    runner = _make_runner(backend, model=getattr(args, "model", None))
    ctx = ProjectContext(ROOT_DIR, runner=runner)

    requirements_ctx = _load_requirements_ctx()
    phases_ctx = _load_phases_ctx()
    shared_components_ctx = ctx.load_shared_components()

    # Stage 2: Discuss the brief and produce a spec
    if brief_path and not spec_path:
        if not os.path.exists(brief_path):
            print(f"Error: Brief file not found: {brief_path}")
            sys.exit(1)

        with open(brief_path, "r", encoding="utf-8") as f:
            feature_brief = f.read()

        discussion_history = "(none — this is the first round)"
        discussion_log = []
        round_num = 0

        print("=" * 60)
        print("FEATURE DISCUSSION")
        print("=" * 60)
        print("The AI agent will review your feature brief and ask questions.")
        print("Respond to refine the feature. When satisfied, type 'done' to")
        print("generate the formal spec, or 'quit' to abort.\n")

        while True:
            round_num += 1
            print(f"\n--- Round {round_num} ---\n")

            discuss_tmpl = ctx.load_prompt("feature_discuss.md")
            prompt = ctx.format_prompt(discuss_tmpl,
                description_ctx=ctx.description_ctx,
                shared_components_ctx=shared_components_ctx,
                requirements_ctx=requirements_ctx,
                phases_ctx=phases_ctx,
                feature_brief=feature_brief,
                discussion_history=discussion_history,
            )

            # Stream agent output live so the user sees it in real-time.
            streamed_lines: list = []
            def _on_line(line: str) -> None:
                print(line)
                streamed_lines.append(line)

            effective_timeout = ctx.agent_timeout
            result = ctx.runner.run(ctx.root_dir, prompt, ctx.image_paths,
                                    on_line=_on_line, timeout=effective_timeout)

            if result.returncode != 0:
                print(f"\n[!] Error during discussion round {round_num}.")
                print(result.stderr)
                sys.exit(1)

            # Use streamed output if available, otherwise fall back to stdout
            agent_response = "\n".join(streamed_lines) if streamed_lines else (result.stdout or "").strip()
            if not streamed_lines and agent_response:
                print(agent_response)
            discussion_log.append(f"## Agent (Round {round_num})\n{agent_response}")

            print(f"\n{'=' * 60}")
            user_input = input("\nYour response (or 'done'/'quit'): ").strip()

            if user_input.lower() == "quit":
                print("Aborted.")
                return
            if user_input.lower() == "done":
                break

            discussion_log.append(f"## User (Round {round_num})\n{user_input}")
            discussion_history = "\n\n".join(discussion_log)

        # Generate the spec
        print("\n" + "=" * 60)
        print("GENERATING FEATURE SPEC")
        print("=" * 60 + "\n")

        # Determine spec output path
        features_dir = os.path.join(ROOT_DIR, "docs", "plan", "features")
        os.makedirs(features_dir, exist_ok=True)

        brief_basename = os.path.splitext(os.path.basename(brief_path))[0]
        spec_filename = f"spec_{brief_basename}.md"
        spec_output = os.path.join(features_dir, spec_filename)

        spec_tmpl = ctx.load_prompt("feature_spec.md")
        prompt = ctx.format_prompt(spec_tmpl,
            description_ctx=ctx.description_ctx,
            shared_components_ctx=shared_components_ctx,
            requirements_ctx=requirements_ctx,
            feature_brief=feature_brief,
            discussion_history="\n\n".join(discussion_log),
            spec_output_path=spec_output,
        )

        if args.dry_run:
            print(f"[dry-run] Would generate spec at: {spec_output}")
            return

        result = ctx.run_ai(prompt, allowed_files=[spec_output])

        if result.returncode != 0 or not os.path.exists(spec_output):
            print(f"\n[!] Error generating spec.")
            if result.returncode != 0:
                print(result.stderr)
            else:
                print(f"Expected file not created: {spec_output}")
            sys.exit(1)

        print(f"\nSpec created: {spec_output}")
        spec_path = spec_output

        # Prompt user to continue to execution
        print(f"\n{'=' * 60}")
        print("SPEC READY — Review it, then press Enter to integrate into")
        print("the project plan, or Ctrl-C to stop here.")
        print(f"{'=' * 60}")
        try:
            input("\nPress Enter to continue...")
        except (KeyboardInterrupt, EOFError):
            print("\nStopped. You can re-run with:")
            print(f"  workflow.py add-feature --spec {spec_path} --phase <phase_id> --sub-epic <name>")
            return

    # Stage 3: Execute — integrate spec into plan
    if not spec_path:
        print("Error: No spec path available.")
        sys.exit(1)

    if not os.path.exists(spec_path):
        print(f"Error: Spec file not found: {spec_path}")
        sys.exit(1)

    phase_id = getattr(args, "phase_id", None)
    sub_epic = getattr(args, "sub_epic", None)

    if not phase_id or not sub_epic:
        print("\nTo integrate the spec, provide --phase and --sub-epic:")
        print(f"  workflow.py add-feature --spec {spec_path} --phase <phase_id> --sub-epic <name>")
        # Try to interactively ask
        if not phase_id:
            phases_dir = os.path.join(ROOT_DIR, "docs", "plan", "phases")
            if os.path.isdir(phases_dir):
                available = sorted(f.replace(".md", "") for f in os.listdir(phases_dir) if f.endswith(".md"))
                if available:
                    print(f"\nAvailable phases: {', '.join(available)}")
            phase_id = input("Target phase (e.g., phase_1): ").strip()
            if not phase_id:
                print("Aborted — no phase specified.")
                return
        if not sub_epic:
            sub_epic = input("Sub-epic name (e.g., my_feature): ").strip()
            if not sub_epic:
                print("Aborted — no sub-epic specified.")
                return

    if args.dry_run:
        print(f"[dry-run] Would integrate spec into {phase_id}/{sub_epic}")
        return

    with open(spec_path, "r", encoding="utf-8") as f:
        feature_spec = f.read()

    phase_dir = os.path.join(get_tasks_dir(), phase_id)
    se_dir = os.path.join(phase_dir, sub_epic)
    os.makedirs(se_dir, exist_ok=True)

    # Determine next task number
    existing = sorted([f for f in os.listdir(se_dir) if f.endswith(".md")]) if os.path.isdir(se_dir) else []
    next_task_num = len(existing) + 1

    print("\n" + "=" * 60)
    print("INTEGRATING FEATURE INTO PLAN")
    print("=" * 60 + "\n")

    exec_tmpl = ctx.load_prompt("feature_execute.md")
    prompt = ctx.format_prompt(exec_tmpl,
        description_ctx=ctx.description_ctx,
        shared_components_ctx=shared_components_ctx,
        requirements_ctx=requirements_ctx,
        phases_ctx=phases_ctx,
        feature_spec=feature_spec,
        phase_id=phase_id,
        sub_epic=sub_epic,
        next_task_num=str(next_task_num),
    )

    # Allow writes to: sub-epic dir, docs/plan/requirements.json, phase doc, shared_components
    req_file = os.path.join(ROOT_DIR, "docs/plan/requirements.json")
    phase_doc = os.path.join(ROOT_DIR, "docs", "plan", "phases", f"{phase_id}.md")
    shared_comp = os.path.join(ROOT_DIR, "docs", "plan", "shared_components.md")
    allowed_files = [
        se_dir + os.sep,
        req_file,
        phase_doc,
        shared_comp,
    ]

    result = ctx.run_ai(prompt, allowed_files=allowed_files)

    if result.returncode != 0:
        print(f"\n[!] Error integrating feature.")
        print(result.stdout)
        print(result.stderr)
        sys.exit(1)

    # Count created tasks
    new_files = sorted(set(
        f for f in os.listdir(se_dir) if f.endswith(".md")
    ) - set(existing)) if os.path.isdir(se_dir) else []

    if new_files:
        print(f"\nCreated {len(new_files)} task(s):")
        for nf in new_files:
            print(f"  docs/plan/tasks/{phase_id}/{sub_epic}/{nf}")
    else:
        print("Warning: No new task files were created by the agent.")

    # Rebuild DAG
    print("\nRebuilding DAG...")
    _rebuild_phase_dag(phase_dir, ctx)

    # Log action
    rp_state = load_replan_state()
    log_action(rp_state, "add-feature", f"{phase_id}/{sub_epic}",
               f"Integrated feature from spec: {spec_path}")
    save_replan_state(rp_state)

    print(f"\nFeature integrated into {phase_id}/{sub_epic}.")
    print("Run 'workflow.py validate' to verify plan consistency.")




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


