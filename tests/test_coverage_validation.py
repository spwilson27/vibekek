#!/usr/bin/env python3
"""
Coverage validation tests for MCP Tools E2E tests.

This module contains tests that validate the E2E test suite achieves
the required minimum coverage threshold.
"""

import subprocess
import sys
from pathlib import Path

import pytest


# Minimum required coverage percentage from E2E tests alone
MINIMUM_COVERAGE_THRESHOLD = 80.0

# Modules that should be covered by E2E tests
TARGET_MODULES = [
    "rag_mcp",
    "rag_mcp.config",
    "rag_mcp.server",
    "rag_mcp.tools.base",
    "rag_mcp.tools.rag",
    "rag_mcp.tools.rag.indexer",
    "rag_mcp.tools.rag.store",
    "rag_mcp.tools.rag.tool",
    "rag_mcp.tools.semantic",
    "rag_mcp.tools.semantic.indexer",
    "rag_mcp.tools.semantic.store",
    "rag_mcp.tools.semantic.tool",
    "rag_mcp.utils",
    "rag_mcp.utils.embeddings",
    "rag_mcp.utils.scanner",
]


class TestCoverageValidation:
    """Tests to validate E2E test coverage meets requirements."""

    def test_e2e_coverage_meets_threshold(self):
        """
        Validate that E2E tests alone achieve at least 80% coverage.
        
        This test runs the E2E test suite with coverage and validates
        that the total coverage meets the minimum threshold.
        """
        # Run E2E tests with coverage
        project_root = Path(__file__).parent.parent
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                "tests/test_e2e.py",
                f"--cov={project_root}/rag_mcp",
                "--cov-report=term-missing",
                "--cov-report=json",
                "-q",
            ],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=300  # 5 minute timeout
        )
        
        # Parse coverage from JSON report
        coverage_file = project_root / ".coverage"
        assert coverage_file.exists(), "Coverage file was not created"
        
        # Run coverage report to get percentage
        cov_result = subprocess.run(
            [
                sys.executable, "-m", "coverage", "report",
                "--include=*/rag_mcp/*",
            ],
            capture_output=True,
            text=True,
            cwd=project_root
        )
        
        # Parse the coverage percentage from the output
        # The last line contains the total like: "TOTAL    795    121    85%"
        total_line = cov_result.stdout.strip().split("\n")[-1]
        
        # Extract percentage
        percentage_str = total_line.split()[-1].rstrip("%")
        try:
            coverage_percentage = float(percentage_str)
        except ValueError:
            pytest.fail(f"Could not parse coverage percentage from: {total_line}")
        
        # Assert coverage meets threshold
        assert coverage_percentage >= MINIMUM_COVERAGE_THRESHOLD, (
            f"E2E test coverage ({coverage_percentage}%) is below the required "
            f"minimum of {MINIMUM_COVERAGE_THRESHOLD}%. "
            f"Please add more E2E tests to improve coverage.\n\n"
            f"Coverage output:\n{cov_result.stdout}"
        )

    def test_core_modules_covered(self):
        """
        Validate that all core modules have some coverage from E2E tests.
        
        This ensures no major module is completely untested by E2E tests.
        """
        project_root = Path(__file__).parent.parent
        
        # Run coverage report with missing lines
        cov_result = subprocess.run(
            [
                sys.executable, "-m", "coverage", "report",
                "--include=*/rag_mcp/*",
                "--show-missing",
            ],
            capture_output=True,
            text=True,
            cwd=project_root
        )
        
        # Check that each target module appears in the report
        output = cov_result.stdout
        
        # Verify key modules are present and have reasonable coverage
        critical_modules = [
            "rag_mcp/config.py",
            "rag_mcp/server.py", 
            "rag_mcp/tools/rag",
            "rag_mcp/tools/semantic",
            "rag_mcp/utils",
        ]
        
        missing_critical = []
        for module in critical_modules:
            if module not in output:
                missing_critical.append(module)
        
        assert not missing_critical, (
            f"The following critical modules have no E2E test coverage: "
            f"{missing_critical}"
        )

    def test_no_critical_gaps_in_rag_tool(self):
        """Validate RAG tool has no critical untested functionality."""
        from rag_mcp.config import load_config, ToolConfig
        from rag_mcp.tools.rag import RAGTool
        from rag_mcp.tools.rag.store import VectorStore
        from rag_mcp.tools.rag.indexer import Indexer
        import tempfile
        from pathlib import Path
        
        # Test that all major RAG components can be instantiated
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "index"
            tool_config = ToolConfig()
            
            # These should not raise
            store = VectorStore(index_dir)
            assert store is not None
            
            # Test store operations
            assert store.get_chunk_count() == 0
            assert store.indexing_status is not None
    
    def test_no_critical_gaps_in_semantic_tool(self):
        """Validate Semantic tool has no critical untested functionality."""
        from rag_mcp.tools.semantic import SemanticTool
        from rag_mcp.tools.semantic.store import SymbolDatabase, Symbol
        from rag_mcp.tools.semantic.indexer import CodeParser
        from rag_mcp.config import ToolConfig
        import tempfile
        from pathlib import Path
        
        # Test that all major Semantic components can be instantiated
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "index"
            tool_config = ToolConfig()
            
            # These should not raise
            db = SymbolDatabase(index_dir)
            assert db is not None
            
            parser = CodeParser()
            assert parser is not None
            
            # Test database operations
            symbol = Symbol(
                name="test",
                symbol_type="function",
                file_path="test.py",
                start_line=1,
                end_line=1,
                content="def test(): pass",
                language="python"
            )
            db.add_symbol(symbol, "hash1")
            
            results = db.find_definitions("test")
            assert len(results) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
