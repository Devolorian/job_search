# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Turn a wantapply browser pass into a collector output the daily scan can read.

WHY THIS EXISTS, and why there is no scripts/collect_wantapply.py beside the other three.

Two facts, both measured 2026-09-18:

1. Cloudflare hard-blocks scripted HTTP. `/jobs/*`, a detail page and `/sitemap.xml` all
   return 403 "Attention Required! | Cloudflare" — to curl with a browser User-Agent, and
   again with the full Chrome header set (sec-ch-ua, Sec-Fetch-*, Accept-Language). It is a
   WAF block, not a JS challenge, so no amount of honest header-setting gets past it and
   defeating it is not something this project does. The site's own robots.txt *allows*
   `/jobs/*` — only `/api/`, `/admin-panel/` and the account paths are disallowed. So
   reading the listing in a real browser is permitted; scripting it is simply blocked.

2. A headless `claude -p` run has no browser tools at all. Verified directly:

       claude -p "...list your mcp__claude-in-chrome tools..." --allowedTools 'mcp__claude-in-chrome'
       -> NONE

   with the extension connected to an interactive session at that very moment, with
   `mcp__claude-in-chrome` in both the launchd allow-list and `.claude/settings.json`. The
   Chrome extension pairs with an interactive session; the scheduled run is not one.

Those two together mean the scheduled runs can never read wantapply, and every
"wantapply: skipped — extension not connected" line from 2026-09-07 to 2026-09-18 (20 runs)
was reporting a permanent condition as if it were a transient one.

The fix is to stop asking the scheduled run to do it. Collection happens in an interactive
session through `/wantapply`, which writes the records it read; this script normalises them
into `state/wantapply.jsonl` in the same shape the other collectors emit, with the same
`sha1(company|title)` dedup key so a posting already collected from a company board or from
LinkedIn collides in `state/seen.json` and never makes a second folder. The scheduled run
then reads the file like any other source and says how old it is.

Input: a JSON array of objects, on stdin or via --from. Fields, all strings except `posted`:
  title, company, url  (required)
  location, description, posted  (optional; `posted` ISO-8601 or a listing age like "3 days ago")

Usage:
  uv run scripts/wantapply_ingest.py --from state/_wantapply_raw.json --json state/wantapply.jsonl
  uv run scripts/wantapply_ingest.py --from raw.json --json state/wantapply.jsonl --hours 72
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import role_routing  # scripts/role_routing.py — which roles this source searches

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
PROFILE = jobprofile.search_profile_path()
SOURCES = jobprofile.SHARED_SOURCES
LAST_RUN = ROOT / "state/last_run.json"

# The same two-week first-run window every newly added source gets, armed on the
# absence of wantapply.first_run_utc in state/last_run.json. wantapply has never completed a
# run, so the first pass through this script is a first run by that definition — which is
# right: the source has no history to lean on.
FIRST_RUN_LOOKBACK_HOURS = 336

# A pass that is invoked by hand has no fixed cadence, so the ordinary window is "since the
# last successful pass" rather than the collectors' 30h. The slack absorbs a pass that ran a
# little early; re-reading a posting costs nothing because its key is already in seen.json.
ROUTINE_MIN_HOURS = 30
ROUTINE_SLACK_HOURS = 2

# One pass runs this script twice: once on the listing cards, then again with the descriptions
# fetched for the cards that survived. That is the documented flow, and a naive "window since
# last_run_utc" turns the second call into a five-minute window that throws the whole capture
# away — measured on 2026-09-18, when it dropped 8 of 22 records the first call had kept. So a
# call that lands within the grace period is the same pass continuing: it measures its window
# from the pass before, and does not move prev_run_utc.
REINGEST_GRACE_HOURS = 3

AGE_RE = re.compile(
    r"(?:(?P<n>\d+)\s*(?P<unit>minute|hour|day|week|month)s?\s*ago)|(?P<today>today|just now)",
    re.I)
UNIT_HOURS = {"minute": 1 / 60, "hour": 1, "day": 24, "week": 168, "month": 730}

LEGAL_SUFFIXES = {"inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "s.l.", "sl",
                  "b.v.", "bv", "s.a.", "sa", "ag", "oy", "ab", "plc"}


def normalise_company(name: str) -> str:
    """Byte-for-byte the rule collect_linkedin.py uses, so "Acme Inc." and "Acme" hash
    the same and the cross-source collision in seen.json actually happens."""
    parts = re.sub(r"\s+", " ", (name or "").strip()).split()
    if len(parts) > 1 and parts[-1].lower().strip(",") in LEGAL_SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts).rstrip(",")


def key_of(rec: dict) -> str:
    """Identical to collect_boards.py and collect_linkedin.py, deliberately. This must never
    drift — the cross-source collision is the entire point."""
    return hashlib.sha1(
        f"{rec['company'].strip().lower()}|{rec['title'].strip().lower()}".encode()
    ).hexdigest()


def title_gate(title: str, profile: dict) -> str | None:
    """Return the matching role family, or None. Same gate as the other collectors: the
    family titles are substrings of the posting title, and title_exclusions hard-drop."""
    t = (title or "").lower()
    for bad in profile.get("title_exclusions", []):
        if bad.lower() in t:
            return None
    # Only the roles profile/routing.yaml routes to wantapply.
    for family, spec in role_routing.apply(profile, "wantapply").get("role_families", {}).items():
        if any(x.lower() in t for x in spec.get("titles", [])):
            return family
    return None


def parse_posted(value: str, now: datetime) -> str:
    """ISO-8601 straight through; a listing age ("3 days ago", "about 21 hours ago") becomes
    a timestamp. Anything unrecognised returns "" — an unknown date must not masquerade as
    now, or the freshness window silently keeps everything."""
    v = (value or "").strip()
    if not v:
        return ""
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(
            timezone.utc).isoformat()
    except ValueError:
        pass
    m = AGE_RE.search(v)
    if not m:
        return ""
    if m.group("today"):
        return now.isoformat()
    hours = int(m.group("n")) * UNIT_HOURS[m.group("unit").lower()]
    return (now - timedelta(hours=hours)).isoformat()


def hours_since(stamp: str) -> float:
    return (datetime.now(timezone.utc)
            - datetime.fromisoformat(stamp)).total_seconds() / 3600


def is_reingest(state: dict) -> bool:
    """True when this call continues the pass that just ran, rather than starting a new one."""
    last = state.get("wantapply", {}).get("last_run_utc")
    return bool(last) and hours_since(last) < REINGEST_GRACE_HOURS


def window_hours(args, state: dict) -> tuple[float, str]:
    if args.hours:
        return float(args.hours), "explicit --hours"
    wa = state.get("wantapply", {})
    if not wa.get("first_run_utc"):
        return float(FIRST_RUN_LOOKBACK_HOURS), "first run (no wantapply.first_run_utc)"
    if is_reingest(state):
        prev = wa.get("prev_run_utc")
        if not prev:
            return float(FIRST_RUN_LOOKBACK_HOURS), "re-ingest of the first pass"
        delta = hours_since(prev)
        return (max(ROUTINE_MIN_HOURS, delta + ROUTINE_SLACK_HOURS),
                f"re-ingest, {delta:.0f}h since the pass before")
    last = wa.get("last_run_utc")
    if not last:
        return float(FIRST_RUN_LOOKBACK_HOURS), "no last_run_utc recorded"
    delta = hours_since(last)
    return max(ROUTINE_MIN_HOURS, delta + ROUTINE_SLACK_HOURS), f"{delta:.0f}h since last pass"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", help="JSON array of records; default stdin")
    ap.add_argument("--json", required=True, help="output .jsonl")
    ap.add_argument("--hours", type=float, help="freshness window; wins over both defaults")
    ap.add_argument("--all", action="store_true", help="skip the title gate (triage only)")
    args = ap.parse_args()

    raw = Path(args.src).read_text() if args.src else sys.stdin.read()
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"!! input is not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(records, list):
        print("!! input must be a JSON array of records", file=sys.stderr)
        return 2

    profile = jobprofile.load_search_profile()
    state = json.loads(LAST_RUN.read_text()) if LAST_RUN.exists() else {}
    hours, why = window_hours(args, state)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    print(f"  window {hours:.0f}h ({why})", file=sys.stderr)

    kept, dropped_title, dropped_age, malformed = [], [], [], 0
    for r in records:
        if not isinstance(r, dict) or not r.get("title") or not r.get("company"):
            malformed += 1
            continue
        rec = {
            "title": str(r.get("title", "")).strip(),
            "company": normalise_company(str(r.get("company", ""))),
            "location": str(r.get("location", "")).strip(),
            "url": str(r.get("url", "")).strip(),
            "description": str(r.get("description", "")).strip(),
            "posted": parse_posted(str(r.get("posted", "")), now),
            "source": "wantapply",
        }
        family = title_gate(rec["title"], profile)
        if not args.all and not family:
            dropped_title.append(f"{rec['company']} — {rec['title'][:50]}")
            continue
        rec["role_family"] = family or ""
        # An unparseable date is kept on purpose. Dropping it would silently lose a posting
        # because wantapply reworded its age label, which is the wrong way round to fail.
        if rec["posted"]:
            try:
                if datetime.fromisoformat(rec["posted"]) < cutoff:
                    dropped_age.append(f"{rec['company']} — {rec['title'][:40]}")
                    continue
            except ValueError:
                pass
        rec["dedup_key"] = key_of(rec)
        kept.append(rec)

    seen_keys, unique = set(), []
    for rec in kept:
        if rec["dedup_key"] in seen_keys:
            continue
        seen_keys.add(rec["dedup_key"])
        unique.append(rec)

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for rec in unique:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\n  {len(unique)} records written to {out} "
          f"({len(records)} in, {len(dropped_title)} off-title, {len(dropped_age)} too old, "
          f"{len(kept) - len(unique)} same-key duplicates, {malformed} malformed)",
          file=sys.stderr)
    for label, items in (("off-title", dropped_title), ("too old", dropped_age)):
        for x in items[:20]:
            print(f"      {label}: {x}", file=sys.stderr)

    # Markers written LAST, after the output file exists — the pattern every source in this
    # project follows. A crash before this point leaves the next pass still treating itself
    # as the first, rather than silently narrowing its window to nothing.
    state.setdefault("wantapply", {})
    if not is_reingest(state) and state["wantapply"].get("last_run_utc"):
        state["wantapply"]["prev_run_utc"] = state["wantapply"]["last_run_utc"]
    state["wantapply"].setdefault("first_run_utc", now.isoformat())
    state["wantapply"]["last_run_utc"] = now.isoformat()
    state["wantapply"]["last_count"] = len(unique)
    state["wantapply"].pop("last_seen_urls", None)
    LAST_RUN.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
