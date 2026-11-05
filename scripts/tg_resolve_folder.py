#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["telethon", "pyyaml"]
# ///
"""Resolve the Telegram folder named in the sources config (profile/sources.yaml overlay) to a
concrete chat list.

Two jobs:
  1. A setup smoke test — proves the API credentials and session string work before the
     MCP server is registered, so an auth problem surfaces here rather than inside a
     headless 09:17 run.
  2. Keeps `state/telegram_chats.json` in sync with the folder, so chats added to
     the folder later are picked up automatically.

Reads credentials from ~/.job_search/telegram.env:
    TELEGRAM_API_ID=...
    TELEGRAM_API_HASH=...
    TELEGRAM_SESSION_STRING=...

Usage:
    uv run scripts/tg_resolve_folder.py            # show the folder's chats, write config
    uv run scripts/tg_resolve_folder.py --list     # list every folder, change nothing
    uv run scripts/tg_resolve_folder.py --dry-run  # resolve but do not write config
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from telethon import TelegramClient, functions
from telethon.sessions import StringSession

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
CONFIG = jobprofile.SHARED_SOURCES
# Resolved chats go here, not back into sources.yaml: yaml.safe_dump would rewrite that
# file and strip every comment in it.
CHATS_OUT = ROOT / "state" / "telegram_chats.json"
ENV_FILE = Path.home() / ".job_search" / "telegram.env"


def load_env() -> dict[str, str]:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_STRING"):
        env.setdefault(k, os.environ.get(k, ""))
    missing = [k for k, v in env.items() if not v and k.startswith("TELEGRAM")]
    if missing:
        sys.exit(
            f"Missing {', '.join(missing)}.\n"
            f"Put them in {ENV_FILE} (chmod 600):\n"
            "  TELEGRAM_API_ID=12345678\n"
            "  TELEGRAM_API_HASH=...\n"
            "  TELEGRAM_SESSION_STRING=1BVtsO...\n\n"
            "api_id/api_hash come from https://my.telegram.org -> API development tools.\n"
            "The session string comes from the telegram-mcp session generator."
        )
    return env


def entity_title(chat) -> str:
    return getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id)


async def run(args) -> int:
    env = load_env()
    client = TelegramClient(
        StringSession(env["TELEGRAM_SESSION_STRING"]),
        int(env["TELEGRAM_API_ID"]),
        env["TELEGRAM_API_HASH"],
    )
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Session string is not authorized. Regenerate it.")

    me = await client.get_me()
    print(f"Signed in as {me.first_name or ''} (@{me.username or 'no username'})\n")

    filters = await client(functions.messages.GetDialogFiltersRequest())
    entries = getattr(filters, "filters", filters)

    named = []
    for f in entries:
        title = getattr(f, "title", None)
        if title is None:
            continue                       # the "All chats" pseudo-filter has no title
        # Telegram 2024+ wraps folder titles in a TextWithEntities object.
        text = getattr(title, "text", title)
        named.append((str(text), f))

    if args.list:
        print("Folders:")
        for text, f in named:
            n = len(getattr(f, "include_peers", []) or [])
            print(f"  - {text}  ({n} pinned/included peer(s))")
        await client.disconnect()
        return 0

    cfg = jobprofile.load_sources()
    wanted = cfg["sources"]["telegram"]["folder"]

    # Telegram folder names routinely carry emoji ("\U0001F50DSearch"), so compare on
    # letters and digits only rather than requiring an exact string.
    def key(t: str) -> str:
        return "".join(ch for ch in t.lower() if ch.isalnum())

    match = next((f for text, f in named if key(text) == key(wanted)), None)
    if match is None:
        print(f"No folder named {wanted!r}. Folders found: "
              + ", ".join(repr(t) for t, _ in named), file=sys.stderr)
        await client.disconnect()
        return 1

    peers = list(getattr(match, "pinned_peers", []) or []) + \
            list(getattr(match, "include_peers", []) or [])

    resolved, seen = [], set()
    for peer in peers:
        try:
            ent = await client.get_entity(peer)
        except Exception as exc:                      # left the channel, deleted, etc.
            print(f"  ! could not resolve a peer: {exc}", file=sys.stderr)
            continue
        if ent.id in seen:
            continue
        seen.add(ent.id)
        resolved.append({
            "id": ent.id,
            "title": entity_title(ent),
            "username": getattr(ent, "username", None) or "",
        })

    print(f"Folder {wanted!r} — {len(resolved)} chat(s):")
    for c in resolved:
        handle = f"@{c['username']}" if c["username"] else "(private)"
        print(f"  {c['id']:>15}  {handle:<28} {c['title']}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
    else:
        CHATS_OUT.parent.mkdir(parents=True, exist_ok=True)
        CHATS_OUT.write_text(
            json.dumps(
                {"folder": wanted,
                 "resolved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "chats": resolved},
                ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {len(resolved)} chat(s) to {CHATS_OUT.relative_to(ROOT)}")

    await client.disconnect()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list all folders and exit")
    ap.add_argument("--dry-run", action="store_true", help="resolve but do not write config")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
