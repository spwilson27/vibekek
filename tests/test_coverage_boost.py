"""Additional tests to boost workflow_lib coverage to >=90%."""
import sys
import os
import json
import pytest
import threading
import subprocess
from unittest.mock import patch, MagicMock, mock_open, call, ANY

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Grab real write_ignore_file BEFORE conftest autouse fixture mocks it
from workflow_lib.runners import AIRunner as _AIRunnerReal
_real_write_ignore_file = _AIRunnerReal.write_ignore_file

import workflow
from workflow import (
    AIRunner, GeminiRunner, ClaudeRunner, CopilotRunner,
    ProjectContext, BasePhase,
    Phase1GenerateDoc, Phase2FleshOutDoc,
    Phase3FinalReview, Phase3BAdversarialReview,
    Phase4AExtractRequirements, Phase4BMergeRequirements,
    Phase4BScopeGate, Phase4COrderRequirements,
    Phase5GenerateEpics, Phase5BSharedComponents,
    Phase6BreakDownTasks, Phase6BReviewTasks,
    Phase6CCrossPhaseReview, Phase6DReorderTasks,
    Phase7ADAGGeneration,
    Logger, run_ai_command,
    get_task_details, get_memory_context, get_project_context,
    run_agent, rebuild_serena_cache, get_existing_worktree,
    process_task, merge_task, execute_dag,
    load_blocked_tasks, get_ready_tasks,
    load_replan_state, save_replan_state,
    load_workflow_state, save_workflow_state,
    log_action, load_dags,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_ctx(state=None):
    ctx = MagicMock(spec=ProjectContext)
    ctx.state = state or {}
    ctx.root_dir = "/fake/root"
    ctx.plan_dir = "/fake/root/docs/plan"
    ctx.requirements_dir = "/fake/root/docs/plan/requirements"
    ctx.description_ctx = "Project description"
    ctx.run_gemini.return_value = MagicMock(returncode=0, stdout="", stderr="")
    ctx.run_ai.return_value = MagicMock(returncode=0, stdout="", stderr="")
    ctx.load_prompt.return_value = "prompt template {description_ctx}"
    ctx.format_prompt.return_value = "formatted prompt"
    ctx.get_document_path.return_value = "/fake/root/docs/plan/specs/doc.md"
    ctx.get_target_path.return_value = "docs/plan/specs/doc.md"
    ctx.get_accumulated_context.return_value = ""
    ctx.parse_markdown_headers.return_value = []
    ctx.load_shared_components.return_value = ""
    return ctx


# ---------------------------------------------------------------------------
# executor.py – Logger
# ---------------------------------------------------------------------------

class TestLogger:
    def test_write_with_newline_in_middle(self):
        terminal = MagicMock()
        log_stream = MagicMock()
        lock = threading.Lock()
        logger = Logger(terminal, log_stream, lock)
        logger.write("line1\nline2\n")
        assert terminal.write.called

    def test_write_blank_newline(self):
        terminal = MagicMock()
        log_stream = MagicMock()
        lock = threading.Lock()
        logger = Logger(terminal, log_stream, lock)
        logger.write("\n")
        assert terminal.write.called

    def test_flush(self):
        terminal = MagicMock()
        log_stream = MagicMock()
        lock = threading.Lock()
        logger = Logger(terminal, log_stream, lock)
        logger.flush()
        terminal.flush.assert_called_once()


# ---------------------------------------------------------------------------
# executor.py – run_ai_command
# ---------------------------------------------------------------------------

class TestRunAiCommand:
    def _mock_proc(self, returncode=0, lines=None):
        proc = MagicMock()
        proc.returncode = returncode
        mock_stdout = MagicMock()
        mock_stdout.readline.side_effect = (lines or ["output line\n"]) + [""]
        proc.stdout = mock_stdout
        proc.wait.return_value = None
        proc.stdin = MagicMock()
        return proc

    def test_claude_backend(self):
        proc = self._mock_proc()
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc):
            rc = run_ai_command("prompt", "/tmp", backend="claude")
        assert rc == 0

    def test_copilot_backend(self):
        proc = self._mock_proc()
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc), \
             patch("tempfile.mkstemp", return_value=(0, "/tmp/mock.txt")), \
             patch("os.fdopen", return_value=MagicMock().__enter__.return_value), \
             patch("os.remove"):
            rc = run_ai_command("prompt", "/tmp", backend="copilot")
        assert rc == 0

    def test_gemini_backend(self):
        proc = self._mock_proc(returncode=1)
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc):
            rc = run_ai_command("prompt", "/tmp", backend="gemini")
        assert rc == 1

    def test_write_input_exception(self):
        """stdin.write raises — should not propagate."""
        proc = self._mock_proc()
        proc.stdin.write.side_effect = Exception("pipe broken")
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc):
            rc = run_ai_command("prompt", "/tmp")
        assert rc == 0

    def test_copilot_cleanup_on_oserror(self):
        """tmp file cleanup handles OSError silently."""
        proc = self._mock_proc()
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc), \
             patch("tempfile.mkstemp", return_value=(0, "/tmp/mock.txt")), \
             patch("os.fdopen", return_value=MagicMock().__enter__.return_value), \
             patch("os.remove", side_effect=OSError):
            rc = run_ai_command("prompt", "/tmp", backend="copilot")
        assert rc == 0


# ---------------------------------------------------------------------------
# executor.py – helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_get_task_details_file(self):
        with patch("os.path.isfile", return_value=True), \
             patch("os.path.isdir", return_value=False), \
             patch("builtins.open", mock_open(read_data="# Task content")):
            result = get_task_details("phase_1/task.md")
        assert "Task content" in result

    def test_get_task_details_directory(self):
        with patch("os.path.isfile", return_value=False), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["task.md"]), \
             patch("builtins.open", mock_open(read_data="# Task dir content")):
            result = get_task_details("phase_1/sub")
        assert "Task dir content" in result

    def test_get_task_details_not_found(self):
        with patch("os.path.isfile", return_value=False), \
             patch("os.path.isdir", return_value=False):
            result = get_task_details("phase_1/missing")
        assert result == ""

    def test_get_memory_context_exists(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="memory content")):
            result = get_memory_context("/fake/root")
        assert result == "memory content"

    def test_get_memory_context_missing(self):
        with patch("os.path.exists", return_value=False):
            result = get_memory_context("/fake/root")
        assert result == ""

    def test_get_project_context_exists(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="project desc")):
            result = get_project_context("/fake/tools")
        assert result == "project desc"

    def test_get_project_context_missing(self):
        with patch("os.path.exists", return_value=False):
            result = get_project_context("/fake/tools")
        assert result == ""


# ---------------------------------------------------------------------------
# executor.py – run_agent
# ---------------------------------------------------------------------------

class TestRunAgent:
    def test_success(self):
        with patch("builtins.open", mock_open(read_data="Hello {task_name}")), \
             patch("workflow_lib.executor.run_ai_command", return_value=0):
            result = run_agent("Impl", "implement_task.md", {"task_name": "my_task"}, "/tmp")
        assert result is True

    def test_failure(self):
        with patch("builtins.open", mock_open(read_data="template")), \
             patch("workflow_lib.executor.run_ai_command", return_value=1):
            result = run_agent("Impl", "implement_task.md", {}, "/tmp")
        assert result is False


# ---------------------------------------------------------------------------
# executor.py – rebuild_serena_cache
# ---------------------------------------------------------------------------

class TestRebuildSerenaCache:
    def test_timeout_then_missing_cache(self):
        """TimeoutExpired → terminate, cache missing → warning + return."""
        proc = MagicMock()
        # First call (with timeout) raises, second call (no timeout) returns normally
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 120), None]
        lock = threading.Lock()
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc), \
             patch("os.path.isdir", return_value=False):
            rebuild_serena_cache("/src", "/root", lock)
        proc.terminate.assert_called_once()

    def test_success_copy(self):
        proc = MagicMock()
        proc.wait.return_value = None
        lock = threading.Lock()
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc), \
             patch("os.path.isdir", return_value=True), \
             patch("shutil.rmtree"), \
             patch("shutil.copytree"), \
             patch("os.rename"):
            rebuild_serena_cache("/src", "/root", lock)


# ---------------------------------------------------------------------------
# executor.py – get_existing_worktree
# ---------------------------------------------------------------------------

class TestGetExistingWorktree:
    def test_found(self):
        output = "worktree /tmp/wt\nbranch refs/heads/ai-phase-task\n"
        with patch("subprocess.run") as mock_run, \
             patch("os.path.isdir", return_value=True):
            mock_run.return_value = MagicMock(stdout=output)
            result = get_existing_worktree("/root", "ai-phase-task")
        assert result == "/tmp/wt"

    def test_stale_prune(self):
        """Branch found but directory missing → prune and return None."""
        output = "worktree /nonexistent/path\nbranch refs/heads/ai-phase-task\n"
        with patch("subprocess.run") as mock_run, \
             patch("os.path.isdir", return_value=False):
            mock_run.return_value = MagicMock(stdout=output)
            result = get_existing_worktree("/root", "ai-phase-task")
        assert result is None

    def test_error(self):
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            result = get_existing_worktree("/root", "branch")
        assert result is None


# ---------------------------------------------------------------------------
# executor.py – load_blocked_tasks
# ---------------------------------------------------------------------------

class TestLoadBlockedTasks:
    def test_missing_file(self):
        with patch("os.path.exists", return_value=False):
            result = load_blocked_tasks()
        assert result == set()

    def test_bad_json(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="not json")):
            result = load_blocked_tasks()
        assert result == set()

    def test_with_blocked(self):
        data = json.dumps({"blocked_tasks": {"phase_1/task.md": {"reason": "x"}}})
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=data)):
            result = load_blocked_tasks()
        assert "phase_1/task.md" in result


# ---------------------------------------------------------------------------
# executor.py – process_task
# ---------------------------------------------------------------------------

class TestProcessTask:
    def _base_patches(self):
        return {
            "workflow_lib.executor.get_existing_worktree": None,
            "workflow_lib.executor.run_agent": True,
            "workflow_lib.executor.get_task_details": "# Task: My Task",
            "workflow_lib.executor.get_project_context": "desc",
            "workflow_lib.executor.get_memory_context": "mem",
        }

    def test_worktree_creation_fails(self):
        err = subprocess.CalledProcessError(1, "git")
        err.stderr = b"error"
        with patch("workflow_lib.executor.get_existing_worktree", return_value=None), \
             patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", side_effect=err):
            result = process_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is False

    def test_success_with_new_worktree(self):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="M file.py", stderr=""))
        with patch("workflow_lib.executor.get_existing_worktree", return_value=None), \
             patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", mock_run), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: My Task"), \
             patch("workflow_lib.executor.get_project_context", return_value="desc"), \
             patch("workflow_lib.executor.get_memory_context", return_value="mem"), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False):
            result = process_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is True

    def test_existing_worktree_reset_fails(self):
        err = subprocess.CalledProcessError(1, "git")
        err.stderr = b"reset error"
        with patch("workflow_lib.executor.get_existing_worktree", return_value="/tmp/existing"), \
             patch("subprocess.run", side_effect=err):
            result = process_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is False

    def test_implementation_agent_fails(self):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch("workflow_lib.executor.get_existing_worktree", return_value=None), \
             patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", mock_run), \
             patch("workflow_lib.executor.run_agent", return_value=False), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False):
            result = process_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is False

    def test_presubmit_fail_then_retry_success(self):
        presubmit_calls = [0]
        def mock_run_side_effect(*args, **kwargs):
            cmd = args[0] if args else []
            if isinstance(cmd, list) and cmd[0] == "./do":
                presubmit_calls[0] += 1
                if presubmit_calls[0] == 1:
                    return MagicMock(returncode=1, stdout="fail", stderr="")
                return MagicMock(returncode=0, stdout="M file.py", stderr="")
            return MagicMock(returncode=0, stdout="M file.py", stderr="")

        with patch("workflow_lib.executor.get_existing_worktree", return_value=None), \
             patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", side_effect=mock_run_side_effect), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False):
            result = process_task("/root", "phase_1/task.md", "./do presubmit", max_retries=2)
        assert result is True

    def test_serena_seeding(self):
        """With serena=True: cache is copied and .mcp.json is copied."""
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="M f", stderr=""))
        with patch("workflow_lib.executor.get_existing_worktree", return_value=None), \
             patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", mock_run), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", side_effect=lambda p: ".serena" in p and "/tmp/wt" not in p), \
             patch("os.path.exists", side_effect=lambda p: ".mcp.json" in p and "/tmp/wt" not in p), \
             patch("workflow_lib.executor.shutil.copytree") as mock_copytree, \
             patch("workflow_lib.executor.shutil.copy2") as mock_copy2:
            result = process_task("/root", "phase_1/task.md", "./do presubmit", serena=True)
        mock_copytree.assert_called_once()
        mock_copy2.assert_called_once()


# ---------------------------------------------------------------------------
# executor.py – merge_task
# ---------------------------------------------------------------------------

class TestMergeTask:
    def _ok_run(self):
        return MagicMock(returncode=0, stdout="", stderr="")

    def test_squash_merge_success(self):
        with patch("subprocess.run", return_value=self._ok_run()), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("shutil.rmtree"):
            result = merge_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is True

    def test_squash_fails_rebase_succeeds(self):
        """First squash fails (conflict), rebase succeeds, second squash succeeds."""
        call_count = [0]
        def side_effect(*args, **kwargs):
            cmd = args[0] if args else []
            if isinstance(cmd, list):
                call_count[0] += 1
                if "merge" in cmd and "--squash" in cmd and call_count[0] == 1:
                    return MagicMock(returncode=1, stdout="conflict", stderr="conflict")
                if "rebase" in cmd and "--abort" not in cmd:
                    return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="M f", stderr="")

        with patch("subprocess.run", side_effect=side_effect), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("shutil.rmtree"):
            result = merge_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is True

    def test_all_attempts_fail(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="")), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("shutil.rmtree"):
            result = merge_task("/root", "phase_1/task.md", "./do presubmit", max_retries=2)
        assert result is False

    def test_serena_rebuild_on_success(self):
        """With serena=True and cache_lock, rebuild_serena_cache is called after push."""
        with patch("subprocess.run", return_value=self._ok_run()), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.rebuild_serena_cache") as mock_rebuild, \
             patch("shutil.rmtree"):
            lock = threading.Lock()
            result = merge_task("/root", "phase_1/task.md", "./do presubmit",
                                cache_lock=lock, serena=True)
        assert result is True
        mock_rebuild.assert_called_once()


# ---------------------------------------------------------------------------
# executor.py – execute_dag
# ---------------------------------------------------------------------------

class TestExecuteDag:
    def test_empty_dag(self):
        state = {"completed_tasks": [], "merged_tasks": []}
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"):
            execute_dag("/root", {}, state, 1, "./do presubmit")

    def test_all_tasks_completed(self):
        dag = {"phase_1/task.md": []}
        state = {"completed_tasks": ["phase_1/task.md"], "merged_tasks": ["phase_1/task.md"]}
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"):
            execute_dag("/root", dag, state, 1, "./do presubmit")

    def test_dev_branch_created(self):
        """When dev branch check fails, create it from main."""
        run_results = [
            MagicMock(returncode=1),  # rev-parse fails
            MagicMock(returncode=0),  # branch creation
        ]
        state = {"completed_tasks": [], "merged_tasks": []}
        with patch("subprocess.run", side_effect=run_results + [MagicMock(returncode=0)] * 100), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.get_ready_tasks", return_value=[]), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"):
            execute_dag("/root", {}, state, 1, "./do presubmit")

    def test_serena_bootstrap_mcp_copy(self):
        """With serena=True and no .mcp.json, template is copied."""
        state = {"completed_tasks": [], "merged_tasks": []}
        def exists_side(path):
            if ".serena/cache" in path:
                return True  # cache exists, no bootstrap needed
            if ".mcp.json" in path and "/root" in path:
                return False  # no mcp.json at root
            if "templates/.mcp.json" in path:
                return True
            return True

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=True), \
             patch("os.path.exists", side_effect=exists_side), \
             patch("os.path.isdir", return_value=True), \
             patch("workflow_lib.executor.shutil.copy2") as mock_copy, \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"):
            execute_dag("/root", {}, state, 1, "./do presubmit")
        mock_copy.assert_called_once()

    def test_task_success_end_to_end(self):
        dag = {"phase_1/task.md": []}
        state = {"completed_tasks": [], "merged_tasks": []}
        with patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("workflow_lib.executor.process_task", return_value=True), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"):
            execute_dag("/root", dag, state, 1, "./do presubmit")
        assert "phase_1/task.md" in state["completed_tasks"]

    def test_task_failure_exits(self):
        dag = {"phase_1/task.md": []}
        state = {"completed_tasks": [], "merged_tasks": []}
        ready_calls = [0]
        def ready_side(master_dag, completed, active):
            ready_calls[0] += 1
            if ready_calls[0] == 1:
                return ["phase_1/task.md"]
            return []

        with patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("workflow_lib.executor.process_task", return_value=False), \
             patch("workflow_lib.executor.get_ready_tasks", side_effect=ready_side), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             pytest.raises(SystemExit):
            execute_dag("/root", dag, state, 1, "./do presubmit")


# ---------------------------------------------------------------------------
# state.py – edge cases
# ---------------------------------------------------------------------------

class TestStateCoverage:
    def test_load_replan_state_json_error(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="not valid json")):
            state = load_replan_state()
        assert "blocked_tasks" in state

    def test_load_workflow_state_json_error(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="not valid json")):
            state = load_workflow_state()
        assert "completed_tasks" in state

    def test_log_action(self):
        state = {}
        log_action(state, "block", "phase_1/task.md", "reason")
        assert len(state["replan_history"]) == 1
        assert state["replan_history"][0]["action"] == "block"

    def test_load_dags_dag_reviewed_fallback(self):
        """load_dags uses dag_reviewed.json when present."""
        dag_data = json.dumps({"01_task.md": []})
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", return_value=True), \
             patch("builtins.open", mock_open(read_data=dag_data)):
            result = load_dags("/fake/tasks")
        assert any("phase_1" in k for k in result.keys())

    def test_load_dags_dag_json_decode_error(self):
        with patch("os.path.exists", side_effect=lambda p: True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", return_value=True), \
             patch("builtins.open", mock_open(read_data="invalid json")):
            result = load_dags("/fake/tasks")
        assert result == {}


# ---------------------------------------------------------------------------
# runners.py – write_ignore_file and CopilotRunner
# ---------------------------------------------------------------------------

class TestRunners:
    def test_write_ignore_file_same_content(self):
        """No write happens when content matches existing file."""
        runner = GeminiRunner()
        with patch.object(AIRunner, "write_ignore_file", new=_real_write_ignore_file), \
             patch("os.path.exists", return_value=True), \
             patch("workflow_lib.runners.ignore_file_lock", threading.Lock()), \
             patch("builtins.open", mock_open(read_data="content")) as m:
            runner.write_ignore_file("/fake/.geminiignore", "content")
        calls = [str(c) for c in m.call_args_list]
        assert not any("'w'" in c for c in calls)

    def test_write_ignore_file_different_content(self):
        """File is written when content differs."""
        runner = GeminiRunner()
        with patch.object(AIRunner, "write_ignore_file", new=_real_write_ignore_file), \
             patch("os.path.exists", return_value=True), \
             patch("workflow_lib.runners.ignore_file_lock", threading.Lock()), \
             patch("builtins.open", mock_open(read_data="old content")) as m:
            runner.write_ignore_file("/fake/.geminiignore", "new content")
        calls = [str(c) for c in m.call_args_list]
        assert any("'w'" in c for c in calls)

    def test_write_ignore_file_not_exists(self):
        """File is created when it doesn't exist."""
        runner = ClaudeRunner()
        with patch.object(AIRunner, "write_ignore_file", new=_real_write_ignore_file), \
             patch("os.path.exists", return_value=False), \
             patch("workflow_lib.runners.ignore_file_lock", threading.Lock()), \
             patch("builtins.open", mock_open()) as m:
            runner.write_ignore_file("/fake/.claudeignore", "content")
        calls = [str(c) for c in m.call_args_list]
        assert any("'w'" in c for c in calls)

    def test_copilot_runner_success(self):
        runner = CopilotRunner()
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result), \
             patch("workflow_lib.runners.ignore_file_lock", threading.Lock()), \
             patch("builtins.open", mock_open(read_data="")):
            result = runner.run("/tmp", "prompt", "", "/fake/.copilotignore")
        assert result.returncode == 0

    def test_copilot_runner_file_not_found_then_raises(self):
        """All candidates raise FileNotFoundError → re-raise."""
        runner = CopilotRunner()
        with patch("subprocess.run", side_effect=FileNotFoundError("not found")), \
             patch("workflow_lib.runners.ignore_file_lock", threading.Lock()), \
             patch("builtins.open", mock_open(read_data="")):
            with pytest.raises(FileNotFoundError):
                runner.run("/tmp", "prompt", "", "/fake/.copilotignore")

    def test_copilot_runner_non_zero_returned(self):
        """All candidates run but return nonzero → return last result."""
        runner = CopilotRunner()
        mock_result = MagicMock(returncode=1)
        with patch("subprocess.run", return_value=mock_result), \
             patch("workflow_lib.runners.ignore_file_lock", threading.Lock()), \
             patch("builtins.open", mock_open(read_data="")):
            result = runner.run("/tmp", "prompt", "", "/fake/.copilotignore")
        assert result.returncode == 1

    def test_ignore_file_name_properties(self):
        assert GeminiRunner().ignore_file_name == ".geminiignore"
        assert ClaudeRunner().ignore_file_name == ".claudeignore"
        assert CopilotRunner().ignore_file_name == ".copilotignore"


# ---------------------------------------------------------------------------
# context.py – uncovered paths
# ---------------------------------------------------------------------------

class TestContextCoverage:
    def _make_ctx(self, **kwargs):
        """Real ProjectContext with mocked filesystem."""
        with patch("os.makedirs"), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="description")), \
             patch("workflow_lib.context.GeminiRunner"):
            ctx = ProjectContext.__new__(ProjectContext)
            ctx.root_dir = "/fake/root"
            ctx.plan_dir = "/fake/root/docs/plan"
            ctx.specs_dir = "/fake/root/docs/plan/specs"
            ctx.research_dir = "/fake/root/docs/plan/research"
            ctx.requirements_dir = "/fake/root/docs/plan/requirements"
            ctx.sandbox_dir = "/fake/root/.sandbox"
            ctx.prompts_dir = "/fake/.tools/prompts"
            ctx.state_file = "/fake/.tools/.gen_state.json"
            ctx.desc_file = "/fake/.tools/input/project-description.md"
            ctx.shared_components_file = "/fake/root/docs/plan/shared_components.md"
            ctx.ignore_file = "/fake/root/.geminiignore"
            ctx.backup_ignore = "/fake/root/.geminiignore.bak"
            ctx.has_existing_ignore = kwargs.get("has_ignore", False)
            ctx.state = kwargs.get("state", {})
            ctx.description_ctx = "project desc"
            ctx.runner = MagicMock()
            return ctx

    def test_load_description_missing(self):
        with patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            ProjectContext._load_description(self._make_ctx())

    def test_load_prompt_missing(self):
        ctx = self._make_ctx()
        with patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            ctx.load_prompt("nonexistent.md")

    def test_load_prompt_success(self):
        ctx = self._make_ctx()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="template content")):
            result = ctx.load_prompt("test.md")
        assert result == "template content"

    def test_backup_ignore_file_when_exists(self):
        ctx = self._make_ctx(has_ignore=True)
        with patch("shutil.copy") as mock_copy:
            ctx.backup_ignore_file()
        mock_copy.assert_called_once_with(ctx.ignore_file, ctx.backup_ignore)

    def test_restore_ignore_file_when_exists_and_backup_present(self):
        ctx = self._make_ctx(has_ignore=True)
        with patch("os.path.exists", return_value=True), \
             patch("shutil.move") as mock_move:
            ctx.restore_ignore_file()
        mock_move.assert_called_once()

    def test_restore_ignore_file_no_original_but_ignore_exists(self):
        ctx = self._make_ctx(has_ignore=False)
        with patch("os.path.exists", return_value=True), \
             patch("os.remove") as mock_remove:
            ctx.restore_ignore_file()
        mock_remove.assert_called_once_with(ctx.ignore_file)

    def test_get_accumulated_context_skip_research(self):
        ctx = self._make_ctx()
        doc = {"id": "arch", "type": "spec", "name": "Arch"}
        with patch("workflow_lib.context.DOCS", [
            {"id": "market", "type": "research"},
            doc,
        ]), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="content")):
            result = ctx.get_accumulated_context(doc, include_research=False)
        # research doc should be skipped
        assert "market" not in result or result == ""

    def test_get_workspace_snapshot_oserror(self):
        ctx = self._make_ctx()
        with patch("os.walk", return_value=[("/root", [], ["file.py"])]), \
             patch("os.path.getmtime", side_effect=OSError):
            result = ctx.get_workspace_snapshot()
        assert result == {}

    def test_stage_changes_empty(self):
        ctx = self._make_ctx()
        with patch("subprocess.run") as mock_run:
            ctx.stage_changes([])
        mock_run.assert_not_called()

    def test_strip_thinking_tags_no_file(self):
        ctx = self._make_ctx()
        with patch("os.path.exists", return_value=False):
            ctx.strip_thinking_tags("/nonexistent.md")  # should not raise

    def test_strip_thinking_tags_directory(self):
        ctx = self._make_ctx()
        def isdir_side(p):
            return p == "/fake/dir"  # only top-level path is a dir

        with patch("os.path.exists", return_value=True), \
             patch("os.path.isdir", side_effect=isdir_side), \
             patch("os.listdir", return_value=["file.md"]), \
             patch("builtins.open", mock_open(read_data="<thinking>hi</thinking>content")):
            ctx.strip_thinking_tags("/fake/dir")

    def test_strip_thinking_tags_modifies_file(self):
        ctx = self._make_ctx()
        with patch("os.path.exists", return_value=True), \
             patch("os.path.isdir", return_value=False), \
             patch("builtins.open", mock_open(read_data="<thinking>remove</thinking>keep")) as m:
            ctx.strip_thinking_tags("/fake/file.md")

    def test_count_task_files(self):
        ctx = self._make_ctx()
        with patch("os.walk", return_value=[
            ("/fake", [], ["task1.md", "task2.md", "review_summary.md"])
        ]):
            count = ctx.count_task_files("/fake")
        assert count == 2

    def test_parse_markdown_headers(self):
        ctx = self._make_ctx()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="# H1\n## H2\ntext\n")):
            headers = ctx.parse_markdown_headers("/fake/file.md")
        assert "# H1" in headers
        assert "## H2" in headers

    def test_verify_changes_directory_boundary(self):
        ctx = self._make_ctx()
        before = {"/fake/root/allowed/file.py": 100.0}
        after = {"/fake/root/allowed/file.py": 200.0}
        with patch("workflow_lib.context.ProjectContext.get_workspace_snapshot", return_value=after):
            # Should not raise when change is within allowed dir
            ctx.verify_changes(before, ["/fake/root/allowed/"])

    def test_verify_changes_violation(self):
        ctx = self._make_ctx()
        before = {}
        after = {"/fake/root/not_allowed/file.py": 200.0}
        with patch("workflow_lib.context.ProjectContext.get_workspace_snapshot", return_value=after), \
             pytest.raises(SystemExit):
            ctx.verify_changes(before, ["/fake/root/allowed/"])

    def test_verify_changes_deletion_violation(self):
        ctx = self._make_ctx()
        before = {"/fake/root/important.py": 100.0}
        after = {}
        with patch("workflow_lib.context.ProjectContext.get_workspace_snapshot", return_value=after), \
             pytest.raises(SystemExit):
            ctx.verify_changes(before, ["/fake/root/other/"])


# ---------------------------------------------------------------------------
# phases.py – skip paths and failure paths
# ---------------------------------------------------------------------------

class TestPhase1:
    def test_already_generated_skip(self):
        ctx = _mock_ctx(state={"generated": ["doc1"]})
        doc = {"id": "doc1", "type": "spec", "name": "Doc1", "prompt_file": "p.md", "desc": "d"}
        Phase1GenerateDoc(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_failure_exits(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        doc = {"id": "doc1", "type": "spec", "name": "Doc1", "prompt_file": "p.md", "desc": "d"}
        with patch("os.path.exists", return_value=True), \
             pytest.raises(SystemExit):
            Phase1GenerateDoc(doc).execute(ctx)

    def test_success(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=0, stdout="", stderr="")
        doc = {"id": "doc1", "type": "spec", "name": "Doc1", "prompt_file": "p.md", "desc": "d"}
        with patch("os.path.exists", return_value=True):
            Phase1GenerateDoc(doc).execute(ctx)
        ctx.save_state.assert_called()


class TestPhase2:
    def test_skip_non_spec(self):
        ctx = _mock_ctx()
        doc = {"id": "doc1", "type": "research", "name": "Doc1"}
        Phase2FleshOutDoc(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_already_fleshed_out_skip(self):
        ctx = _mock_ctx(state={"fleshed_out": ["doc1"]})
        doc = {"id": "doc1", "type": "spec", "name": "Doc1"}
        Phase2FleshOutDoc(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_section_failure_exits(self):
        ctx = _mock_ctx()
        ctx.parse_markdown_headers.return_value = ["## Section"]
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        doc = {"id": "doc1", "type": "spec", "name": "Doc1"}
        with pytest.raises(SystemExit):
            Phase2FleshOutDoc(doc).execute(ctx)


class TestPhase3:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"final_review_completed": True})
        Phase3FinalReview().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_failure_exits(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        with pytest.raises(SystemExit):
            Phase3FinalReview().execute(ctx)

    def test_success(self):
        ctx = _mock_ctx()
        Phase3FinalReview().execute(ctx)
        assert ctx.state.get("final_review_completed")


class TestPhase3B:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"adversarial_review_completed": True})
        Phase3BAdversarialReview().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_failure_exits(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        with patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            Phase3BAdversarialReview().execute(ctx)

    def test_success_no_scope_creep(self):
        ctx = _mock_ctx()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="all good")):
            Phase3BAdversarialReview().execute(ctx)
        assert ctx.state.get("adversarial_review_completed")

    def test_scope_creep_found_continue(self):
        ctx = _mock_ctx()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="scope creep found")), \
             patch("builtins.input", return_value="c"):
            Phase3BAdversarialReview().execute(ctx)

    def test_scope_creep_quit(self):
        ctx = _mock_ctx()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="scope creep found")), \
             patch("builtins.input", return_value="q"), \
             pytest.raises(SystemExit):
            Phase3BAdversarialReview().execute(ctx)


class TestPhase4A:
    def test_skip_research(self):
        ctx = _mock_ctx()
        doc = {"id": "d", "type": "research", "name": "Doc"}
        Phase4AExtractRequirements(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_already_extracted_skip(self):
        ctx = _mock_ctx(state={"extracted_requirements": ["d"]})
        doc = {"id": "d", "type": "spec", "name": "Doc"}
        Phase4AExtractRequirements(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_doc_file_missing_skip(self):
        ctx = _mock_ctx()
        doc = {"id": "d", "type": "spec", "name": "Doc"}
        with patch("os.path.exists", return_value=False):
            Phase4AExtractRequirements(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_run_fails_exits(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        doc = {"id": "d", "type": "spec", "name": "Doc"}
        with patch("os.path.exists", return_value=True), \
             pytest.raises(SystemExit):
            Phase4AExtractRequirements(doc).execute(ctx)

    def test_verify_fails_exits(self):
        ctx = _mock_ctx()
        doc = {"id": "d", "type": "spec", "name": "Doc"}
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="bad", stderr="")), \
             pytest.raises(SystemExit):
            Phase4AExtractRequirements(doc).execute(ctx)


class TestPhase4B:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"requirements_merged": True})
        Phase4BMergeRequirements().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_run_fails_exits(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        with pytest.raises(SystemExit):
            Phase4BMergeRequirements().execute(ctx)

    def test_verify_fails_exits(self):
        ctx = _mock_ctx()
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="err", stderr="")), \
             pytest.raises(SystemExit):
            Phase4BMergeRequirements().execute(ctx)

    def test_success(self):
        ctx = _mock_ctx()
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            Phase4BMergeRequirements().execute(ctx)
        assert ctx.state.get("requirements_merged")


class TestPhase4BScopeGate:
    def test_already_passed_skip(self):
        ctx = _mock_ctx(state={"scope_gate_passed": True})
        Phase4BScopeGate().execute(ctx)

    def test_missing_requirements_exits(self):
        ctx = _mock_ctx()
        with patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            Phase4BScopeGate().execute(ctx)

    def test_continue_action(self):
        ctx = _mock_ctx()
        req_content = "[REQ-001] requirement"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=req_content)), \
             patch("builtins.input", return_value="c"):
            Phase4BScopeGate().execute(ctx)
        assert ctx.state.get("scope_gate_passed")

    def test_quit_action_exits(self):
        ctx = _mock_ctx()
        req_content = "[REQ-001] requirement"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=req_content)), \
             patch("builtins.input", return_value="q"), \
             pytest.raises(SystemExit):
            Phase4BScopeGate().execute(ctx)


class TestPhase4C:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"requirements_ordered": True})
        Phase4COrderRequirements().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_run_fails_exits(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        with pytest.raises(SystemExit):
            Phase4COrderRequirements().execute(ctx)

    def test_verify_fails_exits(self):
        ctx = _mock_ctx()
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="err", stderr="")), \
             pytest.raises(SystemExit):
            Phase4COrderRequirements().execute(ctx)

    def test_success_moves_file(self):
        ctx = _mock_ctx()
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"), \
             patch("shutil.move"):
            Phase4COrderRequirements().execute(ctx)
        assert ctx.state.get("requirements_ordered")


class TestPhase5:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"phases_completed": True})
        Phase5GenerateEpics().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_run_fails_exits(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        with patch("os.makedirs"), pytest.raises(SystemExit):
            Phase5GenerateEpics().execute(ctx)

    def test_verify_fails_exits(self):
        ctx = _mock_ctx()
        with patch("os.makedirs"), \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="err", stderr="")), \
             pytest.raises(SystemExit):
            Phase5GenerateEpics().execute(ctx)

    def test_success(self):
        ctx = _mock_ctx()
        with patch("os.makedirs"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            Phase5GenerateEpics().execute(ctx)
        assert ctx.state.get("phases_completed")


class TestPhase5B:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"shared_components_completed": True})
        Phase5BSharedComponents().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_failure_exits(self):
        ctx = _mock_ctx()
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        with patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            Phase5BSharedComponents().execute(ctx)

    def test_success(self):
        ctx = _mock_ctx()
        with patch("os.path.exists", return_value=True):
            Phase5BSharedComponents().execute(ctx)
        assert ctx.state.get("shared_components_completed")


class TestPhase6BreakDownTasks:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"tasks_completed": True})
        Phase6BreakDownTasks().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_phases_dir_missing(self):
        ctx = _mock_ctx()
        with patch("os.makedirs"), \
             patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            Phase6BreakDownTasks().execute(ctx)

    def test_no_phase_files_exits(self):
        ctx = _mock_ctx()
        with patch("os.makedirs"), \
             patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=[]), \
             pytest.raises(SystemExit):
            Phase6BreakDownTasks().execute(ctx)


class TestPhase6BReviewTasks:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"tasks_reviewed": True})
        Phase6BReviewTasks().execute(ctx)
        ctx.run_gemini.assert_not_called()


class TestPhase6CCrossPhaseReview:
    def test_already_done_skip_pass1(self):
        ctx = _mock_ctx(state={"cross_phase_reviewed_pass_1": True})
        Phase6CCrossPhaseReview(pass_num=1).execute(ctx)
        ctx.run_gemini.assert_not_called()


class TestPhase6DReorderTasks:
    def test_already_done_skip_pass1(self):
        ctx = _mock_ctx(state={"tasks_reordered_pass_1": True})
        Phase6DReorderTasks(pass_num=1).execute(ctx)
        ctx.run_gemini.assert_not_called()


class TestPhase7ADAGGeneration:
    def test_already_done_skip(self):
        ctx = _mock_ctx(state={"dag_completed": True})
        Phase7ADAGGeneration().execute(ctx)
        ctx.run_gemini.assert_not_called()


# ---------------------------------------------------------------------------
# replan.py – cmd_status, cmd_validate, cmd_block, cmd_unblock, cmd_remove
# ---------------------------------------------------------------------------

class TestReplanCmds:
    def _make_args(self, **kwargs):
        args = MagicMock()
        for k, v in kwargs.items():
            setattr(args, k, v)
        return args

    def test_cmd_status_basic(self):
        from workflow_lib.replan import cmd_status
        dag = {"phase_1/sub/task.md": []}
        wf_state = {"completed_tasks": ["phase_1/sub/task.md"], "merged_tasks": ["phase_1/sub/task.md"]}
        rp_state = {"blocked_tasks": {}, "replan_history": []}
        with patch("workflow_lib.replan.load_dags", return_value=dag), \
             patch("workflow_lib.replan.load_workflow_state", return_value=wf_state), \
             patch("workflow_lib.replan.load_replan_state", return_value=rp_state), \
             patch("os.path.exists", return_value=False):
            cmd_status(self._make_args())

    def test_cmd_status_with_blocked(self):
        from workflow_lib.replan import cmd_status
        dag = {"phase_1/sub/task.md": []}
        wf_state = {"completed_tasks": [], "merged_tasks": []}
        rp_state = {"blocked_tasks": {"phase_1/sub/task.md": {"reason": "test"}}}
        with patch("workflow_lib.replan.load_dags", return_value=dag), \
             patch("workflow_lib.replan.load_workflow_state", return_value=wf_state), \
             patch("workflow_lib.replan.load_replan_state", return_value=rp_state), \
             patch("os.path.exists", return_value=False):
            cmd_status(self._make_args())

    def test_cmd_status_with_orphans(self):
        from workflow_lib.replan import cmd_status
        dag = {}
        wf_state = {"completed_tasks": [], "merged_tasks": []}
        rp_state = {"blocked_tasks": {}}
        with patch("workflow_lib.replan.load_dags", return_value=dag), \
             patch("workflow_lib.replan.load_workflow_state", return_value=wf_state), \
             patch("workflow_lib.replan.load_replan_state", return_value=rp_state), \
             patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", return_value=True):
            cmd_status(self._make_args())

    def test_cmd_validate_no_artifacts(self):
        from workflow_lib.replan import cmd_validate
        with patch("os.path.exists", return_value=False), \
             patch("os.path.isdir", return_value=False):
            cmd_validate(self._make_args())

    def test_cmd_validate_all_pass(self):
        from workflow_lib.replan import cmd_validate
        with patch("os.path.exists", return_value=True), \
             patch("os.path.isdir", return_value=True), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             pytest.raises(SystemExit) as exc:
            cmd_validate(self._make_args())
        assert exc.value.code == 0

    def test_cmd_validate_fail(self):
        from workflow_lib.replan import cmd_validate
        with patch("os.path.exists", return_value=True), \
             patch("os.path.isdir", return_value=True), \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="error\n", stderr="")), \
             pytest.raises(SystemExit) as exc:
            cmd_validate(self._make_args())
        assert exc.value.code == 1

    def test_cmd_block_already_completed(self):
        from workflow_lib.replan import cmd_block
        with patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": ["phase_1/t.md"], "merged_tasks": []}), \
             pytest.raises(SystemExit):
            cmd_block(self._make_args(task="phase_1/t.md", reason="x", dry_run=False))

    def test_cmd_block_not_found(self):
        from workflow_lib.replan import cmd_block
        with patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": [], "merged_tasks": []}), \
             patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            cmd_block(self._make_args(task="phase_1/t.md", reason="x", dry_run=False))

    def test_cmd_block_dry_run(self):
        from workflow_lib.replan import cmd_block
        with patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": [], "merged_tasks": []}), \
             patch("os.path.exists", return_value=True), \
             patch("workflow_lib.replan.load_replan_state", return_value={"blocked_tasks": {}}):
            cmd_block(self._make_args(task="phase_1/t.md", reason="reason", dry_run=True))

    def test_cmd_block_actual(self):
        from workflow_lib.replan import cmd_block
        with patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": [], "merged_tasks": []}), \
             patch("os.path.exists", return_value=True), \
             patch("workflow_lib.replan.load_replan_state", return_value={"blocked_tasks": {}}), \
             patch("workflow_lib.replan.save_replan_state"):
            cmd_block(self._make_args(task="phase_1/t.md", reason="because", dry_run=False))

    def test_cmd_unblock_not_blocked(self):
        from workflow_lib.replan import cmd_unblock
        with patch("workflow_lib.replan.load_replan_state", return_value={"blocked_tasks": {}}):
            cmd_unblock(self._make_args(task="phase_1/t.md", dry_run=False))

    def test_cmd_unblock_dry_run(self):
        from workflow_lib.replan import cmd_unblock
        with patch("workflow_lib.replan.load_replan_state",
                   return_value={"blocked_tasks": {"phase_1/t.md": {}}}):
            cmd_unblock(self._make_args(task="phase_1/t.md", dry_run=True))

    def test_cmd_unblock_actual(self):
        from workflow_lib.replan import cmd_unblock
        with patch("workflow_lib.replan.load_replan_state",
                   return_value={"blocked_tasks": {"phase_1/t.md": {}}, "replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"):
            cmd_unblock(self._make_args(task="phase_1/t.md", dry_run=False))

    def test_cmd_remove_already_completed(self):
        from workflow_lib.replan import cmd_remove
        with patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": ["phase_1/t.md"], "merged_tasks": []}), \
             pytest.raises(SystemExit):
            cmd_remove(self._make_args(task="phase_1/t.md", dry_run=False))

    def test_cmd_remove_not_found(self):
        from workflow_lib.replan import cmd_remove
        with patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": [], "merged_tasks": []}), \
             patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            cmd_remove(self._make_args(task="phase_1/t.md", dry_run=False))

    def test_cmd_remove_dry_run(self):
        from workflow_lib.replan import cmd_remove
        with patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": [], "merged_tasks": []}), \
             patch("os.path.exists", return_value=True), \
             patch("workflow_lib.replan.parse_requirements", return_value=set()):
            cmd_remove(self._make_args(task="phase_1/t.md", dry_run=True))

    def test_cmd_remove_actual_with_dag(self):
        from workflow_lib.replan import cmd_remove
        dag_content = json.dumps({"sub/t.md": [], "sub/other.md": ["sub/t.md"]})
        with patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": [], "merged_tasks": []}), \
             patch("os.path.exists", return_value=True), \
             patch("workflow_lib.replan.parse_requirements", return_value={"REQ-001"}), \
             patch("os.remove"), \
             patch("builtins.open", mock_open(read_data=dag_content)), \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"blocked_tasks": {}, "removed_tasks": [], "replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"):
            cmd_remove(self._make_args(task="phase_1/sub/t.md", dry_run=False))

    def test_cmd_add_dry_run(self):
        from workflow_lib.replan import cmd_add
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=_mock_ctx()), \
             patch("os.path.isdir", return_value=True), \
             patch("os.makedirs"), \
             patch("os.listdir", return_value=[]):
            cmd_add(self._make_args(
                phase_id="phase_1", sub_epic="sub", desc="do something",
                dry_run=True, backend="gemini"
            ))

    def test_cmd_add_phase_not_found(self):
        from workflow_lib.replan import cmd_add
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=_mock_ctx()), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            cmd_add(self._make_args(
                phase_id="phase_99", sub_epic="sub", desc="task",
                dry_run=False, backend="gemini"
            ))


# ---------------------------------------------------------------------------
# Phase7A static methods
# ---------------------------------------------------------------------------

class TestPhase7AStatics:
    def test_parse_depends_on_no_match(self):
        assert Phase7ADAGGeneration._parse_depends_on("no metadata here") is None

    def test_parse_depends_on_none_value(self):
        assert Phase7ADAGGeneration._parse_depends_on("- depends_on: [none]") == []
        assert Phase7ADAGGeneration._parse_depends_on('- depends_on: ["none"]') == []
        assert Phase7ADAGGeneration._parse_depends_on("- depends_on: []") == []

    def test_parse_depends_on_with_deps(self):
        result = Phase7ADAGGeneration._parse_depends_on('- depends_on: ["01_a.md", "01_b.md"]')
        assert result == ["01_a.md", "01_b.md"]

    def test_parse_shared_components_no_match(self):
        assert Phase7ADAGGeneration._parse_shared_components("no components") == []

    def test_parse_shared_components_none(self):
        assert Phase7ADAGGeneration._parse_shared_components("- shared_components: [none]") == []

    def test_parse_shared_components_with_values(self):
        result = Phase7ADAGGeneration._parse_shared_components('- shared_components: ["AuthService", "DB"]')
        assert result == ["AuthService", "DB"]

    def test_build_programmatic_dag_all_have_metadata(self):
        content_a = "- depends_on: []\n- shared_components: []"
        content_b = '- depends_on: ["01_a.md"]\n- shared_components: []'
        with patch("os.listdir", side_effect=[
            ["sub"],       # sub_epics in phase dir
            ["01_a.md", "01_b.md"],  # md_files in sub
            ["sub"],       # sub_epics second pass (shared components)
            ["01_a.md", "01_b.md"],  # md_files second pass
        ]), patch("os.path.isdir", return_value=True), \
           patch("builtins.open", side_effect=[
               mock_open(read_data=content_a)(),
               mock_open(read_data=content_b)(),
               mock_open(read_data=content_a)(),
               mock_open(read_data=content_b)(),
           ]):
            result = Phase7ADAGGeneration._build_programmatic_dag("/fake/phase")
        assert result is not None
        assert "sub/01_a.md" in result

    def test_build_programmatic_dag_missing_metadata(self):
        content = "no depends_on metadata"
        with patch("os.listdir", side_effect=[["sub"], ["01_a.md"]]), \
             patch("os.path.isdir", return_value=True), \
             patch("builtins.open", mock_open(read_data=content)):
            result = Phase7ADAGGeneration._build_programmatic_dag("/fake/phase")
        assert result is None

    def test_build_programmatic_dag_shared_components_dep(self):
        """Task A creates component X; task B consumes it — implicit dep added."""
        import tempfile
        content_a = '- depends_on: []\n- shared_components: ["AuthService"]'
        content_b = '- depends_on: []\n- shared_components: ["AuthService"]'
        with tempfile.TemporaryDirectory() as tmpdir:
            sub = os.path.join(tmpdir, "sub")
            os.makedirs(sub)
            with open(os.path.join(sub, "01_a.md"), "w") as f:
                f.write(content_a)
            with open(os.path.join(sub, "01_b.md"), "w") as f:
                f.write(content_b)
            result = Phase7ADAGGeneration._build_programmatic_dag(tmpdir)
        # Consumer should implicitly depend on creator
        assert result is not None
        assert "sub/01_a.md" in result.get("sub/01_b.md", [])


def _mock_ctx_for_phases():
    ctx = MagicMock()
    ctx.state = {}
    ctx.plan_dir = "/fake/plan"
    ctx.jobs = 1
    ctx.description_ctx = "project desc"
    ctx.root_dir = "/fake/root"
    ctx.load_prompt.return_value = "prompt tmpl {phase_id}"
    ctx.format_prompt.return_value = "formatted prompt"
    ctx.run_gemini.return_value = MagicMock(returncode=0)
    ctx.count_task_files.return_value = 2
    return ctx


class TestPhase6BReviewTasks:
    def test_already_reviewed(self):
        ctx = _mock_ctx_for_phases()
        ctx.state["tasks_reviewed"] = True
        Phase6BReviewTasks().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_tasks_dir_not_exists(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=False), pytest.raises(SystemExit):
            Phase6BReviewTasks().execute(ctx)

    def test_no_phase_dirs(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=[]), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            Phase6BReviewTasks().execute(ctx)

    def _make_mock_executor(self, result=True):
        mock_future = MagicMock()
        mock_future.result.return_value = result
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = mock_future
        return mock_executor, mock_future

    def test_review_already_done_skip(self):
        ctx = _mock_ctx_for_phases()
        mock_executor, mock_future = self._make_mock_executor()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor), \
             patch("concurrent.futures.as_completed", return_value=[mock_future]):
            Phase6BReviewTasks().execute(ctx)
        assert ctx.state.get("tasks_reviewed") is True

    def test_process_phase_review_success(self):
        ctx = _mock_ctx_for_phases()
        ctx.run_gemini.return_value = MagicMock(returncode=0)
        mock_executor, mock_future = self._make_mock_executor()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor), \
             patch("concurrent.futures.as_completed", return_value=[mock_future]):
            Phase6BReviewTasks().execute(ctx)


class TestPhase6CCrossPhaseReview:
    def test_already_completed(self):
        ctx = _mock_ctx_for_phases()
        ctx.state["cross_phase_reviewed_pass_1"] = True
        Phase6CCrossPhaseReview(pass_num=1).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_tasks_dir_not_exists(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=False), pytest.raises(SystemExit):
            Phase6CCrossPhaseReview().execute(ctx)

    def test_no_phase_dirs(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=[]), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            Phase6CCrossPhaseReview().execute(ctx)

    def test_summary_exists_skip(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", side_effect=[["phase_1"], []]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase6CCrossPhaseReview(pass_num=1).execute(ctx)
        assert ctx.state.get("cross_phase_reviewed_pass_1") is True

    def test_full_execution_success(self):
        ctx = _mock_ctx_for_phases()
        ctx.run_gemini.return_value = MagicMock(returncode=0)
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "cross_phase_review_summary" in p:
                return exists_calls[0] > 3
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=[["phase_1"], []]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase6CCrossPhaseReview(pass_num=1).execute(ctx)


class TestPhase6DReorderTasks:
    def test_already_completed(self):
        ctx = _mock_ctx_for_phases()
        ctx.state["tasks_reordered_pass_1"] = True
        Phase6DReorderTasks(pass_num=1).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_tasks_dir_not_exists(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=False), pytest.raises(SystemExit):
            Phase6DReorderTasks().execute(ctx)

    def test_no_phase_dirs(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=[]), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            Phase6DReorderTasks().execute(ctx)

    def test_summary_exists_skip(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", side_effect=[["phase_1"], []]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase6DReorderTasks(pass_num=1).execute(ctx)
        assert ctx.state.get("tasks_reordered_pass_1") is True

    def test_full_execution_success(self):
        ctx = _mock_ctx_for_phases()
        ctx.run_gemini.return_value = MagicMock(returncode=0)
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "reorder_tasks_summary" in p:
                return exists_calls[0] > 3
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=[["phase_1"], []]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase6DReorderTasks(pass_num=1).execute(ctx)


class TestPhase7AExecute:
    def test_already_completed(self):
        ctx = _mock_ctx_for_phases()
        ctx.state["dag_completed"] = True
        Phase7ADAGGeneration().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_tasks_dir_not_exists(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=False), pytest.raises(SystemExit):
            Phase7ADAGGeneration().execute(ctx)

    def test_no_phase_dirs(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=[]), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            Phase7ADAGGeneration().execute(ctx)

    def test_dag_already_exists_skip(self):
        ctx = _mock_ctx_for_phases()
        mock_future = MagicMock()
        mock_future.result.return_value = True

        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = mock_future

        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor), \
             patch("concurrent.futures.as_completed", return_value=[mock_future]):
            Phase7ADAGGeneration().execute(ctx)
        assert ctx.state.get("dag_completed") is True

    def test_programmatic_dag_success(self):
        ctx = _mock_ctx_for_phases()
        dag = {"sub/01_a.md": []}

        mock_future = MagicMock()
        mock_future.result.return_value = True
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = mock_future

        with patch("os.path.exists", side_effect=lambda p: "tasks" in p), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=dag), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor), \
             patch("concurrent.futures.as_completed", return_value=[mock_future]), \
             patch("builtins.open", mock_open()):
            Phase7ADAGGeneration().execute(ctx)
        assert ctx.state.get("dag_completed") is True

    def test_ai_fallback_success(self):
        ctx = _mock_ctx_for_phases()
        ctx.run_gemini.return_value = MagicMock(returncode=0)

        mock_future = MagicMock()
        mock_future.result.return_value = True
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = mock_future

        with patch("os.path.exists", side_effect=lambda p: "tasks" in p), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=None), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor), \
             patch("concurrent.futures.as_completed", return_value=[mock_future]), \
             patch("builtins.open", mock_open(read_data="task content")):
            Phase7ADAGGeneration().execute(ctx)


class TestPhase7BDAGReview:
    def test_already_reviewed(self):
        from workflow_lib.phases import Phase7BDAGReview
        ctx = _mock_ctx_for_phases()
        ctx.state["dag_reviewed"] = True
        Phase7BDAGReview().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_tasks_dir_not_exists(self):
        from workflow_lib.phases import Phase7BDAGReview
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=False), pytest.raises(SystemExit):
            Phase7BDAGReview().execute(ctx)

    def _make_mock_executor(self, result=True):
        mock_future = MagicMock()
        mock_future.result.return_value = result
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = mock_future
        return mock_executor, mock_future

    def test_dag_file_missing_skip(self):
        from workflow_lib.phases import Phase7BDAGReview
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag review tmpl"
        mock_executor, mock_future = self._make_mock_executor()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor), \
             patch("concurrent.futures.as_completed", return_value=[mock_future]):
            Phase7BDAGReview().execute(ctx)
        assert ctx.state.get("dag_reviewed") is True

    def test_reviewed_dag_already_exists(self):
        from workflow_lib.phases import Phase7BDAGReview
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag review tmpl"
        mock_executor, mock_future = self._make_mock_executor()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor), \
             patch("concurrent.futures.as_completed", return_value=[mock_future]):
            Phase7BDAGReview().execute(ctx)
        assert ctx.state.get("dag_reviewed") is True

    def test_full_review_success(self):
        from workflow_lib.phases import Phase7BDAGReview
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag review tmpl"
        ctx.run_gemini.return_value = MagicMock(returncode=0)
        mock_executor, mock_future = self._make_mock_executor()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor), \
             patch("concurrent.futures.as_completed", return_value=[mock_future]), \
             patch("builtins.open", mock_open(read_data='{"t":"v"}')):
            Phase7BDAGReview().execute(ctx)


class TestPhase6BreakDownTasksCoverage:
    def test_already_completed(self):
        ctx = _mock_ctx_for_phases()
        ctx.state["tasks_completed"] = True
        Phase6BreakDownTasks().execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_phases_dir_not_exists(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=False), \
             patch("os.makedirs"), \
             pytest.raises(SystemExit):
            Phase6BreakDownTasks().execute(ctx)

    def test_no_phase_files(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=True), \
             patch("os.makedirs"), \
             patch("os.listdir", return_value=[]), \
             pytest.raises(SystemExit):
            Phase6BreakDownTasks().execute(ctx)

    def test_grouping_already_exists(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.side_effect = ["grouping tmpl", "tasks tmpl"]
        ctx.run_gemini.return_value = MagicMock(returncode=0)
        sub_epics = {"Epic A": ["REQ-001"]}

        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1.md"]), \
             patch("os.makedirs"), \
             patch("os.path.isdir", return_value=True), \
             patch("builtins.open", mock_open(read_data=json.dumps(sub_epics))), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            Phase6BreakDownTasks().execute(ctx)
        assert ctx.state.get("tasks_completed") is True


# ---------------------------------------------------------------------------
# replan helpers
# ---------------------------------------------------------------------------

class TestReplanHelpers:
    def test_show_affected_tasks_no_tasks_dir(self):
        from workflow_lib.replan import _show_affected_tasks
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=False):
            _show_affected_tasks("REQ-001")  # should return without error

    def test_show_affected_tasks_found(self):
        from workflow_lib.replan import _show_affected_tasks
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.walk", return_value=[("/fake/tasks/p1/sub", [], ["t.md"])]), \
             patch("workflow_lib.replan.parse_requirements", return_value={"REQ-001"}), \
             patch("builtins.print"):
            _show_affected_tasks("REQ-001")

    def test_show_affected_tasks_not_found(self):
        from workflow_lib.replan import _show_affected_tasks
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.walk", return_value=[("/fake/tasks/p1/sub", [], ["t.md"])]), \
             patch("workflow_lib.replan.parse_requirements", return_value=set()), \
             patch("builtins.print"):
            _show_affected_tasks("REQ-001")

    def test_run_verify_pass(self):
        from workflow_lib.replan import _run_verify
        mock_res = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_res), \
             patch("builtins.print"):
            _run_verify("verify-master")

    def test_run_verify_fail(self):
        from workflow_lib.replan import _run_verify
        mock_res = MagicMock(returncode=1, stdout="err", stderr="")
        with patch("subprocess.run", return_value=mock_res), \
             patch("builtins.print"):
            _run_verify("verify-master")

    def test_run_verify_unknown_mode(self):
        from workflow_lib.replan import _run_verify
        _run_verify("unknown-mode")  # should do nothing

    def test_rebuild_phase_dag_programmatic(self):
        from workflow_lib.replan import _rebuild_phase_dag
        ctx = MagicMock()
        ctx.root_dir = "/fake/root"
        ctx.description_ctx = "desc"
        dag = {"sub/t.md": []}
        with patch("os.path.exists", return_value=False), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=dag), \
             patch("builtins.open", mock_open()), \
             patch("builtins.print"):
            _rebuild_phase_dag("/fake/tasks/phase_1", ctx)

    def test_rebuild_phase_dag_ai_fallback(self):
        from workflow_lib.replan import _rebuild_phase_dag
        ctx = MagicMock()
        ctx.root_dir = "/fake/root"
        ctx.description_ctx = "desc"
        ctx.load_prompt.return_value = "dag tmpl"
        ctx.format_prompt.return_value = "formatted"
        ctx.run_ai.return_value = MagicMock(returncode=0)
        with patch("os.path.exists", side_effect=lambda p: "dag.json" in p and "reviewed" not in p), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=None), \
             patch("os.listdir", side_effect=[["sub"], ["t.md"]]), \
             patch("os.path.isdir", return_value=True), \
             patch("builtins.open", mock_open(read_data="task content")), \
             patch("builtins.print"):
            _rebuild_phase_dag("/fake/tasks/phase_1", ctx)


class TestReplanCmdsCoverage:
    def _make_args(self, **kw):
        args = MagicMock()
        for k, v in kw.items():
            setattr(args, k, v)
        return args

    def _mock_ctx(self):
        ctx = MagicMock()
        ctx.state = {}
        ctx.description_ctx = "desc"
        ctx.root_dir = "/fake/root"
        ctx.run_ai.return_value = MagicMock(returncode=0)
        ctx.load_prompt.return_value = "prompt tmpl"
        ctx.format_prompt.return_value = "formatted prompt"
        ctx.load_shared_components.return_value = ""
        return ctx

    def test_cmd_add_success(self):
        from workflow_lib.replan import cmd_add
        ctx = self._mock_ctx()
        existing_files = set()

        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.makedirs"), \
             patch("os.listdir", side_effect=[[], ["new_task.md"]]), \
             patch("os.path.isdir", return_value=True), \
             patch("workflow_lib.replan._rebuild_phase_dag"), \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"blocked_tasks": {}, "replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("builtins.print"):
            cmd_add(self._make_args(
                phase_id="phase_1", sub_epic="sub", desc="do something",
                dry_run=False, backend="gemini"
            ))

    def test_cmd_add_no_new_file(self):
        from workflow_lib.replan import cmd_add
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.makedirs"), \
             patch("os.listdir", side_effect=[[], []]), \
             patch("builtins.print"), \
             pytest.raises(SystemExit):
            cmd_add(self._make_args(
                phase_id="phase_1", sub_epic="sub", desc="do something",
                dry_run=False, backend="gemini"
            ))

    def test_cmd_modify_req_edit(self):
        from workflow_lib.replan import cmd_modify_req
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run"), \
             patch("workflow_lib.replan._run_verify"):
            cmd_modify_req(self._make_args(edit_req=True, remove_req=None, add_req=None, dry_run=False))

    def test_cmd_modify_req_not_found(self):
        from workflow_lib.replan import cmd_modify_req
        with patch("os.path.exists", return_value=False), pytest.raises(SystemExit):
            cmd_modify_req(self._make_args(edit_req=False, remove_req=None, add_req=None, dry_run=False))

    def test_cmd_modify_req_remove_dry_run(self):
        from workflow_lib.replan import cmd_modify_req
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="### **[REQ-001]** description\n")), \
             patch("workflow_lib.replan._show_affected_tasks"):
            cmd_modify_req(self._make_args(edit_req=False, remove_req="REQ-001", add_req=None, dry_run=True))

    def test_cmd_modify_req_remove_req_not_found(self):
        from workflow_lib.replan import cmd_modify_req
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="no matching req here")), \
             pytest.raises(SystemExit):
            cmd_modify_req(self._make_args(edit_req=False, remove_req="REQ-999", add_req=None, dry_run=False))

    def test_cmd_modify_req_remove_success(self):
        from workflow_lib.replan import cmd_modify_req
        content = "### **[REQ-001]** desc\nsome content\n"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=content)), \
             patch("workflow_lib.replan._show_affected_tasks"), \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("builtins.print"):
            cmd_modify_req(self._make_args(edit_req=False, remove_req="REQ-001", add_req=None, dry_run=False))

    def test_cmd_modify_req_add(self):
        from workflow_lib.replan import cmd_modify_req
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run"), \
             patch("workflow_lib.replan._run_verify"), \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"):
            cmd_modify_req(self._make_args(edit_req=False, remove_req=None, add_req="New req", dry_run=False))

    def test_cmd_regen_dag_dry_run(self):
        from workflow_lib.replan import cmd_regen_dag
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True):
            cmd_regen_dag(self._make_args(phase_id="phase_1", dry_run=True, backend="gemini"))

    def test_cmd_regen_dag_not_found(self):
        from workflow_lib.replan import cmd_regen_dag
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            cmd_regen_dag(self._make_args(phase_id="phase_99", dry_run=False, backend="gemini"))

    def test_cmd_regen_dag_success(self):
        from workflow_lib.replan import cmd_regen_dag
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan._rebuild_phase_dag"), \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"):
            cmd_regen_dag(self._make_args(phase_id="phase_1", dry_run=False, backend="gemini"))

    def test_cmd_regen_tasks_phase_not_found(self):
        from workflow_lib.replan import cmd_regen_tasks
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": []}), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            cmd_regen_tasks(self._make_args(phase_id="phase_99", sub_epic="sub", backend="gemini", dry_run=False, force=False))

    def test_cmd_regen_tasks_sub_epic_not_found(self):
        from workflow_lib.replan import cmd_regen_tasks
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": []}), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p and "sub_epic" not in p), \
             pytest.raises(SystemExit):
            cmd_regen_tasks(self._make_args(phase_id="phase_1", sub_epic="sub_epic", backend="gemini", dry_run=False, force=False))

    def test_cmd_regen_tasks_dry_run(self):
        from workflow_lib.replan import cmd_regen_tasks
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": []}), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=[]):
            cmd_regen_tasks(self._make_args(phase_id="phase_1", sub_epic="sub", backend="gemini", dry_run=True, force=False))

    def test_cmd_regen_tasks_no_sub_epic_dry_run(self):
        from workflow_lib.replan import cmd_regen_tasks
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": []}), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True):
            cmd_regen_tasks(self._make_args(phase_id="phase_1", sub_epic=None, backend="gemini", dry_run=True, force=False))

    def test_cmd_regen_tasks_no_sub_epic_no_dry_run(self):
        from workflow_lib.replan import cmd_regen_tasks
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": []}), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             pytest.raises(SystemExit):
            cmd_regen_tasks(self._make_args(phase_id="phase_1", sub_epic=None, backend="gemini", dry_run=False, force=False))

    def test_cmd_regen_tasks_grouping_file_not_found(self):
        from workflow_lib.replan import cmd_regen_tasks
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": []}), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=[]), \
             patch("os.path.exists", return_value=False), \
             pytest.raises(SystemExit):
            cmd_regen_tasks(self._make_args(phase_id="phase_1", sub_epic="sub", backend="gemini", dry_run=False, force=False))

    def test_cmd_regen_tasks_success(self):
        from workflow_lib.replan import cmd_regen_tasks
        ctx = self._mock_ctx()
        sub_epics = {"Sub Epic": ["REQ-001"]}
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.load_workflow_state",
                   return_value={"completed_tasks": []}), \
             patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=[]), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"), \
             patch("builtins.open", mock_open(read_data=json.dumps(sub_epics))), \
             patch("workflow_lib.replan._rebuild_phase_dag"), \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("builtins.print"):
            cmd_regen_tasks(self._make_args(
                phase_id="phase_1", sub_epic="sub_epic", backend="gemini", dry_run=False, force=False
            ))

    def test_cmd_regen_components_dry_run(self):
        from workflow_lib.replan import cmd_regen_components
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=self._mock_ctx()):
            cmd_regen_components(self._make_args(dry_run=True, backend="gemini"))

    def test_cmd_regen_components_success(self):
        from workflow_lib.replan import cmd_regen_components
        ctx = self._mock_ctx()
        mock_phase = MagicMock()
        with patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan.Phase5BSharedComponents", return_value=mock_phase), \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"):
            cmd_regen_components(self._make_args(dry_run=False, backend="gemini"))
        mock_phase.execute.assert_called_once_with(ctx)

    def test_cmd_cascade_not_found(self):
        from workflow_lib.replan import cmd_cascade
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            cmd_cascade(self._make_args(phase_id="phase_99", dry_run=False, backend="gemini"))

    def test_cmd_cascade_dry_run(self):
        from workflow_lib.replan import cmd_cascade
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True):
            cmd_cascade(self._make_args(phase_id="phase_1", dry_run=True, backend="gemini"))

    def test_cmd_cascade_success(self):
        from workflow_lib.replan import cmd_cascade
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("os.listdir", return_value=[]), \
             patch("workflow_lib.replan.parse_requirements", return_value=set()), \
             patch("workflow_lib.replan._rebuild_phase_dag"), \
             patch("workflow_lib.replan._run_verify"), \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("builtins.print"):
            cmd_cascade(self._make_args(phase_id="phase_1", dry_run=False, backend="gemini"))

    def test_make_runner_claude(self):
        from workflow_lib.replan import _make_runner
        from workflow_lib.runners import ClaudeRunner
        result = _make_runner("claude")
        assert isinstance(result, ClaudeRunner)

    def test_make_runner_copilot(self):
        from workflow_lib.replan import _make_runner
        from workflow_lib.runners import CopilotRunner
        result = _make_runner("copilot")
        assert isinstance(result, CopilotRunner)

    def test_make_runner_gemini(self):
        from workflow_lib.replan import _make_runner
        from workflow_lib.runners import GeminiRunner
        result = _make_runner("gemini")
        assert isinstance(result, GeminiRunner)


# ---------------------------------------------------------------------------
# Inner function coverage — run real ThreadPoolExecutor with mocked FS
# ---------------------------------------------------------------------------

class TestPhase7AInner:
    """Tests that actually exercise the process_phase_dag closure."""

    def test_dag_exists_skip(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag tmpl"
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase7ADAGGeneration().execute(ctx)
        assert ctx.state.get("dag_completed") is True

    def test_programmatic_dag_inner(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag tmpl"
        dag = {"sub/01_a.md": []}
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "dag.json" in p and "reviewed" not in p:
                return False  # not yet generated
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: ["phase_1"] if "tasks" in p else []), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=dag), \
             patch("builtins.open", mock_open()):
            Phase7ADAGGeneration().execute(ctx)
        assert ctx.state.get("dag_completed") is True

    def test_ai_fallback_inner_no_sub_epics(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag tmpl"
        ctx.run_gemini.return_value = MagicMock(returncode=0)
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "dag.json" in p:
                return False
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: (
                 ["phase_1"] if "tasks" in p and "phase_1" not in p else []
             )), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=None):
            Phase7ADAGGeneration().execute(ctx)
        # no sub_epics -> process returns True -> stage + save
        assert ctx.state.get("dag_completed") is True


class TestPhase7BInner:
    """Tests that actually exercise the process_phase_review closure."""

    def test_no_dag_file_inner(self):
        from workflow_lib.phases import Phase7BDAGReview
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag review tmpl"

        def exists_side(p):
            if "dag.json" in p:
                return False  # no dag file -> skip
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: ["phase_1"] if "tasks" in p and "phase_1" not in p else []), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase7BDAGReview().execute(ctx)
        assert ctx.state.get("dag_reviewed") is True

    def test_reviewed_dag_exists_inner(self):
        from workflow_lib.phases import Phase7BDAGReview
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag review tmpl"

        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", side_effect=lambda p: ["phase_1"] if "tasks" in p and "phase_1" not in p else []), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase7BDAGReview().execute(ctx)
        assert ctx.state.get("dag_reviewed") is True

    def test_full_inner_success(self):
        from workflow_lib.phases import Phase7BDAGReview
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag review tmpl"
        ctx.format_prompt.return_value = "formatted"
        ai_results = [0]

        def run_gemini_side(*a, **kw):
            ai_results[0] += 1
            return MagicMock(returncode=0)

        ctx.run_gemini.side_effect = run_gemini_side
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "dag_reviewed.json" in p:
                return exists_calls[0] > 4
            if "dag.json" in p:
                return True
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: (
                 ["phase_1"] if ("tasks" in p and "phase_1" not in p) else
                 ["sub"] if "phase_1" in p and "sub" not in p else
                 ["t.md"]
             )), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p or ("sub" in p and "t.md" not in p)), \
             patch("builtins.open", mock_open(read_data='{"t":"v"}')):
            Phase7BDAGReview().execute(ctx)
        assert ctx.state.get("dag_reviewed") is True


class TestPhase6BInner:
    """Tests that actually exercise process_phase_review closure."""

    def test_review_summary_exists_inner(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", side_effect=lambda p: ["phase_1"] if "tasks" in p and "phase_1" not in p else ["sub"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p or "sub" in p):
            Phase6BReviewTasks().execute(ctx)
        assert ctx.state.get("tasks_reviewed") is True

    def test_no_sub_epics_inner(self):
        ctx = _mock_ctx_for_phases()
        with patch("os.path.exists", side_effect=lambda p: False if "review_summary" in p else True), \
             patch("os.listdir", side_effect=lambda p: ["phase_1"] if "tasks" in p and "phase_1" not in p else []), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase6BReviewTasks().execute(ctx)
        assert ctx.state.get("tasks_reviewed") is True

    def test_review_success_inner(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "review tmpl"
        ctx.format_prompt.return_value = "formatted"
        summary_exists = [False]

        def run_gemini_side(*a, **kw):
            summary_exists[0] = True
            return MagicMock(returncode=0)

        ctx.run_gemini.side_effect = run_gemini_side

        def exists_side(p):
            if "review_summary" in p:
                return summary_exists[0]
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: (
                 ["phase_1"] if ("tasks" in p and "phase_1" not in p) else
                 ["sub"] if "phase_1" in p and "sub" not in p else
                 ["t.md"]
             )), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p or ("sub" in p and ".md" not in p)), \
             patch("builtins.open", mock_open(read_data="task content")):
            Phase6BReviewTasks().execute(ctx)
        assert ctx.state.get("tasks_reviewed") is True


class TestPhase6CCoverage:
    def test_no_tasks_content_inner(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "cross review tmpl"
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "cross_phase_review_summary" in p:
                return False
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: ["phase_1"] if "tasks" in p and "phase_1" not in p else []), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            # no md files -> tasks_content is empty -> return early
            Phase6CCrossPhaseReview(pass_num=1).execute(ctx)

    def test_full_success_inner(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "cross review tmpl"
        ctx.format_prompt.return_value = "formatted"
        ctx.count_task_files.return_value = 1
        summary_exists = [False]

        def run_gemini_side(*a, **kw):
            summary_exists[0] = True
            return MagicMock(returncode=0)

        ctx.run_gemini.side_effect = run_gemini_side
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "cross_phase_review_summary" in p:
                return summary_exists[0]
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: (
                 ["phase_1"] if ("tasks" in p and "phase_1" not in p) else
                 ["sub"] if "phase_1" in p and "sub" not in p else
                 ["t.md"]
             )), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p or ("sub" in p and ".md" not in p)), \
             patch("builtins.open", mock_open(read_data="task content")):
            Phase6CCrossPhaseReview(pass_num=1).execute(ctx)
        assert ctx.state.get("cross_phase_reviewed_pass_1") is True


class TestPhase6DCoverage:
    def test_no_tasks_content_inner(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "reorder tmpl"
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "reorder_tasks_summary" in p:
                return False
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: ["phase_1"] if "tasks" in p and "phase_1" not in p else []), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p):
            Phase6DReorderTasks(pass_num=1).execute(ctx)

    def test_full_success_inner(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "reorder tmpl"
        ctx.format_prompt.return_value = "formatted"
        summary_exists = [False]

        def run_gemini_side(*a, **kw):
            summary_exists[0] = True
            return MagicMock(returncode=0)

        ctx.run_gemini.side_effect = run_gemini_side
        exists_calls = [0]

        def exists_side(p):
            exists_calls[0] += 1
            if "reorder_tasks_summary" in p:
                return summary_exists[0]
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: (
                 ["phase_1"] if ("tasks" in p and "phase_1" not in p) else
                 ["sub"] if "phase_1" in p and "sub" not in p else
                 ["t.md"]
             )), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p or ("sub" in p and ".md" not in p)), \
             patch("builtins.open", mock_open(read_data="task content")):
            Phase6DReorderTasks(pass_num=1).execute(ctx)
        assert ctx.state.get("tasks_reordered_pass_1") is True


class TestCLICoverage:
    def test_cmd_setup_venv_not_exists(self):
        from workflow_lib.cli import cmd_setup
        args = MagicMock()
        with patch("os.path.isdir", side_effect=lambda p: False), \
             patch("os.path.isfile", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("subprocess.run"), \
             patch("builtins.print"):
            cmd_setup(args)

    def test_cmd_setup_template_src_not_exists(self):
        from workflow_lib.cli import cmd_setup
        args = MagicMock()
        with patch("os.path.isdir", side_effect=lambda p: True), \
             patch("os.path.isfile", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("subprocess.run"), \
             patch("builtins.print"):
            cmd_setup(args)

    def test_cmd_setup_dst_already_exists(self):
        from workflow_lib.cli import cmd_setup
        args = MagicMock()
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.isfile", return_value=False), \
             patch("os.path.exists", return_value=True), \
             patch("subprocess.run"), \
             patch("builtins.print"):
            cmd_setup(args)

    def test_cmd_plan_force_known_phase(self):
        from workflow_lib.cli import cmd_plan
        ctx = MagicMock()
        ctx.state = {"dag_completed": True}
        args = MagicMock(phase="7-dag", force=True, backend="gemini", jobs=1)
        mock_orc = MagicMock()
        with patch("workflow_lib.cli._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.cli.ProjectContext", return_value=ctx), \
             patch("workflow_lib.cli.Orchestrator", return_value=mock_orc):
            cmd_plan(args)
        assert ctx.state["dag_completed"] == False

    def test_cmd_plan_force_unknown_phase(self):
        from workflow_lib.cli import cmd_plan
        ctx = MagicMock()
        ctx.state = {}
        args = MagicMock(phase="99-unknown", force=True, backend="gemini", jobs=1)
        mock_orc = MagicMock()
        with patch("workflow_lib.cli._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.cli.ProjectContext", return_value=ctx), \
             patch("workflow_lib.cli.Orchestrator", return_value=mock_orc), \
             patch("builtins.print"):
            cmd_plan(args)

    def test_cmd_plan_no_force(self):
        from workflow_lib.cli import cmd_plan
        ctx = MagicMock()
        ctx.state = {}
        args = MagicMock(phase=None, force=False, backend="gemini", jobs=1)
        mock_orc = MagicMock()
        with patch("workflow_lib.cli._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.cli.ProjectContext", return_value=ctx), \
             patch("workflow_lib.cli.Orchestrator", return_value=mock_orc):
            cmd_plan(args)


# ---------------------------------------------------------------------------
# config.py – explicit coverage of the "file missing" branch
# ---------------------------------------------------------------------------

class TestConfigCoverage:
    def test_get_serena_enabled_no_file(self):
        with patch("os.path.exists", return_value=False):
            from workflow_lib.config import get_serena_enabled
            result = get_serena_enabled()
        assert result is False

    def test_load_config_with_comment(self):
        jsonc = '{\n  // comment\n  "serena": true\n}'
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import load_config
            result = load_config()
        assert result.get("serena") is True
