"""ChromaDB vector store with incremental update support."""

from pathlib import Path
from typing import Optional

import chromadb

from ...utils.embeddings import Chunk, Embedder


class VectorStore:
    """ChromaDB-based vector store for code search."""

    def __init__(self, index_dir: Path):
        self.index_dir = index_dir
        self.index_dir.mkdir(parents=True, exist_ok=True)

        # Lazy initialization - defer expensive ChromaDB setup
        self._client: Optional[chromadb.PersistentClient] = None
        self._collection = None
        self._embedder = Embedder()

        # Status tracking
        self._is_indexing = False
        self._total_files = 0
        self._indexed_files = 0

    def _ensure_initialized(self):
        """Lazy initialization of ChromaDB client and collection."""
        if self._client is None:
            self._client = chromadb.PersistentClient(path=str(self.index_dir))
            self._collection = self._client.get_or_create_collection(
                name="code_index",
                metadata={"hnsw:space": "cosine"},
            )
    
    @property
    def is_indexing(self) -> bool:
        """Whether indexing is currently in progress."""
        return self._is_indexing
    
    @is_indexing.setter
    def is_indexing(self, value: bool):
        self._is_indexing = value
    
    @property
    def indexing_status(self) -> dict:
        """Get current indexing status."""
        self._ensure_initialized()
        return {
            "is_indexing": self._is_indexing,
            "indexed_files": self._indexed_files,
            "total_files": self._total_files,
            "total_chunks": self._collection.count(),
        }
    
    def set_file_count(self, count: int):
        """Set total file count for progress tracking."""
        self._total_files = count
    
    def increment_indexed(self):
        """Increment the indexed file counter."""
        self._indexed_files += 1
    
    def reset_counters(self):
        """Reset indexing counters."""
        self._indexed_files = 0
        self._total_files = 0

    def get_chunk_count(self) -> int:
        """Get current number of chunks in the store."""
        self._ensure_initialized()
        return self._collection.count()
    
    def add_chunks(self, chunks: list[Chunk]):
        """Add chunks to the vector store."""
        if not chunks:
            return

        self._ensure_initialized()

        # Generate embeddings
        contents = [chunk.content for chunk in chunks]
        embeddings = self._embedder.embed(contents)

        # Prepare metadata
        metadatas = []
        for chunk in chunks:
            metadatas.append({
                "file_path": chunk.file_path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "content_hash": chunk.content_hash,
            })

        # Use upsert to handle existing IDs - updates instead of creating duplicates
        self._collection.upsert(
            ids=[chunk.content_hash for chunk in chunks],
            embeddings=embeddings,
            metadatas=metadatas,
            documents=contents,
        )

        # Store metadata file for cleanup tracking
        for chunk in chunks:
            meta_path = self.index_dir / f"{chunk.content_hash}.meta"
            meta_path.write_text(chunk.file_path)
    
    def remove_file_chunks(self, file_path: str):
        """Remove all chunks associated with a file."""
        self._ensure_initialized()
        # Get all IDs for this file
        results = self._collection.get(
            where={"file_path": file_path},
            include=["metadatas"],
        )

        if results["ids"]:
            self._collection.delete(ids=results["ids"])

        # Remove meta files
        for meta_file in self.index_dir.glob("*.meta"):
            try:
                if meta_file.read_text().strip() == file_path:
                    meta_file.unlink()
            except Exception:
                continue
    
    def search(self, query: str, n_results: int = 10) -> list[dict]:
        """Search for relevant code chunks."""
        self._ensure_initialized()
        # Generate query embedding
        query_embedding = self._embedder.embed_single(query)

        # Query collection
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )

        # Format results
        formatted = []
        if results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results["distances"] else 0
                formatted.append({
                    "content": doc,
                    "file_path": metadata.get("file_path", ""),
                    "start_line": metadata.get("start_line", 0),
                    "end_line": metadata.get("end_line", 0),
                    "relevance_score": 1 - distance,  # Convert distance to similarity
                })

        # Remove duplicates based on (file_path, start_line, end_line, content)
        # Keep the first occurrence (highest relevance score)
        seen = set()
        deduplicated = []
        for result in formatted:
            key = (result["file_path"], result["start_line"], result["end_line"], result["content"])
            if key not in seen:
                seen.add(key)
                deduplicated.append(result)

        return deduplicated
    
    def get_indexed_files(self) -> set[str]:
        """Get set of all indexed file paths."""
        files = set()
        for meta_file in self.index_dir.glob("*.meta"):
            try:
                files.add(meta_file.read_text().strip())
            except Exception:
                continue
        return files
