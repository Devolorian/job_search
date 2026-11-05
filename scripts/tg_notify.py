#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["telethon", "pyyaml", "google-api-python-client", "google-auth"]
# ///
"""Post every new vacancy folder to the candidate's private chat with a bot, and read the
candidate's reactions back as "applied".

A Telegram chat where every found position lands; a reaction on one means the candidate applied
to it, and the tracker is marked. Any reaction counts.

Rule 1 still holds. The bot writes only to the one private chat with the candidate
(TELEGRAM_BOT_CHAT_ID) and nowhere else; it has no way to reach an employer. The candidate's own
Telegram account stays read-only — the reactions are read through the same user session
collect_telegram.py uses, with reads only. Secrets: ~/.job_search/telegram_bot.env
(TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_CHAT_ID) and the existing ~/.job_search/telegram.env.

  post   One card per folder in vacancies/ not yet in state/tg_posted.json: track, score,
         contract, company, role, location, why it matches, link, folder path. The first run
         with no state file posts nothing — it records every existing folder as the baseline
         (by design: new folders only).
  sync   For every posted card that carries a reaction and is not marked yet: the folder's
         tracker row gets Applied on = the reaction's date (only if empty), Stage reached =
         Applied (only if empty — it only moves forward and hand notes stay), Status = Applied
         (only from Prepared or empty). A folder with no row (a second role at a company already
         tracked) gets a new row: the reaction is the hand decision that one-row-per-company
         leaves to the candidate. Every change goes to the mail_log tab, From = "telegram reaction".
         A removed reaction is not undone.
  setup  After /start in the bot chat: finds the chat id and writes it to telegram_bot.env.

Columns are resolved by header name; Notes (M) and the formula column (N) are never written,
except Notes on a new row, which carries the folder path as sheets_add_applications.py does.

Usage:
    uv run scripts/tg_notify.py setup
    uv run scripts/tg_notify.py post [--dry-run]
    uv run scripts/tg_notify.py sync [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from googleapiclient.discovery import build
from telethon import TelegramClient
from telethon.sessions import StringSession

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sheets_add_applications import CONFIG, TABS, creds, folder_track, row_for  # noqa: E402

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
VACANCIES = ROOT / "vacancies"
POSTED = ROOT / "state" / "tg_posted.json"
BOT_ENV = Path.home() / ".job_search" / "telegram_bot.env"
USER_ENV = Path.home() / ".job_search" / "telegram.env"
LOG_TAB = "mail_log"
LOG_HEADER = ["Logged on", "Email date", "Company", "Role", "Event", "From", "Subject",
              "Change", "Evidence"]


def read_env(path: Path, keys: tuple[str, ...], required: bool = True) -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in keys:
        env.setdefault(k, os.environ.get(k, ""))
        if required and not env[k]:
            sys.exit(f"Missing {k} in {path} — see README_SETUP.md")
    return env


def bot_api(token: str, method: str, **params) -> dict:
    """One Bot API call. A 429 is waited out once, as long as Telegram asks."""
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(url, data=data, timeout=30) as r:
                return json.loads(r.read())["result"]
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            wait = (body.get("parameters") or {}).get("retry_after")
            if e.code == 429 and wait and attempt == 1:
                time.sleep(int(wait) + 1)
                continue
            # The URL carries the token; never print it.
            sys.exit(f"Bot API {method}: HTTP {e.code} {body.get('description', '')}")
    raise AssertionError("unreachable")


def load_posted() -> dict | None:
    return json.loads(POSTED.read_text(encoding="utf-8")) if POSTED.exists() else None


def save_posted(state: dict) -> None:
    POSTED.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def folders() -> list[Path]:
    return sorted(p for p in VACANCIES.iterdir() if p.is_dir() and (p / "source.json").exists())


# ------------------------------------------------------------------------------------- post --

def card(folder: Path) -> str:
    d = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    job = (folder / "job.md").read_text(encoding="utf-8") if (folder / "job.md").exists() else ""
    contract = re.search(r"Contract:\s*([^\n]+)", job)
    why = re.search(r"## Why this matches\s*\n(.+?)(?:\n## |\Z)", job, re.S)
    e = lambda s: html.escape(str(s or "").strip(), quote=False)

    tags = [f"#{folder_track(folder)}"]
    if d.get("match_score") not in (None, ""):
        tags.append(f"{d['match_score']}/100")
    if contract:
        c = contract.group(1).strip()
        tags.append("резерв: контракт не указан" if c.startswith("unstated") else f"контракт: {c}")
    lines = [" · ".join(e(t) for t in tags), "",
             f"<b>{e(d.get('title'))}</b>", e(d.get("company"))]
    where = " · ".join(x for x in (d.get("location"), d.get("work_format")) if x)
    if where:
        lines.append(f"📍 {e(where)}")
    if d.get("salary"):
        lines.append(f"💶 {e(d['salary'])}")
    lines.append(f"Опубликовано: {e(d.get('posted') or '?')} · {e(d.get('source'))}")
    if why:
        text = re.sub(r"\s+", " ", why.group(1)).strip()
        lines += ["", e(text[:900] + ("…" if len(text) > 900 else ""))]
    if d.get("url"):
        lines += ["", e(d["url"])]
    lines.append(f"📁 <code>{e(folder.relative_to(ROOT))}</code>")
    return "\n".join(lines)


def cmd_post(args) -> int:
    state = load_posted()
    if state is None:
        state = {"baseline_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "folders": {str(f.relative_to(ROOT)): {"message_id": None, "baseline": True}
                             for f in folders()}}
        if not args.dry_run:
            save_posted(state)
        print(f"First run: {len(state['folders'])} existing folder(s) recorded as the baseline, "
              f"nothing posted. New folders are posted from the next run on.", file=sys.stderr)
        return 0

    todo = [f for f in folders() if str(f.relative_to(ROOT)) not in state["folders"]]
    if not todo:
        print("tg_notify post: nothing new.", file=sys.stderr)
        return 0
    if args.dry_run:
        for f in todo:
            print(card(f), "\n-----", file=sys.stderr)
        print(f"--dry-run: {len(todo)} card(s) not posted.", file=sys.stderr)
        return 0

    env = read_env(BOT_ENV, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_CHAT_ID"))
    for f in todo:
        msg = bot_api(env["TELEGRAM_BOT_TOKEN"], "sendMessage",
                      chat_id=env["TELEGRAM_BOT_CHAT_ID"], text=card(f), parse_mode="HTML",
                      link_preview_options=json.dumps({"is_disabled": True}))
        # Saved after every message: a crash halfway must not post the first half twice.
        state["folders"][str(f.relative_to(ROOT))] = {
            "message_id": msg["message_id"],
            "posted_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        save_posted(state)
        print(f"  > {f.name}", file=sys.stderr)
        time.sleep(1.1)   # Telegram allows about one message a second into one chat
    print(f"tg_notify post: {len(todo)} card(s) posted.", file=sys.stderr)
    return 0


# ------------------------------------------------------------------------------------- sync --

CARD_FOLDER = re.compile(r"📁\s*(vacancies/\S+)")


async def read_reactions(bot_username: str, since: datetime,
                         wanted: set[str]) -> dict[str, str]:
    """folder path -> date (YYYY-MM-DD) of a reaction on its card. Reads only.

    A card is found by the folder path printed on it, not by the id sendMessage returned:
    in a private chat the bot and the user number messages separately (measured —
    the bot's message 3 was 248430 on the user's side), so the bot's id finds nothing."""
    env = read_env(USER_ENV, ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_STRING"))
    client = TelegramClient(StringSession(env["TELEGRAM_SESSION_STRING"]),
                            int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Telegram session is not authorized.")
    out: dict[str, str] = {}
    try:
        bot = await client.get_entity(bot_username)
        async for m in client.iter_messages(bot):
            if m.date and m.date < since:
                break
            hit = CARD_FOLDER.search(m.raw_text or "") if not m.out else None
            if not hit or hit.group(1) not in wanted:
                continue
            r = m.reactions
            if not r or not any(getattr(x, "count", 0) for x in (r.results or [])):
                continue
            # In a private chat the only person who can react is the candidate. The reaction's
            # own date when Telegram gives one; otherwise today, the day it was first seen.
            dates = [p.date for p in (r.recent_reactions or []) if getattr(p, "date", None)]
            out[hit.group(1)] = (min(dates).astimezone() if dates
                                 else datetime.now()).date().isoformat()
    finally:
        await client.disconnect()
    return out


def a1(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def cmd_sync(args) -> int:
    state = load_posted()
    if not state:
        print("tg_notify sync: no state/tg_posted.json yet, nothing to read.", file=sys.stderr)
        return 0
    pending = {k: v for k, v in state["folders"].items()
               if v.get("message_id") and not v.get("applied_on")}
    if not pending:
        print("tg_notify sync: no unmarked cards.", file=sys.stderr)
        return 0

    env = read_env(BOT_ENV, ("TELEGRAM_BOT_TOKEN",))
    bot_username = bot_api(env["TELEGRAM_BOT_TOKEN"], "getMe")["username"]
    # History back to the oldest unmarked card, a minute early for clock skew.
    since = min(datetime.fromisoformat(v["posted_utc"]) for v in pending.values())
    reacted = asyncio.run(read_reactions(bot_username, since - timedelta(minutes=1),
                                         set(pending)))
    if not reacted:
        print(f"tg_notify sync: {len(pending)} card(s) checked, no new reactions.",
              file=sys.stderr)
        return 0

    sid = jobprofile.spreadsheet_id()
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False).spreadsheets()
    need = ["Company", "Role", "Applied on", "Status", "Stage reached", "Notes"]
    tabs: dict[str, tuple[dict, list]] = {}
    for tab in TABS.values():
        header = (svc.values().get(spreadsheetId=sid, range=f"{tab}!1:1")
                  .execute().get("values", [[]]) or [[]])[0]
        col = {n: i for i, n in enumerate(header)}
        if any(n not in col for n in need):
            sys.exit(f"{tab} tab lacks one of {need} — run setup_sheets.py first.")
        # The whole A:M block, never one column: see sheets_add_applications.py.
        tabs[tab] = (col, svc.values().get(spreadsheetId=sid, range=f"{tab}!A2:M")
                     .execute().get("values", []))

    def cell(r, i):
        return r[i].strip() if len(r) > i else ""

    updates, appends, log_rows = [], {}, []
    today = date.today().isoformat()
    for rel, when in sorted(reacted.items(), key=lambda kv: kv[1]):
        folder = ROOT / rel
        hit = next(((t, i) for t, (col, rows) in tabs.items() for i, r in enumerate(rows)
                    if cell(r, col["Notes"]).split()[:1] == [rel]), None)
        if hit:
            tab, i = hit
            col, rows = tabs[tab]
            r = rows[i]
            writes = {}
            if not cell(r, col["Applied on"]):
                writes["Applied on"] = when
            if not cell(r, col["Stage reached"]):
                writes["Stage reached"] = "Applied"
            if cell(r, col["Status"]).lower() in ("", "prepared"):
                writes["Status"] = "Applied"
            for k, v in writes.items():
                updates.append({"range": f"{tab}!{a1(col[k])}{i + 2}", "values": [[v]]})
            company, role = cell(r, col["Company"]), cell(r, col["Role"])
            change = "; ".join(f"{a1(col[k])} {k}: {cell(r, col[k])!r} -> {v!r}"
                               for k, v in writes.items()) or "no change (already there)"
        else:
            row = row_for(folder, today) if folder.exists() else None
            if not row:
                print(f"  ! {rel}: reacted, but no tracker row and none can be built — "
                      f"mark it by hand", file=sys.stderr)
                continue
            row[1], row[9], row[11] = when, "Applied", "Applied"
            tab = TABS[folder_track(folder)]
            appends.setdefault(tab, []).append(row)
            company, role = row[2], row[3]
            change = f"new row in {tab} (no row for this folder), Applied on {when}"
        log_rows.append([today, when, company, role, "applied", "telegram reaction",
                         rel, change, "reaction on its card in the bot chat"])
        state["folders"][rel]["applied_on"] = when
        print(f"  ~ {when} {company} — {role}: {change}", file=sys.stderr)

    if args.dry_run:
        print(f"--dry-run: {len(updates)} cell(s), {sum(map(len, appends.values()))} new "
              f"row(s), {len(log_rows)} log row(s) not written.", file=sys.stderr)
        return 0

    if updates:
        svc.values().batchUpdate(spreadsheetId=sid, body={
            "valueInputOption": "USER_ENTERED", "data": updates}).execute()
    for tab, rows in appends.items():
        start = len(tabs[tab][1]) + 2
        svc.values().update(spreadsheetId=sid, range=f"{tab}!A{start}:M{start + len(rows) - 1}",
                            valueInputOption="USER_ENTERED", body={"values": rows}).execute()
    if log_rows:
        names = {s["properties"]["title"] for s in
                 svc.get(spreadsheetId=sid, fields="sheets.properties.title").execute()["sheets"]}
        if LOG_TAB not in names:
            svc.batchUpdate(spreadsheetId=sid, body={"requests": [
                {"addSheet": {"properties": {"title": LOG_TAB}}}]}).execute()
            svc.values().update(spreadsheetId=sid, range=f"{LOG_TAB}!A1",
                                valueInputOption="RAW", body={"values": [LOG_HEADER]}).execute()
        have = svc.values().get(spreadsheetId=sid, range=f"{LOG_TAB}!A1:I"
                                ).execute().get("values", [])
        svc.values().update(spreadsheetId=sid, range=f"{LOG_TAB}!A{len(have) + 1}",
                            valueInputOption="RAW", body={"values": log_rows}).execute()
    # Last, after the sheet: a failed write leaves the cards unmarked and the next sync retries.
    save_posted(state)
    print(f"tg_notify sync: {len(log_rows)} application(s) marked from reactions.",
          file=sys.stderr)
    return 0


# ------------------------------------------------------------------------------------ setup --

def cmd_setup(args) -> int:
    env = read_env(BOT_ENV, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_CHAT_ID"), required=False)
    if not env["TELEGRAM_BOT_TOKEN"]:
        sys.exit(f"Put TELEGRAM_BOT_TOKEN=<token from @BotFather> into {BOT_ENV} first.")
    me = bot_api(env["TELEGRAM_BOT_TOKEN"], "getMe")
    if env["TELEGRAM_BOT_CHAT_ID"]:
        print(f"@{me['username']}: chat id already set ({env['TELEGRAM_BOT_CHAT_ID']}).")
        return 0
    chats = {u["message"]["chat"]["id"]
             for u in bot_api(env["TELEGRAM_BOT_TOKEN"], "getUpdates")
             if u.get("message", {}).get("chat", {}).get("type") == "private"}
    if len(chats) != 1:
        sys.exit(f"Expected one private chat with @{me['username']}, found {len(chats)}. "
                 f"Open the bot, press Start, and run this again.")
    chat_id = chats.pop()
    with BOT_ENV.open("a", encoding="utf-8") as fh:
        fh.write(f"\nTELEGRAM_BOT_CHAT_ID={chat_id}\n")
    print(f"@{me['username']}: chat id {chat_id} written to {BOT_ENV}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["setup", "post", "sync"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return {"setup": cmd_setup, "post": cmd_post, "sync": cmd_sync}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
