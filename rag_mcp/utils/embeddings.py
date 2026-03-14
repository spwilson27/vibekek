"""Chunking and embedding pipeline for code files."""

# MUST be set before ANY imports to suppress Hugging Face warnings
import os
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

from dataclasses import dataclass
from typing import Optional

from sentence_transformers import SentenceTransformer


# Embedding model - small, fast, good for code
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# Chunking settings
CHUNK_SIZE = 512  # characters
CHUNK_OVERLAP = 50  # characters


# Disable Hugging Face Hub telemetry and online checks
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


@dataclass
class Chunk:
    """A chunk of code with metadata."""
    content: str
    file_path: str
    start_line: int
    end_line: int
    content_hash: str


class Embedder:
    """Handles text embedding generation."""

    def __init__(self):
        self._model: Optional[SentenceTransformer] = None

    @property
    def model(self) -> SentenceTransformer:
        """Lazy-load the embedding model."""
        if self._model is None:
            self._model = SentenceTransformer(
                EMBEDDING_MODEL,
                trust_remote_code=False,
            )
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        if not texts:
            return []
        embeddings = self.model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return embeddings.tolist()

    def embed_single(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        return self.embed([text])[0]


def chunk_code_file(content: str, file_path: str, content_hash: str) -> list[Chunk]:
    """
    Split code file into overlapping chunks.
    
    Uses a simple character-based chunking strategy that respects line boundaries.
    """
    chunks = []
    lines = content.splitlines()
    
    current_content = []
    current_length = 0
    start_line = 1
    
    for i, line in enumerate(lines, 1):
        line_with_newline = line + "\n"
        line_length = len(line_with_newline)
        
        # If adding this line exceeds chunk size, save current chunk and start new one
        if current_length + line_length > CHUNK_SIZE and current_content:
            chunk_content = "".join(current_content)
            chunks.append(Chunk(
                content=chunk_content,
                file_path=file_path,
                start_line=start_line,
                end_line=i - 1,
                content_hash=f"{content_hash}_{start_line}_{i-1}",
            ))
            
            # Keep overlap: find how many lines to keep
            overlap_content = []
            overlap_length = 0
            for prev_line in reversed(current_content):
                if overlap_length + len(prev_line) <= CHUNK_OVERLAP:
                    overlap_content.insert(0, prev_line)
                    overlap_length += len(prev_line)
                else:
                    break
            
            current_content = overlap_content
            current_length = overlap_length
            start_line = i - len(overlap_content)
        
        current_content.append(line_with_newline)
        current_length += line_length
    
    # Add final chunk
    if current_content:
        chunk_content = "".join(current_content)
        chunks.append(Chunk(
            content=chunk_content,
            file_path=file_path,
            start_line=start_line,
            end_line=len(lines),
            content_hash=f"{content_hash}_{start_line}_{len(lines)}",
        ))
    
    return chunks
