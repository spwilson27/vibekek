"""Test that .agent/MEMORY.md and .agent/DECISIONS.md stay within size limits.

Both files are injected into every implementation and review agent's context.
If they grow too large they will exceed model context limits and degrade
agent performance. These tests enforce line limits and structural invariants.

Limits:
    MEMORY.md   — 100 lines (ephemeral: changelog + brittle areas; old entries
                  are archived to memory_archive.md)
    DECISIONS.md — 150 lines (durable: tables + invariants; entries are
                   SUPERSEDED rather than deleted, so growth is slower)
"""

import os
import re
import unittest

_AGENT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".agent"
)

MEMORY_FILE = os.path.join(_AGENT_DIR, "MEMORY.md")
DECISIONS_FILE = os.path.join(_AGENT_DIR, "DECISIONS.md")

MEMORY_MAX_LINES = 100
DECISIONS_MAX_LINES = 150


class TestMemorySize(unittest.TestCase):
    def test_memory_file_within_line_limit(self):
        """MEMORY.md must be 100 lines or fewer."""
        if not os.path.exists(MEMORY_FILE):
            return
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertLessEqual(
            len(lines),
            MEMORY_MAX_LINES,
            f".agent/MEMORY.md has {len(lines)} lines, exceeding the "
            f"{MEMORY_MAX_LINES}-line limit. Archive old changelog entries to "
            f".agent/memory_archive.md and condense similar entries.",
        )


class TestDecisionsSize(unittest.TestCase):
    def test_decisions_file_within_line_limit(self):
        """DECISIONS.md must be 150 lines or fewer."""
        if not os.path.exists(DECISIONS_FILE):
            return
        with open(DECISIONS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertLessEqual(
            len(lines),
            DECISIONS_MAX_LINES,
            f".agent/DECISIONS.md has {len(lines)} lines, exceeding the "
            f"{DECISIONS_MAX_LINES}-line limit. Mark superseded decisions as "
            f"[SUPERSEDED] and condense related entries into higher-level summaries.",
        )


if __name__ == "__main__":
    unittest.main()
