# Weaver Reloaded Templates

This directory contains template files for setting up new projects with the Weaver Reloaded workflow.

## Files

| File | Description |
|------|-------------|
| `Dockerfile` | Docker image template with non-root user support for AI CLI tools |
| `.workflow.jsonc` | Workflow configuration template with documented options |
| `.agent/MEMORY.md` | Agent memory template for persistent observations |
| `input/` | Project context files directory template |
| `tests/` | Test templates |
| `.agent/harness_hooks.py` | Agent-customisable setup hooks (called by harness) |

## Docker Setup

The included `Dockerfile` provides a complete development environment with:

- AI CLI tools (Claude, Codex, Gemini, Qwen Code)
- Rust toolchain (1.82.0)
- Python 3 with common packages
- Node.js LTS
- Build tools and system dependencies

### Non-Root User

AI CLI tools require a non-root user for their less-restricted execution modes. The Dockerfile uses build arguments to configure this:

```bash
# Build with defaults (user: username, UID: 1000)
docker build -t weaver-reloaded:dev .

# Custom username and UID
docker build \
  --build-arg USERNAME=myuser \
  --build-arg USER_UID=1001 \
  -t myimage:dev .
```

### Configuring .workflow.jsonc

To enable Docker-based execution, add a `docker` section to your `.workflow.jsonc`:

```jsonc
{
  "docker": {
    "image": "weaver-reloaded:dev",
    "copy_files": [
      { "src": "/home/<your-user>/.claude.json", "dest": "/home/username/.claude.json" },
      { "src": "/home/<your-user>/.codex/config.toml", "dest": "/home/username/.codex/config.toml" },
      { "src": "/home/<your-user>/.gemini/oauth_creds.json", "dest": "/home/username/.gemini/oauth_creds.json" },
      { "src": "/home/<your-user>/.gitconfig", "dest": "/home/username/.gitconfig" },
      { "src": "/home/<your-user>/.git-credentials", "dest": "/home/username/.git-credentials" }
    ]
  }
}
```

**Important:** The destination paths must match the `USERNAME` build argument used when building the image (default: `/home/username/...`).

### Per-Agent Docker Overrides

Individual agents can override or extend the global Docker config:

```jsonc
{
  "docker": {
    "image": "weaver-reloaded:dev",
    "copy_files": [
      { "src": "/home/mrwilson/.gitconfig", "dest": "/home/weaver/.gitconfig" }
    ]
  },
  "agents": [
    {
      "name": "gemini-sub",
      "backend": "gemini",
      "user": "sub",
      "docker": {
        "image": "weaver-reloaded:dev",
        "copy_files": [
          { "src": "/home/sub/.gemini/oauth_creds.json", "dest": "/home/weaver/.gemini/oauth_creds.json" },
          { "src": "/home/sub/.codex/config.toml", "dest": "/home/weaver/.codex/config.toml" }
        ]
      }
    }
  ]
}
```

Agent-level `copy_files` merge with the global config (agent values win for conflicting destinations).

## Using Templates for a New Project

1. Copy template files to your project root:
   ```bash
   cp .tools/templates/Dockerfile .
   cp .tools/templates/.workflow.jsonc .
   cp -r .tools/templates/.agent .
   ```

2. Customize `.workflow.jsonc` with your project settings

3. Build the Docker image:
   ```bash
   docker build -t <your-image>:<tag> .
   ```

4. Update `.workflow.jsonc` `docker.copy_files` paths to match your system
