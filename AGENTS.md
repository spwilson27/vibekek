# AGENTS.md - MCP Tools Codebase Guide

## Project Overview

**MCP Tools** is a Model Context Protocol (MCP) server providing AI assistants with code search capabilities:

- **RAG Search** - Natural language semantic search using sentence-transformers embeddings and ChromaDB
- **Semantic Search** - Find code definitions/references using tree-sitter AST parsing and SQLite

The server runs as a background MCP tool that automatically indexes git repositories and exposes search tools to AI agents.

## Architecture & Structure

```
rag/
├── config.json                 # JSONC config (comments allowed)
├── pyproject.toml              # Dependencies & package config
├── pytest.ini                  # Test configuration
├── rag_mcp/                    # Source code
│   ├── config.py               # Configuration loading (JSONC support)
│   ├── server.py               # Unified MCP server entry point
│   ├── tools/                  # Tool implementations
│   │   ├── base.py             # BaseTool abstract interface
│   │   ├── rag/                # RAG natural language search
│   │   │   ├── tool.py         # RAGTool implementation
│   │   │   ├── indexer.py      # Background indexing with limits
│   │   │   └── store.py        # ChromaDB vector store
│   │   └── semantic/           # Semantic code search
│   │       ├── tool.py         # SemanticTool implementation
│   │       ├── indexer.py      # Tree-sitter parser
│   │       └── store.py        # SQLite symbol database
│   └── utils/                  # Shared utilities
│       ├── scanner.py          # Git-aware file scanning
│       └── embeddings.py       # Code chunking & embeddings
└── tests/                      # Test suite
    ├── test_e2e.py             # 44 E2E tests (85% coverage)
    └── test_coverage_validation.py  # Validates 80% min coverage
```

### Key Components

| Component | Purpose |
|-----------|---------|
| `config.py` | Loads JSONC config with `LimitConfig` and `PriorityConfig` |
| `server.py` | Creates MCP server, loads enabled tools from config |
| `tools/base.py` | Abstract `BaseTool` interface all tools implement |
| `utils/scanner.py` | Git-aware file scanning with exclusions |

## Development Workflow

### Setup

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e .
```

### Running Tests

```bash
# Run all E2E tests (takes ~2 minutes)
pytest tests/test_e2e.py -v

# Run with coverage (requires 80% minimum)
pytest tests/test_e2e.py --cov=rag_mcp --cov-report=term-missing

# Run coverage validation test
pytest tests/test_coverage_validation.py::TestCoverageValidation::test_e2e_coverage_meets_threshold

# Run specific test class
pytest tests/test_e2e.py::TestRAGSearch -v
```

### Testing Guidelines

- **All tests use temp directories** - Never write to project folder
- **E2E tests must maintain 80% coverage** - Validated by `test_coverage_validation.py`
- **Use fixtures** - `temp_dir`, `test_repo`, `sample_code_files`, `config_file`

### Common Tasks

```bash
# Check code formatting (project uses standard Python style)
python -m py_compile rag_mcp/**/*.py

# Test server startup
python -c "from rag_mcp.server import create_server; create_server('config.json')"

# Run server manually (for debugging)
.venv/bin/rag-mcp config.json
```

## Coding Conventions

### Naming Conventions

- **Classes**: PascalCase (`RAGTool`, `SymbolDatabase`, `VectorStore`)
- **Functions/Methods**: snake_case (`scan_git_repo`, `add_chunks`, `find_definitions`)
- **Config classes**: `*Config` suffix (`ToolConfig`, `LimitConfig`, `PriorityConfig`)
- **Test classes**: `Test*` prefix with descriptive name (`TestRAGSearch`, `TestConfigLimits`)

### Code Style

- **Type hints**: Use modern Python 3.10+ syntax (`str | None`, `list[str]`, `dict[str, Any]`)
- **Dataclasses**: Use `@dataclass` for config and data containers
- **Docstrings**: Google-style for public APIs
- **Imports**: Standard library → third-party → local (with blank lines between)

### Patterns

**Tool Implementation Pattern:**
```python
from ..base import BaseTool
from ...config import ToolConfig

class MyTool(BaseTool):
    name = "my_tool"
    
    def __init__(self, repo_path: str, index_dir: str, tool_config: ToolConfig):
        self.repo_path = repo_path
        self.tool_config = tool_config
        # ... initialize components
    
    def get_tool_definition(self) -> MCPTool:
        """Return MCP tool schema."""
        return MCPTool(...)
    
    async def execute(self, arguments: dict[str, Any]) -> str:
        """Execute tool and return formatted result."""
        ...
    
    def start(self) -> None:
        """Start background indexing thread."""
        ...
    
    def get_status(self) -> dict[str, Any]:
        """Return indexing status."""
        ...
```

**Config Pattern:**
```python
@dataclass
class LimitConfig:
    max_files: int = 1000
    max_chunks: int = 20000
    
    @classmethod
    def from_dict(cls, data: dict) -> "LimitConfig":
        return cls(
            max_files=data.get("max_files", 1000),
            max_chunks=data.get("max_chunks", 20000),
        )
```

**Background Indexing Pattern:**
```python
def _index_loop(self):
    """Main indexing loop."""
    self.store.is_indexing = True
    
    try:
        limits = self.tool_config.limits
        files = list(scan_git_repo(..., max_file_size_kb=limits.max_file_size_kb))
        
        # Apply limits
        if limits.max_files > 0:
            files = files[:limits.max_files]
        
        for file_path, content_hash, priority in files:
            # Check chunk limit
            if limits.max_chunks > 0:
                if self.store.get_chunk_count() >= limits.max_chunks:
                    break
            
            # Process file
            ...
    finally:
        self.store.is_indexing = False
```

## Important Dependencies

| Dependency | Purpose |
|------------|---------|
| `mcp` | MCP SDK for server protocol |
| `sentence-transformers` | Local embeddings (all-MiniLM-L6-v2) |
| `chromadb` | Vector database for RAG search |
| `gitpython` | Git repository operations |
| `tree-sitter-*` | Code parsing for semantic analysis |
| `jsoncomment` | JSONC config file support |
| `pytest`, `pytest-cov` | Testing and coverage |

## Gotchas & Common Pitfalls

### ⚠️ Tree-sitter API

The tree-sitter Python API requires wrapping the language in a `Language` object:

```python
# WRONG - will fail with TypeError
from tree_sitter import Parser
import tree_sitter_python
parser = Parser(tree_sitter_python.language())  # ❌

# CORRECT
from tree_sitter import Parser, Language
import tree_sitter_python
lang = Language(tree_sitter_python.language())  # ✅
parser = Parser(lang)
```

### ⚠️ SQLite Reserved Words

`references` is a reserved word in SQLite. Use `symbol_references` instead:

```python
# WRONG
cursor.execute("CREATE TABLE references (...)")  # ❌ Syntax error

# CORRECT
cursor.execute("CREATE TABLE symbol_references (...)")  # ✅
```

### ⚠️ ChromaDB API

The `get()` method doesn't support `include=["ids"]`. Use `metadatas` instead:

```python
# WRONG
results = collection.get(where={"file_path": path}, include=["ids"])  # ❌

# CORRECT
results = collection.get(where={"file_path": path}, include=["metadatas"])  # ✅
ids = results["ids"]
```

### ⚠️ Background Thread Exceptions

Indexing runs in daemon threads. Exceptions in threads won't fail tests but will show as warnings. Always wrap thread targets in try/finally:

```python
def _index_loop(self):
    self.store.is_indexing = True
    try:
        # ... indexing logic
    finally:
        self.store.is_indexing = False  # Always reset state
```

### ⚠️ Config Loading

Use `jsoncomment.JsonComment` for JSONC support, not standard `json`:

```python
from jsoncomment import JsonComment

parser = JsonComment()
with open(config_path) as f:
    data = parser.load(f)  # Supports comments and trailing commas
```

### ⚠️ Test Isolation

Tests create temp directories that are cleaned up automatically. Don't:
- Use hardcoded paths like `/tmp/test`
- Assume files persist between tests
- Share state between test methods

## Examples

### Creating a New Tool

1. Create tool directory: `rag_mcp/tools/mytool/`
2. Implement `BaseTool` interface
3. Register in `server.py`
4. Add config options to `config.py`
5. Write E2E tests in `tests/test_e2e.py`

### Adding Config Options

```python
# In config.py
@dataclass
class ToolConfig:
    enabled: bool = True
    my_option: str = "default"
    
    @classmethod
    def from_dict(cls, data: dict) -> "ToolConfig":
        return cls(
            enabled=data.get("enabled", True),
            my_option=data.get("my_option", "default"),
        )
```

### Writing E2E Tests

```python
class TestMyFeature:
    """Test my new feature."""
    
    def test_feature_works(self, temp_dir, test_repo):
        """Test that the feature works correctly."""
        from rag_mcp.config import load_config
        from rag_mcp.server import create_server
        
        # Setup config
        config = {
            "repo_path": str(test_repo),
            "tools": {"rag": {"enabled": True}}
        }
        config_path = temp_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump(config, f)
        
        # Create server
        server = create_server(str(config_path))
        time.sleep(5)  # Wait for indexing
        
        # Assert behavior
        assert server is not None
```

## Quick Reference

| Task | Command |
|------|---------|
| Install | `pip install -e .` |
| Run tests | `pytest tests/` |
| Run with coverage | `pytest tests/test_e2e.py --cov=rag_mcp` |
| Start server | `.venv/bin/rag-mcp config.json` |
| Check coverage threshold | `pytest tests/test_coverage_validation.py` |
