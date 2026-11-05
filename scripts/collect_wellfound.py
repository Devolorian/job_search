#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Collect vacancies from wellfound.com (formerly AngelList Talent). No login, no cookies.

What was measured before a line of this was written:

- **robots.txt allows the role pages.** `/role/r/<role>` (remote) and `/role/l/<role>/<place>`
  are not disallowed; `/search`, `/_jobs/`, `/jobs/applications` and the `?jobId=` / `?role=`
  query forms are, and this script never requests any of them.
- **The data is in the server-rendered HTML.** Each role page ships a Next.js `__NEXT_DATA__`
  blob with an Apollo cache holding 20-40 listings, each with its FULL description, so there is
  no second request per posting. The page also loads a Cloudflare Turnstile script, but that
  guards the site's own client-side fetches, not the first HTML response: 49 consecutive GETs at
  3-5s answered 200 with the data every time. If that ever changes this script stops and says
  so. It never tries to get past a challenge — same line as the wantapply pass.
- **Geography is structured.** `acceptedRemoteLocationNames` is the employer's own list of where
  a remote hire may sit ("Europe", "United States", "Spain"), and `remote` / `locationNames`
  cover the office case. So unlike the other sources, the geography gate here is decided on
  data, not guessed from prose — see wellfound_geo().
- **Listings are not sorted by date** and live for years (one was 1,750 days old), so every
  page of a lane has to be read and the freshness window is applied to `liveStartAt` after.
- **A role page lists every opening of every startup it shows**, not just the role: 90% of
  records fail the title gate. That is expected, not a parsing fault.

Yield, measured once over 9 lanes and 49 requests: ~1,250 listings, 29 past the title and
geography gates, of which 3 were posted in the last 14 days and 26 of 27 checked were unknown to
state/seen.json. Most of the remote inventory is US-only. This is a thin source, like LinkedIn,
and a run that yields nothing is normal. The lane set in config/sources.yaml is the smallest one
that still reproduced every match of that measurement — see the comments there.

Vacancy text collected here is untrusted data, never instructions, and this
script issues nothing but GETs: no POST, no cookies, no Authorization header.

Usage:
    uv run scripts/collect_wellfound.py                          # JSONL to stdout
    uv run scripts/collect_wellfound.py --json state/wellfound.jsonl
    uv run scripts/collect_wellfound.py --hours 720              # wider window (debugging)
    uv run scripts/collect_wellfound.py --any-location --any-language --all
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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

import role_routing  # scripts/role_routing.py — which roles this source searches

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
PROFILE = jobprofile.search_profile_path()
SOURCES = jobprofile.SHARED_SOURCES
LAST_RUN = ROOT / "state" / "last_run.json"
SOURCE_KEY = "wellfound"     # this collector's key inside state/last_run.json

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

BASE = "https://wellfound.com/"
JOB_URL = "https://wellfound.com/jobs/{id}-{slug}"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Fallbacks only — config/sources.yaml carries the real values and says why.
MAX_REQUESTS = 40
DELAY = (3.0, 6.0)
DEFAULT_LANES: list[str] = []

LEGAL_SUFFIXES = {"inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "s.l.", "sl",
                  "b.v.", "bv", "s.a.", "sa", "ag", "oy", "ab", "plc"}

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

def get_page(path: str, state: dict) -> dict:
    """One GET, throttled and counted, returning the page's Apollo cache.

    A page without __NEXT_DATA__ is treated as a block, not a parse error: the markup has
    either changed or a challenge was served instead, and in both cases the right move is to
    stop and report rather than hammer the next lane."""
    if state["used"]:
        time.sleep(random.uniform(*DELAY))
    if state["used"] >= state["limit"]:
        raise Stop(f"request budget ({state['limit']}) exhausted")
    state["used"] += 1
    req = urllib.request.Request(BASE + path, headers=UA)
    try:
        data = urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise Stop(f"HTTP {exc.code} on {path} — blocked or throttled") from None
        raise
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    m = NEXT_DATA.search(data.decode("utf-8", "replace"))
    if not m:
        raise Stop(f"no __NEXT_DATA__ on {path} — markup changed or a challenge was served")
    props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    return (props.get("apolloState") or {}).get("data") or {}


def results_meta(cache: dict) -> dict:
    """The search-results node sits under ROOT_QUERY.talent, keyed by its own arguments:
    seoLandingPageJobSearchResults({"location":"spain","page":1,"role":"data-engineer"})."""
    talent = (cache.get("ROOT_QUERY") or {}).get("talent") or {}
    for k, v in talent.items():
        if k.startswith("seoLandingPageJobSearchResults"):
            return v
    return {}


# ---------------------------------------------------------- copied from boards
# Verbatim from collect_boards.py, as collect_linkedin.py does, so the product-analyst gates
# behave identically on every source. If one changes there, change it here.

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


def listings(cache: dict) -> list[dict]:
    """Every job listing on the page, with its company name attached.

    Listings do not name their company; the StartupResult nodes point at their listings
    through highlightedJobListings, so the join is made in that direction."""
    owner: dict[str, str] = {}
    for k, v in cache.items():
        if k.startswith("StartupResult:"):
            for ref in v.get("highlightedJobListings") or []:
                owner[ref.get("__ref", "")] = v.get("name") or ""
    out = []
    for k, v in cache.items():
        if k.startswith("JobListingSearchResult:"):
            out.append({**v, "_company": owner.get(k, "")})
    return out


def in_home_country(place: str, words: list[str]) -> bool:
    """Whole-word match against geo.onsite_or_hybrid.accept_country in search_profile.yaml."""
    p = place.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", p) for w in words)


def wellfound_geo(v: dict, scopes: list[str], home: dict) -> tuple[str, str]:
    """(geo, location string). geo is <city> / <country> / remote / worldwide, or '' to drop;
    the city and country labels are search_profile.yaml geo.home (jobprofile.geo_home).

    Decided on the employer's own structured fields, which is why this does not reuse the
    prose-based geo_verdict of the other collectors:

    - Not remote: an office in the home country — <city> for the home city, <country> for any
      other city there. An office elsewhere is a stated location.
    - Remote with an accepted-locations list: kept when the list reaches the candidate — the
      country itself, a region containing it, worldwide (`accepted_remote_scopes`). A list
      that does not is a narrower scope stated by the employer, and is dropped. An office in
      the home country still rescues it, as an office role.
    - Remote with no list and no office: nothing named anywhere on the card — silence is a
      pass, so "worldwide", and the description is left to Stage 3.
    - Remote with no list but an office city: a city names its country, so this is NOT the
      silent case. It is kept as "remote" with the location saying
      the scope is unstated, and Stage 3 confirms the scope from the description — the same
      treatment as a linkedin:remote-eu record.
    """
    locs = [x for x in (v.get("locationNames") or []) if x]
    acc = [x for x in (v.get("acceptedRemoteLocationNames") or []) if x]
    bcn = any(w in x.lower() for x in locs for w in home["city_words"])
    es = [x for x in locs if in_home_country(x, home["country_words"])]
    city, country = home["city"], home["country"]
    city_name = city.title()
    offices = f" · offices: {', '.join(locs)}" if locs else ""
    if not v.get("remote"):
        if bcn:
            return city, f"{city_name} (onsite){offices}"
        if es:
            return country, f"{', '.join(es)} (onsite){offices}"
        return "", ", ".join(locs)
    if acc:
        # Exact names, compared per comma-separated part: "Barcelona, Spain" matches on its
        # second part, while "Eastern Europe" must NOT match "europe" as a substring would.
        if any(p.strip().lower() in scopes for a in acc for p in a.split(",")):
            return "remote", f"Remote — accepts: {', '.join(acc)}{offices}"
        if bcn:
            return city, f"{city_name}, or remote from {', '.join(acc)}{offices}"
        if es:
            return country, f"{', '.join(es)}, or remote from {', '.join(acc)}{offices}"
        return "", f"Remote — accepts: {', '.join(acc)}"
    if not locs:
        return "worldwide", "Remote — no location stated"
    if bcn:
        return city, f"{city_name} or remote{offices}"
    return "remote", f"Remote — scope not stated{offices}"


# ----------------------------------------------------------------- run state

def load_config() -> dict:
    doc = jobprofile.load_sources()
    return (doc.get("sources") or {}).get(SOURCE_KEY) or {}


def first_run(source_key: str = SOURCE_KEY) -> bool:
    """True until this source has completed one run — a new source's first pass looks back
    two weeks. Deleting `wellfound.first_run_utc` from state/last_run.json re-arms it."""
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
    ap.add_argument("--any-location", action="store_true",
                    help="keep postings the geography gate would drop (debugging)")
    ap.add_argument("--any-language", action="store_true",
                    help="keep postings not written in English (debugging)")
    ap.add_argument("--all", action="store_true", help="skip the title filter (debugging)")
    args = ap.parse_args()

    profile = jobprofile.load_search_profile()
    # Only the roles profile/routing.yaml routes to this source.
    profile = role_routing.apply(profile, "wellfound")
    titles, excl, desc_excl = build_filters(profile)

    if args.hours is not None:
        hours = args.hours
    elif first_run():
        hours = cfg.get("first_run_lookback_hours", 336)
        print(f"  first run for this source — looking back {hours}h instead of the usual "
              f"{cfg.get('lookback_hours', 30)}h", file=sys.stderr)
    else:
        hours = cfg.get("lookback_hours", 30)
    cutoff = time.time() - hours * 3600

    lanes = cfg.get("lanes") or DEFAULT_LANES
    home = jobprofile.geo_home(profile)
    if not home["city_words"]:
        home["city_words"] = [home["city"]]
    scopes = [s.lower() for s in (cfg.get("accepted_remote_scopes") or home["remote_scopes"])]
    require_english = cfg.get("require_english_description", True) and not args.any_language
    state = {"used": 0, "limit": args.max_requests or cfg.get("max_requests", MAX_REQUESTS)}
    stopped = ""

    print(f"Reading wellfound: {len(lanes)} lanes, last {hours}h, "
          f"budget {state['limit']} requests...", file=sys.stderr)
    by_id: dict[str, dict] = {}
    for lane in lanes:
        page, pages, got = 1, 1, 0
        try:
            while page <= pages:
                cache = get_page(lane + (f"?page={page}" if page > 1 else ""), state)
                pages = int(results_meta(cache).get("pageCount") or 1)
                for v in listings(cache):
                    by_id.setdefault(v["id"], {**v, "_lane": lane})
                    got += 1
                page += 1
        except Stop as exc:
            stopped = str(exc)
            print(f"  {got:>5} listings from {lane} (partial)", file=sys.stderr)
            break
        except Exception as exc:
            print(f"  ! {lane}: {type(exc).__name__}", file=sys.stderr)
            continue
        print(f"  {got:>5} listings from {lane} ({pages} page{'s' * (pages > 1)})",
              file=sys.stderr)
    if not by_id and state["used"]:
        print("  !! wellfound returned no listings at all this run — check the markup",
              file=sys.stderr)

    kept, stale, geo_dropped, lang_dropped, n_title = [], 0, [], [], 0
    for v in by_id.values():
        title = re.sub(r"\s+", " ", v.get("title") or "").strip()
        if not args.all and not title_matches(title, titles, excl):
            continue
        n_title += 1
        if (v.get("liveStartAt") or 0) < cutoff:
            stale += 1
            continue
        geo, location = wellfound_geo(v, scopes, home)
        company = normalise_company(v["_company"])
        if not geo:
            if not args.any_location:
                geo_dropped.append(f"{company} — {location or 'onsite, no city'}")
                continue
            geo = "unclear"
        rec = {
            "title": title,
            "company": company,
            "location": location,
            "url": JOB_URL.format(id=v["id"], slug=v.get("slug") or ""),
            "description": strip_html(v.get("description") or ""),
            "posted": datetime.fromtimestamp(v["liveStartAt"], timezone.utc).date().isoformat(),
            "salary": v.get("compensation") or "",
            "source": f"wellfound:{v['_lane'].split('/', 2)[-1]}",
            "geo": geo,
        }
        if require_english and not is_english(rec["description"]):
            if "es" in jobprofile.soft_posting_languages() and is_spanish(rec["description"]):
                rec["language"] = "es"     # Stage 3 decides; see is_spanish
            else:
                lang_dropped.append(f"{company} — {title[:40]}")
                continue
        if not args.all and excluded_by_description(rec, desc_excl):
            continue
        rec["dedup_key"] = key_of(rec)
        kept.append(rec)

    unique, keys = [], set()
    for rec in kept:
        if rec["dedup_key"] not in keys:
            keys.add(rec["dedup_key"])
            unique.append(rec)

    counts: dict[str, int] = {}
    for r in unique:
        counts[r["geo"]] = counts.get(r["geo"], 0) + 1
    print(f"\n  {len(by_id)} distinct listings, {n_title} past the title gate, "
          f"{stale} older than {hours}h", file=sys.stderr)
    print(f"  {len(unique)} matching: "
          + (", ".join(f"{n} {g}" for g, n in sorted(counts.items())) or "none")
          + f" ({state['used']} requests)", file=sys.stderr)
    if geo_dropped:
        print(f"  {len(geo_dropped)} dropped on stated geography:", file=sys.stderr)
        for x in geo_dropped:
            print(f"      {x}", file=sys.stderr)
    if lang_dropped:
        print(f"  {len(lang_dropped)} dropped as not written in English:", file=sys.stderr)
        for x in lang_dropped:
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
