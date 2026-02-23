# AI Agents & Setup

Welcome! This project uses the Model Context Protocol (MCP) to provide context-aware capabilities to AI IDEs and CLI agents.

If you are using **Cursor**, **VS Code (Copilot)**, or **Gemini CLI**, the project is already pre-configured! Simply open the project in your IDE.

If you are using **Claude Desktop**, run the following setup script to automatically register the local MCP servers globally:
```bash
python3 .tools/setup_mcp.py
```

### Verifying MCP Servers

To ensure the MCP servers are working correctly, you can run the following checks depending on your preferred AI tool:

- **Claude Desktop / CLI**: Open a chat and ask Claude, *"What tools do you have access to?"* It should list `query`, `retrieve`, and `reindex`.
- **Gemini CLI**: Run `gemini mcp list` in your terminal to view active servers.
- **Cursor**: Open settings (gear icon) > **Features** > **MCP** to see the list of active servers and their status.
- **VS Code (Copilot)**: Look for the tool icon (usually a wrench or plug) in your Copilot Chat view, or search for "MCP" in the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`).

### Remote RAG Caching

To speed up AI indexing, this project is configured to automatically pull a pre-built RAG database from a GitLab CI artifact. If the database is missing or stale, the local MCP server will attempt to download it in the background rather than tying up your local CPU/GPU doing embeddings.

To enable this feature, you must configure a GitLab Personal Access Token (PAT) with at least the `read_api` scope:

1. Create a Personal Access Token in your GitLab settings.
2. Export it in your environment:
   ```bash
   export GITLAB_TOKEN="glpat-xxxxxxxxxx"
   ```
3. *(Optional)* Add this export to your `~/.zshrc` or `~/.bashrc` so it is always available to your CLI tools and IDEs.

You can also trigger this download manually:
```bash
python3 .tools/pull_rag_cache.py
```

**Note for AI Agents**: Please refer to [`docs/agents.md`](docs/agents.md) for detailed documentation on the available MCPs, how to use them, and how to extend the project's configurations further.