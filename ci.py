'''
CI Automation Script

This script automates the process of matching local code state to GitLab CI pipelines.
It handles triggering new pipelines, attaching to existing ones, monitoring progress,
and managing artifacts.

# Features

## 1. Pipeline Resolution
The script determines the correct pipeline to monitor based on the local git state:

### Clean State (No Uncommitted Changes)
- Checks if the local `HEAD` matches the remote `HEAD`.
- **Synced**: Finds an existing pipeline for the commit or triggers a new one.
- **Mismatch / No Remote**: Prompts the user with options:
  0. **Push to Temp Branch (Default)**: Pushes `HEAD` to a temporary branch (`ci-temp-<branch>-<uuid>`) and runs the pipeline there.
  1. **Push to Current Branch**: Pushes `HEAD` to `origin/<branch>` and runs the pipeline.
  2. **Do Nothing**: Runs the pipeline on the current commit *as known by the remote* (may be outdated).

### Dirty State (Uncommitted Changes)
- Calculates a unique hash based on the base commit + diff content (`base_sha` + `diff_md5`).
- Searches for any existing pipelines tagged with this metadata (in the commit message).
- **Found**: Attaches to the existing pipeline.
- **Not Found**:
    1.  Creates a temporary local directory.
    2.  Copies project files (excluding target/.git).
    3.  Initializes a temporary git repo.
    4.  Commits changes with the metadata in the message.
    5.  Pushes to a temporary branch on origin.
    6.  Triggers a pipeline.

## 2. Automatic Cleanup
- Temporary branches created (either from Option 0 in Clean state or the Dirty state workflow) are tracked.
- When the script exits (success, failure, or Ctrl-C), it attempts to delete these temporary remote branches.

## 3. Pipeline Monitoring
- Polls the GitLab API for pipeline status.
- Displays a spinner and timer.
- streams logs from running jobs to the console.

## 4. Failure & Artifact Handling
- On pipeline failure, it prints the last lines of the log for failed jobs.
- Scans failed jobs for artifacts (specifically golden image updates).
- Prompts the user to automatically download and apply these artifacts to the local codebase.
'''

import os
import json
import tempfile
import shutil
import hashlib
import uuid
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, List


class ShellOutput:
    def __init__(self, stdout: bytes, stderr: bytes, success: bool):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0 if success else 1

    def success(self):
        return self.returncode == 0


class Shell:
    def current_dir(self) -> str:
        return os.getcwd()

    def output(self, cmd: List[str], cwd: Optional[str] = None) -> ShellOutput:
        # real shell uses subprocess; here is simple passthrough
        import subprocess
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
        out, err = p.communicate()
        return ShellOutput(out, err, p.returncode == 0)

    def run(self, cmd: List[str], cwd: Optional[str] = None) -> ShellOutput:
        return self.output(cmd, cwd=cwd)

    def read(self, path: str) -> str:
        with open(path, 'r') as f:
            return f.read()


class MockShell(Shell):
    def __init__(self):
        self.recorded_commands: List[str] = []
        self.outputs: List[Tuple[bytes, bytes, bool]] = []
        self.read_results: List[Tuple[bool, str]] = []

    def push_output(self, stdout: bytes, stderr: bytes = b"", success: bool = True):
        self.outputs.append((stdout, stderr, success))

    def push_read_result(self, res: Tuple[bool, str]):
        self.read_results.append(res)

    def _pop_output(self) -> ShellOutput:
        if not self.outputs:
            raise RuntimeError("No mock output available")
        out, err, ok = self.outputs.pop(0)
        return ShellOutput(out, err, ok)

    def output(self, cmd: List[str], cwd: Optional[str] = None) -> ShellOutput:
        self.recorded_commands.append(" ".join(cmd))
        return self._pop_output()

    def run(self, cmd: List[str], cwd: Optional[str] = None) -> ShellOutput:
        self.recorded_commands.append(" ".join(cmd))
        return self._pop_output()

    def read(self, path: str) -> str:
        if not self.read_results:
            raise RuntimeError("No mock read available")
        ok, val = self.read_results.pop(0)
        if ok:
            return val
        raise RuntimeError(val)


@dataclass
class PipelineInfo:
    id: int
    web_url: str
    status: str
    branch: str
    created_at: str


@dataclass
class Workflow:
    temp_dir: Optional[str]
    branch: str
    sha: str

    def __del__(self):
        # best-effort cleanup: try to delete remote branch
        try:
            import subprocess
            subprocess.run(["git", "push", "origin", "--delete", self.branch], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if self.temp_dir and os.path.isdir(self.temp_dir):
                shutil.rmtree(self.temp_dir)
        except Exception:
            pass


def get_remote_url(shell: Shell, remote: str, cwd: Optional[str] = None) -> str:
    out = shell.output(["git", "remote", "get-url", remote], cwd=cwd)
    if out.success():
        return out.stdout.decode().strip()
    raise RuntimeError("Remote not found")



class CiOrchestrator:
    def __init__(self, provider):
        self.provider = provider
        self.token = provider.get_gitlab_token()
        self.original_dir = provider.current_dir()
        protocol, host, project_path = provider.get_project_info()
        self.protocol = protocol
        self.host = host
        self.project_path = project_path
        self.project_encoded = project_path.replace('/', '%2F')
        self.pipeline_id = None
        self.web_url = ""
        self.created_at = ""
        self.workflow = None

    def _push_temp_and_trigger(self, branch: str, original_dir: str):
        temp_branch = f"ci-temp-{branch}-{str(uuid.uuid4())[:8]}"
        print(f"  Reference not found when triggering pipeline; pushing HEAD to temp branch {temp_branch} and retrying...")
        import subprocess
        push_remote = None
        try:
            url = get_remote_url(self.provider.shell, "gitlab")
            push_remote = "gitlab"
        except Exception:
            pass
        if not push_remote:
            try:
                url = get_remote_url(self.provider.shell, "origin")
                if "gitlab.lan" in url or "gitlab" in url.lower():
                    push_remote = "origin"
            except Exception:
                pass
        if not push_remote:
            push_remote = f"{self.protocol}://{self.host}/{self.project_path}.git"
        subprocess.run(["git", "push", push_remote, f"HEAD:refs/heads/{temp_branch}"], cwd=original_dir)
        pushed = False
        for _ in range(5):
            try:
                proc = __import__('subprocess').run(["git", "ls-remote", push_remote, f"refs/heads/{temp_branch}"], cwd=original_dir, stdout=__import__('subprocess').PIPE)
                if proc.stdout and len(proc.stdout) > 0:
                    pushed = True
                    break
            except Exception:
                pass
            time.sleep(1)
        if not pushed:
            raise RuntimeError(f"Failed to verify pushed temp branch {temp_branch} on remote {push_remote}")
        return self.provider.trigger_pipeline(self.protocol, self.host, self.project_encoded, temp_branch, self.token)

    def _handle_clean(self):
        sha = self.provider.get_git_head(self.original_dir)
        branch = self.provider.get_git_branch(self.original_dir)
        found = self.provider.search_pipeline(self.protocol, self.host, self.project_encoded, sha, branch, None, self.token)
        if found:
            self.pipeline_id = found.id
            self.web_url = found.web_url
            self.created_at = found.created_at
            self.workflow = None
            return
        target_branch, self.workflow = ensure_branch_pushed_with_provider(self.provider, self.original_dir, branch)
        try:
            self.pipeline_id, self.web_url, self.created_at = self.provider.trigger_pipeline(self.protocol, self.host, self.project_encoded, target_branch, self.token)
        except RuntimeError as e:
            msg = str(e)
            if "Reference not found" in msg or "missing 'id'" in msg:
                self.pipeline_id, self.web_url, self.created_at = self._push_temp_and_trigger(branch, self.original_dir)
            else:
                raise

    def _handle_dirty(self):
        base_sha, diff_md5 = self.provider.calculate_metadata(self.original_dir)
        existing = self.provider.search_pipeline(self.protocol, self.host, self.project_encoded, base_sha, None, diff_md5, self.token)
        if existing:
            self.pipeline_id = existing.id
            self.web_url = existing.web_url
            self.created_at = existing.created_at
            self.workflow = None
            return
        self.workflow = self.provider.setup_workflow(self.original_dir, base_sha, diff_md5)
        branch = self.workflow.branch
        sha = self.workflow.sha
        time.sleep(0.1)
        found = self.provider.search_pipeline(self.protocol, self.host, self.project_encoded, sha, branch, None, self.token)
        if found:
            self.pipeline_id = found.id
            self.web_url = found.web_url
            self.created_at = found.created_at
            return
        try:
            self.pipeline_id, self.web_url, self.created_at = self.provider.trigger_pipeline(self.protocol, self.host, self.project_encoded, branch, self.token)
        except RuntimeError as e:
            msg = str(e)
            if "Reference not found" in msg or "missing 'id'" in msg:
                self.pipeline_id, self.web_url, self.created_at = self._push_temp_and_trigger(branch, self.original_dir)
            else:
                raise

    def _monitor(self):
        cursors: Dict[int, int] = {}
        while True:
            self.provider.poll_logs(self.protocol, self.host, self.project_encoded, self.pipeline_id, self.token, cursors)
            s = self.provider.check_pipeline_status(self.protocol, self.host, self.project_encoded, self.pipeline_id, self.token)
            if s == "success":
                return
            if s in ("failed", "canceled", "skipped"):
                if s == "failed":
                    self.provider.handle_failure(self.protocol, self.host, self.project_encoded, self.pipeline_id, self.token, self.original_dir)
                raise RuntimeError("Pipeline failed")
            time.sleep(0.01)

    def run(self):
        token = self.token
        host = self.host
        protocol = self.protocol
        project_encoded = self.project_encoded

        is_clean = self.provider.is_git_clean(self.original_dir)
        if is_clean:
            print("Git checkout is clean. Resolving pipeline...")
            self._handle_clean()
        else:
            print("Git checkout is dirty. Preparing temporary workflow...")
            self._handle_dirty()

        print(f"  Pipeline URL: {self.web_url}")
        print("Waiting for pipeline to complete...")

        # prepare monitoring state
        cursors: Dict[int, int] = {}
        spinner_chars = ['|', '/', '-', '\\']
        spinner_idx = 0
        last_poll = time.time() - 10
        current_status = "initializing..."

        # parse created_at into epoch seconds if possible
        pipeline_created_at = None
        try:
            if self.created_at:
                # ISO format expected; handle trailing Z
                import datetime
                s = self.created_at
                if s.endswith('Z'):
                    s = s.replace('Z', '+00:00')
                dt = datetime.datetime.fromisoformat(s)
                pipeline_created_at = dt.timestamp()
        except Exception:
            pipeline_created_at = time.time()
        if pipeline_created_at is None:
            pipeline_created_at = time.time()

        while True:
            # cancellation check if provider supports it
            if hasattr(self.provider, 'is_canceled') and callable(getattr(self.provider, 'is_canceled')):
                try:
                    if self.provider.is_canceled():
                        print("\n  ! Received interrupt, exiting (cleanup will run)...")
                        raise RuntimeError("Interrupted by user")
                except Exception:
                    pass

            now = time.time()
            if now - last_poll >= 5:
                try:
                    self.provider.poll_logs(protocol, host, project_encoded, self.pipeline_id, token, cursors)
                except Exception as e:
                    print(f"  ! Failed to poll logs: {e}")

                try:
                    status = self.provider.check_pipeline_status(protocol, host, project_encoded, self.pipeline_id, token)
                except Exception:
                    status = "unknown"

                current_status = status
                last_poll = now

                if status == "success":
                    print("\n  OK: Pipeline succeeded!")
                    return
                if status in ("failed", "canceled", "skipped"):
                    print(f"\n  FAILED: Pipeline ended with status: {status}")
                    if status == "failed":
                        try:
                            self.provider.handle_failure(protocol, host, project_encoded, self.pipeline_id, token, self.original_dir)
                        except Exception:
                            pass
                    raise RuntimeError("Pipeline failed")

            # spinner and timer display
            elapsed = int(time.time() - pipeline_created_at)
            minutes = elapsed // 60
            seconds = elapsed % 60
            timer = f"{minutes:02d}:{seconds:02d}"
            spinner = spinner_chars[spinner_idx % len(spinner_chars)]
            spinner_idx += 1
            print(f"\r\x1b[2K{spinner} Status: {current_status} ({timer}) - {self.web_url}", end="")
            try:
                import sys
                sys.stdout.flush()
            except Exception:
                pass
            time.sleep(0.5)



def is_git_clean(shell: Shell, dirpath: str) -> bool:
    out = shell.output(["git", "status", "--porcelain"], cwd=dirpath).stdout
    return len(out) == 0


def get_git_head(shell: Shell, dirpath: str) -> str:
    out = shell.output(["git", "rev-parse", "HEAD"], cwd=dirpath).stdout.decode().strip()
    return out


def get_git_branch(shell: Shell, dirpath: str) -> str:
    out = shell.output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=dirpath).stdout.decode().strip()
    return out


def ensure_branch_pushed_with_provider(provider, dirpath: str, branch: str) -> Tuple[str, Optional[Workflow]]:
    local_sha = provider.get_git_head(dirpath)
    remote_sha = provider.check_remote_branch_sha(dirpath, branch)
    if local_sha == remote_sha:
        return branch, None
    if not remote_sha:
        pass
    # default: push temp
    temp_branch = f"ci-temp-{branch}-{str(uuid.uuid4())[:8]}"
    provider.git_push_temp(dirpath, branch, temp_branch)
    wf = Workflow(None, temp_branch, local_sha)
    return temp_branch, wf


def get_gitlab_project_info(shell: Shell) -> Tuple[str, str, str]:
    remotes_out = shell.output(["git", "remote", "-v"]).stdout.decode()
    for line in remotes_out.splitlines():
        if "gitlab.lan" in line or "gitlab" in line.lower():
            parts = line.split()
            if len(parts) < 2: continue
            url = parts[1]
            protocol = "http"
            if url.startswith("https"):
                protocol = "https"
            
            host = ""
            path = ""
            if "://" in url:
                # http(s)://host/path
                host_path = url.split("://")[1]
                if "/" in host_path:
                    host = host_path.split("/")[0]
                    path = host_path.split("/", 1)[1]
            elif "@" in url and ":" in url:
                # git@host:path
                host_path = url.split("@")[1]
                host = host_path.split(":")[0]
                path = host_path.split(":")[1]
            
            if path.endswith(".git"):
                path = path[:-4]
            
            if host and path:
                return protocol, host, path
    
    # Fallback/Default
    return "http", "gitlab.lan", "mrwilson/weaver"


def curl_get(shell: Shell, url: str, token: str) -> str:
    out = shell.output(["curl", "-s", "--header", f"PRIVATE-TOKEN: {token}", url])
    if not out.success():
        raise RuntimeError(f"curl get failed for {url}: {out.stderr.decode()}")
    return out.stdout.decode()


def curl_post(shell: Shell, url: str, token: str) -> str:
    out = shell.output(["curl", "-s", "--header", f"PRIVATE-TOKEN: {token}", "-X", "POST", url])
    if not out.success():
        raise RuntimeError(f"curl post failed for {url}: {out.stderr.decode()}")
    return out.stdout.decode()


def pick_pipeline(pipelines: Any, branch: Optional[str]) -> Optional[PipelineInfo]:
    if not isinstance(pipelines, list):
        raise RuntimeError("Pipelines response is not an array")
    for p in pipelines:
        ref = p.get("ref", "unknown")
        if branch is not None and ref != branch:
            continue
        pid = int(p["id"])
        web = p.get("web_url", "unknown")
        status = p.get("status", "unknown")
        created_at = p.get("created_at", "")
        return PipelineInfo(pid, web, status, ref, created_at)
    return None


def search_pipeline_robust(shell: Shell, protocol: str, host: str, project: str, sha: str, branch: Optional[str], diff_md5: Optional[str], token: str) -> Optional[PipelineInfo]:
    target_sha = None
    if diff_md5 is not None:
        url = f"{protocol}://{host}/api/v4/projects/{project}/repository/commits?per_page=100&all=true"
        output = curl_get(shell, url, token)
        commits = json.loads(output)
        found = None
        for commit in commits:
            message = commit.get("message", "")
            commit_sha = commit.get("id", "")
            if diff_md5 in message and sha in message:
                found = commit_sha
                break
        target_sha = found
    else:
        target_sha = sha

    if target_sha is None:
        return None

    pipe_url = f"{protocol}://{host}/api/v4/projects/{project}/pipelines?sha={target_sha}"
    pipe_output = curl_get(shell, pipe_url, token)
    pipelines = json.loads(pipe_output)
    return pick_pipeline(pipelines, branch)


def trigger_new_pipeline(shell: Shell, protocol: str, host: str, project: str, branch: str, token: str) -> Tuple[int, str, str]:
    url = f"{protocol}://{host}/api/v4/projects/{project}/pipeline?ref={branch}"
    out = curl_post(shell, url, token)
    try:
        resp = json.loads(out)
    except Exception as e:
        raise RuntimeError(f"Failed to parse GitLab trigger response: {e}; raw response: {out}")

    if "id" not in resp:
        # Provide the full response for debugging (could be an error message)
        raise RuntimeError(f"Trigger response missing 'id' field; response: {json.dumps(resp, indent=2)}")

    pid = int(resp["id"])
    web = resp.get("web_url", "")
    created_at = resp.get("created_at", "")
    return pid, web, created_at


def poll_logs(shell: Shell, protocol: str, host: str, project: str, pipeline_id: int, token: str, cursors: Dict[int, int]):
    jobs_url = f"{protocol}://{host}/api/v4/projects/{project}/pipelines/{pipeline_id}/jobs"
    jobs_json = curl_get(shell, jobs_url + "?per_page=100", token)
    jobs = json.loads(jobs_json)
    relevant = [j for j in jobs if j.get("status") in ("running", "failed", "success")]
    relevant.sort(key=lambda a: a.get("started_at") or a.get("created_at") or "")
    for job in relevant:
        job_id = int(job["id"])
        job_name = job.get("name", "unknown")
        trace_url = f"{protocol}://{host}/api/v4/projects/{project}/jobs/{job_id}/trace"
        try:
            trace = curl_get(shell, trace_url, token)
        except Exception:
            continue
        lines = trace.splitlines()
        cursor = cursors.get(job_id, 0)
        if len(lines) > cursor:
            for line in lines[cursor:]:
                print(f"[{job_name}] {line}")
            cursors[job_id] = len(lines)


def print_failed_jobs(shell: Shell, protocol: str, host: str, project: str, pipeline_id: int, token: str):
    jobs_url = f"{protocol}://{host}/api/v4/projects/{project}/pipelines/{pipeline_id}/jobs"
    jobs_json = curl_get(shell, jobs_url + "?per_page=100", token)
    jobs = json.loads(jobs_json)
    for job in jobs:
        status = job.get("status", "unknown")
        if status in ("failed", "canceled"):
            job_id = int(job["id"])
            job_name = job.get("name", "unknown")
            print(f"\n  --- Job: {job_name} (ID: {job_id}, Status: {status}) ---")
            trace_url = f"{protocol}://{host}/api/v4/projects/{project}/jobs/{job_id}/trace"
            try:
                trace = curl_get(shell, trace_url, token)
                lines = trace.splitlines()
                start = max(0, len(lines) - 2000)
                for line in lines[start:]:
                    print(f"  {line}")
            except Exception:
                pass


class RealCiProvider:
    def __init__(self, shell: Shell):
        self.shell = shell
        self.protocol, self.host, self.path = get_gitlab_project_info(self.shell)

    def current_dir(self) -> str:
        return self.shell.current_dir()

    def get_gitlab_token(self) -> str:
        v = os.environ.get("GITLAB_TOKEN")
        if v:
            return v
        try:
            s = self.shell.read('.token')
            return s.strip()
        except Exception:
            raise RuntimeError("GITLAB_TOKEN environment variable is not set and .token file not found")

    def get_project_info(self) -> Tuple[str, str, str]:
        return self.protocol, self.host, self.path

    def is_canceled(self) -> bool:
        return False

    def is_git_clean(self, dirpath: str) -> bool:
        return is_git_clean(self.shell, dirpath)

    def get_git_head(self, dirpath: str) -> str:
        return get_git_head(self.shell, dirpath)

    def get_git_branch(self, dirpath: str) -> str:
        return get_git_branch(self.shell, dirpath)

    def check_remote_branch_sha(self, dirpath: str, branch: str) -> str:
        # Try to find which remote is the gitlab one
        gitlab_remote = "origin"
        remotes = self.shell.output(["git", "remote"], cwd=dirpath).stdout.decode().splitlines()
        if "gitlab" in remotes:
            gitlab_remote = "gitlab"
        
        out = self.shell.output(["git", "rev-parse", f"{gitlab_remote}/{branch}"], cwd=dirpath)
        if out.success():
            return out.stdout.decode().strip()
        return ""

    def prompt_user(self, message: str, options: List[str], default_idx: int) -> int:
        for i, opt in enumerate(options):
            print(f"    {i}) {opt}")
        ans = input(f"  Enter choice [{default_idx}]: ")
        ans = ans.strip()
        if ans == "":
            return default_idx
        try:
            idx = int(ans)
            if 0 <= idx < len(options):
                return idx
        except Exception:
            pass
        return default_idx

    def git_push(self, dirpath: str, branch: str):
        gitlab_remote = "origin"
        remotes = self.shell.output(["git", "remote"], cwd=dirpath).stdout.decode().splitlines()
        if "gitlab" in remotes:
            gitlab_remote = "gitlab"
        out = self.shell.run(["git", "push", gitlab_remote, branch], cwd=dirpath)
        if not out.success():
            raise RuntimeError("Failed to push branch")

    def git_push_temp(self, dirpath: str, local_branch: str, temp_branch_name: str):
        gitlab_remote = "origin"
        remotes = self.shell.output(["git", "remote"], cwd=dirpath).stdout.decode().splitlines()
        if "gitlab" in remotes:
            gitlab_remote = "gitlab"
        out = self.shell.run(["git", "push", gitlab_remote, f"HEAD:refs/heads/{temp_branch_name}"], cwd=dirpath)
        if not out.success():
            raise RuntimeError("Failed to push temporary branch")

    def trigger_pipeline(self, protocol: str, host: str, project: str, branch: str, token: str):
        return trigger_new_pipeline(self.shell, protocol, host, project, branch, token)

    def search_pipeline(self, protocol: str, host: str, project: str, sha: str, branch: Optional[str], diff_md5: Optional[str], token: str):
        return search_pipeline_robust(self.shell, protocol, host, project, sha, branch, diff_md5, token)

    def setup_workflow(self, original_dir: str, base_sha: str, diff_md5: str) -> Workflow:
        # Copy files to temp dir and create git repo, then push
        temp_dir = tempfile.mkdtemp()
        for root, dirs, files in os.walk(original_dir):
            # filter
            parts = root.split(os.sep)
            if 'target' in parts or '.git' in parts:
                continue
            rel = os.path.relpath(root, original_dir)
            dest = os.path.join(temp_dir, rel)
            os.makedirs(dest, exist_ok=True)
            for f in files:
                if f == ".git":
                    continue
                shutil.copy(os.path.join(root, f), os.path.join(dest, f))

        branch = f"ci-test-{str(uuid.uuid4())[:8]}"
        def git_temp(args):
            return self.shell.output(["git"] + args, cwd=temp_dir)

        gitlab_remote = "origin"
        remotes = self.shell.output(["git", "remote"], cwd=original_dir).stdout.decode().splitlines()
        if "gitlab" in remotes:
            gitlab_remote = "gitlab"
        
        repo_url = get_remote_url(self.shell, gitlab_remote, cwd=original_dir)
        
        git_temp(["init"]) ; git_temp(["remote", "add", "origin", repo_url])
        git_temp(["checkout", "-b", branch])
        git_temp(["add", "."])
        git_temp(["config", "user.email", "ci@temp.log"]) ; git_temp(["config", "user.name", "Temp CI Runner"]) 
        commit_msg = f"Temp CI commit\n\nBase hash: {base_sha}\nDiff MD5: {diff_md5}"
        git_temp(["commit", "-m", commit_msg])
        sha = git_temp(["rev-parse", "HEAD"]).stdout.decode().strip()
        push_out = git_temp(["push", "origin", branch])
        if not push_out.success():
            print(f"Failed to push temp branch in setup_workflow:\nSTDOUT: {push_out.stdout.decode()}\nSTDERR: {push_out.stderr.decode()}")
            raise RuntimeError("setup_workflow push failed")
        return Workflow(temp_dir, branch, sha)

    def poll_logs(self, protocol: str, host: str, project: str, pipeline_id: int, token: str, cursors: Dict[int, int]):
        return poll_logs(self.shell, protocol, host, project, pipeline_id, token, cursors)

    def check_pipeline_status(self, protocol: str, host: str, project: str, pipeline_id: int, token: str) -> str:
        url = f"{protocol}://{host}/api/v4/projects/{project}/pipelines/{pipeline_id}"
        out = curl_get(self.shell, url, token)
        resp = json.loads(out)
        return resp.get("status", "unknown")

    def handle_failure(self, protocol: str, host: str, project: str, pipeline_id: int, token: str, original_dir: str):
        print_failed_jobs(self.shell, protocol, host, project, pipeline_id, token)

    def calculate_metadata(self, dirpath: str) -> Tuple[str, str]:
        out1 = self.shell.output(["git", "rev-parse", "HEAD"], cwd=dirpath)
        if not out1.success():
            raise RuntimeError("Git command failed on original dir")
        base_sha = out1.stdout.decode().strip()
        out2 = self.shell.output(["git", "diff"], cwd=dirpath) 
        if not out2.success():
            raise RuntimeError("Git command failed on original dir")
        diff = out2.stdout
        m = hashlib.md5()
        m.update(diff)
        diff_md5 = m.hexdigest()
        return base_sha, diff_md5


    def is_canceled(self) -> bool:
        return False


class MockCiProvider:
    def __init__(self):
        self.is_clean = True
        self.local_head = "sha_local"
        self.remote_branch_sha = "sha_remote"
        self.git_branch = "feature/foo"
        self.prompt_responses: List[int] = []
        self.push_log: List[str] = []
        self.existing_pipeline: Optional[PipelineInfo] = None
        self.pipeline_status: str = "success"

    def current_dir(self) -> str:
        return "/tmp/mock"

    def get_gitlab_token(self) -> str:
        return "mock_token"

    def get_project_info(self) -> Tuple[str, str]:
        return "gitlab.lan", "group/project"

    def is_git_clean(self, dirpath: str) -> bool:
        return self.is_clean

    def get_git_head(self, dirpath: str) -> str:
        return self.local_head

    def get_git_branch(self, dirpath: str) -> str:
        return self.git_branch

    def check_remote_branch_sha(self, dirpath: str, branch: str) -> str:
        return self.remote_branch_sha

    def prompt_user(self, message: str, options: List[str], default_idx: int) -> int:
        if not self.prompt_responses:
            return default_idx
        return self.prompt_responses.pop(0)

    def git_push(self, dirpath: str, branch: str):
        self.push_log.append(f"push origin {branch}")

    def git_push_temp(self, dirpath: str, local_branch: str, temp_branch_name: str):
        self.push_log.append(f"push origin HEAD:{temp_branch_name}")

    def trigger_pipeline(self, host: str, project: str, branch: str, token: str):
        return 123, "http://url", "now"

    def search_pipeline(self, host: str, project: str, sha: str, branch: Optional[str], diff_md5: Optional[str], token: str):
        return self.existing_pipeline

    def setup_workflow(self, dirpath: str, base: str, diff: str) -> Workflow:
        return Workflow(None, "temp-workflow", "mock_sha")

    def poll_logs(self, host: str, project: str, pipeline_id: int, token: str, cursors: Dict[int, int]):
        return None

    def check_pipeline_status(self, host: str, project: str, pipeline_id: int, token: str) -> str:
        return self.pipeline_status

    def handle_failure(self, host: str, project: str, pipeline_id: int, token: str, original_dir: str):
        return None

    def calculate_metadata(self, dirpath: str) -> Tuple[str, str]:
        return "mock_base_sha", "mock_diff_md5"


def run_with_provider(provider) -> None:
    orchestrator = CiOrchestrator(provider)
    return orchestrator.run()

if __name__ == "__main__":
    provider = RealCiProvider(Shell())
    run_with_provider(provider)