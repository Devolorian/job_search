#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "pyyaml"]
# ///
"""Add a prepared-application row to the tracker for every vacancy folder.

One row per folder in vacancies/, created as soon as the CV exists — with the link to the
posting, the match score and the ATS coverage — so nothing has to be retyped at apply time.
The candidate fills in 'Applied on' when the application is actually sent; until then the row counts as
"Prepared, not yet sent" in the funnel.

One row per company, ever: if any vacancy from a company is already in the tracker, a second
one is not written. Applying to two roles at the same company is a decision made by hand, not
something the pipeline should queue up.

Idempotent: a folder already present is skipped, matched on the **folder path** it wrote into
Notes, and on the company name.

The key used to be the CV file name. That stopped working once every rendered CV got the same
name (jobprofile.cv_filename, CLAUDE.md rule 6) — one identical name in every folder would have made the first
tracked folder match all the others, and no row would ever have been added again. The folder
path is the natural key anyway: it is unique per vacancy, the script already writes it, and it
does not change when a file is renamed. Rows that carry no folder (tracked from LinkedIn only,
with prose in Notes) never collide with one.

Columns A:M only. Column N is an ARRAYFORMULA and writing it destroys the reply metrics.
Column B ("Applied on") is left empty on purpose — that column is what the funnel counts as
an application actually sent. The candidate fills it, or sheets_apply_mail.py does when the
employer's confirmation email arrives; a new row never carries it.

Tracks come from search_profile.yaml (`jobprofile.tracks()`): a second track (e.g. `bi`) gets
its own tab, same layout, while the default track writes `applications`. `--track` picks which
folders this call writes and which tab it writes them to. Every track's tab is READ for the
idempotency checks, so "one row per company, ever" spans the whole tracker, and a folder
tracked in another tab before a track was split off is never written a second time.

Usage:
    uv run scripts/sheets_add_applications.py                 # default track -> applications
    uv run scripts/sheets_add_applications.py --track bi      # a second track -> its own tab
    uv run scripts/sheets_add_applications.py --dry-run
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
ENV_FILE = Path.home() / ".job_search" / "sheets.env"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Matched by prefix against source.json's "source" field; anything unmatched falls through to
# "Board". A new collector needs its prefix here the day it ships: LinkedIn's first matched
# folder was labelled "Board" because this line was added a day after its collector.
SOURCE_LABEL = {"wantapply": "wantapply", "telegram": "Telegram", "linkedin": "LinkedIn",
                "wellfound": "Wellfound"}

# Trailing legal-form tokens, stripped so "Acme" and "Acme Inc." count as one company.
LEGAL_SUFFIXES = {"inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "s.l.", "sl",
                  "b.v.", "bv", "s.a.", "sa", "ag", "oy", "ab", "plc"}


TRACKS = jobprofile.tracks()
TABS = {t["name"]: t["tab"] for t in TRACKS}


def folder_track(folder: Path) -> str:
    """The track a folder belongs to, by its role family (search_profile.yaml tracks; a
    track's `family_aliases` catch older spellings). Older folders spell the family as a label
    ("Analytics Engineer") or leave it out; job.md's 'Role family:' line is the fallback."""
    fam = ""
    try:
        fam = str(json.loads((folder / "source.json").read_text(encoding="utf-8"))
                  .get("role_family") or "")
    except Exception:
        pass
    if not fam and (folder / "job.md").exists():
        m = re.search(r"Role family:\s*([^\n|]+)", (folder / "job.md").read_text(encoding="utf-8"))
        fam = m.group(1) if m else ""
    fam = re.sub(r"[\s-]+", "_", fam.strip().lower())
    for t in TRACKS[1:]:
        if fam in set(t["families"]) | set(t.get("family_aliases") or []):
            return t["name"]
    return TRACKS[0]["name"]


def norm_company(name: str) -> str:
    parts = re.sub(r"\s+", " ", (name or "").strip().lower()).split()
    if len(parts) > 1 and parts[-1].strip(",") in LEGAL_SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts).rstrip(",")


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


def coverage_from_report(folder: Path) -> str:
    """The ATS report records coverage as a bold percentage line; reuse it rather than
    re-running the lint."""
    rep = folder / "ats_report.md"
    if not rep.exists():
        return ""
    m = re.search(r"JD keyword coverage:\s*(\d+)%", rep.read_text(encoding="utf-8"))
    return f"{int(m.group(1)) / 100}" if m else ""


def row_for(folder: Path, added_on: str) -> list | None:
    src = folder / "source.json"
    if not src.exists():
        return None
    d = json.loads(src.read_text(encoding="utf-8"))
    own = folder / jobprofile.cv_filename()
    pdfs = [own] if own.exists() else sorted(folder.glob("CV_*.pdf"))
    if not pdfs:
        print(f"  ! {folder.name}: no CV yet, skipped", file=sys.stderr)
        return None
    # A blank Company cell matches no mail reply and slips past one-row-per-company: one row
    # carried " " and the company's rejection email was later applied to nothing.
    d["company"] = str(d.get("company") or "").strip()
    if not d["company"]:
        print(f"  ! {folder.name}: source.json has no company, skipped — fix it there",
              file=sys.stderr)
        return None
    raw = str(d.get("source", ""))
    label = next((v for k, v in SOURCE_LABEL.items() if raw.startswith(k)), "Board")
    cov = coverage_from_report(folder)
    return [
        added_on,                             # A  Date added
        "",                                   # B  Applied on — never on a new row
        d.get("company", ""),                 # C  Company
        d.get("title", ""),                   # D  Role
        label,                                # E  Source
        d.get("url", ""),                     # F  Link
        d.get("match_score", ""),             # G  Match score
        float(cov) if cov else "",            # H  ATS coverage
        pdfs[0].name,                         # I  CV file — always jobprofile.cv_filename;
                                              #    kept because an empty cell here
                                              #    is what marks a row with no CV at all
        "Prepared",                            # J  Status
        "",                                   # K  First reply on
        "",                                   # L  Stage reached
        str(folder.relative_to(ROOT)),        # M  Notes -> folder path
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--track", choices=sorted(TABS), default=TRACKS[0]["name"])
    args = ap.parse_args()
    tab = TABS[args.track]

    sid = jobprofile.spreadsheet_id()
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False).spreadsheets()

    today = date.today().isoformat()

    # Resolve columns by header NAME, never by hardcoded letter. Hardcoding cost a
    # mis-migration once: a guard read column B as "Applied on" while the sheet still had the
    # old layout where B was "Company", so a clear was refused and rows were appended below
    # stale ones. Reading the header first makes that class of mistake impossible.
    header = (svc.values().get(spreadsheetId=sid, range=f"{tab}!1:1")
              .execute().get("values", [[]]) or [[]])[0]
    try:
        col = {name: i for i, name in enumerate(header)}
        company_col, notes_col = col["Company"], col["Notes"]
        # Resolved but not read: this is the layout assertion. If either column has been
        # renamed or removed the KeyError below fires here, before anything is written,
        # instead of a row landing in the wrong place.
        _cv_col, _applied_col = col["CV file"], col["Applied on"]
    except KeyError as missing:
        sys.exit(f"{tab} tab has no {missing} column — run setup_sheets.py first.\n"
                 f"Header seen: {header}")
    if header[:2] != ["Date added", "Applied on"]:
        sys.exit(f"Unexpected column order: {header[:3]}. Run setup_sheets.py first.")

    def a1(idx: int) -> str:
        return chr(ord("A") + idx)

    # Read every column we write, not just the CV column.
    #
    # Reading one column to find the end of the table is what broke this once: rows
    # added by hand carried no CV file name, so the CV column looked shorter than the sheet
    # actually was, and this script appended straight over three live rows. The row count has
    # to come from the full block or any row with a gap in that one column is invisible.
    rows_now = (svc.values().get(spreadsheetId=sid, range=f"{tab}!A2:M")
                .execute().get("values", []))
    start = len(rows_now) + 2

    def cell(r: list, idx: int) -> str:
        return r[idx].strip() if len(r) > idx else ""

    # Keyed on the folder path in Notes, not on the CV file name — see the module docstring.
    # Read across BOTH tabs: one row per company spans the tracker, not one tab. The other
    # tab's columns are resolved from its own header, never assumed to match this one.
    tracked = [(rows_now, company_col, notes_col)]
    for other in (t for t in TABS.values() if t != tab):
        oh = (svc.values().get(spreadsheetId=sid, range=f"{other}!1:1")
              .execute().get("values", [[]]) or [[]])[0]
        if "Company" in oh and "Notes" in oh:
            orows = (svc.values().get(spreadsheetId=sid, range=f"{other}!A2:M")
                     .execute().get("values", []))
            tracked.append((orows, oh.index("Company"), oh.index("Notes")))
    # Only the first token: a note may follow the path ("vacancies/... | sent CV had Snowflake").
    # The path itself never contains a space.
    have_folders = {cell(r, n).split()[0] for rs, _, n in tracked for r in rs if cell(r, n)}
    have_companies = {norm_company(cell(r, c)) for rs, c, _ in tracked for r in rs
                      if cell(r, c)}

    # Highest match score first, so that when one company has several new folders the row
    # goes to the best of them. Alphabetical order once gave a company's row to its weakest
    # folder ("financial-data-analyst", 54) over its Senior Analytics Engineer (86).
    # The rows are written back in folder order below; only the choice depends on the score.
    def score(row: list) -> float:
        try:
            return float(row[6])
        except (TypeError, ValueError):
            return -1.0

    candidates = []
    for folder in sorted(ROOT.joinpath("vacancies").iterdir()):
        if not folder.is_dir() or folder_track(folder) != args.track:
            continue
        row = row_for(folder, today)
        if row:
            candidates.append((folder, row))
    candidates.sort(key=lambda fr: -score(fr[1]))

    rows = []
    for folder, row in candidates:
        if row[notes_col] in have_folders:
            print(f"  = {folder.name}: already tracked", file=sys.stderr)
            continue
        comp = norm_company(row[company_col])
        if comp in have_companies:
            print(f"  = {folder.name}: {row[company_col]} is already in the tracker, skipped",
                  file=sys.stderr)
            continue
        have_folders.add(row[notes_col])   # and one row per folder within a single run
        have_companies.add(comp)   # also one row per company within a single run
        rows.append(row)
        print(f"  + {row[2]} — {row[3]}  (score {row[6]})", file=sys.stderr)

    rows.sort(key=lambda r: r[notes_col])
    if not rows:
        print("Nothing to add.", file=sys.stderr)
        return 0
    if args.dry_run:
        print(f"--dry-run: {len(rows)} row(s) not written.", file=sys.stderr)
        return 0

    svc.values().update(
        spreadsheetId=sid,
        range=f"{tab}!A{start}:M{start + len(rows) - 1}",
        valueInputOption="USER_ENTERED", body={"values": rows}).execute()
    print(f"\nAdded {len(rows)} row(s) to {tab} at A{start}. "
          f"Fill in 'Applied on' when you actually send one.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
