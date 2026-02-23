#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

def setup_claude_desktop():
    # macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
    # Windows: %APPDATA%/Claude/claude_desktop_config.json
    
    if sys.platform == "win32":
        config_dir = Path(os.environ.get("APPDATA", "")) / "Claude"
    elif sys.platform == "darwin":
        config_dir = Path.home() / "Library" / "Application Support" / "Claude"
    else:
        # Linux / other
        print("Claude Desktop is mostly for Mac/Windows, but trying ~/.config/Claude")
        config_dir = Path.home() / ".config" / "Claude"
        
    config_file = config_dir / "claude_desktop_config.json"
    
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"Failed to create directory {config_dir}: {e}")
        return

    # Load existing config if it exists
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
        except Exception:
            # File might be empty or invalid json
            config = {}
    else:
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}
        
    project_root = Path(__file__).parent.parent.resolve()
    wrapper_script = project_root / ".tools" / "mcp_server_wrapper.py"
    
    # We use absolute paths for the global Claude config
    config["mcpServers"]["vibekek-rag"] = {
        "command": "python3",
        "args": [str(wrapper_script), "rag"],
        "env": {}
    }
    config["mcpServers"]["vibekek-mapper"] = {
        "command": "python3",
        "args": [str(wrapper_script), "mapper"],
        "env": {}
    }
    
    try:
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
        print(f"✅ Successfully updated Claude Desktop config at: {config_file}")
        print("   Please restart Claude Desktop for the changes to take effect.")
    except Exception as e:
        print(f"❌ Failed to write config: {e}")

def main():
    print("Setting up MCP configurations for global tools...")
    setup_claude_desktop()
    print("\nProject-local tools (Cursor, VSCode / Copilot, Gemini) have been configured in their respective directories.")

if __name__ == "__main__":
    main()
