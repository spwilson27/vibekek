"""Semantic search tool implementation."""

import hashlib
import threading
from pathlib import Path
from typing import Any

from mcp.types import Tool as MCPTool

from ..base import BaseTool
from ...utils.scanner import scan_git_repo, is_code_file, get_directory_size_mb
from ...utils import truncate_content
from .store import SymbolDatabase
from .indexer import CodeParser
from ...config import ToolConfig


class SemanticTool(BaseTool):
    """Semantic search tool for finding code definitions and references."""

    name = "semantic"
    description = "Search for code definitions, references, and symbols"

    def __init__(self, repo_path: str, index_dir: str, tool_config: ToolConfig):
        self.repo_path = repo_path
        self.index_dir = Path(index_dir)
        self.tool_config = tool_config
        self.db = SymbolDatabase(self.index_dir, tool_config.pagerank)
        self.parser = CodeParser()
        self._thread: threading.Thread | None = None
        # Start background indexing automatically
        self.start()
    
    def get_tool_definition(self) -> MCPTool:
        """Return MCP tool definition for semantic_search."""
        return MCPTool(
            name="semantic_search",
            description="Search for code symbols by name. Find definitions and references.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The symbol name to search for",
                    },
                    "search_type": {
                        "type": "string",
                        "description": "Type of search: 'definition', 'references', or 'all'",
                        "enum": ["definition", "references", "all"],
                        "default": "all",
                    },
                    "symbol_type": {
                        "type": "string",
                        "description": "Filter by symbol type: function, class, method, variable, interface, type",
                        "enum": ["function", "class", "method", "variable", "interface", "type"],
                    },
                    "line_limit": {
                        "type": "integer",
                        "description": "Maximum lines to display per result (0 = no limit, default: 25)",
                        "default": 25,
                    },
                },
                "required": ["query"],
            },
        )

    def get_list_tool(self) -> MCPTool:
        """Return MCP tool definition for semantic_list."""
        return MCPTool(
            name="semantic_list",
            description="List symbols by type or from a specific file. Supports browsing and discovery.",
            inputSchema={
                "type": "object",
                "properties": {
                    "list_type": {
                        "type": "string",
                        "description": "What to list: 'by_type' for all symbols of a type, 'file' for symbols in a file",
                        "enum": ["by_type", "file"],
                    },
                    "symbol_type": {
                        "type": "string",
                        "description": "Symbol type when list_type is 'by_type': function, class, method, variable, interface, type",
                        "enum": ["function", "class", "method", "variable", "interface", "type"],
                    },
                    "file_path": {
                        "type": "string",
                        "description": "File path when list_type is 'file'",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 100)",
                        "default": 100,
                    },
                },
            },
        )

    def get_hierarchy_tool(self) -> MCPTool:
        """Return MCP tool definition for semantic_hierarchy."""
        return MCPTool(
            name="semantic_hierarchy",
            description="Get call hierarchy or class inheritance. Find callers, callees, or class relationships.",
            inputSchema={
                "type": "object",
                "properties": {
                    "hierarchy_type": {
                        "type": "string",
                        "description": "Type of hierarchy: 'callers' (who calls this), 'callees' (what this calls), 'class' (class inheritance)",
                        "enum": ["callers", "callees", "class"],
                    },
                    "symbol_name": {
                        "type": "string",
                        "description": "Symbol name to analyze",
                    },
                },
                "required": ["hierarchy_type", "symbol_name"],
            },
        )
    
    async def execute(self, arguments: dict[str, Any]) -> str:
        """Execute semantic search."""
        query = arguments.get("query", "")
        search_type = arguments.get("search_type", "all")
        symbol_type = arguments.get("symbol_type")
        # Allow argument to override config
        line_limit = arguments.get("line_limit", self.tool_config.limits.line_limit)

        if not query:
            return "Error: query is required"

        output_parts = []

        # Add warning if still indexing
        if self.db.is_indexing:
            status = self.db.indexing_status
            output_parts.append(
                f"⚠️ **Warning**: Indexing is still in progress "
                f"({status['indexed_files']}/{status['total_files']} files indexed, "
                f"{status['total_symbols']} symbols in index).\n"
                f"Results may be incomplete.\n\n"
            )

        if search_type in ("definition", "all"):
            definitions = self.db.find_definitions(query, symbol_type)
            if definitions:
                output_parts.append(f"## Definitions ({len(definitions)})\n")
                for i, defn in enumerate(definitions, 1):
                    content = defn['content']
                    truncated = False
                    
                    # Apply line limit if configured
                    if line_limit > 0:
                        lines = content.split('\n')
                        if len(lines) > line_limit:
                            content = '\n'.join(lines[:line_limit])
                            truncated = True
                    
                    output_parts.append(
                        f"### {defn['name']} ({defn['symbol_type']})\n"
                        f"File: {defn['file_path']}\n"
                        f"Lines: {defn['start_line']}-{defn['end_line']}\n"
                        f"Language: {defn['language']}\n"
                    )
                    if defn.get('pagerank_score'):
                        output_parts.append(f"PageRank Score: {defn['pagerank_score']:.6f}\n")
                    output_parts.append(f"```\n{content}\n```")
                    if truncated:
                        output_parts.append(f"\n⚠️ **Note**: Content truncated to first {line_limit} lines\n")
                    output_parts.append("\n")
            elif search_type == "definition":
                output_parts.append(f"No definitions found for '{query}'.\n")

        if search_type in ("references", "all"):
            references = self.db.find_references(query)
            if references:
                output_parts.append(f"\n## References ({len(references)})\n")
                for i, ref in enumerate(references, 1):
                    content = ref['content']
                    truncated = False
                    
                    # Apply line limit if configured
                    if line_limit > 0:
                        lines = content.split('\n')
                        if len(lines) > line_limit:
                            content = '\n'.join(lines[:line_limit])
                            truncated = True
                    
                    output_parts.append(
                        f"### Reference {i}\n"
                        f"File: {ref['file_path']}\n"
                        f"Lines: {ref['start_line']}-{ref['end_line']}\n"
                        f"```\n{content}\n```"
                    )
                    if truncated:
                        output_parts.append(f"\n⚠️ **Note**: Content truncated to first {line_limit} lines\n")
                    output_parts.append("\n")
            elif search_type == "references" and not output_parts:
                output_parts.append(f"No references found for '{query}'.\n")

        if not output_parts or (len(output_parts) == 1 and "Warning" in output_parts[0]):
            output_parts.append(f"No results found for '{query}'.\n")

        return "\n".join(output_parts)
    
    def start(self) -> None:
        """Start background indexing."""
        if self._thread and self._thread.is_alive():
            return
        
        self._thread = threading.Thread(target=self._index_loop, daemon=True)
        self._thread.start()
    
    def _index_loop(self):
        """Main indexing loop with size limits."""
        self.db.is_indexing = True
        self.db.reset_counters()

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

            self.db.set_file_count(len(files))

            # Get currently indexed files
            # Skip if database doesn't exist yet
            try:
                indexed_files = self.db.get_indexed_files()
            except Exception:
                indexed_files = {}
            current_files = {str(f) for f, _, _ in files}

            # Remove deleted files
            deleted_files = set(indexed_files.keys()) - current_files
            for file_path in deleted_files:
                self.db.remove_file_symbols(file_path)

            # Index new/updated files
            for file_path, content_hash, priority in files:
                # Check if we've hit the index size limit
                try:
                    if limits.max_index_size_mb > 0:
                        index_size_mb = get_directory_size_mb(self.index_dir)
                        if index_size_mb >= limits.max_index_size_mb:
                            break
                except Exception:
                    # Skip limit check if directory isn't accessible
                    pass

                # Check if we've hit the symbol limit (deprecated, kept for backward compat)
                try:
                    if limits.max_chunks > 0:
                        current_symbols = self.db.get_symbol_count()
                        if current_symbols >= limits.max_chunks:
                            break
                except Exception:
                    # Skip limit check if database isn't ready
                    pass

                try:
                    # Skip if file hasn't changed
                    if file_path in indexed_files and indexed_files[file_path] == content_hash:
                        self.db.increment_indexed()
                        continue

                    # Check if it's a code file
                    if not is_code_file(Path(file_path), set(self.tool_config.priority.extensions) if self.tool_config.priority.extensions else None):
                        self.db.increment_indexed()
                        continue

                    content = Path(file_path).read_text()

                    # Truncate if needed
                    if limits.truncate_size_kb > 0:
                        content = truncate_content(content, limits.truncate_size_kb)

                    # Parse and extract symbols
                    symbols = self.parser.parse_file(Path(file_path), content)

                    for symbol in symbols:
                        symbol_hash = hashlib.md5(
                            f"{symbol.file_path}:{symbol.start_line}:{symbol.name}".encode()
                        ).hexdigest()
                        
                        # Register with PageRank
                        self.db.register_symbol_for_pagerank(symbol)
                        
                        self.db.add_symbol(symbol, symbol_hash)

                        # Extract and store call relationships for functions/methods
                        if symbol.symbol_type in ("function", "method"):
                            calls = self.parser.extract_calls(Path(file_path), content, symbol)
                            for call_name, call_line, call_content, call_hash in calls:
                                self.db.add_call(symbol, call_name, call_line, call_content, call_hash)
                                # Register call with PageRank
                                self.db.register_call_for_pagerank(symbol, call_name)

                    self.db.mark_file_indexed(file_path, content_hash)
                    self.db.increment_indexed()

                except Exception:
                    self.db.increment_indexed()
                    continue

        finally:
            # Compute PageRank after indexing is complete
            if self.db.is_pagerank_enabled():
                self.db.compute_pagerank()
            self.db.is_indexing = False
    
    def get_status(self) -> dict[str, Any]:
        """Get semantic tool status."""
        return self.db.indexing_status
    
    def get_status_tool(self) -> MCPTool:
        """Return MCP tool definition for semantic_status."""
        return MCPTool(
            name="semantic_status",
            description="Get the current semantic indexing status",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        )
    
    async def execute_status(self, arguments: dict[str, Any]) -> str:
        """Execute semantic_status tool."""
        status = self.db.indexing_status

        output = "## Semantic Index Status\n\n"
        output += f"- **Indexing in progress**: {status['is_indexing']}\n"
        output += f"- **Files indexed**: {status['indexed_files']}/{status['total_files']}\n"
        output += f"- **Total symbols**: {status['total_symbols']}\n"

        if status['total_files'] > 0:
            progress = (status['indexed_files'] / status['total_files']) * 100
            output += f"- **Progress**: {progress:.1f}%\n"

        return output

    async def execute_list(self, arguments: dict[str, Any]) -> str:
        """Execute semantic_list tool."""
        list_type = arguments.get("list_type", "by_type")
        symbol_type = arguments.get("symbol_type")
        file_path = arguments.get("file_path")
        n_results = arguments.get("n_results", 100)
        # Allow argument to override config
        line_limit = arguments.get("line_limit", self.tool_config.limits.line_limit)

        output_parts = []

        # Add warning if still indexing
        if self.db.is_indexing:
            status = self.db.indexing_status
            output_parts.append(
                f"⚠️ **Warning**: Indexing is still in progress "
                f"({status['indexed_files']}/{status['total_files']} files indexed).\n"
                f"Results may be incomplete.\n\n"
            )

        if list_type == "by_type":
            if not symbol_type:
                return "Error: symbol_type is required when list_type is 'by_type'"

            symbols = self.db.list_symbols_by_type(symbol_type, n_results=n_results)

            if symbols:
                output_parts.append(f"## {symbol_type.title()}s ({len(symbols)})\n")
                for sym in symbols:
                    content = sym['content']
                    truncated = False
                    
                    # Apply line limit if configured
                    if line_limit > 0:
                        lines = content.split('\n')
                        if len(lines) > line_limit:
                            content = '\n'.join(lines[:line_limit])
                            truncated = True
                    
                    output_parts.append(
                        f"### {sym['name']}\n"
                        f"File: {sym['file_path']}\n"
                        f"Lines: {sym['start_line']}-{sym['end_line']}\n"
                    )
                    if sym.get('decorators'):
                        output_parts.append(f"Decorators: {', '.join(sym['decorators'])}\n")
                    if sym.get('parameters'):
                        output_parts.append(f"Parameters: {', '.join(sym['parameters'])}\n")
                    if sym.get('pagerank_score'):
                        output_parts.append(f"PageRank Score: {sym['pagerank_score']:.6f}\n")
                    output_parts.append(f"```\n{content}\n```")
                    if truncated:
                        output_parts.append(f"\n⚠️ **Note**: Content truncated to first {line_limit} lines\n")
                    output_parts.append("\n")
            else:
                output_parts.append(f"No {symbol_type}s found.\n")

        elif list_type == "file":
            if not file_path:
                return "Error: file_path is required when list_type is 'file'"

            symbols = self.db.get_file_symbols(file_path)

            if symbols:
                output_parts.append(f"## Symbols in {file_path} ({len(symbols)})\n")
                for sym in symbols:
                    content = sym['content']
                    truncated = False
                    
                    # Apply line limit if configured
                    if line_limit > 0:
                        lines = content.split('\n')
                        if len(lines) > line_limit:
                            content = '\n'.join(lines[:line_limit])
                            truncated = True
                    
                    indent = "  " if sym.get('parent') else ""
                    output_parts.append(
                        f"{indent}### {sym['name']} ({sym['symbol_type']})\n"
                        f"{indent}Lines: {sym['start_line']}-{sym['end_line']}\n"
                    )
                    if sym.get('parameters'):
                        output_parts.append(f"{indent}Parameters: {', '.join(sym['parameters'])}\n")
                    if sym.get('pagerank_score'):
                        output_parts.append(f"{indent}PageRank Score: {sym['pagerank_score']:.6f}\n")
                    output_parts.append(f"{indent}```\n{content}\n```")
                    if truncated:
                        output_parts.append(f"\n{indent}⚠️ **Note**: Content truncated to first {line_limit} lines\n")
                    output_parts.append("\n")
            else:
                output_parts.append(f"No symbols found in {file_path}.\n")

        return "\n".join(output_parts)

    async def execute_hierarchy(self, arguments: dict[str, Any]) -> str:
        """Execute semantic_hierarchy tool."""
        hierarchy_type = arguments.get("hierarchy_type")
        symbol_name = arguments.get("symbol_name")
        # Allow argument to override config
        line_limit = arguments.get("line_limit", self.tool_config.limits.line_limit)

        if not hierarchy_type or not symbol_name:
            return "Error: hierarchy_type and symbol_name are required"

        output_parts = []

        # Add warning if still indexing
        if self.db.is_indexing:
            status = self.db.indexing_status
            output_parts.append(
                f"⚠️ **Warning**: Indexing is still in progress.\n"
                f"Results may be incomplete.\n\n"
            )

        if hierarchy_type == "callers":
            callers = self.db.get_callers(symbol_name)
            if callers:
                output_parts.append(f"## Callers of {symbol_name} ({len(callers)})\n")
                for call in callers:
                    content = call['call_content']
                    truncated = False
                    if line_limit > 0:
                        lines = content.split('\n')
                        if len(lines) > line_limit:
                            content = '\n'.join(lines[:line_limit])
                            truncated = True
                    output_parts.append(
                        f"### {call['caller_symbol']}\n"
                        f"File: {call['caller_file']}\n"
                        f"Line: {call['call_line']}\n"
                        f"```\n{content}\n```"
                    )
                    if truncated:
                        output_parts.append(f"\n⚠️ **Note**: Content truncated to first {line_limit} lines\n")
                    output_parts.append("\n")
            else:
                output_parts.append(f"No callers found for {symbol_name}.\n")

        elif hierarchy_type == "callees":
            callees = self.db.get_callees(symbol_name)
            if callees:
                output_parts.append(f"## Functions called by {symbol_name} ({len(callees)})\n")
                for call in callees:
                    content = call['call_content']
                    truncated = False
                    if line_limit > 0:
                        lines = content.split('\n')
                        if len(lines) > line_limit:
                            content = '\n'.join(lines[:line_limit])
                            truncated = True
                    output_parts.append(
                        f"### {call['callee_name']}\n"
                        f"Line: {call['call_line']}\n"
                        f"```\n{content}\n```"
                    )
                    if truncated:
                        output_parts.append(f"\n⚠️ **Note**: Content truncated to first {line_limit} lines\n")
                    output_parts.append("\n")
            else:
                output_parts.append(f"No function calls found in {symbol_name}.\n")

        elif hierarchy_type == "class":
            hierarchy = self.db.get_class_hierarchy(symbol_name)
            if hierarchy:
                output_parts.append(f"## Class Hierarchy: {symbol_name}\n")
                output_parts.append(f"File: {hierarchy.get('file_path', 'unknown')}\n\n")
                
                if hierarchy.get('parents'):
                    output_parts.append(f"### Extends\n")
                    for parent in hierarchy['parents']:
                        output_parts.append(f"- {parent}\n")
                    output_parts.append("\n")
                
                if hierarchy.get('children'):
                    output_parts.append(f"### Extended by\n")
                    for child in hierarchy['children']:
                        output_parts.append(f"- {child['name']} ({child['file_path']})\n")
                
                # Get class members
                members = self.db.get_class_members(symbol_name)
                if members:
                    output_parts.append(f"\n### Members ({len(members)})\n")
                    for member in members:
                        output_parts.append(
                            f"- {member['name']} ({member['symbol_type']}) "
                            f"lines {member['start_line']}-{member['end_line']}\n"
                        )
            else:
                output_parts.append(f"Class {symbol_name} not found.\n")

        return "\n".join(output_parts)
