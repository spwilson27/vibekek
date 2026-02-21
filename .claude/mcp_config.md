# Claude Desktop — MCP Config for `example-rs-rag`

Add the snippet below to your Claude Desktop config file, then restart Claude Desktop.

**Config file location (macOS):**
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

**JSON to merge in:**

```json
{
  "mcpServers": {
    "example-rs-rag": {
      "command": "{workspace}/.rag/mcp_server_wrapper.sh",
      "args": [],
      "env": {}
    }
  }
}
```

> If you already have other `mcpServers` entries, just add the `"example-rs-rag"` key
> inside the existing `mcpServers` object — don't replace the whole file.

After restarting Claude Desktop the `query` and `retrieve` tools will appear
in the tools list and can be invoked by Claude directly.
