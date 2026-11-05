#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "pyyaml"]
# ///
"""One-time setup of the Job Search Tracker spreadsheet.

Creates the three tabs and their formulas, and records the spreadsheet id in
config/sources.yaml. Uses the Google API directly rather than the MCP server: this runs
once, and a direct call gives clearer errors when the sheet has not been shared with the
service account (by far the most common failure).

The daily run uses the MCP server, not this script.

Usage:
    uv run scripts/setup_sheets.py             # find the sheet, build the tabs
    uv run scripts/setup_sheets.py --find      # just report what the service account can see
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
CONFIG = jobprofile.SHARED_SOURCES
ENV_FILE = Path.home() / ".job_search" / "sheets.env"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

# Column layout, and the two rules that come with it:
#
#   A  Date added      written by the pipeline when the folder's CV is rendered
#   B  Applied on      the candidate by hand, or an employer's confirmation email (sheets_apply_mail.py)
#                      -- this is what the funnel counts as "sent"
#   C  Company         D  Role            E  Source        F  Link
#   G  Match score     H  ATS coverage    I  CV file       J  Status
#   K  First reply on  L  Stage reached   M  Notes
#   N  Days to reply   DERIVED -- an ARRAYFORMULA, must stay the last column
#
# "Days to reply" is last on purpose: anything writing a whole row would otherwise overwrite
# the formula with a literal, which is exactly what happened the first time this was tested.
# Writers touch A:M only, and never write B.
APPLICATIONS_HEADER = [
    "Date added", "Applied on", "Company", "Role", "Source", "Link", "Match score",
    "ATS coverage", "CV file", "Status", "First reply on", "Stage reached", "Notes",
    "Days to reply",
]
DAILY_STATS_HEADER = [
    "Date", "Scanned", "New", "Matched", "Folders created", "Errors", "Note", "Mail events",
]
# Written by sheets_apply_mail.py, one row per employer email it acted on.
MAIL_LOG_HEADER = ["Logged on", "Email date", "Company", "Role", "Event", "From", "Subject",
                   "Change", "Evidence"]


def creds():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))
    path = os.environ.get("SERVICE_ACCOUNT_PATH", "")
    if not path or not Path(path).exists():
        sys.exit(f"SERVICE_ACCOUNT_PATH not set or missing (looked in {ENV_FILE}).")
    info = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"Service account: {info['client_email']}")
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES), info


def find_sheet(drive, title: str) -> list[dict]:
    q = ("mimeType='application/vnd.google-apps.spreadsheet' and trashed=false")
    res = drive.files().list(q=q, fields="files(id,name,owners,modifiedTime)",
                             includeItemsFromAllDrives=True, supportsAllDrives=True,
                             pageSize=100).execute()
    return res.get("files", [])


def ensure_tabs(sheets, sid: str, wanted: list[str]) -> dict[str, int]:
    meta = sheets.spreadsheets().get(spreadsheetId=sid).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta["sheets"]}
    requests = [{"addSheet": {"properties": {"title": t}}}
                for t in wanted if t not in existing]
    if requests:
        sheets.spreadsheets().batchUpdate(spreadsheetId=sid,
                                          body={"requests": requests}).execute()
        meta = sheets.spreadsheets().get(spreadsheetId=sid).execute()
        existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                    for s in meta["sheets"]}
        print(f"  created tab(s): {', '.join(r['addSheet']['properties']['title'] for r in requests)}")
    # The default "Sheet1" tab (its name is localised, e.g. in a Russian-locale account) is noise
    # once the real tabs exist.
    for name, gid in existing.items():
        if name not in wanted and re.fullmatch(r"(Sheet|Лист)\s*1", name):
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"deleteSheet": {"sheetId": gid}}]}).execute()
            print(f"  removed default tab {name!r}")
    return existing


def write(sheets, sid: str, rng: str, values: list[list]) -> None:
    sheets.spreadsheets().values().update(
        spreadsheetId=sid, range=rng, valueInputOption="USER_ENTERED",
        body={"values": values}).execute()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--find", action="store_true", help="report visible spreadsheets and exit")
    ap.add_argument("--id", help="use this spreadsheet id instead of searching by name")

    args = ap.parse_args()

    cred, info = creds()
    drive = build("drive", "v3", credentials=cred, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=cred, cache_discovery=False)

    cfg = jobprofile.load_sources()
    want_title = cfg["destinations"]["google_sheets"]["spreadsheet_title"]

    if args.id:
        sid = args.id
    else:
        files = find_sheet(drive, want_title)
        if args.find:
            print(f"\n{len(files)} spreadsheet(s) visible to the service account:")
            for f in files:
                print(f"  {f['id']}  {f['name']}")
            return 0
        match = [f for f in files if f["name"].strip().lower() == want_title.strip().lower()]
        if not match:
            print(f"\nNo spreadsheet named {want_title!r} is reachable.", file=sys.stderr)
            print(f"Visible to the service account right now: "
                  f"{', '.join(repr(f['name']) for f in files) or 'nothing at all'}",
                  file=sys.stderr)
            print("\nThe service account cannot create the sheet itself: Google removed Drive"
                  "\nstorage quota from service accounts, so it can only work on a file someone"
                  "\nelse owns and shares with it. In your own Drive:", file=sys.stderr)
            print(f"\n  1. Create a spreadsheet named exactly: {want_title}", file=sys.stderr)
            print(f"  2. Share -> paste: {info['client_email']}", file=sys.stderr)
            print("  3. Role: Editor. Untick 'Notify people' (it cannot receive email).",
                  file=sys.stderr)
            print("\nThen re-run this script. Or pass --id <spreadsheet id from the URL> if the"
                  "\nsheet is named something else.", file=sys.stderr)
            return 1
        if len(match) > 1:
            print(f"More than one sheet named {want_title!r}; pass --id to pick:", file=sys.stderr)
            for f in match:
                print(f"  {f['id']}", file=sys.stderr)
            return 1
        sid = match[0]["id"]

    print(f"Spreadsheet: {sid}")

    # Formula argument separators are locale-dependent: a ru_RU spreadsheet wants ";" and
    # rejects the "," in COUNTIF(range, "x"). Pinning en_US keeps the formulas below portable.
    # Dates are then written explicitly as yyyy-mm-dd, which is unambiguous in any locale and
    # is what the daily scan writes anyway.
    sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{
        "updateSpreadsheetProperties": {
            "properties": {"locale": "en_US", "timeZone": jobprofile.timezone()},
            "fields": "locale,timeZone"}}]}).execute()

    # One applications tab and one daily_stats tab per track (search_profile.yaml tracks), all
    # in the same layout, so every rule about the applications tab (A:M only, never B, N is a
    # formula) holds on each. The funnel reads the default track's tabs.
    tracks = jobprofile.tracks()
    app_tabs = [t["tab"] for t in tracks]
    stats_tabs = [t["stats_tab"] for t in tracks]
    ensure_tabs(sheets, sid, [app_tabs[0], stats_tabs[0], "funnel", "mail_log",
                              *app_tabs[1:], *stats_tabs[1:]])

    for tab in app_tabs:
        write(sheets, sid, f"{tab}!A1", [APPLICATIONS_HEADER])
        # Derived, not typed: a hand-entered day count is wrong exactly when someone forgets.
        # Clear the whole column first. Found 2026-09-30: the formula had drifted to N3 (a row
        # deleted above it), so row 2 had no days-to-reply, and writing N2 on top of a second
        # ARRAYFORMULA below would only have produced #REF!.
        sheets.spreadsheets().values().clear(spreadsheetId=sid, range=f"{tab}!N2:N").execute()
        write(sheets, sid, f"{tab}!N2",
              [['=ARRAYFORMULA(IF(LEN(B2:B)*LEN(K2:K)=0,"",K2:K-B2:B))']])
    for tab in stats_tabs:
        write(sheets, sid, f"{tab}!A1", [DAILY_STATS_HEADER])
    write(sheets, sid, "mail_log!A1", [MAIL_LOG_HEADER])

    # NOTE on "Applications sent": rows are pre-created by the scan for every vacancy folder,
    # before anything is sent. So the count MUST key off column A ("Applied on"), not column B
    # ("Company") — counting companies would report every prepared row as an application and
    # silently inflate the whole funnel. Same reason the per-source counts require A to be set.
    A = f"{app_tabs[0]}!"
    D = stats_tabs[0]
    def by_source(label: str, key: str) -> list[str]:
        # "Prepared" means a CV exists (column I), which is what the scan produces; rows
        # tracked by hand from LinkedIn with no CV are not something the scan prepared.
        return [label, f'=COUNTIFS({A}E2:E,"{key}",{A}I2:I,"<>")',
                f'=COUNTIFS({A}E2:E,"{key}",{A}B2:B,"<>")',
                f'=COUNTIFS({A}E2:E,"{key}",{A}J2:J,"Rejected")']

    funnel = [
        ["Job search funnel", "", ""],
        ["Recomputed live from the applications tab. Do not type into column B.", "", ""],
        ["A row exists as soon as the scan prepares a CV; it counts as SENT once 'Applied on' "
         "is filled - by you, or by the employer's confirmation email.", "", ""],
        ["Metric", "Value", "Notes"],
        ["Prepared, not yet sent", f'=COUNTIFS({A}C2:C,"<>",{A}B2:B,"")',
         "folders with a CV waiting on you"],                                            # 5
        ["Applications sent", f'=COUNTA({A}B2:B)', "rows with an 'Applied on' date"],  # 6
        ["Awaiting reply", f'=COUNTIF({A}J2:J,"Applied")',
         "confirmed by email, no answer yet"],                                           # 7
        # COUNT, not COUNTA: K has hand notes in it ("lead position"), which are not replies.
        ["Replies received", f'=COUNT({A}K2:K)', "rows with a first-reply date"],       # 8
        ["Reply rate", '=IFERROR(B8/B6,"")', "replies / applications sent"],            # 9
        ["Rejections", f'=COUNTIF({A}J2:J,"Rejected")', "status from mail or by hand"],  # 10
        ["Rejection rate", '=IFERROR(B10/B6,"")', "rejections / applications sent"],    # 11
        # Stage reached is the furthest stage: an offer went through an interview, and a
        # rejection after an interview leaves it at Interview (sheets_apply_mail.py).
        ["Interviews", f'=COUNTIF({A}L2:L,"Interview")+COUNTIF({A}L2:L,"Offer")',
         "reached at least one interview"],                                              # 12
        ["Offers", f'=COUNTIF({A}L2:L,"Offer")', ""],                                   # 13
        ["Interview rate", '=IFERROR(B12/B6,"")', ""],                                  # 14
        ["Offer rate", '=IFERROR(B13/B6,"")', ""],                                      # 15
        ["Median days to reply", f'=IFERROR(MEDIAN({A}N2:N),"")', ""],                  # 16
        ["", "", ""],
        ["By source", "Prepared", "Sent", "Rejected"],                                   # 18
        by_source("Telegram", "Telegram"),
        by_source("wantapply", "wantapply"),
        by_source("Company board", "Board"),
        by_source("LinkedIn", "LinkedIn"),
        by_source("Wellfound", "Wellfound"),                                             # 23
        ["", "", ""],
        ["Scan activity", "", ""],
        # Several runs a day means several rows per date, so days must be counted distinctly —
        # COUNTA here would silently multiply the number of days.
        ["Days scanned", f'=IFERROR(COUNTUNIQUE({D}!A2:A),0)', "distinct dates"],  # 26
        ["Runs", f'=COUNTA({D}!A2:A)', "scheduled runs"],
        ["Vacancies seen", f'=SUM({D}!B2:B)', ""],
        ["Matched", f'=SUM({D}!D2:D)', ""],
        ["Sent / prepared", '=IFERROR(B6/(B5+B6),"")', "how much of what is ready you act on"],
        ["Emails applied", '=COUNTA(mail_log!A2:A)', "employer emails acted on; see mail_log"],  # 31
    ]
    # Cleared first: the layout grew, and a shorter old layout must not leave rows behind.
    sheets.spreadsheets().values().clear(spreadsheetId=sid, range="funnel!A1:E60").execute()
    write(sheets, sid, "funnel!A1", funnel)
    # Make the write contract visible in the sheet itself, not only in the code.
    write(sheets, sid, "funnel!E1",
          [["applications: write columns A:M only - column N is a formula."]])

    meta = sheets.spreadsheets().get(spreadsheetId=sid).execute()
    gids = {sh["properties"]["title"]: sh["properties"]["sheetId"] for sh in meta["sheets"]}

    requests = []
    # Percentages read better than raw decimals for the rate rows — but the non-rate rows must
    # be reset to plain numbers in the same pass. Formatting is sticky: when the funnel layout
    # shifted by a row, the old PERCENT format stayed behind on "Vacancies seen" and rendered
    # 3352 as 335200%. Setting both halves every time makes this idempotent.
    PCT_ROWS = (9, 11, 14, 15, 30)      # the five rates, Sent/prepared last
    PLAIN_ROWS = (5, 6, 7, 8, 10, 12, 13, 16, 19, 20, 21, 22, 23, 26, 27, 28, 29, 31)

    def _fmt(row: int, number_format: dict) -> dict:
        return {"repeatCell": {
            "range": {"sheetId": gids["funnel"], "startRowIndex": row - 1, "endRowIndex": row,
                      "startColumnIndex": 1, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"numberFormat": number_format}},
            "fields": "userEnteredFormat.numberFormat"}}

    requests += [_fmt(r, {"type": "PERCENT", "pattern": "0.0%"}) for r in PCT_ROWS]
    requests += [_fmt(r, {"type": "NUMBER", "pattern": "0"}) for r in PLAIN_ROWS]
    # ISO dates in the date columns, and bold headers so the sheet is usable by hand.
    for tab, cols in ([(t, [(0, 2), (10, 11)]) for t in app_tabs]
                      + [(t, [(0, 1)]) for t in stats_tabs] + [("mail_log", [(0, 2)])]):
        for c0, c1 in cols:
            requests.append({"repeatCell": {
                "range": {"sheetId": gids[tab], "startRowIndex": 1,
                          "startColumnIndex": c0, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE",
                                                               "pattern": "yyyy-mm-dd"}}},
                "fields": "userEnteredFormat.numberFormat"}})
        requests.append({"repeatCell": {
            "range": {"sheetId": gids[tab], "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold"}})
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": gids[tab],
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}})
    sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests}).execute()

    # The id belongs to the candidate, so it goes into the profile overlay, never into the
    # shared config/sources.yaml.
    overlay = jobprofile.sources_overlay_path()
    doc = yaml.safe_load(overlay.read_text(encoding="utf-8")) if overlay.exists() else None
    if ((doc or {}).get("destinations") or {}).get("google_sheets", {}).get("spreadsheet_id") != sid:
        with overlay.open("a", encoding="utf-8") as fh:
            if doc and "destinations" in doc:
                print(f"  ! set destinations.google_sheets.spreadsheet_id: \"{sid}\" in {overlay}",
                      file=sys.stderr)
            else:
                fh.write(f'\ndestinations:\n  google_sheets:\n    spreadsheet_id: "{sid}"\n')

    print(f"\nTabs ready: {', '.join(app_tabs + stats_tabs)}, funnel, mail_log")
    print(f"https://docs.google.com/spreadsheets/d/{sid}/edit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
