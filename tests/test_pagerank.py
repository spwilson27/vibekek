"""Tests for PageRank functionality in semantic search."""

import json
import time
from pathlib import Path

import pytest

from rag_mcp.config import load_config, PageRankConfig, LimitConfig
from rag_mcp.tools.semantic.pagerank import PageRankCalculator, PageRankResult
from rag_mcp.tools.semantic.store import SymbolDatabase, Symbol
from rag_mcp.tools.semantic.tool import SemanticTool


class TestPageRankCalculator:
    """Test PageRank calculation algorithm."""

    def test_pagerank_initialization(self):
        """Test PageRank calculator initializes correctly."""
        calculator = PageRankCalculator(damping_factor=0.85, iterations=20)
        
        assert calculator.damping_factor == 0.85
        assert calculator.iterations == 20
        assert len(calculator._scores) == 0

    def test_pagerank_add_symbol(self):
        """Test adding symbols to PageRank calculator."""
        calculator = PageRankCalculator()
        
        calculator.add_symbol("function_a", "/path/to/file.py")
        calculator.add_symbol("function_b", "/path/to/file.py")
        
        assert len(calculator._scores) == 2
        assert "/path/to/file.py:function_a" in calculator._scores
        assert "/path/to/file.py:function_b" in calculator._scores

    def test_pagerank_add_call(self):
        """Test adding call relationships."""
        calculator = PageRankCalculator()
        
        # Add symbols
        calculator.add_symbol("caller", "/path/to/file.py")
        calculator.add_symbol("callee", "/path/to/file.py")
        
        # Add call relationship
        calculator.add_call("caller", "/path/to/file.py", "callee")
        
        # Verify call graph
        caller_key = "/path/to/file.py:caller"
        callee_key = "/path/to/file.py:callee"
        
        assert callee_key in calculator._call_graph[caller_key]
        assert caller_key in calculator._reverse_graph[callee_key]

    def test_pagerank_compute_simple(self):
        """Test PageRank computation with simple graph."""
        calculator = PageRankCalculator(damping_factor=0.85, iterations=20)
        
        # Create a simple call graph: A -> B -> C
        calculator.add_symbol("A", "/file.py")
        calculator.add_symbol("B", "/file.py")
        calculator.add_symbol("C", "/file.py")
        
        calculator.add_call("A", "/file.py", "B")
        calculator.add_call("B", "/file.py", "C")
        
        scores = calculator.compute()
        
        # All symbols should have scores
        assert len(scores) == 3
        assert all(score > 0 for score in scores.values())
        
        # C should have highest score (most called)
        c_score = scores["/file.py:C"]
        a_score = scores["/file.py:A"]
        assert c_score > a_score

    def test_pagerank_compute_hub_authority(self):
        """Test PageRank identifies hubs and authorities."""
        calculator = PageRankCalculator(damping_factor=0.85, iterations=20)
        
        # Create a hub that calls many functions
        calculator.add_symbol("hub", "/file.py")
        calculator.add_symbol("func1", "/file.py")
        calculator.add_symbol("func2", "/file.py")
        calculator.add_symbol("func3", "/file.py")
        
        calculator.add_call("hub", "/file.py", "func1")
        calculator.add_call("hub", "/file.py", "func2")
        calculator.add_call("hub", "/file.py", "func3")
        
        scores = calculator.compute()
        
        # Functions called by hub should have higher scores than hub
        func1_score = scores["/file.py:func1"]
        func2_score = scores["/file.py:func2"]
        func3_score = scores["/file.py:func3"]
        hub_score = scores["/file.py:hub"]
        
        assert func1_score > hub_score
        assert func2_score > hub_score
        assert func3_score > hub_score

    def test_pagerank_reset(self):
        """Test PageRank calculator reset."""
        calculator = PageRankCalculator()
        
        calculator.add_symbol("func", "/file.py")
        calculator.compute()
        
        calculator.reset()
        
        assert len(calculator._scores) == 0
        assert len(calculator._call_graph) == 0
        assert len(calculator._reverse_graph) == 0

    def test_pagerank_get_ranked_symbols(self):
        """Test getting ranked symbols."""
        calculator = PageRankCalculator()
        
        # Create call graph
        calculator.add_symbol("main", "/file.py")
        calculator.add_symbol("helper1", "/file.py")
        calculator.add_symbol("helper2", "/file.py")
        
        # main calls both helpers
        calculator.add_call("main", "/file.py", "helper1")
        calculator.add_call("main", "/file.py", "helper2")
        
        calculator.compute()
        
        ranked = calculator.get_ranked_symbols(limit=10)
        
        assert len(ranked) == 3
        assert all(isinstance(r, PageRankResult) for r in ranked)
        assert ranked[0].rank == 1
        assert ranked[1].rank == 2

    def test_pagerank_empty_graph(self):
        """Test PageRank with no symbols."""
        calculator = PageRankCalculator()
        
        scores = calculator.compute()
        
        assert scores == {}
        
        ranked = calculator.get_ranked_symbols()
        assert ranked == []

    def test_pagerank_dangling_nodes(self):
        """Test PageRank with nodes that have no outgoing links."""
        calculator = PageRankCalculator()
        
        calculator.add_symbol("isolated", "/file.py")
        calculator.add_symbol("called", "/file.py")
        
        # isolated doesn't call anything (dangling node)
        # called is never called
        
        scores = calculator.compute()
        
        # Both should still have scores due to teleportation
        assert len(scores) == 2
        assert all(score > 0 for score in scores.values())


class TestSymbolDatabaseWithPageRank:
    """Test SymbolDatabase integration with PageRank."""

    def test_symbol_database_pagerank_initialization(self, temp_dir):
        """Test SymbolDatabase initializes with PageRank config."""
        from rag_mcp.config import PageRankConfig
        
        pagerank_config = PageRankConfig(enabled=True, damping_factor=0.9, iterations=30)
        db = SymbolDatabase(temp_dir / "test_index", pagerank_config)
        
        assert db.is_pagerank_enabled()
        assert db.get_pagerank_config().damping_factor == 0.9
        assert not db.is_pagerank_computed()

    def test_symbol_database_register_symbol(self, temp_dir):
        """Test registering symbols for PageRank."""
        db = SymbolDatabase(temp_dir / "test_index", PageRankConfig(enabled=True))
        
        symbol = Symbol(
            name="test_func",
            symbol_type="function",
            file_path="/test/file.py",
            start_line=1,
            end_line=10,
            content="def test_func():\n    pass",
            language="python"
        )
        
        db.register_symbol_for_pagerank(symbol)
        
        # PageRank should have the symbol registered
        assert not db.is_pagerank_computed()

    def test_symbol_database_register_call(self, temp_dir):
        """Test registering calls for PageRank."""
        db = SymbolDatabase(temp_dir / "test_index", PageRankConfig(enabled=True))
        
        caller = Symbol(
            name="caller",
            symbol_type="function",
            file_path="/test/file.py",
            start_line=1,
            end_line=10,
            content="def caller():\n    callee()",
            language="python"
        )
        
        db.register_symbol_for_pagerank(caller)
        db.register_call_for_pagerank(caller, "callee")

    def test_symbol_database_compute_pagerank(self, temp_dir):
        """Test computing PageRank in SymbolDatabase."""
        db = SymbolDatabase(temp_dir / "test_index", PageRankConfig(enabled=True))
        
        # Add symbols and calls
        caller = Symbol(
            name="caller",
            symbol_type="function",
            file_path="/test/file.py",
            start_line=1,
            end_line=10,
            content="def caller():\n    callee()",
            language="python"
        )
        
        callee = Symbol(
            name="callee",
            symbol_type="function",
            file_path="/test/file.py",
            start_line=12,
            end_line=20,
            content="def callee():\n    pass",
            language="python"
        )
        
        db.register_symbol_for_pagerank(caller)
        db.register_symbol_for_pagerank(callee)
        db.register_call_for_pagerank(caller, "callee")
        
        db.compute_pagerank()
        
        assert db.is_pagerank_computed()
        
        # Get scores
        caller_score = db.get_pagerank_scores("caller", "/test/file.py")
        callee_score = db.get_pagerank_scores("callee", "/test/file.py")
        
        # Callee should have higher score (being called)
        assert callee_score > caller_score

    def test_symbol_database_pagerank_disabled(self, temp_dir):
        """Test SymbolDatabase with PageRank disabled."""
        db = SymbolDatabase(temp_dir / "test_index", PageRankConfig(enabled=False))
        
        symbol = Symbol(
            name="test",
            symbol_type="function",
            file_path="/test/file.py",
            start_line=1,
            end_line=10,
            content="def test(): pass",
            language="python"
        )
        
        db.register_symbol_for_pagerank(symbol)
        db.compute_pagerank()
        
        score = db.get_pagerank_scores("test", "/test/file.py")
        assert score == 0.0


class TestPageRankConfig:
    """Test PageRank configuration."""

    def test_pagerank_config_defaults(self):
        """Test PageRankConfig default values."""
        config = PageRankConfig()
        
        assert config.enabled is True
        assert config.damping_factor == 0.85
        assert config.iterations == 20
        assert config.weight == 0.5

    def test_pagerank_config_from_dict(self):
        """Test PageRankConfig from dictionary."""
        data = {
            "enabled": False,
            "damping_factor": 0.9,
            "iterations": 30,
            "weight": 0.7
        }
        
        config = PageRankConfig.from_dict(data)
        
        assert config.enabled is False
        assert config.damping_factor == 0.9
        assert config.iterations == 30
        assert config.weight == 0.7

    def test_pagerank_config_from_dict_partial(self):
        """Test PageRankConfig from partial dictionary."""
        data = {
            "damping_factor": 0.9,
        }
        
        config = PageRankConfig.from_dict(data)
        
        assert config.enabled is True  # default
        assert config.damping_factor == 0.9
        assert config.iterations == 20  # default
        assert config.weight == 0.5  # default

    def test_pagerank_config_in_tool_config(self):
        """Test PageRankConfig in ToolConfig."""
        from rag_mcp.config import ToolConfig
        
        data = {
            "enabled": True,
            "pagerank": {
                "enabled": True,
                "damping_factor": 0.75
            }
        }
        
        tool_config = ToolConfig.from_dict(data)
        
        assert tool_config.pagerank.enabled is True
        assert tool_config.pagerank.damping_factor == 0.75


class TestLineLimitConfig:
    """Test line limit configuration."""

    def test_limit_config_line_limit_default(self):
        """Test LimitConfig default line_limit."""
        config = LimitConfig()
        
        assert config.line_limit == 25

    def test_limit_config_line_limit_from_dict(self):
        """Test LimitConfig line_limit from dictionary."""
        data = {
            "line_limit": 50
        }
        
        config = LimitConfig.from_dict(data)
        
        assert config.line_limit == 50

    def test_limit_config_line_limit_zero(self):
        """Test LimitConfig line_limit=0 means no limit."""
        data = {
            "line_limit": 0
        }
        
        config = LimitConfig.from_dict(data)
        
        assert config.line_limit == 0


class TestSemanticToolWithPageRank:
    """Test SemanticTool integration with PageRank."""

    @pytest.fixture
    def test_code_repo(self, temp_dir):
        """Create a test repository with code that has call relationships."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()
        
        # Create a Python file with functions that call each other
        code = '''
def utility_function():
    """A utility function called by many."""
    return "utility"

def helper_function():
    """Helper that calls utility."""
    return utility_function()

def main_function():
    """Main function that calls helpers."""
    result1 = helper_function()
    result2 = utility_function()
    return result1 + result2

def another_function():
    """Another function calling utility."""
    return utility_function()

class MyClass:
    """A test class."""
    
    def __init__(self):
        self.value = utility_function()
    
    def method(self):
        """Method that calls utility."""
        return helper_function()
'''
        
        (repo_path / "test.py").write_text(code)
        
        # Initialize git repo
        import subprocess
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], 
                      cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], 
                      cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], 
                      cwd=repo_path, check=True, capture_output=True)
        
        return repo_path

    def test_semantic_tool_pagerank_integration(self, temp_dir, test_code_repo):
        """Test SemanticTool integrates PageRank correctly."""
        from rag_mcp.config import ToolConfig, PageRankConfig, LimitConfig
        
        # Create config with PageRank enabled
        tool_config = ToolConfig(
            enabled=True,
            index_dir=str(temp_dir / "semantic_index"),
            limits=LimitConfig(line_limit=25),
            pagerank=PageRankConfig(enabled=True, iterations=20)
        )
        
        # Create semantic tool
        tool = SemanticTool(
            repo_path=str(test_code_repo),
            index_dir=str(temp_dir / "semantic_index"),
            tool_config=tool_config
        )
        
        # Wait for indexing
        time.sleep(3)
        
        # Check PageRank was computed
        assert tool.db.is_pagerank_computed()

    def test_semantic_tool_search_with_pagerank(self, temp_dir, test_code_repo):
        """Test semantic search returns results with PageRank scores."""
        from rag_mcp.config import ToolConfig, PageRankConfig, LimitConfig
        import asyncio
        
        tool_config = ToolConfig(
            enabled=True,
            index_dir=str(temp_dir / "semantic_index"),
            limits=LimitConfig(line_limit=25),
            pagerank=PageRankConfig(enabled=True, iterations=20)
        )
        
        tool = SemanticTool(
            repo_path=str(test_code_repo),
            index_dir=str(temp_dir / "semantic_index"),
            tool_config=tool_config
        )
        
        # Wait for indexing
        time.sleep(3)
        
        # Search for utility_function (should be highly ranked)
        async def search():
            result = await tool.execute({"query": "utility_function", "search_type": "definition"})
            return result
        
        result = asyncio.run(search())
        
        # Result should contain PageRank score
        assert "utility_function" in result
        assert "PageRank Score:" in result


class TestPageRankScenarios:
    """Test various PageRank scenarios."""

    def test_pagerank_popular_function(self):
        """Test that frequently-called functions rank higher."""
        calculator = PageRankCalculator()
        
        # Create a popular function that many call
        calculator.add_symbol("popular", "/file.py")
        for i in range(10):
            calculator.add_symbol(f"caller_{i}", "/file.py")
            calculator.add_call(f"caller_{i}", "/file.py", "popular")
        
        scores = calculator.compute()
        
        # Popular function should have highest score
        popular_score = scores["/file.py:popular"]
        caller_scores = [scores[f"/file.py:caller_{i}"] for i in range(10)]
        
        assert popular_score > max(caller_scores)

    def test_pagerank_recursive_calls(self):
        """Test PageRank with recursive call patterns."""
        calculator = PageRankCalculator()
        
        calculator.add_symbol("recursive", "/file.py")
        calculator.add_call("recursive", "/file.py", "recursive")
        
        scores = calculator.compute()
        
        # Should not crash and should have a score
        assert len(scores) == 1
        assert scores["/file.py:recursive"] > 0

    def test_pagerank_chain_of_calls(self):
        """Test PageRank with a chain of function calls."""
        calculator = PageRankCalculator()
        
        # Create a chain: A -> B -> C -> D
        prev = "A"
        calculator.add_symbol(prev, "/file.py")
        for name in ["B", "C", "D"]:
            calculator.add_symbol(name, "/file.py")
            calculator.add_call(prev, "/file.py", name)
            prev = name
        
        scores = calculator.compute()
        
        # D (at end of chain, called but doesn't call) should have highest score
        d_score = scores["/file.py:D"]
        a_score = scores["/file.py:A"]
        assert d_score > a_score
