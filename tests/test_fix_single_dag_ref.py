"""Tests for _fix_single_dag_ref in workflow_lib.replan."""
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.replan import _fix_single_dag_ref

PHASE_PATH = "/project/docs/plan/tasks/phase_5"
PHASE_DIR  = "phase_5"


def _exists_only(*valid_paths):
    """Return an os.path.exists mock that is True only for the given paths."""
    return lambda p: p in valid_paths


# ---------------------------------------------------------------------------
# Cases that should return None (cross-phase → remove the edge)
# ---------------------------------------------------------------------------

def test_cross_phase_full_path_different_phase_returns_none():
    """docs/plan/tasks/phase_1/... in a phase_5 DAG must be removed."""
    ref = "docs/plan/tasks/phase_1/03_template_resolution_context/01_template_resolver_skeleton.md"
    with patch("os.path.exists", return_value=False):
        assert _fix_single_dag_ref(ref, PHASE_PATH, PHASE_DIR) is None


def test_cross_phase_full_path_any_other_phase_returns_none():
    """docs/plan/tasks/phase_3/... in a phase_5 DAG must also be removed."""
    ref = "docs/plan/tasks/phase_3/02_state_recovery_and_lifecycle/01_crash_recovery_logic.md"
    with patch("os.path.exists", return_value=False):
        assert _fix_single_dag_ref(ref, PHASE_PATH, PHASE_DIR) is None


def test_cross_phase_bare_ref_returns_none():
    """phase_1/sub_epic/task.md (no docs/ prefix) must be removed."""
    ref = "phase_1/03_template_resolution_context/01_template_resolver_skeleton.md"
    with patch("os.path.exists", return_value=False):
        assert _fix_single_dag_ref(ref, PHASE_PATH, PHASE_DIR) is None


# ---------------------------------------------------------------------------
# Cases that should return a corrected (or unchanged) reference
# ---------------------------------------------------------------------------

def test_already_valid_ref_returned_unchanged():
    local_ref = "23_risk_006_verification/02_template_resolver_single_pass.md"
    full = os.path.join(PHASE_PATH, local_ref)
    with patch("os.path.exists", side_effect=_exists_only(full)):
        assert _fix_single_dag_ref(local_ref, PHASE_PATH, PHASE_DIR) == local_ref


def test_same_phase_full_path_is_stripped():
    """docs/plan/tasks/phase_5/foo/bar.md → foo/bar.md"""
    ref = "docs/plan/tasks/phase_5/23_risk_006_verification/02_template_resolver_single_pass.md"
    local = "23_risk_006_verification/02_template_resolver_single_pass.md"
    full  = os.path.join(PHASE_PATH, local)
    with patch("os.path.exists", side_effect=_exists_only(full)):
        assert _fix_single_dag_ref(ref, PHASE_PATH, PHASE_DIR) == local


def test_same_phase_dir_prefix_is_stripped():
    """phase_5/foo/bar.md → foo/bar.md"""
    ref   = "phase_5/23_risk_006_verification/02_template_resolver_single_pass.md"
    local = "23_risk_006_verification/02_template_resolver_single_pass.md"
    full  = os.path.join(PHASE_PATH, local)
    with patch("os.path.exists", side_effect=_exists_only(full)):
        assert _fix_single_dag_ref(ref, PHASE_PATH, PHASE_DIR) == local


def test_dotdot_prefix_is_stripped():
    """../foo/bar.md → foo/bar.md when the resolved path exists."""
    ref   = "../23_risk_006_verification/02_template_resolver_single_pass.md"
    local = "23_risk_006_verification/02_template_resolver_single_pass.md"
    full  = os.path.join(PHASE_PATH, local)
    with patch("os.path.exists", side_effect=_exists_only(full)):
        assert _fix_single_dag_ref(ref, PHASE_PATH, PHASE_DIR) == local


def test_unknown_broken_ref_returned_as_is():
    """An unrecognised broken ref is passed through for validation to catch."""
    ref = "completely/unknown/ref.md"
    with patch("os.path.exists", return_value=False):
        assert _fix_single_dag_ref(ref, PHASE_PATH, PHASE_DIR) == ref
