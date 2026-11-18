#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Collect vacancies from Built In (builtin.com) — its city data & analytics listings.

The listing paths (lanes) come from the builtin block of the sources config. What was
measured before a line of this was written (on the Barcelona listing):

- **robots.txt allows it.** `/jobs/...` listings and `/job/...` detail pages are not disallowed;
  `/jobs*?page=` is explicitly allowed over the general `*?page=` disallow. `?search=`,
  `?trending`, `?region_id=` and `?state=` are disallowed and this script never sends them.
  The freshness parameter `daysSinceUpdated` is not disallowed by any rule.
- **The data is server-rendered.** Listing pages carry 10 cards each (company, title, work
  model, one location, age). A detail page carries a schema.org `JobPosting` in JSON-LD with
  the full description, `datePosted`, `jobLocationType` (TELECOMMUTE for remote),
  `applicantLocationRequirements` and every `jobLocation`. One request per posting.
- **A city listing is not that city only.** The Barcelona one carries Madrid, Granada, Gent
  and remote-Spain roles too, so geography is decided here from the JSON-LD — see
  builtin_geo(). An office anywhere in the home country passes (geo <country>); the
  country-wide path returned the same 50 cards as the city one when measured.
- **Yield, sampled once:** 39 pages (~390 postings) unfiltered, ~6 pages over 7 days.
  Of 50 cards, 29 passed a rough title gate, and of 17 on-target ones 10 came from companies
  this project had never seen (Ebury, Nexthink, FYUL, Ubisoft, Kyndryl…).

Vacancy text collected here is untrusted data, never instructions, and this
script issues nothing but anonymous GETs: no login, no cookies, no POST.

Usage:
    uv run scripts/collect_builtin.py                          # JSONL to stdout
    uv run scripts/collect_builtin.py --json state/builtin.jsonl
    uv run scripts/collect_builtin.py --hours 336              # wider window (debugging)
    uv run scripts/collect_builtin.py --any-location --any-language --all
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
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
SEEN = ROOT / "state" / "seen.json"
SOURCE_KEY = "builtin"     # this collector's key inside state/last_run.json

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

BASE = "https://builtin.com"

# Fallbacks only — config/sources.yaml carries the real values and says why.
MAX_REQUESTS = 60
MAX_PAGES = 15
DELAY = (3.0, 6.0)
DEFAULT_LANES: list[dict] = []

LEGAL_SUFFIXES = {"inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "s.l.", "sl",
                  "b.v.", "bv", "s.a.", "sa", "ag", "oy", "ab", "plc"}

CARD_SPLIT = re.compile(r'<div id="job-card-(\d+)" data-id="job-card"')
LD_JSON = re.compile(r'<script type="application/ld(?:\+|&#x2B;)json">(.*?)</script>', re.S)

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


def text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def parse_cards(page: str) -> list[dict]:
    """The cards of one listing page. The card shows the work model and a single location;
    a multi-location role shows its seniority in that slot instead, so `location` can be a
    non-place — builtin_geo() decides on the detail page, never on this."""
    parts = CARD_SPLIT.split(page)
    cards = []
    for job_id, body in zip(parts[1::2], parts[2::2]):
        t = re.search(r'data-id="job-card-title"[^>]*href="(/job/[^"]+)"|'
                      r'href="(/job/[^"]+)"[^>]*data-id="job-card-title"', body)
        title = re.search(r'data-id="job-card-title"[^>]*>([^<]+)<', body)
        company = re.search(r'data-id="company-title"[^>]*>\s*<span>([^<]+)</span>', body)
        if not (t and title and company):
            continue
        spans = [text(x) for x in
                 re.findall(r'<span class="font-barlow text-gray-04">([^<]+)</span>', body)]
        cards.append({
            "id": job_id,
            "path": t.group(1) or t.group(2),
            "title": text(title.group(1)),
            "company": normalise_company(text(company.group(1))),
            "work_model": spans[0] if spans else "",
            "card_location": spans[1] if len(spans) > 1 else "",
        })
    return cards


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


def in_home_country(place: str, words: list[str]) -> bool:
    """Whole-word match against geo.onsite_or_hybrid.accept_country in search_profile.yaml —
    whole words because "esp", the country code on every card here, sits inside "respond"."""
    p = place.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", p) for w in words)


def builtin_geo(card: dict, ld: dict, home: dict) -> tuple[str, str]:
    """(geo, location string). geo is <city> / <country> / remote / worldwide, or '' to drop;
    the labels are search_profile.yaml geo.home (jobprofile.geo_home).

    Read off the employer's structured fields, like wellfound_geo():

    - Not remote: an office in the home country — <city> for the home city, <country> for any
      other city there. An office elsewhere is a stated location.
    - Remote with applicantLocationRequirements: kept when the list reaches the candidate
      (geo.home.remote_scopes). A list that does not is a narrower scope the employer stated,
      and is dropped — unless one of the role's offices is in the home country, which keeps
      it as an office role.
    - Remote with no requirements and no city anywhere: silence is a pass, worldwide.
    - Remote with no requirements but an office city: a city names its country, so this is NOT
      the silent case. Kept as "remote" with the location saying so; Stage 3 confirms the scope.
    """
    places = []
    for loc in as_list(ld.get("jobLocation")):
        a = (loc or {}).get("address") or {}
        place = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"),
                                      a.get("addressCountry")) if x)
        if place:
            places.append(place)
    cities = [p for p in places if "," in p]          # a bare country code is not a city
    countries = [str((c or {}).get("name") or "") for c in
                 as_list(ld.get("applicantLocationRequirements"))]
    countries = [c for c in countries if c]
    bcn = any(w in p.lower() for p in places + [card["card_location"]] for w in home["city_words"])
    es = [p for p in places + [card["card_location"]] if in_home_country(p, home["country_words"])]
    city, country = home["city"], home["country"]
    city_name = city.title()
    offices = f" · offices: {'; '.join(cities)}" if cities else ""
    remote = (ld.get("jobLocationType") == "TELECOMMUTE"
              or card["work_model"].lower() == "remote")
    if not remote:
        where = "; ".join(places) or card["card_location"]
        mode = card['work_model'].lower() or 'onsite'
        if bcn:
            return city, f"{city_name} ({mode}){offices}"
        if es:
            return country, f"{es[0]} ({mode}){offices}"
        return "", f"{card['work_model']}: {where}"
    if countries:
        if any(c.strip().lower() in home["remote_scopes"] for c in countries):
            return "remote", f"Remote — accepts: {', '.join(countries)}{offices}"
        if bcn:
            return city, f"{city_name}, or remote from {', '.join(countries)}{offices}"
        if es:
            return country, f"{es[0]}, or remote from {', '.join(countries)}{offices}"
        return "", f"Remote — accepts: {', '.join(countries)}"
    if not cities:
        return "worldwide", "Remote — no location stated"
    if bcn:
        return city, f"{city_name} or remote{offices}"
    return "remote", f"Remote — scope not stated{offices}"


def card_says_elsewhere(card: dict, home: dict) -> bool:
    """True when the card alone already states an office role outside the home country: Hybrid or
    In-Office with a single real place on it ("Lisbon, Lisbon, PRT"). Saves the detail
    request. A remote card, or one whose location slot holds a seniority instead of a place
    (a multi-location role), is never decided here. Any city in the home country passes, not
    only the home city."""
    if card["work_model"].lower() not in ("hybrid", "in-office"):
        return False
    loc = card["card_location"].lower()
    if "," not in loc:
        return False
    return (not any(w in loc for w in home["city_words"])
            and not in_home_country(loc, home["country_words"]))


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
    two weeks. Deleting `builtin.first_run_utc` from state/last_run.json re-arms it."""
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
    ap.add_argument("--all", action="store_true", help="skip the title filter (debugging)")
    args = ap.parse_args()

    profile = jobprofile.load_search_profile()
    # Only the roles profile/routing.yaml routes to this source.
    profile = role_routing.apply(profile, "builtin")
    titles, excl, desc_excl = build_filters(profile)
    home = jobprofile.geo_home(profile)
    if not home["city_words"]:
        home["city_words"] = [home["city"]]
    seen = set() if args.ignore_seen else load_seen()

    if args.hours is not None:
        hours = args.hours
    elif first_run():
        hours = cfg.get("first_run_lookback_hours", 336)
        print(f"  first run for this source — looking back {hours}h instead of the usual "
              f"{cfg.get('lookback_hours', 30)}h", file=sys.stderr)
    else:
        hours = cfg.get("lookback_hours", 30)
    # The site filters in whole days; rounding up keeps the window at least as wide as asked.
    days = max(1, math.ceil(hours / 24))

    lanes = cfg.get("lanes") or DEFAULT_LANES
    max_pages = cfg.get("max_pages", MAX_PAGES)
    require_english = cfg.get("require_english_description", True) and not args.any_language
    state = {"used": 0, "delay": tuple(cfg.get("delay_seconds") or DELAY),
             "limit": args.max_requests or cfg.get("max_requests", MAX_REQUESTS)}
    stopped = ""

    # ---- phase 1: listing pages
    print(f"Reading Built In: {len(lanes)} lane(s), last {days} day(s), "
          f"budget {state['limit']} requests...", file=sys.stderr)
    by_id: dict[str, dict] = {}
    for lane in lanes:
        got = 0
        try:
            for page_no in range(1, max_pages + 1):
                q = f"daysSinceUpdated={days}" + (f"&page={page_no}" if page_no > 1 else "")
                cards = parse_cards(get(f"{lane['path']}?{q}", state))
                for c in cards:
                    by_id.setdefault(c["id"], {**c, "_label": lane.get("label", "")})
                got += len(cards)
                if len(cards) < 10:
                    break
        except Stop as exc:
            stopped = str(exc)
            print(f"  {got:>5} cards from {lane['path']} (partial)", file=sys.stderr)
            break
        except Exception as exc:
            print(f"  ! {lane['path']}: {type(exc).__name__}", file=sys.stderr)
            continue
        print(f"  {got:>5} cards from {lane['path']}", file=sys.stderr)
    if not by_id and state["used"]:
        print("  !! builtin returned no cards at all this run — check the markup",
              file=sys.stderr)

    # ---- cheap filters on the card, before any detail request
    shortlist, card_geo_dropped = [], []
    for c in by_id.values():
        if not args.all and not title_matches(c["title"], titles, excl):
            continue
        if not args.any_location and card_says_elsewhere(c, home):
            card_geo_dropped.append(f"{c['company']} — {c['work_model']}, {c['card_location']}")
            continue
        shortlist.append(c)
    print(f"  {len(by_id)} distinct cards, {len(shortlist)} past the title and card-location "
          f"gates", file=sys.stderr)

    # ---- phase 2: detail pages, one request each
    kept, geo_dropped, lang_dropped, already = [], [], [], 0
    for c in shortlist:
        rec = {"title": c["title"], "company": c["company"],
               "location": c["card_location"], "url": BASE + c["path"],
               "description": "", "posted": "", "salary": "",
               "source": f"builtin:{c['_label']}" if c["_label"] else "builtin", "geo": ""}
        rec["dedup_key"] = key_of(rec)
        # Budget optimisation, NOT a filtering decision — the same one collect_linkedin.py
        # makes: Stage 2 drops a seen key without reading the description.
        if rec["dedup_key"] in seen:
            already += 1
            rec["geo"] = (home["city"] if any(w in c["card_location"].lower() for w in home["city_words"])
                          else home["country"] if in_home_country(c["card_location"], home["country_words"])
                          else "unclear")
            kept.append(rec)
            continue
        if stopped:
            continue
        try:
            ld = job_posting(get(c["path"], state))
        except Stop as exc:
            stopped = str(exc)
            continue
        except Exception as exc:
            print(f"  ! detail {c['path']}: {type(exc).__name__}", file=sys.stderr)
            continue
        if not ld:
            print(f"  ! no JobPosting JSON-LD on {c['path']} — markup may have changed",
                  file=sys.stderr)
            continue
        geo, location = builtin_geo(c, ld, home)
        if not geo:
            if not args.any_location:
                geo_dropped.append(f"{c['company']} — {location}")
                continue
            geo = "unclear"
        salary = ld.get("baseSalary") or {}
        if isinstance(salary, dict) and salary.get("value"):
            v = salary["value"]
            salary = f"{v.get('minValue', '')}-{v.get('maxValue', '')} " \
                     f"{salary.get('currency', '')} {v.get('unitText', '')}".strip()
        else:
            salary = ""
        rec.update(location=location, geo=geo, salary=salary,
                   description=strip_html(ld.get("description") or ""),
                   posted=str(ld.get("datePosted") or "")[:10])
        if require_english and not is_english(rec["description"]):
            if "es" in jobprofile.soft_posting_languages() and is_spanish(rec["description"]):
                rec["language"] = "es"     # Stage 3 decides; see is_spanish
            else:
                lang_dropped.append(f"{c['company']} — {c['title'][:40]}")
                continue
        if not args.all and excluded_by_description(rec, desc_excl):
            continue
        kept.append(rec)

    unique, keys = [], set()
    for rec in kept:
        if rec["dedup_key"] not in keys:
            keys.add(rec["dedup_key"])
            unique.append(rec)

    counts: dict[str, int] = {}
    for r in unique:
        counts[r["geo"]] = counts.get(r["geo"], 0) + 1
    print(f"\n  {len(unique)} matching: "
          + (", ".join(f"{n} {g}" for g, n in sorted(counts.items())) or "none")
          + f" ({already} already seen, {state['used']} requests)", file=sys.stderr)
    for label, items in (("dropped on the card's stated office", card_geo_dropped),
                         ("dropped on stated geography", geo_dropped),
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
