#!/usr/bin/env python3
import os
import sys
import json
import argparse
import subprocess
import tempfile
import threading
import traceback
import concurrent.futures
import re
from typing import Dict, List, Any

class Logger(object):
    def __init__(self, terminal, log_stream, lock):
        self.terminal = terminal
        self.log_stream = log_stream
        self.lock = lock

    def write(self, message):
        with self.lock:
            self.terminal.write(message)
            self.log_stream.write(message)
            self.log_stream.flush()

    def flush(self):
        with self.lock:
            self.terminal.flush()
            self.log_stream.flush()


# Default CLI backends config for gemini/claude. Assumes same runner logic as gen_all.py
def run_ai_command(prompt: str, cwd: str, prefix: str = "", backend: str = "gemini") -> int:
    cmd = ["gemini", "-y"]
    tmp_file_name = None

    if backend == "claude":
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
    elif backend == "copilot":
        fd, tmp_file_name = tempfile.mkstemp(text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(prompt)
        cmd = ["copilot", "--model", "gpt-5-mini", "-p", f"Follow the instructions in @{tmp_file_name}", "--yolo"]

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        text=True
    )

    def write_input():
        try:
            if process.stdin:
                process.stdin.write(prompt)
        except Exception:
            pass
        finally:
            if process.stdin:
                process.stdin.close()
            
    writer = threading.Thread(target=write_input)
    writer.start()

    if process.stdout:
        for line in iter(process.stdout.readline, ""):
            if line:
                print(f"{prefix}{line}", end="")
                sys.stdout.flush()

    process.wait()
    writer.join()

    if tmp_file_name:
        try:
            os.remove(tmp_file_name)
        except OSError:
            pass

    return process.returncode


def load_dags(tasks_dir: str) -> Dict[str, List[str]]:
    """Loads all dag_reviewed.json or dag.json files from the tasks directories."""
    master_dag = {}
    if not os.path.exists(tasks_dir):
        return master_dag

    for phase_dir in sorted(os.listdir(tasks_dir)):
        phase_path = os.path.join(tasks_dir, phase_dir)
        if not os.path.isdir(phase_path) or not phase_dir.startswith("phase_"):
            continue

        # Try reviewed DAG first, then fallback to unreviewed
        dag_file = os.path.join(phase_path, "dag_reviewed.json")
        if not os.path.exists(dag_file):
            dag_file = os.path.join(phase_path, "dag.json")

        if os.path.exists(dag_file):
            with open(dag_file, "r", encoding="utf-8") as f:
                try:
                    phase_dag = json.load(f)
                    for task_id, prerequisites in phase_dag.items():
                        # DAG task_ids are usually relative like "01_project_infrastructure_monorepo_setup"
                        # We key them by their fully qualified path: phase_X/task_id
                        full_task_id = f"{phase_dir}/{task_id}"
                        master_dag[full_task_id] = [f"{phase_dir}/{p}" for p in prerequisites]
                except json.JSONDecodeError as e:
                    print(f"Error parsing {dag_file}: {e}")
    return master_dag


def load_workflow_state(state_file: str) -> Dict[str, Any]:
    state = {"completed_tasks": [], "merged_tasks": []}
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            try:
                state.update(json.load(f))
            except json.JSONDecodeError:
                pass
    return state


def save_workflow_state(state_file: str, state: Dict[str, Any]):
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)


def phase_sort_key(task_id: str):
    """Parses task ID (e.g., 'phase_1/01_foo') to return a sortable tuple (phase_num, task_num)."""
    parts = task_id.split("/")
    if len(parts) >= 2:
        phase_part = parts[0]
        task_part = parts[1]
        
        phase_num = 0
        if phase_part.startswith("phase_"):
            try:
                phase_num = int(phase_part.split("_")[1])
            except ValueError:
                pass
                
        task_num = 0
        try:
            task_num = int(task_part.split("_")[0])
        except ValueError:
            pass
            
        return (phase_num, task_num)
    return (999, 999)
    
def get_task_details(root_dir: str, full_task_id: str) -> str:
    """Reads all markdown files for a given task and returns them as a single context string."""
    task_path = os.path.join(root_dir, "docs", "plan", "tasks", full_task_id)
    content = ""
    if os.path.isfile(task_path):
        with open(task_path, "r", encoding="utf-8") as file:
            content += file.read() + "\n\n"
    elif os.path.isdir(task_path):
        for f in os.listdir(task_path):
            if f.endswith(".md"):
                with open(os.path.join(task_path, f), "r", encoding="utf-8") as file:
                    content += file.read() + "\n\n"
    return content


def get_memory_context(root_dir: str) -> str:
    memory_file = os.path.join(root_dir, ".agent", "MEMORY.md")
    if os.path.exists(memory_file):
        with open(memory_file, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def get_project_context(root_dir: str) -> str:
    desc_file = os.path.join(root_dir, "docs", "plan", "input", "description.md")
    if os.path.exists(desc_file):
        with open(desc_file, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def run_agent(agent_type: str, prompt_file: str, root_dir: str, task_context: dict, cwd: str, backend: str = "gemini") -> bool:
    """Formats the prompt and executes the AI agent."""
    prompt_path = os.path.join(root_dir, "scripts", "prompts", prompt_file)
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt_tmpl = f.read()

    # Simple template replacement
    prompt = prompt_tmpl
    for k, v in task_context.items():
        prompt = prompt.replace(f"{{{k}}}", str(v))

    print(f"      [{agent_type}] Starting agent in {cwd}...")

    phase_id = task_context.get("phase_filename", "phase")
    task_name = task_context.get("task_name", "task")
    short_task = task_name[:15] + ".." if len(task_name) > 15 else task_name
    prefix = f"[{phase_id}/{short_task}] "

    returncode = run_ai_command(prompt, cwd, prefix=prefix, backend=backend)
    
    if returncode != 0:
        print(f"      [{agent_type}] FATAL: Agent process failed with exit code {returncode}")
        return False
        
    return True


def get_existing_worktree(root_dir: str, branch_name: str) -> str:
    """Returns the path of an existing worktree for the given branch, or None."""
    try:
        res = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root_dir, capture_output=True, text=True, check=True)
        current_wt = None
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                current_wt = line[9:].strip()
            elif line.startswith("branch ") and line[7:].endswith(f"refs/heads/{branch_name}"):
                if current_wt and os.path.isdir(current_wt):
                    return current_wt
                else:
                    # Stale worktree detected, prune it
                    print(f"      Cleaning stale worktree metadata for {branch_name}...")
                    subprocess.run(["git", "worktree", "prune"], cwd=root_dir, check=False)
                    # Also try to delete the branch if it's not merged, or just let add -B handle it
                    return None
    except subprocess.CalledProcessError:
        pass
    return None


def process_task(root_dir: str, full_task_id: str, presubmit_cmd: str, backend: str = "gemini", max_retries: int = 3) -> bool:
    """Handles the lifecycle of a single task: worktree creation, agents, and commit."""
    phase_id, task_id = full_task_id.split("/", 1)
    safe_task_id = task_id.replace("/", "_").replace(".md", "")
    branch_name = f"ai-phase-{safe_task_id}"
    
    print(f"\n   -> [Implementation] Starting {full_task_id}")
    
    tmpdir = ""
    success = False
    try:
        existing_wt = get_existing_worktree(root_dir, branch_name)
        if existing_wt:
            tmpdir = existing_wt
            print(f"      Found existing worktree at {tmpdir} on branch {branch_name}. Resetting to dev...")
            try:
                subprocess.run(["git", "reset", "--hard", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                subprocess.run(["git", "clean", "-fd"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as e:
                print(f"      [!] Failed to reset existing worktree:\n{e.stderr.decode('utf-8')}")
                return False
        else:
            tmpdir = tempfile.mkdtemp(prefix=f"ai_{safe_task_id}_")
            print(f"      Creating git worktree at {tmpdir} on branch {branch_name}...")
            try:
                subprocess.run(["git", "worktree", "add", "-B", branch_name, tmpdir, "dev"], cwd=root_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as e:
                print(f"      [!] Failed to create worktree:\n{e.stderr.decode('utf-8')}")
                return False

        task_details = get_task_details(root_dir, full_task_id)
        description_ctx = get_project_context(root_dir)
        memory_ctx = get_memory_context(root_dir)
        
        context = {
            "phase_filename": phase_id,
            "task_name": task_id,
            "target_dir": full_task_id,
            "task_details": task_details,
            "description_ctx": description_ctx,
            "memory_ctx": memory_ctx,
            "worktree_dir": tmpdir
        }

        # 1. Implementation Agent
        if not run_agent("Implementation", "implement_task.md", root_dir, context, tmpdir, backend):
            return False

        # 2. Review Agent
        if not run_agent("Review", "review_task.md", root_dir, context, tmpdir, backend):
            return False

        # 3. Verification Loop
        for attempt in range(1, max_retries + 1):
            print(f"      [Verification] Running presubmit (Attempt {attempt}/{max_retries})...")
            # We split the command string into a list for subprocess
            cmd_list = presubmit_cmd.split()
            presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True)
            
            if presubmit_res.returncode == 0:
                print(f"      [Verification] Presubmit passed!")
                
                # Commit the changes
                subprocess.run(["git", "add", "-A"], cwd=tmpdir, check=True)
                # Only commit if there are changes
                status = subprocess.run(["git", "status", "--porcelain"], cwd=tmpdir, capture_output=True, text=True)
                if status.stdout.strip():
                     commit_msg = f"{phase_id}:{task_id}: Standardized Implementation"
                     match = re.search(r'^#\s*Task:\s*(.*?)(?:\s*\(Sub-Epic:.*?\))?$', task_details, re.MULTILINE)
                     if match and match.group(1).strip():
                         commit_msg = f"{phase_id}:{task_id}: {match.group(1).strip()}"
                     subprocess.run(["git", "commit", "--no-verify", "-m", commit_msg], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL)
                else:
                     print(f"      [Verification] No changes to commit for {full_task_id}.")
                success = True
                return True
            
            print(f"      [Verification] Presubmit failed.")
            if attempt < max_retries:
                 # Feed the failure back to the review agent
                 failure_ctx = dict(context)
                 failure_ctx["task_details"] += f"\n\n### PRESUBMIT FAILURE (Attempt {attempt})\nThe presubmit script failed with the following output. Please fix the code.\n\n```\n{presubmit_res.stdout}\n{presubmit_res.stderr}\n```\n"
                 if not run_agent("Review (Retry)", "review_task.md", root_dir, failure_ctx, tmpdir, backend):
                     return False
                     
        print(f"   -> [!] Task {full_task_id} failed presubmit {max_retries} times. Aborting task.")
        return False
        
    finally:
        if success:
            # Cleanup worktree
            print(f"      Cleaning up worktree {tmpdir}...")
            subprocess.run(["git", "worktree", "remove", "-f", tmpdir], cwd=root_dir, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print(f"      [!] Task failed. Leaving worktree {tmpdir} and branch {branch_name} for investigation.")


def merge_task(root_dir: str, task_id: str, presubmit_cmd: str, backend: str = "gemini", max_retries: int = 3) -> bool:
    """Creates a clean clone of the repo, merges the task branch, and verifies presubmit."""
    phase_part, name_part = task_id.split("/", 1)
    safe_name_part = name_part.replace("/", "_").replace(".md", "")
    branch_name = f"ai-phase-{safe_name_part}"
    
    # We clone into a new tmpdir to avoid messing with the developer's main working tree
    tmpdir = tempfile.mkdtemp(prefix=f"merge_{safe_name_part}_")
    
    print(f"\n   => [Merge] Attempting to merge {task_id} into dev...")
    print(f"      Cloning repository to {tmpdir}...")
    
    # Clone the repo locally
    subprocess.run(["git", "clone", root_dir, tmpdir], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "checkout", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        context = {
            "phase_filename": phase_part,
            "task_name": name_part,
            "branches_list": branch_name,
            "description_ctx": get_project_context(root_dir)
        }
        
        # 1. Verification Loop for Merge
        for attempt in range(1, max_retries + 1):
            if attempt == 1:
                # First attempt: Try a simple fast-forward merge via git CLI
                print(f"      [Merge] Attempting fast-forward merge (Attempt 1/{max_retries})...")
                # Checkout branch to fetch it into the clone
                subprocess.run(["git", "fetch", "origin", branch_name], cwd=tmpdir, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                merge_res = subprocess.run(["git", "merge", "--ff-only", f"origin/{branch_name}"], cwd=tmpdir, capture_output=True, text=True)
                
                if merge_res.returncode == 0:
                    print(f"      [Merge] Fast-forward successful. Skipping presubmit...")
                    print(f"      [Merge] Pushing to local origin.")
                    subprocess.run(["git", "push", "origin", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                else:
                    print(f"      [Merge] Fast-forward failed (diverged). Attempting rebase...")
                    # Let's try to rebase the task branch onto the current dev
                    rebase_res = subprocess.run(["git", "rebase", "dev", f"origin/{branch_name}"], cwd=tmpdir, capture_output=True, text=True)
                    if rebase_res.returncode == 0:
                        print(f"      [Merge] Rebase successful. Verifying with presubmit...")
                        # Now we are on the task branch (detached or new head). We need to update dev to this point.
                        # Since rebase succeeded, we can just fast-forward dev to this new head.
                        new_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmpdir, capture_output=True, text=True).stdout.strip()
                        subprocess.run(["git", "checkout", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["git", "merge", "--ff-only", new_head], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        
                        cmd_list = presubmit_cmd.split()
                        presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True)
                        if presubmit_res.returncode == 0:
                            print(f"      [Merge] Presubmit passed after rebase! Pushing to local origin.")
                            subprocess.run(["git", "push", "origin", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            return True
                        else:
                            print(f"      [Merge] Presubmit failed after rebase.")
                            failure_output = f"{presubmit_res.stdout}\n{presubmit_res.stderr}"
                            # We failed presubmit, so we fall through to the agent attempt in next iteration
                    else:
                        print(f"      [Merge] Rebase failed to apply cleanly. Aborting rebase.")
                        subprocess.run(["git", "rebase", "--abort"], cwd=tmpdir, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        # Ensure we are back on dev
                        subprocess.run(["git", "checkout", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        failure_output = f"{rebase_res.stdout}\n{rebase_res.stderr}"
            else:
                # Merge Agent Attempt
                print(f"      [Merge] Spawning Merge Agent to resolve conflicts (Attempt {attempt}/{max_retries})...")
                
                # Reset to clean dev before the agent tries
                subprocess.run(["git", "reset", "--hard", "origin/dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "clean", "-fd"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                failure_ctx = dict(context)
                failure_ctx["description_ctx"] += f"\n\n### PREVIOUS ATTEMPT FAILURE\nThe previous merge or presubmit failed with:\n```\n{failure_output}\n```\n"
                
                if not run_agent("Merge", "merge_task.md", root_dir, failure_ctx, tmpdir, backend):
                    print(f"      [!] Merge agent failed to cleanly exit.")
                    continue
                    
                # The agent claims it's done. Let's verify.
                print(f"      [Merge] Verifying agent's merge...")
                cmd_list = presubmit_cmd.split()
                presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True)
                
                if presubmit_res.returncode == 0:
                     print(f"      [Merge] Presubmit passed! Pushing to local origin.")
                     subprocess.run(["git", "push", "origin", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     return True
                else:
                     failure_output = f"{presubmit_res.stdout}\n{presubmit_res.stderr}"
                     print(f"      [Merge] Presubmit failed after agent merge.")
                     
        print(f"   -> [!] Failed to merge {task_id} after {max_retries} attempts.")
        return False
        
    finally:
        # Cleanup clone
        print(f"      Cleaning up merge clone {tmpdir}...")
        subprocess.run(["rm", "-rf", tmpdir])


def get_ready_tasks(master_dag: Dict[str, List[str]], completed_tasks: List[str], active_tasks: List[str]) -> List[str]:
    """Returns a list of task IDs whose prerequisites are fully met and aren't already running or completed."""
    ready = []
    completed_set = set(completed_tasks)
    
    # 1. First, find all tasks that are ready regardless of phase
    all_ready = []
    for task_id, prereqs in master_dag.items():
        if task_id in completed_set or task_id in active_tasks:
            continue
            
        # Check if all prerequisites are in the completed set
        if all(prereq in completed_set for prereq in prereqs):
            all_ready.append(task_id)

    if not all_ready:
        return []

    # 2. Find the lowest (earliest) phase among all incomplete tasks to act as a barrier
    incomplete_tasks = [tid for tid in master_dag.keys() if tid not in completed_set]
    if not incomplete_tasks:
         return []
         
    incomplete_tasks.sort(key=phase_sort_key)
    active_phase_num = phase_sort_key(incomplete_tasks[0])[0]

    # 3. Filter ready tasks to only allow tasks from the active phase
    for task_id in all_ready:
         if phase_sort_key(task_id)[0] == active_phase_num:
             ready.append(task_id)
            
    # Sort the final ready list
    ready.sort(key=phase_sort_key)
    return ready


def execute_dag(root_dir: str, master_dag: Dict[str, List[str]], state: Dict[str, Any], state_file: str, jobs: int, presubmit_cmd: str, backend: str = "gemini"):
    """Orchestrates the parallel execution of tasks according to the DAG."""
    # Ensure dev branch exists
    res = subprocess.run(["git", "rev-parse", "--verify", "dev"], cwd=root_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        subprocess.run(["git", "branch", "dev", "main"], cwd=root_dir, check=True)

    active_tasks = set()
    failed_tasks = set()
    state_lock = threading.Lock()
    
    print("\n=> Starting Parallel DAG Execution Loop...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        # Dictionary to keep track of futures mapping to task_id
        future_to_task = {}
        
        while True:
            # Check for newly ready tasks
            with state_lock:
                ready_tasks = get_ready_tasks(master_dag, state["completed_tasks"], list(active_tasks))
            
            # Submit ready tasks if we have capacity
            for task_id in ready_tasks:
                if len(active_tasks) >= jobs:
                    break
                    
                with state_lock:
                    active_tasks.add(task_id)
                    
                future = executor.submit(process_task, root_dir, task_id, presubmit_cmd, backend)
                future_to_task[future] = task_id
            
            # If no tasks are running and none are ready, we are either done or deadlocked
            if not future_to_task:
                with state_lock:
                    if failed_tasks:
                        break
                    
                    if len(state["completed_tasks"]) == len(master_dag):
                        print("\n=> All implementation tasks completed successfully!")
                        break
                    else:
                        print("\n[!] FATAL: DAG deadlock or unrecoverable error. No tasks running and none ready.")
                        print(f"    Completed: {len(state['completed_tasks'])} / {len(master_dag)}")
                        os._exit(1)
            
            # Wait for at least one future to complete
            done, not_done = concurrent.futures.wait(
                future_to_task.keys(), 
                return_when=concurrent.futures.FIRST_COMPLETED
            )
            
            for future in done:
                task_id = future_to_task.pop(future)
                with state_lock:
                    active_tasks.remove(task_id)
                    
                try:
                    success = future.result()
                    if success:
                        print(f"   -> [Implementation] Task {task_id} completed successfully.")
                        
                        # Trigger DAG Merge Workflow immediately
                        if merge_task(root_dir, task_id, presubmit_cmd, backend):
                            with state_lock:
                                state["completed_tasks"].append(task_id)
                                state["merged_tasks"].append(task_id)
                                save_workflow_state(state_file, state)
                            print(f"   -> [Success] Task {task_id} fully integrated into dev.")
                            print(f"      Pushing dev to remote origin...")
                            subprocess.run(["git", "push", "origin", "dev"], cwd=root_dir, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        else:
                            with state_lock:
                                failed_tasks.add(f"Task {task_id} failed merging into dev.")
                            executor.shutdown(wait=True, cancel_futures=True)
                    else:
                        with state_lock:
                            failed_tasks.add(f"Task {task_id} failed implementation.")
                        executor.shutdown(wait=True, cancel_futures=True)
                except Exception as exc:
                    traceback.print_exc()
                    with state_lock:
                        failed_tasks.add(f"Task {task_id} generated an exception.")
                    executor.shutdown(wait=True, cancel_futures=True)

    if failed_tasks:
        print("\n" + "="*80)
        for err in failed_tasks:
            print(f"[!] FATAL: {err} Halting workflow.")
        print("="*80 + "\n")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Parallel development workflow orchestrator")
    parser.add_argument("--jobs", type=int, default=1, help="Number of parallel implementation agents")
    parser.add_argument("--presubmit-cmd", type=str, default="./do presubmit", help="Command to evaluate correctness")
    parser.add_argument("--backend", choices=["gemini", "claude", "copilot"], default="gemini", help="AI CLI backend to use (default: gemini)")
    args = parser.parse_args()

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tasks_dir = os.path.join(root_dir, "docs", "plan", "tasks")
    state_file = os.path.join(root_dir, "scripts", ".workflow_state.json")
    log_file = os.path.join(root_dir, "run_workflow.log")

    # Redirect stdout and stderr to both terminal and log file
    log_stream = open(log_file, "a", encoding="utf-8")
    log_lock = threading.Lock()
    sys.stdout = Logger(sys.stdout, log_stream, log_lock)
    sys.stderr = Logger(sys.stderr, log_stream, log_lock)

    master_dag = load_dags(tasks_dir)
    state = load_workflow_state(state_file)
    
    print(f"Loaded {len(master_dag)} tasks across all phases.")
    execute_dag(root_dir, master_dag, state, state_file, args.jobs, args.presubmit_cmd, args.backend)

if __name__ == "__main__":
    main()
