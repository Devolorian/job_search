#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "pyyaml"]
# ///
"""Apply the model's reading of employer emails to the tracker.

Input is state/mail_decisions.json, written by the model at Stage 5a of prompts/daily_scan.md
from state/mail.jsonl (collect_mail.py). One decision per message:

    {"id": "<gmail message id>", "event": "applied|rejected|interview|assessment|offer|
     recruiter_reply|none", "company": "<exactly as in the tracker's Company column, or null>",
     "role": "<optional, disambiguates two rows at one company>", "date": "YYYY-MM-DD",
     "evidence": "<short quote from the email>"}

Subject and sender are taken from state/mail.jsonl by id, never from the decision — the model
decides what an email means, not what it said.

What gets written, and the rules that keep it honest:

  applied      B Applied on = email date, ONLY if B is empty. An ATS confirmation is proof the
               application was sent — the one case where the pipeline may write B.
               L Stage reached = "Applied".
  assessment   L = "Assessment"      J Status = "Assessment"
  interview    L = "Interview"       J = "Interview"   (funnel_report counts L == "Interview")
  offer        L = "Offer"           J = "Offer"
  rejected     L = "declined" (the tracker's word for it) — unless an interview was already
               reached, because L is the furthest stage and funnel_report's interview count reads
               it. J = "Rejected" either way.
  recruiter_reply  J = "Replied"; L unchanged.
  Every event except `applied` sets K First reply on, if K is empty.

  - Nothing is ever downgraded. L only moves forward: Applied < Assessment < Interview < Offer.
  - L is overwritten only when it is empty or already holds one of the values above. Hand
    notes there (e.g. "spanish") are never replaced — the event
    still goes to J.
  - J is overwritten only from "Prepared", empty, or a value this script writes. "skipped"
    and anything else typed by hand stays.
  - Never column M (Notes — the idempotency key of sheets_add_applications.py) and never N
    (the ARRAYFORMULA). Cells are written one by one, never as a row.
  - Columns are resolved by header name.
  - A company with two rows and no role that picks one is ambiguous: logged, not written.

Every event is printed with what it changed (the run log keeps it); the tracker has one tab
and no log tab since 2026-11-18. Handled message ids go into state/mail_seen.json only after
the sheet write succeeds, so a failed run re-reads them.

An interview invitation or an offer is also pushed to the candidate's private chat with their
bot (~/.job_search/telegram_bot.env, the chat tg_notify.py posts vacancy cards into) — once per
email, after the sheet write, whether or not the company has a tracker row. No bot configured,
or the Bot API failing, costs the push only: the sheet and mail_seen are already written.

Usage:
    uv run scripts/sheets_apply_mail.py --dry-run
    uv run scripts/sheets_apply_mail.py
    uv run scripts/sheets_apply_mail.py --no-push     # backfill of old mail
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import yaml
from google.oauth2 import service_account
from googleapiclient.discovery import build

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
CONFIG = jobprofile.SHARED_SOURCES
MAIL = ROOT / "state" / "mail.jsonl"
DECISIONS = ROOT / "state" / "mail_decisions.json"
SEEN = ROOT / "state" / "mail_seen.json"
ENV_FILE = Path.home() / ".job_search" / "sheets.env"
BOT_ENV = Path.home() / ".job_search" / "telegram_bot.env"
PUSH_EVENTS = {"interview": "🔔 Приглашение на собеседование", "offer": "🎉 Оффер"}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TAB = jobprofile.tracker_tab()

EVENTS = {"applied", "rejected", "interview", "assessment", "offer", "recruiter_reply", "none"}
STAGE_OF = {"applied": "Applied", "assessment": "Assessment", "interview": "Interview",
            "offer": "Offer", "rejected": "declined"}
STAGE_RANK = {"": 0, "applied": 1, "assessment": 2, "interview": 3, "offer": 4}
STATUS_OF = {"applied": "Applied", "recruiter_reply": "Replied", "assessment": "Assessment",
             "interview": "Interview", "offer": "Offer", "rejected": "Rejected"}
STATUS_RANK = {"": 0, "prepared": 0, "applied": 1, "replied": 2, "assessment": 3,
               "interview": 4, "offer": 5, "rejected": 5}
FORBIDDEN = {"Notes", "Days to reply"}

LEGAL_SUFFIXES = {"inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "s.l.", "sl",
                  "b.v.", "bv", "s.a.", "sa", "ag", "oy", "ab", "plc"}


def norm_company(name: str) -> str:
    """Same normalisation as sheets_add_applications.py, copied rather than imported."""
    parts = re.sub(r"\s+", " ", (name or "").strip().lower()).split()
    if len(parts) > 1 and parts[-1].strip(",") in LEGAL_SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts).rstrip(",")


def norm_role(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def creds():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
    path = os.environ.get("SERVICE_ACCOUNT_PATH", "")
    if not path or not Path(path).exists():
        sys.exit(f"SERVICE_ACCOUNT_PATH not set or missing (looked in {ENV_FILE}).")
    info = json.loads(Path(path).read_text(encoding="utf-8"))
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def push(items: list[tuple[dict, dict, str]]) -> set[str]:
    """Send one bot message per (decision, mail record, tracker role). Returns the ids sent.

    Same chat and same env file as tg_notify.py; the token is in the URL, so an error prints
    the method and status only."""
    env = {}
    if BOT_ENV.exists():
        for line in BOT_ENV.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_BOT_CHAT_ID")
    if not (token and chat):
        print(f"  ! {len(items)} interview/offer push(es) not sent: no TELEGRAM_BOT_TOKEN / "
              f"TELEGRAM_BOT_CHAT_ID in {BOT_ENV}", file=sys.stderr)
        return set()
    e = lambda s: html.escape(str(s or "").strip(), quote=False)
    sent = set()
    for d, m, role in items:
        lines = [f"<b>{PUSH_EVENTS[d['event']]}</b>",
                 f"<b>{e(d.get('company') or 'компания не определена')}</b>"
                 + (f" — {e(role)}" if role else ""),
                 f"Письмо от {e(d['date'])}: {e(m['from'])}",
                 f"Тема: {e(m['subject'])}"]
        if d.get("evidence"):
            lines += ["", f"«{e(d['evidence'][:300])}»"]
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": True})}).encode()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                        data=data, timeout=30):
                sent.add(d["id"])
        except (urllib.error.URLError, OSError) as ex:
            code = getattr(ex, "code", type(ex).__name__)
            print(f"  ! push for {d.get('company')} not sent: Bot API sendMessage {code}",
                  file=sys.stderr)
    return sent


def a1(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def pick_row(col: dict, rows: list, company: str, role: str) -> tuple[int | None, str]:
    """(index into rows), or (None, reason).

    A company with two rows (Constructor as an analytics engineer and as a data engineer) has
    its reply matched by role, then by which of them was applied to."""
    def cell(i, name):
        r, c = rows[i], col[name]
        return r[c].strip() if len(r) > c else ""

    hits = [i for i in range(len(rows))
            if norm_company(cell(i, "Company")) == norm_company(company)]
    if not hits:
        return None, "no tracker row"
    if role:
        nr = norm_role(role)
        by_role = [h for h in hits if norm_role(cell(h, "Role")) == nr] or \
                  [h for h in hits if nr and (nr in norm_role(cell(h, "Role"))
                                              or norm_role(cell(h, "Role")) in nr)]
        if len(by_role) == 1:
            return by_role[0], ""
    if len(hits) == 1:
        return hits[0], ""
    applied = [h for h in hits if cell(h, "Applied on")]
    if len(applied) == 1:
        return applied[0], ""
    return None, f"ambiguous: {len(hits)} rows for {company}"


def plan(rows, col, idx: int, d: dict) -> tuple[dict[str, str], list[str], str]:
    """-> ({column name: new value}, human-readable changes, note)."""
    r = rows[idx]

    def cur(name):
        i = col[name]
        return r[i].strip() if len(r) > i else ""

    ev, when = d["event"], d["date"]
    writes: dict[str, str] = {}

    if ev == "applied" and not cur("Applied on"):
        writes["Applied on"] = when

    l_now = cur("Stage reached")
    l_key = l_now.lower()
    l_ours = l_key in STAGE_RANK or l_key == "declined"
    if ev in STAGE_OF and l_ours:
        if ev == "rejected":
            if l_key != "declined" and STAGE_RANK.get(l_key, 0) < STAGE_RANK["interview"]:
                writes["Stage reached"] = "declined"
        elif l_key != "declined" and STAGE_RANK[ev] > STAGE_RANK.get(l_key, 0):
            writes["Stage reached"] = STAGE_OF[ev]
    note = "" if l_ours or ev not in STAGE_OF else f"L kept (hand note {l_now!r})"

    j_now = cur("Status").lower()
    # J is the latest state: a rejection replaces anything but an offer; otherwise forward only.
    if ev in STATUS_OF and j_now in STATUS_RANK and j_now != "offer":
        new = STATUS_OF[ev]
        if ev == "rejected" or ev == "offer" or (
                j_now != "rejected" and STATUS_RANK[new.lower()] > STATUS_RANK[j_now]):
            if new.lower() != j_now:
                writes["Status"] = new

    if ev not in ("applied", "none") and not cur("First reply on"):
        writes["First reply on"] = when

    changes = [f"{a1(col[k])} {k}: {cur(k)!r} -> {v!r}" for k, v in writes.items()]
    return writes, changes, note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true",
                    help="no Telegram push — for a backfill of old mail, whose interviews are past")
    ap.add_argument("--decisions", type=Path, default=DECISIONS)
    ap.add_argument("--mail", type=Path, default=MAIL)
    args = ap.parse_args()

    if not args.decisions.exists():
        print(f"No {args.decisions.name}; nothing to apply.", file=sys.stderr)
        return 0
    decisions = json.loads(args.decisions.read_text(encoding="utf-8"))
    mail = {}
    if args.mail.exists():
        for line in args.mail.read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = json.loads(line)
                mail[m["id"]] = m
    try:
        seen = json.loads(SEEN.read_text(encoding="utf-8"))
    except Exception:
        seen = {}

    bad = [d for d in decisions if d.get("event") not in EVENTS
           or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d.get("date", "")))
           or d.get("id") not in mail]
    for d in bad:
        print(f"  ! rejected decision (unknown id, event or date): {d}", file=sys.stderr)
    todo = sorted((d for d in decisions if d not in bad and d["id"] not in seen),
                  key=lambda d: (d["date"], mail[d["id"]]["date"]))

    sid = jobprofile.spreadsheet_id()
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False).spreadsheets()

    need = ["Company", "Role", "Applied on", "Status", "First reply on", "Stage reached"]
    header = (svc.values().get(spreadsheetId=sid, range=f"{TAB}!1:1")
              .execute().get("values", [[]]) or [[]])[0]
    col = {name: i for i, name in enumerate(header)}
    missing = [n for n in need if n not in col]
    if missing:
        sys.exit(f"{TAB} tab has no {missing} column — run setup_sheets.py first.")
    last = a1(max(col[n] for n in need))
    rows = (svc.values().get(spreadsheetId=sid, range=f"{TAB}!A2:{last}")
            .execute().get("values", []))

    updates: dict[tuple[int, str], str] = {}
    events = 0
    pushes: list[tuple[dict, dict, str]] = []
    today = date.today().isoformat()
    for d in todo:
        m = mail[d["id"]]
        if d["event"] in PUSH_EVENTS and not d.get("company"):
            pushes.append((d, m, ""))
        if d["event"] == "none" or not d.get("company"):
            if d["event"] != "none":
                events += 1
                print(f"  ? {d['date']} {d['event']} from {m['from']} ({m['subject']}): "
                      f"no company identified", file=sys.stderr)
            continue
        idx, note = pick_row(col, rows, d["company"], d.get("role") or "")
        if idx is not None:
            writes, changes, note = plan(rows, col, idx, d)
            for k, v in writes.items():
                assert k not in FORBIDDEN, k
                updates[(idx, k)] = v
                # later emails in this run see the state earlier ones produced
                r = rows[idx]
                r.extend([""] * (col[k] + 1 - len(r)))
                r[col[k]] = v
            role = rows[idx][col["Role"]] if len(rows[idx]) > col["Role"] else ""
            change = "; ".join(changes + ([note] if note else [])) or "no change (already there)"
        else:
            role, change = d.get("role") or "", note
        if d["event"] in PUSH_EVENTS:
            pushes.append((d, m, role))
        events += 1
        print(f"  {'~' if idx is not None else '?'} {d['date']} {d['company']} — {d['event']}: "
              f"{change}", file=sys.stderr)

    if args.dry_run:
        print(f"--dry-run: {len(updates)} cell(s) not written, "
              f"{len(pushes)} Telegram push(es) not sent.", file=sys.stderr)
        return 0

    if updates:
        svc.values().batchUpdate(spreadsheetId=sid, body={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"{TAB}!{a1(col[k])}{i + 2}", "values": [[v]]}
                     for (i, k), v in updates.items()]}).execute()

    for d in todo:
        seen[d["id"]] = {"event": d["event"], "company": d.get("company"), "at": today}
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Applied {len(updates)} cell(s) to the tracker from {events} event(s); "
          f"{len(todo)} message(s) marked handled.", file=sys.stderr)

    # Last: the sheet and mail_seen are written, so a failed push is lost, never duplicated.
    if pushes and args.no_push:
        print(f"--no-push: {len(pushes)} interview/offer push(es) not sent.", file=sys.stderr)
    elif pushes:
        sent = push(pushes)
        for mid in sent:
            seen[mid]["pushed"] = True
        SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"Telegram: {len(sent)} of {len(pushes)} interview/offer push(es) sent.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
