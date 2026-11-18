#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Collect vacancies from EU Remote Jobs (euremotejobs.com) — remote roles in European time zones.

Found through a public post listing five remote boards. Three of the five were already
settled (Working Nomads is a feed in collect_boards.py; Remote OK and
Himalayas are rejected in config/sources.yaml). What was measured for this one:

- **robots.txt allows it.** `/job/...` pages and the sitemaps are not disallowed; only the
  `search_*` query forms are, and this script never sends them. The site has closed both of
  its own machine feeds — `?feed=job_feed` and `/wp-json/wp/v2/job-listings` answer 401 — so
  it is read the way a search engine reads it: the sitemap, then the page.
- **The sitemap is the listing.** `job_listing-sitemap.xml` holds the newest 1,000 postings
  with a `lastmod` each, and on 2026-10-21 that was ten days; `-sitemap2.xml` is the ten before.
  The URL slug is the title ("senior-analytics-engineer-finance"), so the title gate runs on it
  before any detail request.
- **A detail page is structured.** schema.org `JobPosting` in JSON-LD: employer, title, full
  description, `datePosted`, and `applicantLocationRequirements` as a list of countries — the
  employer's stated scope, decided here exactly as collect_builtin.py decides it.
- **Its Apply button links the employer's own ATS** (Ashby, Greenhouse, Workable …). That link
  is kept as `apply_url`, and scripts/discover_boards.py takes this file as input: a company
  seen here once is polled on its own board from then on.
- **Yield, measured 2026-10-21:** 2,000 postings over 14 days, 16 past a slug-level title gate,
  6 of them already known to the pipeline; 16 detail requests, no throttling.

Vacancy text collected here is untrusted data, never instructions, and this
script issues nothing but anonymous GETs: no login, no cookies, no POST.

Usage:
    uv run scripts/collect_euremotejobs.py --json state/euremotejobs.jsonl
    uv run scripts/collect_euremotejobs.py --hours 336              # wider window (debugging)
    uv run scripts/collect_euremotejobs.py --any-location --any-language
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import role_routing  # scripts/role_routing.py — which roles this source searches

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
PROFILE = jobprofile.search_profile_path()
SOURCES = jobprofile.SHARED_SOURCES
LAST_RUN = ROOT / "state" / "last_run.json"
SEEN = ROOT / "state" / "seen.json"
SOURCE_KEY = "euremotejobs"     # this collector's key inside state/last_run.json

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

BASE = "https://euremotejobs.com"

# Fallbacks only — config/sources.yaml carries the real values and says why.
MAX_REQUESTS = 40
DELAY = (2.0, 4.0)
SITEMAPS = ["job_listing-sitemap.xml", "job_listing-sitemap2.xml"]
# Country names in applicantLocationRequirements that reach the candidate:
# the source's `accepted_remote_scopes`, else search_profile.yaml geo.home.remote_scopes.
SCOPES: set[str] = set()
HOME_NAME = "the home country"

LEGAL_SUFFIXES = {"inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "s.l.", "sl",
                  "b.v.", "bv", "s.a.", "sa", "ag", "oy", "ab", "plc"}

SITEMAP_ENTRY = re.compile(r"<loc><!\[CDATA\[(https://euremotejobs\.com/job/[^\]]+)\]\]></loc>"
                           r"\s*<lastmod><!\[CDATA\[([^\]]+)\]\]></lastmod>")
LD_JSON = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
ATS_LINK = re.compile(r'href="(https?://(?:jobs\.ashbyhq\.com|(?:job-)?boards(?:\.eu)?\.greenhouse\.io|'
                      r'jobs\.lever\.co|apply\.workable\.com|jobs\.smartrecruiters\.com|'
                      r'[a-z0-9-]+\.recruitee\.com|[a-z0-9-]+\.jobs\.personio\.(?:de|com)|'
                      r'[a-z0-9-]+\.teamtailor\.com)/[^"]*)"')

# Copied from collect_linkedin.py — see the note there. Same threshold, same measurement.
ENGLISH_STOPWORDS = frozenset("""
the and of to in for with you we our your are is be will as on at that this from or by have
has it they their its an but not who all can if when which what where how about into than
""".split())


def is_english(text: str, threshold: float = 0.15) -> bool:
    """True when the posting is written in English. Short texts always pass: rejecting a
    vacancy for a language we never saw would silently throw away good matches."""
    words = re.findall(r"[a-zA-Záéíóúñüàèìòùäöüßçâêîôûœëïÿåæøþðłńśźżčšžğı]+", text.lower())
    if len(words) < 80:
        return True
    return sum(1 for w in words if w in ENGLISH_STOPWORDS) / len(words) >= threshold


# Spanish-distinctive function words. A Spanish posting is not dropped here: it goes to the
# model with `language: "es"`, and Stage 3 of prompts/daily_scan.md decides whether it can
# be done in English. Other non-English postings are still dropped. Words shared with French,
# Portuguese, Italian or Catalan (de, la, que, para, por, en) are left out on purpose. Measured
# on 30 days of LinkedIn: Spanish 0.11-0.18, French/German/Swedish/Portuguese/Italian
# 0.00-0.03.
SPANISH_STOPWORDS = frozenset("""
el los las y del con una más pero al lo también muy nuestro nuestra nuestros nuestras tu tus
""".split())


def is_spanish(text: str, threshold: float = 0.06) -> bool:
    words = re.findall(r"[a-zA-Záéíóúñüàèìòùäöüßçâêîôûœëïÿåæøþðłńśźżčšžğı]+", text.lower())
    if len(words) < 80:
        return False
    return sum(1 for w in words if w in SPANISH_STOPWORDS) / len(words) >= threshold


class Stop(Exception):
    """End the run early but successfully: blocked, throttled or out of budget. Caught once,
    in main(); whatever was collected is emitted and the exit code is 0."""


# ------------------------------------------------------------------------ http

def get(path: str, state: dict) -> str:
    """One GET, throttled and counted. 403/429 is a block or a throttle: stop and report, never
    retry around it — same line as the wantapply pass and collect_wellfound.py."""
    if state["used"]:
        time.sleep(random.uniform(*state["delay"]))
    if state["used"] >= state["limit"]:
        raise Stop(f"request budget ({state['limit']}) exhausted")
    state["used"] += 1
    # The sitemap gives slugs as raw UTF-8 (an emoji, Cyrillic); http.client sends ASCII only,
    # so such a detail page raised UnicodeEncodeError on every run (2026-11-13, -14). Percent-
    # encode what is not ASCII; "%" stays safe so an already-encoded slug is not encoded twice.
    path = urllib.parse.quote(path, safe="/%:?=&")
    req = urllib.request.Request(f"{BASE}/{path.lstrip('/')}", headers=UA)
    try:
        data = urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise Stop(f"HTTP {exc.code} on {path} — blocked or throttled") from None
        raise
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------- copied from boards
# Verbatim from collect_boards.py, as collect_linkedin.py and collect_wellfound.py do, so the
# product-analyst gates behave identically on every source. If one changes there, change it here.

def strip_html(s: str) -> str:
    """Unescape entities BEFORE stripping tags, not after.

    Greenhouse returns its job content HTML-escaped ("&lt;div&gt;..."). Stripping tags first
    finds nothing to strip, and the unescape then turns the entities into live markup — so
    the description arrives full of <div class="content-intro"> noise that pollutes both
    job.md and the keyword matching. Unescape twice: some boards double-encode.
    """
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def build_filters(profile: dict):
    titles, excl = [], [t.lower() for t in profile.get("title_exclusions", [])]
    fam_titles: dict[str, list[str]] = {}
    for name, fam in (profile.get("role_families") or {}).items():
        ts = [t.lower() for t in fam.get("titles", [])]
        fam_titles[name] = ts
        titles += ts
    de = profile.get("description_exclusions") or {}
    desc_excl = {
        "families": [f.lower() for f in de.get("applies_to_families", [])],
        "terms": [t.lower() for t in de.get("terms", [])],
        "require_any": [t.lower() for t in de.get("require_any", [])],
        "family_titles": fam_titles,
    }
    return titles, excl, desc_excl


def title_matches(title: str, titles: list[str], excl: list[str]) -> bool:
    t = title.lower()
    if any(x in t for x in excl):
        return False
    return any(x in t for x in titles)


def excluded_by_description(rec: dict, desc_excl: dict) -> bool:
    """True when the role is the product-analyst shape: a title in one of the weaker analyst
    families whose description is built on experiment work.

    Scoped on purpose. The terms alone are not evidence — measured on 2026-09-07, a bare
    keyword drop also removed both Preply Analytics Engineer roles in Barcelona, which carry
    'A/B tests' in the team blurb rather than in the requirements. A title that also matches
    a stronger family (Analytics Engineer, DWH, BI) wins and is kept.
    """
    terms = desc_excl.get("terms") or []
    families = desc_excl.get("families") or []
    if not terms or not families:
        return False
    t = rec["title"].lower()
    fam_titles = desc_excl.get("family_titles") or {}
    in_scope = any(any(x in t for x in fam_titles.get(f, [])) for f in families)
    if not in_scope:
        return False
    stronger = any(
        any(x in t for x in ts) for f, ts in fam_titles.items() if f not in families
    )
    if stronger:
        return False
    d = rec["description"].lower()
    if any(x in d for x in terms):
        return True
    # require_any (2026-10-30): a scoped family must also name warehouse / modelling work.
    # An empty description is never judged — it is not evidence either way.
    req = desc_excl.get("require_any") or []
    return bool(req) and bool(d.strip()) and not any(x in d for x in req)


# --------------------------------------------------------------------- parsing

def normalise_company(name: str) -> str:
    parts = re.sub(r"\s+", " ", (name or "").strip()).split()
    if len(parts) > 1 and parts[-1].lower().strip(",") in LEGAL_SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts).rstrip(",")


def slug_title(url: str) -> str:
    """'…/job/senior-analytics-engineer-finance/' -> 'senior analytics engineer finance'.
    WordPress appends -2, -3 to repeated titles; the number is dropped."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"-\d+$", "", slug).replace("-", " ")


def euremote_geo(ld: dict) -> tuple[str, str]:
    """(geo, location string). geo is remote / worldwide, or '' to drop.

    Every posting on this board is remote (TELECOMMUTE), so the question is only the stated
    scope: a country list that reaches the candidate is kept, one that does not is a narrower
    scope the employer stated and is dropped, and no list at all is the silence rule
    (unstated = worldwide). Europe-wide postings list every European country by name.
    """
    countries = [str((c or {}).get("name") or "") for c in
                 as_list(ld.get("applicantLocationRequirements"))]
    countries = [c for c in countries if c]
    if not countries:
        return "worldwide", "Remote — no location stated"
    if any(c.strip().lower() in SCOPES for c in countries):
        short = ", ".join(countries) if len(countries) <= 8 else f"{len(countries)} countries incl. {HOME_NAME}"
        return "remote", f"Remote — accepts: {short}"
    return "", f"Remote — accepts: {', '.join(countries)}"


def job_posting(page: str) -> dict:
    """The schema.org JobPosting node of a detail page, or {} when there is none."""
    for m in LD_JSON.finditer(page):
        try:
            doc = json.loads(m.group(1))
        except ValueError:
            continue
        nodes = doc.get("@graph", [doc]) if isinstance(doc, dict) else doc
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                return node
    return {}


def as_list(v) -> list:
    return v if isinstance(v, list) else [v] if v else []



# ----------------------------------------------------------------- run state

def load_config() -> dict:
    doc = jobprofile.load_sources()
    return (doc.get("sources") or {}).get(SOURCE_KEY) or {}


def load_seen() -> set[str]:
    """Keys of every track's seen file (jobprofile.tracks()). A key any track has handled
    costs no detail request."""
    # Until 2026-11-16 this read seen.json and seen_bi.json only, so the data_engineer and
    # part_time keys were never "seen" here (the same bug fixed in collect_linkedin.py 2026-11-15).
    keys: set[str] = set()
    paths = {SEEN, SEEN.with_name("seen_bi.json")}
    paths |= {ROOT / t["seen_file"] for t in jobprofile.tracks()}
    for path in paths:
        try:
            keys |= set((json.loads(path.read_text(encoding="utf-8")) or {}).get("entries", {}))
        except Exception:
            pass
    return keys


def first_run(source_key: str = SOURCE_KEY) -> bool:
    """True until this source has completed one run — a new source's first pass looks back
    two weeks. Deleting `euremotejobs.first_run_utc` from state/last_run.json re-arms it."""
    try:
        doc = json.loads(LAST_RUN.read_text(encoding="utf-8")) or {}
        return not (doc.get(source_key) or {}).get("first_run_utc")
    except Exception:
        return True          # unreadable state file: widen rather than silently narrow


def mark_run(source_key: str = SOURCE_KEY) -> None:
    """Written last, after the output file exists — a crash mid-run must leave the next run
    still treating itself as the first, rather than silently narrowing to 30 hours."""
    try:
        doc = json.loads(LAST_RUN.read_text(encoding="utf-8")) if LAST_RUN.exists() else {}
    except Exception:
        doc = {}
    now = datetime.now(timezone.utc).isoformat()
    entry = doc.get(source_key) or {}
    entry.setdefault("first_run_utc", now)
    entry["last_run_utc"] = now
    doc[source_key] = entry
    LAST_RUN.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


def key_of(rec: dict) -> str:
    """Identical to collect_boards.py, deliberately: a posting found here and on the company's
    own ATS board must produce the same key and collide in state/seen.json."""
    return hashlib.sha1(
        f"{rec['company'].strip().lower()}|{rec['title'].strip().lower()}".encode()
    ).hexdigest()



# ------------------------------------------------------------------------ main

def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path, help="write results as JSONL here")
    ap.add_argument("--hours", type=int, default=None,
                    help="freshness window; defaults to lookback_hours, or "
                         "first_run_lookback_hours if this source has never run")
    ap.add_argument("--max-requests", type=int, default=None)
    ap.add_argument("--ignore-seen", action="store_true",
                    help="fetch detail pages even for postings already in state/seen.json")
    ap.add_argument("--any-location", action="store_true",
                    help="keep postings the geography gate would drop (debugging)")
    ap.add_argument("--any-language", action="store_true",
                    help="keep postings not written in English (debugging)")
    args = ap.parse_args()

    profile = jobprofile.load_search_profile()
    global SCOPES, HOME_NAME
    home = jobprofile.geo_home(profile)
    SCOPES = {s.lower() for s in (cfg.get("accepted_remote_scopes") or home["remote_scopes"])}
    HOME_NAME = home["country_name"]
    # Only the roles profile/routing.yaml routes to this source.
    profile = role_routing.apply(profile, "euremotejobs")
    titles, excl, desc_excl = build_filters(profile)
    seen = set() if args.ignore_seen else load_seen()

    if args.hours is not None:
        hours = args.hours
    elif first_run():
        hours = cfg.get("first_run_lookback_hours", 336)
        print(f"  first run for this source — looking back {hours}h instead of the usual "
              f"{cfg.get('lookback_hours', 30)}h", file=sys.stderr)
    else:
        hours = cfg.get("lookback_hours", 30)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    require_english = cfg.get("require_english_description", True) and not args.any_language
    state = {"used": 0, "delay": tuple(cfg.get("delay_seconds") or DELAY),
             "limit": args.max_requests or cfg.get("max_requests", MAX_REQUESTS)}
    stopped = ""

    # ---- phase 1: the sitemaps, newest first, until one ends before the window
    print(f"Reading EU Remote Jobs: last {hours}h, budget {state['limit']} requests...",
          file=sys.stderr)
    entries: dict[str, datetime] = {}
    try:
        for sm in cfg.get("sitemaps") or SITEMAPS:
            got = [(u, datetime.fromisoformat(m)) for u, m in SITEMAP_ENTRY.findall(get(sm, state))]
            fresh = [(u, d) for u, d in got if d >= since]
            for u, d in fresh:
                entries.setdefault(u, d)
            print(f"  {len(fresh):>5} of {len(got)} in the window from {sm}", file=sys.stderr)
            if not got or min(d for _, d in got) < since:
                break
    except Stop as exc:
        stopped = str(exc)
    if not entries and state["used"] and not stopped:
        print("  !! euremotejobs sitemap gave no postings in the window — check the markup",
              file=sys.stderr)

    shortlist = [u for u in entries if title_matches(slug_title(u), titles, excl)]
    print(f"  {len(entries)} postings in the window, {len(shortlist)} past the title gate "
          f"on the URL slug", file=sys.stderr)

    # ---- phase 2: detail pages, one request each
    kept, geo_dropped, lang_dropped = [], [], []
    for url in shortlist:
        if stopped:
            break
        try:
            page = get(url.replace(BASE, ""), state)
        except Stop as exc:
            stopped = str(exc)
            break
        except Exception as exc:
            # 2026-10-30: the evening run printed bare "HTTPError" for two pages, which cannot
            # tell a removed posting (404) from a server fault — the status goes in the line.
            code = getattr(exc, "code", None)
            print(f"  ! detail {url}: {type(exc).__name__}{f' {code}' if code else ''}",
                  file=sys.stderr)
            continue
        ld = job_posting(page)
        if not ld:
            print(f"  ! no JobPosting JSON-LD on {url} — markup may have changed", file=sys.stderr)
            continue
        rec = {"title": html.unescape(str(ld.get("title") or "")).strip(),
               "company": normalise_company(html.unescape(
                   str((ld.get("hiringOrganization") or {}).get("name") or ""))),
               "location": "", "url": url, "description": "", "posted": "", "salary": "",
               "source": "euremotejobs", "geo": "", "apply_url": ""}
        # The slug passed; the real title must too — the slug can carry a word the title lacks.
        if not title_matches(rec["title"], titles, excl):
            continue
        rec["dedup_key"] = key_of(rec)
        geo, location = euremote_geo(ld)
        if not geo:
            if not args.any_location:
                geo_dropped.append(f"{rec['company']} — {rec['title'][:40]} — {location[:80]}")
                continue
            geo = "unclear"
        ats = ATS_LINK.search(page)
        rec.update(location=location, geo=geo,
                   description=strip_html(ld.get("description") or ""),
                   posted=str(ld.get("datePosted") or "")[:10],
                   apply_url=html.unescape(ats.group(1)).split("?")[0] if ats else "")
        if require_english and not is_english(rec["description"]):
            if "es" in jobprofile.soft_posting_languages() and is_spanish(rec["description"]):
                rec["language"] = "es"     # Stage 3 decides; see is_spanish
            else:
                lang_dropped.append(f"{rec['company']} — {rec['title'][:40]}")
                continue
        if excluded_by_description(rec, desc_excl):
            continue
        kept.append(rec)

    unique, keys = [], set()
    for rec in kept:
        if rec["dedup_key"] not in keys:
            keys.add(rec["dedup_key"])
            unique.append(rec)
    already = sum(r["dedup_key"] in seen for r in unique)

    counts: dict[str, int] = {}
    for r in unique:
        counts[r["geo"]] = counts.get(r["geo"], 0) + 1
    print(f"\n  {len(unique)} matching: "
          + (", ".join(f"{n} {g}" for g, n in sorted(counts.items())) or "none")
          + f" ({already} already seen, {state['used']} requests)", file=sys.stderr)
    for label, items in (("dropped on stated geography", geo_dropped),
                         ("dropped as not written in English", lang_dropped)):
        if items:
            print(f"  {len(items)} {label}:", file=sys.stderr)
            for x in items:
                print(f"      {x}", file=sys.stderr)
    if stopped:
        print(f"  ! stopped early: {stopped} — results are partial", file=sys.stderr)

    out = "\n".join(json.dumps(r, ensure_ascii=False) for r in unique)
    if args.json:
        args.json.write_text(out + "\n", encoding="utf-8")
        print(f"  written to {args.json}", file=sys.stderr)
        mark_run()          # last, and only on a real pipeline run
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
