"""Tests for the fix-requirements subcommand."""

import json
import os
import re
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from workflow_lib.replan import cmd_fix_requirements


def _setup_dirs(tmp, phases_content, tasks_content, grouping_jsons=None):
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


def _generation_patches(tmp, tasks_dir, mock_ctx, verify_rc=0):
    """Return a combined context manager for generation tests."""
    from contextlib import contextmanager

    @contextmanager
    def ctx():
        with _patches(tmp, tasks_dir), \
             patch("workflow_lib.replan.ProjectContext", return_value=mock_ctx), \
             patch("workflow_lib.replan._make_runner", return_value=MagicMock()), \
             patch("workflow_lib.replan._rebuild_phase_dag") as mock_rebuild, \
             patch("workflow_lib.replan.subprocess") as mock_sp, \
             patch("workflow_lib.replan.load_replan_state", return_value={}), \
             patch("workflow_lib.replan.save_replan_state"), \
             patch("workflow_lib.replan.log_action"):
            mock_sp.run.return_value = types.SimpleNamespace(
                returncode=verify_rc, stdout="verify output"
            )
            yield mock_rebuild
    return ctx()


# ── Detection tests (dry-run only, no AI, no writes to host) ─────────────


class TestFixRequirementsDetection(unittest.TestCase):

    def test_all_mapped_nothing_to_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "[A-REQ-001] [A-REQ-002]"},
                tasks_content={"phase_0/sub/01.md": "[A-REQ-001] [A-REQ-002]"},
            )
            with _patches(tmp, tasks_dir):
                cmd_fix_requirements(_make_args(dry_run=True))

    def test_detects_unmapped_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "[A-REQ-001] [A-REQ-002] [A-REQ-003]"},
                tasks_content={"phase_0/sub/01.md": "[A-REQ-001]"},
            )
            with _patches(tmp, tasks_dir):
                cmd_fix_requirements(_make_args(dry_run=True))

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
                cmd_fix_requirements(_make_args(dry_run=True))

            self.assertEqual(unmapped, {"X-REQ-001", "X-REQ-003"})

    def test_no_phases_dir_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patches(tmp, os.path.join(tmp, "docs", "plan", "tasks")):
                with self.assertRaises(SystemExit) as cm:
                    cmd_fix_requirements(_make_args(dry_run=True))
                self.assertEqual(cm.exception.code, 1)

    def test_no_tasks_dir_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "docs", "plan", "phases"))
            with _patches(tmp, os.path.join(tmp, "nonexistent")):
                with self.assertRaises(SystemExit) as cm:
                    cmd_fix_requirements(_make_args(dry_run=True))
                self.assertEqual(cm.exception.code, 1)

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
                cmd_fix_requirements(_make_args(dry_run=True))

            self.assertEqual(unmapped, {"A-REQ-002", "B-REQ-001"})


# ── Generation tests (mocked AI runner) ─────────────────────────────────


class TestFixRequirementsGeneration(unittest.TestCase):

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
                cmd_fix_requirements(_make_args(dry_run=False))

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

            with _patches(tmp, tasks_dir), \
                 patch("workflow_lib.replan.ProjectContext", return_value=ctx), \
                 patch("workflow_lib.replan._make_runner", return_value=MagicMock()):
                with self.assertRaises(SystemExit) as cm:
                    cmd_fix_requirements(_make_args(dry_run=False))
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
                cmd_fix_requirements(_make_args(dry_run=False))

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
                cmd_fix_requirements(_make_args(dry_run=False))

            self.assertEqual(captured["target_dir"], "phase_0/alpha_epic")

    def test_verification_failure_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _setup_dirs(
                tmp,
                phases_content={"phase_0.md": "[V-REQ-001]"},
                tasks_content={"phase_0/sub/_placeholder": ""},
            )
            os.remove(os.path.join(tasks_dir, "phase_0", "sub", "_placeholder"))

            ctx = _mock_ctx()
            ctx.run_ai.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")

            printed = []
            real_print = print

            def capture(*a, **kw):
                printed.append(" ".join(str(x) for x in a))
                real_print(*a, **kw)

            with _generation_patches(tmp, tasks_dir, ctx, verify_rc=1), \
                 patch("builtins.print", side_effect=capture):
                cmd_fix_requirements(_make_args(dry_run=False))

            self.assertTrue(any("FAIL" in p for p in printed))


# ── CLI wiring test ──────────────────────────────────────────────────────


class TestFixRequirementsCLIWiring(unittest.TestCase):

    def test_command_in_dispatch_table(self):
        from workflow_lib.cli import main
        import inspect
        source = inspect.getsource(main)
        self.assertIn('"fix-requirements"', source)
        self.assertIn("cmd_fix_requirements", source)

    def test_import_exists(self):
        from workflow_lib.replan import cmd_fix_requirements as fn
        self.assertTrue(callable(fn))


if __name__ == "__main__":
    unittest.main()
