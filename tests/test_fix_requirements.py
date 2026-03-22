"""Tests for the fixup subcommand (formerly fix-requirements)."""

import json
import os
import re
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch, ANY

from workflow_lib.replan import (
    cmd_fixup, _fix_task_mappings, _fix_phase_mappings,
    _fix_dag_references, _fix_single_dag_ref,
)


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

    def test_phase_removed_reqs_not_treated_as_unmapped(self):
        """Requirements only in phase_removed.md are ignored by _fix_task_mappings."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={
                    "phase_0.md": "[ACT-001]",
                    "phase_removed.md": "[REM-001]",
                },
                tasks_content={"phase_0/sub/01.md": "[ACT-001]"},
            )
            with _patches(tmp, tasks_dir):
                result = _fix_task_mappings(["REM-001"], MagicMock(), dry_run=True)
            # REM-001 is only in phase_removed.md — nothing to fix
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

            def fake_run_ai(prompt, allowed_files=None):
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
             patch("workflow_lib.replan._fix_dag_references", return_value=0), \
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


# ── _fix_single_dag_ref tests ─────────────────────────────────────────────


class TestFixSingleDagRef(unittest.TestCase):

    def test_valid_ref_returned_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "04_foo"))
            task = os.path.join(tmp, "04_foo", "01_bar.md")
            with open(task, "w") as f:
                f.write("")
            result = _fix_single_dag_ref("04_foo/01_bar.md", tmp, "phase_0")
            self.assertEqual(result, "04_foo/01_bar.md")

    def test_dotdot_prefix_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "14_risk"))
            task = os.path.join(tmp, "14_risk", "01_plans.md")
            with open(task, "w") as f:
                f.write("")
            result = _fix_single_dag_ref("../14_risk/01_plans.md", tmp, "phase_0")
            self.assertEqual(result, "14_risk/01_plans.md")

    def test_same_phase_prefix_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "02_css"))
            task = os.path.join(tmp, "02_css", "07_spec.md")
            with open(task, "w") as f:
                f.write("")
            result = _fix_single_dag_ref("phase_2/02_css/07_spec.md", tmp, "phase_2")
            self.assertEqual(result, "02_css/07_spec.md")

    def test_cross_phase_ref_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _fix_single_dag_ref(
                "phase_1/01_crate/04_backend.md", tmp, "phase_3"
            )
            self.assertIsNone(result)

    def test_unknown_broken_ref_returned_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _fix_single_dag_ref("nonexistent/task.md", tmp, "phase_0")
            self.assertEqual(result, "nonexistent/task.md")


# ── _fix_dag_references tests ────────────────────────────────────────────


class TestFixDagReferences(unittest.TestCase):

    def _make_phase_dir(self, tmp, phase_name, dag, task_files):
        """Create a phase directory with a DAG and task files."""
        tasks_dir = os.path.join(tmp, "docs", "plan", "tasks")
        phase_dir = os.path.join(tasks_dir, phase_name)
        os.makedirs(phase_dir, exist_ok=True)
        with open(os.path.join(phase_dir, "dag.json"), "w") as f:
            json.dump(dag, f)
        for rel_path in task_files:
            full = os.path.join(phase_dir, rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(f"# Task\n")
        return tasks_dir

    def test_fixes_dotdot_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag = {
                "14_risk/01_plans.md": [],
                "15_gov/02_lifecycle.md": [
                    "../14_risk/01_plans.md"
                ],
            }
            tasks_dir = self._make_phase_dir(tmp, "phase_0", dag, [
                "14_risk/01_plans.md",
                "15_gov/02_lifecycle.md",
            ])
            with _patches(tmp, tasks_dir):
                fixes = _fix_dag_references()
            self.assertGreater(fixes, 0)
            with open(os.path.join(tasks_dir, "phase_0", "dag.json")) as f:
                fixed_dag = json.load(f)
            self.assertIn("14_risk/01_plans.md", fixed_dag["15_gov/02_lifecycle.md"])
            self.assertNotIn("../14_risk/01_plans.md", fixed_dag["15_gov/02_lifecycle.md"])

    def test_fixes_same_phase_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag = {
                "02_css/07_spec.md": [],
                "03_style/03_cascade.md": [
                    "phase_2/02_css/07_spec.md"
                ],
            }
            tasks_dir = self._make_phase_dir(tmp, "phase_2", dag, [
                "02_css/07_spec.md",
                "03_style/03_cascade.md",
            ])
            with _patches(tmp, tasks_dir):
                fixes = _fix_dag_references()
            self.assertGreater(fixes, 0)
            with open(os.path.join(tasks_dir, "phase_2", "dag.json")) as f:
                fixed_dag = json.load(f)
            self.assertEqual(
                fixed_dag["03_style/03_cascade.md"],
                ["02_css/07_spec.md"],
            )

    def test_removes_cross_phase_dep(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag = {
                "01_render/01_pixel.md": [
                    "phase_1/10_mcp/03_buffer.md"
                ],
            }
            tasks_dir = self._make_phase_dir(tmp, "phase_3", dag, [
                "01_render/01_pixel.md",
            ])
            with _patches(tmp, tasks_dir):
                fixes = _fix_dag_references()
            self.assertGreater(fixes, 0)
            with open(os.path.join(tasks_dir, "phase_3", "dag.json")) as f:
                fixed_dag = json.load(f)
            self.assertEqual(fixed_dag["01_render/01_pixel.md"], [])

    def test_no_changes_when_dag_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag = {
                "01_foo/01_bar.md": [],
                "01_foo/02_baz.md": ["01_foo/01_bar.md"],
            }
            tasks_dir = self._make_phase_dir(tmp, "phase_0", dag, [
                "01_foo/01_bar.md",
                "01_foo/02_baz.md",
            ])
            with _patches(tmp, tasks_dir):
                fixes = _fix_dag_references()
            self.assertEqual(fixes, 0)

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag = {
                "14_risk/01_plans.md": [],
                "15_gov/02_lifecycle.md": ["../14_risk/01_plans.md"],
            }
            tasks_dir = self._make_phase_dir(tmp, "phase_0", dag, [
                "14_risk/01_plans.md",
                "15_gov/02_lifecycle.md",
            ])
            with _patches(tmp, tasks_dir):
                fixes = _fix_dag_references(dry_run=True)
            self.assertGreater(fixes, 0)
            # File should still have the broken ref
            with open(os.path.join(tasks_dir, "phase_0", "dag.json")) as f:
                unchanged = json.load(f)
            self.assertIn("../14_risk/01_plans.md", unchanged["15_gov/02_lifecycle.md"])

    def test_orphan_triggers_dag_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag = {
                "01_foo/01_bar.md": [],
            }
            tasks_dir = self._make_phase_dir(tmp, "phase_0", dag, [
                "01_foo/01_bar.md",
                "01_foo/02_orphan.md",  # on disk but not in DAG
            ])
            with _patches(tmp, tasks_dir), \
                 patch("workflow_lib.replan._rebuild_phase_dag") as mock_rebuild:
                fixes = _fix_dag_references()
            self.assertGreater(fixes, 0)
            mock_rebuild.assert_called_once()

    def test_multiple_fixes_in_single_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag = {
                "01_a/01_x.md": [],
                "02_b/01_y.md": [
                    "../01_a/01_x.md",
                    "phase_0/01_a/01_x.md",
                ],
            }
            tasks_dir = self._make_phase_dir(tmp, "phase_0", dag, [
                "01_a/01_x.md",
                "02_b/01_y.md",
            ])
            with _patches(tmp, tasks_dir):
                fixes = _fix_dag_references()
            self.assertEqual(fixes, 2)
            with open(os.path.join(tasks_dir, "phase_0", "dag.json")) as f:
                fixed_dag = json.load(f)
            # Both should resolve to the same valid ref
            self.assertEqual(
                fixed_dag["02_b/01_y.md"],
                ["01_a/01_x.md", "01_a/01_x.md"],
            )


# ── cmd_fixup DAG integration tests ─────────────────────────────────────


class TestCmdFixupDags(unittest.TestCase):

    def test_fixup_calls_fix_dag_references_on_dag_failure(self):
        results = {
            "all_pass": False,
            "checks": {
                "verify-dags": {"passed": False, "output": "FAILED", "missing_reqs": []},
            },
        }
        final_results = {"all_pass": True, "checks": {}}

        with patch("workflow_lib.replan._run_all_checks", side_effect=[results, final_results]), \
             patch("workflow_lib.replan._fix_dag_references", return_value=3) as mock_fix_dags, \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()), \
             patch("workflow_lib.replan.load_replan_state", return_value={}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("workflow_lib.replan.log_action"):
            cmd_fixup(_make_args(dry_run=False))

        mock_fix_dags.assert_called_once_with(dry_run=False, ctx=ANY)

    def test_fixup_skips_dags_when_dags_pass(self):
        results = {
            "all_pass": False,
            "checks": {
                "verify-dags": {"passed": True, "output": "", "missing_reqs": []},
                "verify-tasks": {"passed": False, "missing_reqs": ["R-001"]},
            },
        }
        final_results = {"all_pass": True, "checks": {}}

        with patch("workflow_lib.replan._run_all_checks", side_effect=[results, final_results]), \
             patch("workflow_lib.replan._fix_dag_references") as mock_fix_dags, \
             patch("workflow_lib.replan._fix_task_mappings", return_value=True), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()), \
             patch("workflow_lib.replan.load_replan_state", return_value={}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("workflow_lib.replan.log_action"):
            cmd_fixup(_make_args(dry_run=False))

        mock_fix_dags.assert_not_called()

    def test_fixup_exits_when_dag_fix_returns_zero(self):
        results = {
            "all_pass": False,
            "checks": {
                "verify-dags": {"passed": False, "output": "FAILED", "missing_reqs": []},
            },
        }

        with patch("workflow_lib.replan._run_all_checks", return_value=results), \
             patch("workflow_lib.replan._fix_dag_references", return_value=0), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()):
            with self.assertRaises(SystemExit) as cm:
                cmd_fixup(_make_args(dry_run=False))
            self.assertEqual(cm.exception.code, 1)


# ── _rebuild_phase_dag tests ─────────────────────────────────────────────


class TestRebuildPhaseDag(unittest.TestCase):
    """E2E tests for _rebuild_phase_dag validation and retry logic."""

    def _make_phase(self, tmp, tasks):
        """Create a minimal phase directory with the given task files."""
        phase_dir = os.path.join(tmp, "phase_1")
        for sub_epic, fname in tasks:
            se_dir = os.path.join(phase_dir, sub_epic)
            os.makedirs(se_dir, exist_ok=True)
            with open(os.path.join(se_dir, fname), "w") as f:
                f.write("# Task\n\ndepends_on: []\n")
        return phase_dir

    def test_programmatic_dag_valid_writes_without_ai(self):
        """When programmatic DAG passes validation, it is written and AI is not called."""
        from workflow_lib.replan import _rebuild_phase_dag

        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = self._make_phase(tmp, [("sub", "01_task.md")])
            ctx = MagicMock()
            dag = {"sub/01_task.md": []}

            with patch.object(
                __import__("workflow_lib.phases", fromlist=["Phase7ADAGGeneration"]).Phase7ADAGGeneration,
                "_build_programmatic_dag", return_value=dag
            ):
                _rebuild_phase_dag(phase_dir, ctx)

            ctx.run_ai.assert_not_called()
            dag_file = os.path.join(phase_dir, "dag.json")
            self.assertTrue(os.path.exists(dag_file))
            with open(dag_file) as f:
                written = json.load(f)
            self.assertEqual(written, dag)

    def test_programmatic_dag_invalid_falls_back_to_ai(self):
        """When programmatic DAG fails _validate_dag, AI fallback is used."""
        from workflow_lib.replan import _rebuild_phase_dag

        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = self._make_phase(tmp, [("sub", "01_task.md"), ("sub", "02_task.md")])
            ctx = MagicMock()
            ctx.load_prompt.return_value = "tmpl"
            ctx.format_prompt.return_value = "prompt"
            # Programmatic DAG is missing sub/02_task.md -> validation fails
            partial_dag = {"sub/01_task.md": []}
            ai_dag = {"sub/01_task.md": [], "sub/02_task.md": []}

            dag_file = os.path.join(phase_dir, "dag.json")

            def fake_run_ai(*args, **kwargs):
                with open(dag_file, "w") as f:
                    json.dump(ai_dag, f)
                return MagicMock(returncode=0)

            ctx.run_ai.side_effect = fake_run_ai

            with patch.object(
                __import__("workflow_lib.phases", fromlist=["Phase7ADAGGeneration"]).Phase7ADAGGeneration,
                "_build_programmatic_dag", return_value=partial_dag
            ):
                _rebuild_phase_dag(phase_dir, ctx)

            ctx.run_ai.assert_called_once()
            with open(dag_file) as f:
                written = json.load(f)
            self.assertEqual(written, ai_dag)

    def test_ai_fallback_retries_on_validation_failure(self):
        """AI fallback retries up to 3 times when DAG validation fails."""
        from workflow_lib.replan import _rebuild_phase_dag

        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = self._make_phase(tmp, [("sub", "01_task.md")])
            ctx = MagicMock()
            ctx.load_prompt.return_value = "tmpl"
            ctx.format_prompt.return_value = "prompt"

            dag_file = os.path.join(phase_dir, "dag.json")
            call_count = [0]

            def fake_run_ai(*args, **kwargs):
                call_count[0] += 1
                # First 2 calls produce a DAG with a phantom file (fails validation)
                # 3rd call produces a valid DAG
                if call_count[0] < 3:
                    bad_dag = {"sub/01_task.md": [], "sub/PHANTOM.md": []}
                    with open(dag_file, "w") as f:
                        json.dump(bad_dag, f)
                else:
                    good_dag = {"sub/01_task.md": []}
                    with open(dag_file, "w") as f:
                        json.dump(good_dag, f)
                return MagicMock(returncode=0)

            ctx.run_ai.side_effect = fake_run_ai

            with patch.object(
                __import__("workflow_lib.phases", fromlist=["Phase7ADAGGeneration"]).Phase7ADAGGeneration,
                "_build_programmatic_dag", return_value=None
            ):
                _rebuild_phase_dag(phase_dir, ctx)

            self.assertEqual(call_count[0], 3)
            with open(dag_file) as f:
                written = json.load(f)
            self.assertEqual(written, {"sub/01_task.md": []})

    def test_ai_fallback_gives_up_after_3_attempts(self):
        """AI fallback stops after 3 failed attempts and leaves no dag.json."""
        from workflow_lib.replan import _rebuild_phase_dag

        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = self._make_phase(tmp, [("sub", "01_task.md")])
            ctx = MagicMock()
            ctx.load_prompt.return_value = "tmpl"
            ctx.format_prompt.return_value = "prompt"

            dag_file = os.path.join(phase_dir, "dag.json")

            def fake_run_ai(*args, **kwargs):
                # Always produce a DAG with phantom file -> always fails validation
                bad_dag = {"sub/PHANTOM.md": []}
                with open(dag_file, "w") as f:
                    json.dump(bad_dag, f)
                return MagicMock(returncode=0)

            ctx.run_ai.side_effect = fake_run_ai

            with patch.object(
                __import__("workflow_lib.phases", fromlist=["Phase7ADAGGeneration"]).Phase7ADAGGeneration,
                "_build_programmatic_dag", return_value=None
            ):
                _rebuild_phase_dag(phase_dir, ctx)

            self.assertEqual(ctx.run_ai.call_count, 3)
            # After all retries fail, the bad dag.json should have been removed
            self.assertFalse(os.path.exists(dag_file))


# ── orphan detection depth tests ─────────────────────────────────────────


class TestFixDagOrphanDepth(unittest.TestCase):
    """Tests that _fix_dag_references orphan detection uses 2-level depth."""

    def test_deeply_nested_file_not_treated_as_orphan(self):
        """A file at sub/nested/deep.md is ignored by orphan detection."""
        with tempfile.TemporaryDirectory() as tmp:
            dag = {"sub/01_task.md": []}
            tasks_dir = os.path.join(tmp, "docs", "plan", "tasks")
            phase_dir = os.path.join(tasks_dir, "phase_0")
            os.makedirs(phase_dir, exist_ok=True)
            with open(os.path.join(phase_dir, "dag.json"), "w") as f:
                json.dump(dag, f)
            # Create the valid task
            os.makedirs(os.path.join(phase_dir, "sub"), exist_ok=True)
            with open(os.path.join(phase_dir, "sub", "01_task.md"), "w") as f:
                f.write("# Task\n")
            # Create a deeply-nested file that should NOT be treated as an orphan
            os.makedirs(os.path.join(phase_dir, "sub", "nested"), exist_ok=True)
            with open(os.path.join(phase_dir, "sub", "nested", "deep.md"), "w") as f:
                f.write("# Deep\n")

            with patch("workflow_lib.replan.ROOT_DIR", tmp), \
                 patch("workflow_lib.replan.get_tasks_dir", return_value=tasks_dir), \
                 patch("workflow_lib.replan._rebuild_phase_dag") as mock_rebuild:
                fixes = _fix_dag_references()

            # No rebuild should have been triggered by the deep file
            mock_rebuild.assert_not_called()
            self.assertEqual(fixes, 0)

    def test_shallow_orphan_still_triggers_rebuild(self):
        """A file at sub/02_orphan.md (2-level) does trigger a rebuild."""
        with tempfile.TemporaryDirectory() as tmp:
            dag = {"sub/01_task.md": []}
            tasks_dir = os.path.join(tmp, "docs", "plan", "tasks")
            phase_dir = os.path.join(tasks_dir, "phase_0")
            os.makedirs(phase_dir, exist_ok=True)
            with open(os.path.join(phase_dir, "dag.json"), "w") as f:
                json.dump(dag, f)
            os.makedirs(os.path.join(phase_dir, "sub"), exist_ok=True)
            with open(os.path.join(phase_dir, "sub", "01_task.md"), "w") as f:
                f.write("# Task\n")
            # Add a real orphan at 2-level depth
            with open(os.path.join(phase_dir, "sub", "02_orphan.md"), "w") as f:
                f.write("# Orphan\n")

            with patch("workflow_lib.replan.ROOT_DIR", tmp), \
                 patch("workflow_lib.replan.get_tasks_dir", return_value=tasks_dir), \
                 patch("workflow_lib.replan._rebuild_phase_dag") as mock_rebuild:
                fixes = _fix_dag_references()

            mock_rebuild.assert_called_once()
            self.assertGreater(fixes, 0)


# ── phase_removed regression tests ───────────────────────────────────────


class TestCmdFixupPhaseRemovedRegression(unittest.TestCase):
    """Regression: cmd_fixup must not generate tasks for phase_removed requirements."""

    def test_fixup_all_pass_when_only_removed_reqs_are_untracked(self):
        """When the only uncovered reqs live in phase_removed.md, fixup reports all-pass."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={
                    "phase_0.md": "[ACT-001]",
                    "phase_removed.md": "[REM-001]",
                },
                tasks_content={"phase_0/sub/01.md": "[ACT-001]"},
            )
            args = _make_args(dry_run=True)

            with _patches(tmp, tasks_dir), \
                 patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()), \
                 patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
                 patch("workflow_lib.replan.load_replan_state", return_value={}), \
                 patch("workflow_lib.replan.save_replan_state"), \
                 patch("workflow_lib.replan.log_action"), \
                 patch("subprocess.run") as mock_sp, \
                 patch("workflow_lib.replan._fix_task_mappings") as mock_fix_tasks:
                # verify-tasks passes (no missing reqs after the fix)
                mock_sp.return_value = types.SimpleNamespace(
                    returncode=0,
                    stdout="Success: All requirements mapped.\n",
                )
                cmd_fixup(args)
                # The fixer must not be invoked for removed reqs
                mock_fix_tasks.assert_not_called()

    def test_no_task_files_created_under_phase_removed_dir(self):
        """cmd_fixup must not create any task files under tasks/phase_removed/."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={
                    "phase_0.md": "[ACT-001]",
                    "phase_removed.md": "[REM-001] [REM-002] [REM-003]",
                },
                tasks_content={"phase_0/sub/01.md": "[ACT-001]"},
            )
            args = _make_args(dry_run=True)

            with _patches(tmp, tasks_dir), \
                 patch("workflow_lib.replan.ProjectContext", return_value=MagicMock()), \
                 patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
                 patch("workflow_lib.replan.load_replan_state", return_value={}), \
                 patch("workflow_lib.replan.save_replan_state"), \
                 patch("workflow_lib.replan.log_action"), \
                 patch("subprocess.run") as mock_sp:
                mock_sp.return_value = types.SimpleNamespace(
                    returncode=0,
                    stdout="Success: All requirements mapped.\n",
                )
                cmd_fixup(args)

            phase_removed_dir = os.path.join(tasks_dir, "phase_removed")
            # No phase_removed/ task directory should exist
            self.assertFalse(
                os.path.isdir(phase_removed_dir),
                "phase_removed task directory should not be created",
            )


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
