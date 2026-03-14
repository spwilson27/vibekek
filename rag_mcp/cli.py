#!/usr/bin/env python3
"""CLI interface for testing MCP tools interactively."""

# MUST be set before ANY other imports to suppress Hugging Face warnings
import os
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .config import load_config
from .tools.rag import RAGTool
from .tools.semantic import SemanticTool


def setup_parser() -> argparse.ArgumentParser:
    """Create argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="rag-mcp-cli",
        description="CLI interface for testing MCP code search tools",
    )
    
    # Global options
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=None,
        help="Path to config file (default: config.jsonc)",
    )
    parser.add_argument(
        "-r", "--repo",
        type=str,
        default=None,
        help="Repository path (default: current directory)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Don't wait for indexing to complete",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # RAG search command
    rag_parser = subparsers.add_parser(
        "rag",
        help="Search code using natural language (RAG)",
        description="Search code using RAG embeddings",
    )
    rag_parser.add_argument(
        "query",
        type=str,
        help="Search query (natural language or code)",
    )
    rag_parser.add_argument(
        "-n", "--n-results",
        type=int,
        default=10,
        help="Number of results to return (default: 10)",
    )
    rag_parser.set_defaults(func=cmd_rag_search)
    
    # Semantic search command
    semantic_parser = subparsers.add_parser(
        "semantic",
        help="Search for code symbols by name",
        description="Find code definitions and references",
    )
    semantic_parser.add_argument(
        "query",
        type=str,
        help="Symbol name to search for",
    )
    semantic_parser.add_argument(
        "-t", "--search-type",
        type=str,
        choices=["definition", "references", "all"],
        default="all",
        help="Search type (default: all)",
    )
    semantic_parser.add_argument(
        "-s", "--symbol-type",
        type=str,
        choices=["function", "class", "method", "variable", "interface", "type"],
        default=None,
        help="Filter by symbol type",
    )
    semantic_parser.set_defaults(func=cmd_semantic_search)
    
    # Semantic list command
    list_parser = subparsers.add_parser(
        "list",
        help="List symbols by type or file",
        description="Browse symbols for discovery",
    )
    list_parser.add_argument(
        "--by-type",
        type=str,
        choices=["function", "class", "method", "variable", "interface", "type"],
        default=None,
        help="List all symbols of a type",
    )
    list_parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="List symbols in a specific file",
    )
    list_parser.add_argument(
        "-n", "--n-results",
        type=int,
        default=100,
        help="Maximum results (default: 100)",
    )
    list_parser.set_defaults(func=cmd_semantic_list)
    
    # Semantic hierarchy command
    hierarchy_parser = subparsers.add_parser(
        "hierarchy",
        help="Get call hierarchy or class inheritance",
        description="Explore code relationships",
    )
    hierarchy_parser.add_argument(
        "--callers",
        type=str,
        default=None,
        help="Find callers of a function",
    )
    hierarchy_parser.add_argument(
        "--callees",
        type=str,
        default=None,
        help="Find functions called by a symbol",
    )
    hierarchy_parser.add_argument(
        "--class",
        dest="class_name",
        type=str,
        default=None,
        help="Get class hierarchy and members",
    )
    hierarchy_parser.set_defaults(func=cmd_semantic_hierarchy)
    
    # Status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show indexing status for RAG and semantic tools",
        description="Check indexing progress",
    )
    status_parser.set_defaults(func=cmd_status)
    
    # Interactive command
    interactive_parser = subparsers.add_parser(
        "interactive",
        help="Start interactive REPL mode",
        description="Interactive mode for testing tools",
    )
    interactive_parser.set_defaults(func=cmd_interactive)
    
    return parser


def get_tools(args, config):
    """Initialize tools based on config."""
    repo_path = args.repo or config.get_repo_path()
    
    rag_tool = None
    semantic_tool = None
    
    if config.is_tool_enabled("rag"):
        index_dir = config.get_index_dir("rag")
        tool_config = config.get_tool_config("rag")
        rag_tool = RAGTool(repo_path, str(index_dir), tool_config or config.get_tool_config("rag"))
    
    if config.is_tool_enabled("semantic"):
        index_dir = config.get_index_dir("semantic")
        tool_config = config.get_tool_config("semantic")
        semantic_tool = SemanticTool(repo_path, str(index_dir), tool_config or config.get_tool_config("semantic"))
    
    return rag_tool, semantic_tool


def wait_for_indexing(rag_tool, semantic_tool, verbose=False):
    """Wait for background indexing to complete."""
    max_wait = 120  # 2 minutes max
    wait_interval = 1  # Check every second
    
    if verbose:
        print("Waiting for indexing to complete...")
    
    start_time = time.time()
    while time.time() - start_time < max_wait:
        indexing = False
        
        if rag_tool and rag_tool.vector_store.is_indexing:
            status = rag_tool.vector_store.indexing_status
            if status["is_indexing"]:
                indexing = True
                if verbose:
                    print(f"  RAG: {status['indexed_files']}/{status['total_files']} files, "
                          f"{status['total_chunks']} chunks")
        
        if semantic_tool and semantic_tool.db.is_indexing:
            status = semantic_tool.db.indexing_status
            if status["is_indexing"]:
                indexing = True
                if verbose:
                    print(f"  Semantic: {status['indexed_files']}/{status['total_files']} files, "
                          f"{status['total_symbols']} symbols")
        
        if not indexing:
            if verbose:
                print("Indexing complete!")
            return
        
        time.sleep(wait_interval)
    
    if verbose:
        print("Warning: Indexing timeout, proceeding with partial index")


def cmd_rag_search(args, config):
    """Execute RAG search."""
    rag_tool, _ = get_tools(args, config)
    
    if not rag_tool:
        print("Error: RAG tool is not enabled in config")
        return 1
    
    if not args.no_wait:
        wait_for_indexing(rag_tool, None, args.verbose)
    
    result = asyncio.run(rag_tool.execute({
        "query": args.query,
        "n_results": args.n_results,
    }))
    
    print(result)
    return 0


def cmd_semantic_search(args, config):
    """Execute semantic search."""
    _, semantic_tool = get_tools(args, config)
    
    if not semantic_tool:
        print("Error: Semantic tool is not enabled in config")
        return 1
    
    if not args.no_wait:
        wait_for_indexing(None, semantic_tool, args.verbose)
    
    result = asyncio.run(semantic_tool.execute({
        "query": args.query,
        "search_type": args.search_type,
        "symbol_type": args.symbol_type,
    }))
    
    print(result)
    return 0


def cmd_semantic_list(args, config):
    """Execute semantic list."""
    _, semantic_tool = get_tools(args, config)
    
    if not semantic_tool:
        print("Error: Semantic tool is not enabled in config")
        return 1
    
    if not args.by_type and not args.file:
        print("Error: Must specify --by-type or --file")
        return 1
    
    if not args.no_wait:
        wait_for_indexing(None, semantic_tool, args.verbose)
    
    if args.by_type:
        result = asyncio.run(semantic_tool.execute_list({
            "list_type": "by_type",
            "symbol_type": args.by_type,
            "n_results": args.n_results,
        }))
    else:
        result = asyncio.run(semantic_tool.execute_list({
            "list_type": "file",
            "file_path": args.file,
            "n_results": args.n_results,
        }))
    
    print(result)
    return 0


def cmd_semantic_hierarchy(args, config):
    """Execute semantic hierarchy."""
    _, semantic_tool = get_tools(args, config)
    
    if not semantic_tool:
        print("Error: Semantic tool is not enabled in config")
        return 1
    
    if not args.callers and not args.callees and not args.class_name:
        print("Error: Must specify --callers, --callees, or --class")
        return 1
    
    if not args.no_wait:
        wait_for_indexing(None, semantic_tool, args.verbose)
    
    if args.callers:
        result = asyncio.run(semantic_tool.execute_hierarchy({
            "hierarchy_type": "callers",
            "symbol_name": args.callers,
        }))
    elif args.callees:
        result = asyncio.run(semantic_tool.execute_hierarchy({
            "hierarchy_type": "callees",
            "symbol_name": args.callees,
        }))
    else:
        result = asyncio.run(semantic_tool.execute_hierarchy({
            "hierarchy_type": "class",
            "symbol_name": args.class_name,
        }))
    
    print(result)
    return 0


def cmd_status(args, config):
    """Show indexing status."""
    rag_tool, semantic_tool = get_tools(args, config)
    
    print("## MCP Tools Status\n")
    
    if rag_tool:
        status = rag_tool.vector_store.indexing_status
        print("### RAG")
        print(f"- Indexing: {'Yes' if status['is_indexing'] else 'No'}")
        print(f"- Files: {status['indexed_files']}/{status['total_files']}")
        print(f"- Chunks: {status['total_chunks']}")
        print()
    
    if semantic_tool:
        status = semantic_tool.db.indexing_status
        print("### Semantic")
        print(f"- Indexing: {'Yes' if status['is_indexing'] else 'No'}")
        print(f"- Files: {status['indexed_files']}/{status['total_files']}")
        print(f"- Symbols: {status['total_symbols']}")
        print()
    
    return 0


def cmd_interactive(args, config):
    """Start interactive REPL mode."""
    rag_tool, semantic_tool = get_tools(args, config)
    
    print("MCP Tools Interactive Mode")
    print("=" * 40)
    print("Commands:")
    print("  rag <query>           - RAG search")
    print("  semantic <name>       - Semantic search")
    print("  list type <type>      - List symbols by type")
    print("  list file <path>      - List symbols in file")
    print("  callers <function>    - Find callers")
    print("  callees <function>    - Find callees")
    print("  class <name>          - Class hierarchy")
    print("  status                - Show status")
    print("  help                  - Show this help")
    print("  quit/exit             - Exit")
    print("=" * 40)
    
    if not args.no_wait:
        wait_for_indexing(rag_tool, semantic_tool, args.verbose)
        print("\nIndexing complete!\n")
    
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        
        if not line:
            continue
        
        parts = line.split(maxsplit=2)
        cmd = parts[0].lower()
        
        if cmd in ("quit", "exit", "q"):
            break
        
        if cmd == "help":
            print("Commands:")
            print("  rag <query>           - RAG search")
            print("  semantic <name>       - Semantic search")
            print("  list type <type>      - List symbols by type")
            print("  list file <path>      - List symbols in file")
            print("  callers <function>    - Find callers")
            print("  callees <function>    - Find callees")
            print("  class <name>          - Class hierarchy")
            print("  status                - Show status")
            print("  quit/exit             - Exit")
            continue
        
        if cmd == "status":
            result = cmd_status(args, config)
            continue
        
        if cmd == "rag":
            if len(parts) < 2:
                print("Usage: rag <query>")
                continue
            query = parts[1] if len(parts) == 2 else parts[1] + " " + parts[2]
            if rag_tool:
                result = asyncio.run(rag_tool.execute({"query": query, "n_results": 10}))
                print(result)
            else:
                print("RAG tool not enabled")
            continue
        
        if cmd == "semantic":
            if len(parts) < 2:
                print("Usage: semantic <name>")
                continue
            query = parts[1]
            if semantic_tool:
                result = asyncio.run(semantic_tool.execute({
                    "query": query,
                    "search_type": "all",
                }))
                print(result)
            else:
                print("Semantic tool not enabled")
            continue
        
        if cmd == "list":
            if len(parts) < 3:
                print("Usage: list type <type> | list file <path>")
                continue
            list_cmd = parts[1].lower()
            list_arg = parts[2]
            
            if semantic_tool:
                if list_cmd == "type":
                    result = asyncio.run(semantic_tool.execute_list({
                        "list_type": "by_type",
                        "symbol_type": list_arg,
                        "n_results": 100,
                    }))
                elif list_cmd == "file":
                    result = asyncio.run(semantic_tool.execute_list({
                        "list_type": "file",
                        "file_path": list_arg,
                        "n_results": 100,
                    }))
                else:
                    print("Usage: list type <type> | list file <path>")
                    continue
                print(result)
            else:
                print("Semantic tool not enabled")
            continue
        
        if cmd == "callers":
            if len(parts) < 2:
                print("Usage: callers <function>")
                continue
            if semantic_tool:
                result = asyncio.run(semantic_tool.execute_hierarchy({
                    "hierarchy_type": "callers",
                    "symbol_name": parts[1],
                }))
                print(result)
            else:
                print("Semantic tool not enabled")
            continue
        
        if cmd == "callees":
            if len(parts) < 2:
                print("Usage: callees <function>")
                continue
            if semantic_tool:
                result = asyncio.run(semantic_tool.execute_hierarchy({
                    "hierarchy_type": "callees",
                    "symbol_name": parts[1],
                }))
                print(result)
            else:
                print("Semantic tool not enabled")
            continue
        
        if cmd == "class":
            if len(parts) < 2:
                print("Usage: class <name>")
                continue
            if semantic_tool:
                result = asyncio.run(semantic_tool.execute_hierarchy({
                    "hierarchy_type": "class",
                    "symbol_name": parts[1],
                }))
                print(result)
            else:
                print("Semantic tool not enabled")
            continue
        
        print(f"Unknown command: {cmd}")
        print("Type 'help' for available commands")
    
    return 0


def main():
    """Main entry point for CLI."""
    parser = setup_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Load configuration
    config_path = args.config
    if not config_path:
        # Try default config locations
        default_configs = ["config.jsonc", "config.json"]
        for cfg in default_configs:
            if Path(cfg).exists():
                config_path = cfg
                break
    
    if config_path:
        config = load_config(config_path)
    else:
        # Create minimal default config
        from .config import MCPConfig
        config = MCPConfig()
    
    # Override repo path if specified
    if args.repo:
        config.repo_path = args.repo
    
    # Execute command
    if hasattr(args, "func"):
        return args.func(args, config)
    
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
