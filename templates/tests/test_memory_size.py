"""Test that .agent/MEMORY.md stays within size limits.

The MEMORY.md file is used by implementation agents to share architectural
context across tasks. If it grows too large, it will exceed agent context
limits and degrade performance. This test enforces a 100-line maximum.
"""

import os
import unittest

MEMORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".agent", "MEMORY.md")
MAX_LINES = 100


class TestMemorySize(unittest.TestCase):
    def test_memory_file_within_line_limit(self):
        """MEMORY.md must be 100 lines or fewer."""
        if not os.path.exists(MEMORY_FILE):
            return  # No memory file yet — that's fine

        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

        self.assertLessEqual(
            len(lines),
            MAX_LINES,
            f".agent/MEMORY.md has {len(lines)} lines, exceeding the {MAX_LINES}-line limit. "
            f"Archive old entries or compress similar entries to stay within the limit.",
        )


if __name__ == "__main__":
    unittest.main()
