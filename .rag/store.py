"""Parallelised LlamaIndex ingestion pipeline for example-rs.

Usage:
    python store.py
    python store.py --repo ../example-rs --index ./repo_index --workers 8

The script walks the target repo directory, loads every Rust/TOML/Markdown
file in parallel, splits them with SentenceSplitter, generates embeddings
(GPU-accelerated when CUDA/MPS is available, otherwise CPU), and persists
the resulting vector index to disk so the MCP server can reload it cheaply.
"""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import torch
import faiss
from llama_index.core import Document, StorageContext, VectorStoreIndex
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.vector_stores.faiss import FaissVectorStore


def _detect_device() -> str:
    """Return the best available torch device: cuda > mps > cpu."""
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG  = os.path.join(_HERE, "rag_config.json")
DEFAULT_INDEX   = os.path.join(_HERE, "repo_index")
DEFAULT_WORKERS = min(8, (os.cpu_count() or 4))
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass
class Source:
    directory: str
    extensions: frozenset
    label: str


def load_config(config_path: str) -> tuple[list[Source], set[str]]:
    """Parse rag_config.json and return (sources, skip_dirs)."""
    with open(config_path) as f:
        cfg = json.load(f)

    config_dir = os.path.dirname(os.path.abspath(config_path))
    skip_dirs = set(cfg.get("skip_dirs", [".git", "target", "node_modules", ".venv", "dist"]))

    sources = []
    for entry in cfg.get("sources", []):
        raw_dir = entry["dir"]
        # Resolve relative paths from the config file's location
        resolved = os.path.abspath(os.path.join(config_dir, raw_dir))
        sources.append(Source(
            directory=resolved,
            extensions=frozenset(entry.get("extensions", [".md", ".txt"])),
            label=entry.get("label", raw_dir),
        ))
    return sources, skip_dirs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def collect_paths(source: Source, skip_dirs: set[str]) -> list[str]:
    """Return all indexable file paths for a single Source entry."""
    paths = []
    for root, dirs, files in os.walk(source.directory):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if any(name.endswith(ext) for ext in source.extensions):
                paths.append(os.path.join(root, name))
    return paths


def load_file(path: str) -> Document:
    """Read a single file and return a LlamaIndex Document."""
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    rel = os.path.relpath(path)
    return Document(
        text=text,
        metadata={
            "file_path": path,
            "file_name": os.path.basename(path),
            "relative_path": rel,
        },
        doc_id=rel,
    )


# ---------------------------------------------------------------------------
# Build function (callable by mcp_server for auto-rebuild)
# ---------------------------------------------------------------------------

def build_index(
    config_path: str = DEFAULT_CONFIG,
    index_dir: str = DEFAULT_INDEX,
    workers: int = DEFAULT_WORKERS,
    embed_model: str = DEFAULT_EMBED_MODEL,
) -> None:
    """Build (or rebuild) the FAISS vector index from rag_config.json sources."""
    sources, skip_dirs = load_config(config_path)
    if not sources:
        print("[store] No sources defined in config — nothing to index.")
        return

    # Collect paths from all sources, tagged with their label
    all_paths: list[tuple[str, str]] = []  # (file_path, source_label)
    for src in sources:
        src_paths = collect_paths(src, skip_dirs)
        print(f"[store] {src.label}: {len(src_paths)} files ({', '.join(sorted(src.extensions))})")
        all_paths.extend((p, src.label) for p in src_paths)

    total = len(all_paths)
    print(f"[store] Total: {total} files — loading with {workers} parallel workers…")

    # ── Phase 1: parallel file I/O ──────────────────────────────────────────
    docs: list[Document] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(load_file, p): (p, lbl) for p, lbl in all_paths}
        for fut in as_completed(futures):
            try:
                docs.append(fut.result())
            except Exception as exc:
                errors += 1
                path, _ = futures[fut]
                print(f"  WARN: skipping {path}: {exc}")

    print(f"[store] Loaded {len(docs)} documents ({errors} skipped)")

    # ── Phase 2: embedding (GPU if available) ─────────────────────────────
    device = _detect_device()
    print(f"[store] Embedding device: {device}")
    embed = HuggingFaceEmbedding(model_name=embed_model, device=device)
    pipeline = IngestionPipeline(
        transformations=[
            SentenceSplitter(chunk_size=512, chunk_overlap=64),
            embed,
        ]
    )

    print(f"[store] Running ingestion pipeline…")
    nodes = pipeline.run(documents=docs)
    print(f"[store] Generated {len(nodes)} nodes")

    # ── Phase 3: build FAISS vector index & persist ────────────────────────
    print(f"[store] Building FAISS vector index…")
    dim = len(embed.get_text_embedding("probe"))
    faiss_index = faiss.IndexFlatL2(dim)
    vector_store = FaissVectorStore(faiss_index=faiss_index)
    storage_ctx = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex(nodes, storage_context=storage_ctx, embed_model=embed)
    index.storage_context.persist(persist_dir=index_dir)
    print(f"[store] ✓ Index saved to {index_dir} (FAISS binary + docstore JSON)")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build LlamaIndex RAG index.")
    parser.add_argument("--config",      default=DEFAULT_CONFIG,     help="Path to rag_config.json")
    parser.add_argument("--index",       default=DEFAULT_INDEX,      help="Directory to persist the index")
    parser.add_argument("--workers",     type=int, default=DEFAULT_WORKERS, help="Parallel workers")
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help="HuggingFace embed model")
    args = parser.parse_args()
    build_index(args.config, args.index, args.workers, args.embed_model)


if __name__ == "__main__":
    main()