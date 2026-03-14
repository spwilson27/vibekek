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


def setup_parser() -> argparse.ArgumentParser:
    """Create argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="rag-mcp-cli",
        description="CLI interface for testing MCP code search tools",
    )
    
    parser.add_argument("-c", "--config", type=str, default=None)
    parser.add_argument("-r", "--repo", type=str, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    
    subparsers = parser.add_subparsers(dest="command")
    
    # RAG
    rag = subparsers.add_parser("rag", help="Search code using RAG")
    rag.add_argument("query", type=str)
    rag.add_argument("-n", "--n-results", type=int, default=10)
    rag.set_defaults(func=_cmd_rag)
    
    # Semantic
    sem = subparsers.add_parser("semantic", help="Search symbols by name")
    sem.add_argument("query", type=str)
    sem.add_argument("-t", "--search-type", choices=["definition", "references", "all"], default="all")
    sem.add_argument("-s", "--symbol-type", choices=["function", "class", "method", "variable", "interface", "type"])
    sem.set_defaults(func=_cmd_semantic)
    
    # List
    lst = subparsers.add_parser("list", help="List symbols")
    lst.add_argument("--by-type", choices=["function", "class", "method", "variable", "interface", "type"])
    lst.add_argument("--file", type=str)
    lst.add_argument("-n", "--n-results", type=int, default=100)
    lst.set_defaults(func=_cmd_list)
    
    # Hierarchy
    hier = subparsers.add_parser("hierarchy", help="Get call/class hierarchy")
    hier.add_argument("--callers", type=str)
    hier.add_argument("--callees", type=str)
    hier.add_argument("--class", dest="class_name", type=str)
    hier.set_defaults(func=_cmd_hierarchy)
    
    # Status
    stat = subparsers.add_parser("status", help="Show indexing status")
    stat.set_defaults(func=_cmd_status)
    
    # Interactive
    inter = subparsers.add_parser("interactive", help="Interactive REPL")
    inter.set_defaults(func=_cmd_interactive)
    
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
    print(asyncio.run(rag.execute({"query": args.query, "n_results": args.n_results})))
    return 0


def _cmd_semantic(args, config):
    import asyncio
    _, sem = _get_tools(args, config)
    if not sem:
        print("Error: Semantic not enabled")
        return 1
    if not args.no_wait:
        _wait(None, sem, args.verbose)
    print(asyncio.run(sem.execute({
        "query": args.query,
        "search_type": args.search_type,
        "symbol_type": args.symbol_type,
    })))
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
    
    if args.by_type:
        print(asyncio.run(sem.execute_list({
            "list_type": "by_type",
            "symbol_type": args.by_type,
            "n_results": args.n_results,
        })))
    else:
        print(asyncio.run(sem.execute_list({
            "list_type": "file",
            "file_path": args.file,
            "n_results": args.n_results,
        })))
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
    
    if args.callers:
        print(asyncio.run(sem.execute_hierarchy({"hierarchy_type": "callers", "symbol_name": args.callers})))
    elif args.callees:
        print(asyncio.run(sem.execute_hierarchy({"hierarchy_type": "callees", "symbol_name": args.callees})))
    else:
        print(asyncio.run(sem.execute_hierarchy({"hierarchy_type": "class", "symbol_name": args.class_name})))
    return 0


def _cmd_status(args, config):
    rag, sem = _get_tools(args, config)
    print("## MCP Tools Status\n")
    if rag:
        s = rag.vector_store.indexing_status
        print(f"### RAG\n- Indexing: {'Yes' if s['is_indexing'] else 'No'}\n- Files: {s['indexed_files']}/{s['total_files']}\n- Chunks: {s['total_chunks']}\n")
    if sem:
        s = sem.db.indexing_status
        print(f"### Semantic\n- Indexing: {'Yes' if s['is_indexing'] else 'No'}\n- Files: {s['indexed_files']}/{s['total_files']}\n- Symbols: {s['total_symbols']}\n")
    return 0


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
        for c in ["config.jsonc", "config.json"]:
            if os.path.exists(c):
                cfg_path = c
                break
    
    config = load_config(cfg_path) if cfg_path else MCPConfig()
    if args.repo:
        config.repo_path = args.repo
    
    return args.func(args, config) if hasattr(args, "func") else 1


if __name__ == "__main__":
    sys.exit(main())
