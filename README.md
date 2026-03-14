# MCP Tools Server

A multi-tool Model Context Protocol (MCP) server providing code search capabilities:

1. **RAG Search** - Natural language semantic search using embeddings
2. **Semantic Search** - Find code definitions and references using tree-sitter

## Installation

```bash
cd /path/to/rag
.venv/bin/pip install -e .
```

## Configuration

The configuration file uses **JSONC** (JSON with Comments), allowing you to add comments and use more permissive JSON syntax.

Create a `config.json` file:

```jsonc
{
  // Path to the git repository to index (default: current directory)
  "repo_path": "/path/to/your/repo",
  
  "tools": {
    "rag": {
      "enabled": true,
      "index_dir": "/tmp/rag-{hash}"
    },
    "semantic": {
      "enabled": false,
      "index_dir": "/tmp/semantic-{hash}"
    }
  }
}
```

### Configuration Options

| Option | Description |
|--------|-------------|
| `repo_path` | Path to git repository to index (default: current directory) |
| `tools.rag.enabled` | Enable/disable RAG natural language search |
| `tools.rag.index_dir` | Custom index directory (use `{hash}` for repo hash) |
| `tools.semantic.enabled` | Enable/disable semantic code search |
| `tools.semantic.index_dir` | Custom index directory for semantic index |

### Size Limits

Prevent indexes from growing too large with these per-tool options:

```jsonc
{
  "tools": {
    "rag": {
      "limits": {
        // Max files to index (0 = no limit)
        "max_files": 1000,
        // Max chunks/symbols (0 = no limit)
        "max_chunks": 20000,
        // Skip files larger than this (KB, 0 = no limit)
        "max_file_size_kb": 1024,
        // Truncate files to this size before indexing (KB, 0 = no truncation)
        "truncate_size_kb": 256
      }
    }
  }
}
```

### Priority & Filtering

Control which files get indexed first and which are excluded:

```jsonc
{
  "tools": {
    "rag": {
      "priority": {
        // Index these directories first (order matters)
        "dirs": ["src/", "lib/", "app/"],
        // Exclude these directories (default: node_modules, vendor, etc.)
        "exclude_dirs": ["tests/", "examples/"],
        // Only index these extensions (empty = all code extensions)
        "extensions": [".py", ".js", ".ts"]
      }
    }
  }
}
```

**Default excluded directories:** `node_modules`, `vendor`, `__pycache__`, `.git`, `.venv`, `venv`, `env`, `.env`, `dist`, `build`, `target`, `third_party`, `extern`

### Example Configurations

**RAG only** (copy `config-rag-only.json`):
```json
{
  "tools": {
    "rag": { "enabled": true },
    "semantic": { "enabled": false }
  }
}
```

**Semantic only** (copy `config-semantic-only.json`):
```json
{
  "tools": {
    "rag": { "enabled": false },
    "semantic": { "enabled": true }
  }
}
```

## Usage

### MCP Configuration

Add to your MCP client config (e.g., Claude Desktop):

```json
{
  "mcpServers": {
    "mcp-tools": {
      "command": "/path/to/rag/.venv/bin/rag-mcp",
      "args": ["/path/to/config.json"]
    }
  }
}
```

### Tools

#### RAG Tools

**`rag_search`** - Search code using natural language

```json
{
  "query": "how is authentication implemented",
  "n_results": 10
}
```

**`rag_status`** - Get RAG indexing status

#### Semantic Tools

**`semantic_search`** - Find code definitions and references

```json
{
  "query": "UserService",
  "search_type": "all",
  "symbol_type": "class"
}
```

Parameters:
- `query` (required): Symbol name to search
- `search_type`: `definition`, `references`, or `all`
- `symbol_type`: Filter by `function`, `class`, `method`, `variable`, `interface`, `type`

**`semantic_status`** - Get semantic indexing status

## Architecture

```
rag_mcp/
├── config.py               # Configuration loading
├── server.py               # Unified MCP server
├── tools/
│   ├── base.py             # Base tool interface
│   ├── rag/
│   │   ├── tool.py         # RAG tool implementation
│   │   ├── indexer.py      # Background indexing
│   │   └── store.py        # ChromaDB vector store
│   └── semantic/
│       ├── tool.py         # Semantic search tool
│       ├── indexer.py      # Tree-sitter parser
│       └── store.py        # SQLite symbol database
└── utils/
    ├── scanner.py          # Git-aware file scanning
    └── embeddings.py       # Code chunking & embeddings
```

## Index Storage

Indexes are stored in `/tmp/<tool>-<md5hash>` where the hash is derived from the repository path:
- RAG index: `/tmp/rag-<hash>`
- Semantic index: `/tmp/semantic-<hash>`

## Features

- **Background Indexing**: Automatically indexes your repository on startup
- **Incremental Updates**: Only re-indexes changed files, cleans up deleted files
- **Partial Search**: Search works even while indexing is in progress (with warning)
- **Git-aware**: Respects `.gitignore` and only indexes code files
- **Configurable**: Enable/disable tools via config file

## Dependencies

- **mcp**: MCP SDK
- **sentence-transformers**: Local embedding model (all-MiniLM-L6-v2)
- **chromadb**: Vector database for RAG
- **gitpython**: Git repository operations
- **tree-sitter**: Code parsing for semantic analysis
- **tree-sitter-python**: Python language support
- **tree-sitter-javascript**: JavaScript language support
- **tree-sitter-typescript**: TypeScript language support
