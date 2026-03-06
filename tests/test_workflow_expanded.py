import sys
import os
import json
import pytest
from unittest.mock import patch, MagicMock, mock_open, ANY
import threading
import subprocess

# Add .tools to sys.path so we can import workflow
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import workflow
from workflow import (
    AIRunner, GeminiRunner, ClaudeRunner, CopilotRunner,
    ProjectContext, BasePhase, Phase1GenerateDoc, Phase2FleshOutDoc,
    Phase3FinalReview, Phase3BAdversarialReview, Phase4AExtractRequirements,
    Phase4BMergeRequirements, Phase4BScopeGate, Phase4COrderRequirements,
    Phase5GenerateEpics, Phase5BSharedComponents, Phase6BreakDownTasks,
    Phase6BReviewTasks, Phase6CCrossPhaseReview, Phase6DReorderTasks,
    Phase7ADAGGeneration, Phase7BDAGReview, Orchestrator,
    Logger, run_ai_command, load_dags, get_ready_tasks, process_task, merge_task, execute_dag,
    load_replan_state, save_replan_state, load_workflow_state, save_workflow_state,
    log_action, resolve_task_path, is_completed
)

# --- 1. Utility Tests ---

def test_get_gitlab_remote_url_found():
    with patch('subprocess.run') as mock_run:
        mock_res = MagicMock()
        mock_res.stdout = "origin\tgit@gitlab.lan:mrwilson/dreamer.git (fetch)\n"
        mock_run.return_value = mock_res
        assert workflow.get_gitlab_remote_url("/fake/root") == "git@gitlab.lan:mrwilson/dreamer.git"

def test_get_gitlab_remote_url_not_found():
    with patch('subprocess.run') as mock_run:
        mock_res = MagicMock()
        mock_res.stdout = "origin\tgit@github.com:foo/bar.git (fetch)\n"
        mock_run.return_value = mock_res
        assert workflow.get_gitlab_remote_url("/fake/root") == "http://gitlab.lan/mrwilson/dreamer"
        
def test_get_gitlab_remote_url_error():
    with patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, 'cmd')):
        assert workflow.get_gitlab_remote_url("/fake/root") == "http://gitlab.lan/mrwilson/dreamer"

def test_phase_sort_key():
    assert workflow.phase_sort_key("phase_1/01_foo") == (1, 1)
    assert workflow.phase_sort_key("phase_2/10_bar") == (2, 10)
    assert workflow.phase_sort_key("invalid/01_foo") == (0, 1)
    assert workflow.phase_sort_key("phase_x/xx_foo") == (0, 0)
    assert workflow.phase_sort_key("invalid") == (999, 999)

def test_load_workflow_state():
    with patch('os.path.exists', return_value=True):
        m = mock_open(read_data='{"completed_tasks": ["phase_1/01_foo"]}')
        with patch('builtins.open', m):
            state = load_workflow_state()
            assert state["completed_tasks"] == ["phase_1/01_foo"]

def test_load_workflow_state_empty():
    with patch('os.path.exists', return_value=False):
        state = load_workflow_state()
        assert state == {"completed_tasks": [], "merged_tasks": []}

def test_save_workflow_state():
    m = mock_open()
    with patch('builtins.open', m):
        save_workflow_state({"test": 1})
    m.assert_called_once()

def test_load_replan_state():
    with patch('os.path.exists', return_value=True):
        m = mock_open(read_data='{"blocked_tasks": {}}')
        with patch('builtins.open', m):
            state = load_replan_state()
            assert "blocked_tasks" in state

def test_save_replan_state():
    m = mock_open()
    with patch('os.makedirs'):
        with patch('builtins.open', m):
            save_replan_state({"test": 1})
    m.assert_called_once()

def test_log_action():
    state = {}
    log_action(state, "test_action", "target", "details")
    assert len(state["replan_history"]) == 1
    assert state["replan_history"][0]["action"] == "test_action"

def test_is_completed():
    assert is_completed("t1", {"completed_tasks": ["t1"]}) == True
    assert is_completed("t2", {"merged_tasks": ["t2"]}) == True
    assert is_completed("t3", {}) == False

def test_resolve_task_path():
    with patch('workflow_lib.state.get_tasks_dir', return_value='/fake/tasks'):
        assert resolve_task_path('phase_1/t1.md') == '/fake/tasks/phase_1/t1.md'

def test_logger():
    class DummyStream:
        def __init__(self):
            self.data = ""
        def write(self, d):
            self.data += d
        def flush(self):
            pass
            
    t = DummyStream()
    l = DummyStream()
    logger = Logger(t, l, threading.Lock())
    logger.write("hello\nworld")
    logger.flush()
    assert "hello\n" in t.data
    assert "world" in t.data

def test_signal_handler():
    import workflow_lib.executor as executor_mod
    # Setup global state
    executor_mod.shutdown_requested = False

    # First interrupt
    with patch('builtins.print') as p:
        executor_mod.signal_handler(None, None)
        assert executor_mod.shutdown_requested == True

    # Second interrupt
    with patch('os._exit') as e:
        executor_mod.signal_handler(None, None)
        e.assert_called_with(1)

# --- 2. AI Runners ---

def test_runner_base():
    runner = AIRunner()
    with pytest.raises(NotImplementedError):
        runner.run("cwd", "prompt", "ignore", "file")
    with pytest.raises(NotImplementedError):
        _ = runner.ignore_file_name

def test_gemini_runner():
    runner = GeminiRunner()
    assert runner.ignore_file_name == ".geminiignore"
    with patch('subprocess.run') as mock_run:
        mock_res = MagicMock(returncode=0)
        mock_run.return_value = mock_res
        res = runner.run(".", "hello", "ignore_this", ".geminiignore")
        assert res.returncode == 0
        mock_run.assert_called_with(["gemini", "-y"], input="hello", cwd=".", capture_output=True, text=True)

def test_claude_runner():
    runner = ClaudeRunner()
    assert runner.ignore_file_name == ".claudeignore"
    with patch('subprocess.run') as mock_run:
        mock_res = MagicMock(returncode=0)
        mock_run.return_value = mock_res
        res = runner.run(".", "hello", "ignore_this", ".claudeignore")
        assert res.returncode == 0

def test_copilot_runner():
    runner = CopilotRunner()
    assert runner.ignore_file_name == ".copilotignore"
    with patch('subprocess.run') as mock_run:
        mock_res = MagicMock(returncode=0)
        mock_run.return_value = mock_res
        with patch('tempfile.NamedTemporaryFile') as mock_temp:
            mock_f = MagicMock()
            mock_f.name = "tmp.txt"
            mock_temp.return_value.__enter__.return_value = mock_f
            res = runner.run(".", "hello", "ignore_this", ".copilotignore")
            assert res.returncode == 0

# --- 3. Project Context ---

@pytest.fixture
def mock_ctx():
    with patch('os.makedirs'), \
         patch('os.path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data='test content')):
        ctx = ProjectContext("/fake/root")
        ctx.state = {}
        yield ctx

def test_project_context_init(mock_ctx):
    assert mock_ctx.root_dir == "/fake/root"
    assert mock_ctx.jobs == 1
    
def test_project_context_load_shared_components(mock_ctx):
    assert mock_ctx.load_shared_components() == 'test content'
    
def test_project_context_format_prompt(mock_ctx):
    res = mock_ctx.format_prompt("Hello {name}", name="World")
    assert res == "Hello World"

def test_project_context_stage_changes(mock_ctx):
    with patch('subprocess.run') as mock_run:
        mock_ctx.stage_changes(["file1.txt"])
        mock_run.assert_called_once()

def test_project_context_verify_changes_fail(mock_ctx):
    with patch.object(mock_ctx, 'get_workspace_snapshot') as mock_snap:
        mock_snap.return_value = {"/fake/root/unauthorized.txt": 2}
        with pytest.raises(SystemExit):
            mock_ctx.verify_changes({"/fake/root/unauthorized.txt": 1}, ["/fake/root/allowed.txt"])

def test_project_context_run_ai(mock_ctx):
    with patch.object(mock_ctx, 'get_workspace_snapshot', return_value={}), \
         patch.object(mock_ctx.runner, 'run') as mock_run, \
         patch.object(mock_ctx, 'verify_changes'), \
         patch.object(mock_ctx, 'strip_thinking_tags'):
        mock_res = MagicMock(returncode=0)
        mock_run.return_value = mock_res
        
        res = mock_ctx.run_ai("prompt", "ignore", allowed_files=["file1"])
        assert res.returncode == 0

def test_parse_markdown_headers(mock_ctx):
    # Overwrite the builtins.open patch for this specific test
    with patch('builtins.open', mock_open(read_data="# Header 1\n## Header 2\n### Header 3\nText")):
        headers = mock_ctx.parse_markdown_headers("test.md")
        assert headers == ["# Header 1", "## Header 2"]

# --- 4. Orchestrator ---

def test_orchestrator_run_phase_with_retry(mock_ctx):
    orc = Orchestrator(mock_ctx)
    phase = MagicMock()
    phase.__class__.__name__ = "TestPhase"
    
    # Success on first try
    orc.run_phase_with_retry(phase, max_retries=1)
    assert phase.execute.call_count == 1
    
    # Exit 0
    phase.execute.side_effect = SystemExit(0)
    orc.run_phase_with_retry(phase, max_retries=1)
    
    # Retry and then pass
    phase.execute.side_effect = [Exception("error"), None]
    with patch('builtins.input', return_value=''):
        orc.run_phase_with_retry(phase, max_retries=2)

    # Quit on error
    phase.execute.side_effect = Exception("error")
    with patch('builtins.input', return_value='q'):
        with pytest.raises(SystemExit):
            orc.run_phase_with_retry(phase, max_retries=2)
            
# --- 5. Phase Execution (Stubbed execution to verify logic flows) ---

def test_phase1_generate_doc(mock_ctx):
    doc = workflow.DOCS[0]
    phase = Phase1GenerateDoc(doc)
    with patch.object(mock_ctx, 'run_gemini') as mock_run, \
         patch.object(mock_ctx, 'stage_changes'):
        mock_res = MagicMock(returncode=0)
        mock_run.return_value = mock_res
        with patch('os.path.exists', return_value=True):
            phase.execute(mock_ctx)
        assert doc["id"] in mock_ctx.state["generated"]

def test_phase2_flesh_out_doc(mock_ctx):
    doc = [d for d in workflow.DOCS if d["type"] == "spec"][0]
    phase = Phase2FleshOutDoc(doc)
    with patch.object(mock_ctx, 'parse_markdown_headers', return_value=["# H1"]), \
         patch.object(mock_ctx, 'run_gemini') as mock_run, \
         patch.object(mock_ctx, 'stage_changes'):
        mock_res = MagicMock(returncode=0)
        mock_run.return_value = mock_res
        phase.execute(mock_ctx)
        assert doc["id"] in mock_ctx.state["fleshed_out"]

def test_phase3_final_review(mock_ctx):
    phase = Phase3FinalReview()
    with patch.object(mock_ctx, 'run_gemini') as mock_run, \
         patch.object(mock_ctx, 'stage_changes'):
        mock_res = MagicMock(returncode=0)
        mock_run.return_value = mock_res
        phase.execute(mock_ctx)
        assert mock_ctx.state["final_review_completed"] == True

# Run workflow tests
def test_load_dags():
    with patch('os.path.exists', return_value=True), \
         patch('os.listdir', return_value=["phase_1"]), \
         patch('os.path.isdir', return_value=True):
        m = mock_open(read_data='{"01_task": []}')
        with patch('builtins.open', m):
            dags = load_dags("tasks")
            assert "phase_1/01_task" in dags

def test_get_ready_tasks():
    dag = {
        "phase_1/01_a": [],
        "phase_1/01_b": ["phase_1/01_a"],
        "phase_2/01_c": [],
    }
    # a is ready, b is not, c is ready but blocked by phase
    ready = get_ready_tasks(dag, [], [])
    assert ready == ["phase_1/01_a"]
    
    # now a is complete
    ready = get_ready_tasks(dag, ["phase_1/01_a"], [])
    assert "phase_1/01_b" in ready
    
    # now a and b complete
    ready = get_ready_tasks(dag, ["phase_1/01_a", "phase_1/01_b"], [])
    assert ready == ["phase_2/01_c"]

def test_execute_dag():
    import workflow_lib.executor as executor_mod
    executor_mod.shutdown_requested = False
    dag = {"phase_1/01_a": []}
    state = {"completed_tasks": [], "merged_tasks": []}
    
    with patch('workflow_lib.executor.subprocess.run') as mock_run, \
         patch('workflow_lib.executor.process_task', return_value=True), \
         patch('workflow_lib.executor.merge_task', return_value=True), \
         patch('workflow_lib.executor.rebuild_serena_cache'), \
         patch('workflow_lib.executor.save_workflow_state'), \
         patch('workflow_lib.executor.get_serena_enabled', return_value=False), \
         patch('os.path.isdir', return_value=True):
        
        mock_res = MagicMock(returncode=0)
        mock_run.return_value = mock_res
        
        execute_dag("/root", dag, state, 1, "cmd", "gemini")
        assert "phase_1/01_a" in state["completed_tasks"]

# Replan commands
def test_cmd_status():
    args = MagicMock()
    with patch('workflow_lib.replan.load_dags', return_value={"phase_1/t1": []}), \
         patch('workflow_lib.replan.load_workflow_state', return_value={"completed_tasks": []}), \
         patch('workflow_lib.replan.load_replan_state', return_value={"blocked_tasks": {}}), \
         patch('workflow_lib.replan.get_tasks_dir', return_value='/fake/tasks'), \
         patch('os.listdir', return_value=[]), \
         patch('os.path.exists', return_value=True), \
         patch('builtins.print') as mock_print:
        workflow.cmd_status(args)
        mock_print.assert_any_call("    [ ] phase_1/t1")

def test_cmd_block():
    args = MagicMock(task="phase_1/t1", reason="bug", dry_run=False)
    with patch('workflow_lib.replan.load_workflow_state', return_value={}), \
         patch('os.path.exists', return_value=True), \
         patch('workflow_lib.replan.load_replan_state', return_value={}), \
         patch('workflow_lib.replan.save_replan_state') as mock_save, \
         patch('builtins.print'):
        workflow.cmd_block(args)
        mock_save.assert_called_once()
        saved_state = mock_save.call_args[0][0]
        assert "phase_1/t1" in saved_state["blocked_tasks"]

def test_cmd_unblock():
    args = MagicMock(task="phase_1/t1", dry_run=False)
    state = {"blocked_tasks": {"phase_1/t1": {}}}
    with patch('workflow_lib.replan.load_replan_state', return_value=state), \
         patch('workflow_lib.replan.save_replan_state') as mock_save, \
         patch('builtins.print'):
        workflow.cmd_unblock(args)
        mock_save.assert_called_once()
        assert "phase_1/t1" not in mock_save.call_args[0][0]["blocked_tasks"]


# --- Runner get_cmd Tests ---

def test_gemini_runner_get_cmd():
    runner = GeminiRunner()
    assert runner.get_cmd() == ["gemini", "-y"]
    assert runner.get_cmd(image_paths=["/img.png"]) == ["gemini", "-y"]


def test_claude_runner_get_cmd():
    runner = ClaudeRunner()
    assert runner.get_cmd() == ["claude", "-p", "--dangerously-skip-permissions"]
    assert runner.get_cmd(image_paths=["/a.png", "/b.jpg"]) == [
        "claude", "-p", "--dangerously-skip-permissions", "--image", "/a.png", "--image", "/b.jpg"
    ]


def test_copilot_runner_get_cmd():
    runner = CopilotRunner()
    cmd = runner.get_cmd()
    assert cmd[0] == "copilot"


def test_opencode_runner_get_cmd():
    from workflow import OpencodeRunner
    runner = OpencodeRunner()
    assert runner.get_cmd() == ["opencode", "run"]
    assert runner.get_cmd(image_paths=["/x.png"]) == ["opencode", "run", "-f", "/x.png"]


# --- _write_last_failed_command Tests ---

def test_write_last_failed_command(tmp_path):
    runner = GeminiRunner()
    with patch("builtins.open", _real_open_for_tmp(tmp_path)), \
         patch("os.chmod") as mock_chmod, \
         patch("builtins.print"):
        ctx = ProjectContext.__new__(ProjectContext)
        ctx.root_dir = str(tmp_path)
        ctx.runner = runner
        ctx.image_paths = None
        ctx.ignore_file = str(tmp_path / ".geminiignore")

        ctx._write_last_failed_command("test prompt content", "/*\n!/docs/\n")

    prompt_file = tmp_path / ".last_failed_prompt.txt"
    script_file = tmp_path / ".last_failed_command.sh"
    assert prompt_file.read_text() == "test prompt content"

    script = script_file.read_text()
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "gemini -y" in script
    assert "< .last_failed_prompt.txt" in script
    assert f"cd {str(tmp_path)!r}" in script or f"cd '{tmp_path}'" in script or f"cd {tmp_path}" in script
    assert "# Ignore file:" in script
    mock_chmod.assert_called_once_with(str(script_file), 0o755)


def test_write_last_failed_command_no_ignore(tmp_path):
    runner = ClaudeRunner()
    with patch("builtins.open", _real_open_for_tmp(tmp_path)), \
         patch("os.chmod"), \
         patch("builtins.print"):
        ctx = ProjectContext.__new__(ProjectContext)
        ctx.root_dir = str(tmp_path)
        ctx.runner = runner
        ctx.image_paths = ["/img/screenshot.png"]
        ctx.ignore_file = str(tmp_path / ".claudeignore")

        ctx._write_last_failed_command("prompt here", "")

    script = (tmp_path / ".last_failed_command.sh").read_text()
    assert "claude" in script
    assert "--image" in script
    assert "# Ignore file:" not in script


def test_run_ai_writes_last_failed_on_failure(tmp_path):
    runner = GeminiRunner()
    mock_result = MagicMock()
    mock_result.returncode = 1

    with patch.object(runner, "run", return_value=mock_result), \
         patch("builtins.open", _real_open_for_tmp(tmp_path)), \
         patch("os.chmod"), \
         patch("builtins.print"):
        ctx = ProjectContext.__new__(ProjectContext)
        ctx.root_dir = str(tmp_path)
        ctx.runner = runner
        ctx.image_paths = None
        ctx.ignore_file = str(tmp_path / ".geminiignore")

        result = ctx.run_ai("my prompt", "ignore stuff")

    assert result.returncode == 1
    assert (tmp_path / ".last_failed_command.sh").exists()
    assert (tmp_path / ".last_failed_prompt.txt").exists()


def test_run_ai_no_failed_file_on_success(tmp_path):
    runner = GeminiRunner()
    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch.object(runner, "run", return_value=mock_result), \
         patch("builtins.print"):
        ctx = ProjectContext.__new__(ProjectContext)
        ctx.root_dir = str(tmp_path)
        ctx.runner = runner
        ctx.image_paths = None
        ctx.ignore_file = str(tmp_path / ".geminiignore")

        result = ctx.run_ai("my prompt", "ignore stuff")

    assert result.returncode == 0
    assert not (tmp_path / ".last_failed_command.sh").exists()


def _real_open_for_tmp(tmp_path):
    """Return an open wrapper that allows real writes to tmp_path only."""
    import builtins
    _real = builtins.open.__wrapped__ if hasattr(builtins.open, '__wrapped__') else builtins.open
    # In test context, builtins.open may be the guarded version from conftest
    # We need the actual open for tmp_path writes
    import io
    _actual_open = io.open

    def _open(file, mode="r", *args, **kwargs):
        path = os.path.abspath(str(file))
        if path.startswith(str(tmp_path)):
            return _actual_open(file, mode, *args, **kwargs)
        return _actual_open(file, mode, *args, **kwargs)
    return _open