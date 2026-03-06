import os
import subprocess
import sys
import json
import re
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from .constants import TOOLS_DIR, ROOT_DIR, parse_requirements
from .state import *
from .executor import phase_sort_key
from .context import ProjectContext
from .runners import GeminiRunner, ClaudeRunner, CopilotRunner
from .phases import Phase5BSharedComponents, Phase7ADAGGeneration
def cmd_status(args):
    """Show current plan and execution status."""
    tasks_dir = get_tasks_dir()
    master_dag = load_dags(tasks_dir)
    wf_state = load_workflow_state()
    rp_state = load_replan_state()

    completed = set(wf_state.get("completed_tasks", []))
    merged = set(wf_state.get("merged_tasks", []))
    blocked = set(rp_state.get("blocked_tasks", {}).keys())

    # Group by phase
    phases = {}
    for task_id in sorted(master_dag.keys()):
        phase = task_id.split("/")[0]
        phases.setdefault(phase, []).append(task_id)

    # Also find tasks on disk not in DAG
    on_disk = set()
    if os.path.exists(tasks_dir):
        for phase_dir in sorted(os.listdir(tasks_dir)):
            phase_path = os.path.join(tasks_dir, phase_dir)
            if not os.path.isdir(phase_path) or not phase_dir.startswith("phase_"):
                continue
            for sub_epic in sorted(os.listdir(phase_path)):
                se_path = os.path.join(phase_path, sub_epic)
                if not os.path.isdir(se_path):
                    continue
                for md in sorted(os.listdir(se_path)):
                    if md.endswith(".md"):
                        on_disk.add(f"{phase_dir}/{sub_epic}/{md}")

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


def cmd_validate(args):
    """Run all verification checks."""
    verify_script = os.path.join(TOOLS_DIR, "verify_requirements.py")
    plan_dir = os.path.join(ROOT_DIR, "docs", "plan")
    req_file = os.path.join(ROOT_DIR, "requirements.md")
    phases_dir = os.path.join(plan_dir, "phases")
    tasks_dir = get_tasks_dir()

    checks = []

    if os.path.exists(req_file) and os.path.isdir(os.path.join(plan_dir, "requirements")):
        checks.append(("verify-master", [sys.executable, verify_script, "--verify-master"]))

    if os.path.exists(req_file) and os.path.isdir(phases_dir):
        checks.append(("verify-phases", [sys.executable, verify_script, "--verify-phases", "requirements.md", "docs/plan/phases/"]))

    if os.path.isdir(phases_dir) and os.path.isdir(tasks_dir):
        checks.append(("verify-tasks", [sys.executable, verify_script, "--verify-tasks", "docs/plan/phases/", "docs/plan/tasks/"]))
        checks.append(("verify-dags", [sys.executable, verify_script, "--verify-dags", "docs/plan/tasks/"]))

    if not checks:
        print("No plan artifacts found to validate.")
        return

    all_pass = True
    for name, cmd in checks:
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT_DIR)
        if res.returncode == 0:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}")
            if res.stdout.strip():
                for line in res.stdout.strip().splitlines():
                    print(f"        {line}")
            all_pass = False

    sys.exit(0 if all_pass else 1)


def cmd_block(args):
    """Mark a task as blocked."""
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


def cmd_unblock(args):
    """Remove a block from a task."""
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


def cmd_remove(args):
    """Remove a task and update its phase DAG."""
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


def cmd_add(args):
    """AI-generate a new task in a sub-epic."""
    phase_id = args.phase_id
    sub_epic = args.sub_epic
    description = args.desc
    backend = args.backend

    runner = _make_runner(backend)
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
    ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/tasks/{target_dir}/\n!/docs/plan/phases/\n!/requirements.md\n"
    allowed_files = [se_dir + os.sep]
    result = ctx.run_ai(prompt, ignore_content, allowed_files=allowed_files, sandbox=False)

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


def cmd_modify_req(args):
    """Modify requirements.md."""
    req_file = os.path.join(ROOT_DIR, "requirements.md")
    if not os.path.exists(req_file):
        print(f"Error: {req_file} not found.")
        sys.exit(1)

    if args.edit_req:
        # Open editor
        editor = os.environ.get("EDITOR", "vim")
        subprocess.run([editor, req_file])
        # Verify
        _run_verify("verify-master")
        return

    if args.remove_req:
        req_id = args.remove_req
        with open(req_file, "r", encoding="utf-8") as f:
            content = f.read()

        if f"[{req_id}]" not in content:
            print(f"Requirement [{req_id}] not found in requirements.md")
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
        _run_verify("verify-master")

        rp_state = load_replan_state()
        log_action(rp_state, "add-req", args.add_req)
        save_replan_state(rp_state)


def cmd_regen_dag(args):
    """Rebuild DAG for a specific phase."""
    phase_id = args.phase_id
    phase_dir = os.path.join(get_tasks_dir(), phase_id)

    if not os.path.isdir(phase_dir):
        print(f"Error: Phase directory not found: {phase_dir}")
        sys.exit(1)

    if args.dry_run:
        print(f"[dry-run] Would rebuild DAG for {phase_id}")
        return

    runner = _make_runner(args.backend)
    ctx = ProjectContext(ROOT_DIR, runner=runner)
    _rebuild_phase_dag(phase_dir, ctx)

    rp_state = load_replan_state()
    log_action(rp_state, "regen-dag", phase_id)
    save_replan_state(rp_state)


def cmd_regen_tasks(args):
    """Re-run task breakdown for a phase or sub-epic."""
    phase_id = args.phase_id
    sub_epic = args.sub_epic
    backend = args.backend

    runner = _make_runner(backend)
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

        ignore_content = f"/*\n!/.sandbox/\n!/requirements.md\n!/docs/plan/phases/\n!/docs/plan/tasks/\n!/scripts/verify_requirements.py\n"
        allowed_files = [se_dir + os.sep]
        result = ctx.run_ai(prompt, ignore_content, allowed_files=allowed_files, sandbox=False)

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


def cmd_regen_components(args):
    """Regenerate shared_components.md."""
    runner = _make_runner(args.backend)
    ctx = ProjectContext(ROOT_DIR, runner=runner)

    if args.dry_run:
        print("[dry-run] Would regenerate shared_components.md")
        return

    phase = Phase5BSharedComponents()
    ctx.state["shared_components_completed"] = False
    ctx.save_state()
    phase.execute(ctx)

    rp_state = load_replan_state()
    log_action(rp_state, "regen-components", "shared_components.md")
    save_replan_state(rp_state)


def cmd_cascade(args):
    """After manual task edits, rescan and rebuild DAG + validate."""
    phase_id = args.phase_id
    phase_dir = os.path.join(get_tasks_dir(), phase_id)

    if not os.path.isdir(phase_dir):
        print(f"Error: Phase directory not found: {phase_dir}")
        sys.exit(1)

    if args.dry_run:
        print(f"[dry-run] Would cascade changes for {phase_id}")
        return

    runner = _make_runner(args.backend)
    ctx = ProjectContext(ROOT_DIR, runner=runner)

    # Scan tasks and collect requirement coverage
    print(f"Scanning tasks in {phase_id}...")
    task_reqs = set()
    task_count = 0
    for sub_epic in sorted(os.listdir(phase_dir)):
        se_path = os.path.join(phase_dir, sub_epic)
        if not os.path.isdir(se_path):
            continue
        for md in sorted(os.listdir(se_path)):
            if md.endswith(".md"):
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
    _run_verify("verify-dags")

    rp_state = load_replan_state()
    log_action(rp_state, "cascade", phase_id)
    save_replan_state(rp_state)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(backend: str):
    if backend == "claude":
        return ClaudeRunner()
    elif backend == "copilot":
        return CopilotRunner()
    return GeminiRunner()


def _rebuild_phase_dag(phase_dir: str, ctx: ProjectContext):
    """Rebuild DAG for a phase: programmatic first, AI fallback."""
    dag_file = os.path.join(phase_dir, "dag.json")
    dag_reviewed = os.path.join(phase_dir, "dag_reviewed.json")

    # Remove reviewed DAG — after replanning, the new DAG is authoritative
    if os.path.exists(dag_reviewed):
        os.remove(dag_reviewed)

    # Try programmatic build
    programmatic_dag = Phase7ADAGGeneration._build_programmatic_dag(phase_dir)
    if programmatic_dag is not None:
        with open(dag_file, "w", encoding="utf-8") as f:
            json.dump(programmatic_dag, f, indent=2)
        print(f"Built DAG programmatically ({len(programmatic_dag)} tasks): {dag_file}")
        return

    # Fallback to AI
    phase_id = os.path.basename(phase_dir)
    print(f"Some tasks lack depends_on metadata. Using AI to generate DAG for {phase_id}...")

    dag_prompt_tmpl = ctx.load_prompt("dag_tasks.md")

    tasks_content = ""
    sub_epics = [d for d in os.listdir(phase_dir) if os.path.isdir(os.path.join(phase_dir, d))]
    for sub_epic in sorted(sub_epics):
        se_dir = os.path.join(phase_dir, sub_epic)
        for md in sorted(os.listdir(se_dir)):
            if md.endswith(".md"):
                task_id = f"{sub_epic}/{md}"
                tasks_content += f"### Task ID: {task_id}\n"
                with open(os.path.join(se_dir, md), "r", encoding="utf-8") as f:
                    content = f.read()
                    tasks_content += "\n".join(f"    {line}" for line in content.split("\n")) + "\n\n"

    prompt = ctx.format_prompt(dag_prompt_tmpl,
        phase_filename=phase_id,
        target_path=f"docs/plan/tasks/{phase_id}/dag.json",
        description_ctx=ctx.description_ctx,
        tasks_content=tasks_content,
    )

    ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/tasks/{phase_id}/dag.json\n"
    result = ctx.run_ai(prompt, ignore_content, allowed_files=[dag_file], sandbox=False)

    if result.returncode == 0 and os.path.exists(dag_file):
        print(f"AI-generated DAG: {dag_file}")
    else:
        print(f"[!] Failed to generate DAG for {phase_id}")
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)


def _show_affected_tasks(req_id: str):
    """Show tasks that reference a requirement ID."""
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


def _run_verify(mode: str):
    """Run a specific verification mode."""
    verify_script = os.path.join(TOOLS_DIR, "verify_requirements.py")
    cmd_map = {
        "verify-master": [sys.executable, verify_script, "--verify-master"],
        "verify-dags": [sys.executable, verify_script, "--verify-dags", "docs/plan/tasks/"],
    }
    cmd = cmd_map.get(mode)
    if cmd:
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT_DIR)
        if res.returncode == 0:
            print(f"  PASS  {mode}")
        else:
            print(f"  FAIL  {mode}")
            if res.stdout.strip():
                print(res.stdout.strip())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


