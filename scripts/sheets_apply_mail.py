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
  interview    L = "Interview"       J = "Interview"   (the funnel counts L == "Interview")
  offer        L = "Offer"           J = "Offer"
  rejected     L = "declined" (the tracker's word for it) — unless an interview was already
               reached, because L is the furthest stage and the funnel's interview count reads
               it. J = "Rejected" either way.
  recruiter_reply  J = "Replied"; L unchanged.
  Every event except `applied` sets K First reply on, if K is empty.

  - Nothing is ever downgraded. L only moves forward: Applied < Assessment < Interview < Offer.
  - L is overwritten only when it is empty or already holds one of the values above. Hand
    notes there (e.g. "spanish") are never replaced — the event
    still goes to J and to the mail_log tab.
  - J is overwritten only from "Prepared", empty, or a value this script writes. "skipped"
    and anything else typed by hand stays.
  - Never column M (Notes — the idempotency key of sheets_add_applications.py) and never N
    (the ARRAYFORMULA). Cells are written one by one, never as a row.
  - Columns are resolved by header name.
  - A company with two rows and no role that picks one is ambiguous: logged, not written.

Every event with a company, matched or not, gets a row in the `mail_log` tab — the audit trail
of what the pipeline changed and why. Handled message ids go into state/mail_seen.json only
after the sheet write succeeds, so a failed run re-reads them.

Usage:
    uv run scripts/sheets_apply_mail.py --dry-run
    uv run scripts/sheets_apply_mail.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
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
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# One tab per track (search_profile.yaml tracks); the default track's tab first.
TRACKER_TABS = [t["tab"] for t in jobprofile.tracks()]
LOG_TAB = "mail_log"
LOG_HEADER = ["Logged on", "Email date", "Company", "Role", "Event", "From", "Subject",
              "Change", "Evidence"]

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


def a1(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def pick_row(rows: list[list[str]], col: dict, company: str, role: str) -> tuple[int | None, str]:
    """Index into rows, or None with the reason."""
    def cell(r, name):
        i = col[name]
        return r[i].strip() if len(r) > i else ""

    hits = [i for i, r in enumerate(rows) if norm_company(cell(r, "Company")) == norm_company(company)]
    if not hits:
        return None, "no tracker row"
    if len(hits) == 1:
        return hits[0], ""
    if role:
        nr = norm_role(role)
        by_role = [i for i in hits if norm_role(cell(rows[i], "Role")) == nr] or \
                  [i for i in hits if nr and (nr in norm_role(cell(rows[i], "Role"))
                                              or norm_role(cell(rows[i], "Role")) in nr)]
        if len(by_role) == 1:
            return by_role[0], ""
    applied = [i for i in hits if cell(rows[i], "Applied on")]
    if len(applied) == 1:
        return applied[0], ""
    return None, f"ambiguous: {len(hits)} rows for {company}"


def plan(rows, col, d: dict) -> tuple[int | None, dict[str, str], list[str], str]:
    """-> (row index, {column name: new value}, human-readable changes, note)."""
    idx, why = pick_row(rows, col, d["company"], d.get("role") or "")
    if idx is None:
        return None, {}, [], why
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
    return idx, writes, changes, note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
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

    # One tracker tab per track (e.g. a second track's bi_applications). A reply is applied
    # to whichever tab holds the company, the default track's tab first. Each tab's columns
    # come from its own header. A missing tab (before setup_sheets.py has run) is skipped.
    need = ["Company", "Role", "Applied on", "Status", "First reply on", "Stage reached"]
    tabs_data: dict[str, tuple[dict, list]] = {}
    for tab in TRACKER_TABS:
        try:
            header = (svc.values().get(spreadsheetId=sid, range=f"{tab}!1:1")
                      .execute().get("values", [[]]) or [[]])[0]
        except Exception:
            if tab == TRACKER_TABS[0]:
                raise
            continue
        tcol = {name: i for i, name in enumerate(header)}
        missing = [n for n in need if n not in tcol]
        if missing:
            sys.exit(f"{tab} tab has no {missing} column — run setup_sheets.py first.")
        last = a1(max(tcol[n] for n in need))
        trows = (svc.values().get(spreadsheetId=sid, range=f"{tab}!A2:{last}")
                 .execute().get("values", []))
        tabs_data[tab] = (tcol, trows)

    updates: dict[tuple[str, int, str], str] = {}
    log_rows = []
    today = date.today().isoformat()
    for d in todo:
        m = mail[d["id"]]
        if d["event"] == "none" or not d.get("company"):
            if d["event"] != "none":
                log_rows.append([today, d["date"], "", "", d["event"], m["from"], m["subject"],
                                 "no company identified", d.get("evidence", "")])
            continue
        for tab, (col, rows) in tabs_data.items():
            idx, writes, changes, note = plan(rows, col, d)
            if idx is not None:
                break
        else:
            col, rows = tabs_data[TRACKER_TABS[0]]
            tab = TRACKER_TABS[0]
            idx, writes, changes, note = plan(rows, col, d)
        if idx is not None:
            for k, v in writes.items():
                assert k not in FORBIDDEN, k
                updates[(tab, idx, k)] = v
                # later emails in this run see the state earlier ones produced
                r = rows[idx]
                r.extend([""] * (col[k] + 1 - len(r)))
                r[col[k]] = v
            role = rows[idx][col["Role"]] if len(rows[idx]) > col["Role"] else ""
            change = "; ".join(changes + ([note] if note else [])) or "no change (already there)"
        else:
            role, change = d.get("role") or "", note
        log_rows.append([today, d["date"], d["company"], role, d["event"], m["from"],
                         m["subject"], change, d.get("evidence", "")[:300]])
        print(f"  {'~' if idx is not None else '?'} {d['date']} {d['company']} — {d['event']}: "
              f"{change}", file=sys.stderr)

    if args.dry_run:
        print(f"--dry-run: {len(updates)} cell(s), {len(log_rows)} log row(s) not written.",
              file=sys.stderr)
        return 0

    if updates:
        svc.values().batchUpdate(spreadsheetId=sid, body={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"{t}!{a1(tabs_data[t][0][k])}{i + 2}", "values": [[v]]}
                     for (t, i, k), v in updates.items()]}).execute()

    if log_rows:
        tabs = {s["properties"]["title"] for s in
                svc.get(spreadsheetId=sid, fields="sheets.properties.title").execute()["sheets"]}
        if LOG_TAB not in tabs:
            svc.batchUpdate(spreadsheetId=sid, body={"requests": [
                {"addSheet": {"properties": {"title": LOG_TAB}}}]}).execute()
            svc.values().update(spreadsheetId=sid, range=f"{LOG_TAB}!A1",
                                valueInputOption="RAW", body={"values": [LOG_HEADER]}).execute()
        # Same lesson as the tracker: the end of the table comes from the whole block.
        have = svc.values().get(spreadsheetId=sid, range=f"{LOG_TAB}!A1:I"
                                ).execute().get("values", [])
        start = len(have) + 1
        svc.values().update(spreadsheetId=sid, range=f"{LOG_TAB}!A{start}",
                            valueInputOption="RAW", body={"values": log_rows}).execute()

    for d in todo:
        seen[d["id"]] = {"event": d["event"], "company": d.get("company"), "at": today}
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Applied {len(updates)} cell(s) to the tracker, {len(log_rows)} row(s) to "
          f"{LOG_TAB}; {len(todo)} message(s) marked handled.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
