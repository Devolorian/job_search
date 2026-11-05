#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "pyyaml"]
# ///
"""Read the tracker and say what converts — by source, role family, score band and geography.

Written for the `funnel-review` and `follow-up` skills. Once the tracker holds a few weeks of
rows with replies applied from mail, the question is whether the score predicts anything: which sources and which kinds of posting turn
into an interview, and which only ever into "declined".

**Read-only.** The token is asked for `spreadsheets.readonly`; this script cannot write a cell,
whatever is changed in it later. Changing a weight is a `profile/search_profile.yaml` edit that
the candidate agrees to — the report proposes, it never applies.

Each tracker row is joined to its vacancy folder through the folder path in Notes (the key
`sheets_add_applications.py` writes), which gives the role family and the geography verdict the
sheet does not carry. Rows added by hand have no folder and are reported under "(no folder)".

Usage:
    uv run scripts/funnel_report.py                    # markdown report, both tabs
    uv run scripts/funnel_report.py --followups 10     # applied 10+ days ago, no reply yet
    uv run scripts/funnel_report.py --json out.json    # rows with outcomes, for the model
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml
from google.oauth2 import service_account
from googleapiclient.discovery import build

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
CONFIG = jobprofile.SHARED_SOURCES
ENV_FILE = Path.home() / ".job_search" / "sheets.env"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
TABS = {t["name"]: t["tab"] for t in jobprofile.tracks()}


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


def parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def score_band(s: str) -> str:
    try:
        v = float(s)
    except ValueError:
        return "(none)"
    v = v * 100 if v <= 1 else v
    return "80+" if v >= 80 else "70-79" if v >= 70 else "60-69" if v >= 60 else "<60"


def outcome(r: dict) -> dict:
    stage = r.get("Stage reached", "").strip().lower()
    status = r.get("Status", "").strip().lower()
    return {
        "applied": bool(r.get("Applied on", "").strip()),
        "replied": bool(r.get("First reply on", "").strip()),
        "interview": stage in ("interview", "offer") or status in ("interview", "offer"),
        "rejected": stage == "declined" or status in ("rejected", "declined"),
        "skipped": status == "skipped",
    }


def folder_facts(notes: str) -> dict:
    first = notes.split()[0] if notes.strip() else ""
    src = ROOT / first / "source.json" if first.startswith("vacancies/") else None
    if not src or not src.exists():
        return {"folder": "", "family": "(no folder)", "geo": "(no folder)"}
    rec = json.loads(src.read_text(encoding="utf-8"))
    # Folders from before 2026-10 spell the family as a label ("Analytics Engineer").
    fam = (rec.get("role_family") or "").strip().lower().replace(" ", "_")
    return {"folder": first, "family": fam or "(unset)",
            "geo": rec.get("geo") or "(unset)"}


def read_rows() -> list[dict]:
    sid = jobprofile.spreadsheet_id()
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False).spreadsheets()
    rows = []
    for track, tab in TABS.items():
        # Columns by header name, never by letter (CLAUDE.md).
        header = (svc.values().get(spreadsheetId=sid, range=f"{tab}!1:1")
                  .execute().get("values", [[]]) or [[]])[0]
        for i, raw in enumerate(svc.values().get(spreadsheetId=sid, range=f"{tab}!A2:N")
                                .execute().get("values", []), start=2):
            r = {h: (raw[j].strip() if j < len(raw) else "") for j, h in enumerate(header)}
            if not r.get("Company") and not r.get("Role"):
                continue
            r.update(track=track, tab=tab, row=i, **folder_facts(r.get("Notes", "")))
            r["band"] = score_band(r.get("Match score", ""))
            r["outcome"] = outcome(r)
            rows.append(r)
    return rows


def table(rows: list[dict], key: str) -> str:
    g: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        g[r.get(key) or "(blank)"].append(r)
    out = [f"| {key} | rows | applied | replied | interview | rejected | interview / applied |",
           "|---|---|---|---|---|---|---|"]
    for k, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        c = {f: sum(r["outcome"][f] for r in rs) for f in ("applied", "replied", "interview", "rejected")}
        rate = f"{c['interview'] / c['applied']:.0%}" if c["applied"] else "-"
        out.append(f"| {k} | {len(rs)} | {c['applied']} | {c['replied']} | {c['interview']} "
                   f"| {c['rejected']} | {rate} |")
    return "\n".join(out)


def report(rows: list[dict]) -> str:
    applied = [r for r in rows if r["outcome"]["applied"]]
    days = [(parse_date(r["First reply on"]) - parse_date(r["Applied on"])).days
            for r in applied if parse_date(r.get("First reply on", "")) and parse_date(r["Applied on"])]
    parts = [
        f"# Funnel — {date.today().isoformat()}",
        f"{len(rows)} rows, {len(applied)} applied, "
        f"{sum(r['outcome']['replied'] for r in rows)} replied, "
        f"{sum(r['outcome']['interview'] for r in rows)} reached interview, "
        f"{sum(r['outcome']['rejected'] for r in rows)} rejected. "
        + (f"Median days to first reply: {sorted(days)[len(days) // 2]}." if days else ""),
        "Rates are over applied rows only: a row never sent says nothing about the posting.",
    ]
    for key in ("Source", "family", "band", "geo", "track"):
        parts += [f"\n## By {key}", table(applied, key)]
    # Rows the candidate chose not to send carry the reason as a hand note in Stage reached
    # (e.g. "spanish"). Those are the gates' misses: postings the pipeline passed and a person
    # turned down.
    ours = {"", "applied", "assessment", "interview", "offer", "declined"}
    unsent = [r for r in rows if not r["outcome"]["applied"]]
    why = defaultdict(list)
    for r in unsent:
        note = r.get("Stage reached", "").strip()
        if note.lower() not in ours:
            why[note.lower()].append(f"{r['Company']} ({r['tab']}!{r['row']})")
    parts += [f"\n## Not sent — {len(unsent)} rows, {sum(map(len, why.values()))} with a reason",
              "| reason | rows | companies |", "|---|---|---|"]
    parts += [f"| {k} | {len(v)} | {', '.join(v)} |" for k, v in sorted(why.items(), key=lambda kv: -len(kv[1]))]
    return "\n".join(parts)


def followups(rows: list[dict], min_days: int) -> str:
    today = date.today()
    due = []
    for r in rows:
        o, applied = r["outcome"], parse_date(r.get("Applied on", ""))
        if not applied or o["replied"] or o["rejected"] or o["skipped"]:
            continue
        if (today - applied).days >= min_days:
            due.append(((today - applied).days, r))
    lines = [f"# No reply {min_days}+ days after applying — {len(due)} rows",
             "| days | company | role | applied on | tab row | folder |", "|---|---|---|---|---|---|"]
    for d, r in sorted(due, key=lambda x: -x[0]):
        lines.append(f"| {d} | {r['Company']} | {r['Role']} | {r['Applied on']} | {r['tab']}!{r['row']} "
                     f"| {r['folder'] or '-'} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--followups", type=int, metavar="DAYS")
    ap.add_argument("--json", metavar="PATH")
    a = ap.parse_args()
    rows = read_rows()
    if a.json:
        Path(a.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(rows)} rows -> {a.json}")
    print(followups(rows, a.followups) if a.followups is not None else report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
