"""Tests for verify_requirements.py — specifically verify_tasks exclusion of phase_removed.md.

Regression coverage for the bug where verify_tasks() included phase_removed.md
in its phase-requirement scan, causing all removed/aliased requirements to be
flagged as "unmapped" and triggering spurious task generation.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import verify_requirements


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class TestVerifyTasksExcludesPhaseRemoved(unittest.TestCase):
    """verify_tasks() must ignore phase_removed.md when building the phase req set."""

    def test_removed_reqs_not_flagged_as_unmapped(self):
        """A req only in phase_removed.md must NOT be reported as missing from tasks."""
        with tempfile.TemporaryDirectory() as tmp:
            phases_dir = os.path.join(tmp, "phases")
            tasks_dir = os.path.join(tmp, "tasks")

            _write(os.path.join(phases_dir, "phase_0.md"), "[ACT-001]\n")
            _write(os.path.join(phases_dir, "phase_removed.md"), "[REM-001]\n")
            # Task covers the active req; no task for the removed req
            _write(os.path.join(tasks_dir, "phase_0", "sub", "01_task.md"),
                   "[ACT-001]\n")

            result = verify_requirements.verify_tasks(phases_dir, tasks_dir)
            self.assertEqual(result, 0, "verify_tasks should pass — REM-001 is removed")

    def test_active_reqs_still_enforced(self):
        """An active-phase req not covered by any task must still be flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            phases_dir = os.path.join(tmp, "phases")
            tasks_dir = os.path.join(tmp, "tasks")

            _write(os.path.join(phases_dir, "phase_0.md"), "[ACT-001]\n")
            _write(os.path.join(phases_dir, "phase_removed.md"), "[REM-001]\n")
            # No task covers ACT-001
            os.makedirs(os.path.join(tasks_dir, "phase_0", "sub"), exist_ok=True)

            result = verify_requirements.verify_tasks(phases_dir, tasks_dir)
            self.assertEqual(result, 1, "verify_tasks should fail — ACT-001 is unmapped")

    def test_all_active_reqs_covered_across_multiple_phases(self):
        """Multiple active phases all covered; phase_removed reqs absent from tasks → pass."""
        with tempfile.TemporaryDirectory() as tmp:
            phases_dir = os.path.join(tmp, "phases")
            tasks_dir = os.path.join(tmp, "tasks")

            _write(os.path.join(phases_dir, "phase_0.md"), "[ACT-001]\n")
            _write(os.path.join(phases_dir, "phase_1.md"), "[ACT-002]\n")
            _write(os.path.join(phases_dir, "phase_removed.md"), "[REM-001]\n[REM-002]\n")
            _write(os.path.join(tasks_dir, "phase_0", "sub", "01.md"), "[ACT-001]\n")
            _write(os.path.join(tasks_dir, "phase_1", "sub", "01.md"), "[ACT-002]\n")
            # Deliberately no tasks for REM-001 or REM-002

            result = verify_requirements.verify_tasks(phases_dir, tasks_dir)
            self.assertEqual(result, 0)

    def test_no_phases_dir_returns_error(self):
        """Missing phases directory should return error code 1."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = os.path.join(tmp, "tasks")
            os.makedirs(tasks_dir)
            result = verify_requirements.verify_tasks(
                os.path.join(tmp, "nonexistent_phases"), tasks_dir
            )
            self.assertEqual(result, 1)

    def test_no_tasks_dir_returns_error(self):
        """Missing tasks directory should return error code 1."""
        with tempfile.TemporaryDirectory() as tmp:
            phases_dir = os.path.join(tmp, "phases")
            os.makedirs(phases_dir)
            _write(os.path.join(phases_dir, "phase_0.md"), "[ACT-001]\n")
            result = verify_requirements.verify_tasks(
                phases_dir, os.path.join(tmp, "nonexistent_tasks")
            )
            self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
