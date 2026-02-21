#!/usr/bin/env bash
# .rag/mcp_server_wrapper.sh
#
# Zero-config entrypoint for the RAG MCP server.
# On first run (or after requirements.txt changes) it automatically:
#   1. Creates a .venv inside .rag/ if one doesn't exist
#   2. Installs / updates pip dependencies from requirements.txt
#   3. Launches mcp_server.py (which auto-rebuilds the index if source files changed)
#
# Usage: referenced directly in MCP config files — no manual steps needed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
REQ="$SCRIPT_DIR/requirements.txt"
STAMP="$SCRIPT_DIR/.venv/.install_stamp"

# ── 1. Create venv if missing ─────────────────────────────────────────────
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "[wrapper] Creating .venv…" >&2
    python3 -m venv "$VENV"
fi

PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"

# ── 2. Install deps if requirements.txt is newer than the stamp file ──────
if [[ ! -f "$STAMP" || "$REQ" -nt "$STAMP" ]]; then
    echo "[wrapper] Installing/updating dependencies from requirements.txt…" >&2
    "$PIP" install -q -r "$REQ"
    touch "$STAMP"
fi

# ── 3. Launch MCP server (auto-rebuilds index if source files are stale) ──
exec "$PYTHON" "$SCRIPT_DIR/mcp_server.py" "$@"
