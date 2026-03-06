import os
import subprocess
import sys
import json
import re
from typing import List, Dict, Any, Optional
import threading
import concurrent.futures
import tempfile
import traceback
from datetime import datetime, timezone

from .constants import TOOLS_DIR, ROOT_DIR
from .context import ProjectContext
from .state import save_workflow_state

shutdown_requested = False


def signal_handler(sig, frame):
    global shutdown_requested
    if not shutdown_requested:
        print("\n[!] Ctrl-C detected. Initiating graceful shutdown...")
        print("    Waiting for active tasks to complete and merge before exiting.")
        shutdown_requested = True
    else:
        print("\n[!] Ctrl-C detected again. Forcing immediate exit...")
        os._exit(1)
def get_gitlab_remote_url(root_dir: str) -> str:
    """Returns the URL of the gitlab or origin remote if it contains 'gitlab'."""
    try:
        res = subprocess.run(["git", "remote", "-v"], cwd=root_dir, capture_output=True, text=True, check=True)
        for line in res.stdout.splitlines():
            if "gitlab.lan" in line or "gitlab" in line.lower():
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
    except subprocess.CalledProcessError:
        pass
    return "http://gitlab.lan/mrwilson/dreamer"

class Logger(object):
    def __init__(self, terminal, log_stream, lock):
        self.terminal = terminal
        self.log_stream = log_stream
        self.lock = lock
        self._at_line_start = True

    def write(self, message):
        with self.lock:
            # Prepend timestamp at the start of each new line
            parts = message.splitlines(keepends=True)
            out = ""
            for part in parts:
                if self._at_line_start and not part.startswith("\n"):
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    out += f"[{ts}] {part}"
                else:
                    out += part
                self._at_line_start = part.endswith("\n")

            self.terminal.write(out)
            self.log_stream.write(out)
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
    
def get_task_details(full_task_id: str) -> str:
    """Reads all markdown files for a given task and returns them as a single context string."""
    task_path = os.path.join(ROOT_DIR, "docs", "plan", "tasks", full_task_id)
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


def get_project_context(tools_dir: str) -> str:
    desc_file = os.path.join(TOOLS_DIR, "input", "project-description.md")
    if os.path.exists(desc_file):
        with open(desc_file, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def run_agent(agent_type: str, prompt_file: str, task_context: dict, cwd: str, backend: str = "gemini") -> bool:
    """Formats the prompt and executes the AI agent."""
    prompt_path = os.path.join(TOOLS_DIR, "prompts", prompt_file)
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


def rebuild_serena_cache(source_dir: str, root_dir: str, cache_lock: threading.Lock):
    """Rebuilds the Serena cache in source_dir (which has dev checked out) and
    copies it back to the main repo's .serena/cache/. source_dir is typically a merge clone."""
    cache_src = os.path.join(source_dir, ".serena", "cache")
    cache_dst = os.path.join(root_dir, ".serena", "cache")

    print(f"      [Serena] Re-indexing from {source_dir}...")
    # Serena indexes on MCP server startup. Start it to trigger indexing, then terminate.
    serena_proc = subprocess.Popen(
        ["uvx", "--from", "serena", "serena-mcp-server",
         "--project-from-cwd", "--mode", "no-onboarding"],
        cwd=source_dir,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        serena_proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        serena_proc.terminate()
        serena_proc.wait()

    if not os.path.isdir(cache_src):
        print(f"      [Serena] Warning: cache not found at {cache_src} after indexing. Skipping.")
        return

    with cache_lock:
        tmp_dst = cache_dst + ".tmp"
        if os.path.isdir(tmp_dst):
            shutil.rmtree(tmp_dst)
        shutil.copytree(cache_src, tmp_dst)
        if os.path.isdir(cache_dst):
            shutil.rmtree(cache_dst)
        os.rename(tmp_dst, cache_dst)
    print(f"      [Serena] Cache updated at {cache_dst}.")


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

        # Seed Serena cache from main repo so agents don't start cold
        serena_cache_src = os.path.join(root_dir, ".serena", "cache")
        serena_cache_dst = os.path.join(tmpdir, ".serena", "cache")
        if os.path.isdir(serena_cache_src) and not os.path.isdir(serena_cache_dst):
            shutil.copytree(serena_cache_src, serena_cache_dst)

        # Copy .mcp.json so Claude CLI picks up Serena in the worktree
        mcp_src = os.path.join(root_dir, ".mcp.json")
        mcp_dst = os.path.join(tmpdir, ".mcp.json")
        if os.path.exists(mcp_src) and not os.path.exists(mcp_dst):
            shutil.copy2(mcp_src, mcp_dst)

        task_details = get_task_details(full_task_id)
        description_ctx = get_project_context()
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
        if not run_agent("Implementation", "implement_task.md", context, tmpdir, backend):
            return False

        # 2. Review Agent
        if not run_agent("Review", "review_task.md", context, tmpdir, backend):
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
                 if not run_agent("Review (Retry)", "review_task.md", failure_ctx, tmpdir, backend):
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


def merge_task(root_dir: str, task_id: str, presubmit_cmd: str, backend: str = "gemini", max_retries: int = 3, cache_lock: Optional[threading.Lock] = None) -> bool:
    """Creates a clean clone of the repo, merges the task branch via squash, and verifies presubmit."""
    phase_part, name_part = task_id.split("/", 1)
    safe_name_part = name_part.replace("/", "_").replace(".md", "")
    branch_name = f"ai-phase-{safe_name_part}"
    
    # Extract task title for the commit message
    task_details = get_task_details(task_id)
    commit_msg = f"{phase_part}:{name_part}: Standardized Implementation"
    match = re.search(r'^#\s*Task:\s*(.*?)(?:\s*\(Sub-Epic:.*?\))?$', task_details, re.MULTILINE)
    if match and match.group(1).strip():
        commit_msg = f"{phase_part}:{name_part}: {match.group(1).strip()}"

    # We clone into a new tmpdir to avoid messing with the developer's main working tree
    tmpdir = tempfile.mkdtemp(prefix=f"merge_{safe_name_part}_")
    
    print(f"\n   => [Merge] Attempting to squash merge {task_id} into dev...")
    print(f"      Cloning repository to {tmpdir}...")
    
    # Clone the repo locally
    subprocess.run(["git", "clone", root_dir, tmpdir], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Ensure gitlab remote exists in the clone for CI
    gitlab_url = get_gitlab_remote_url(root_dir)
    subprocess.run(["git", "remote", "add", "gitlab", gitlab_url], cwd=tmpdir, check=False)
    
    subprocess.run(["git", "checkout", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    push_succeeded = False
    try:
        context = {
            "phase_filename": phase_part,
            "task_name": name_part,
            "branches_list": branch_name,
            "description_ctx": get_project_context()
        }
        
        # 1. Verification Loop for Merge
        for attempt in range(1, max_retries + 1):
            failure_output = ""
            if attempt == 1:
                # First attempt: Try a squash merge via git CLI
                print(f"      [Merge] Attempting squash merge (Attempt 1/{max_retries})...")
                # Checkout branch to fetch it into the clone
                subprocess.run(["git", "fetch", "origin", branch_name], cwd=tmpdir, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                # Perform squash merge
                merge_res = subprocess.run(["git", "merge", "--squash", f"origin/{branch_name}"], cwd=tmpdir, capture_output=True, text=True)
                
                if merge_res.returncode == 0:
                    # Check if there are actually changes to commit
                    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmpdir, capture_output=True, text=True)
                    if status.stdout.strip():
                        # Squash merge staged the changes, now commit them
                        subprocess.run(["git", "commit", "--no-verify", "-m", commit_msg], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL)
                    else:
                        print(f"      [Merge] No changes to squash merge for {task_id}.")
                    
                    print(f"      [Merge] Squash successful. Verifying with presubmit...")
                    cmd_list = presubmit_cmd.split()
                    presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True)
                    
                    if presubmit_res.returncode == 0:
                        print(f"      [Merge] Presubmit passed! Pushing to local origin.")
                        res = subprocess.run(["git", "push", "origin", "dev"], cwd=tmpdir, capture_output=True, text=True)
                        if res.returncode != 0:
                            print(f"      [!] Failed to push merge to local origin:\n{res.stderr}")
                            return False
                        push_succeeded = True
                        return True
                    else:
                        print(f"      [Merge] Presubmit failed after squash merge.")
                        failure_output = f"{presubmit_res.stdout}\n{presubmit_res.stderr}"
                else:
                    print(f"      [Merge] Squash merge failed (conflicts). Attempting rebase...")
                    # Clean up failed squash merge state before rebasing
                    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # Let's try to rebase the task branch onto the current dev to help resolve conflicts
                    rebase_res = subprocess.run(["git", "rebase", "dev", f"origin/{branch_name}"], cwd=tmpdir, capture_output=True, text=True)
                    if rebase_res.returncode == 0:
                        print(f"      [Merge] Rebase successful. Retrying squash merge...")
                        # Now that we rebased origin/{branch_name} locally, let's try to squash it into dev
                        new_task_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmpdir, capture_output=True, text=True).stdout.strip()
                        subprocess.run(["git", "checkout", "dev"], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        
                        # Try squash again from the newly rebased head
                        merge_res = subprocess.run(["git", "merge", "--squash", new_task_head], cwd=tmpdir, capture_output=True, text=True)
                        if merge_res.returncode == 0:
                            # Check if there are actually changes to commit
                            status = subprocess.run(["git", "status", "--porcelain"], cwd=tmpdir, capture_output=True, text=True)
                            if status.stdout.strip():
                                subprocess.run(["git", "commit", "--no-verify", "-m", commit_msg], cwd=tmpdir, check=True, stdout=subprocess.DEVNULL)
                            else:
                                print(f"      [Merge] No changes to squash merge after rebase for {task_id}.")
                            
                            cmd_list = presubmit_cmd.split()
                            presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True)
                            if presubmit_res.returncode == 0:
                                print(f"      [Merge] Presubmit passed after rebase + squash! Pushing to local origin.")
                                res = subprocess.run(["git", "push", "origin", "dev"], cwd=tmpdir, capture_output=True, text=True)
                                if res.returncode != 0:
                                    print(f"      [!] Failed to push merge to local origin:\n{res.stderr}")
                                    return False
                                push_succeeded = True
                                return True
                            else:
                                print(f"      [Merge] Presubmit failed after rebase + squash.")
                                failure_output = f"{presubmit_res.stdout}\n{presubmit_res.stderr}"
                        else:
                            print(f"      [Merge] Squash merge still failed after rebase.")
                            failure_output = f"{merge_res.stdout}\n{merge_res.stderr}"
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
                failure_ctx["description_ctx"] += f"\n\n### PREVIOUS ATTEMPT FAILURE\nThe previous squash merge or presubmit failed with:\n```\n{failure_output}\n```\n"
                failure_ctx["description_ctx"] += f"\nPlease resolve the conflicts and ensure the final state is a single commit on the dev branch with the message: {commit_msg}"
                
                if not run_agent("Merge", "merge_task.md", failure_ctx, tmpdir, backend):
                    print(f"      [!] Merge agent failed to cleanly exit.")
                    continue
                    
                # The agent claims it's done. Let's verify.
                print(f"      [Merge] Verifying agent's merge...")
                cmd_list = presubmit_cmd.split()
                presubmit_res = subprocess.run(cmd_list, cwd=tmpdir, capture_output=True, text=True)
                
                if presubmit_res.returncode == 0:
                     print(f"      [Merge] Presubmit passed! Pushing to local origin.")
                     res = subprocess.run(["git", "push", "origin", "dev"], cwd=tmpdir, capture_output=True, text=True)
                     if res.returncode != 0:
                         print(f"      [!] Failed to push merge to local origin:\n{res.stderr}")
                         return False
                     push_succeeded = True
                     return True
                else:
                     failure_output = f"{presubmit_res.stdout}\n{presubmit_res.stderr}"
                     print(f"      [Merge] Presubmit failed after agent merge.")
                     
        print(f"   -> [!] Failed to merge {task_id} after {max_retries} attempts.")
        return False
        
    finally:
        if push_succeeded and cache_lock is not None:
            rebuild_serena_cache(tmpdir, root_dir, cache_lock)
        # Cleanup clone
        print(f"      Cleaning up merge clone {tmpdir}...")
        subprocess.run(["rm", "-rf", tmpdir])


def load_blocked_tasks() -> set:
    """Load blocked task IDs from replan state."""
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    replan_state_file = os.path.join("scripts", ".replan_state.json")
    if os.path.exists(replan_state_file):
        with open(replan_state_file, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
                return set(state.get("blocked_tasks", {}).keys())
            except (json.JSONDecodeError, AttributeError):
                pass
    return set()


def get_ready_tasks(master_dag: Dict[str, List[str]], completed_tasks: List[str], active_tasks: List[str]) -> List[str]:
    """Returns a list of task IDs whose prerequisites are fully met and aren't already running or completed."""
    ready = []
    completed_set = set(completed_tasks)
    blocked_set = load_blocked_tasks()

    # 1. First, find all tasks that are ready regardless of phase
    all_ready = []
    for task_id, prereqs in master_dag.items():
        if task_id in completed_set or task_id in active_tasks or task_id in blocked_set:
            continue

        # Check if all prerequisites are in the completed set (blocked prereqs are NOT met)
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


def execute_dag(root_dir: str, master_dag: Dict[str, List[str]], state: Dict[str, Any], jobs: int, presubmit_cmd: str, backend: str = "gemini"):
    """Orchestrates the parallel execution of tasks according to the DAG."""
    # Ensure dev branch exists
    res = subprocess.run(["git", "rev-parse", "--verify", "dev"], cwd=root_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        subprocess.run(["git", "branch", "dev", "main"], cwd=root_dir, check=True)

    # Bootstrap Serena cache from dev if not present
    cache_lock = threading.Lock()
    serena_cache = os.path.join(root_dir, ".serena", "cache")
    if not os.path.isdir(serena_cache):
        print("=> [Serena] No cache found. Bootstrapping index from dev branch...")
        init_wt = tempfile.mkdtemp(prefix="serena_init_")
        try:
            subprocess.run(["git", "worktree", "add", "--detach", init_wt, "dev"],
                           cwd=root_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            rebuild_serena_cache(init_wt, root_dir, cache_lock)
        finally:
            subprocess.run(["git", "worktree", "remove", "-f", init_wt],
                           cwd=root_dir, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    active_tasks = set()
    failed_tasks = set()
    state_lock = threading.Lock()
    
    print("\n=> Starting Parallel DAG Execution Loop...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        # Dictionary to keep track of futures mapping to task_id
        future_to_task = {}
        
        while True:
            # Check for newly ready tasks
            ready_tasks = []
            if not shutdown_requested:
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
            
            # If no tasks are running and (none are ready or shutdown requested), we are done/deadlocked
            if not future_to_task:
                if shutdown_requested:
                    print("\n=> Graceful shutdown complete. Exiting.")
                    break
                    
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
                        if merge_task(root_dir, task_id, presubmit_cmd, backend, cache_lock=cache_lock):
                            with state_lock:
                                state["completed_tasks"].append(task_id)
                                state["merged_tasks"].append(task_id)
                                save_workflow_state(state)
                            print(f"   -> [Success] Task {task_id} fully integrated into dev.")
                            print(f"      Pushing dev to remote origin...")
                            push_res = subprocess.run(["git", "push", "origin", "dev"], cwd=root_dir, capture_output=True, text=True)
                            if push_res.returncode != 0:
                                print(f"      [!] Failed to push to remote:\n{push_res.stderr}")
                            else:
                                print(f"      [Push] Success.")
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


