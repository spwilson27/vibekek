# Claude Desktop — MCP Config for `example-rs-rag`

Add the snippet below to your Claude Desktop config file, then restart Claude Desktop.

Note: It only needs to be added once in order to continue to use this rag
server. (Since it uses local paths, as long as claude is launched from within the root project dir it will work.)

**Config file location (macOS):**
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

**JSON to merge in:**

```json
{
  "mcpServers": {
    "example-rs-rag": {
      "command": ".rag/mcp_server_wrapper.sh",
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
