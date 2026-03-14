"""Base tool interface for MCP tools."""

from abc import ABC, abstractmethod
from typing import Any

from mcp.types import Tool as MCPTool


class BaseTool(ABC):
    """Base class for MCP tools."""
    
    name: str
    description: str
    
    @abstractmethod
    def get_tool_definition(self) -> MCPTool:
        """Return the MCP tool definition."""
        pass
    
    @abstractmethod
    async def execute(self, arguments: dict[str, Any]) -> str:
        """Execute the tool with given arguments."""
        pass
    
    @abstractmethod
    def start(self) -> None:
        """Start background tasks (indexing, etc.)."""
        pass
    
    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """Get tool status information."""
        pass
