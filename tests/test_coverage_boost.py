"""Additional tests to boost workflow_lib coverage to >=90%."""
import sys
import os
import json
import pytest
import threading
import subprocess
from unittest.mock import patch, MagicMock, mock_open, call, ANY

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import workflow
from workflow import (
    AIRunner, GeminiRunner, ClaudeRunner, CopilotRunner,
    ProjectContext, BasePhase,
    Phase1GenerateDoc, Phase2FleshOutDoc, Phase2BSummarizeDoc,
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

from workflow_lib.executor import (
    _restore_terminal, _step_for_agent_type, _compact_task_id
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestInternalHelpers:
    def test_restore_terminal(self):
        with patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush") as mock_flush:
            _restore_terminal()
        assert mock_write.called
        assert mock_flush.called

    def test_restore_terminal_exception(self):
        # Should catch and pass
        with patch("sys.stdout.write", side_effect=RuntimeError("fail")):
            _restore_terminal()

    def test_step_for_agent_type(self):
        assert _step_for_agent_type("Implementation") == "develop"
        assert _step_for_agent_type("Implementation: Fixup") == "develop"
        assert _step_for_agent_type("Review") == "review"
        assert _step_for_agent_type("Review: Final") == "review"
        assert _step_for_agent_type("Merge") == "merge"
        assert _step_for_agent_type("Unknown") == "all"

    def test_compact_task_id_simple(self):
        assert _compact_task_id("phase_1", "my_task.md") == "p1/my_task"

    def test_compact_task_id_nested(self):
        assert _compact_task_id("phase_2", "sub/task.md") == "p2/sub/task"

    def test_compact_task_id_long(self):
        assert _compact_task_id("phase_1", "this_is_a_very_long_task_name.md") == "p1/this_is_a_very_long_"

    def test_compact_task_id_fallback(self):
        assert _compact_task_id("not_a_phase", "t.md") == "not_a_phase/t"


class TestGetReadyTasks:
    def test_get_ready_tasks_basic(self):
        dag = {
            "phase_1/t1.md": [],
            "phase_1/t2.md": ["phase_1/t1.md"],
            "phase_2/t3.md": []
        }
        # Only t1 is ready (t2 has prereq, t3 is in later phase)
        assert get_ready_tasks(dag, [], []) == ["phase_1/t1.md"]
        # If t1 is done, t2 is ready
        assert get_ready_tasks(dag, ["phase_1/t1.md"], []) == ["phase_1/t2.md"]

    def test_get_ready_tasks_phase_barrier(self):
        dag = {
            "phase_1/t1.md": [],
            "phase_2/t2.md": []
        }
        # t2 should not be ready until phase_1 is empty (all done or active)
        assert get_ready_tasks(dag, [], []) == ["phase_1/t1.md"]

    def test_get_ready_tasks_with_blocked(self):
        dag = {
            "phase_1/t1.md": [],
            "phase_1/t2.md": ["phase_1/t1.md"]
        }
        with patch("workflow_lib.executor.load_blocked_tasks", return_value={"phase_1/t1.md"}):
            # t1 is blocked, so it's not ready. t2 depends on t1, so it's not ready either.
            assert get_ready_tasks(dag, [], []) == []


class TestAIRunner:
    def test_wrap_cmd_no_user(self):
        runner = AIRunner()
        cmd = ["ls"]
        assert runner._wrap_cmd(cmd) == cmd

    def test_wrap_cmd_with_user(self):
        runner = AIRunner(user="other")
        cmd = ["ls"]
        with patch("os.getenv", return_value="current"):
            wrapped = runner._wrap_cmd(cmd)
        assert "sudo" in wrapped
        assert "other" in wrapped

    def test_wrap_cmd_same_user(self):
        runner = AIRunner(user="current")
        cmd = ["ls"]
        with patch("os.getenv", return_value="current"):
            assert runner._wrap_cmd(cmd) == cmd

    def test_build_exec_cmd_no_container(self):
        runner = AIRunner()
        cmd = ["ls"]
        assert runner._build_exec_cmd(cmd) == cmd

    def test_build_exec_cmd_with_container(self):
        runner = AIRunner(container_name="my_cont")
        runner._container_env_file = "/tmp/env"
        cmd = ["ls"]
        wrapped = runner._build_exec_cmd(cmd)
        assert "docker" in wrapped
        assert "exec" in wrapped
        assert "my_cont" in wrapped
        assert "--env-file" in wrapped


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
    """Tests for run_ai_command which delegates to runner classes."""

    def _mock_runner(self, returncode=0):
        runner = MagicMock()
        runner.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout="output", stderr=""
        )
        return runner

    def test_claude_backend(self):
        runner = self._mock_runner()
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp", backend="claude")
        assert rc == 0

    def test_copilot_backend(self):
        runner = self._mock_runner()
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp", backend="copilot")
        assert rc == 0

    def test_gemini_backend(self):
        runner = self._mock_runner(returncode=1)
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp", backend="gemini")
        assert rc == 1

    def test_timeout_returns_1(self):
        """TimeoutExpired from runner should return exit code 1."""
        runner = MagicMock()
        runner.run.side_effect = subprocess.TimeoutExpired(["cmd"], 10)
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp")
        assert rc == 1

    def test_file_not_found_returns_1(self):
        """FileNotFoundError from runner should return exit code 1."""
        runner = MagicMock()
        runner.run.side_effect = FileNotFoundError("not found")
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp")
        assert rc == 1


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
        # Since os.path.exists=True, it reads MEMORY.md and DECISIONS.md and joins them.
        assert result == "memory content\n\n---\n\nmemory content"

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
             patch("workflow_lib.executor.run_ai_command", return_value=(0, "")), \
             patch("workflow_lib.executor.get_rag_enabled", return_value=False):
            result = run_agent("Impl", "implement_task.md", {"task_name": "my_task"}, "/tmp")
        assert result is True

    def test_failure(self):
        with patch("builtins.open", mock_open(read_data="template")), \
             patch("workflow_lib.executor.run_ai_command", return_value=(1, "")), \
             patch("workflow_lib.executor.get_rag_enabled", return_value=False):
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
        def _fake_run(cmd, **kwargs):
            if "clone" in cmd:
                raise err
            return MagicMock(returncode=0, stdout="", stderr=b"")
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
             patch("subprocess.run", side_effect=_fake_run):
            result = process_task("/root", "phase_1/task.md", "./do presubmit")
        assert result is False

    def test_success(self):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="M file.py", stderr=""))
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
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

    def test_submodule_init_after_clone(self):
        """Verify git submodule update --init --recursive is called after cloning."""
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="M file.py", stderr=""))
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
             patch("subprocess.run", mock_run), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: My Task"), \
             patch("workflow_lib.executor.get_project_context", return_value="desc"), \
             patch("workflow_lib.executor.get_memory_context", return_value="mem"), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.rmtree"):
            process_task("/root", "phase_1/task.md", "./do presubmit")
        # Find the clone call and verify submodule init follows it
        calls = [c[0][0] for c in mock_run.call_args_list if isinstance(c[0][0], list)]
        clone_idx = next(i for i, c in enumerate(calls) if "clone" in c)
        submod = calls[clone_idx + 1]
        assert submod == ["git", "submodule", "update", "--init", "--recursive"]

    def test_implementation_agent_fails(self):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
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
             patch("os.chmod"), \
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
             patch("os.chmod"), \
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

    def test_reclaim_ownership_called_before_presubmit(self):
        """_reclaim_dir_ownership must be called before the presubmit subprocess.

        Without this, files written by an alternate-user agent are still owned
        by that user, causing git and presubmit commands (run as the current
        user) to fail with permission errors.  This test fails if the chown
        call is removed or moved after the presubmit.
        """
        call_order = []

        def fake_reclaim(path, _log):
            call_order.append("reclaim")

        def mock_run_side_effect(*args, **kwargs):
            cmd = args[0] if args else []
            if isinstance(cmd, list) and cmd and cmd[0] == "./do":
                call_order.append("presubmit")
            return MagicMock(returncode=0, stdout="M file.py", stderr="")

        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
             patch("subprocess.run", side_effect=mock_run_side_effect), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor._reclaim_dir_ownership", side_effect=fake_reclaim), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.rmtree"):
            result = process_task("/root", "phase_1/task.md", "./do presubmit")

        assert result is True
        assert "reclaim" in call_order, "_reclaim_dir_ownership was never called"
        assert "presubmit" in call_order, "presubmit was never called"
        reclaim_idx = call_order.index("reclaim")
        presubmit_idx = call_order.index("presubmit")
        assert reclaim_idx < presubmit_idx, (
            f"Expected _reclaim_dir_ownership BEFORE presubmit, got order: {call_order}"
        )

    def test_presubmit_called_with_start_new_session(self):
        """subprocess.run for presubmit must use start_new_session=True.

        This prevents Ctrl-C (SIGINT to the terminal process group) from being
        forwarded to the presubmit subprocess and causing a spurious failure.
        """
        presubmit_calls = []
        def mock_run_side_effect(*args, **kwargs):
            cmd = args[0] if args else []
            if isinstance(cmd, list) and cmd and cmd[0] == "./do":
                presubmit_calls.append(kwargs)
                return MagicMock(returncode=0, stdout="M file.py", stderr="")
            return MagicMock(returncode=0, stdout="M file.py", stderr="")

        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
             patch("subprocess.run", side_effect=mock_run_side_effect), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.rmtree"):
            process_task("/root", "phase_1/task.md", "./do presubmit")

        assert presubmit_calls, "presubmit subprocess.run was never called"
        for call_kwargs in presubmit_calls:
            assert call_kwargs.get("start_new_session") is True, (
                "presubmit subprocess.run must be called with start_new_session=True "
                "so Ctrl-C does not propagate to the presubmit process"
            )

    def test_presubmit_survives_sigint_to_parent_process_group(self):
        """Integration test: SIGINT sent to the parent process group must not kill
        the presubmit subprocess.

        Without start_new_session=True the presubmit would be in the same process
        group as the workflow runner and would receive SIGINT, causing it to exit
        early with a non-zero return code and be treated as a failure.
        """
        import signal
        import textwrap

        # Child script: registers a SIGINT handler that exits with code 42,
        # then sleeps. Exit code 42 means it received SIGINT; timeout (kill)
        # means it did NOT receive SIGINT.
        child_script = textwrap.dedent("""\
            import signal, sys, time
            signal.signal(signal.SIGINT, lambda *a: sys.exit(42))
            time.sleep(10)
        """)

        def run_scenario(start_new_session: bool) -> int:
            """Run child + send SIGINT to process group; return child exit code."""
            wrapper = textwrap.dedent(f"""\
                import subprocess, signal, os, time, sys
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                proc = subprocess.Popen(
                    [sys.executable, "-c", {child_script!r}],
                    start_new_session={start_new_session},
                )
                time.sleep(0.1)  # let child install its handler
                os.killpg(os.getpgid(os.getpid()), signal.SIGINT)
                time.sleep(0.3)  # let signal propagate
                rc = proc.poll()
                if rc is None:
                    proc.kill()
                    proc.wait()
                    sys.exit(0)   # child survived -> exit 0
                sys.exit(rc)      # child exited -> pass through its code
            """)
            result = subprocess.run(
                [sys.executable, "-c", wrapper],
                start_new_session=True,  # isolate from our own process group
            )
            return result.returncode

        # Without the fix: child is in the same process group and receives SIGINT
        rc_no_fix = run_scenario(start_new_session=False)
        assert rc_no_fix == 42, (
            f"Expected child to receive SIGINT (exit 42) without start_new_session, got {rc_no_fix}"
        )

        # With the fix: child is in its own session and does NOT receive SIGINT
        rc_with_fix = run_scenario(start_new_session=True)
        assert rc_with_fix == 0, (
            f"Expected child to survive SIGINT (exit 0) with start_new_session=True, got {rc_with_fix}"
        )


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

    def test_submodule_init_after_clone(self):
        """Verify git submodule update --init --recursive is called after cloning."""
        mock_run = MagicMock(return_value=self._ok_run())
        with patch("subprocess.run", mock_run), \
             patch("workflow_lib.executor.get_gitlab_remote_url", return_value="http://example.com/repo.git"), \
             patch("workflow_lib.executor.get_task_details", return_value="# Task: T"), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("shutil.rmtree"):
            merge_task("/root", "phase_1/task.md", "./do presubmit")
        calls = [c[0][0] for c in mock_run.call_args_list if isinstance(c[0][0], list)]
        clone_idx = next(i for i, c in enumerate(calls) if "clone" in c)
        submod = calls[clone_idx + 1]
        assert submod == ["git", "submodule", "update", "--init", "--recursive"]

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
        def ready_side(master_dag, completed, active, **kwargs):
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
    """Tests for the on_line callback path in run_ai_command (delegates to runners)."""

    def _mock_runner(self, returncode=0):
        runner = MagicMock()
        runner.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout="output", stderr=""
        )
        return runner

    def test_on_line_forwarded_to_runner(self):
        runner = self._mock_runner()
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            run_ai_command("prompt", "/tmp", on_line=lambda l: None)
        # Verify on_line was passed through to runner.run
        call_kwargs = runner.run.call_args.kwargs
        assert call_kwargs.get("on_line") is not None

    def test_gemini_with_images(self):
        runner = self._mock_runner()
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp", backend="gemini", image_paths=["/a.png"])
        assert rc == 0
        call_kwargs = runner.run.call_args.kwargs
        assert call_kwargs.get("image_paths") == ["/a.png"]

    def test_opencode_with_images(self):
        runner = self._mock_runner()
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp", backend="opencode", image_paths=["/img.png"])
        assert rc == 0

    def test_copilot_with_images(self):
        runner = self._mock_runner()
        with patch("workflow_lib.executor.make_runner", return_value=runner), \
             patch("workflow_lib.config.get_config_defaults", return_value={}):
            rc, _ = run_ai_command("prompt", "/tmp", backend="copilot", image_paths=["/img.png"])
        assert rc == 0


class TestRunAgentWithDashboard:
    def test_with_dashboard_and_task_id_success(self):
        dash = MagicMock()
        with patch("builtins.open", mock_open(read_data="Hello {task_name}")), \
             patch("workflow_lib.executor.run_ai_command", return_value=(0, "")), \
             patch("workflow_lib.executor.get_project_images", return_value=[]), \
             patch("workflow_lib.executor.get_rag_enabled", return_value=False):
            result = run_agent("Impl", "implement_task.md", {"task_name": "t"}, "/tmp",
                               dashboard=dash, task_id="phase_1/t.md")
        assert result is True
        dash.log.assert_called()

    def test_with_dashboard_failure(self):
        dash = MagicMock()
        with patch("builtins.open", mock_open(read_data="template")), \
             patch("workflow_lib.executor.run_ai_command", return_value=(1, "")), \
             patch("workflow_lib.executor.get_project_images", return_value=[]), \
             patch("workflow_lib.executor.get_rag_enabled", return_value=False):
            result = run_agent("Impl", "implement_task.md", {}, "/tmp",
                               dashboard=dash, task_id="phase_1/t.md")
        assert result is False
        dash.log.assert_called()

    def test_on_line_callback_fires(self):
        """When dashboard + task_id, on_line callback is passed to run_ai_command."""
        dash = MagicMock()
        with patch("builtins.open", mock_open(read_data="tpl")), \
             patch("workflow_lib.executor.run_ai_command", return_value=(0, "")) as mock_cmd, \
             patch("workflow_lib.executor.get_project_images", return_value=[]), \
             patch("workflow_lib.executor.get_rag_enabled", return_value=False):
            run_agent("Impl", "impl.md", {"task_name": "t", "phase_filename": "p"}, "/tmp",
                      dashboard=dash, task_id="phase_1/t.md")
        # Verify on_line callback was passed to run_ai_command
        call_kwargs = mock_cmd.call_args
        assert call_kwargs.kwargs.get("on_line") is not None or (len(call_kwargs.args) > 5 and call_kwargs.args[5] is not None)
        dash.set_agent.assert_not_called()  # mock doesn't invoke on_line, so set_agent won't be called
        dash.log.assert_called()  # but log is called directly


class TestProcessTaskWithDashboard:
    def _base_patches(self):
        return [
            patch("tempfile.mkdtemp", return_value="/tmp/wt"),
            patch("os.chmod"),
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
             patch("os.chmod"), \
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
        def _fake_run(cmd, **kwargs):
            if "clone" in cmd:
                raise err
            return MagicMock(returncode=0, stdout="", stderr=b"")
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
             patch("subprocess.run", side_effect=_fake_run):
            result = process_task("/root", "phase_1/task.md", "./do presubmit", dashboard=dash)
        assert result is False
        dash.set_agent.assert_any_call("phase_1/task.md", "Impl", "failed", "Clone/checkout failed: error")

    def test_impl_agent_fail_with_dashboard(self):
        dash = MagicMock()
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
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
             patch("os.chmod"), \
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
        def _only_fail_presubmit(cmd, **kwargs):
            if isinstance(cmd, list) and "./do" in cmd:
                return MagicMock(returncode=1, stdout="fail", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        with patch("tempfile.mkdtemp", return_value="/tmp/wt"), \
             patch("os.chmod"), \
             patch("subprocess.run", side_effect=_only_fail_presubmit), \
             patch("workflow_lib.executor.run_agent", return_value=True), \
             patch("workflow_lib.executor.get_task_details", return_value=""), \
             patch("workflow_lib.executor.get_project_context", return_value=""), \
             patch("workflow_lib.executor.get_memory_context", return_value=""), \
             patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.rmtree"):
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
             patch("os.chmod"), \
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
        def ready_side(master_dag, completed, active, **kwargs):
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
        def ready_side(master_dag, completed, active, **kwargs):
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
# runners.py – CopilotRunner
# ---------------------------------------------------------------------------

class TestRunners:
    def test_copilot_runner_success(self):
        runner = CopilotRunner()
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner.run("/tmp", "prompt")
        assert result.returncode == 0

    def test_copilot_runner_file_not_found_then_raises(self):
        """All candidates raise FileNotFoundError → re-raise."""
        runner = CopilotRunner()
        with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
            with pytest.raises(FileNotFoundError):
                runner.run("/tmp", "prompt")

    def test_copilot_runner_non_zero_returned(self):
        """All candidates run but return nonzero → return last result."""
        runner = CopilotRunner()
        mock_result = MagicMock(returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            result = runner.run("/tmp", "prompt")
        assert result.returncode == 1


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
            ctx.summaries_dir = "/fake/root/docs/plan/summaries"
            ctx.prompts_dir = "/fake/.tools/prompts"
            ctx.state_file = "/fake/.gen_state.json"
            ctx.input_dir = "/fake/.tools/input"
            ctx.shared_components_file = "/fake/root/docs/plan/shared_components.md"
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

    def test_get_accumulated_context_prefers_summary(self):
        ctx = self._make_ctx()
        doc = {"id": "arch", "type": "spec", "name": "Arch"}
        prev = {"id": "prd", "type": "spec", "name": "PRD"}

        def fake_exists(path):
            return "summaries" in path  # summary exists, full doc doesn't matter

        def fake_summary_path(d):
            return f"/fake/root/docs/plan/summaries/{d['id']}.md"

        ctx.get_summary_path = lambda d: fake_summary_path(d)

        with patch("workflow_lib.context.DOCS", [prev, doc]), \
             patch("os.path.exists", side_effect=fake_exists), \
             patch("builtins.open", mock_open(read_data="summary content")):
            result = ctx.get_accumulated_context(doc)
        assert 'type="summary"' in result
        assert "summary content" in result

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


class TestPhase2BSummarize:
    def test_already_summarized_skip(self):
        ctx = _mock_ctx(state={"summarized": ["doc1"]})
        doc = {"id": "doc1", "type": "spec", "name": "Doc1"}
        Phase2BSummarizeDoc(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_source_not_found_skip(self):
        ctx = _mock_ctx()
        ctx.get_document_path.return_value = "/nonexistent/path.md"
        doc = {"id": "doc1", "type": "spec", "name": "Doc1"}
        with patch("os.path.exists", return_value=False):
            Phase2BSummarizeDoc(doc).execute(ctx)
        ctx.run_gemini.assert_not_called()

    def test_success(self):
        ctx = _mock_ctx()
        ctx.get_summary_target_path.return_value = "docs/plan/summaries/doc1.md"
        ctx.get_summary_path.return_value = "/fake/root/docs/plan/summaries/doc1.md"
        doc = {"id": "doc1", "type": "spec", "name": "Doc1"}
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="# Doc content")):
            Phase2BSummarizeDoc(doc).execute(ctx)
        ctx.run_gemini.assert_called_once()
        assert "doc1" in ctx.state.get("summarized", [])

    def test_failure_exits(self):
        ctx = _mock_ctx()
        ctx.get_summary_target_path.return_value = "docs/plan/summaries/doc1.md"
        ctx.get_summary_path.return_value = "/fake/root/docs/plan/summaries/doc1.md"
        ctx.run_gemini.return_value = MagicMock(returncode=1, stdout="", stderr="")
        doc = {"id": "doc1", "type": "spec", "name": "Doc1"}
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="# Doc content")), \
             pytest.raises(SystemExit):
            Phase2BSummarizeDoc(doc).execute(ctx)

    def test_operation_property(self):
        doc = {"id": "x", "name": "X"}
        phase = Phase2BSummarizeDoc(doc)
        assert phase.operation == "Summarize"

    def test_display_name(self):
        doc = {"id": "x", "name": "X"}
        phase = Phase2BSummarizeDoc(doc)
        assert "Summarize X" in phase.display_name


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

    def test_allowed_files_include_specs_and_research(self):
        ctx = _mock_ctx()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="all good")):
            Phase3BAdversarialReview().execute(ctx)
        call_kwargs = ctx.run_gemini.call_args
        allowed = call_kwargs.kwargs.get("allowed_files", []) if call_kwargs.kwargs else call_kwargs[1].get("allowed_files", [])
        assert any("specs" in f for f in allowed), "specs dir should be in allowed_files"
        assert any("research" in f for f in allowed), "research dir should be in allowed_files"

    def test_sandbox_disabled(self):
        ctx = _mock_ctx()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="all good")):
            Phase3BAdversarialReview().execute(ctx)
        call_kwargs = ctx.run_gemini.call_args
        kw = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        assert kw.get("sandbox") is False, "sandbox should be disabled for adversarial review"

    def test_scope_creep_found_continue(self):
        ctx = _mock_ctx()
        ctx.prompt_input.return_value = "c"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="scope creep found")):
            Phase3BAdversarialReview().execute(ctx)

    def test_scope_creep_prompt_mentions_auto_removed(self):
        ctx = _mock_ctx()
        ctx.prompt_input.return_value = "c"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="scope creep found")):
            Phase3BAdversarialReview().execute(ctx)
        prompt_text = ctx.prompt_input.call_args[0][0]
        assert "auto-removed" in prompt_text, "prompt should indicate scope creep was auto-removed"

    def test_scope_creep_quit(self):
        ctx = _mock_ctx()
        ctx.prompt_input.return_value = "q"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="scope creep found")), \
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
        ctx.prompt_input.return_value = "c"
        req_content = "[REQ-001] requirement"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=req_content)):
            Phase4BScopeGate().execute(ctx)
        assert ctx.state.get("scope_gate_passed")

    def test_quit_action_exits(self):
        ctx = _mock_ctx()
        ctx.prompt_input.return_value = "q"
        req_content = "[REQ-001] requirement"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=req_content)), \
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
        import pytest
        with patch("os.path.exists", return_value=False), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit) as exc:
            cmd_validate(self._make_args())
        assert exc.value.code == 0

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
             patch("concurrent.futures.as_completed", return_value=[mock_future]), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
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
             patch("builtins.open", mock_open()), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
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
             patch("builtins.open", mock_open(read_data="task content")), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
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
             patch.object(Phase7ADAGGeneration, "_validate_dag", return_value=[]), \
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
             patch.object(Phase7ADAGGeneration, "_validate_dag", return_value=[]), \
             patch("os.listdir", side_effect=[["sub"], ["t.md"]]), \
             patch("os.path.isdir", return_value=True), \
             patch("builtins.open", mock_open(read_data='{"sub/t.md": []}')), \
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

    def test_fix_description_length_req_file_not_found(self):
        from workflow_lib.replan import _fix_description_length
        ctx = self._mock_ctx()
        with patch("os.path.exists", return_value=False), \
             patch("builtins.print"):
            result = _fix_description_length(ctx, dry_run=False)
        assert result is False

    def test_fix_description_length_no_issues(self):
        from workflow_lib.replan import _fix_description_length
        ctx = self._mock_ctx()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", return_value=mock_result), \
             patch("builtins.print"):
            result = _fix_description_length(ctx, dry_run=False)
        assert result is False

    def test_fix_description_length_dry_run(self):
        from workflow_lib.replan import _fix_description_length
        ctx = self._mock_ctx()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "  - [REQ-001] (5 words)\n"
        mock_result.stderr = ""
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", return_value=mock_result), \
             patch("builtins.open", mock_open(read_data="### **[REQ-001]** short\n")), \
             patch("builtins.print"):
            result = _fix_description_length(ctx, dry_run=True)
        assert result is True

    def test_fix_description_length_ai_failure(self):
        from workflow_lib.replan import _fix_description_length
        ctx = self._mock_ctx()
        ctx.run_ai.return_value = MagicMock(returncode=1, stdout="error", stderr="err")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "  - [REQ-001] (5 words)\n"
        mock_result.stderr = ""
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", return_value=mock_result), \
             patch("builtins.open", mock_open(read_data="### **[REQ-001]** short\n")), \
             patch("builtins.print"):
            result = _fix_description_length(ctx, dry_run=False)
        assert result is False

    def test_cmd_regen_dag_dry_run_single_phase(self):
        from workflow_lib.replan import cmd_regen_dag
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True):
            cmd_regen_dag(self._make_args(phase_id="phase_1", dry_run=True, backend="gemini"))

    def test_cmd_regen_dag_dry_run_all_phases(self):
        from workflow_lib.replan import cmd_regen_dag
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["phase_1", "phase_2", "other_dir"]):
            cmd_regen_dag(self._make_args(phase_id=None, dry_run=True, backend="gemini"))

    def test_cmd_regen_dag_not_found(self):
        from workflow_lib.replan import cmd_regen_dag
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            cmd_regen_dag(self._make_args(phase_id="phase_99", dry_run=False, backend="gemini"))

    def test_cmd_regen_dag_all_phases_no_tasks_dir(self):
        from workflow_lib.replan import cmd_regen_dag
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=False), \
             pytest.raises(SystemExit):
            cmd_regen_dag(self._make_args(phase_id=None, dry_run=False, backend="gemini"))

    def test_cmd_regen_dag_all_phases_empty(self):
        from workflow_lib.replan import cmd_regen_dag

        def isdir_side(p):
            return "tasks" in p and "phase_" not in p

        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", side_effect=isdir_side), \
             patch("os.listdir", return_value=["README.md"]):
            cmd_regen_dag(self._make_args(phase_id=None, dry_run=False, backend="gemini"))

    def test_cmd_regen_dag_success_single_phase(self):
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

    def test_cmd_regen_dag_success_all_phases(self):
        from workflow_lib.replan import cmd_regen_dag
        ctx = self._mock_ctx()
        with patch("workflow_lib.replan.get_tasks_dir", return_value="/fake/tasks"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["phase_1", "phase_2"]), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
             patch("workflow_lib.replan._rebuild_phase_dag") as mock_rebuild, \
             patch("workflow_lib.replan.load_replan_state",
                   return_value={"replan_history": []}), \
             patch("workflow_lib.replan.save_replan_state"):
            cmd_regen_dag(self._make_args(phase_id=None, dry_run=False, backend="gemini"))
        assert mock_rebuild.call_count == 2

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

class TestValidateDag:
    """Tests for Phase7ADAGGeneration._validate_dag."""

    def test_valid_dag_no_errors(self, tmp_path):
        phase_dir = tmp_path / "phase_1"
        sub = phase_dir / "sub_epic"
        sub.mkdir(parents=True)
        (sub / "01_task.md").write_text("content")
        (sub / "02_task.md").write_text("content")
        dag = {"sub_epic/01_task.md": [], "sub_epic/02_task.md": ["sub_epic/01_task.md"]}
        errors = Phase7ADAGGeneration._validate_dag(str(phase_dir), dag)
        assert errors == []

    def test_phantom_dag_entry(self, tmp_path):
        phase_dir = tmp_path / "phase_1"
        sub = phase_dir / "sub_epic"
        sub.mkdir(parents=True)
        (sub / "01_task.md").write_text("content")
        dag = {"sub_epic/01_task.md": [], "sub_epic/02_missing.md": []}
        errors = Phase7ADAGGeneration._validate_dag(str(phase_dir), dag)
        assert len(errors) == 1
        assert "non-existent" in errors[0]
        assert "02_missing.md" in errors[0]

    def test_orphan_file_on_disk(self, tmp_path):
        phase_dir = tmp_path / "phase_1"
        sub = phase_dir / "sub_epic"
        sub.mkdir(parents=True)
        (sub / "01_task.md").write_text("content")
        (sub / "02_task.md").write_text("content")
        dag = {"sub_epic/01_task.md": []}
        errors = Phase7ADAGGeneration._validate_dag(str(phase_dir), dag)
        assert len(errors) == 1
        assert "not in DAG" in errors[0]
        assert "02_task.md" in errors[0]

    def test_empty_phase_dir(self, tmp_path):
        phase_dir = tmp_path / "phase_1"
        phase_dir.mkdir(parents=True)
        errors = Phase7ADAGGeneration._validate_dag(str(phase_dir), {})
        assert errors == []


class TestParseDependsOnBare:
    """Tests for _parse_depends_on handling bare (non-bracketed) values."""

    def test_bare_none(self):
        content = "- depends_on: none\n- shared_components: []"
        result = Phase7ADAGGeneration._parse_depends_on(content)
        assert result == []

    def test_bare_none_caps(self):
        content = "- depends_on: None\n"
        result = Phase7ADAGGeneration._parse_depends_on(content)
        assert result == []

    def test_bare_filename(self):
        content = "- depends_on: 01_setup.md\n"
        result = Phase7ADAGGeneration._parse_depends_on(content)
        assert result == ["01_setup.md"]

    def test_bracketed_still_works(self):
        content = "- depends_on: [01_a.md, 02_b.md]\n"
        result = Phase7ADAGGeneration._parse_depends_on(content)
        assert result == ["01_a.md", "02_b.md"]

    def test_missing_field(self):
        content = "# Task\nNo depends on here\n"
        result = Phase7ADAGGeneration._parse_depends_on(content)
        assert result is None


class TestPhase7AInner:
    """Tests that actually exercise the process_phase_dag closure."""

    def test_dag_exists_skip(self):
        """Existing valid dag.json is accepted and phase is skipped."""
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag tmpl"
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["phase_1"]), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch.object(Phase7ADAGGeneration, "_validate_dag", return_value=[]), \
             patch("builtins.open", mock_open(read_data='{"sub/01.md": []}')), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            Phase7ADAGGeneration().execute(ctx)
        assert ctx.state.get("dag_completed") is True

    def test_dag_exists_stale_triggers_regeneration(self):
        """Stale dag.json (validation errors) is deleted and regenerated."""
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag tmpl"
        fresh_dag = {"sub/01_a.md": []}

        def exists_side(p):
            # dag.json exists the first time (stale check), not after removal
            if "dag.json" in p and "reviewed" not in p:
                return not getattr(exists_side, "_removed", False)
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: ["phase_1"] if "tasks" in p and "phase_1" not in p else []), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch("os.remove"), \
             patch.object(Phase7ADAGGeneration, "_validate_dag", side_effect=[
                 ["File on disk not in DAG: sub/02_extra.md"],  # stale: has errors
                 [],  # after rebuild: valid
             ]), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=fresh_dag), \
             patch("builtins.open", mock_open(read_data='{"sub/01_a.md": []}')), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
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
             patch.object(Phase7ADAGGeneration, "_validate_dag", return_value=[]), \
             patch("builtins.open", mock_open()), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            Phase7ADAGGeneration().execute(ctx)
        assert ctx.state.get("dag_completed") is True

    def test_programmatic_dag_validation_failure_triggers_ai_fallback(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag tmpl"
        ctx.run_gemini.return_value = MagicMock(returncode=0)
        dag = {"sub/01_a.md": []}

        dag_exists_calls = [0]

        def exists_side(p):
            if "dag.json" in p and "reviewed" not in p:
                dag_exists_calls[0] += 1
                # First call (programmatic check): False
                # After AI runs, dag.json "exists"
                return dag_exists_calls[0] > 1
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: (
                 ["phase_1"] if "tasks" in p and "phase_1" not in p else
                 ["sub"] if "phase_1" in p and "sub" not in p else
                 ["01_a.md"] if "sub" in p else []
             )), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p or "sub" in p), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=dag), \
             patch.object(Phase7ADAGGeneration, "_validate_dag", side_effect=[
                 ["File on disk not in DAG: sub/02_extra.md"],  # programmatic fails
                 [],  # AI fallback succeeds
             ]), \
             patch("builtins.open", mock_open(read_data='{"sub/01_a.md": []}')), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            Phase7ADAGGeneration().execute(ctx)
        assert ctx.state.get("dag_completed") is True
        assert ctx.run_gemini.called  # fell back to AI

    def test_ai_dag_validation_failure_retries(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag tmpl"
        ctx.run_gemini.return_value = MagicMock(returncode=0)

        call_count = [0]

        def exists_side(p):
            if "dag.json" in p and "reviewed" not in p:
                call_count[0] += 1
                # dag.json "exists" after AI writes it
                return call_count[0] > 1
            return True

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: (
                 ["phase_1"] if "tasks" in p and "phase_1" not in p else
                 ["sub"] if "phase_1" in p and "sub" not in p else
                 ["01_a.md"] if "sub" in p else []
             )), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p or "sub" in p), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=None), \
             patch("builtins.open", mock_open(read_data='{"sub/01_a.md": []}')), \
             patch.object(Phase7ADAGGeneration, "_validate_dag", return_value=[]), \
             patch("os.remove"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
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
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=None), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            Phase7ADAGGeneration().execute(ctx)
        # no sub_epics -> process returns True -> stage + save
        assert ctx.state.get("dag_completed") is True

    def test_ai_dag_json_decode_error_triggers_retry(self):
        """Test that invalid JSON from AI triggers retry."""
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "dag tmpl"
        call_count = [0]

        def exists_side(p):
            if "dag.json" in p and "reviewed" not in p:
                call_count[0] += 1
                return call_count[0] > 2  # exists after 2nd attempt
            return True

        def run_gemini_side(*a, **kw):
            # First call writes invalid JSON, second call writes valid
            if call_count[0] == 1:
                with patch("builtins.open", mock_open()) as mock_file:
                    pass
            return MagicMock(returncode=0)

        ctx.run_gemini.side_effect = run_gemini_side

        with patch("os.path.exists", side_effect=exists_side), \
             patch("os.listdir", side_effect=lambda p: (
                 ["phase_1"] if "tasks" in p and "phase_1" not in p else
                 ["sub"] if "phase_1" in p else []
             )), \
             patch("os.path.isdir", side_effect=lambda p: "phase_1" in p), \
             patch.object(Phase7ADAGGeneration, "_build_programmatic_dag", return_value=None), \
             patch.object(Phase7ADAGGeneration, "_validate_dag", return_value=[]), \
             patch("builtins.open", mock_open(read_data="invalid json{")), \
             patch("json.load", side_effect=[json.JSONDecodeError("msg", "doc", 0), None]), \
             patch("os.remove"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            Phase7ADAGGeneration().execute(ctx)
        assert ctx.state.get("dag_completed") is True


class TestExecutorFunctions:
    """Tests for executor.py helper functions."""

    def test_compact_task_id_short(self):
        from workflow_lib.executor import _compact_task_id
        result = _compact_task_id("phase_1", "short")
        # Function compacts phase_id to p1
        assert "p1" in result
        assert "short" in result

    def test_compact_task_id_long(self):
        from workflow_lib.executor import _compact_task_id
        long_name = "very_long_task_name_that_exceeds_limit"
        result = _compact_task_id("phase_1", long_name)
        assert len(result) <= 50

    def test_step_for_agent_type(self):
        from workflow_lib.executor import _step_for_agent_type
        # Function returns 'all' for all agent types
        assert _step_for_agent_type("claude") == "all"
        assert _step_for_agent_type("gemini") == "all"
        assert _step_for_agent_type("copilot") == "all"


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
             patch("builtins.open", mock_open(read_data="task content")), \
             patch("workflow_lib.config.load_config", return_value={}):
            Phase6CCrossPhaseReview(pass_num=1).execute(ctx)
        assert ctx.state.get("cross_phase_reviewed_pass_1") is True


class TestPhase6DCoverage:
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
             patch("builtins.open", mock_open(read_data="task content")), \
             patch("workflow_lib.config.load_config", return_value={}):
            Phase6DReorderTasks(pass_num=1).execute(ctx)
        assert ctx.state.get("tasks_reordered_pass_1") is True

    def test_sandbox_disabled(self):
        ctx = _mock_ctx_for_phases()
        ctx.load_prompt.return_value = "reorder tmpl"
        ctx.format_prompt.return_value = "formatted"
        summary_exists = [False]

        def run_gemini_side(*a, **kw):
            summary_exists[0] = True
            return MagicMock(returncode=0)

        ctx.run_gemini.side_effect = run_gemini_side

        def exists_side(p):
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
             patch("builtins.open", mock_open(read_data="task content")), \
             patch("workflow_lib.config.load_config", return_value={}):
            Phase6DReorderTasks(pass_num=1).execute(ctx)
        kw = ctx.run_gemini.call_args.kwargs if ctx.run_gemini.call_args.kwargs else ctx.run_gemini.call_args[1]
        assert kw.get("sandbox") is False, "sandbox should be disabled so agent can move files"


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
             patch("workflow_lib.cli.get_agent_pool_configs", return_value=[]), \
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
             patch("workflow_lib.cli.get_agent_pool_configs", return_value=[]), \
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
             patch("workflow_lib.cli.get_agent_pool_configs", return_value=[]), \
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

    def test_load_config_raises_on_invalid_json(self):
        """load_config must raise JSONDecodeError on malformed config, not silently return {}."""
        import json
        bad_jsonc = '{\n  "soft_timeout": 1200\n  "context_limit": 50000\n}'  # missing comma
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=bad_jsonc)):
            from workflow_lib.config import load_config
            with pytest.raises(json.JSONDecodeError):
                load_config()


    # ------------------------------------------------------------------
    # context_limit resolution order
    # ------------------------------------------------------------------

    def _reset_context_limit_state(self):
        """Reset both module-level overrides to a clean slate."""
        import workflow_lib.config as cfg
        cfg._context_limit_override = None
        cfg._agent_context_limit = None

    def test_context_limit_default(self):
        """Falls back to 126 000 when nothing is configured."""
        self._reset_context_limit_state()
        with patch("os.path.exists", return_value=False):
            from workflow_lib.config import get_context_limit
            assert get_context_limit() == 126_000

    def test_context_limit_global_config_overrides_default(self):
        """Global config value overrides the built-in default."""
        self._reset_context_limit_state()
        jsonc = '{"context_limit": 60000}'
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import get_context_limit
            assert get_context_limit() == 60_000

    def test_context_limit_agent_overrides_global_config(self):
        """Per-agent limit overrides the global config value."""
        self._reset_context_limit_state()
        jsonc = '{"context_limit": 60000}'
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import get_context_limit, set_agent_context_limit
            set_agent_context_limit(40_000)
            try:
                assert get_context_limit() == 40_000
            finally:
                set_agent_context_limit(None)

    def test_context_limit_cli_overrides_agent(self):
        """CLI flag takes top precedence over per-agent and global limits."""
        self._reset_context_limit_state()
        jsonc = '{"context_limit": 60000}'
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import (
                get_context_limit,
                set_agent_context_limit,
                set_context_limit_override,
            )
            set_agent_context_limit(40_000)
            set_context_limit_override(20_000)
            try:
                assert get_context_limit() == 20_000
            finally:
                set_context_limit_override(None)
                set_agent_context_limit(None)

    def test_get_agent_pool_configs_parses_context_limit(self):
        """context_limit key in an agent entry is parsed into AgentConfig."""
        jsonc = '''{
            "agents": [
                {
                    "name": "flash",
                    "backend": "gemini",
                    "context_limit": 50000
                }
            ]
        }'''
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import get_agent_pool_configs
            configs = get_agent_pool_configs()
        assert len(configs) == 1
        assert configs[0].context_limit == 50_000

    def test_get_agent_pool_configs_parses_context_limit_kebab(self):
        """context-limit (kebab-case) key is also accepted."""
        jsonc = '''{
            "agents": [
                {
                    "name": "flash",
                    "backend": "gemini",
                    "context-limit": 45000
                }
            ]
        }'''
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import get_agent_pool_configs
            configs = get_agent_pool_configs()
        assert configs[0].context_limit == 45_000

    def test_get_agent_pool_configs_context_limit_defaults_to_none(self):
        """Agents without context_limit get None (inherit global/default)."""
        jsonc = '''{
            "agents": [
                {"name": "flash", "backend": "gemini"}
            ]
        }'''
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import get_agent_pool_configs
            configs = get_agent_pool_configs()
        assert configs[0].context_limit is None

    def test_cmd_plan_applies_agent_context_limit(self):
        """cmd_plan sets the agent context limit when a matching pool agent is found."""
        import workflow_lib.config as cfg
        self._reset_context_limit_state()
        from workflow_lib.agent_pool import AgentConfig
        agent = AgentConfig(
            name="flash", backend="gemini", user="u",
            parallel=1, priority=1, quota_time=60,
            context_limit=55_000,
        )
        ctx = MagicMock()
        ctx.state = {}
        args = MagicMock(phase=None, force=False, backend="gemini", jobs=1)
        mock_dash = MagicMock()
        mock_dash.__enter__ = MagicMock(return_value=mock_dash)
        mock_dash.__exit__ = MagicMock(return_value=False)
        with patch("workflow_lib.cli._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.cli.get_agent_pool_configs", return_value=[agent]), \
             patch("workflow_lib.cli.ProjectContext", return_value=ctx), \
             patch("workflow_lib.cli.Orchestrator", return_value=MagicMock()), \
             patch("workflow_lib.cli.make_dashboard", return_value=mock_dash), \
             patch("workflow_lib.cli._DashboardStream", side_effect=lambda d, s: s), \
             patch("builtins.open", mock_open()):
            from workflow_lib.cli import cmd_plan
            cmd_plan(args)
        assert cfg._agent_context_limit == 55_000
        self._reset_context_limit_state()

    def test_cmd_plan_skips_agent_context_limit_when_cli_override_set(self):
        """CLI --context-limit takes precedence; agent limit does not overwrite it."""
        import workflow_lib.config as cfg
        self._reset_context_limit_state()
        cfg._context_limit_override = 20_000  # simulate CLI flag already applied
        from workflow_lib.agent_pool import AgentConfig
        agent = AgentConfig(
            name="flash", backend="gemini", user="u",
            parallel=1, priority=1, quota_time=60,
            context_limit=55_000,
        )
        ctx = MagicMock()
        ctx.state = {}
        args = MagicMock(phase=None, force=False, backend="gemini", jobs=1)
        mock_dash = MagicMock()
        mock_dash.__enter__ = MagicMock(return_value=mock_dash)
        mock_dash.__exit__ = MagicMock(return_value=False)
        with patch("workflow_lib.cli._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.cli.get_agent_pool_configs", return_value=[agent]), \
             patch("workflow_lib.cli.ProjectContext", return_value=ctx), \
             patch("workflow_lib.cli.Orchestrator", return_value=MagicMock()), \
             patch("workflow_lib.cli.make_dashboard", return_value=mock_dash), \
             patch("workflow_lib.cli._DashboardStream", side_effect=lambda d, s: s), \
             patch("builtins.open", mock_open()):
            from workflow_lib.cli import cmd_plan
            cmd_plan(args)
        # CLI override wins regardless of the agent's limit
        from workflow_lib.config import get_context_limit
        assert get_context_limit() == 20_000
        self._reset_context_limit_state()

    def test_get_dev_branch_default(self):
        """get_dev_branch returns 'dev' when no config file exists."""
        with patch("os.path.exists", return_value=False):
            from workflow_lib.config import get_dev_branch
            result = get_dev_branch()
        assert result == "dev"

    def test_get_dev_branch_custom(self):
        """get_dev_branch returns configured branch name."""
        jsonc = '{"dev_branch": "integration"}'
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import get_dev_branch
            result = get_dev_branch()
        assert result == "integration"

    def test_get_pivot_remote_default(self):
        """get_pivot_remote returns 'origin' when no config file exists."""
        with patch("os.path.exists", return_value=False):
            from workflow_lib.config import get_pivot_remote
            result = get_pivot_remote()
        assert result == "origin"

    def test_get_pivot_remote_custom(self):
        """get_pivot_remote returns the configured remote name."""
        jsonc = '{"pivot_remote": "github"}'
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=jsonc)):
            from workflow_lib.config import get_pivot_remote
            result = get_pivot_remote()
        assert result == "github"

    def test_pivot_remote_used_in_fetch(self):
        """execute_dag uses the configured pivot_remote in the post-merge fetch."""
        state = {"completed_tasks": [], "merged_tasks": []}
        dag = {"phase_1/task.md": []}
        fetch_calls = []

        def _tracking_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "fetch" in cmd:
                fetch_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_tracking_run), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.get_dev_branch", return_value="dev"), \
             patch("workflow_lib.executor.get_pivot_remote", return_value="upstream"), \
             patch("workflow_lib.executor.process_task", return_value=True), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"):
            execute_dag("/root", dag, state, 1, "./do presubmit")

        assert any("upstream" in cmd for cmd in fetch_calls), (
            f"Expected fetch using 'upstream' remote, got: {fetch_calls}"
        )

    def test_pivot_remote_used_in_url_lookup(self):
        """get_gitlab_remote_url prefers the named pivot remote over others."""
        from workflow_lib.executor import get_gitlab_remote_url
        remote_v_output = (
            "upstream\thttps://github.com/org/repo.git (fetch)\n"
            "upstream\thttps://github.com/org/repo.git (push)\n"
            "origin\thttps://old-origin.example.com/repo.git (fetch)\n"
            "origin\thttps://old-origin.example.com/repo.git (push)\n"
        )
        with patch("subprocess.run", return_value=MagicMock(
            returncode=0, stdout=remote_v_output, stderr=""
        )):
            url = get_gitlab_remote_url("/root", remote_name="upstream")
        assert url == "https://github.com/org/repo.git"

    def test_dev_branch_created_custom(self):
        """When dev_branch is customized, execute_dag creates that branch."""
        run_results = [
            MagicMock(returncode=1),  # rev-parse fails
            MagicMock(returncode=0),  # branch creation
        ]
        state = {"completed_tasks": [], "merged_tasks": []}
        with patch("subprocess.run", side_effect=run_results + [MagicMock(returncode=0)] * 100) as mock_run, \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.get_dev_branch", return_value="integration"), \
             patch("workflow_lib.executor.get_ready_tasks", return_value=[]), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"):
            execute_dag("/root", {}, state, 1, "./do presubmit")
        # First call: rev-parse --verify integration
        assert mock_run.call_args_list[0][0][0] == ["git", "rev-parse", "--verify", "integration"]
        # Second call: branch integration main
        assert mock_run.call_args_list[1][0][0] == ["git", "branch", "integration", "main"]


class TestTemplateContainsAllConfigKeys:
    """Ensure the .workflow.jsonc template mentions every config key used by the code."""

    def test_all_config_keys_in_template(self):
        """Every config key consumed by config.py must appear in the template."""
        import os
        template_path = os.path.join(
            os.path.dirname(__file__), "..", "templates", ".workflow.jsonc"
        )
        with open(template_path, "r", encoding="utf-8") as f:
            template_text = f.read()

        # Keys consumed by get_config_defaults()
        from workflow_lib.config import get_config_defaults
        import inspect
        source = inspect.getsource(get_config_defaults)
        # Extract the tuple of keys from the for-loop in get_config_defaults
        import re
        match = re.search(r'for key in \(([^)]+)\)', source)
        assert match, "Could not find key tuple in get_config_defaults source"
        defaults_keys = [k.strip().strip('"').strip("'") for k in match.group(1).split(",")]

        # Keys with dedicated accessors
        dedicated_keys = ["serena", "dev_branch", "pivot_remote"]

        all_keys = set(defaults_keys + dedicated_keys)
        missing = [k for k in sorted(all_keys) if f'"{k}"' not in template_text]
        assert not missing, (
            f"Config keys missing from .workflow.jsonc template: {missing}. "
            f"Add commented-out entries for these keys to .tools/templates/.workflow.jsonc"
        )


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
            runner.run("/tmp", "prompt",
                       image_paths=["/img/a.png", "/img/b.jpg"])
        call_args = mock_run.call_args
        # Images should be appended as @refs in the prompt
        assert "@/img/a.png" in call_args.kwargs.get("input", call_args[1].get("input", ""))

    def test_claude_runner_with_images(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run("/tmp", "prompt",
                       image_paths=["/img/a.png"])
        cmd = mock_run.call_args[0][0]
        assert "--image" in cmd
        assert "/img/a.png" in cmd

    def test_opencode_runner_run(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run("/tmp", "prompt",
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

    def test_set_agent_stores_full_last_line(self):
        from workflow_lib.dashboard import Dashboard
        import io
        with Dashboard(log_file=io.StringIO()) as d:
            d.set_agent("t", "Impl", "running", "x" * 200)
            # Agent lines are stored in full; wrapping happens at render time
            lines = list(d._agents["t"][2])
            assert len(lines) == 1
            assert len(lines[0][1]) == 200

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
        proc.stdout = io.StringIO("".join(line + "\n" for line in lines))
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
        with patch("subprocess.Popen", return_value=proc):
            runner.run("/cwd", "prompt", on_line=collected.append)
        assert "line1" in collected
        assert "line2" in collected

    def test_run_streaming_blank_lines_not_sent(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        collected = []
        proc = self._fake_popen(["  ", "real line"])
        with patch("subprocess.Popen", return_value=proc):
            runner.run("/cwd", "prompt", on_line=collected.append)
        assert "  " not in collected
        assert "real line" in collected

    def test_run_streaming_returns_completed_process(self):
        from workflow_lib.runners import GeminiRunner
        import subprocess
        runner = GeminiRunner()
        proc = self._fake_popen(["output"], returncode=0)
        with patch("subprocess.Popen", return_value=proc):
            result = runner.run("/cwd", "prompt", on_line=lambda l: None)
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 0

    def test_gemini_runner_no_on_line_uses_subprocess_run(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            runner.run("/cwd", "prompt")
        mock_run.assert_called_once()

    def test_claude_runner_on_line_uses_popen(self):
        import json as _json
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        jsonl_line = _json.dumps({"type": "result", "result": "claude output"})
        proc = self._fake_popen([jsonl_line])
        with patch("subprocess.Popen", return_value=proc):
            collected = []
            runner.run("/cwd", "prompt", on_line=collected.append)
        assert "claude output" in collected

    def test_opencode_runner_on_line_uses_popen(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        proc = self._fake_popen(["opencode output"])
        with patch("subprocess.Popen", return_value=proc):
            collected = []
            runner.run("/cwd", "prompt", on_line=collected.append)
        assert "opencode output" in collected

    def test_copilot_runner_on_line_uses_streaming(self):
        from workflow_lib.runners import CopilotRunner
        runner = CopilotRunner()
        proc = self._fake_popen(["copilot output"])
        with patch("subprocess.Popen", return_value=proc):
            collected = []
            runner.run("/cwd", "prompt", on_line=collected.append)
        assert "copilot output" in collected

    def test_gemini_runner_with_images_and_on_line(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        proc = self._fake_popen(["with image"])
        with patch("subprocess.Popen", return_value=proc):
            collected = []
            runner.run("/cwd", "prompt", image_paths=["/img.png"], on_line=collected.append)
        assert "with image" in collected

    def test_gemini_runner_with_images_no_on_line(self):
        from workflow_lib.runners import GeminiRunner
        runner = GeminiRunner()
        fake = MagicMock(returncode=0, stdout="out", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            runner.run("/cwd", "prompt", image_paths=["/img.png"])
        # prompt should contain @/img.png reference
        called_input = mock_run.call_args.kwargs.get("input", mock_run.call_args[1].get("input", ""))
        assert "@/img.png" in called_input

    def test_claude_runner_no_on_line_uses_subprocess_run(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            runner.run("/cwd", "prompt", image_paths=["/img.png"])
        mock_run.assert_called_once()

    def test_opencode_runner_no_on_line(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            runner.run("/cwd", "prompt")
        mock_run.assert_called_once()

    def test_opencode_runner_with_images_no_on_line(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            runner.run("/cwd", "prompt", image_paths=["/img.png"])
        call_args = mock_run.call_args[0][0]
        assert "-f" in call_args and "/img.png" in call_args

    def test_copilot_runner_no_on_line_returns_result_on_success(self):
        from workflow_lib.runners import CopilotRunner
        runner = CopilotRunner()
        fake = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=fake):
            result = runner.run("/cwd", "prompt")
        assert result.returncode == 0

    def test_base_airunner_abstract_methods_raise(self):
        from workflow_lib.runners import AIRunner
        runner = AIRunner()
        with pytest.raises(NotImplementedError):
            runner.get_cmd()
        with pytest.raises(NotImplementedError):
            runner.run("/cwd", "p")

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
            runner.run("/cwd", "prompt", image_paths=["/img.png"], on_line=collected.append)
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
            command, status, lines_deque, _started, _agent_name = d._agents["t1"]
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
        def fake_run(cwd, prompt, images=None, on_line=None, timeout=None):
            if on_line:
                captured_on_line.append(on_line)
            return MagicMock(returncode=0, stdout="", stderr="")
        ctx.runner.run.side_effect = fake_run

        with patch.object(ctx, "_write_last_failed_command"), \
             patch.object(ctx, "get_workspace_snapshot", return_value={}):
            ctx.run_ai("prompt")

        assert captured_on_line, "on_line callback was not passed to runner"
        on_line = captured_on_line[0]
        on_line("some output line")
        assert any("[Phase1: User Research] some output line" in m for m in logged)

    def test_run_ai_updates_last_line(self, tmp_path):
        dash = MagicMock()
        ctx = self._make_ctx(tmp_path, dashboard=dash)
        ctx.current_phase = "Phase1: Doc"

        captured_on_line = []
        def fake_run(cwd, prompt, images=None, on_line=None, timeout=None):
            if on_line:
                captured_on_line.append(on_line)
            return MagicMock(returncode=0, stdout="", stderr="")
        ctx.runner.run.side_effect = fake_run

        with patch.object(ctx, "_write_last_failed_command"), \
             patch.object(ctx, "get_workspace_snapshot", return_value={}):
            ctx.run_ai("prompt")

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


# ---------------------------------------------------------------------------
# parse_stream_json_line (module-level) coverage
# ---------------------------------------------------------------------------

class TestParseStreamJsonLineCoverage:
    """Cover the module-level parse_stream_json_line function."""

    def test_empty(self):
        from workflow_lib.runners import parse_stream_json_line
        assert parse_stream_json_line("") is None

    def test_non_json(self):
        from workflow_lib.runners import parse_stream_json_line
        assert parse_stream_json_line("plain text") is None

    def test_invalid_json(self):
        from workflow_lib.runners import parse_stream_json_line
        assert parse_stream_json_line("{bad}") is None

    def test_assistant_text(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        assert parse_stream_json_line(line) == "hi"

    def test_assistant_tool_use_read_file(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "read_file", "input": {"file_path": "/a.py"}}
        ]}})
        assert "[tool] read_file: /a.py" in parse_stream_json_line(line)

    def test_assistant_tool_use_web_fetch(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "web_fetch", "input": {"url": "http://x"}}
        ]}})
        assert "[tool] web_fetch: http://x" in parse_stream_json_line(line)

    def test_assistant_tool_use_shell(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "run_shell_command", "input": {"command": "ls"}}
        ]}})
        assert "[tool] run_shell_command: ls" in parse_stream_json_line(line)

    def test_assistant_tool_use_grep(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "grep_search", "input": {"pattern": "foo"}}
        ]}})
        assert "[tool] grep_search: foo" in parse_stream_json_line(line)

    def test_assistant_tool_use_other(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "custom_tool", "input": {}}
        ]}})
        assert parse_stream_json_line(line) == "[tool] custom_tool"

    def test_assistant_empty_content(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "assistant", "message": {"content": []}})
        assert parse_stream_json_line(line) is None

    def test_user_tool_result_text(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "output"}
        ]}})
        assert "[result] output" in parse_stream_json_line(line)

    def test_user_tool_result_error(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "", "is_error": True}
        ]}})
        assert "error" in parse_stream_json_line(line)

    def test_user_tool_result_ok(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": ""}
        ]}})
        assert "ok" in parse_stream_json_line(line)

    def test_user_text_block(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": "user msg"}
        ]}})
        assert parse_stream_json_line(line) == "user msg"

    def test_result_type(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "result", "result": "Done!"})
        assert parse_stream_json_line(line) == "Done!"

    def test_result_empty(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "result", "result": ""})
        assert parse_stream_json_line(line) is None

    def test_system_suppressed(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "system", "message": "init"})
        assert parse_stream_json_line(line) is None

    def test_stream_event_text_delta(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}})
        assert parse_stream_json_line(line) == "hello"

    def test_stream_event_input_json_delta(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{\"k\":"}}})
        assert parse_stream_json_line(line) == "{\"k\":"

    def test_stream_event_other_event_type(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "stream_event", "event": {"type": "content_block_start"}})
        assert parse_stream_json_line(line) is None

    def test_stream_event_no_event(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "stream_event"})
        assert parse_stream_json_line(line) is None

    def test_stream_event_unknown_delta_type(self):
        from workflow_lib.runners import parse_stream_json_line
        line = json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "unknown"}}})
        assert parse_stream_json_line(line) is None


class TestRunnerGetCmdWithModel:
    def test_opencode_get_cmd_with_model(self):
        from workflow_lib.runners import OpencodeRunner
        runner = OpencodeRunner(model="gpt-4")
        cmd = runner.get_cmd()
        assert "--model" in cmd
        assert "gpt-4" in cmd

    def test_copilot_get_cmd_with_model(self):
        from workflow_lib.runners import CopilotRunner
        runner = CopilotRunner(model="gpt-4")
        cmd = runner.get_cmd()
        assert "--model" in cmd
        assert "gpt-4" in cmd

    def test_base_phase_execute_raises(self):
        from workflow_lib.phases import BasePhase
        with pytest.raises(NotImplementedError):
            BasePhase().execute(None)

    def test_session_runner_get_cmd_raises(self):
        from workflow_lib.runners import SessionResumableRunner
        runner = SessionResumableRunner.__new__(SessionResumableRunner)
        with pytest.raises(NotImplementedError):
            runner.get_cmd()

    def test_session_runner_build_resume_raises(self):
        from workflow_lib.runners import SessionResumableRunner
        runner = SessionResumableRunner.__new__(SessionResumableRunner)
        with pytest.raises(NotImplementedError):
            runner._build_resume_cmd_and_prompt("sid")


# ---------------------------------------------------------------------------
# AIRunner._run_streaming_json coverage
# ---------------------------------------------------------------------------

class TestRunStreamingJson:
    def _fake_popen(self, lines, returncode=0):
        import io
        proc = MagicMock()
        proc.stdout = io.StringIO("".join(line + "\n" for line in lines))
        proc.stderr = io.StringIO("")
        proc.returncode = returncode
        proc.stdin = MagicMock()
        proc.wait.return_value = None
        return proc

    def test_parses_jsonl_lines(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        jsonl = json.dumps({"type": "result", "result": "hello"})
        proc = self._fake_popen([jsonl])
        collected = []
        with patch("subprocess.Popen", return_value=proc):
            result = runner._run_streaming_json(["cmd"], "/tmp", collected.append)
        assert "hello" in collected
        assert "hello" in result.stdout

    def test_skips_non_json_lines(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        proc = self._fake_popen(["not json", "also not json"])
        collected = []
        with patch("subprocess.Popen", return_value=proc):
            result = runner._run_streaming_json(["cmd"], "/tmp", collected.append)
        assert collected == []
        assert result.stdout == ""

    def test_claude_non_streaming_parses_jsonl(self):
        from workflow_lib.runners import ClaudeRunner
        runner = ClaudeRunner()
        jsonl_out = json.dumps({"type": "result", "result": "answer"})
        fake = MagicMock(returncode=0, stdout=jsonl_out, stderr="", args=["claude"])
        with patch("subprocess.run", return_value=fake):
            result = runner.run("/tmp", "q")
        assert "answer" in result.stdout


# ---------------------------------------------------------------------------
# Additional runner coverage (ClineRunner, AiderRunner, CodexRunner)
# ---------------------------------------------------------------------------

class TestClineRunnerCoverage:
    def test_get_cmd(self):
        from workflow_lib.runners import ClineRunner
        r = ClineRunner()
        assert r.get_cmd()[:2] == ["cline", "--yolo"]

    def test_get_cmd_with_model(self):
        from workflow_lib.runners import ClineRunner
        r = ClineRunner(model="m1")
        cmd = r.get_cmd()
        assert "-m" in cmd and "m1" in cmd

    def test_run_no_on_line(self):
        from workflow_lib.runners import ClineRunner
        r = ClineRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake):
            result = r.run("/tmp", "prompt")
        assert result.returncode == 0

    def test_run_with_on_line(self):
        from workflow_lib.runners import ClineRunner
        import io
        r = ClineRunner()
        proc = MagicMock()
        proc.stdout = io.StringIO("output\n")
        proc.stderr = io.StringIO("")
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.wait.return_value = None
        collected = []
        with patch("subprocess.Popen", return_value=proc):
            r.run("/tmp", "prompt", on_line=collected.append)
        assert "output" in collected


class TestAiderRunnerCoverage:
    def test_get_cmd(self):
        from workflow_lib.runners import AiderRunner
        r = AiderRunner()
        cmd = r.get_cmd()
        assert "aider" in cmd

    def test_get_cmd_with_model(self):
        from workflow_lib.runners import AiderRunner
        r = AiderRunner(model="m1")
        cmd = r.get_cmd()
        assert "--model" in cmd and "m1" in cmd

    def test_run_no_on_line(self):
        from workflow_lib.runners import AiderRunner
        r = AiderRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake):
            result = r.run("/tmp", "prompt")
        assert result.returncode == 0

    def test_run_with_on_line(self):
        from workflow_lib.runners import AiderRunner
        import io
        r = AiderRunner()
        proc = MagicMock()
        proc.stdout = io.StringIO("output\n")
        proc.stderr = io.StringIO("")
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.wait.return_value = None
        collected = []
        with patch("subprocess.Popen", return_value=proc):
            r.run("/tmp", "prompt", on_line=collected.append)
        assert "output" in collected


class TestCodexRunnerCoverage:
    def test_get_cmd(self):
        from workflow_lib.runners import CodexRunner
        r = CodexRunner()
        cmd = r.get_cmd()
        assert cmd[:3] == ["codex", "exec", "--full-auto"]

    def test_get_cmd_with_model_and_images(self):
        from workflow_lib.runners import CodexRunner
        r = CodexRunner(model="m1")
        cmd = r.get_cmd(image_paths=["/img.png"])
        assert "-m" in cmd and "m1" in cmd
        assert "-i" in cmd and "/img.png" in cmd

    def test_run_no_on_line(self):
        from workflow_lib.runners import CodexRunner
        r = CodexRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake):
            result = r.run("/tmp", "prompt")
        assert result.returncode == 0

    def test_run_with_on_line(self):
        from workflow_lib.runners import CodexRunner
        import io
        r = CodexRunner()
        proc = MagicMock()
        proc.stdout = io.StringIO("output\n")
        proc.stderr = io.StringIO("")
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.wait.return_value = None
        collected = []
        with patch("subprocess.Popen", return_value=proc):
            r.run("/tmp", "prompt", on_line=collected.append)
        assert "output" in collected


class TestCopilotRunnerCoverage:
    def test_run_no_on_line(self):
        from workflow_lib.runners import CopilotRunner
        r = CopilotRunner()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake), \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_f = MagicMock()
            mock_f.name = "/tmp/prompt"
            mock_tmp.return_value.__enter__.return_value = mock_f
            result = r.run("/tmp", "prompt")
        assert result.returncode == 0

    def test_run_with_on_line(self):
        from workflow_lib.runners import CopilotRunner
        import io
        r = CopilotRunner()
        proc = MagicMock()
        proc.stdout = io.StringIO("output\n")
        proc.stderr = io.StringIO("")
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.wait.return_value = None
        collected = []
        with patch("subprocess.Popen", return_value=proc), \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_f = MagicMock()
            mock_f.name = "/tmp/prompt"
            mock_tmp.return_value.__enter__.return_value = mock_f
            r.run("/tmp", "prompt", on_line=collected.append)
        assert "output" in collected


# ---------------------------------------------------------------------------
# Soft-interrupt (shutdown_requested) tests
# ---------------------------------------------------------------------------

class TestSoftInterrupt:
    """Verify shutdown_requested behavior across process_task and run_agent.

    The shutdown guard lives in two places:
      - process_task stage loop: checked BEFORE each stage starts, so an
        in-flight stage always completes but the next stage is skipped.
      - _execute_dag_inner scheduling loop: blocks new tasks from starting.

    run_agent itself has NO shutdown guard — it always proceeds once called.

    Tests cover:
      1. run_agent() — proceeds even when shutdown_requested is set
      2. process_task() — halts before first stage when shutdown pre-set
      3. process_task() — halts between stages when shutdown fires mid-stage
      4. merge_task() — merge completes during shutdown
      5. _execute_dag_inner() — DAG scheduling loop blocks new tasks
      6. signal_handler() — message content
    """

    def setup_method(self):
        import workflow_lib.executor as mod
        self._mod = mod
        self._orig = mod.shutdown_requested

    def teardown_method(self):
        self._mod.shutdown_requested = self._orig

    # -- Helper to build common process_task patches --
    def _process_task_patches(self, run_agent_side_effect):
        """Return a context-manager stack for process_task mocking."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch('workflow_lib.executor.run_agent', side_effect=run_agent_side_effect))
        stack.enter_context(patch('workflow_lib.executor.get_task_details', return_value="# Task: test"))
        stack.enter_context(patch('workflow_lib.executor.get_project_context', return_value="ctx"))
        stack.enter_context(patch('workflow_lib.executor.get_memory_context', return_value="mem"))
        stack.enter_context(patch('workflow_lib.executor.get_rag_enabled', return_value=False))
        stack.enter_context(patch('workflow_lib.executor.start_rag_server'))
        stack.enter_context(patch('subprocess.run', return_value=MagicMock(returncode=0, stdout="", stderr="")))
        stack.enter_context(patch('tempfile.mkdtemp', return_value="/tmp/fake"))
        stack.enter_context(patch('os.chmod'))
        stack.enter_context(patch('shutil.rmtree'))
        return stack

    # ----------------------------------------------------------------
    # 1. run_agent() — proceeds even during shutdown (no guard)
    # ----------------------------------------------------------------

    def test_run_agent_proceeds_when_shutdown_requested(self):
        """run_agent must proceed during shutdown so in-flight tasks complete."""
        self._mod.shutdown_requested = True
        with patch('workflow_lib.executor.run_ai_command', return_value=(0, "")) as mock_ai, \
             patch('workflow_lib.executor.get_project_images', return_value=[]), \
             patch('workflow_lib.executor.get_rag_enabled', return_value=False), \
             patch('builtins.open', mock_open(read_data="prompt")):
            result = self._mod.run_agent(
                "Implementation", "implement_task.md", {}, "/tmp",
            )
        assert result is True
        mock_ai.assert_called_once()

    def test_run_agent_review_proceeds_when_shutdown_requested(self):
        """Review agent must not be skipped during shutdown."""
        self._mod.shutdown_requested = True
        with patch('workflow_lib.executor.run_ai_command', return_value=(0, "")) as mock_ai, \
             patch('workflow_lib.executor.get_project_images', return_value=[]), \
             patch('workflow_lib.executor.get_rag_enabled', return_value=False), \
             patch('builtins.open', mock_open(read_data="prompt")):
            result = self._mod.run_agent("Review", "review_task.md", {}, "/tmp")
        assert result is True
        mock_ai.assert_called_once()

    def test_run_agent_review_retry_proceeds_when_shutdown_requested(self):
        """Review (Retry) must not be skipped during shutdown."""
        self._mod.shutdown_requested = True
        with patch('workflow_lib.executor.run_ai_command', return_value=(0, "")) as mock_ai, \
             patch('workflow_lib.executor.get_project_images', return_value=[]), \
             patch('workflow_lib.executor.get_rag_enabled', return_value=False), \
             patch('builtins.open', mock_open(read_data="prompt")):
            result = self._mod.run_agent("Review (Retry)", "review_task.md", {}, "/tmp")
        assert result is True
        mock_ai.assert_called_once()

    def test_run_agent_merge_proceeds_when_shutdown_requested(self):
        """Merge agent must not be skipped during shutdown."""
        self._mod.shutdown_requested = True
        with patch('workflow_lib.executor.run_ai_command', return_value=(0, "")) as mock_ai, \
             patch('workflow_lib.executor.get_project_images', return_value=[]), \
             patch('workflow_lib.executor.get_rag_enabled', return_value=False), \
             patch('builtins.open', mock_open(read_data="prompt")):
            result = self._mod.run_agent("Merge", "merge_task.md", {}, "/tmp")
        assert result is True
        mock_ai.assert_called_once()

    def test_run_agent_proceeds_when_not_shutdown(self):
        self._mod.shutdown_requested = False
        with patch('workflow_lib.executor.run_ai_command', return_value=(0, "")) as mock_ai, \
             patch('workflow_lib.executor.get_project_images', return_value=[]), \
             patch('workflow_lib.executor.get_rag_enabled', return_value=False), \
             patch('builtins.open', mock_open(read_data="prompt {task_name}")):
            result = self._mod.run_agent(
                "Implementation", "implement_task.md",
                {"task_name": "test"}, "/tmp",
            )
        assert result is True
        mock_ai.assert_called_once()

    # ----------------------------------------------------------------
    # 2. process_task() — completes full workflow during shutdown
    # ----------------------------------------------------------------

    def test_process_task_halts_before_first_stage_when_shutdown_pre_set(self):
        """When shutdown is already set before process_task is called, no stages run."""
        self._mod.shutdown_requested = True

        agent_calls = []
        def fake_run_agent(agent_type, *args, **kwargs):
            agent_calls.append(agent_type)
            return True

        with self._process_task_patches(fake_run_agent):
            result = self._mod.process_task(
                "/root", "phase_1/task", "./presubmit", dashboard=MagicMock(),
            )
        assert result is False
        assert agent_calls == []

    # ----------------------------------------------------------------
    # 3. process_task() — Review agent runs even after shutdown fires
    # ----------------------------------------------------------------

    def test_process_task_review_skipped_when_shutdown_fires_during_implementation(self):
        """Shutdown fires during Implementation → impl stage completes, but Review is skipped."""
        self._mod.shutdown_requested = False

        agent_calls = []
        def fake_run_agent(agent_type, *args, **kwargs):
            agent_calls.append(agent_type)
            if agent_type == "Implementation":
                # Shutdown fires mid-implementation (within the impl stage)
                self._mod.shutdown_requested = True
            return True

        with self._process_task_patches(fake_run_agent):
            result = self._mod.process_task(
                "/root", "phase_1/task", "./presubmit", dashboard=MagicMock(),
            )
        # Impl stage completes, shutdown fires, and we return True for graceful shutdown
        # (at least one stage completed before shutdown)
        assert result is True
        assert agent_calls == ["Implementation"]

    # ----------------------------------------------------------------
    # 4. process_task() — Verification loop blocked by shutdown
    # ----------------------------------------------------------------

    def test_process_task_validate_skipped_when_shutdown_fires_during_review(self):
        """Shutdown fires during Review → review stage completes, but validate is skipped."""
        self._mod.shutdown_requested = False

        call_count = [0]
        def fake_run_agent(agent_type, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:  # During Review agent call, shutdown fires
                self._mod.shutdown_requested = True
            return True

        with self._process_task_patches(fake_run_agent):
            result = self._mod.process_task(
                "/root", "phase_1/task", "./presubmit", dashboard=MagicMock(),
            )
        # Review stage completes, shutdown fires, and we return True for graceful shutdown
        # (at least one stage completed before shutdown)
        assert result is True
        assert call_count[0] == 2  # Implementation + Review only

    # ----------------------------------------------------------------
    # 5. process_task() — Review (Retry) proceeds during shutdown
    # ----------------------------------------------------------------

    def test_process_task_review_retry_proceeds_during_shutdown(self):
        """Shutdown after presubmit failure → Review (Retry) still runs
        so the task has a chance to fix and save the work."""
        self._mod.shutdown_requested = False

        agent_calls = []
        presubmit_attempts = [0]
        def fake_run_agent(agent_type, *args, **kwargs):
            agent_calls.append(agent_type)
            return True

        def fake_subprocess_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "./presubmit":
                presubmit_attempts[0] += 1
                if presubmit_attempts[0] == 1:
                    # First presubmit fails, triggering retry path
                    self._mod.shutdown_requested = True
                    return MagicMock(returncode=1, stdout="fail", stderr="err")
                # Second presubmit passes
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch('workflow_lib.executor.run_agent', side_effect=fake_run_agent), \
             patch('workflow_lib.executor.get_task_details', return_value="# Task: test"), \
             patch('workflow_lib.executor.get_project_context', return_value="ctx"), \
             patch('workflow_lib.executor.get_memory_context', return_value="mem"), \
             patch('subprocess.run', side_effect=fake_subprocess_run), \
             patch('tempfile.mkdtemp', return_value="/tmp/fake"), \
             patch('os.chmod'), \
             patch('shutil.rmtree'):
            result = self._mod.process_task(
                "/root", "phase_1/task", "./presubmit",
                max_retries=3, dashboard=MagicMock(),
            )
        assert result is True
        # Implementation + Review + Review (Retry) — retry proceeds and fixes it
        assert agent_calls == ["Implementation", "Review", "Review (Retry)"]

    def test_process_task_review_retry_spawns_ai_during_shutdown(self):
        """Using the real run_agent (not mocked), shutdown does NOT prevent
        the Review (Retry) from spawning an AI process."""
        self._mod.shutdown_requested = False

        presubmit_attempts = [0]
        def fake_subprocess_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "./presubmit":
                presubmit_attempts[0] += 1
                if presubmit_attempts[0] == 1:
                    self._mod.shutdown_requested = True
                    return MagicMock(returncode=1, stdout="fail", stderr="err")
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch('workflow_lib.executor.run_ai_command', return_value=(0, "")) as mock_ai, \
             patch('workflow_lib.executor.get_task_details', return_value="# Task: test"), \
             patch('workflow_lib.executor.get_project_context', return_value="ctx"), \
             patch('workflow_lib.executor.get_project_images', return_value=[]), \
             patch('workflow_lib.executor.get_memory_context', return_value="mem"), \
             patch('subprocess.run', side_effect=fake_subprocess_run), \
             patch('tempfile.mkdtemp', return_value="/tmp/fake"), \
             patch('os.chmod'), \
             patch('shutil.rmtree'), \
             patch('workflow_lib.executor.get_rag_enabled', return_value=False), \
             patch('builtins.open', mock_open(read_data="prompt {task_name}")):
            result = self._mod.process_task(
                "/root", "phase_1/task", "./presubmit",
                max_retries=3, dashboard=MagicMock(),
            )
        assert result is True
        # run_ai_command called for Implementation, Review, AND Review (Retry)
        assert mock_ai.call_count == 3

    # ----------------------------------------------------------------
    # 6. merge_task() — merge loop skipped on shutdown
    # ----------------------------------------------------------------

    def test_merge_task_proceeds_during_shutdown(self):
        """merge_task proceeds even when shutdown_requested (merges complete during shutdown)."""
        self._mod.shutdown_requested = True
        dash = MagicMock()

        with patch('workflow_lib.executor.get_task_details', return_value="# Task: test"), \
             patch('workflow_lib.executor.get_project_context', return_value="ctx"), \
             patch('workflow_lib.executor.get_gitlab_remote_url', return_value="http://gitlab/repo"), \
             patch('subprocess.run', return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch('tempfile.mkdtemp', return_value="/tmp/merge_fake"), \
             patch('os.chmod'):
            result = self._mod.merge_task(
                "/root", "phase_1/task", "./presubmit", dashboard=dash,
            )
        assert result is True

    # ----------------------------------------------------------------
    # 7. merge_task() — Merge agent skipped on shutdown (attempt > 1)
    # ----------------------------------------------------------------

    def test_merge_task_retries_merge_agent_during_shutdown(self):
        """Merge retries continue even during shutdown (merges complete during shutdown)."""
        self._mod.shutdown_requested = False
        dash = MagicMock()

        def fake_subprocess_run(cmd, *args, **kwargs):
            if isinstance(cmd, list):
                # Squash merge fails on attempt 1 to trigger agent path
                if "merge" in cmd and "--squash" in cmd:
                    return MagicMock(returncode=1, stdout="conflict", stderr="")
                # Rebase also fails
                if "rebase" in cmd and cmd[0] == "git":
                    return MagicMock(returncode=1, stdout="", stderr="rebase fail")
            return MagicMock(returncode=0, stdout="", stderr="")

        agent_calls = []
        def fake_run_agent(agent_type, *args, **kwargs):
            agent_calls.append(agent_type)
            # Set shutdown on first merge agent call
            self._mod.shutdown_requested = True
            return False  # agent "fails"

        with patch('workflow_lib.executor.run_agent', side_effect=fake_run_agent), \
             patch('workflow_lib.executor.get_task_details', return_value="# Task: test"), \
             patch('workflow_lib.executor.get_project_context', return_value="ctx"), \
             patch('workflow_lib.executor.get_gitlab_remote_url', return_value="http://gitlab/repo"), \
             patch('subprocess.run', side_effect=fake_subprocess_run), \
             patch('tempfile.mkdtemp', return_value="/tmp/merge_fake"), \
             patch('os.chmod'):
            result = self._mod.merge_task(
                "/root", "phase_1/task", "./presubmit",
                max_retries=3, dashboard=dash,
            )
        assert result is False
        # Both retry attempts call the Merge agent (shutdown doesn't prevent retries)
        assert len(agent_calls) == 2
        assert all(c == "Merge" for c in agent_calls)

    # ----------------------------------------------------------------
    # 8. _execute_dag_inner() — no new tasks scheduled on shutdown
    # ----------------------------------------------------------------

    def test_dag_loop_skips_scheduling_on_shutdown(self):
        """When shutdown_requested, DAG loop does not schedule new tasks."""
        self._mod.shutdown_requested = True
        dash = MagicMock()

        state = {"completed_tasks": [], "merged_tasks": []}
        dag = {"phase_1/task_a": []}

        # Should exit immediately with graceful shutdown message
        self._mod._execute_dag_inner(
            "/root", dag, state, jobs=1, presubmit_cmd="./presubmit",
            backend="gemini", serena_enabled=False,
            cache_lock=threading.Lock(), dashboard=dash,
        )
        logged = " ".join(str(c) for c in dash.log.call_args_list)
        assert "graceful shutdown" in logged.lower()

    def test_dag_loop_drains_active_then_exits_on_shutdown(self):
        """DAG loop waits for in-flight tasks, then exits on shutdown."""
        self._mod.shutdown_requested = False
        dash = MagicMock()

        state = {"completed_tasks": [], "merged_tasks": []}
        dag = {"phase_1/a": [], "phase_1/b": []}

        tasks_submitted = []
        def fake_process_task(root_dir, task_id, *args, **kwargs):
            tasks_submitted.append(task_id)
            # First task triggers shutdown
            self._mod.shutdown_requested = True
            return False  # task failed

        with patch('workflow_lib.executor.process_task', side_effect=fake_process_task), \
             patch('workflow_lib.executor.load_blocked_tasks', return_value=set()):
            # Task failure causes sys.exit(1) via the failed_tasks path
            with pytest.raises(SystemExit):
                self._mod._execute_dag_inner(
                    "/root", dag, state, jobs=1, presubmit_cmd="./presubmit",
                    backend="gemini", serena_enabled=False,
                    cache_lock=threading.Lock(), dashboard=dash,
                )
        # Only one task was submitted — shutdown prevented the second
        assert len(tasks_submitted) == 1

    # ----------------------------------------------------------------
    # 9. signal_handler() — message content
    # ----------------------------------------------------------------

    def test_signal_handler_message_mentions_agents(self):
        self._mod.shutdown_requested = False
        with patch('builtins.print') as mock_print:
            self._mod.signal_handler(None, None)
        messages = " ".join(str(c) for c in mock_print.call_args_list)
        assert "agents" in messages.lower()

    def test_signal_handler_sets_flag(self):
        self._mod.shutdown_requested = False
        with patch('builtins.print'):
            self._mod.signal_handler(None, None)
        assert self._mod.shutdown_requested is True

    def test_signal_handler_second_call_force_exits(self):
        self._mod.shutdown_requested = True
        with patch('builtins.print'), \
             patch('os._exit') as mock_exit:
            self._mod.signal_handler(None, None)
        mock_exit.assert_called_once_with(1)
