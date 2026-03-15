#!/usr/bin/env python3
"""CLI interface for testing MCP tools interactively."""

# MUST be set before ANY other imports to suppress Hugging Face warnings
import os
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import argparse
import sys
import time
from pathlib import Path


# PID file location - stored in project directory (not cwd)
PID_FILE = ".rag-mcp-server.pid"
LOG_FILE = ".rag-mcp-server.log"


def _get_pid_file_path(config) -> Path:
    """Get path to PID file in project directory."""
    repo_path = Path(config.get_repo_path()).resolve()
    return repo_path / PID_FILE


def _get_log_file_path(config) -> Path:
    """Get path to log file in project directory."""
    repo_path = Path(config.get_repo_path()).resolve()
    return repo_path / LOG_FILE


def _is_server_running(config) -> bool:
    """Check if server is already running for this project."""
    pid_file = _get_pid_file_path(config)
    if not pid_file.exists():
        return False
    
    try:
        pid = int(pid_file.read_text().strip())
        # Check if process exists
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        # PID file exists but process is not running, clean up
        pid_file.unlink(missing_ok=True)
        return False


def _get_server_pid(config) -> int | None:
    """Get server PID if running, None otherwise."""
    pid_file = _get_pid_file_path(config)
    if not pid_file.exists():
        return None
    
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pid_file.unlink(missing_ok=True)
        return None


def _write_pid_file(pid: int, config):
    """Write PID to file in project directory."""
    pid_file = _get_pid_file_path(config)
    pid_file.write_text(str(pid))


def _remove_pid_file(config):
    """Remove PID file from project directory."""
    pid_file = _get_pid_file_path(config)
    pid_file.unlink(missing_ok=True)


def setup_parser() -> argparse.ArgumentParser:
    """Create argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="rag-mcp-cli",
        description="""MCP Tools CLI - Code Search for AI Agents

A Model Context Protocol (MCP) server providing AI assistants with powerful code search capabilities:

  • RAG Search     - Natural language semantic search using embeddings
  • Semantic Search - Find code definitions/references using tree-sitter AST parsing

QUICK START FOR AGENTS:
  This CLI serves as both a testing tool and an MCP server interface.
  When you need to search or analyze code, use these tools:

  1. rag <query>        - Search code using natural language (best for "how does X work?")
  2. semantic <name>    - Find exact symbol definitions/references (best for "where is X defined?")
  3. list --by-type X   - List all symbols of a type (functions, classes, etc.)
  4. hierarchy --callers X   - Find what calls function X
  5. hierarchy --callees X   - Find what function X calls
  6. status             - Check indexing progress

EXAMPLE WORKFLOWS:
  • Understand a feature:  rag "how is authentication implemented"
  • Find a class:          semantic UserService --symbol-type class
  • Find references:       semantic UserService --search-type references
  • Explore codebase:      list --by-type class
  • Trace call chains:     hierarchy --callers main

GLOBAL OPTIONS:
  -c, --config    Path to .rag-config.jsonc (default: config.jsonc or config.json in current dir)
  -r, --repo      Path to git repository (default: current directory)
  -v, --verbose   Show detailed progress during indexing
  --no-wait       Don't wait for indexing to complete (results may be incomplete)
  -L, --line-limit  Maximum lines to display per result (0 = no limit)
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("-c", "--config", type=str, default=None,
                        help="Path to config file (default: config.jsonc or config.json)")
    parser.add_argument("-r", "--repo", type=str, default=None,
                        help="Path to git repository (default: current directory)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show detailed progress during indexing")
    parser.add_argument("--no-wait", action="store_true",
                        help="Don't wait for indexing to complete")
    parser.add_argument("-L", "--line-limit", type=int, default=None,
                        help="Maximum lines to display per result (0 = no limit)")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # RAG
    rag = subparsers.add_parser(
        "rag",
        help="Search code using natural language (RAG)",
        description="""RAG (Retrieval-Augmented Generation) Search

Use natural language queries to search code semantically. Best for:
  • Understanding how features work ("how is authentication implemented")
  • Finding related code patterns ("database connection pooling")
  • Exploring unfamiliar codebases ("error handling patterns")
  • High-level conceptual searches ("API rate limiting")

Unlike semantic search, RAG understands intent and context, not just symbol names.

EXAMPLES:
  rag-mcp-cli rag "how is user authentication implemented"
  rag-mcp-cli rag "database connection pooling" -n 20
  rag-mcp-cli rag "error handling middleware" -L 50
  rag-mcp-cli rag "API rate limiting" --verbose

TIPS:
  • Use descriptive, natural language queries
  • Increase -n for broader searches (default: 10 results)
  • Use -L to control output verbosity
  • Works best with well-documented codebases
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rag.add_argument("query", type=str,
                     help="Natural language search query (e.g., 'how is authentication implemented')")
    rag.add_argument("-n", "--n-results", type=int, default=10,
                     help="Number of results to return (default: 10)")
    rag.add_argument("-L", "--line-limit", type=int, default=None,
                     help="Maximum lines to display per result (0 = no limit)")
    rag.set_defaults(func=_cmd_rag)

    # Semantic
    sem = subparsers.add_parser(
        "semantic",
        help="Search symbols by name (definitions, references)",
        description="""Semantic Code Search

Find exact code symbols (functions, classes, methods, variables) using tree-sitter AST parsing.
Best for:
  • Finding where a symbol is defined
  • Finding all references to a symbol
  • Locating specific functions or classes
  • Understanding symbol usage across the codebase

Unlike RAG, semantic search matches exact symbol names, not concepts.

SEARCH TYPES:
  definition   - Find where the symbol is defined
  references   - Find all places the symbol is used/referenced
  all          - Show both definitions and references (default)

SYMBOL TYPES:
  function, class, method, variable, interface, type

EXAMPLES:
  # Find a class definition
  rag-mcp-cli semantic UserService --symbol-type class

  # Find all references to a function
  rag-mcp-cli semantic process_payment --search-type references

  # Find a specific method
  rag-mcp-cli semantic "UserService.authenticate" --symbol-type method

  # Search with limited output
  rag-mcp-cli semantic main -L 30

TIPS:
  • Use --symbol-type to narrow results
  • Use --search-type references to find usage
  • Works with Python, JavaScript, TypeScript
  • Symbol names must be exact (no fuzzy matching)
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sem.add_argument("query", type=str,
                     help="Symbol name to search for (e.g., 'UserService', 'process_payment')")
    sem.add_argument("-t", "--search-type", choices=["definition", "references", "all"], default="all",
                     help="Search type: definition, references, or all (default: all)")
    sem.add_argument("-s", "--symbol-type", choices=["function", "class", "method", "variable", "interface", "type"],
                     help="Filter by symbol type (e.g., class, function, method)")
    sem.add_argument("-L", "--line-limit", type=int, default=None,
                     help="Maximum lines to display per result (0 = no limit)")
    sem.set_defaults(func=_cmd_semantic)

    # List
    lst = subparsers.add_parser(
        "list",
        help="List symbols by type or file",
        description="""List Symbols

Browse and discover symbols in the codebase by listing them by type or by file.
Best for:
  • Exploring codebase structure
  • Finding all classes/functions in a project
  • Understanding what's in a specific file
  • Discovering available APIs

LIST MODES:
  --by-type   List all symbols of a specific type (function, class, method, etc.)
  --file      List all symbols defined in a specific file

EXAMPLES:
  # List all classes in the project
  rag-mcp-cli list --by-type class

  # List all functions
  rag-mcp-cli list --by-type function -n 50

  # List symbols in a specific file
  rag-mcp-cli list --file src/auth.py

  # List with limited output
  rag-mcp-cli list --by-type class -n 20 -L 10

TIPS:
  • Use -n to control number of results (default: 100)
  • Combine with -L for concise output
  • Great for exploring unfamiliar codebases
  • File paths should be relative to repo root
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    lst.add_argument("--by-type", choices=["function", "class", "method", "variable", "interface", "type"],
                     help="List all symbols of this type")
    lst.add_argument("--file", type=str,
                     help="List all symbols in this file (path relative to repo root)")
    lst.add_argument("-n", "--n-results", type=int, default=100,
                     help="Maximum number of results (default: 100)")
    lst.add_argument("-L", "--line-limit", type=int, default=None,
                     help="Maximum lines to display per result (0 = no limit)")
    lst.set_defaults(func=_cmd_list)

    # Hierarchy
    hier = subparsers.add_parser(
        "hierarchy",
        help="Get call hierarchy (callers/callees) or class inheritance",
        description="""Call/Class Hierarchy Analysis

Understand code flow and relationships by analyzing:
  • Callers: What functions call this function?
  • Callees: What functions does this function call?
  • Class: Class inheritance hierarchy

Best for:
  • Understanding execution flow
  • Tracing bugs through call chains
  • Finding entry points to a function
  • Understanding dependencies
  • Analyzing class inheritance

HIERARCHY TYPES:
  --callers   Find all functions that call the specified function
  --callees   Find all functions called by the specified function
  --class     Show class inheritance (parent/child classes)

EXAMPLES:
  # What calls the main function?
  rag-mcp-cli hierarchy --callers main

  # What does process_payment call?
  rag-mcp-cli hierarchy --callees process_payment

  # What classes inherit from BaseHandler?
  rag-mcp-cli hierarchy --class BaseHandler

  # Trace a call chain (run multiple times)
  rag-mcp-cli hierarchy --callers api_handler
  rag-mcp-cli hierarchy --callers <result_from_previous>

TIPS:
  • Start from entry points and work backwards with --callers
  • Use --callees to understand what a function depends on
  • Combine with semantic search to find symbol names first
  • Great for debugging and code review
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hier.add_argument("--callers", type=str, metavar="FUNCTION",
                      help="Find all functions that call this function")
    hier.add_argument("--callees", type=str, metavar="FUNCTION",
                      help="Find all functions called by this function")
    hier.add_argument("--class", dest="class_name", type=str, metavar="CLASS",
                      help="Show class inheritance hierarchy")
    hier.add_argument("-L", "--line-limit", type=int, default=None,
                      help="Maximum lines to display per result (0 = no limit)")
    hier.set_defaults(func=_cmd_hierarchy)

    # Status
    stat = subparsers.add_parser(
        "status",
        help="Show indexing status (files indexed, symbols found)",
        description="""Indexing Status

Check the progress of background indexing for RAG and semantic search.
Shows:
  • Whether background server is running
  • Number of files indexed vs total
  • Number of chunks/symbols found
  • Whether indexing is still in progress

EXAMPLES:
  rag-mcp-cli status
  rag-mcp-cli status --verbose

TIPS:
  • Run status before searching to ensure indexing is complete
  • Partial results available during indexing (with warnings)
  • Indexing runs in background threads on startup
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stat.set_defaults(func=_cmd_status)

    # Serve (start background server)
    srv = subparsers.add_parser(
        "serve",
        help="Start MCP server in background",
        description="""Start Background MCP Server

Start the MCP index server as a background process. The server:
  • Automatically indexes your git repository on startup
  • Runs incremental updates when files change
  • Exposes MCP tools to AI assistants
  • Stores indexes in /tmp/rag-<hash> and /tmp/semantic-<hash>

MODES:
  Default: Run in background, output to log file
  --daemon: Full daemon mode (detach from terminal)

EXAMPLES:
  # Start server (outputs to rag-mcp-server.log)
  rag-mcp-cli serve

  # Start as full daemon
  rag-mcp-cli serve --daemon

  # Start with specific config
  rag-mcp-cli serve -c /path/to/config.jsonc

TIPS:
  • Server auto-starts indexing on launch
  • Use 'rag-mcp-cli status' to check progress
  • Use 'rag-mcp-cli stop' to stop the server
  • Logs written to rag-mcp-server.log
  • PID stored in rag-mcp-server.pid
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    srv.add_argument("-d", "--daemon", action="store_true",
                     help="Run as daemon (fully detach from terminal)")
    srv.set_defaults(func=_cmd_serve)

    # Stop (stop background server)
    stp = subparsers.add_parser(
        "stop",
        help="Stop background MCP server",
        description="""Stop Background MCP Server

Gracefully stop the background MCP server process.
  • Sends SIGTERM for graceful shutdown
  • Falls back to SIGKILL if needed
  • Removes PID file automatically

EXAMPLES:
  rag-mcp-cli stop

TIPS:
  • Always stop server gracefully when done
  • Removes stale PID files automatically
  • Check 'rag-mcp-cli status' to verify stopped
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stp.set_defaults(func=_cmd_stop)

    # Interactive
    inter = subparsers.add_parser(
        "interactive",
        help="Interactive REPL for testing commands",
        description="""Interactive REPL Mode

Start an interactive shell for testing MCP tools without typing full commands.
Available commands in REPL:
  rag <query>          - Search with RAG
  semantic <name>      - Semantic search
  list type <t>        - List symbols by type
  list file <f>        - List symbols in file
  callers <fn>         - Find function callers
  callees <fn>         - Find function callees
  class <name>         - Show class hierarchy
  status               - Show indexing status
  help                 - Show quick help
  quit/exit/q          - Exit REPL

EXAMPLES:
  rag-mcp-cli interactive
  rag-mcp-cli interactive --no-wait

TIPS:
  • Great for rapid exploration
  • No need to type 'rag-mcp-cli' each time
  • Use tab completion if available
  • Type 'help' for quick reference
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    inter.set_defaults(func=_cmd_interactive)

    # Help (comprehensive usage guide)
    hlp = subparsers.add_parser(
        "help",
        help="Show comprehensive usage guide with examples",
        description="""Comprehensive Usage Guide

This command displays detailed usage examples and workflows for all MCP tools.
Use this as a reference when working with the code search tools.

EXAMPLES:
  rag-mcp-cli help
  rag-mcp-cli help rag        # Show help for specific command
  rag-mcp-cli help semantic

QUICK REFERENCE:

  SEARCH COMMANDS:
    rag <query>              Natural language search ("how does auth work?")
    semantic <name>          Symbol search (definitions, references)
    list --by-type <type>    List all symbols of a type
    list --file <path>       List symbols in a file
    hierarchy --callers X    What functions call X
    hierarchy --callees X    What functions does X call
    hierarchy --class X      Class inheritance

  SERVER COMMANDS:
    serve                    Start background MCP server
    stop                     Stop background server
    status                   Show indexing status

  OTHER:
    interactive              Start interactive REPL
    help                     Show this comprehensive guide

COMMON WORKFLOWS:

  1. UNDERSTAND A FEATURE:
     $ rag-mcp-cli rag "how is authentication implemented"
     $ rag-mcp-cli rag "password hashing and validation"

  2. FIND AND ANALYZE A CLASS:
     $ rag-mcp-cli semantic UserService --symbol-type class
     $ rag-mcp-cli hierarchy --callers UserService
     $ rag-mcp-cli list --file src/services/user.py

  3. TRACE A BUG:
     $ rag-mcp-cli semantic process_payment --search-type references
     $ rag-mcp-cli hierarchy --callers process_payment
     $ rag-mcp-cli hierarchy --callees validate_card

  4. EXPLORE A CODEBASE:
     $ rag-mcp-cli list --by-type class
     $ rag-mcp-cli list --by-type function -n 50
     $ rag-mcp-cli rag "API endpoints" -n 20

  5. CODE REVIEW PREP:
     $ rag-mcp-cli status
     $ rag-mcp-cli semantic main --search-type callers
     $ rag-mcp-cli hierarchy --class BaseController

TIPS FOR AGENTS:
  • Always check 'status' before searching to ensure indexing is complete (fallback to grep if needed)
  • Use 'rag' for conceptual queries, 'semantic' for exact symbol names
  • Combine commands: semantic to find, hierarchy to trace, list to explore
  • Use -L to limit output verbosity in large codebases
  • Use --verbose during indexing to show progress
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hlp.set_defaults(func=_cmd_help_guide)

    return parser


def _get_tools(args, config):
    """Lazy load tools."""
    from .config import ToolConfig
    from .tools.rag import RAGTool
    from .tools.semantic import SemanticTool
    
    repo = args.repo or config.get_repo_path()
    rag = semantic = None
    
    if config.is_tool_enabled("rag"):
        idx = config.get_index_dir("rag")
        tc = config.get_tool_config("rag") or ToolConfig()
        rag = RAGTool(repo, str(idx), tc)
    
    if config.is_tool_enabled("semantic"):
        idx = config.get_index_dir("semantic")
        tc = config.get_tool_config("semantic") or ToolConfig()
        semantic = SemanticTool(repo, str(idx), tc)
    
    return rag, semantic


def _wait(rag, semantic, verbose=False):
    """Wait for indexing."""
    import time
    if verbose:
        print("Waiting for indexing...")
    
    for _ in range(120):
        idx = False
        if rag and rag.vector_store.is_indexing:
            s = rag.vector_store.indexing_status
            if s["is_indexing"]:
                idx = True
                if verbose:
                    print(f"  RAG: {s['indexed_files']}/{s['total_files']}")
        if semantic and semantic.db.is_indexing:
            s = semantic.db.indexing_status
            if s["is_indexing"]:
                idx = True
                if verbose:
                    print(f"  Semantic: {s['indexed_files']}/{s['total_files']}")
        if not idx:
            break
        time.sleep(1)


def _cmd_rag(args, config):
    import asyncio
    from .tools.rag import RAGTool
    rag, _ = _get_tools(args, config)
    if not rag:
        print("Error: RAG not enabled")
        return 1
    if not args.no_wait:
        _wait(rag, None, args.verbose)
    result_args = {"query": args.query, "n_results": args.n_results}
    if args.line_limit is not None:
        result_args["line_limit"] = args.line_limit
    print(asyncio.run(rag.execute(result_args)))
    return 0


def _cmd_semantic(args, config):
    import asyncio
    _, sem = _get_tools(args, config)
    if not sem:
        print("Error: Semantic not enabled")
        return 1
    if not args.no_wait:
        _wait(None, sem, args.verbose)
    result_args = {
        "query": args.query,
        "search_type": args.search_type,
        "symbol_type": args.symbol_type,
    }
    if args.line_limit is not None:
        result_args["line_limit"] = args.line_limit
    print(asyncio.run(sem.execute(result_args)))
    return 0


def _cmd_list(args, config):
    import asyncio
    _, sem = _get_tools(args, config)
    if not sem:
        print("Error: Semantic not enabled")
        return 1
    if not args.by_type and not args.file:
        print("Error: Specify --by-type or --file")
        return 1
    if not args.no_wait:
        _wait(None, sem, args.verbose)
    
    result_args = {"n_results": args.n_results}
    if args.line_limit is not None:
        result_args["line_limit"] = args.line_limit
    
    if args.by_type:
        result_args["list_type"] = "by_type"
        result_args["symbol_type"] = args.by_type
        print(asyncio.run(sem.execute_list(result_args)))
    else:
        result_args["list_type"] = "file"
        result_args["file_path"] = args.file
        print(asyncio.run(sem.execute_list(result_args)))
    return 0


def _cmd_hierarchy(args, config):
    import asyncio
    _, sem = _get_tools(args, config)
    if not sem:
        print("Error: Semantic not enabled")
        return 1
    if not any([args.callers, args.callees, args.class_name]):
        print("Error: Specify --callers, --callees, or --class")
        return 1
    if not args.no_wait:
        _wait(None, sem, args.verbose)
    
    result_args = {}
    if args.line_limit is not None:
        result_args["line_limit"] = args.line_limit
    
    if args.callers:
        result_args["hierarchy_type"] = "callers"
        result_args["symbol_name"] = args.callers
        print(asyncio.run(sem.execute_hierarchy(result_args)))
    elif args.callees:
        result_args["hierarchy_type"] = "callees"
        result_args["symbol_name"] = args.callees
        print(asyncio.run(sem.execute_hierarchy(result_args)))
    else:
        result_args["hierarchy_type"] = "class"
        result_args["symbol_name"] = args.class_name
        print(asyncio.run(sem.execute_hierarchy(result_args)))
    return 0


def _format_size(size_bytes: int) -> str:
    """Format size in human-readable format (KB/MB/GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _get_directory_size(path):
    """Calculate total size of a directory in bytes."""
    from pathlib import Path
    
    total_size = 0
    path = Path(path)
    
    if not path.exists():
        return 0
    
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total_size += entry.stat().st_size
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass
    
    return total_size


def _get_index_status_quick(index_dir):
    """Get index status without loading full database (fast check)."""
    from pathlib import Path
    
    index_path = Path(index_dir)
    if not index_path.exists():
        return {
            "exists": False,
            "is_indexing": False,
            "indexed_files": 0,
            "total_files": 0,
            "total_chunks": 0,
            "total_symbols": 0,
            "status": "not_created",
            "status_message": "Will be created on first query",
            "index_size": 0,
            "index_size_formatted": "0 B",
        }
    
    # Check status file written by index server
    status_file = index_path / ".index_status"
    indexing_status = "unknown"
    last_update = 0
    
    if status_file.exists():
        try:
            content = status_file.read_text().strip().split("\n")
            indexing_status = content[0] if content else "unknown"
            if len(content) > 1:
                last_update = float(content[1])
        except Exception:
            pass
    
    # Count meta files for RAG (fast filesystem operation)
    meta_files = list(index_path.glob("*.meta"))
    chunk_count = len(meta_files)
    
    # Check for ChromaDB sqlite file
    chroma_exists = (index_path / "chroma.sqlite3").exists()
    
    # Check for semantic database
    semantic_db = index_path / "symbols.db"
    symbol_count = 0
    if semantic_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(semantic_db))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM symbols")
            symbol_count = cursor.fetchone()[0]
            conn.close()
        except Exception:
            pass
    
    # Calculate index size
    index_size = _get_directory_size(index_path)
    index_size_formatted = _format_size(index_size)
    
    # Determine status message
    if indexing_status == "indexing":
        status = "indexing"
        status_message = "🔄 Indexing in progress..."
    elif indexing_status == "stopped":
        status = "stopped"
        status_message = "⏸️ Index server stopped"
    elif chunk_count > 0 or symbol_count > 0:
        status = "ready"
        status_message = "✅ Ready for queries"
    elif chroma_exists:
        status = "ready"
        status_message = "✅ Ready for queries (empty index)"
    else:
        status = "not_initialized"
        status_message = "⏳ Will be created on first query"
    
    return {
        "exists": True,
        "is_indexing": indexing_status == "indexing",
        "indexed_files": len(set(m.read_text().strip() for m in meta_files if m.exists())),
        "total_files": 0,
        "total_chunks": chunk_count,
        "total_symbols": symbol_count,
        "chroma_exists": chroma_exists,
        "status": status,
        "status_message": status_message,
        "last_update": last_update,
        "index_size": index_size,
        "index_size_formatted": index_size_formatted,
    }


def _cmd_status(args, config):
    """Show indexing status - optimized for fast response."""
    from .config import ToolConfig
    
    print("## MCP Tools Status\n")
    
    # Show server status (fast - just checks PID file)
    if _is_server_running(config):
        pid = _get_server_pid(config)
        print(f"**Background server**: Running (PID: {pid})\n")
    else:
        print(f"**Background server**: Not running\n")
    
    repo = args.repo or config.get_repo_path()
    
    # Quick status check for RAG (no model loading)
    if config.is_tool_enabled("rag"):
        idx_dir = config.get_index_dir("rag")
        status = _get_index_status_quick(idx_dir)
        
        if status["exists"]:
            print(f"### RAG")
            print(f"- Index: Initialized")
            print(f"- Size: {status['index_size_formatted']}")
            print(f"- Files indexed: {status['indexed_files']}")
            print(f"- Chunks: {status['total_chunks']}")
            print(f"- Status: {status['status_message']}")
        else:
            print(f"### RAG")
            print(f"- Index: Not created yet")
            print(f"- Status: {status['status_message']}")
        print()
    
    # Quick status check for Semantic (no parser loading)
    if config.is_tool_enabled("semantic"):
        idx_dir = config.get_index_dir("semantic")
        status = _get_index_status_quick(idx_dir)
        
        if status["exists"]:
            print(f"### Semantic")
            print(f"- Index: Initialized")
            print(f"- Size: {status['index_size_formatted']}")
            print(f"- Symbols: {status['total_symbols']}")
            print(f"- Status: {status['status_message']}")
        else:
            print(f"### Semantic")
            print(f"- Index: Not created yet")
            print(f"- Status: {status['status_message']}")
        print()
    
    return 0


def _cmd_help_guide(args, config):
    """Show comprehensive help guide."""
    print("""
================================================================================
                        MCP TOOLS CLI - COMPREHENSIVE GUIDE
================================================================================

A Model Context Protocol (MCP) server providing AI assistants with code search:

  * RAG Search      - Natural language semantic search using embeddings
  * Semantic Search - Find code definitions/references using tree-sitter

================================================================================
                              QUICK START
================================================================================

COMMANDS AT A GLANCE:

  Search Commands:
    rag <query>              Natural language search ("how does auth work?")
    semantic <name>          Symbol search (definitions, references)
    list --by-type <type>    List all symbols of a type
    list --file <path>       List symbols in a file
    hierarchy --callers X    What functions call X
    hierarchy --callees X    What functions does X call
    hierarchy --class X      Class inheritance

  Server Commands:
    serve                    Start background MCP server
    stop                     Stop background server
    status                   Show indexing status

  Other:
    interactive              Start interactive REPL
    help                     Show this guide

================================================================================
                           COMMON WORKFLOWS
================================================================================

1. UNDERSTAND A FEATURE:

   $ rag-mcp-cli rag "how is authentication implemented"
   $ rag-mcp-cli rag "password hashing and validation"

   Use RAG for conceptual questions about how features work.

2. FIND AND ANALYZE A CLASS:

   $ rag-mcp-cli semantic UserService --symbol-type class
   $ rag-mcp-cli hierarchy --callers UserService
   $ rag-mcp-cli list --file src/services/user.py

   Use semantic to find exact symbols, hierarchy to trace usage.

3. TRACE A BUG:

   $ rag-mcp-cli semantic process_payment --search-type references
   $ rag-mcp-cli hierarchy --callers process_payment
   $ rag-mcp-cli hierarchy --callees validate_card

   Follow the call chain to find where bugs originate.

4. EXPLORE A CODEBASE:

   $ rag-mcp-cli list --by-type class
   $ rag-mcp-cli list --by-type function -n 50
   $ rag-mcp-cli rag "API endpoints" -n 20

   List symbols to understand structure, RAG to find patterns.

5. CODE REVIEW PREP:

   $ rag-mcp-cli status
   $ rag-mcp-cli semantic main --search-type callers
   $ rag-mcp-cli hierarchy --class BaseController

   Check status first, then trace entry points and inheritance.

================================================================================
                           COMMAND REFERENCE
================================================================================

RAG SEARCH (Natural Language)
-----------------------------
Usage: rag-mcp-cli rag <query> [options]

Search code using natural language queries. Best for:
  * Understanding how features work
  * Finding related code patterns
  * Exploring unfamiliar codebases
  * High-level conceptual searches

Options:
  -n, --n-results NUM     Number of results (default: 10)
  -L, --line-limit NUM    Max lines per result (0 = no limit)

Examples:
  $ rag-mcp-cli rag "how is user authentication implemented"
  $ rag-mcp-cli rag "database connection pooling" -n 20
  $ rag-mcp-cli rag "error handling middleware" -L 50


SEMANTIC SEARCH (Symbol Names)
------------------------------
Usage: rag-mcp-cli semantic <name> [options]

Find exact code symbols using tree-sitter AST parsing. Best for:
  * Finding where a symbol is defined
  * Finding all references to a symbol
  * Locating specific functions or classes

Options:
  -t, --search-type TYPE    definition | references | all (default: all)
  -s, --symbol-type TYPE    function | class | method | variable | interface | type
  -L, --line-limit NUM      Max lines per result (0 = no limit)

Examples:
  $ rag-mcp-cli semantic UserService --symbol-type class
  $ rag-mcp-cli semantic process_payment --search-type references
  $ rag-mcp-cli semantic "UserService.authenticate" --symbol-type method


LIST SYMBOLS
------------
Usage: rag-mcp-cli list [options]

Browse and discover symbols by type or file.

Options:
  --by-type TYPE          List all symbols of this type
  --file PATH             List all symbols in this file
  -n, --n-results NUM     Maximum results (default: 100)
  -L, --line-limit NUM    Max lines per result

Examples:
  $ rag-mcp-cli list --by-type class
  $ rag-mcp-cli list --by-type function -n 50
  $ rag-mcp-cli list --file src/auth.py


HIERARCHY ANALYSIS
------------------
Usage: rag-mcp-cli hierarchy [options]

Understand code flow and relationships.

Options:
  --callers FUNCTION      Find all functions that call this function
  --callees FUNCTION      Find all functions called by this function
  --class CLASS           Show class inheritance hierarchy
  -L, --line-limit NUM    Max lines per result

Examples:
  $ rag-mcp-cli hierarchy --callers main
  $ rag-mcp-cli hierarchy --callees process_payment
  $ rag-mcp-cli hierarchy --class BaseHandler


STATUS
------
Usage: rag-mcp-cli status

Check indexing progress for RAG and semantic search.


SERVE (Start Server)
--------------------
Usage: rag-mcp-cli serve [options]

Start the MCP index server as a background process.

Options:
  -d, --daemon            Run as full daemon (detach from terminal)

Examples:
  $ rag-mcp-cli serve
  $ rag-mcp-cli serve --daemon


STOP (Stop Server)
------------------
Usage: rag-mcp-cli stop

Gracefully stop the background MCP server.


INTERACTIVE REPL
----------------
Usage: rag-mcp-cli interactive

Start an interactive shell. Commands:
  rag <query>          semantic <name>       list type <t>
  list file <f>        callers <fn>          callees <fn>
  class <name>         status                help
  quit/exit/q

================================================================================
                              TIPS FOR AGENTS
================================================================================

* Always check 'status' before searching to ensure indexing is complete
* Use 'rag' for conceptual queries, 'semantic' for exact symbol names
* Combine commands: semantic to find, hierarchy to trace, list to explore
* Use -L to limit output verbosity in large codebases
* Use --verbose during indexing to show progress
* Works with Python, JavaScript, and TypeScript codebases

================================================================================
""")
    return 0


def _cmd_serve(args, config):
    """Start MCP index server in background."""
    import subprocess
    
    if _is_server_running(config):
        pid = _get_server_pid(config)
        print(f"Server is already running (PID: {pid})")
        return 0
    
    # Get path to rag-mcp-index script (the entry point)
    import sys
    from pathlib import Path
    
    # Find the rag-mcp-index entry point script in the venv bin directory
    cli_script = Path(__file__).resolve()
    venv_bin = cli_script.parent
    server_entry = venv_bin / "rag-mcp-index"
    
    if not server_entry.exists():
        # Fallback to python -m
        server_entry = None
    
    # Get config path
    cfg_path = args.config
    if not cfg_path:
        for c in [".rag-config.jsonc", "config.jsonc", "config.json"]:
            if os.path.exists(c):
                cfg_path = c
                break
    
    if not cfg_path:
        print("Error: No config file found. Create config.json or config.jsonc in current directory.")
        return 1
    
    # Build command - use the entry point script or python -m
    if server_entry and server_entry.exists():
        cmd = [str(server_entry), cfg_path]
    else:
        # Fallback to python -m
        cmd = [sys.executable, "-m", "rag_mcp.index_server", cfg_path]
    
    # Get log file path
    log_file = _get_log_file_path(config)
    
    try:
        # Open log file for appending
        log_fd = open(log_file, "a")
        
        # Start server in background using nohup-like behavior
        if args.daemon:
            # Full daemon mode - detach from terminal
            proc = subprocess.Popen(
                cmd,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # Create new session (full daemon)
                cwd=os.getcwd(),
                preexec_fn=os.setpgrp if os.name != 'nt' else None,
            )
        else:
            # Background mode - output to log file
            proc = subprocess.Popen(
                cmd,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=os.getcwd(),
                preexec_fn=os.setpgrp if os.name != 'nt' else None,
            )
        
        # Write PID file
        _write_pid_file(proc.pid, config)
        log_fd.close()
        
        if args.daemon:
            print(f"Server started in daemon mode (PID: {proc.pid})")
        else:
            print(f"Server started (PID: {proc.pid})")
        print(f"Logs: {log_file}")
        print("Use 'rag-mcp-cli stop' to stop the server")
        
        # Wait a moment and check if server is still running
        time.sleep(1)
        
        # Check if process exited immediately
        if proc.poll() is not None:
            # Server exited immediately - read log to show error
            print(f"Error: Server failed to start. Check {log_file} for details.")
            if log_file.exists():
                print("--- Last 10 lines of log ---")
                print(log_file.read_text()[-2000:])
            _remove_pid_file(config)
            return 1
        
        # Verify process is still running
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            print(f"Error: Server process exited unexpectedly")
            _remove_pid_file(config)
            return 1
        
        return 0
        
    except Exception as e:
        print(f"Error starting server: {e}")
        _remove_pid_file(config)
        return 1


def _cmd_stop(args, config):
    """Stop background MCP server."""
    if not _is_server_running(config):
        print("Server is not running")
        _remove_pid_file(config)  # Clean up stale PID file
        return 0

    pid = _get_server_pid(config)

    try:
        import signal
        # Send SIGTERM to the process group for graceful shutdown
        # (server is started with start_new_session=True, so it's in its own group)
        try:
            os.killpg(pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError):
            # Fallback for systems without killpg or if process already exited
            os.kill(pid, signal.SIGTERM)

        # Wait for process to exit
        import time
        for _ in range(30):  # Wait up to 3 seconds
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                # Process has exited
                _remove_pid_file(config)
                print(f"Server stopped (PID: {pid})")
                return 0

        # Force kill the process group if still running
        try:
            os.killpg(pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        
        # Wait for process to exit
        time.sleep(0.5)
        _remove_pid_file(config)
        print(f"Server forcefully stopped (PID: {pid})")
        return 0

    except ProcessLookupError:
        _remove_pid_file(config)
        print("Server was not running (stale PID file removed)")
        return 0
    except PermissionError:
        print(f"Error: Permission denied when trying to stop server (PID: {pid})")
        return 1
    except Exception as e:
        print(f"Error stopping server: {e}")
        return 1


def _cmd_interactive(args, config):
    import asyncio
    print("MCP Tools Interactive Mode")
    print("=" * 40)
    print("Commands: rag, semantic, list, hierarchy, status, help, quit")
    print("=" * 40)
    
    rag, sem = _get_tools(args, config)
    if not args.no_wait:
        _wait(rag, sem, args.verbose)
    
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            break
        if line == "help":
            print("Commands: rag <q>, semantic <name>, list type <t>|file <f>, callers <fn>, callees <fn>, class <name>, status")
            continue
        if line == "status":
            _cmd_status(args, config)
            continue
        
        parts = line.split(maxsplit=2)
        cmd = parts[0]
        
        if cmd == "rag" and len(parts) >= 2:
            q = parts[1] if len(parts) == 2 else parts[1] + " " + parts[2]
            if rag:
                print(asyncio.run(rag.execute({"query": q, "n_results": 10})))
        elif cmd == "semantic" and len(parts) >= 2:
            if sem:
                print(asyncio.run(sem.execute({"query": parts[1], "search_type": "all"})))
        elif cmd == "list" and len(parts) >= 3:
            if sem:
                if parts[1] == "type":
                    print(asyncio.run(sem.execute_list({"list_type": "by_type", "symbol_type": parts[2], "n_results": 100})))
                elif parts[1] == "file":
                    print(asyncio.run(sem.execute_list({"list_type": "file", "file_path": parts[2], "n_results": 100})))
        elif cmd == "callers" and len(parts) >= 2:
            if sem:
                print(asyncio.run(sem.execute_hierarchy({"hierarchy_type": "callers", "symbol_name": parts[1]})))
        elif cmd == "callees" and len(parts) >= 2:
            if sem:
                print(asyncio.run(sem.execute_hierarchy({"hierarchy_type": "callees", "symbol_name": parts[1]})))
        elif cmd == "class" and len(parts) >= 2:
            if sem:
                print(asyncio.run(sem.execute_hierarchy({"hierarchy_type": "class", "symbol_name": parts[1]})))
        else:
            print(f"Unknown: {cmd}")
    return 0


def main():
    """Main entry point."""
    parser = setup_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    from .config import MCPConfig, load_config
    
    cfg_path = args.config
    if not cfg_path:
        for c in [".rag-config.jsonc", "config.jsonc", "config.json"]:
            if os.path.exists(c):
                cfg_path = c
                break
    
    config = load_config(cfg_path) if cfg_path else MCPConfig()
    if args.repo:
        config.repo_path = args.repo
    
    return args.func(args, config) if hasattr(args, "func") else 1


if __name__ == "__main__":
    sys.exit(main())
