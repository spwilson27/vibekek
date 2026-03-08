"""Tests for the fixup subcommand (formerly fix-requirements)."""

import json
import os
import re
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch, ANY

from workflow_lib.replan import cmd_fixup, _fix_task_mappings, _fix_phase_mappings


def _setup_dirs(tmp, phases_content, tasks_content, grouping_jsons=None, req_content=None):
    """Create a minimal docs/plan structure under a temp directory."""
    plan_dir = os.path.join(tmp, "docs", "plan")
    phases_dir = os.path.join(plan_dir, "phases")
    tasks_dir = os.path.join(plan_dir, "tasks")
    os.makedirs(phases_dir, exist_ok=True)
    os.makedirs(tasks_dir, exist_ok=True)

    for fname, content in phases_content.items():
        with open(os.path.join(phases_dir, fname), "w") as f:
            f.write(content)

    for relpath, content in tasks_content.items():
        full = os.path.join(tasks_dir, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    if grouping_jsons:
        for fname, data in grouping_jsons.items():
            with open(os.path.join(tasks_dir, fname), "w") as f:
                json.dump(data, f)

    if req_content:
        with open(os.path.join(tmp, "requirements.md"), "w") as f:
            f.write(req_content)

    return tasks_dir


def _make_args(dry_run=True, backend="gemini", model=None):
    return types.SimpleNamespace(dry_run=dry_run, backend=backend, model=model)


def _patches(tmp, tasks_dir):
    """Return a combined context manager patching ROOT_DIR and get_tasks_dir."""
    from contextlib import contextmanager

    @contextmanager
    def ctx():
        with patch("workflow_lib.replan.ROOT_DIR", tmp), \
             patch("workflow_lib.replan.get_tasks_dir", return_value=tasks_dir):
            yield
    return ctx()


def _mock_ctx():
    ctx = MagicMock()
    ctx.description_ctx = "test project"
    ctx.load_prompt.return_value = "prompt template"
    ctx.format_prompt.return_value = "formatted prompt"
    ctx.load_shared_components.return_value = ""
    return ctx


def _generation_patches(tmp, tasks_dir, mock_ctx):
    """Return a combined context manager for generation tests."""
    from contextlib import contextmanager

    @contextmanager
    def ctx():
        with _patches(tmp, tasks_dir), \
             patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan._rebuild_phase_dag") as mock_rebuild, \
             patch("workflow_lib.replan.load_replan_state", return_value={}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("workflow_lib.replan.log_action"):
            yield mock_rebuild
    return ctx()


# ── _run_all_checks tests ─────────────────────────────────────────────────


class TestRunAllChecks(unittest.TestCase):

    def test_returns_all_pass_when_no_artifacts(self):
        from workflow_lib.replan import _run_all_checks
        with patch("os.path.exists", return_value=False), \
             patch("os.path.isdir", return_value=False):
            results = _run_all_checks(quiet=True)
        self.assertTrue(results["all_pass"])
        self.assertEqual(results["checks"], {})

    def test_parses_missing_reqs_from_output(self):
        from workflow_lib.replan import _run_all_checks
        with patch("os.path.exists", return_value=True), \
             patch("os.path.isdir", return_value=True), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = types.SimpleNamespace(
                returncode=1,
                stdout="FAILED:\n  - [REQ-001]\n  - [REQ-002]\n",
            )
            results = _run_all_checks(quiet=True)
        self.assertFalse(results["all_pass"])
        # At least one check should have parsed reqs
        has_reqs = any(
            c.get("missing_reqs") for c in results["checks"].values()
        )
        self.assertTrue(has_reqs)


# ── _fix_task_mappings detection tests (dry-run only, no AI) ─────────────


class TestFixTaskMappingsDetection(unittest.TestCase):

    def test_no_unmapped_returns_false(self):
        result = _fix_task_mappings([], MagicMock())
        self.assertFalse(result)

    def test_detects_unmapped_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "[A-REQ-001] [A-REQ-002] [A-REQ-003]"},
                tasks_content={"phase_0/sub/01.md": "[A-REQ-001]"},
            )
            with _patches(tmp, tasks_dir):
                result = _fix_task_mappings(["A-REQ-002", "A-REQ-003"], MagicMock(), dry_run=True)
            self.assertTrue(result)

    def test_detects_exact_unmapped_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_1.md": "[X-REQ-001] [X-REQ-002] [X-REQ-003]"},
                tasks_content={"phase_1/epic/01.md": "[X-REQ-002]"},
            )

            unmapped = set()
            real_print = print

            def capture(*a, **kw):
                text = " ".join(str(x) for x in a)
                if text.strip().startswith("- ["):
                    m = re.search(r"\[([A-Z0-9_]+-[A-Z0-9\-_]+)\]", text)
                    if m:
                        unmapped.add(m.group(1))
                real_print(*a, **kw)

            with _patches(tmp, tasks_dir), \
                 patch("builtins.print", side_effect=capture):
                _fix_task_mappings(["X-REQ-001", "X-REQ-003"], MagicMock(), dry_run=True)

            self.assertEqual(unmapped, {"X-REQ-001", "X-REQ-003"})

    def test_missing_dirs_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patches(tmp, os.path.join(tmp, "nonexistent")):
                result = _fix_task_mappings(["REQ-001"], MagicMock())
            self.assertFalse(result)

    def test_multi_phase_unmapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={
                    "phase_0.md": "[A-REQ-001] [A-REQ-002]",
                    "phase_1.md": "[B-REQ-001]",
                },
                tasks_content={
                    "phase_0/sub/01.md": "[A-REQ-001]",
                    "phase_1/sub/01.md": "",
                },
            )

            unmapped = set()
            real_print = print

            def capture(*a, **kw):
                text = " ".join(str(x) for x in a)
                if text.strip().startswith("- ["):
                    m = re.search(r"\[([A-Z0-9_]+-[A-Z0-9\-_]+)\]", text)
                    if m:
                        unmapped.add(m.group(1))
                real_print(*a, **kw)

            with _patches(tmp, tasks_dir), \
                 patch("builtins.print", side_effect=capture):
                _fix_task_mappings(["A-REQ-002", "B-REQ-001"], MagicMock(), dry_run=True)

            self.assertEqual(unmapped, {"A-REQ-002", "B-REQ-001"})


# ── _fix_task_mappings generation tests (mocked AI runner) ───────────────


class TestFixTaskMappingsGeneration(unittest.TestCase):

    def test_generates_tasks_and_rebuilds_dag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "[Z-REQ-001] [Z-REQ-002]"},
                tasks_content={"phase_0/epic_a/01_task.md": "[Z-REQ-001]"},
                grouping_jsons={"phase_0_grouping.json": {"Epic A": ["Z-REQ-001", "Z-REQ-002"]}},
            )
            se_dir = os.path.join(tasks_dir, "phase_0", "epic_a")

            ctx = _mock_ctx()

            def fake_run_ai(prompt, allowed_files=None, sandbox=False):
                with open(os.path.join(se_dir, "02_fix.md"), "w") as f:
                    f.write("# Task\n## Covered Requirements\n- [Z-REQ-002]\n")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            ctx.run_ai = fake_run_ai

            with _generation_patches(tmp, tasks_dir, ctx) as mock_rebuild:
                _fix_task_mappings(["Z-REQ-002"], ctx)

            self.assertTrue(os.path.exists(os.path.join(se_dir, "02_fix.md")))
            with open(os.path.join(se_dir, "02_fix.md")) as f:
                self.assertIn("Z-REQ-002", f.read())
            mock_rebuild.assert_called_once()

    def test_ai_failure_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "[Z-REQ-001]"},
                tasks_content={"phase_0/sub/_placeholder": ""},
            )
            os.remove(os.path.join(tasks_dir, "phase_0", "sub", "_placeholder"))

            ctx = _mock_ctx()
            ctx.run_ai.return_value = types.SimpleNamespace(returncode=1, stdout="err", stderr="err")

            with _patches(tmp, tasks_dir):
                with self.assertRaises(SystemExit) as cm:
                    _fix_task_mappings(["Z-REQ-001"], ctx)
                self.assertEqual(cm.exception.code, 1)

    def test_grouping_json_selects_correct_sub_epic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "[S-REQ-001] [S-REQ-002]"},
                tasks_content={"phase_0/api_design/01.md": "[S-REQ-001]"},
                grouping_jsons={
                    "phase_0_grouping.json": {
                        "API Design": ["S-REQ-001"],
                        "Security Hardening": ["S-REQ-002"],
                    }
                },
            )
            os.makedirs(os.path.join(tasks_dir, "phase_0", "security_hardening"), exist_ok=True)

            captured = {}
            ctx = _mock_ctx()

            def capture_format(tmpl, **kwargs):
                if "target_dir" in kwargs:
                    captured["target_dir"] = kwargs["target_dir"]
                return "formatted"

            ctx.format_prompt.side_effect = capture_format
            ctx.run_ai.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with _generation_patches(tmp, tasks_dir, ctx):
                _fix_task_mappings(["S-REQ-002"], ctx)

            self.assertEqual(captured["target_dir"], "phase_0/security_hardening")

    def test_falls_back_to_first_sub_epic_without_grouping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "[F-REQ-001] [F-REQ-002]"},
                tasks_content={"phase_0/beta_epic/01.md": "[F-REQ-001]"},
            )
            os.makedirs(os.path.join(tasks_dir, "phase_0", "alpha_epic"), exist_ok=True)

            captured = {}
            ctx = _mock_ctx()

            def capture_format(tmpl, **kwargs):
                if "target_dir" in kwargs:
                    captured["target_dir"] = kwargs["target_dir"]
                return "formatted"

            ctx.format_prompt.side_effect = capture_format
            ctx.run_ai.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with _generation_patches(tmp, tasks_dir, ctx):
                _fix_task_mappings(["F-REQ-002"], ctx)

            self.assertEqual(captured["target_dir"], "phase_0/alpha_epic")


# ── _fix_phase_mappings tests ─────────────────────────────────────────────


class TestFixPhaseMappings(unittest.TestCase):

    def test_no_unmapped_returns_false(self):
        result = _fix_phase_mappings([], MagicMock())
        self.assertFalse(result)

    def test_dry_run_returns_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "# Phase 0\n## Requirements Covered\n"},
                tasks_content={},
                req_content="### **[R-001]** Some requirement\n",
            )
            with _patches(tmp, os.path.join(tmp, "docs", "plan", "tasks")):
                result = _fix_phase_mappings(["R-001"], MagicMock(), dry_run=True)
            self.assertTrue(result)

    def test_calls_ai_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "# Phase 0\n## Objective\nTest\n## Requirements Covered\n"},
                tasks_content={},
                req_content="### **[R-001]** Some requirement\n",
            )
            ctx = _mock_ctx()
            ctx.run_ai.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with _patches(tmp, os.path.join(tmp, "docs", "plan", "tasks")):
                result = _fix_phase_mappings(["R-001"], ctx)

            self.assertTrue(result)
            ctx.run_ai.assert_called_once()

    def test_ai_failure_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "# Phase 0\n"},
                tasks_content={},
                req_content="### **[R-001]** Some requirement\n",
            )
            ctx = _mock_ctx()
            ctx.run_ai.return_value = types.SimpleNamespace(returncode=1, stdout="err", stderr="err")

            with _patches(tmp, os.path.join(tmp, "docs", "plan", "tasks")):
                result = _fix_phase_mappings(["R-001"], ctx)

            self.assertFalse(result)


# ── Phase6AFixupValidation tests ─────────────────────────────────────────


class TestPhase6AFixupValidation(unittest.TestCase):

    def test_skips_when_already_completed(self):
        from workflow_lib.phases import Phase6AFixupValidation
        ctx = MagicMock()
        ctx.state = {"fixup_validation_completed": True}
        phase = Phase6AFixupValidation()
        # Should not raise
        phase.execute(ctx)

    def test_passes_when_all_checks_ok(self):
        from workflow_lib.phases import Phase6AFixupValidation
        ctx = MagicMock()
        ctx.state = {}

        with patch("workflow_lib.replan._run_all_checks", return_value={"all_pass": True, "checks": {}}):
            phase = Phase6AFixupValidation()
            phase.execute(ctx)

        self.assertTrue(ctx.state["fixup_validation_completed"])

    def test_exits_when_no_fixes_available(self):
        from workflow_lib.phases import Phase6AFixupValidation
        ctx = MagicMock()
        ctx.state = {}

        results = {
            "all_pass": False,
            "checks": {
                "verify-dags": {"passed": False, "missing_reqs": []},
            },
        }

        with patch("workflow_lib.replan._run_all_checks", return_value=results), \
             patch("workflow_lib.replan._fix_phase_mappings", return_value=False), \
             patch("workflow_lib.replan._fix_task_mappings", return_value=False):
            phase = Phase6AFixupValidation()
            with self.assertRaises(SystemExit):
                phase.execute(ctx)


# ── cmd_fixup tests ──────────────────────────────────────────────────────


class TestCmdFixup(unittest.TestCase):

    def test_all_pass_nothing_to_fix(self):
        with patch("workflow_lib.replan._run_all_checks") as mock_checks:
            mock_checks.return_value = {"all_pass": True, "checks": {}}
            cmd_fixup(_make_args(dry_run=False))

    def test_fixup_calls_fix_phases_then_tasks(self):
        results = {
            "all_pass": False,
            "checks": {
                "verify-phases": {"passed": False, "missing_reqs": ["R-001"]},
                "verify-tasks": {"passed": False, "missing_reqs": ["R-002"]},
            },
        }
        final_results = {"all_pass": True, "checks": {}}

        with patch("workflow_lib.replan._run_all_checks", side_effect=[results, final_results]), \
             patch("workflow_lib.replan._fix_phase_mappings", return_value=True) as mock_phases, \
             patch("workflow_lib.replan._fix_task_mappings", return_value=True) as mock_tasks, \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()), \
             patch("workflow_lib.replan.load_replan_state", return_value={}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("workflow_lib.replan.log_action"):
            cmd_fixup(_make_args(dry_run=False))

        mock_phases.assert_called_once_with(["R-001"], ANY, dry_run=False)
        mock_tasks.assert_called_once_with(["R-002"], ANY, dry_run=False)

    def test_fixup_dry_run_no_ai(self):
        results = {
            "all_pass": False,
            "checks": {
                "verify-tasks": {"passed": False, "missing_reqs": ["R-001"]},
            },
        }

        with patch("workflow_lib.replan._run_all_checks", return_value=results), \
             patch("workflow_lib.replan._fix_task_mappings", return_value=True) as mock_tasks, \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()):
            cmd_fixup(_make_args(dry_run=True))

        mock_tasks.assert_called_once_with(["R-001"], ANY, dry_run=True)

    def test_fixup_exits_when_no_fixes_available(self):
        results = {
            "all_pass": False,
            "checks": {
                "verify-dags": {"passed": False, "missing_reqs": []},
            },
        }

        with patch("workflow_lib.replan._run_all_checks", return_value=results), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()):
            with self.assertRaises(SystemExit) as cm:
                cmd_fixup(_make_args(dry_run=False))
            self.assertEqual(cm.exception.code, 1)

    def test_fixup_exits_when_still_failing_after_fix(self):
        results = {
            "all_pass": False,
            "checks": {
                "verify-tasks": {"passed": False, "missing_reqs": ["R-001"]},
            },
        }
        final_results = {
            "all_pass": False,
            "checks": {
                "verify-tasks": {"passed": False, "missing_reqs": ["R-001"]},
            },
        }

        with patch("workflow_lib.replan._run_all_checks", side_effect=[results, final_results]), \
             patch("workflow_lib.replan._fix_task_mappings", return_value=True), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()), \
             patch("workflow_lib.replan.load_replan_state", return_value={}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("workflow_lib.replan.log_action"):
            with self.assertRaises(SystemExit) as cm:
                cmd_fixup(_make_args(dry_run=False))
            self.assertEqual(cm.exception.code, 1)


# ── CLI wiring test ──────────────────────────────────────────────────────


class TestFixupCLIWiring(unittest.TestCase):

    def test_command_in_dispatch_table(self):
        from workflow_lib.cli import main
        import inspect
        source = inspect.getsource(main)
        self.assertIn('"fixup"', source)
        self.assertIn("cmd_fixup", source)

    def test_import_exists(self):
        from workflow_lib.replan import cmd_fixup as fn
        self.assertTrue(callable(fn))


if __name__ == "__main__":
    unittest.main()
