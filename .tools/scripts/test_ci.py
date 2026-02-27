import pytest
import tempfile
import os
import json
from xtask import ci
from xtask.ci import MockShell, MockCiProvider, RealCiProvider, Workflow, PipelineInfo
from time import sleep


def test_get_remote_url_success():
    shell = MockShell()
    shell.push_output(b"https://gitlab.lan/foo/bar.git\n")
    url = ci.get_remote_url(shell, "origin")
    assert url == "https://gitlab.lan/foo/bar.git"


def test_get_gitlab_project_info_success():
    shell = MockShell()
    shell.push_output(b"", b"error", False)
    shell.push_output(b"git@gitlab.lan:group/proj.git\n")
    host, path = ci.get_gitlab_project_info(shell)
    assert host == "gitlab.lan"
    assert path == "group/proj"


def test_pick_pipeline_filtering():
    json = [
        {"id": 1, "ref": "other-branch", "web_url": "http://url/1", "status": "success", "created_at": "2024-01-01T00:00:00Z"},
        {"id": 2, "ref": "main", "web_url": "http://url/2", "status": "success", "created_at": "2024-01-01T00:00:01Z"}
    ]
    res = ci.pick_pipeline(json, "main")
    assert res is not None and res.id == 2
    res = ci.pick_pipeline(json, None)
    assert res is not None and res.id == 1
    res = ci.pick_pipeline(json, "missing")
    assert res is None


def test_trigger_new_pipeline_success():
    shell = MockShell()
    trigger_json = {"id": 200, "web_url": "http://url/200", "status": "created", "created_at": "2024-01-02T00:00:00Z"}
    shell.push_output(bytes(json.dumps(trigger_json), 'utf-8'))
    id, url, created = ci.trigger_new_pipeline(shell, "host", "project", "feature/new", "token")
    assert id == 200
    assert url == "http://url/200"


def test_search_pipeline_robust_found():
    shell = MockShell()
    commits_json = [{"id": "commit_sha", "message": "diff_md5:mock_diff\nbase_sha:mock_base"}]
    pipelines_json = [{"id": 100, "ref": "feature/branch", "web_url": "http://url", "status": "pending", "created_at": "2024-01-01T00:00:00Z"}]
    shell.push_output(bytes(json.dumps(commits_json), 'utf-8'))
    shell.push_output(bytes(json.dumps(pipelines_json), 'utf-8'))
    res = ci.search_pipeline_robust(shell, "host", "project", "mock_base", "feature/branch", "mock_diff", "token")
    assert res is not None
    assert res.id == 100
    assert res.branch == "feature/branch"


def test_poll_logs_logic():
    shell = MockShell()
    cursors = {}
    jobs_json = [{"id": 400, "name": "test-job", "status": "running", "started_at": "2024-01-01T00:00:00Z"}]
    shell.push_output(bytes(json.dumps(jobs_json), 'utf-8'))
    shell.push_output(b"line 1\nline 2\n")
    ci.poll_logs(shell, "host", "project", 123, "token", cursors)
    assert cursors.get(400) == 2
    shell.push_output(bytes(json.dumps(jobs_json), 'utf-8'))
    shell.push_output(b"line 1\nline 2\nline 3\n")
    ci.poll_logs(shell, "host", "project", 123, "token", cursors)
    assert cursors.get(400) == 3


def test_print_failed_jobs_logic():
    shell = MockShell()
    jobs_json = [{"id": 500, "name": "failed-job", "status": "failed"}, {"id": 501, "name": "success-job", "status": "success"}]
    shell.push_output(bytes(json.dumps(jobs_json), 'utf-8'))
    shell.push_output(b"error log line 1\nerror log line 2\n")
    ci.print_failed_jobs(shell, "host", "project", 123, "token")
    cmds = shell.recorded_commands
    assert any("pipelines/123/jobs" in c for c in cmds)
    assert any("jobs/500/trace" in c for c in cmds)


def test_calculate_metadata_with_mock_shell():
    shell = MockShell()
    shell.push_output(b"mock_sha\n")
    shell.push_output(b"some diff content")
    provider = RealCiProvider(shell)
    sha, diff_md5 = provider.calculate_metadata("/mock/dir")
    assert sha == "mock_sha"
    import hashlib
    expected = hashlib.md5(b"some diff content").hexdigest()
    assert diff_md5 == expected
    assert len(shell.recorded_commands) == 2
    assert any("rev-parse" in c for c in shell.recorded_commands)


def test_workflow_setup_logic():
    shell = MockShell()
    td = tempfile.TemporaryDirectory()
    root = td.name
    # prepare outputs for workflow git commands
    shell.push_output(b"https://gitlab.lan/repo.git\n")
    for _ in range(7):
        shell.push_output(b"", b"", True)
    shell.push_output(b"new_sha\n")
    shell.push_output(b"", b"", True)
    provider = RealCiProvider(shell)
    wf = provider.setup_workflow(root, "base_sha", "diff_md5")
    assert wf.sha == "new_sha"
    assert wf.branch.startswith("ci-test-")
    assert any("push" in c for c in shell.recorded_commands)

import os
import sys
import importlib.util
import pytest


# Load the ci.py module by path (module lives under .tools/scripts/ci.py)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ci_path = os.path.join(ROOT, '.tools', 'scripts', 'ci.py')
spec = importlib.util.spec_from_file_location("ci_mod", ci_path)
ci_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_mod)


def test_clean_with_existing_pipeline():
    provider = ci_mod.MockCiProvider()
    provider.existing_pipeline = ci_mod.PipelineInfo(42, "http://url", "running", provider.git_branch, "now")
    orch = ci_mod.CiOrchestrator(provider)
    orch._handle_clean()
    assert orch.pipeline_id == 42


def test_clean_without_existing_triggers_push_and_pipeline():
    provider = ci_mod.MockCiProvider()
    provider.existing_pipeline = None
    # ensure remote differs so temp push path is used
    provider.remote_branch_sha = "different"
    orch = ci_mod.CiOrchestrator(provider)
    orch._handle_clean()
    assert orch.pipeline_id == 123
    assert len(provider.push_log) > 0


def test_dirty_existing_pipeline():
    provider = ci_mod.MockCiProvider()
    provider.is_clean = False
    provider.existing_pipeline = ci_mod.PipelineInfo(77, "http://url", "running", "temp", "now")
    orch = ci_mod.CiOrchestrator(provider)
    orch._handle_dirty()
    assert orch.pipeline_id == 77


def test_dirty_trigger_new_pipeline():
    provider = ci_mod.MockCiProvider()
    provider.is_clean = False
    provider.existing_pipeline = None
    orch = ci_mod.CiOrchestrator(provider)
    orch._handle_dirty()
    assert orch.pipeline_id == 123


def test_monitor_success_and_failure():
    provider = ci_mod.MockCiProvider()
    orch = ci_mod.CiOrchestrator(provider)
    orch.pipeline_id = 1
    # success should return cleanly
    provider.pipeline_status = "success"
    orch._monitor()
    # failure should raise and call failure handler
    provider.pipeline_status = "failed"
    orch.pipeline_id = 2
    with pytest.raises(RuntimeError):
        orch._monitor()


def test_ensure_branch_pushed_with_provider_synced():
    provider = ci_mod.MockCiProvider()
    # make remote equal to local
    provider.local_head = "same"
    provider.remote_branch_sha = "same"
    branch, wf = ci_mod.ensure_branch_pushed_with_provider(provider, "/tmp", "feature/x")
    assert branch == "feature/x"
    assert wf is None


def test_ensure_branch_pushed_with_provider_pushes_temp():
    provider = ci_mod.MockCiProvider()
    provider.local_head = "sha_local"
    provider.remote_branch_sha = "sha_remote"
    branch, wf = ci_mod.ensure_branch_pushed_with_provider(provider, "/tmp", "feature/x")
    assert branch.startswith("ci-temp-")
    assert isinstance(wf, ci_mod.Workflow)
    assert wf.branch == branch
    assert wf.sha == provider.local_head
    assert any("HEAD:" in s for s in provider.push_log)


def test_run_with_provider_clean_and_dirty():
    # clean path
    provider = ci_mod.MockCiProvider()
    provider.is_clean = True
    provider.existing_pipeline = None
    provider.pipeline_status = "success"
    ci_mod.run_with_provider(provider)

    # dirty path
    provider = ci_mod.MockCiProvider()
    provider.is_clean = False
    provider.existing_pipeline = None
    provider.pipeline_status = "success"
    ci_mod.run_with_provider(provider)
