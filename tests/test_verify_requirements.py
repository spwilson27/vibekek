"""Tests for verify.py — specifically verify_tasks exclusion of phase_removed.md.

Regression coverage for the bug where verify_tasks() included phase_removed.md
in its phase-requirement scan, causing all removed/aliased requirements to be
flagged as "unmapped" and triggering spurious task generation.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from verify import parse_requirements, verify_tasks, verify_req_desc_length


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

            result = verify_tasks(phases_dir, tasks_dir)
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

            result = verify_tasks(phases_dir, tasks_dir)
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

            result = verify_tasks(phases_dir, tasks_dir)
            self.assertEqual(result, 0)

    def test_no_phases_dir_returns_error(self):
        """Missing phases directory should return error code 1."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = os.path.join(tmp, "tasks")
            os.makedirs(tasks_dir)
            result = verify_tasks(
                os.path.join(tmp, "nonexistent_phases"), tasks_dir
            )
            self.assertEqual(result, 1)

    def test_no_tasks_dir_returns_error(self):
        """Missing tasks directory should return error code 1."""
        with tempfile.TemporaryDirectory() as tmp:
            phases_dir = os.path.join(tmp, "phases")
            os.makedirs(phases_dir)
            _write(os.path.join(phases_dir, "phase_0.md"), "[ACT-001]\n")
            result = verify_tasks(
                phases_dir, os.path.join(tmp, "nonexistent_tasks")
            )
            self.assertEqual(result, 1)


class TestVerifyDescriptionLength(unittest.TestCase):
    """Tests for verify_req_desc_length() - validates requirement descriptions are 10+ words."""

    def test_passes_with_long_enough_descriptions(self):
        """Requirements with descriptions of 10+ words should pass."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** This is a description that has more than ten words in it.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None

### **[REQ-002]** Second Requirement
- **Type:** Technical
- **Description:** The system must handle at least one thousand concurrent users without degradation.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 0)
            os.unlink(f.name)

    def test_fails_with_short_descriptions(self):
        """Requirements with descriptions shorter than 10 words should fail."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** This is too short.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None

### **[REQ-002]** Second Requirement
- **Type:** Technical
- **Description:** The system must handle at least one thousand concurrent users without degradation.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 1)
            os.unlink(f.name)

    def test_fails_with_dash_description(self):
        """Requirements with '-' as description should fail (common anti-pattern)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** -
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 1)
            os.unlink(f.name)

    def test_fails_with_empty_description(self):
        """Requirements with empty descriptions should fail."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:**
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 1)
            os.unlink(f.name)

    def test_fails_with_tbd_description(self):
        """Requirements with 'TBD' descriptions should fail."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** TBD
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 1)
            os.unlink(f.name)

    def test_exactly_ten_words_passes(self):
        """Requirements with exactly 10 words should pass."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** One two three four five six seven eight nine ten words exactly here.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 0)
            os.unlink(f.name)

    def test_nine_words_fails(self):
        """Requirements with exactly 9 words should fail."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** One two three four five six seven eight nine.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 1)
            os.unlink(f.name)

    def test_custom_min_words(self):
        """Custom min_words parameter should be respected."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** Five words in this one.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            # Should fail with min_words=10
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 1)
            # Should pass with min_words=5
            result = verify_req_desc_length(f.name, min_words=5)
            self.assertEqual(result, 0)
            os.unlink(f.name)

    def test_file_not_found(self):
        """Non-existent file should return error code 1."""
        result = verify_req_desc_length("/nonexistent/file.md", min_words=10)
        self.assertEqual(result, 1)

    def test_multiple_short_descriptions_reported(self):
        """Multiple short descriptions should all be reported in output."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** Too short.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None

### **[REQ-002]** Second Requirement
- **Type:** Technical
- **Description:** Also too brief.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None

### **[REQ-003]** Third Requirement
- **Type:** UX
- **Description:** This one is also not long enough at all.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 1)
            os.unlink(f.name)

    def test_multiline_description_counted_correctly(self):
        """Multi-line descriptions should have word count calculated correctly."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""
### **[REQ-001]** First Requirement
- **Type:** Functional
- **Description:** This is a multi-line description
    that spans across multiple lines
    and should have ten words total here.
- **Source:** Test Document (docs/test.md)
- **Dependencies:** None
""")
            f.flush()
            result = verify_req_desc_length(f.name, min_words=10)
            self.assertEqual(result, 0)
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
