# Available MCP Tools

1. **`rag`** (Retrieval-Augmented Generation):
   - **`query`**: Ask questions about the codebase.
   - **`retrieve`**: Fetch raw chunks.
   - **`reindex`**: Update the index after making significant file changes.
   - *Note*: If you add new file types or directories, update `.tools/config.json` to ensure they are indexed, then run `reindex`.

2. **`mapper`** (Page-Rank Codebase Navigation):
   - Generates an automated repository map based on dependencies and page-rank algorithms to help you understand the project structure dynamically.