"""Tests for the per-file agent memory system (save_agent_memory / get_memory_context)."""

import os
import time

import pytest

# Allow imports from the workflow_lib package.
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workflow_lib.executor import save_agent_memory, get_memory_context


@pytest.fixture()
def fake_root(tmp_path):
    """Create a minimal project root with .agent/ structure."""
    (tmp_path / ".agent" / "memories").mkdir(parents=True)
    (tmp_path / ".agent" / "decisions").mkdir(parents=True)
    return tmp_path


class TestSaveAgentMemory:
    def test_creates_file_in_memories_dir(self, fake_root):
        mem_dir = str(fake_root / ".agent" / "memories")
        path = save_agent_memory("agent1", "p1_t1", "changelog", "did stuff", memories_dir=mem_dir)
        assert os.path.isfile(path)
        assert path.startswith(mem_dir)

    def test_file_contains_frontmatter(self, fake_root):
        mem_dir = str(fake_root / ".agent" / "memories")
        path = save_agent_memory("agent1", "p1_t1", "brittle_area", "watch out", memories_dir=mem_dir)
        content = open(path).read()
        assert "agent: agent1" in content
        assert "task: p1_t1" in content
        assert "category: brittle_area" in content
        assert "watch out" in content

    def test_decision_routes_to_decisions_dir(self, fake_root):
        """When category is 'decision' and no override, it should use DECISIONS_DIR."""
        dec_dir = str(fake_root / ".agent" / "decisions")
        path = save_agent_memory("agent1", "p1_t1", "decision", "use JSON", memories_dir=dec_dir)
        assert "/decisions/" in path

    def test_sanitizes_slashes_and_spaces(self, fake_root):
        mem_dir = str(fake_root / ".agent" / "memories")
        path = save_agent_memory("my agent/v2", "phase 1/task 3", "observation", "ok", memories_dir=mem_dir)
        filename = os.path.basename(path)
        assert "/" not in filename.replace(mem_dir, "")
        assert " " not in filename

    def test_parallel_writes_no_conflict(self, fake_root):
        """Multiple rapid writes produce distinct files."""
        mem_dir = str(fake_root / ".agent" / "memories")
        paths = set()
        for i in range(5):
            p = save_agent_memory(f"agent{i}", "task1", "changelog", f"entry {i}", memories_dir=mem_dir)
            paths.add(p)
        assert len(paths) == 5

    def test_creates_dir_if_missing(self, tmp_path):
        mem_dir = str(tmp_path / "nonexistent" / "memories")
        path = save_agent_memory("a", "t", "observation", "hi", memories_dir=mem_dir)
        assert os.path.isfile(path)


class TestGetMemoryContext:
    def test_empty_dirs_returns_empty(self, fake_root):
        result = get_memory_context(str(fake_root))
        assert result == ""

    def test_reads_memory_files(self, fake_root):
        mem_dir = str(fake_root / ".agent" / "memories")
        save_agent_memory("a1", "t1", "changelog", "first entry", memories_dir=mem_dir)
        result = get_memory_context(str(fake_root))
        assert "first entry" in result

    def test_reads_decision_files(self, fake_root):
        dec_dir = str(fake_root / ".agent" / "decisions")
        save_agent_memory("a1", "t1", "decision", "use capnp for IPC", memories_dir=dec_dir)
        result = get_memory_context(str(fake_root))
        assert "use capnp for IPC" in result
        assert "Architectural Decisions" in result

    def test_limit_caps_memories(self, fake_root):
        mem_dir = str(fake_root / ".agent" / "memories")
        for i in range(10):
            save_agent_memory("a", f"t{i:02d}", "changelog", f"entry-{i:02d}", memories_dir=mem_dir)
            time.sleep(0.01)  # ensure distinct timestamps
        result = get_memory_context(str(fake_root), limit=3)
        # Should contain the last 3 entries
        assert "entry-09" in result
        assert "entry-08" in result
        assert "entry-07" in result
        # Should NOT contain early entries
        assert "entry-00" not in result

    def test_decisions_not_capped_by_limit(self, fake_root):
        """Decisions are always included regardless of limit."""
        dec_dir = str(fake_root / ".agent" / "decisions")
        mem_dir = str(fake_root / ".agent" / "memories")
        for i in range(5):
            save_agent_memory("a", f"t{i}", "decision", f"decision-{i}", memories_dir=dec_dir)
        save_agent_memory("a", "t99", "changelog", "mem", memories_dir=mem_dir)
        result = get_memory_context(str(fake_root), limit=1)
        # All 5 decisions should be present
        for i in range(5):
            assert f"decision-{i}" in result
        # The 1 memory should also be present
        assert "mem" in result

    def test_reads_legacy_files(self, fake_root):
        """Legacy MEMORY.md and DECISIONS.md are still read."""
        agent_dir = fake_root / ".agent"
        (agent_dir / "MEMORY.md").write_text("legacy memory")
        (agent_dir / "DECISIONS.md").write_text("legacy decision")
        result = get_memory_context(str(fake_root))
        assert "legacy memory" in result
        assert "legacy decision" in result

    def test_combined_output_has_separators(self, fake_root):
        dec_dir = str(fake_root / ".agent" / "decisions")
        mem_dir = str(fake_root / ".agent" / "memories")
        save_agent_memory("a", "t1", "decision", "dec1", memories_dir=dec_dir)
        save_agent_memory("a", "t2", "changelog", "mem1", memories_dir=mem_dir)
        result = get_memory_context(str(fake_root))
        assert "---" in result
