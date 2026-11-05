#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Search-engine hits on ATS domains -> verified company boards in state/discovered_boards.json.

Built from the "search ATS platforms with Google" tip:
`site:jobs.ashbyhq.com "Analytics Engineer"` and the same for Greenhouse, Lever and Workable.
The search itself is done by the model (prompts/board_discovery.md, WebSearch) because there is
no search API this project may call from a script: Google, Bing and DuckDuckGo all disallow
/search in robots.txt, and Google's Custom Search JSON API is closed to new customers.

What a hit is worth is not the posting — search snapshots run weeks behind the board — but the
BOARD it sits on. `jobs.ashbyhq.com/elevenlabs/<id>` names the slug exactly, with none of the
guessing `resolve_board_slugs.py` has to guard against: the URL is the board. So a new slug is
verified once against the provider's API, written to state/, and from then on
`collect_boards.py` polls the whole board every run, with the same title and geography gates as
every other company. The search finds companies; the boards supply the postings.

state/, not config/companies.yaml: the config is curated by hand, and a runtime discovery belongs
beside board_overrides.json. A board that later 404s is healed or retired by the same mechanism.

Usage:
    uv run scripts/discover_boards.py --due            # exit 0 if a discovery pass is due, 3 if not
    uv run scripts/discover_boards.py --from state/_discovery_hits.json
    uv run scripts/discover_boards.py --list           # what has been discovered so far

`--from` takes a JSON list of {"url": ..., "title": ..., "query": ...} — the raw search results.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
SOURCES = jobprofile.SHARED_SOURCES
COMPANIES = jobprofile.COMPANIES
OVERRIDES = ROOT / "state" / "board_overrides.json"
STATE = ROOT / "state" / "discovered_boards.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"}

# Verbatim from collect_boards.py, as the other collectors copy what they need from it.
PROVIDERS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "ashby":      "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
    "lever":      "https://api.lever.co/v0/postings/{slug}?mode=json",
    "workable":   "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
    "recruitee":  "https://{slug}.recruitee.com/api/offers/",
    "personio":   "https://{slug}.jobs.personio.de/search.json",
    "teamtailor": "https://{slug}.teamtailor.com/jobs.json",
}

# Path segments that sit where a slug would and are not one.
NOT_A_SLUG = {"embed", "api", "j", "jobs", "v1", "careers", "search", "job_app", "job_board"}

# A rejected slug is not re-verified for this long — the same idea as board_overrides.json's
# seven days for a dead board, longer because a hit that failed verification is weaker evidence
# than a board that once worked.
REJECT_TTL = timedelta(days=30)


def fetch(url: str, timeout: int = 20, retries: int = 2):
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            data = urllib.request.urlopen(req, timeout=timeout).read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return json.loads(data)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last = exc
                continue
            raise
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                last = exc
                continue
            raise
    raise last if last else RuntimeError("unreachable")


def extract_jobs(provider: str, j):
    if provider in ("lever", "personio", "teamtailor"):
        return j if isinstance(j, list) else []
    if provider == "smartrecruiters":
        return j.get("content") or []
    if provider == "recruitee":
        return j.get("offers") or []
    return j.get("jobs") or []


def parse_board(url: str) -> tuple[str, str] | None:
    """(provider, slug) for an ATS URL, or None.

    Greenhouse's EU instance (job-boards.eu.greenhouse.io) is answered by the main board API —
    verified 2026-10-21 on currenciesdirect — so it parses as plain greenhouse. Lever's EU
    instance (jobs.eu.lever.co) is NOT: it has its own API host that collect_boards.py does not
    speak, so those hits are reported and skipped rather than recorded as boards that 404.
    """
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    first = parts[0] if parts else ""
    if re.fullmatch(r"(boards|job-boards)(\.eu)?\.greenhouse\.io", host):
        slug = parse_qs(u.query).get("for", [""])[0] if first == "embed" else first
        return ("greenhouse", slug) if slug else None
    if host == "jobs.lever.co" and first:
        return "lever", first
    if host == "jobs.ashbyhq.com" and first:
        return "ashby", first
    if host == "apply.workable.com" and first:
        return "workable", first
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and first:
        return "smartrecruiters", first
    for prov, pat in (("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
                      ("personio", r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
                      ("teamtailor", r"([a-z0-9-]+)\.teamtailor\.com")):
        m = re.fullmatch(pat, host)
        if m and m.group(1) not in ("www", "app", "career", "careers"):
            return prov, m.group(1)
    return None


def name_from_title(provider: str, title: str) -> str:
    """The company's display name as the search result titled it, when the provider says it.

    It matters for the dedup key, sha1(company|title): a board polled as "eventbriteinc" makes a
    record that never collides with LinkedIn's "Eventbrite". Ashby titles its pages
    "Role @ Company"; Workable "Role - Company"; Greenhouse's API names the board owner itself.
    """
    title = (title or "").strip()
    if provider == "ashby" and " @ " in title:
        return title.rsplit(" @ ", 1)[1].strip()
    if provider == "lever":
        # "Airalo - Senior Analytics Engineer" on a posting, "MoonPay jobs" on the board itself.
        if " - " in title:
            return title.split(" - ", 1)[0].strip()
        if title.lower().endswith(" jobs"):
            return title[:-5].strip()
        return ""
    if provider == "workable" and " - " in title:
        bits = [b.strip() for b in title.split(" - ")]
        if bits[-1].lower() == "application":
            bits = bits[:-1]
        if len(bits) >= 2:
            return bits[-1]
    return ""


def verify(provider: str, slug: str) -> tuple[bool, int, str, str]:
    """(is a live board, postings, owner name, reason). Same evidence rule as collect_boards:
    an answer in the provider's own shape is the board existing — except SmartRecruiters, which
    answers 200 with an empty list for any slug at all and so counts only with a posting."""
    try:
        j = fetch(PROVIDERS[provider].format(slug=slug), timeout=20)
    except urllib.error.HTTPError as exc:
        return False, 0, "", f"HTTP {exc.code}"
    except Exception as exc:
        return False, 0, "", type(exc).__name__
    n = len(extract_jobs(provider, j))
    if provider == "smartrecruiters" and not n:
        return False, 0, "", "smartrecruiters answers empty for any slug"
    if provider not in ("lever", "personio", "teamtailor") and not isinstance(j, dict):
        return False, 0, "", "unexpected payload"
    owner = ""
    if provider == "workable":
        owner = j.get("name") or ""
    elif provider == "greenhouse":
        try:
            owner = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=15).get("name") or ""
        except Exception:
            pass
    return True, n, owner, ""


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"last_run_utc": None, "boards": {}, "rejected": {}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", type=Path, help="JSON list of search hits")
    ap.add_argument("--due", action="store_true", help="exit 0 if a pass is due, 3 if not")
    ap.add_argument("--list", action="store_true", help="print discovered boards")
    args = ap.parse_args()

    cfg = (jobprofile.load_sources()["sources"].get("board_discovery") or {})
    state = load_state()
    now = datetime.now(timezone.utc)

    if args.due:
        last = state.get("last_run_utc")
        every = timedelta(hours=cfg.get("every_hours", 20))
        if not cfg.get("enabled", True):
            print("board discovery: disabled in config/sources.yaml")
            return 3
        if last and now - datetime.fromisoformat(last) < every:
            print(f"board discovery: not due — last pass {last}, runs every {every.total_seconds()/3600:.0f}h")
            return 3
        print(f"board discovery: due (last pass {last or 'never'})")
        return 0

    if args.list:
        for slug, b in sorted(state["boards"].items()):
            print(f"{b['provider']:<15} {slug:<28} {b.get('name') or '':<28} jobs={b.get('jobs')}  found {b['found_utc'][:10]}")
        print(f"{len(state['boards'])} discovered, {len(state['rejected'])} rejected")
        return 0

    if not args.src:
        ap.error("--from is required")
    # A collector's JSONL (collect_euremotejobs.py) is accepted too: its records carry the
    # employer's own ATS link as `apply_url` and the employer's name. Reading one is not a
    # search pass, so it leaves the --due clock alone.
    from_collector = args.src.suffix == ".jsonl"
    if from_collector:
        recs = [json.loads(l) for l in args.src.read_text(encoding="utf-8").splitlines() if l.strip()]
        hits = [{"url": r["apply_url"], "name": r.get("company", ""), "query": r.get("source", "")}
                for r in recs if r.get("apply_url")]
    else:
        hits = json.loads(args.src.read_text(encoding="utf-8"))

    companies = yaml.safe_load(COMPANIES.read_text(encoding="utf-8"))["companies"]
    known = {c["slug"].lower() for c in companies}
    if OVERRIDES.exists():
        known |= {s.lower() for s in json.loads(OVERRIDES.read_text())}
    known |= {s.lower() for s in state["boards"]}
    ignore = {s.lower() for s in cfg.get("ignore_slugs", [])}
    recent_reject = {s for s, r in state["rejected"].items()
                     if now - datetime.fromisoformat(r["checked_utc"]) < REJECT_TTL}

    candidates: dict[str, dict] = {}
    unparsed = lever_eu = 0
    for h in hits:
        url = h.get("url", "")
        if "jobs.eu.lever.co" in url:
            lever_eu += 1
            continue
        pb = parse_board(url)
        if not pb or pb[1].lower() in NOT_A_SLUG:
            unparsed += 1
            continue
        prov, slug = pb
        key = slug.lower()
        if key in known or key in ignore or key in recent_reject:
            continue
        c = candidates.setdefault(slug, {"provider": prov, "url": url, "query": h.get("query", ""),
                                         "name": "", "hits": 0})
        c["hits"] += 1
        c["name"] = c["name"] or h.get("name") or name_from_title(prov, h.get("title", ""))

    already = len({(parse_board(h.get("url", "")) or ("", ""))[1].lower() for h in hits} & known)
    print(f"{len(hits)} hits: {len(candidates)} new board(s) to verify, {already} already polled, "
          f"{unparsed} not an ATS board URL" + (f", {lever_eu} on Lever EU (unsupported)" if lever_eu else ""),
          file=sys.stderr)

    added = rejected = 0
    stamp = now.isoformat(timespec="seconds")
    for slug, c in sorted(candidates.items()):
        ok, n, owner, reason = verify(c["provider"], slug)
        time.sleep(0.5)
        if not ok:
            state["rejected"][slug] = {"provider": c["provider"], "checked_utc": stamp,
                                       "reason": reason, "url": c["url"]}
            rejected += 1
            print(f"  - {c['provider']}/{slug}: rejected ({reason})", file=sys.stderr)
            continue
        state["boards"][slug] = {"provider": c["provider"], "name": owner or c["name"],
                                 "found_utc": stamp, "url": c["url"], "query": c["query"], "jobs": n}
        state["rejected"].pop(slug, None)
        added += 1
        print(f"  + {c['provider']}/{slug} ({owner or c['name'] or 'no name'}): {n} posting(s)",
              file=sys.stderr)

    if not from_collector:
        state["last_run_utc"] = stamp
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  {added} board(s) added, {rejected} rejected; {len(state['boards'])} discovered in total "
          f"-> {STATE.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
