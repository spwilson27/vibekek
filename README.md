# ai-setup — AI Agent Development Template

A template repository for bootstrapping new projects with AI agent tooling: a RAG (Retrieval-Augmented Generation) codebase index exposed as an MCP server that plugs directly into **gemini-cli**, **Claude Desktop**, and **GitHub Copilot CLI**.

> **Zero-config startup** — on first use the server automatically creates a virtualenv, installs dependencies, and builds the index. Every subsequent start skips steps that are already up-to-date.

---

## How it works

```
your-project/            ← source code to index
.rag/
  rag_config.json        ← configure which dirs + file types to index
  mcp_server_wrapper.sh  ← entrypoint: bootstraps venv, then starts server
  mcp_server.py          ← auto-rebuilds index when source files change
  store.py               ← parallelised indexer (FAISS binary store)
  repo_index/            ← generated — gitignored
.gemini/settings.json    ← gemini-cli MCP config (auto-loaded from project root)
.copilot/mcp-config.json ← Copilot CLI config template
.claude/mcp_config.md    ← Claude Desktop setup instructions
```

On every startup the wrapper runs these steps — skipping any that are already done:

```
mcp_server_wrapper.sh
  1. Create .rag/.venv (if missing)
  2. pip install -r requirements.txt (only if requirements.txt changed since last install)
  3. Start mcp_server.py
       └─ Rebuild FAISS index (only if source files changed since last index build)
       └─ Serve MCP tools: query() + retrieve()
```

---

## Setup (2 steps)

### 1. Configure what to index

Edit **`.rag/rag_config.json`** to point at your source directories:

```json
{
  "skip_dirs": [".git", "target", "node_modules", ".venv", "dist"],
  "sources": [
    {
      "dir": "../my-rust-project",
      "extensions": [".rs", ".toml", ".md"],
      "label": "my-rust-project (Rust)"
    },
    {
      "dir": "../my-ts-app",
      "extensions": [".ts", ".tsx", ".json", ".md"],
      "label": "my-ts-app (TypeScript)"
    }
  ]
}
```

`"dir"` paths are resolved relative to `rag_config.json`. Paths can point anywhere on disk.

### 2. Register the MCP server with your AI CLI

Pick one — or all three:

**gemini-cli** — automatic. `.gemini/settings.json` is already configured. Run `gemini` from the project root.

**GitHub Copilot CLI** — automatic `.vscode/mcp.json` is already configured. Run `copilot` from the project root.

**Claude Desktop** — follow [`.claude/mcp_config.md`](.claude/mcp_config.md) to add one entry to `~/Library/Application Support/Claude/claude_desktop_config.json`, then restart Claude.

That's it. The first time an agent uses the RAG tool, the wrapper handles everything else automatically.

---

## Available MCP tools

| Tool | Description |
|---|---|
| `query(question, top_k=5)` | Synthesised answer grounded in the source code |
| `retrieve(question, top_k=5)` | Raw scored chunks with file paths — no synthesis |

---

## Advanced configuration

| Option | Default | Override |
|---|---|---|
| Sources to index | defined in `rag_config.json` | edit that file |
| Index location | `.rag/repo_index/` | `RAG_INDEX_DIR=...` env var |
| Embedding model | `BAAI/bge-small-en-v1.5` (local, no API key) | `RAG_EMBED_MODEL=...` env var |
| Accelerator | Auto-detected: CUDA → MPS → CPU | set by torch at runtime |

### Force a full index rebuild

```bash
.rag/.venv/bin/python .rag/store.py
```

### Prerequisites

- Python 3.9+ (Python 3.14 supported)
- No API keys required