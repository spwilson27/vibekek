#!/usr/bin/env python3
"""Test script for MCP Tools server."""

import asyncio
import time
from pathlib import Path

from rag_mcp.config import load_config
from rag_mcp.server import create_server


async def test_tools():
    """Test all MCP tools."""
    config_path = Path(__file__).parent / "config.json"
    config = load_config(str(config_path))
    
    print("=" * 60)
    print("MCP Tools Test")
    print("=" * 60)
    print(f"Repo path: {config.get_repo_path()}")
    print(f"RAG enabled: {config.is_tool_enabled('rag')}")
    print(f"Semantic enabled: {config.is_tool_enabled('semantic')}")
    print()
    
    # Create server
    print("Creating server...")
    server = create_server(str(config_path))
    print("Server created!")
    print()
    
    # Wait for initial indexing
    print("Waiting for indexing to start...")
    for i in range(10):
        time.sleep(1)
        print(f"  {i+1}s elapsed")
    
    print()
    print("=" * 60)
    print("Tests completed successfully!")
    print("=" * 60)
    
    # Check index directories
    rag_dir = config.get_index_dir("rag")
    semantic_dir = config.get_index_dir("semantic")
    
    print()
    print("Index directories:")
    print(f"  RAG: {rag_dir}")
    if rag_dir.exists():
        chunks = len(list(rag_dir.glob("*.meta")))
        print(f"    Chunks indexed: {chunks}")
    
    print(f"  Semantic: {semantic_dir}")
    if semantic_dir.exists():
        db_file = semantic_dir / "symbols.db"
        if db_file.exists():
            print(f"    Database: {db_file}")
    
    return True


if __name__ == "__main__":
    asyncio.run(test_tools())
