#!/bin/bash
# Launch chigwell/telegram-mcp with credentials sourced from ~/.job_search/telegram.env.
#
# A wrapper rather than putting the values in .mcp.json: the session string is equivalent
# to full access to the candidate's Telegram account, so it must never sit in a file inside
# the project. This also works identically in an interactive session and under launchd,
# where the environment is otherwise empty.
set -euo pipefail

ENV_FILE="$HOME/.job_search/telegram.env"
# A checkout of https://github.com/chigwell/telegram-mcp.
SERVER_DIR="${TELEGRAM_MCP_DIR:-$HOME/tools/telegram-mcp}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE — see README.md" >&2
  exit 1
fi
set -a; . "$ENV_FILE"; set +a

# Read-only: send/delete/edit tools are not registered at all, so no prompt or mistake can
# post from the real account. See CLAUDE.md rule 1.
export TELEGRAM_EXPOSED_TOOLS="${TELEGRAM_EXPOSED_TOOLS:-read-only}"

exec "$(command -v uv || echo /opt/homebrew/bin/uv)" run --directory "$SERVER_DIR" main.py
