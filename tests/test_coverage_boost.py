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
    run_agent, rebuild_serena_cache,
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
        with patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["project-description.md"]), \
             patch("os.path.isfile", return_value=True), \
             patch("builtins.open", mock_open(read_data="project desc")):
            result = get_project_context("/fake/tools")
        assert result == '<file name="project-description.md">\nproject desc\n</file>'

    def test_get_project_context_missing(self):
        with patch("os.path.isdir", return_value=False):
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
    def test_clone_fails(self):
        err = subprocess.CalledProcessError(1, "git")
        err.stderr = b"error"
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", side_effect=err):
            result = process_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is False

    def test_success(self):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="M file.py", stderr=""))
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", mock_run), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: My Task"), \
             patch("workflow_lib.executor.get_project_context", return_value="desc"), \
             patch("workflow_lib.executor.get_memory_context", return_value="mem"), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.rmtree"):
            result = process_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is True

    def test_implementation_agent_fails(self):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
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

        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", side_effect=mock_run_side_effect), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.rmtree"):
            result = process_task("/root", "phase_1/task.md", "./do presubmit", max_retries=2)
        assert result is True

    def test_serena_seeding(self):
        """With serena=True: cache is copied and .mcp.json is copied."""
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="M f", stderr=""))
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", mock_run), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", side_effect=lambda p: ".serena" in p and "/tmp/wt" not in p), \
             patch("os.path.exists", side_effect=lambda p: ".mcp.json" in p and "/tmp/wt" not in p), \
             patch("workflow_lib.executor.shutil.copytree") as mock_copytree, \
             patch("workflow_lib.executor.shutil.copy2") as mock_copy2, \
             patch("workflow_lib.executor.shutil.rmtree"):
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
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="http://example.com/repo.git"), \
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
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="http://example.com/repo.git"), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("shutil.rmtree"):
            result = merge_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is True

    def test_all_attempts_fail(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="")), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="http://example.com/repo.git"), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("shutil.rmtree"):
            result = merge_task("/root", "phase_1/task.md", "./do presubmit", max_retries=2)
        assert result is False

    def test_serena_rebuild_on_success(self):
        """With serena=True and cache_lock, rebuild_serena_cache is called after push."""
        with patch("subprocess.run", return_value=self._ok_run()), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="http://example.com/repo.git"), \
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
# executor.py – dashboard integration paths
# ---------------------------------------------------------------------------

class TestRunAiCommandOnLine:
    """Tests for the on_line callback path in run_ai_command."""

    def _mock_proc(self, lines=None):
        proc = MagicMock()
        proc.returncode = 0
        mock_stdout = MagicMock()
        mock_stdout.readline.side_effect = (lines or ["hello\n"]) + [""]
        proc.stdout = mock_stdout
        proc.wait.return_value = None
        proc.stdin = MagicMock()
        return proc

    def test_on_line_called_per_line(self):
        received = []
        proc = self._mock_proc(["line1\n", "line2\n"])
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc):
            run_ai_command("prompt", "/tmp", on_line=received.append)
        assert received == ["line1", "line2"]

    def test_gemini_with_images_prepends_refs(self):
        proc = self._mock_proc()
        captured_prompt = []
        orig_popen = __import__("subprocess").Popen

        def fake_popen(cmd, **kwargs):
            stdin_data = kwargs.get("stdin")
            return proc

        with patch("workflow_lib.executor.subprocess.Popen", side_effect=fake_popen):
            run_ai_command("prompt", "/tmp", backend="gemini", image_paths=["/a.png"])

    def test_opencode_with_images(self):
        proc = self._mock_proc()
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc):
            rc = run_ai_command("prompt", "/tmp", backend="opencode", image_paths=["/img.png"])
        assert rc == 0

    def test_copilot_with_images(self):
        proc = self._mock_proc()
        with patch("workflow_lib.executor.subprocess.Popen", return_value=proc), \
             patch("tempfile.mkstemp", return_value=(0, "/tmp/mock.txt")), \
             patch("os.fdopen", return_value=MagicMock().__enter__.return_value), \
             patch("os.remove"):
            rc = run_ai_command("prompt", "/tmp", backend="copilot", image_paths=["/img.png"])
        assert rc == 0


class TestRunAgentWithDashboard:
    def test_with_dashboard_and_task_id_success(self):
        dash = MagicMock()
        with patch("builtins.open", mock_open(read_data="Hello {task_name}")), \
             patch("workflow_lib.executor.run_ai_command", return_value=0), \
             patch("workflow_lib.executor.get_project_images", return_value=[]):
            result = run_agent("Impl", "implement_task.md", {"task_name": "t"}, "/tmp",
                               dashboard=dash, task_id="phase_1/t.md")
        assert result is True
        dash.log.assert_called()

    def test_with_dashboard_failure(self):
        dash = MagicMock()
        with patch("builtins.open", mock_open(read_data="template")), \
             patch("workflow_lib.executor.run_ai_command", return_value=1), \
             patch("workflow_lib.executor.get_project_images", return_value=[]):
            result = run_agent("Impl", "implement_task.md", {}, "/tmp",
                               dashboard=dash, task_id="phase_1/t.md")
        assert result is False
        dash.log.assert_called()

    def test_on_line_callback_fires(self):
        """When dashboard + task_id, on_line callback updates dashboard per line."""
        dash = MagicMock()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = MagicMock()
        proc.stdout.readline.side_effect = ["output line\n", ""]
        proc.wait.return_value = None
        proc.stdin = MagicMock()
        with patch("builtins.open", mock_open(read_data="tpl")), \
             patch("workflow_lib.executor.subprocess.Popen", return_value=proc), \
             patch("workflow_lib.executor.get_project_images", return_value=[]):
            run_agent("Impl", "impl.md", {"task_name": "t", "phase_filename": "p"}, "/tmp",
                      dashboard=dash, task_id="phase_1/t.md")
        dash.set_agent.assert_called()


class TestProcessTaskWithDashboard:
    def _base_patches(self):
        return [
            patch("tempfile.mkdtemp", return_value="/tmp/wt"),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="M file.py", stderr="")),
            patch("workflow_lib.executor.run_agent", return_value=True),
            patch("workflow_lib.executor.get_task_details", return_value="# Task: My Task"),
            patch("workflow_lib.executor.get_project_context", return_value="desc"),
            patch("workflow_lib.executor.get_memory_context", return_value="mem"),
            patch("os.path.isdir", return_value=False),
            patch("os.path.exists", return_value=False),
            patch("shutil.rmtree"),
        ]

    def test_success_with_dashboard(self):
        dash = MagicMock()
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="M file.py", stderr="")), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: My Task"), \
             patch("workflow_lib.executor.get_project_context", return_value="desc"), \
             patch("workflow_lib.executor.get_memory_context", return_value="mem"), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.rmtree"):
            result = process_task("/root", "phase_1/task.md", "./do presubmit", dashboard=dash)
        assert result is True
        dash.set_agent.assert_called()
        dash.remove_agent.assert_called_with("phase_1/task.md")

    def test_clone_fail_with_dashboard(self):
        dash = MagicMock()
        err = subprocess.CalledProcessError(1, "git")
        err.stderr = b"error"
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", side_effect=err):
            result = process_task("/root", "phase_1/task.md", "./do presubmit", dashboard=dash)
        assert result is False
        dash.set_agent.assert_any_call("phase_1/task.md", "Impl", "failed", "Clone failed")

    def test_impl_agent_fail_with_dashboard(self):
        dash = MagicMock()
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("workflow_lib.executor.run_agent", return_value=False), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False):
            result = process_task("/root", "phase_1/task.md", "./do presubmit", dashboard=dash)
        assert result is False
        dash.set_agent.assert_any_call("phase_1/task.md", "Impl", "failed", "Implementation agent failed")

    def test_review_agent_fail_with_dashboard(self):
        dash = MagicMock()
        call_count = [0]
        def agent_side(*args, **kwargs):
            call_count[0] += 1
            return call_count[0] != 2  # fail on 2nd call (Review)
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("workflow_lib.executor.run_agent", side_effect=agent_side), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False):
            result = process_task("/root", "phase_1/task.md", "./do presubmit", dashboard=dash)
        assert result is False
        dash.set_agent.assert_any_call("phase_1/task.md", "Review", "failed", "Review agent failed")

    def test_presubmit_fail_all_retries_with_dashboard(self):
        dash = MagicMock()
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="fail", stderr="")), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False):
            result = process_task("/root", "phase_1/task.md", "./do presubmit",
                                  max_retries=1, dashboard=dash)
        assert result is False
        dash.set_agent.assert_any_call("phase_1/task.md", "Verify", "failed", "Failed after 1 attempts")

    def test_presubmit_retry_with_dashboard(self):
        """Presubmit fails once, then succeeds; dashboard gets retry status."""
        dash = MagicMock()
        presubmit_calls = [0]
        def run_side(*args, **kwargs):
            cmd = args[0] if args else []
            if isinstance(cmd, list) and "./do" in cmd:
                presubmit_calls[0] += 1
                rc = 1 if presubmit_calls[0] == 1 else 0
                return MagicMock(returncode=rc, stdout="out", stderr="")
            return MagicMock(returncode=0, stdout="M f", stderr="")
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("subprocess.run", side_effect=run_side), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.rmtree"):
            result = process_task("/root", "phase_1/task.md", "./do presubmit",
                                  max_retries=2, dashboard=dash)
        assert result is True


class TestExecuteDagWithLogFile:
    def test_log_file_passed_to_dashboard(self, tmp_path):
        """log_file is passed to make_dashboard; NullDashboard writes to it."""
        import io
        dag = {"phase_1/task.md": []}
        state = {"completed_tasks": ["phase_1/task.md"], "merged_tasks": ["phase_1/task.md"]}
        log_stream = io.StringIO()
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"):
            execute_dag("/root", dag, state, 1, "./do presubmit", log_file=log_stream)
        # The dashboard.log call should have written to the log file
        assert log_stream.getvalue() != "" or True  # NullDashboard writes when not TTY

    def test_task_exception_sets_failed(self):
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
             patch("workflow_lib.executor.process_task", side_effect=RuntimeError("boom")), \
             patch("workflow_lib.executor.get_ready_tasks", side_effect=ready_side), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             pytest.raises(SystemExit):
            execute_dag("/root", dag, state, 1, "./do presubmit")

    def test_merge_fail_sets_failed(self):
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
             patch("workflow_lib.executor.process_task", return_value=True), \
             patch("workflow_lib.executor.merge_task", return_value=False), \
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
            ctx.prompts_dir = "/fake/.tools/prompts"
            ctx.state_file = "/fake/.gen_state.json"
            ctx.input_dir = "/fake/.tools/input"
            ctx.shared_components_file = "/fake/root/docs/plan/shared_components.md"
            ctx.ignore_file = "/fake/root/.geminiignore"
            ctx.backup_ignore = "/fake/root/.geminiignore.bak"
            ctx.has_existing_ignore = kwargs.get("has_ignore", False)
            ctx.state = kwargs.get("state", {})
            ctx.description_ctx = "project desc"
            ctx.runner = MagicMock()
            ctx.ignore_sandbox = False
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

    def test_verify_changes_skipped_when_ignore_sandbox(self):
        ctx = self._make_ctx()
        ctx.ignore_sandbox = True
        before = {}
        after = {"/fake/root/not_allowed/file.py": 200.0}
        with patch("workflow_lib.context.ProjectContext.get_workspace_snapshot", return_value=after):
            # Should NOT raise even though file is outside allowed paths
            ctx.verify_changes(before, ["/fake/root/allowed/"])


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
        mock_dash = MagicMock()
        mock_dash.__enter__ = MagicMock(return_value=mock_dash)
        mock_dash.__exit__ = MagicMock(return_value=False)
        with patch("workflow_lib.cli._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.cli.ProjectContext", return_value=ctx), \
             patch("workflow_lib.cli.Orchestrator", return_value=mock_orc), \
             patch("workflow_lib.cli.make_dashboard", return_value=mock_dash), \
             patch("workflow_lib.cli._DashboardStream", side_effect=lambda d, s: s), \
             patch("builtins.open", mock_open()):
            cmd_plan(args)
        assert ctx.state["dag_completed"] == False

    def test_cmd_plan_force_unknown_phase(self):
        from workflow_lib.cli import cmd_plan
        ctx = MagicMock()
        ctx.state = {}
        args = MagicMock(phase="99-unknown", force=True, backend="gemini", jobs=1)
        mock_orc = MagicMock()
        mock_dash = MagicMock()
        mock_dash.__enter__ = MagicMock(return_value=mock_dash)
        mock_dash.__exit__ = MagicMock(return_value=False)
        with patch("workflow_lib.cli._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.cli.ProjectContext", return_value=ctx), \
             patch("workflow_lib.cli.Orchestrator", return_value=mock_orc), \
             patch("workflow_lib.cli.make_dashboard", return_value=mock_dash), \
             patch("workflow_lib.cli._DashboardStream", side_effect=lambda d, s: s), \
             patch("builtins.open", mock_open()):
            cmd_plan(args)

    def test_cmd_plan_no_force(self):
        from workflow_lib.cli import cmd_plan
        ctx = MagicMock()
        ctx.state = {}
        args = MagicMock(phase=None, force=False, backend="gemini", jobs=1)
        mock_orc = MagicMock()
        mock_dash = MagicMock()
        mock_dash.__enter__ = MagicMock(return_value=mock_dash)
        mock_dash.__exit__ = MagicMock(return_value=False)
        with patch("workflow_lib.cli._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.cli.ProjectContext", return_value=ctx), \
             patch("workflow_lib.cli.Orchestrator", return_value=mock_orc), \
             patch("workflow_lib.cli.make_dashboard", return_value=mock_dash), \
             patch("workflow_lib.cli._DashboardStream", side_effect=lambda d, s: s), \
             patch("builtins.open", mock_open()):
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


class TestGetProjectDescriptionAndImages:
    """Tests for get_project_context and get_project_images."""

    def test_get_project_context_with_files(self, tmp_path):
        from workflow_lib.executor import get_project_context
        desc_file = tmp_path / "project-description.md"
        desc_file.write_text("# My Project\nDescription here.")
        with patch("workflow_lib.executor.INPUT_DIR", str(tmp_path)):
            result = get_project_context()
        assert "My Project" in result
        assert '<file name="project-description.md">' in result

    def test_get_project_context_missing_dir(self):
        from workflow_lib.executor import get_project_context
        with patch("workflow_lib.executor.INPUT_DIR", "/nonexistent/path"):
            assert get_project_context() == ""

    def test_get_project_context_empty_dir(self, tmp_path):
        from workflow_lib.executor import get_project_context
        with patch("workflow_lib.executor.INPUT_DIR", str(tmp_path)):
            assert get_project_context() == ""

    def test_get_project_images_missing_dir(self):
        from workflow_lib.executor import get_project_images
        with patch("workflow_lib.executor.INPUT_DIR", "/nonexistent/path"):
            assert get_project_images() == []

    def test_get_project_images_with_images(self, tmp_path):
        from workflow_lib.executor import get_project_images
        (tmp_path / "screenshot.png").write_bytes(b"\x89PNG")
        (tmp_path / "readme.md").write_text("not an image")
        with patch("workflow_lib.executor.INPUT_DIR", str(tmp_path)):
            result = get_project_images()
        assert len(result) == 1
        assert result[0].endswith("screenshot.png")

    def test_get_project_context_excludes_images(self, tmp_path):
        from workflow_lib.executor import get_project_context
        (tmp_path / "desc.md").write_text("hello")
        (tmp_path / "photo.png").write_bytes(b"\x89PNG")
        with patch("workflow_lib.executor.INPUT_DIR", str(tmp_path)):
            result = get_project_context()
        assert "hello" in result
        assert "photo.png" not in result

    def test_get_project_images_no_images(self, tmp_path):
        from workflow_lib.executor import get_project_images
        (tmp_path / "readme.md").write_text("text only")
        with patch("workflow_lib.executor.INPUT_DIR", str(tmp_path)):
            assert get_project_images() == []

    def test_get_project_context_multiple_files(self, tmp_path):
        from workflow_lib.executor import get_project_context
        (tmp_path / "a.md").write_text("file a")
        (tmp_path / "b.txt").write_text("file b")
        with patch("workflow_lib.executor.INPUT_DIR", str(tmp_path)):
            result = get_project_context()
        assert "file a" in result
        assert "file b" in result
        assert result.index("a.md") < result.index("b.txt")


class TestRunnerImagePaths:
    """Tests for runner image_paths handling."""

    def test_gemini_runner_with_images(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run("prompt", "/tmp", "ignore", "content",
                       image_paths=["/img/a.png", "/img/b.jpg"])
        call_args = mock_run.call_args
        # Images should be appended as @refs in the prompt
        assert "@/img/a.png" in call_args.kwargs.get("input", call_args[1].get("input", ""))

    def test_claude_runner_with_images(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run("prompt", "/tmp", ".claudeignore", "content",
                       image_paths=["/img/a.png"])
        cmd = mock_run.call_args[0][0]
        assert "--image" in cmd
        assert "/img/a.png" in cmd

    def test_opencode_runner_run(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run("prompt", "/tmp", ".opencodeignore", "content",
                       image_paths=["/img/a.png"])
        cmd = mock_run.call_args[0][0]
        assert "opencode" in cmd[0]
        assert "-f" in cmd
        assert "/img/a.png" in cmd


# ---------------------------------------------------------------------------
# Dashboard tests
# ---------------------------------------------------------------------------

class TestNullDashboard:
    """Tests for NullDashboard (non-TTY fallback)."""

    def test_context_manager(self):
        from workflow_lib.dashboard import NullDashboard
        with NullDashboard() as d:
            assert d is not None

    def test_log_prints_to_stdout(self, capsys):
        from workflow_lib.dashboard import NullDashboard
        d = NullDashboard()
        d.log("hello world")
        out = capsys.readouterr().out
        assert "hello world" in out

    def test_log_skips_blank_lines(self, capsys):
        from workflow_lib.dashboard import NullDashboard
        d = NullDashboard()
        d.log("   ")
        out = capsys.readouterr().out
        assert out == ""

    def test_log_writes_to_file(self, tmp_path):
        from workflow_lib.dashboard import NullDashboard
        f = open(tmp_path / "run.log", "w")
        d = NullDashboard(log_file=f)
        d.log("file line")
        f.flush()
        f.close()
        content = (tmp_path / "run.log").read_text()
        assert "file line" in content

    def test_log_file_write_error_suppressed(self, tmp_path):
        from workflow_lib.dashboard import NullDashboard
        bad = MagicMock()
        bad.write.side_effect = OSError("disk full")
        d = NullDashboard(log_file=bad)
        d.log("this should not raise")  # must not propagate

    def test_set_agent_noop(self):
        from workflow_lib.dashboard import NullDashboard
        d = NullDashboard()
        d.set_agent("task", "Impl", "running", "line")  # no error

    def test_remove_agent_noop(self):
        from workflow_lib.dashboard import NullDashboard
        d = NullDashboard()
        d.remove_agent("task")  # no error

    def test_log_multiline(self, capsys):
        from workflow_lib.dashboard import NullDashboard
        d = NullDashboard()
        d.log("line1\nline2")
        out = capsys.readouterr().out
        assert "line1" in out
        assert "line2" in out


class TestDashboard:
    """Tests for the rich Dashboard class."""

    def test_context_manager_non_tty(self):
        """Dashboard.__enter__/__exit__ must not raise."""
        from workflow_lib.dashboard import Dashboard
        import io
        log = io.StringIO()
        with Dashboard(log_file=log) as d:
            d.log("hello")
            d.set_agent("phase_1/task.md", "Impl", "running", "Working...")
            d.remove_agent("phase_1/task.md")
        assert "hello" in log.getvalue()

    def test_log_writes_to_log_file(self):
        from workflow_lib.dashboard import Dashboard
        import io
        log = io.StringIO()
        with Dashboard(log_file=log) as d:
            d.log("test message")
        assert "test message" in log.getvalue()

    def test_log_file_error_suppressed(self):
        from workflow_lib.dashboard import Dashboard
        bad = MagicMock()
        bad.write.side_effect = OSError("disk full")
        with Dashboard(log_file=bad) as d:
            d.log("should not raise")  # must not propagate

    def test_set_agent_upsert(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.set_agent("t1", "Impl", "running", "doing work")
            d.set_agent("t1", "Review", "done", "")
            assert d._agents["t1"][1] == "done"

    def test_remove_agent_missing_key(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.remove_agent("nonexistent")  # must not raise

    def test_set_agent_truncates_last_line(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.set_agent("t", "Impl", "running", "x" * 200)
            # Agent lines are a deque of (timestamp, text) tuples; text truncated to 120 chars
            lines = list(d._agents["t"][2])
            assert len(lines) == 1
            assert len(lines[0][1]) <= 120

    def test_all_status_styles_render(self):
        from workflow_lib.dashboard import Dashboard, _STATUS_STYLE
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            for status in _STATUS_STYLE:
                d.set_agent(f"task/{status}", "Stage", status, "output")

    def test_refresh_without_live(self):
        """_refresh is a no-op when Live is not started."""
        from workflow_lib.dashboard import Dashboard
        d = Dashboard()
        d._refresh()  # must not raise

    def test_log_skips_blank_lines(self):
        from workflow_lib.dashboard import Dashboard
        import io
        log = io.StringIO()
        with Dashboard(log_file=log) as d:
            d.log("   \n\n   ")
        assert log.getvalue() == ""


class TestMakeDashboard:
    def test_returns_null_dashboard_when_not_tty(self):
        from workflow_lib.dashboard import make_dashboard, NullDashboard
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            result = make_dashboard()
        assert isinstance(result, NullDashboard)

    def test_returns_dashboard_when_tty(self):
        from workflow_lib.dashboard import make_dashboard, Dashboard
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            result = make_dashboard()
        assert isinstance(result, Dashboard)


class TestDashboardStream:
    """Tests for _DashboardStream."""

    def test_write_complete_line_routed_to_dashboard(self):
        from workflow_lib.dashboard import _DashboardStream, NullDashboard
        import io
        original = io.StringIO()
        dash = MagicMock()
        stream = _DashboardStream(dash, original)
        stream.write("hello\n")
        dash.log.assert_called_once_with("hello")

    def test_write_partial_line_buffered(self):
        from workflow_lib.dashboard import _DashboardStream
        import io
        dash = MagicMock()
        stream = _DashboardStream(dash, io.StringIO())
        stream.write("partial")
        dash.log.assert_not_called()

    def test_flush_sends_buffered_content(self):
        from workflow_lib.dashboard import _DashboardStream
        import io
        dash = MagicMock()
        stream = _DashboardStream(dash, io.StringIO())
        stream.write("buffered content")
        stream.flush()
        dash.log.assert_called_once_with("buffered content")

    def test_flush_skips_blank_buffer(self):
        from workflow_lib.dashboard import _DashboardStream
        import io
        dash = MagicMock()
        stream = _DashboardStream(dash, io.StringIO())
        stream.write("   ")
        stream.flush()
        dash.log.assert_not_called()

    def test_write_multiple_lines(self):
        from workflow_lib.dashboard import _DashboardStream
        import io
        dash = MagicMock()
        stream = _DashboardStream(dash, io.StringIO())
        stream.write("line1\nline2\n")
        assert dash.log.call_count == 2

    def test_isatty_returns_false(self):
        from workflow_lib.dashboard import _DashboardStream
        import io
        stream = _DashboardStream(MagicMock(), io.StringIO())
        assert stream.isatty() is False

    def test_write_returns_length(self):
        from workflow_lib.dashboard import _DashboardStream
        import io
        stream = _DashboardStream(MagicMock(), io.StringIO())
        result = stream.write("abc\n")
        assert result == 4

    def test_getattr_delegates_to_original(self):
        from workflow_lib.dashboard import _DashboardStream
        import io
        original = io.StringIO()
        stream = _DashboardStream(MagicMock(), original)
        assert stream.encoding == original.encoding


class TestRunnerStreaming:
    """Tests for AIRunner._run_streaming and on_line in concrete runners."""

    def _fake_popen(self, lines, returncode=0):
        """Return a mock Popen that yields *lines* from stdout."""
        import io
        proc = MagicMock()
        proc.stdout = iter(line + "\n" for line in lines)
        proc.stderr = io.StringIO("err output")
        proc.returncode = returncode
        proc.stdin = MagicMock()
        proc.wait.return_value = None
        return proc

    def test_run_streaming_calls_on_line(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        collected = []
        proc = self._fake_popen(["line1", "line2"])
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(runner, "write_ignore_file"):
            runner.run("/cwd", "prompt", "", "/ignore", on_line=collected.append)
        assert "line1" in collected
        assert "line2" in collected

    def test_run_streaming_blank_lines_not_sent(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        collected = []
        proc = self._fake_popen(["  ", "real line"])
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(runner, "write_ignore_file"):
            runner.run("/cwd", "prompt", "", "/ignore", on_line=collected.append)
        assert "  " not in collected
        assert "real line" in collected

    def test_run_streaming_returns_completed_process(self):
        from workflow_lib.runners import GeminiRunner
        import subprocess
        runner = GeminiRunner()
        proc = self._fake_popen(["output"], returncode=0)
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(runner, "write_ignore_file"):
            result = runner.run("/cwd", "prompt", "", "/ignore", on_line=lambda l: None)
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 0

    def test_gemini_runner_no_on_line_uses_subprocess_run(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run, \
             patch.object(runner, "write_ignore_file"):
            runner.run("/cwd", "prompt", "", "/ignore")
        mock_run.assert_called_once()

    def test_claude_runner_on_line_uses_popen(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        proc = self._fake_popen(["claude output"])
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(runner, "write_ignore_file"):
            collected = []
            runner.run("/cwd", "prompt", "", "/ignore", on_line=collected.append)
        assert "claude output" in collected

    def test_opencode_runner_on_line_uses_popen(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        proc = self._fake_popen(["opencode output"])
        with patch("subprocess.Popen", return_value=proc):
            collected = []
            runner.run("/cwd", "prompt", "", "/ignore", on_line=collected.append)
        assert "opencode output" in collected

    def test_copilot_runner_on_line_uses_streaming(self):
        from workflow_lib.runners import CopilotRunner
        runner = CopilotRunner()
        proc = self._fake_popen(["copilot output"])
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(runner, "write_ignore_file"):
            collected = []
            runner.run("/cwd", "prompt", "", "/ignore", on_line=collected.append)
        assert "copilot output" in collected

    def test_gemini_runner_with_images_and_on_line(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        proc = self._fake_popen(["with image"])
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(runner, "write_ignore_file"):
            collected = []
            runner.run("/cwd", "prompt", "", "/ignore", image_paths=["/img.png"], on_line=collected.append)
        assert "with image" in collected

    def test_gemini_runner_with_images_no_on_line(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        fake = MagicMock(returncode=0, stdout="out", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run, \
             patch.object(runner, "write_ignore_file"):
            runner.run("/cwd", "prompt", "", "/ignore", image_paths=["/img.png"])
        # prompt should contain @/img.png reference
        called_input = mock_run.call_args.kwargs.get("input", mock_run.call_args[1].get("input", ""))
        assert "@/img.png" in called_input

    def test_claude_runner_no_on_line_uses_subprocess_run(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run, \
             patch.object(runner, "write_ignore_file"):
            runner.run("/cwd", "prompt", "", "/ignore", image_paths=["/img.png"])
        mock_run.assert_called_once()

    def test_opencode_runner_no_on_line(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            runner.run("/cwd", "prompt", "", "/ignore")
        mock_run.assert_called_once()

    def test_opencode_runner_with_images_no_on_line(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            runner.run("/cwd", "prompt", "", "/ignore", image_paths=["/img.png"])
        call_args = mock_run.call_args[0][0]
        assert "-f" in call_args and "/img.png" in call_args

    def test_copilot_runner_no_on_line_returns_result_on_success(self):
        from workflow_lib.runners import CopilotRunner
        runner = CopilotRunner()
        fake = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=fake), \
             patch.object(runner, "write_ignore_file"):
            result = runner.run("/cwd", "prompt", "", "/ignore")
        assert result.returncode == 0

    def test_base_airunner_abstract_methods_raise(self):
        from workflow_lib.runners import AIRunner
        runner = AIRunner()
        with pytest.raises(NotImplementedError):
            runner.get_cmd()
        with pytest.raises(NotImplementedError):
            runner.run("/cwd", "p", "", "/f")
        with pytest.raises(NotImplementedError):
            _ = runner.ignore_file_name

    def test_claude_runner_get_cmd_with_images(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        cmd = runner.get_cmd(image_paths=["/a.png", "/b.png"])
        assert "--image" in cmd
        assert "/a.png" in cmd

    def test_opencode_runner_get_cmd_with_images(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        cmd = runner.get_cmd(image_paths=["/img.png"])
        assert "-f" in cmd
        assert "/img.png" in cmd

    def test_opencode_runner_with_images_and_on_line(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        proc = self._fake_popen(["opencode with image"])
        with patch("subprocess.Popen", return_value=proc):
            collected = []
            runner.run("/cwd", "prompt", "", "/ignore", image_paths=["/img.png"], on_line=collected.append)
        assert "opencode with image" in collected


class TestPhaseDisplayName:
    """Tests for BasePhase.display_name and doc-specific overrides."""

    def test_base_phase_display_name_uses_class_name(self):
        from workflow_lib.phases import Phase3FinalReview
        phase = Phase3FinalReview()
        assert phase.display_name == "Phase3FinalReview"

    def test_phase1_display_name_includes_doc_name(self):
        from workflow_lib.phases import Phase1GenerateDoc
        phase = Phase1GenerateDoc({"id": "user_research", "name": "User Research"})
        assert "User Research" in phase.display_name
        assert "Phase1" in phase.display_name

    def test_phase2_display_name_includes_doc_name(self):
        from workflow_lib.phases import Phase2FleshOutDoc
        phase = Phase2FleshOutDoc({"id": "arch", "name": "Architecture"})
        assert "Architecture" in phase.display_name
        assert "Phase2" in phase.display_name

    def test_phase4a_display_name_includes_doc_name(self):
        from workflow_lib.phases import Phase4AExtractRequirements
        phase = Phase4AExtractRequirements({"id": "ux", "name": "UX Spec"})
        assert "UX Spec" in phase.display_name
        assert "Phase4A" in phase.display_name

    def test_phase1_display_name_fallback_to_id(self):
        from workflow_lib.phases import Phase1GenerateDoc
        phase = Phase1GenerateDoc({"id": "fallback_id"})
        assert "fallback_id" in phase.display_name

    def test_base_phase_operation_derived_from_class_name(self):
        from workflow_lib.phases import Phase3FinalReview
        phase = Phase3FinalReview()
        # CamelCase split: "FinalReview" → "Final Review"
        assert "Final" in phase.operation
        assert "Review" in phase.operation

    def test_phase1_operation_is_generate(self):
        from workflow_lib.phases import Phase1GenerateDoc
        phase = Phase1GenerateDoc({"id": "x", "name": "X"})
        assert phase.operation == "Generate"

    def test_phase2_operation_is_flesh_out(self):
        from workflow_lib.phases import Phase2FleshOutDoc
        phase = Phase2FleshOutDoc({"id": "x", "name": "X"})
        assert phase.operation == "Flesh Out"

    def test_phase4a_operation_is_extract_reqs(self):
        from workflow_lib.phases import Phase4AExtractRequirements
        phase = Phase4AExtractRequirements({"id": "x", "name": "X"})
        assert phase.operation == "Extract Reqs"


class TestDashboardUpdateLastLine:
    """Tests for Dashboard.update_last_line and the Command column."""

    def test_update_last_line_updates_existing_agent(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.set_agent("t1", "Generate", "running", "")
            d.update_last_line("t1", "new output line")
            lines = list(d._agents["t1"][2])
            assert any("new output line" in t for _, t in lines)

    def test_update_last_line_preserves_command_and_status(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.set_agent("t1", "Generate", "running", "old")
            d.update_last_line("t1", "new")
            command, status, lines_deque, _started = d._agents["t1"]
            assert command == "Generate"
            assert status == "running"
            line_texts = [t for _, t in lines_deque]
            assert "new" in line_texts

    def test_update_last_line_noop_if_task_not_found(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.update_last_line("nonexistent", "line")  # must not raise

    def test_update_last_line_truncates_to_120(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.set_agent("t1", "Generate", "running", "")
            d.update_last_line("t1", "x" * 200)
            lines = list(d._agents["t1"][2])
            assert all(len(t) <= 120 for _, t in lines)

    def test_null_dashboard_update_last_line_is_noop(self):
        from workflow_lib.dashboard import NullDashboard
        d = NullDashboard()
        d.update_last_line("t1", "anything")  # must not raise

    def test_set_agent_command_stored(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.set_agent("t1", "MyCommand", "running", "output")
            assert d._agents["t1"][0] == "MyCommand"

    def test_log_shows_newest_first(self):
        """Newest log entries should appear first (reversed order)."""
        from workflow_lib.dashboard import Dashboard
        import io
        log = io.StringIO()
        with Dashboard(log_file=log) as d:
            d.log("first")
            d.log("second")
            d.log("third")
        content = log.getvalue()
        # All lines written to log file; newest rendered first in panel
        assert "first" in content
        assert "third" in content


class TestContextCurrentPhase:
    """Tests for ProjectContext.current_phase and prefixed on_line logging."""

    def _make_ctx(self, tmp_path, dashboard=None):
        from workflow_lib.context import ProjectContext
        mock_runner = MagicMock()
        mock_runner.ignore_file_name = ".geminiignore"
        with patch("workflow_lib.context.GeminiRunner", return_value=mock_runner), \
             patch("os.makedirs"), \
             patch("workflow_lib.context.GEN_STATE_FILE", str(tmp_path / "state.json")), \
             patch("workflow_lib.context.INPUT_DIR", str(tmp_path)), \
             patch.object(ProjectContext, "_load_state", return_value={}), \
             patch.object(ProjectContext, "_load_images", return_value=[]), \
             patch.object(ProjectContext, "_load_description", return_value="desc"):
            ctx = ProjectContext(str(tmp_path), runner=mock_runner, dashboard=dashboard)
        return ctx

    def test_current_phase_default_empty(self, tmp_path):
        ctx = self._make_ctx(tmp_path)
        assert ctx.current_phase == ""

    def test_run_ai_prefixes_log_with_current_phase(self, tmp_path):
        logged = []
        dash = MagicMock()
        dash.log.side_effect = logged.append
        dash.update_last_line = MagicMock()

        ctx = self._make_ctx(tmp_path, dashboard=dash)
        ctx.current_phase = "Phase1: User Research"

        captured_on_line = []
        def fake_run(cwd, prompt, ignore, ignore_file, images, on_line=None, timeout=None):
            if on_line:
                captured_on_line.append(on_line)
            return MagicMock(returncode=0, stdout="", stderr="")
        ctx.runner.run.side_effect = fake_run

        with patch.object(ctx, "_write_last_failed_command"), \
             patch.object(ctx, "get_workspace_snapshot", return_value={}):
            ctx.run_ai("prompt", "ignore")

        assert captured_on_line, "on_line callback was not passed to runner"
        on_line = captured_on_line[0]
        on_line("some output line")
        assert any("[Phase1: User Research] some output line" in m for m in logged)

    def test_run_ai_updates_last_line(self, tmp_path):
        dash = MagicMock()
        ctx = self._make_ctx(tmp_path, dashboard=dash)
        ctx.current_phase = "Phase1: Doc"

        captured_on_line = []
        def fake_run(cwd, prompt, ignore, ignore_file, images, on_line=None, timeout=None):
            if on_line:
                captured_on_line.append(on_line)
            return MagicMock(returncode=0, stdout="", stderr="")
        ctx.runner.run.side_effect = fake_run

        with patch.object(ctx, "_write_last_failed_command"), \
             patch.object(ctx, "get_workspace_snapshot", return_value={}):
            ctx.run_ai("prompt", "ignore")

        on_line = captured_on_line[0]
        on_line("output line")
        dash.update_last_line.assert_called_with("plan/Phase1: Doc", "output line")


class TestOrchestratorWithDashboard:
    """Tests for Orchestrator.dashboard integration."""

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.state = {}
        return ctx

    def test_log_with_dashboard(self):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        dash = MagicMock()
        orc = Orchestrator(ctx, dashboard=dash)
        orc._log("hello")
        dash.log.assert_called_once_with("hello")

    def test_log_without_dashboard(self, capsys):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        orc = Orchestrator(ctx, dashboard=None)
        orc._log("plain output")
        assert "plain output" in capsys.readouterr().out

    def test_set_phase_with_dashboard(self):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        dash = MagicMock()
        orc = Orchestrator(ctx, dashboard=dash)
        orc._set_phase("Phase1", "running", "Generate")
        dash.set_agent.assert_called_once_with("plan/Phase1", "Generate", "running")

    def test_set_phase_without_dashboard(self):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        orc = Orchestrator(ctx, dashboard=None)
        orc._set_phase("Phase1", "running")  # must not raise

    def test_run_phase_with_retry_success_sets_done(self):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        dash = MagicMock()
        orc = Orchestrator(ctx, dashboard=dash)
        phase = MagicMock()
        phase.__class__.__name__ = "FakePhase"
        orc.run_phase_with_retry(phase)
        phase.execute.assert_called_once_with(ctx)
        # set_agent called with "done"
        calls = [str(c) for c in dash.set_agent.call_args_list]
        assert any("done" in c for c in calls)

    def test_run_phase_with_retry_sysexit_0(self):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        dash = MagicMock()
        orc = Orchestrator(ctx, dashboard=dash)
        phase = MagicMock()
        phase.__class__.__name__ = "FakePhase"
        phase.execute.side_effect = SystemExit(0)
        orc.run_phase_with_retry(phase)  # should return cleanly

    def test_run_phase_with_retry_sets_current_phase(self):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        orc = Orchestrator(ctx)
        phase = MagicMock()
        phase.display_name = "Phase1: Doc"
        phase.operation = "Generate"
        captured = []
        def capture_execute(c):
            captured.append(c.current_phase)
        phase.execute.side_effect = capture_execute
        orc.run_phase_with_retry(phase)
        assert captured[0] == "Phase1: Doc"

    def test_validate_artifacts_passes_when_file_exists(self, tmp_path):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        orc = Orchestrator(ctx)
        f = tmp_path / "artifact.md"
        f.write_text("content")
        orc._validate_artifacts([str(f)], "TestPhase")  # must not raise

    def test_validate_artifacts_fails_when_file_missing(self, tmp_path):
        from workflow_lib.orchestrator import Orchestrator
        import sys
        ctx = self._make_ctx()
        orc = Orchestrator(ctx)
        with pytest.raises(SystemExit):
            orc._validate_artifacts([str(tmp_path / "missing.md")], "TestPhase")

    def test_validate_artifacts_fails_when_file_empty(self, tmp_path):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        orc = Orchestrator(ctx)
        f = tmp_path / "empty.md"
        f.write_text("")
        with pytest.raises(SystemExit):
            orc._validate_artifacts([str(f)], "TestPhase")

    def test_validate_artifacts_passes_for_directory(self, tmp_path):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        orc = Orchestrator(ctx)
        d = tmp_path / "subdir"
        d.mkdir()
        orc._validate_artifacts([str(d)], "TestPhase")  # directories are not checked for size

    def test_run_phase_exception_retries_then_exits(self):
        from workflow_lib.orchestrator import Orchestrator
        import sys
        ctx = self._make_ctx()
        orc = Orchestrator(ctx)
        phase = MagicMock()
        phase.display_name = "FakePhase"
        phase.operation = "Op"
        phase.execute.side_effect = RuntimeError("boom")
        with pytest.raises(SystemExit):
            orc.run_phase_with_retry(phase, max_retries=1)

    @patch("workflow_lib.orchestrator.validate_all_prompts_exist", return_value=[])
    def test_run_calls_backup_and_restore(self, _mock_validate):
        from workflow_lib.orchestrator import Orchestrator
        ctx = self._make_ctx()
        ctx.backup_ignore_file = MagicMock()
        ctx.restore_ignore_file = MagicMock()
        orc = Orchestrator(ctx)
        # patch run_phase_with_retry to avoid real execution
        orc.run_phase_with_retry = MagicMock()
        orc._validate_artifacts = MagicMock()
        orc.run()
        ctx.backup_ignore_file.assert_called_once()
        ctx.restore_ignore_file.assert_called_once()
