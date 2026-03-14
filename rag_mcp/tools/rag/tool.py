"""RAG tool implementation."""

import asyncio
from pathlib import Path
from typing import Any

from mcp.types import Tool as MCPTool

from ..base import BaseTool
from .indexer import Indexer
from .store import VectorStore
from ...config import ToolConfig


class RAGTool(BaseTool):
    """RAG (Retrieval-Augmented Generation) tool for natural language code search."""

    name = "rag"
    description = "Search code using natural language queries"

    def __init__(self, repo_path: str, index_dir: str, tool_config: ToolConfig):
        self.repo_path = repo_path
        self.index_dir = Path(index_dir)
        self.tool_config = tool_config
        self.vector_store = VectorStore(self.index_dir)
        self.indexer = Indexer(repo_path, self.vector_store, tool_config)
        # Start background indexing automatically
        self.indexer.start_indexing()
    
    def get_tool_definition(self) -> MCPTool:
        """Return MCP tool definition for rag_search."""
        return MCPTool(
            name="rag_search",
            description="Search code using RAG. Returns relevant code chunks with file locations and line numbers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query (natural language or code)",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Number of results to return (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        )
    
    async def execute(self, arguments: dict[str, Any]) -> str:
        """Execute RAG search."""
        query = arguments.get("query", "")
        n_results = arguments.get("n_results", 10)
        
        if not query:
            return "Error: query is required"
        
        # Search
        results = self.vector_store.search(query, n_results)
        
        # Format results
        output_parts = []
        
        # Add warning if still indexing
        if self.vector_store.is_indexing:
            status = self.vector_store.indexing_status
            output_parts.append(
                f"⚠️ **Warning**: Indexing is still in progress "
                f"({status['indexed_files']}/{status['total_files']} files indexed, "
                f"{status['total_chunks']} chunks in index).\n"
                f"Results may be incomplete.\n"
            )
        
        if not results:
            output_parts.append("No results found.")
        else:
            output_parts.append(f"Found {len(results)} relevant result(s):\n")
            
            for i, result in enumerate(results, 1):
                output_parts.append(
                    f"--- Result {i} ---\n"
                    f"File: {result['file_path']}\n"
                    f"Lines: {result['start_line']}-{result['end_line']}\n"
                    f"Relevance: {result['relevance_score']:.2%}\n"
                    f"```\n{result['content']}\n```\n"
                )
        
        return "\n".join(output_parts)
    
    def start(self) -> None:
        """Start background indexing."""
        self.indexer.start_indexing()
    
    def get_status(self) -> dict[str, Any]:
        """Get RAG tool status."""
        return self.vector_store.indexing_status
    
    def get_status_tool(self) -> MCPTool:
        """Return MCP tool definition for rag_status."""
        return MCPTool(
            name="rag_status",
            description="Get the current RAG indexing status (progress, file count, etc.)",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        )
    
    async def execute_status(self, arguments: dict[str, Any]) -> str:
        """Execute rag_status tool."""
        status = self.vector_store.indexing_status
        
        output = "## RAG Index Status\n\n"
        output += f"- **Indexing in progress**: {status['is_indexing']}\n"
        output += f"- **Files indexed**: {status['indexed_files']}/{status['total_files']}\n"
        output += f"- **Total chunks**: {status['total_chunks']}\n"
        
        if status['total_files'] > 0:
            progress = (status['indexed_files'] / status['total_files']) * 100
            output += f"- **Progress**: {progress:.1f}%\n"
        
        return output
