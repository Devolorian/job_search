#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["telethon", "pyyaml", "google-api-python-client", "google-auth"]
# ///
"""Add every company that rejected the candidate to profile/excluded_companies.yaml.

Candidate, 2026-11-16: a company on that list gets no new folder, CV or row — triage.py drops
its postings before the model sees them. The list is the candidate's; this script only ever adds
to it, never removes or rewrites an entry, so a name added by hand stays and a company taken off
by hand is not put back (it is remembered in `released`).

A company is rejected when any of its rows has Status "Rejected" or Stage reached
"declined" — what sheets_apply_mail.py writes for a rejection, and what the candidate writes by
hand. "skipped" is the candidate's own drop, not a rejection, and does not count.

Run by daily_run.sh before the tracks, and by an interactive /job-scan at Stage 1. If the
tracker cannot be read the list stays as it is.

Usage:
    uv run scripts/rejected_companies.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobprofile  # noqa: E402  scripts/jobprofile.py — profile/ paths and the merged config
from sheets_add_applications import TAB, creds  # noqa: E402

HEADER = """\
# Companies whose new postings are not taken: no folder, no CV, no tracker row (candidate,
# 2026-11-16). triage.py drops them before the model reads them; names match the tracker's way
# ("Acme" = "Acme Inc.").
#
# scripts/rejected_companies.py adds every company with a rejection in the tracker (Status
# Rejected, or Stage reached declined) at the start of each run. Add others by hand with any
# reason. To take a company off, delete its entry and put its name under `released`, or the
# next run adds it back from the tracker.
"""


def rejections(svc, sid) -> dict[str, dict]:
    out: dict[str, dict] = {}
    rows = svc.values().get(spreadsheetId=sid, range=f"{TAB}!A1:M").execute().get("values", [])
    if not rows:
        return out
    # By header name, never by letter (CLAUDE.md, the tracker).
    col = {n.strip(): i for i, n in enumerate(rows[0])}
    get = lambda r, n: r[col[n]].strip() if n in col and col[n] < len(r) else ""  # noqa: E731
    for r in rows[1:]:
        name = get(r, "Company")
        if not name:
            continue
        if get(r, "Status").lower() != "rejected" and get(r, "Stage reached").lower() != "declined":
            continue
        when = get(r, "First reply on") or get(r, "Applied on") or get(r, "Date added")
        out.setdefault(jobprofile.norm_company(name),
                       {"name": name, "reason": f"rejected {when} ({get(r, 'Role')})".strip()})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print what would be added")
    args = ap.parse_args()
    path = jobprofile.excluded_companies_path()
    doc = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    companies = doc.get("companies") or []
    released = {jobprofile.norm_company(str(n)) for n in doc.get("released") or []}
    have = {jobprofile.norm_company(str(c.get("name") if isinstance(c, dict) else c))
            for c in companies}
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False).spreadsheets()
    found = rejections(svc, jobprofile.spreadsheet_id())
    new = [v for k, v in sorted(found.items()) if k not in have and k not in released]
    for v in new:
        print(f"  + {v['name']} — {v['reason']}", file=sys.stderr)
    print(f"rejected_companies: {len(found)} rejected in the tracker, {len(new)} new on the "
          f"list ({len(companies) + len(new)} in all)", file=sys.stderr)
    if args.dry_run or not new:
        return 0
    doc["companies"] = companies + new
    doc.setdefault("released", [])
    path.write_text(HEADER + "\n" + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                                                   width=100), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
