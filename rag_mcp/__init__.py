"""RAG MCP Server - Multi-tool code search."""

__version__ = "0.2.0"

# Lazy imports to avoid loading heavy dependencies on package import
def __getattr__(name: str):
    if name == "load_config":
        from .config import load_config
        return load_config
    elif name == "create_server":
        from .server import create_server
        return create_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["load_config", "create_server"]
