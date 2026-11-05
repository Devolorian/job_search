#!/bin/bash
# Launch xing5/mcp-google-sheets with a service account, so it works unattended with no
# OAuth prompt. Credentials come from ~/.job_search/sheets.env.
set -euo pipefail

ENV_FILE="$HOME/.job_search/sheets.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE — see README.md" >&2
  exit 1
fi
set -a; . "$ENV_FILE"; set +a

# mcp-google-sheets 1.29.x still imports mcp.server.fastmcp, which mcp 2.x renamed to
# MCPServer. Without this pin uvx resolves mcp 2.x and the server dies on import.
exec "$(command -v uvx || echo /opt/homebrew/bin/uvx)" --with "mcp<2" mcp-google-sheets@latest
