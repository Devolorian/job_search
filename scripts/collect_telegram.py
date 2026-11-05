#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["telethon", "pyyaml"]
# ///
"""Collect vacancy posts from the Telegram folder, deterministically.

Same reasoning as collect_boards.py: reading 21 chats through the MCP server would pull every
message into the model's context, most of it chatter. This does the mechanical part — window,
prefilter, candidate-vs-vacancy classification — and emits only what survives.

The classification rule is the one in config/sources.yaml: a message is a vacancy when it
carries a vacancy marker and no candidate marker. Several chats in the folder are group chats
where people post their OWN CV; those contain every keyword we search for.

Usage:
    uv run scripts/collect_telegram.py --json state/telegram.jsonl
    uv run scripts/collect_telegram.py --hours 168 --json out.jsonl   # override the window
    uv run scripts/collect_telegram.py --json state/telegram.jsonl \
        --cursors-out state/telegram_cursors.pending.json             # read now (daily_run.sh)
    uv run scripts/collect_telegram.py --commit-from state/telegram_cursors.pending.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from telethon import TelegramClient, functions
from telethon.sessions import StringSession

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
CONFIG = jobprofile.SHARED_SOURCES
CHATS = ROOT / "state" / "telegram_chats.json"
LAST_RUN = ROOT / "state" / "last_run.json"
ENV_FILE = Path.home() / ".job_search" / "telegram.env"


def load_env() -> dict[str, str]:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_STRING"):
        env.setdefault(k, os.environ.get(k, ""))
        if not env[k]:
            sys.exit(f"Missing {k} — see README_SETUP.md")
    return env


def classify(text: str, cfg: dict, from_channel: bool) -> tuple[str, list[str]]:
    t = text.lower()
    cand = [m for v in cfg["candidate_post_markers"].values() for m in v if m.lower() in t]
    if cand:
        return "candidate", cand[:3]
    vac = [m for v in cfg["vacancy_markers"].values() for m in v if m.lower() in t]
    if vac:
        return "vacancy", vac[:3]
    # No marker either way. A channel broadcast is almost always a posting; an individual's
    # message in a group chat is not safe to assume either way.
    return ("vacancy", []) if from_channel else ("ambiguous", [])


async def run(args) -> int:
    env = load_env()
    cfg = jobprofile.load_sources()["sources"]["telegram"]
    excluded = {e["id"] for e in cfg.get("exclude_chats", [])}
    prefilter = [k.lower() for k in cfg["prefilter_keywords"]]

    # Resolve the folder live rather than trusting cached ids: Telethon cannot turn a bare
    # integer id back into a peer without having seen the dialog in this session, so the ids
    # in state/telegram_chats.json are for reporting, not for fetching.
    known = {c["id"]: c for c in json.loads(CHATS.read_text(encoding="utf-8"))["chats"]}

    last = json.loads(LAST_RUN.read_text(encoding="utf-8")) if LAST_RUN.exists() else {}
    cursors = (last.get("telegram") or {}).get("last_message_id_by_chat", {}) or {}

    # The window is per chat (2026-10-30): a chat with no cursor is a new source and gets the
    # first-run lookback, however many other chats already have one — before this a channel
    # added to the folder got the 30h window on its first read. An explicit --hours wins.
    now = datetime.now(timezone.utc)
    hours = args.hours or cfg["lookback_hours"]
    first_hours = args.hours or cfg.get("first_run_lookback_hours", 336)
    since = now - timedelta(hours=hours)
    since_first = now - timedelta(hours=first_hours)
    print(f"Window: {hours}h (since {since:%Y-%m-%d %H:%M} UTC); "
          f"chats without a cursor: {first_hours}h", file=sys.stderr)

    client = TelegramClient(StringSession(env["TELEGRAM_SESSION_STRING"]),
                            int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Telegram session is not authorized.")

    wanted = cfg["folder"]
    key = lambda t: "".join(ch for ch in str(t).lower() if ch.isalnum())
    filters = await client(functions.messages.GetDialogFiltersRequest())
    folder = None
    for f in getattr(filters, "filters", filters):
        title = getattr(f, "title", None)
        if title is None:
            continue
        if key(getattr(title, "text", title)) == key(wanted):
            folder = f
            break
    if folder is None:
        sys.exit(f"No Telegram folder named {wanted!r}")

    peers = list(getattr(folder, "pinned_peers", []) or []) + \
            list(getattr(folder, "include_peers", []) or [])

    chats, seen_ids = [], set()
    for peer in peers:
        try:
            ent = await client.get_entity(peer)
        except Exception as exc:
            print(f"  ! unresolvable peer: {type(exc).__name__}", file=sys.stderr)
            continue
        if ent.id in excluded or ent.id in seen_ids:
            continue
        seen_ids.add(ent.id)
        chats.append({"id": ent.id, "entity": ent,
                      "title": getattr(ent, "title", "") or str(ent.id),
                      "username": getattr(ent, "username", None) or ""})
    print(f"  {len(chats)} chats after exclusions", file=sys.stderr)

    out, stats, new_cursors = [], {"read": 0, "prefiltered": 0, "candidate": 0,
                                   "ambiguous": 0, "vacancy": 0}, {}
    for chat in chats:
        cid, ent = chat["id"], chat["entity"]
        min_id = int(cursors.get(str(cid), 0))
        chat_since = since if str(cid) in cursors else since_first
        if str(cid) not in cursors:
            print(f"  new chat, first-run window: {chat['title'][:40]}", file=sys.stderr)
        top = min_id
        n_chat = 0
        try:
            async for msg in client.iter_messages(ent, limit=args.per_chat,
                                                  min_id=min_id, offset_date=None):
                if msg.date and msg.date < chat_since:
                    break
                top = max(top, msg.id)
                stats["read"] += 1
                text = (msg.text or "").strip()
                if len(text) < 60:
                    continue
                low = text.lower()
                if not any(k in low for k in prefilter):
                    continue
                stats["prefiltered"] += 1
                from_channel = bool(getattr(ent, "broadcast", False))
                verdict, why = classify(text, cfg, from_channel)
                stats[verdict] += 1
                if verdict != "vacancy":
                    continue
                handle = chat.get("username") or ""
                out.append({
                    "title": "",              # parsed by the model from the text
                    "company": "",
                    "location": "",
                    "url": (f"https://t.me/{handle}/{msg.id}" if handle
                            else f"tg://chat?id={cid}&msg={msg.id}"),
                    "description": text,
                    "posted": msg.date.isoformat() if msg.date else "",
                    "source": f"telegram:{handle or chat['title']}",
                    "chat_id": cid,
                    "message_id": msg.id,
                    "matched_markers": why,
                })
                n_chat += 1
        except Exception as exc:
            print(f"  ! {chat['title'][:30]}: {type(exc).__name__}: {str(exc)[:60]}",
                  file=sys.stderr)
            continue
        if top > min_id:
            new_cursors[str(cid)] = top
        if n_chat:
            print(f"  {n_chat:>3} from {(chat.get('username') and '@'+chat['username']) or chat['title'][:28]}",
                  file=sys.stderr)

    await client.disconnect()

    seen, uniq = set(), []
    for r in out:
        key = hashlib.sha1(r["description"][:400].encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        r["dedup_key"] = key
        uniq.append(r)

    print(f"\n  read {stats['read']} | prefiltered {stats['prefiltered']} | "
          f"candidate posts dropped {stats['candidate']} | ambiguous dropped "
          f"{stats['ambiguous']} | vacancies {len(uniq)} (from {stats['vacancy']} before dedup)",
          file=sys.stderr)

    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in uniq)
    if args.json:
        args.json.write_text(body + "\n", encoding="utf-8")
        print(f"  written to {args.json}", file=sys.stderr)
    else:
        print(body)

    # Pending cursors: the ids of THIS read, saved for a later --commit-from. Added for the
    # parallel two-track run, and it also closes a window seen in an early run log —
    # committing with a second read advanced past messages that arrived between the two reads,
    # which no track had seen. Committing the first read's ids loses nothing.
    if args.cursors_out:
        args.cursors_out.write_text(json.dumps({**cursors, **new_cursors}, indent=1) + "\n",
                                    encoding="utf-8")
        print(f"  pending cursors for {len(new_cursors)} chat(s) -> {args.cursors_out}",
              file=sys.stderr)

    # Cursors are written only when asked, and only at the very end — a crash mid-run must
    # leave the next run re-reading rather than silently skipping.
    if args.commit_cursors:
        last.setdefault("telegram", {})["last_message_id_by_chat"] = {**cursors, **new_cursors}
        last["telegram"]["last_run_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        LAST_RUN.write_text(json.dumps(last, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"  cursors committed for {len(new_cursors)} chat(s)", file=sys.stderr)
    return 0


def commit_from(path: Path) -> int:
    """Advance state/last_run.json to cursors saved by an earlier --cursors-out read. No
    Telegram connection: the run that read the messages is over, this only records it."""
    if not path.exists():
        sys.exit(f"{path} not found — nothing to commit")
    pending = json.loads(path.read_text(encoding="utf-8"))
    last = json.loads(LAST_RUN.read_text(encoding="utf-8")) if LAST_RUN.exists() else {}
    tg = last.setdefault("telegram", {})
    tg["last_message_id_by_chat"] = {**(tg.get("last_message_id_by_chat") or {}), **pending}
    tg["last_run_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    LAST_RUN.write_text(json.dumps(last, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.unlink()
    print(f"  cursors committed for {len(pending)} chat(s) from {path.name}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--hours", type=int, help="override the lookback window")
    ap.add_argument("--per-chat", type=int, default=300, help="max messages read per chat")
    ap.add_argument("--commit-cursors", action="store_true",
                    help="advance state/last_run.json after a successful run")
    ap.add_argument("--cursors-out", type=Path,
                    help="save this read's cursors here instead of committing them")
    ap.add_argument("--commit-from", type=Path,
                    help="commit cursors saved by --cursors-out, without reading Telegram")
    args = ap.parse_args()
    if args.commit_from:
        return commit_from(args.commit_from)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
