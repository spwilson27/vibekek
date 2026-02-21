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

import torch
import faiss
from llama_index.core import Document, StorageContext, VectorStoreIndex
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.vector_stores.faiss import FaissVectorStore

from config_utils import load_config, expand_source_files


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


# `load_config` and `expand_source_files` are provided by `.config_utils`.
# Local `Source` dataclass and config parsing helpers were removed in
# favor of the shared utilities in `config_utils`.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# `collect_paths` replaced by `expand_source_files` from config_utils.


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
    sources: list[str] | None = None,
) -> None:
    """Build (or rebuild) the FAISS vector index.

    If `sources` is provided it overrides `rag_config.json` and should be a
    list of paths or glob patterns to index. When `sources` is None the
    configured sources from `config_path` are used (backwards compatible).
    """
    all_paths: list[tuple[str, str]] = []  # (file_path, source_label)

    if sources:
        # Expand provided paths/globs. Use simple globbing and include files only.
        import glob

        for s in sources:
            matched = sorted(glob.glob(s, recursive=True))
            matched_files = [p for p in matched if os.path.isfile(p)]
            print(f"[store] source={s}: {len(matched_files)} files")
            all_paths.extend((p, s) for p in matched_files)
        if not all_paths:
            print("[store] No files found for provided sources — nothing to index.")
            return
    else:
        # New `load_config` returns (sources, skip_dirs, config_dir)
        cfg_sources, skip_dirs, config_dir = load_config(config_path)
        if not cfg_sources:
            print("[store] No sources defined in config — nothing to index.")
            return

        # Collect paths from all configured sources, tagged with their label.
        for src in cfg_sources:
            src_paths = expand_source_files(src, skip_dirs)

            # Support both dict-like and attribute-like source objects
            if isinstance(src, dict):
                label = src.get("label") or src.get("dir") or "source"
                exts = src.get("extensions", [".md", ".txt"])
            else:
                label = getattr(src, "label", "source")
                exts = getattr(src, "extensions", [".md", ".txt"]) or [".md", ".txt"]

            exts_list = sorted(set(exts))
            print(f"[store] {label}: {len(src_paths)} files ({', '.join(exts_list)})")
            all_paths.extend((p, label) for p in src_paths)

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
    parser.add_argument("--paths",       nargs="+", default=None,    help="Optional list of paths/globs to index (overrides config)")
    parser.add_argument("--workers",     type=int, default=DEFAULT_WORKERS, help="Parallel workers")
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help="HuggingFace embed model")
    args = parser.parse_args()
    build_index(args.config, args.index, args.workers, args.embed_model, sources=args.paths)


if __name__ == "__main__":
    main()