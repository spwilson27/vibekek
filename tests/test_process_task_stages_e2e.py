"""E2E test baseline for the process_task staged-refactor.

Phase 1 — Baseline tests of the CURRENT monolithic ``process_task`` contract.
These must all pass BEFORE any refactor begins and continue to pass after.

Phase 2 — Staged behaviour tests for the POST-REFACTOR API.
These are marked ``xfail(strict=True)`` until the refactor is complete; once
the refactor lands they should all flip to passing (remove the marker then).

Real git operations are used throughout (bare remote + local clone).  Only
AI-agent calls and context-loading helpers are mocked.
"""

import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.executor import process_task
import workflow_lib.executor as executor_mod

# Capture real subprocess.run before any test-level patches shadow it.
_real_subprocess_run = subprocess.run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd, cwd, check=True):
    """Run a git command, raising on failure by default."""
    return _real_subprocess_run(
        cmd, cwd=cwd, check=check, capture_output=True, text=True
    )


def _make_dual_repo(tmp_path) -> tuple:
    """Create a bare remote and a local clone with a ``dev`` branch.

    Returns ``(remote_path, local_path)``.  The bare remote acts as the
    authoritative origin so ``git push`` works correctly without triggering
    the "updating current branch" refusal of non-bare repos.
    """
    remote = str(tmp_path / "remote.git")
    local  = str(tmp_path / "local")

    _run(["git", "init", "--bare", remote], cwd=str(tmp_path))
    _run(["git", "clone", remote, local],   cwd=str(tmp_path))
    _run(["git", "config", "user.email", "test@test.com"], cwd=local)
    _run(["git", "config", "user.name",  "Test"],           cwd=local)
    (Path(local) / "README.md").write_text("init\n")
    _run(["git", "add", "README.md"],         cwd=local)
    _run(["git", "commit", "-m", "init"],     cwd=local)
    _run(["git", "branch", "-M", "dev"],      cwd=local)
    _run(["git", "push", "-u", "origin", "dev"], cwd=local)
    return remote, local


def _branch_in_remote(remote: str, branch_name: str) -> bool:
    """Return True if *branch_name* exists as a ref in *remote*."""
    res = _run(
        ["git", "ls-remote", "--heads", remote, branch_name],
        cwd="/tmp", check=False,
    )
    return "refs/heads/" in res.stdout


def _context_patches():
    """Return a list of patch objects for all context-loading helpers."""
    return [
        patch("workflow_lib.executor.get_task_details",
              return_value="# Task: Test\n"),
        patch("workflow_lib.executor.get_project_context",    return_value=""),
        patch("workflow_lib.executor.get_memory_context",     return_value=""),
        patch("workflow_lib.executor.get_spec_context",       return_value=""),
        patch("workflow_lib.executor.get_shared_components_context", return_value=""),
    ]


def _call_process_task(remote, local, full_task_id, *, run_agent_mock,
                        presubmit_cmd="echo ok", max_retries=1,
                        dev_branch="dev", cleanup=True):
    """Invoke process_task with standard test patches applied."""
    executor_mod.shutdown_requested = False
    with ExitStack() as stack:
        stack.enter_context(
            patch("workflow_lib.executor.run_agent", side_effect=run_agent_mock)
        )
        for p in _context_patches():
            stack.enter_context(p)
        result = process_task(
            root_dir=local,
            full_task_id=full_task_id,
            presubmit_cmd=presubmit_cmd,
            dev_branch=dev_branch,
            remote_url=remote,
            max_retries=max_retries,
            cleanup=cleanup,
        )
    return result


# ---------------------------------------------------------------------------
# Phase 1 — Baseline tests (current monolithic contract)
# ---------------------------------------------------------------------------

class TestProcessTaskHappyPath:
    """process_task succeeds end-to-end and pushes the task branch."""

    def test_full_happy_path(self, tmp_path):
        """Returns True and creates the task branch in the remote."""
        remote, local = _make_dual_repo(tmp_path)
        full_task_id  = "phase_1/01_setup.md"
        branch_name   = "ai-phase-01_setup"

        # Both impl and review agents succeed.
        result = _call_process_task(
            remote, local, full_task_id,
            run_agent_mock=lambda *a, **kw: True,
        )

        assert result is True
        assert _branch_in_remote(remote, branch_name), (
            f"Expected branch {branch_name!r} in remote after success"
        )

    def test_agent_calls_include_impl_and_review(self, tmp_path):
        """Both 'Implementation' and 'Review' agents are invoked."""
        remote, local = _make_dual_repo(tmp_path)
        calls = []

        def _mock_agent(agent_type, *a, **kw):
            calls.append(agent_type)
            return True

        _call_process_task(
            remote, local, "phase_1/02_core.md",
            run_agent_mock=_mock_agent,
        )

        assert "Implementation" in calls
        assert "Review" in calls


class TestProcessTaskAgentFailure:
    """process_task returns False when an agent fails."""

    def test_impl_agent_failure_returns_false(self, tmp_path):
        """Returns False when the Implementation agent fails."""
        remote, local = _make_dual_repo(tmp_path)
        call_num = [0]

        def _mock(agent_type, *a, **kw):
            call_num[0] += 1
            return False  # fail on first call (Implementation)

        result = _call_process_task(
            remote, local, "phase_1/03_fail_impl.md",
            run_agent_mock=_mock,
        )
        assert result is False

    def test_review_agent_failure_returns_false(self, tmp_path):
        """Returns False when the Review agent fails (impl succeeds)."""
        remote, local = _make_dual_repo(tmp_path)
        call_num = [0]

        def _mock(agent_type, *a, **kw):
            call_num[0] += 1
            return call_num[0] == 1  # True for impl, False for review

        result = _call_process_task(
            remote, local, "phase_1/04_fail_review.md",
            run_agent_mock=_mock,
        )
        assert result is False

    def test_branch_pushed_after_impl_even_when_review_fails(self, tmp_path):
        """After impl succeeds, the branch is pushed to remote (staged arch).
        When review then fails, process_task returns False but the branch
        still exists in remote (from the impl push) for investigation."""
        remote, local = _make_dual_repo(tmp_path)
        call_num = [0]

        def _mock(agent_type, *a, **kw):
            call_num[0] += 1
            return call_num[0] == 1  # impl succeeds, review fails

        branch_name = "ai-phase-04_no_push"
        result = _call_process_task(
            remote, local, "phase_1/04_no_push.md",
            run_agent_mock=_mock,
        )
        assert result is False
        # Branch IS in remote after impl stage pushed it (even though review failed)
        assert _branch_in_remote(remote, branch_name)


class TestProcessTaskPresubmit:
    """process_task verification loop behaviour."""

    def test_presubmit_always_fails_returns_false(self, tmp_path):
        """Returns False when presubmit keeps failing up to max_retries."""
        remote, local = _make_dual_repo(tmp_path)

        result = _call_process_task(
            remote, local, "phase_1/05_presubmit_fail.md",
            run_agent_mock=lambda *a, **kw: True,
            presubmit_cmd="/bin/false",
            max_retries=1,
        )
        assert result is False

    def test_presubmit_passes_on_second_attempt(self, tmp_path):
        """Returns True when presubmit fails once then succeeds after review-retry."""
        remote, local = _make_dual_repo(tmp_path)

        # Counter script: exits 1 on first call, 0 on second.
        counter_file = tmp_path / "presubmit_calls"
        script = tmp_path / "presubmit_check.py"
        script.write_text(
            f"import os, sys\n"
            f"n = int(open('{counter_file}').read()) if os.path.exists('{counter_file}') else 0\n"
            f"n += 1\n"
            f"open('{counter_file}', 'w').write(str(n))\n"
            f"sys.exit(0 if n >= 2 else 1)\n"
        )

        agent_calls = []

        def _mock(agent_type, *a, **kw):
            agent_calls.append(agent_type)
            return True

        result = _call_process_task(
            remote, local, "phase_1/06_retry_ok.md",
            run_agent_mock=_mock,
            presubmit_cmd=f"python3 {script}",
            max_retries=2,
        )

        assert result is True
        assert "Review (Retry)" in agent_calls, (
            "Review (Retry) agent should have been called after first presubmit failure"
        )


class TestProcessTaskEarlyOut:
    """process_task skips work when the task branch already exists."""

    def test_branch_exists_early_out_returns_true(self, tmp_path):
        """Returns True immediately when task branch already exists in remote."""
        remote, local = _make_dual_repo(tmp_path)

        # Pre-create the task branch in the remote (simulating a prior run).
        branch_name = "ai-phase-07_already_done"
        _run(["git", "push", "origin", f"dev:{branch_name}"], cwd=local)
        assert _branch_in_remote(remote, branch_name)

        call_count = [0]

        def _mock(agent_type, *a, **kw):
            call_count[0] += 1
            return True

        result = _call_process_task(
            remote, local, "phase_1/07_already_done.md",
            run_agent_mock=_mock,
        )

        assert result is True
        assert call_count[0] == 0, "run_agent must not be called when branch exists"

    def test_branch_absent_agent_is_called(self, tmp_path):
        """When the branch does NOT exist, the Implementation agent is invoked."""
        remote, local = _make_dual_repo(tmp_path)
        call_count = [0]

        def _mock(agent_type, *a, **kw):
            call_count[0] += 1
            return False  # fail so we don't need a working commit

        _call_process_task(
            remote, local, "phase_1/08_not_done.md",
            run_agent_mock=_mock,
        )

        assert call_count[0] > 0, "run_agent must be called when branch is absent"


class TestProcessTaskCleanupBehaviour:
    """Temp directory lifecycle on success and failure."""

    def test_tmpdir_removed_on_success(self, tmp_path):
        """Temporary clone is deleted after a successful process_task run."""
        remote, local = _make_dual_repo(tmp_path)
        created = []
        original = tempfile.mkdtemp

        def _tracking_mkdtemp(**kwargs):
            d = original(**kwargs)
            created.append(d)
            return d

        executor_mod.shutdown_requested = False
        with ExitStack() as stack:
            stack.enter_context(
                patch("workflow_lib.executor.run_agent", return_value=True)
            )
            for p in _context_patches():
                stack.enter_context(p)
            stack.enter_context(
                patch("tempfile.mkdtemp", side_effect=_tracking_mkdtemp)
            )
            process_task(
                root_dir=local,
                full_task_id="phase_1/09_cleanup_ok.md",
                presubmit_cmd="echo ok",
                dev_branch="dev",
                remote_url=remote,
                max_retries=1,
                cleanup=True,
            )

        assert len(created) >= 1
        for d in created:
            assert not os.path.exists(d), (
                f"Tmpdir {d} should have been cleaned up after success"
            )

    def test_tmpdir_kept_on_failure_when_cleanup_false(self, tmp_path):
        """Temporary clone is retained on failure when cleanup=False."""
        remote, local = _make_dual_repo(tmp_path)
        created = []
        original = tempfile.mkdtemp

        def _tracking_mkdtemp(**kwargs):
            d = original(**kwargs)
            created.append(d)
            return d

        executor_mod.shutdown_requested = False
        try:
            with ExitStack() as stack:
                stack.enter_context(
                    patch("workflow_lib.executor.run_agent", return_value=True)
                )
                for p in _context_patches():
                    stack.enter_context(p)
                stack.enter_context(
                    patch("tempfile.mkdtemp", side_effect=_tracking_mkdtemp)
                )
                result = process_task(
                    root_dir=local,
                    full_task_id="phase_1/10_cleanup_fail.md",
                    presubmit_cmd="/bin/false",
                    dev_branch="dev",
                    remote_url=remote,
                    max_retries=1,
                    cleanup=False,
                )

            assert result is False
            assert len(created) >= 1
            # Staged architecture: only the FAILING stage retains its tmpdir
            # when cleanup=False. Successful stages (impl, review) clean up normally.
            failed_dirs = [d for d in created if os.path.exists(d)]
            assert len(failed_dirs) >= 1, (
                f"At least one tmpdir should be retained on failure when cleanup=False; "                f"created={created}"
            )
        finally:
            import shutil
            for d in created:
                shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase 2 — Staged behaviour tests.
# ---------------------------------------------------------------------------


class TestStageAPI:
    """Direct tests of run_impl_stage / run_review_stage / run_validate_stage."""

    def test_run_impl_stage_pushes_branch(self, tmp_path):
        """run_impl_stage creates the task branch in the remote."""
        from workflow_lib.executor import run_impl_stage
        remote, local = _make_dual_repo(tmp_path)
        full_task_id = "phase_1/11_impl_stage.md"
        branch_name = "ai-phase-11_impl_stage"

        executor_mod.shutdown_requested = False
        with ExitStack() as stack:
            stack.enter_context(patch("workflow_lib.executor.run_agent", return_value=True))
            for p in _context_patches():
                stack.enter_context(p)
            ok = run_impl_stage(
                root_dir=local,
                full_task_id=full_task_id,
                branch_name=branch_name,
                dev_branch="dev",
                remote_url=remote,
                backend="gemini",
                model=None,
                serena=False,
                dashboard=None,
                agent_pool=None,
                cleanup=True,
                docker_config=None,
            )

        assert ok is True
        assert _branch_in_remote(remote, branch_name)

    def test_run_review_stage_calls_review_agent(self, tmp_path):
        """run_review_stage clones the task branch and invokes the Review agent."""
        from workflow_lib.executor import run_impl_stage, run_review_stage

        remote, local = _make_dual_repo(tmp_path)
        full_task_id = "phase_1/12_review_stage.md"
        branch_name = "ai-phase-12_review_stage"
        agent_calls = []

        def _mock(agent_type, *a, **kw):
            agent_calls.append(agent_type)
            return True

        executor_mod.shutdown_requested = False
        common_kwargs = dict(
            root_dir=local,
            full_task_id=full_task_id,
            branch_name=branch_name,
            dev_branch="dev",
            remote_url=remote,
            backend="gemini",
            model=None,
            dashboard=None,
            agent_pool=None,
            cleanup=True,
            docker_config=None,
        )
        with ExitStack() as stack:
            stack.enter_context(patch("workflow_lib.executor.run_agent", side_effect=_mock))
            for p in _context_patches():
                stack.enter_context(p)
            run_impl_stage(serena=False, **common_kwargs)
            agent_calls.clear()
            ok = run_review_stage(**common_kwargs)

        assert ok is True
        assert "Review" in agent_calls
        assert "Implementation" not in agent_calls

    def test_run_validate_stage_passes_presubmit(self, tmp_path):
        """run_validate_stage runs presubmit; returns True on success."""
        from workflow_lib.executor import run_impl_stage, run_review_stage, run_validate_stage

        remote, local = _make_dual_repo(tmp_path)
        full_task_id = "phase_1/13_validate_stage.md"
        branch_name = "ai-phase-13_validate_stage"

        executor_mod.shutdown_requested = False
        common_kwargs = dict(
            root_dir=local,
            full_task_id=full_task_id,
            branch_name=branch_name,
            dev_branch="dev",
            remote_url=remote,
            backend="gemini",
            model=None,
            dashboard=None,
            agent_pool=None,
            cleanup=True,
            docker_config=None,
        )
        with ExitStack() as stack:
            stack.enter_context(patch("workflow_lib.executor.run_agent", return_value=True))
            for p in _context_patches():
                stack.enter_context(p)
            run_impl_stage(serena=False, **common_kwargs)
            run_review_stage(**common_kwargs)
            ok = run_validate_stage(presubmit_cmd="echo ok", max_retries=1, **common_kwargs)

        assert ok is True


class TestStagedCallbacks:
    """on_stage_complete callback is fired after each completed stage."""

    def test_callback_called_after_each_stage_in_order(self, tmp_path):
        """on_stage_complete is called once per stage in impl→review→validate order."""
        from workflow_lib.executor import STAGE_IMPL, STAGE_REVIEW, STAGE_VALIDATE

        remote, local = _make_dual_repo(tmp_path)
        completed_stages = []

        def _on_done(task_id, stage):
            completed_stages.append(stage)

        executor_mod.shutdown_requested = False
        with ExitStack() as stack:
            stack.enter_context(patch("workflow_lib.executor.run_agent", return_value=True))
            for p in _context_patches():
                stack.enter_context(p)
            result = process_task(
                root_dir=local,
                full_task_id="phase_1/14_callbacks.md",
                presubmit_cmd="echo ok",
                dev_branch="dev",
                remote_url=remote,
                max_retries=1,
                cleanup=True,
                on_stage_complete=_on_done,  # NEW param
            )

        assert result is True
        assert completed_stages == [STAGE_IMPL, STAGE_REVIEW, STAGE_VALIDATE]

    def test_callback_not_called_on_impl_failure(self, tmp_path):
        """on_stage_complete is never called when impl fails."""
        remote, local = _make_dual_repo(tmp_path)
        completed_stages = []

        executor_mod.shutdown_requested = False
        with ExitStack() as stack:
            stack.enter_context(patch("workflow_lib.executor.run_agent", return_value=False))
            for p in _context_patches():
                stack.enter_context(p)
            process_task(
                root_dir=local,
                full_task_id="phase_1/15_no_callback.md",
                presubmit_cmd="echo ok",
                dev_branch="dev",
                remote_url=remote,
                max_retries=1,
                cleanup=True,
                on_stage_complete=lambda tid, stage: completed_stages.append(stage),
            )

        assert completed_stages == []


class TestStagedRecovery:
    """process_task resumes from the correct stage when starting_stage is provided."""

    def test_starting_at_review_skips_impl(self, tmp_path):
        """When starting_stage=STAGE_REVIEW, Implementation agent is never called."""
        from workflow_lib.executor import STAGE_REVIEW

        remote, local = _make_dual_repo(tmp_path)
        # Pre-push task branch (simulating a completed impl stage).
        branch_name = "ai-phase-16_skip_impl"
        _run(["git", "push", "origin", f"dev:{branch_name}"], cwd=local)

        agent_calls = []

        def _mock(agent_type, *a, **kw):
            agent_calls.append(agent_type)
            return True

        executor_mod.shutdown_requested = False
        with ExitStack() as stack:
            stack.enter_context(patch("workflow_lib.executor.run_agent", side_effect=_mock))
            for p in _context_patches():
                stack.enter_context(p)
            process_task(
                root_dir=local,
                full_task_id="phase_1/16_skip_impl.md",
                presubmit_cmd="echo ok",
                dev_branch="dev",
                remote_url=remote,
                max_retries=1,
                cleanup=True,
                starting_stage=STAGE_REVIEW,  # NEW param
            )

        assert "Implementation" not in agent_calls
        assert "Review" in agent_calls

    def test_starting_at_validate_skips_impl_and_review(self, tmp_path):
        """When starting_stage=STAGE_VALIDATE, only presubmit loop runs."""
        from workflow_lib.executor import STAGE_VALIDATE

        remote, local = _make_dual_repo(tmp_path)
        branch_name = "ai-phase-17_skip_both"
        _run(["git", "push", "origin", f"dev:{branch_name}"], cwd=local)

        agent_calls = []

        def _mock(agent_type, *a, **kw):
            agent_calls.append(agent_type)
            return True

        executor_mod.shutdown_requested = False
        with ExitStack() as stack:
            stack.enter_context(patch("workflow_lib.executor.run_agent", side_effect=_mock))
            for p in _context_patches():
                stack.enter_context(p)
            result = process_task(
                root_dir=local,
                full_task_id="phase_1/17_skip_both.md",
                presubmit_cmd="echo ok",
                dev_branch="dev",
                remote_url=remote,
                max_retries=1,
                cleanup=True,
                starting_stage=STAGE_VALIDATE,  # NEW param
            )

        assert "Implementation" not in agent_calls
        assert "Review" not in agent_calls
        assert result is True  # presubmit (echo ok) passes


class TestStagedExecuteDagIntegration:
    """_execute_dag_inner saves and restores task stage state."""

    def test_execute_dag_saves_stage_state_after_impl(self, tmp_path):
        """After impl completes and review crashes, state['task_stages'] records 'impl'."""
        from workflow_lib.executor import execute_dag, STAGE_IMPL
        import workflow_lib.executor as executor_mod

        executor_mod.shutdown_requested = False
        remote, local = _make_dual_repo(tmp_path)

        dag = {"phase_1/18_crash_review.md": []}
        state = {"completed_tasks": [], "merged_tasks": [], "task_stages": {}}
        impl_done = [False]

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          on_stage_complete=None, **kwargs):
            # Simulate impl stage completing then review crashing.
            if on_stage_complete:
                on_stage_complete(task_id, STAGE_IMPL)
            raise RuntimeError("Simulated crash during review stage")

        ready_calls = [0]

        def _get_ready(master_dag, completed, active, **kwargs):
            ready_calls[0] += 1
            if ready_calls[0] == 1:
                return ["phase_1/18_crash_review.md"]
            return []

        with patch("workflow_lib.executor.process_task", side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.get_ready_tasks", side_effect=_get_ready), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             pytest.raises(SystemExit):
            execute_dag(local, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        assert state.get("task_stages", {}).get("phase_1/18_crash_review.md") == STAGE_IMPL

    def test_execute_dag_resumes_from_stage_state(self, tmp_path):
        """Restarting with task_stages[task_id]='impl' only runs review + validate."""
        from workflow_lib.executor import execute_dag, STAGE_IMPL, STAGE_REVIEW

        executor_mod.shutdown_requested = False
        remote, local = _make_dual_repo(tmp_path)

        dag = {"phase_1/19_resume.md": []}
        state = {
            "completed_tasks": [],
            "merged_tasks": [],
            "task_stages": {"phase_1/19_resume.md": STAGE_IMPL},  # impl already done
        }
        starting_stages_seen = []

        def _fake_process(root_dir, task_id, presubmit_cmd, backend,
                          starting_stage=None, **kwargs):
            starting_stages_seen.append(starting_stage)
            return True

        with patch("workflow_lib.executor.process_task", side_effect=_fake_process), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")):
            execute_dag(local, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        assert starting_stages_seen == [STAGE_REVIEW], (
            f"Expected starting_stage=STAGE_REVIEW, got: {starting_stages_seen}"
        )

    def test_task_stages_cleaned_up_after_full_completion(self, tmp_path):
        """After a task fully completes and merges, task_stages no longer has its ID."""
        from workflow_lib.executor import execute_dag

        executor_mod.shutdown_requested = False
        remote, local = _make_dual_repo(tmp_path)

        task_id = "phase_1/20_cleanup_stages.md"
        dag = {task_id: []}
        state = {"completed_tasks": [], "merged_tasks": [], "task_stages": {}}

        with patch("workflow_lib.executor.process_task", return_value=True), \
             patch("workflow_lib.executor.merge_task", return_value=True), \
             patch("workflow_lib.executor.get_serena_enabled", return_value=False), \
             patch("workflow_lib.executor.load_blocked_tasks", return_value=set()), \
             patch("workflow_lib.executor.save_workflow_state"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")):
            execute_dag(local, dag, state, jobs=1,
                        presubmit_cmd="echo ok", backend="gemini")

        assert task_id in state["completed_tasks"]
        assert task_id not in state.get("task_stages", {}), (
            "task_stages entry should be cleaned up after full task completion"
        )


def test_graceful_shutdown_after_impl_stage_persists_progress(tmp_path):
    """Verify that when shutdown occurs after impl stage completes, the stage
    is persisted and task resumes from review on next run.
    
    This tests the fix for the issue where SIGINT during stage transitions
    would mark tasks as failed instead of preserving partial progress.
    """
    from workflow_lib.executor import (
        process_task, STAGE_IMPL, STAGE_REVIEW, STAGE_VALIDATE,
        run_impl_stage, run_review_stage, run_validate_stage,
    )
    import workflow_lib.executor as executor_mod
    
    remote, local = _make_dual_repo(tmp_path)
    task_id = "phase_1/01_test_shutdown.md"
    full_task_id = task_id
    
    # Track which stages were executed
    stages_executed = []
    
    # Mock stage functions that track execution
    def mock_impl_stage(*args, **kwargs):
        stages_executed.append(STAGE_IMPL)
        return True
    
    def mock_review_stage(*args, **kwargs):
        stages_executed.append(STAGE_REVIEW)
        return True
    
    def mock_validate_stage(*args, **kwargs):
        stages_executed.append(STAGE_VALIDATE)
        return True
    
    # Run 1: Complete impl stage, then trigger shutdown before review
    executor_mod.shutdown_requested = False
    state = {"completed_tasks": [], "merged_tasks": [], "task_stages": {}}
    persisted_stages = []
    
    def mock_stage_callback(tid, stage):
        """Track stage persistence."""
        persisted_stages.append((tid, stage))
        state.setdefault("task_stages", {})[tid] = stage
    
    # We need to trigger shutdown AFTER impl completes but BEFORE review starts
    # We do this by patching the stage functions to set the flag after impl
    shutdown_triggered = [False]
    
    def mock_impl_with_shutdown(*args, **kwargs):
        result = mock_impl_stage(*args, **kwargs)
        # Trigger shutdown after impl completes
        executor_mod.shutdown_requested = True
        shutdown_triggered[0] = True
        return result
    
    with ExitStack() as stack:
        # Patch all context loaders to avoid any external dependencies
        for p in _context_patches():
            stack.enter_context(p)
        
        # Patch stage functions - impl triggers shutdown, review/validate shouldn't run
        stack.enter_context(
            patch("workflow_lib.executor.run_impl_stage", side_effect=mock_impl_with_shutdown)
        )
        stack.enter_context(
            patch("workflow_lib.executor.run_review_stage", side_effect=mock_review_stage)
        )
        stack.enter_context(
            patch("workflow_lib.executor.run_validate_stage", side_effect=mock_validate_stage)
        )
        
        # Run process_task
        result = process_task(
            root_dir=local,
            full_task_id=full_task_id,
            presubmit_cmd="echo ok",
            backend="gemini",
            serena=False,
            dashboard=None,
            model=None,
            dev_branch="dev",
            remote_url=remote,
            agent_pool=None,
            cleanup=True,
            docker_config=None,
            starting_stage=STAGE_IMPL,
            on_stage_complete=mock_stage_callback,
        )
    
    # Verify:
    # 1. Only impl stage was executed (shutdown prevented review/validate)
    assert stages_executed == [STAGE_IMPL], (
        f"Expected only impl stage to run, got: {stages_executed}"
    )
    
    # 2. Shutdown was triggered
    assert shutdown_triggered[0], "Shutdown should have been triggered after impl"
    
    # 3. impl stage was persisted to state
    assert state.get("task_stages", {}).get(full_task_id) == STAGE_IMPL, (
        "impl stage should be persisted in task_stages after graceful shutdown"
    )
    
    # 4. process_task returned True (graceful shutdown, not failure)
    assert result is True, (
        "process_task should return True for graceful shutdown after completing a stage"
    )
    
    # Run 2: Verify task resumes from review stage (not impl)
    executor_mod.shutdown_requested = False
    stages_executed.clear()
    starting_stages_received = []
    
    def mock_process_task_capture_start(*args, starting_stage=None, **kwargs):
        """Capture what starting_stage was passed."""
        starting_stages_received.append(starting_stage)
        # Run all remaining stages successfully
        return True
    
    with ExitStack() as stack:
        for p in _context_patches():
            stack.enter_context(p)
        
        stack.enter_context(
            patch("workflow_lib.executor.run_impl_stage", side_effect=mock_impl_stage)
        )
        stack.enter_context(
            patch("workflow_lib.executor.run_review_stage", side_effect=mock_review_stage)
        )
        stack.enter_context(
            patch("workflow_lib.executor.run_validate_stage", side_effect=mock_validate_stage)
        )
        stack.enter_context(
            patch("workflow_lib.executor.process_task", side_effect=mock_process_task_capture_start)
        )
        
        # Simulate DAG loop calling _starting_stage_for
        from workflow_lib.executor import _starting_stage_for
        starting_stage = _starting_stage_for(full_task_id, state)
        
        # Verify _starting_stage_for returns review (not impl)
        assert starting_stage == STAGE_REVIEW, (
            f"Expected starting_stage={STAGE_REVIEW} (next after impl), got {starting_stage}"
        )
