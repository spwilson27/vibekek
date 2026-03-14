#!/usr/bin/env python3
"""Background indexing server that runs independently of MCP server."""

import os
import sys
import time
import signal
from pathlib import Path

# MUST be set before ANY other imports to suppress Hugging Face warnings
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


class IndexServer:
    """Background server that keeps indexes up to date."""
    
    def __init__(self, repo_path: str, config):
        self.repo_path = repo_path
        self.config = config
        self.rag_tool = None
        self.semantic_tool = None
        self._running = True
        
    def start(self):
        """Start the indexing server."""
        print(f"Starting index server for: {self.repo_path}")
        
        # Initialize tools (this starts their background indexers)
        if self.config.is_tool_enabled("rag"):
            from .tools.rag import RAGTool
            index_dir = self.config.get_index_dir("rag")
            tool_config = self.config.get_tool_config("rag")
            self.rag_tool = RAGTool(self.repo_path, str(index_dir), tool_config or self._default_tool_config())
            print("  - RAG indexer initialized")
        
        if self.config.is_tool_enabled("semantic"):
            from .tools.semantic import SemanticTool
            index_dir = self.config.get_index_dir("semantic")
            tool_config = self.config.get_tool_config("semantic")
            self.semantic_tool = SemanticTool(self.repo_path, str(index_dir), tool_config or self._default_tool_config())
            print("  - Semantic indexer initialized")
        
        if not self.rag_tool and not self.semantic_tool:
            # Enable RAG by default
            from .tools.rag import RAGTool
            from .config import ToolConfig
            index_dir = self.config.get_index_dir("rag")
            self.rag_tool = RAGTool(self.repo_path, str(index_dir), self._default_tool_config())
            print("  - RAG indexer initialized (default)")
        
        print("Indexing started. Server will continue running to watch for file changes.")
        print("Press Ctrl+C to stop.")
        
        # Keep server running and periodically re-index
        self._run_watch_loop()
    
    def _default_tool_config(self):
        """Create default tool config."""
        from .config import ToolConfig
        return ToolConfig()
    
    def _run_watch_loop(self):
        """Watch for file changes and re-index periodically."""
        reindex_interval = 300  # Re-index every 5 minutes
        
        last_reindex = time.time()
        
        while self._running:
            time.sleep(1)
            
            # Check if it's time to re-index
            if time.time() - last_reindex > reindex_interval:
                print(f"[{time.strftime('%H:%M:%S')}] Periodic re-index triggered")
                self._trigger_reindex()
                last_reindex = time.time()
    
    def _trigger_reindex(self):
        """Trigger re-indexing of all tools."""
        if self.rag_tool:
            self.rag_tool.start()
        
        if self.semantic_tool:
            self.semantic_tool.start()
    
    def stop(self):
        """Stop the indexing server."""
        print("\nStopping index server...")
        self._running = False


def main():
    """Main entry point for the index server."""
    from .config import load_config, MCPConfig
    
    # Get config path from argument or find default
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not config_path:
        for c in ["config.jsonc", "config.json"]:
            if os.path.exists(c):
                config_path = c
                break
    
    if not config_path:
        print("Error: No config file found. Create config.json or config.jsonc in current directory.")
        sys.exit(1)
    
    config = load_config(config_path)
    repo_path = config.get_repo_path()
    
    if not repo_path or not Path(repo_path).exists():
        print(f"Error: Repository path does not exist: {repo_path}")
        sys.exit(1)
    
    server = IndexServer(repo_path, config)
    
    # Handle shutdown signals
    def signal_handler(signum, frame):
        server.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    server.start()


if __name__ == "__main__":
    main()
