"""Configuration module for MCP tools."""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from jsoncomment import JsonComment


# Default excluded directories (vendor-like dirs)
DEFAULT_EXCLUDE_DIRS = [
    "node_modules",
    "vendor",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    ".env",
    "dist",
    "build",
    "target",
    "third_party",
    "extern",
]


@dataclass
class LimitConfig:
    """Size limits for tool indexes."""
    max_files: int = 1000          # Max files to index
    max_chunks: int = 20000        # Max chunks (RAG) or symbols (Semantic), deprecated in favor of max_index_size_mb
    max_index_size_mb: int = 100   # Max index size in MB (0 = no limit)
    max_file_size_kb: int = 1024   # Skip files larger than this (0 = no limit)
    truncate_size_kb: int = 256    # Truncate files to this size before indexing (0 = no truncation)

    @classmethod
    def from_dict(cls, data: dict) -> "LimitConfig":
        return cls(
            max_files=data.get("max_files", 1000),
            max_chunks=data.get("max_chunks", 20000),
            max_index_size_mb=data.get("max_index_size_mb", 100),
            max_file_size_kb=data.get("max_file_size_kb", 1024),
            truncate_size_kb=data.get("truncate_size_kb", 256),
        )


@dataclass
class PriorityConfig:
    """Priority configuration for indexing."""
    dirs: list[str] = field(default_factory=list)       # Index these dirs first
    exclude_dirs: list[str] = field(default_factory=list)  # Exclude these dirs
    extensions: list[str] = field(default_factory=list)    # Only these extensions (empty = all code)
    
    @classmethod
    def from_dict(cls, data: dict) -> "PriorityConfig":
        # Merge with defaults for exclude_dirs
        exclude_dirs = data.get("exclude_dirs", [])
        if not exclude_dirs:
            exclude_dirs = DEFAULT_EXCLUDE_DIRS.copy()
        
        return cls(
            dirs=data.get("dirs", []),
            exclude_dirs=exclude_dirs,
            extensions=data.get("extensions", []),
        )


@dataclass
class ToolConfig:
    """Configuration for a single tool."""
    enabled: bool = True
    index_dir: Optional[str] = None
    limits: LimitConfig = field(default_factory=LimitConfig)
    priority: PriorityConfig = field(default_factory=PriorityConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "ToolConfig":
        limits_data = data.get("limits", {})
        priority_data = data.get("priority", {})
        
        return cls(
            enabled=data.get("enabled", True),
            index_dir=data.get("index_dir"),
            limits=LimitConfig.from_dict(limits_data),
            priority=PriorityConfig.from_dict(priority_data),
        )


@dataclass
class MCPConfig:
    """Main configuration for MCP server."""
    repo_path: Optional[str] = None
    tools: dict[str, ToolConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "MCPConfig":
        tools = {}
        for tool_name, tool_data in data.get("tools", {}).items():
            tools[tool_name] = ToolConfig.from_dict(tool_data)

        return cls(
            repo_path=data.get("repo_path"),
            tools=tools,
        )

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "MCPConfig":
        """Load configuration from file or create default."""
        if config_path and Path(config_path).exists():
            parser = JsonComment()
            with open(config_path) as f:
                data = parser.load(f)
            return cls.from_dict(data)

        # Return default config
        return cls()

    def get_tool_config(self, name: str) -> Optional[ToolConfig]:
        """Get configuration for a specific tool."""
        return self.tools.get(name)

    def is_tool_enabled(self, name: str) -> bool:
        """Check if a tool is enabled."""
        config = self.get_tool_config(name)
        return config is not None and config.enabled

    def get_repo_path(self) -> str:
        """Get repository path, defaulting to current directory."""
        if self.repo_path:
            return self.repo_path
        return str(Path.cwd())

    def get_index_dir(self, tool_name: str) -> Path:
        """Get index directory for a tool."""
        repo_path = self.get_repo_path()
        md5hash = hashlib.md5(repo_path.encode()).hexdigest()

        tool_config = self.get_tool_config(tool_name)
        if tool_config and tool_config.index_dir:
            # Support {hash} placeholder in custom paths
            return Path(tool_config.index_dir.replace("{hash}", md5hash))

        # Default to /tmp/<tool>-<hash>
        return Path("/tmp") / f"{tool_name}-{md5hash}"


def load_config(config_path: Optional[str] = None) -> MCPConfig:
    """Load MCP configuration."""
    return MCPConfig.load(config_path)
