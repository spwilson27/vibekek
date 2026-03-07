"""Tests for prompt template integrity.

Validates that:
1. All registered prompt files exist on disk.
2. All prompts have valid placeholder syntax (no broken braces).
3. Placeholder names in prompts match the canonical registry.
4. No prompt file on disk is missing from the registry.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from workflow_lib.prompt_registry import PROMPT_PLACEHOLDERS, validate_all_prompts_exist

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")


class TestPromptFilesExist:
    """Every prompt in the registry must exist on disk."""

    def test_all_registered_prompts_exist(self):
        missing = validate_all_prompts_exist(PROMPTS_DIR)
        assert missing == [], f"Missing prompt files: {missing}"

    def test_no_unregistered_prompts(self):
        """Every .md file in prompts/ should be in the registry."""
        # Files that are used directly (not as registered templates).
        NON_TEMPLATE_PROMPTS = {"resume.md"}
        on_disk = {
            f for f in os.listdir(PROMPTS_DIR)
            if f.endswith(".md") and os.path.isfile(os.path.join(PROMPTS_DIR, f))
        }
        registered = set(PROMPT_PLACEHOLDERS.keys())
        unregistered = on_disk - registered - NON_TEMPLATE_PROMPTS
        assert unregistered == set(), (
            f"Prompt files on disk but not in registry: {sorted(unregistered)}. "
            "Add them to PROMPT_PLACEHOLDERS in prompt_registry.py"
        )


class TestPromptPlaceholders:
    """Validate placeholder syntax and registry consistency."""

    @pytest.fixture(params=sorted(PROMPT_PLACEHOLDERS.keys()))
    def prompt_info(self, request):
        filename = request.param
        path = os.path.join(PROMPTS_DIR, filename)
        if not os.path.exists(path):
            pytest.skip(f"{filename} does not exist")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return filename, content

    def test_no_broken_braces(self, prompt_info):
        """Detect unmatched braces that indicate broken placeholders."""
        filename, content = prompt_info
        # Remove code blocks (Mermaid, JSON examples, etc.) before checking
        cleaned = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        # Look for single braces not part of a valid {placeholder} pair
        opens = cleaned.count('{')
        closes = cleaned.count('}')
        # Allow small mismatch for JSON/Mermaid examples in non-code-block prose
        assert abs(opens - closes) <= 2, (
            f"{filename}: Mismatched braces (opens={opens}, closes={closes}). "
            "Check for broken placeholder syntax."
        )

    def test_required_placeholders_present(self, prompt_info):
        """Every required placeholder in the registry should appear in the prompt."""
        filename, content = prompt_info
        required = PROMPT_PLACEHOLDERS[filename]
        missing = []
        for placeholder in required:
            if f"{{{placeholder}}}" not in content:
                missing.append(placeholder)
        assert missing == [], (
            f"{filename}: Required placeholders missing from content: "
            f"{', '.join('{' + p + '}' for p in missing)}. "
            "Update the prompt or the registry."
        )


class TestPerPhaseDagCycleDetection:
    """Verify that verify_requirements detects per-phase cycles."""

    def test_find_cycle_detects_simple_cycle(self):
        from verify_requirements import _find_cycle
        dag = {"a": ["b"], "b": ["a"]}
        cycle = _find_cycle(dag)
        assert cycle is not None, "Should detect a→b→a cycle"

    def test_find_cycle_no_cycle(self):
        from verify_requirements import _find_cycle
        dag = {"a": [], "b": ["a"], "c": ["b"]}
        cycle = _find_cycle(dag)
        assert cycle is None, "Should not detect a cycle in a valid DAG"


class TestDuplicateRequirementDetection:
    """Verify the uniqueness checker works."""

    def test_detects_duplicates(self, tmp_path):
        from verify_requirements import verify_uniqueness
        (tmp_path / "a.md").write_text("[1_PRD-REQ-001] First\n")
        (tmp_path / "b.md").write_text("[1_PRD-REQ-001] Duplicate\n")
        assert verify_uniqueness(str(tmp_path)) == 1

    def test_passes_unique(self, tmp_path):
        from verify_requirements import verify_uniqueness
        (tmp_path / "a.md").write_text("[1_PRD-REQ-001] First\n")
        (tmp_path / "b.md").write_text("[2_TAS-REQ-001] Second\n")
        assert verify_uniqueness(str(tmp_path)) == 0
