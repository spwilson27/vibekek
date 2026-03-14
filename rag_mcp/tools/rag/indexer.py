"""Background indexer with cleanup logic and size limits."""

import threading
from pathlib import Path

from ...utils.scanner import scan_git_repo, get_directory_size_mb
from ...utils.embeddings import chunk_code_file
from ...utils import truncate_content
from .store import VectorStore
from ...config import ToolConfig


class Indexer:
    """Background indexer for the RAG system."""

    def __init__(self, repo_path: str, vector_store: VectorStore, tool_config: ToolConfig):
        self.repo_path = repo_path
        self.vector_store = vector_store
        self.tool_config = tool_config
        self._thread: threading.Thread | None = None

    def start_indexing(self):
        """Start background indexing thread."""
        if self._thread and self._thread.is_alive():
            return  # Already indexing

        self._thread = threading.Thread(target=self._index_loop, daemon=True)
        self._thread.start()

    def _index_loop(self):
        """Main indexing loop with size limits."""
        self.vector_store.is_indexing = True
        self.vector_store.reset_counters()

        limits = self.tool_config.limits
        priority = self.tool_config.priority

        try:
            # Scan repository for files with filtering
            # Skip indexing if repo path doesn't exist yet (e.g., in tests)
            if not Path(self.repo_path).exists():
                return
                
            files = list(scan_git_repo(
                self.repo_path,
                exclude_dirs=priority.exclude_dirs,
                extensions=priority.extensions,
                max_file_size_kb=limits.max_file_size_kb,
                priority_dirs=priority.dirs,
            ))
            
            # Apply max_files limit
            if limits.max_files > 0 and len(files) > limits.max_files:
                files = files[:limits.max_files]
            
            self.vector_store.set_file_count(len(files))

            # Get currently indexed files for cleanup
            # Skip if database doesn't exist yet
            try:
                indexed_files = self.vector_store.get_indexed_files()
            except Exception:
                indexed_files = set()
            current_files = {str(f) for f, _, _ in files}

            # Remove deleted files
            deleted_files = indexed_files - current_files
            for file_path in deleted_files:
                self.vector_store.remove_file_chunks(file_path)

            # Index new/updated files
            for file_path, content_hash, priority in files:
                # Check if we've hit the index size limit
                try:
                    if limits.max_index_size_mb > 0:
                        index_size_mb = get_directory_size_mb(self.vector_store.index_dir)
                        if index_size_mb >= limits.max_index_size_mb:
                            break
                except Exception:
                    # Skip limit check if directory isn't accessible
                    pass

                # Check if we've hit the chunk limit (deprecated, kept for backward compat)
                try:
                    if limits.max_chunks > 0:
                        current_chunks = self.vector_store.get_chunk_count()
                        if current_chunks >= limits.max_chunks:
                            break
                except Exception:
                    # Skip limit check if database isn't ready
                    pass

                try:
                    content = file_path.read_text()

                    # Truncate if needed
                    if limits.truncate_size_kb > 0:
                        content = truncate_content(content, limits.truncate_size_kb)

                    # Chunk the file
                    chunks = chunk_code_file(content, str(file_path), content_hash)

                    # Add to vector store
                    self.vector_store.add_chunks(chunks)
                    self.vector_store.increment_indexed()

                except Exception as e:
                    # Skip files that can't be read
                    self.vector_store.increment_indexed()
                    continue

        finally:
            self.vector_store.is_indexing = False

    def is_indexing(self) -> bool:
        """Check if indexing is in progress."""
        return self._thread is not None and self._thread.is_alive()
