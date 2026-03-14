"""Shared utilities for MCP tools."""


def truncate_content(content: str, max_size_kb: int) -> str:
    """
    Truncate content to max_size_kb while preserving complete lines.
    
    Args:
        content: The text content to truncate
        max_size_kb: Maximum size in kilobytes
        
    Returns:
        Truncated content with a notice appended
    """
    if max_size_kb <= 0:
        return content
    
    max_bytes = max_size_kb * 1024
    
    if len(content.encode("utf-8")) <= max_bytes:
        return content
    
    # Truncate and find last complete line
    truncated = content.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    
    # Find last newline to avoid cutting mid-line
    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        truncated = truncated[:last_newline + 1]
    
    # Add truncation notice
    truncated += "\n# [Content truncated due to size limit]\n"
    
    return truncated
