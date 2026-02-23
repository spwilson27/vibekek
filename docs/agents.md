# AI Agent Documentation

Welcome, AI Agent! This project has been structured to serve as an AI Agent development template. To help you navigate and enhance the project, several Model Context Protocol (MCP) servers are available.

## Available MCP Servers

1. **`rag`** (Retrieval-Augmented Generation):
   - Indexes the project for semantic search.
   - Use the `query` tool to ask questions about the codebase.
   - Use the `retrieve` tool to fetch raw chunks.
   - Use the `reindex` tool to update the index after making significant file changes.
   - **Configuration**: The files indexed by the RAG tool are governed by `.tools/config.json`.
     - **Progressive Adjustment**: As you (the AI agent) add new file types (e.g., `.ts`, `.rs`, `.html`) or new directories to the project, you **must** update the `"extensions"`, `"dirs"`, and `"skip_dirs"` in `.tools/config.json` to ensure the new files are picked up by the indexer.
     - After updating `config.json`, run the `reindex` tool to apply your changes.
   - **Caching**: AI Agents can optionally run `python3 .tools/pull_rag_cache.py` to download a pre-built remote RAG database (set by the `gitlab` settings in `config.json`) if it exists instead of re-generating it locally. Requires a `GITLAB_TOKEN` env var.
   - Config directory: `.tools/rag/`

2. **`mapper`** (Page-Rank Codebase Navigation):
   - Generates an automated repository map based on dependencies and page-rank algorithms.
   - Helps understand the project structure dynamically.
   - Config directory: `.tools/mapper/`

## Configuration Files

The project includes pre-configured MCP settings for various tools. When adding new files or projects, you may need to adjust these.

- **Gemini (`.gemini/settings.json`)**: Pre-configured for Gemini CLI/IDE.
- **Cursor (`.cursor/mcp.json`)**: Pre-configured for the Cursor IDE.
- **VS Code (`.vscode/mcp.json`)**: Pre-configured for GitHub Copilot in VS Code.
- **Claude Desktop (`scripts/setup_mcp.py`)**: A script is provided to register the servers globally for Claude Desktop since it does not support workspace-level configurations natively.

## Adding a New MCP Server

If you need to add a new MCP server (e.g., SQLite, explicit filesystem access, or external API integration):

1. **Add to `.tools/`**: Create a new directory under `.tools/` (e.g., `.tools/sqlite/`) containing your server script and `requirements.txt`.
2. **Update the Wrapper**: Modify `.tools/mcp_server_wrapper.py` if necessary to handle your new server target.
3. **Register Locally**: Add the server entry to `.cursor/mcp.json`, `.vscode/mcp.json`, and `.gemini/settings.json`.
4. **Register Globally**: Update `scripts/setup_mcp.py` to register the new server in the global Claude Desktop config.
5. **Document**: Update this `agents.md` file to explain the new server and its capabilities.

## Suggested Next MCPs

Depending on the direction of the project, consider suggesting or implementing:
- **`sqlite`**: If the project adopts a local database.
- **`github`**: To automate PR creation or review directly from the agent.
- **`puppeteer`/`playwright`**: For automated browser testing capabilities.

## Making Significant Changes

After making significant code changes, consider using the `reindex` tool provided by the `rag` server so that your context remains up to date for subsequent queries.
