"""Unified MCP Server that loads enabled tools."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .config import load_config, ToolConfig

if TYPE_CHECKING:
    from .tools.rag import RAGTool
    from .tools.semantic import SemanticTool


class LazyToolLoader:
    """Lazily loads and initializes tools on first access."""

    def __init__(self, repo_path: str, config: Any):
        self.repo_path = repo_path
        self.config = config
        self._tools: dict[str, Any] = {}
        self._loaded = False
        self._loading = False  # Prevent race condition during async loading

    def _load_tools(self):
        """Load enabled tools (called once on first access)."""
        if self._loaded:
            return

        # Import tools only when needed
        from .tools.rag import RAGTool
        from .tools.semantic import SemanticTool

        if self.config.is_tool_enabled("rag"):
            index_dir = self.config.get_index_dir("rag")
            tool_config = self.config.get_tool_config("rag") or ToolConfig()
            self._tools["rag"] = RAGTool(self.repo_path, str(index_dir), tool_config)

        if self.config.is_tool_enabled("semantic"):
            index_dir = self.config.get_index_dir("semantic")
            tool_config = self.config.get_tool_config("semantic") or ToolConfig()
            self._tools["semantic"] = SemanticTool(self.repo_path, str(index_dir), tool_config)

        if not self._tools:
            # Enable RAG by default if no tools configured
            index_dir = self.config.get_index_dir("rag")
            self._tools["rag"] = RAGTool(self.repo_path, str(index_dir), ToolConfig())

        self._loaded = True

    def start_background_indexing(self):
        """Start indexing in background threads (non-blocking)."""
        import threading

        def _load_and_start():
            self._load_tools()
            # Tools are already started in their constructors

        # Start loading in a daemon thread so it doesn't block shutdown
        thread = threading.Thread(target=_load_and_start, daemon=True)
        thread.start()

    def get_tools(self) -> dict[str, Any]:
        """Get all enabled tools, loading them if necessary."""
        self._load_tools()
        return self._tools

    def get_tool(self, name: str) -> Any | None:
        """Get a specific tool by name, loading tools if necessary."""
        self._load_tools()
        return self._tools.get(name)


def create_server(config_path: str | None = None):
    """Create and configure the MCP server with enabled tools."""

    # Load configuration
    config = load_config(config_path)
    repo_path = config.get_repo_path()

    # Create lazy tool loader (tools loaded on first access)
    tool_loader = LazyToolLoader(repo_path, config)

    # Start background indexing immediately (non-blocking)
    tool_loader.start_background_indexing()

    # Create MCP server
    server = Server("mcp-tools")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List all available tools from enabled tools."""
        tools = tool_loader.get_tools()
        mcp_tools = []

        for tool in tools.values():
            mcp_tools.append(tool.get_tool_definition())
            # Add status tool for each
            if hasattr(tool, "get_status_tool"):
                mcp_tools.append(tool.get_status_tool())
            # Add semantic list and hierarchy tools
            if hasattr(tool, "get_list_tool"):
                mcp_tools.append(tool.get_list_tool())
            if hasattr(tool, "get_hierarchy_tool"):
                mcp_tools.append(tool.get_hierarchy_tool())

        return mcp_tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Handle tool calls."""

        # Extract base tool name from method name (e.g., "rag_search" -> "rag")
        base_name = name.rsplit("_", 1)[0] if "_" in name else name

        # Get tools (lazy load if needed)
        tools = tool_loader.get_tools()
        tool = tools.get(base_name)

        if tool is None:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        # Check if this is the main search tool
        if name == f"{tool.name}_search" or (name == tool.name and hasattr(tool, "execute")):
            result = await tool.execute(arguments)
            return [TextContent(type="text", text=result)]

        # Check if this is the status tool
        if name == f"{tool.name}_status" and hasattr(tool, "execute_status"):
            result = await tool.execute_status(arguments)
            return [TextContent(type="text", text=result)]

        # Check if this is the semantic list tool
        if name == f"{tool.name}_list" and hasattr(tool, "execute_list"):
            result = await tool.execute_list(arguments)
            return [TextContent(type="text", text=result)]

        # Check if this is the semantic hierarchy tool
        if name == f"{tool.name}_hierarchy" and hasattr(tool, "execute_hierarchy"):
            result = await tool.execute_hierarchy(arguments)
            return [TextContent(type="text", text=result)]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return server


async def main_async():
    """Main async entry point for the MCP server."""
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    server = create_server(config_path)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main():
    """Main entry point for the MCP server."""
    import asyncio
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
