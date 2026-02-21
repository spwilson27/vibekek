"""MCP stdio server — example-rs RAG.

Exposes two tools for semantic search over the persisted LlamaIndex vector
store built by store.py:
  - query(question)    → synthesised answer
  - retrieve(question) → raw scored chunks

The index is rebuilt automatically on startup when any source file is newer
than the stored FAISS index (staleness check via mtime comparison).
"""

import os
import sys
import psutil
import subprocess

# ---------------------------------------------------------------------------
# Config (can be overridden via env vars)
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("RAG_CONFIG",    os.path.join(_HERE, "rag_config.json"))
INDEX_DIR   = os.environ.get("RAG_INDEX_DIR", os.path.join(_HERE, "repo_index"))
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

# FAISS index file — its mtime is the staleness reference point
_FAISS_FILE = os.path.join(INDEX_DIR, "default__vector_store.json")


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------

def _newest_source_mtime() -> float:
    """Return the mtime of the most recently modified source file across all
    configured sources.  Returns 0.0 if the config can't be read."""
    import json
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        return 0.0

    config_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    skip_dirs  = set(cfg.get("skip_dirs", [".git", "target", "node_modules", ".venv", "dist"]))
    newest = 0.0

    for entry in cfg.get("sources", []):
        raw_dir  = entry["dir"]
        resolved = os.path.abspath(os.path.join(config_dir, raw_dir))
        exts     = tuple(entry.get("extensions", [".md", ".txt"]))
        for root, dirs, files in os.walk(resolved):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for name in files:
                if name.endswith(exts):
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(root, name)))
                    except OSError:
                        pass

    # Also watch the config file itself
    try:
        newest = max(newest, os.path.getmtime(CONFIG_PATH))
    except OSError:
        pass

    return newest


def _is_index_stale() -> bool:
    """Return True if the index doesn't exist or any source file is newer."""
    if not os.path.isfile(_FAISS_FILE):
        return True
    index_mtime  = os.path.getmtime(_FAISS_FILE)
    source_mtime = _newest_source_mtime()
    return source_mtime > index_mtime


# ---------------------------------------------------------------------------
# Auto-rebuild on startup
# ---------------------------------------------------------------------------

def _ensure_index() -> None:
    if _is_index_stale():
        print("[mcp_server] Index is missing or stale — rebuilding…", file=sys.stderr)
        # Import here so heavy deps (torch, llama-index) only load once
        from store import build_index  # store.py lives next to mcp_server.py
        build_index(
            config_path=CONFIG_PATH,
            index_dir=INDEX_DIR,
            embed_model=EMBED_MODEL,
        )
        print("[mcp_server] Index rebuild complete.", file=sys.stderr)
    else:
        print("[mcp_server] Index is up-to-date.", file=sys.stderr)


_ensure_index()

# ---------------------------------------------------------------------------
# Heavy imports (after index is guaranteed to exist)
# ---------------------------------------------------------------------------

import torch  # noqa: E402
from fastmcp import FastMCP  # noqa: E402
from llama_index.core import StorageContext, load_index_from_storage  # noqa: E402
from llama_index.embeddings.huggingface import HuggingFaceEmbedding  # noqa: E402
from llama_index.vector_stores.faiss import FaissVectorStore  # noqa: E402


def _detect_device() -> str:
    """Return the best available torch device: cuda > mps > cpu."""
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ---------------------------------------------------------------------------
# Lazy index loader
# ---------------------------------------------------------------------------
_index = None


def _get_index():
    global _index
    if _index is None:
        embed = HuggingFaceEmbedding(model_name=EMBED_MODEL, device=_detect_device())
        vector_store = FaissVectorStore.from_persist_dir(INDEX_DIR)
        sc = StorageContext.from_defaults(vector_store=vector_store, persist_dir=INDEX_DIR)
        _index = load_index_from_storage(sc, embed_model=embed)
    return _index


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="example-rs-rag",
    version="0.1.0",
)


def _detect_agent_cli() -> str | None:
    """Detect if we are running as a subprocess of gemini, claude, or copilot."""
    try:
        p = psutil.Process(os.getpid())
        for parent in p.parents():
            try:
                cmdline = parent.cmdline()
                if not cmdline:
                    continue
                cmd_str = " ".join(cmdline).lower()
                if "gemini" in cmdline[0].lower() or "gemini" in cmd_str:
                    return "gemini"
                if "claude" in cmdline[0].lower() or "claude" in cmd_str:
                    return "claude"
                if "copilot" in cmdline[0].lower() or "copilot" in cmd_str:
                    return "copilot"
            except Exception:
                pass
    except Exception:
        pass
    return None


@mcp.tool()
def query(question: str, top_k: int = 5) -> str:
    """Query the codebase using natural language.

    Args:
        question: A natural-language question about the codebase.
        top_k:    Number of top matching chunks to retrieve (default 5).

    Returns:
        A synthesised answer grounded in the source code.
    """
    retriever = _get_index().as_retriever(similarity_top_k=top_k)
    nodes = retriever.retrieve(question)
    
    if not nodes:
        return "No relevant context found in the codebase."
        
    context_blocks = []
    for n in nodes:
        file_path = n.metadata.get("file_path", "Unknown File")
        context_blocks.append(f"--- {file_path} ---\n{n.text}")
        
    context_str = "\n\n".join(context_blocks)
    
    prompt = (
        f"Answer the following question solely based on the retrieved context below.\n"
        f"QUESTION:\n{question}\n\n"
        f"CONTEXT:\n{context_str}\n\n"
        f"CRITICAL INSTRUCTION: Do NOT use any tools to answer this. Synthesize the answer immediately from the context provided above."
    )

    agent = _detect_agent_cli()
    if agent:
        try:
            print(f"[mcp_server] Detected parent agent '{agent}', synthesizing answer via subprocess...", file=sys.stderr)
            result = subprocess.run([agent, "-p", prompt], capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except Exception as e:
            print(f"[mcp_server] Subprocess synthesis (using {agent}) failed: {e}", file=sys.stderr)

    return (
        f"Retrieved context for your question: '{question}'\n\n"
        f"{context_str}\n\n"
        f"--- End of context ---\n"
        f"Please synthesize an answer for the user based on the above context."
    )


@mcp.tool()
def retrieve(question: str, top_k: int = 5) -> list[dict]:
    """Retrieve raw source chunks relevant to a question (no synthesis).

    Returns a list of dicts with keys: `file_path`, `score`, `text`.
    Useful when you want raw passages rather than a synthesised answer.
    """
    retriever = _get_index().as_retriever(similarity_top_k=top_k)
    nodes = retriever.retrieve(question)
    return [
        {
            "file_path": n.metadata.get("file_path", ""),
            "score": round(n.score or 0.0, 4),
            "text": n.text,
        }
        for n in nodes
    ]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
