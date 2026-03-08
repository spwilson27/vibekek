"""Tests for _compact_task_id log prefix generation."""

import pytest
from workflow_lib.executor import _compact_task_id


@pytest.mark.parametrize(
    "phase_id, task_name, expected",
    [
        # Typical two-level task path
        (
            "phase_1",
            "02_definition_of_done_guidelines/02_enforce_test_coverage_threshold.md",
            "p1/02/02_enforce_test",
        ),
        (
            "phase_1",
            "02_definition_of_done_guidelines/03_enforce_rustdoc_linting.md",
            "p1/02/03_enforce_rust",
        ),
        # Different phase numbers
        (
            "phase_3",
            "01_api/05_setup_auth.md",
            "p3/01/05_setup_auth",
        ),
        # Short leaf name (no truncation needed)
        (
            "phase_2",
            "01_core/01_init.md",
            "p2/01/01_init",
        ),
        # Single-level task (no sub-epic)
        (
            "phase_1",
            "01_simple_task.md",
            "p1/01_simple_task",
        ),
        # Long single-level task name gets truncated
        (
            "phase_1",
            "01_a_very_long_task_name_here.md",
            "p1/01_a_very_long_task_",
        ),
        # Non-numeric sub-epic prefix
        (
            "phase_1",
            "misc_stuff/02_do_thing.md",
            "p1/misc/02_do_thing",
        ),
        # Fallback when phase_id doesn't match pattern
        (
            "custom",
            "01_core/01_init.md",
            "custom/01/01_init",
        ),
    ],
)
def test_compact_task_id(phase_id: str, task_name: str, expected: str) -> None:
    assert _compact_task_id(phase_id, task_name) == expected


def test_sibling_tasks_are_distinguishable() -> None:
    """Tasks in the same sub-epic must produce different IDs."""
    id_a = _compact_task_id(
        "phase_1",
        "02_dod/02_enforce_test_coverage.md",
    )
    id_b = _compact_task_id(
        "phase_1",
        "02_dod/03_enforce_rustdoc.md",
    )
    assert id_a != id_b
