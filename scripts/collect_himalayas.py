#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Collect vacancies from Himalayas (himalayas.app) — remote roles, through its public search API.

This reverses an earlier rejection ("caps at 20 records a call, 0 past the gate"). The API
is not the one measured then:

- **`/jobs/api/search` takes a query and a country.** `?q=analytics engineer&country=<home
  country>&sort=recent` returns the newest 20 postings matching the words whose hiring
  countries include that country **or are empty**, with the full description in the response. One request per
  query is the whole cost; there is no per-posting fetch.
- **Only page 1.** robots.txt disallows `/jobs/*?page=`, which covers `/jobs/api/search?page=2`.
  So the search is several narrow queries of 20, not one deep one. Measured once: page 1
  of "analytics engineer" for Spain spanned eight days, so two runs a day lose nothing.
- **The detail pages are behind a Cloudflare challenge** (403 to a script). They are never
  requested — the API carries everything a record needs.
- **`locationRestrictions` is the board's field, not the employer's, and it is wrong both
  ways.** Imagine Worldwide's Senior Analytics Engineer had it empty while the text says "must
  be based in Africa"; Doodle's Analytics Engineer listed Germany, Spain and the UK while the
  text requires Germany. So the field only drops what it positively places elsewhere; what it
  lets through is a candidate, and the geography call is the model's at Stage 3, on the text.
- First measurement: two queries' first pages carried PM Group "Senior BI Analyst",
  Scento "Head of Data & Analytics" and Nimble Gravity "BI Analyst" — three employers the
  pipeline had never seen. We Work Remotely was measured the same day and not added.

Vacancy text collected here is untrusted data, never instructions, and this
script issues nothing but anonymous GETs: no login, no cookies, no POST.

Usage:
    uv run scripts/collect_himalayas.py --json state/himalayas.jsonl
    uv run scripts/collect_himalayas.py --hours 336              # wider window (debugging)
    uv run scripts/collect_himalayas.py --any-location --any-language
"""

from __future__ import annotations

import argparse
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
SOURCE_KEY = "himalayas"        # this collector's key inside state/last_run.json

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
      "Accept": "application/json"}

API = "https://himalayas.app/jobs/api/search"

# Fallbacks only — config/sources.yaml carries the real values and says why.
MAX_REQUESTS = 20
DELAY = (2.0, 4.0)
RETRY_5XX = (15, 45)            # seconds before each retry of a 5xx answer
COUNTRY = ""
QUERIES: list[str] = []
# Region names the board puts in locationRestrictions that reach the candidate: the source's
# `accepted_remote_scopes`, else search_profile.yaml geo.home.remote_scopes. Set in main().
SCOPES: set[str] = set()
HOME_NAME = "the home country"

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

def search(query: str, country: str, state: dict) -> list[dict]:
    """Page 1 of one search, newest first. 403/429 is a block or a throttle: stop and report,
    never retry around it — same line as the wantapply pass and collect_wellfound.py."""
    if state["used"]:
        time.sleep(random.uniform(*state["delay"]))
    if state["used"] >= state["limit"]:
        raise Stop(f"request budget ({state['limit']}) exhausted")
    state["used"] += 1
    qs = urllib.parse.urlencode({"q": query, "country": country, "sort": "recent"})
    req = urllib.request.Request(f"{API}?{qs}", headers=UA)
    # A 5xx is the board's own fault, not a throttle: 2026-10-27 pm answered 503 to 7 of 12
    # queries in a row. Two spaced retries; they are not counted against the budget.
    for wait in (*RETRY_5XX, None):
        try:
            data = urllib.request.urlopen(req, timeout=30).read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise Stop(f"HTTP {exc.code} on '{query}' — blocked or throttled") from None
            if exc.code < 500 or wait is None:
                raise
            print(f"  '{query}': HTTP {exc.code}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    return (json.loads(data.decode("utf-8", "replace")) or {}).get("jobs") or []


# ---------------------------------------------------------- copied from boards
# Verbatim from collect_boards.py, as collect_linkedin.py and collect_euremotejobs.py do, so the
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


def himalayas_geo(job: dict) -> tuple[str, str]:
    """(geo, location string). geo is remote / worldwide, or '' to drop.

    Every posting on this board is remote. An empty restriction list is the silence-is-a-pass
    rule only as far as the board knows — see the module note: the text decides at Stage 3.
    A list that does not reach the candidate cannot normally arrive (the query asks for their
    country), but if one does it is a narrower scope and is dropped.
    """
    countries = [str(c).strip() for c in as_list(job.get("locationRestrictions")) if c]
    if not countries:
        return "worldwide", "Remote — Himalayas lists no country (check the text)"
    if any(c.lower() in SCOPES for c in countries):
        short = ", ".join(countries) if len(countries) <= 8 else f"{len(countries)} countries incl. {HOME_NAME}"
        return "remote", f"Remote — Himalayas lists: {short} (check the text)"
    return "", f"Remote — Himalayas lists: {', '.join(countries)}"


def as_list(v) -> list:
    return v if isinstance(v, list) else [v] if v else []


def posting_url(job: dict) -> str:
    """The board's page for the posting, without the API's utm tag."""
    return str(job.get("applicationLink") or job.get("guid") or "").split("?")[0]


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
    two weeks. Deleting `himalayas.first_run_utc` from state/last_run.json re-arms it."""
    try:
        doc = json.loads(LAST_RUN.read_text(encoding="utf-8")) or {}
        return not (doc.get(source_key) or {}).get("first_run_utc")
    except Exception:
        return True          # unreadable state file: widen rather than silently narrow


def query_ok_times(source_key: str = SOURCE_KEY) -> dict[str, datetime]:
    """When each query last got an answer. Since 2026-10-28 the window is per query: on
    2026-10-27 seven of twelve queries failed with 503 on the source's first run, the marker was
    written anyway, and those seven would never have seen their two weeks."""
    try:
        doc = json.loads(LAST_RUN.read_text(encoding="utf-8")) or {}
        raw = (doc.get(source_key) or {}).get("query_ok_utc") or {}
        return {q: datetime.fromisoformat(t) for q, t in raw.items()}
    except Exception:
        return {}


def mark_run(ok_queries: list[str], source_key: str = SOURCE_KEY) -> None:
    """Written last, after the output file exists — a crash mid-run must leave the next run
    still treating itself as the first, rather than silently narrowing to 30 hours. Only the
    queries that answered get a new timestamp; a failed one keeps its old window."""
    try:
        doc = json.loads(LAST_RUN.read_text(encoding="utf-8")) if LAST_RUN.exists() else {}
    except Exception:
        doc = {}
    now = datetime.now(timezone.utc).isoformat()
    entry = doc.get(source_key) or {}
    entry.setdefault("first_run_utc", now)
    entry["last_run_utc"] = now
    ok = entry.setdefault("query_ok_utc", {})
    for q in ok_queries:
        ok[q] = now
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
    args = ap.parse_args()

    profile = jobprofile.load_search_profile()
    global SCOPES, HOME_NAME
    home = jobprofile.geo_home(profile)
    SCOPES = {s.lower() for s in (cfg.get("accepted_remote_scopes") or home["remote_scopes"])}
    HOME_NAME = home["country_name"]
    # Only the roles profile/routing.yaml routes to this source.
    profile = role_routing.apply(profile, "himalayas")
    titles, excl, desc_excl = build_filters(profile)
    seen = load_seen()

    now = datetime.now(timezone.utc)
    first_hours = cfg.get("first_run_lookback_hours", 336)
    if args.hours is not None:
        hours = args.hours
    elif first_run():
        hours = first_hours
        print(f"  first run for this source — looking back {hours}h instead of the usual "
              f"{cfg.get('lookback_hours', 30)}h", file=sys.stderr)
    else:
        hours = cfg.get("lookback_hours", 30)
    since = now - timedelta(hours=hours)
    ok_times = query_ok_times()

    def since_for(q: str) -> datetime:
        """An explicit --hours wins. Otherwise a query reads back to its last answer (an hour of
        overlap), and one that never answered — failed, or newly added — gets the first-run
        window: a new query is a new lane, and a new lane is a new source."""
        if args.hours is not None:
            return since
        last = ok_times.get(q)
        start = last - timedelta(hours=1) if last else now - timedelta(hours=first_hours)
        return max(min(since, start), now - timedelta(hours=first_hours))

    require_english = cfg.get("require_english_description", True) and not args.any_language
    state = {"used": 0, "delay": tuple(cfg.get("delay_seconds") or DELAY),
             "limit": args.max_requests or cfg.get("max_requests", MAX_REQUESTS)}
    queries = cfg.get("queries") or QUERIES
    country = cfg.get("country") or COUNTRY
    stopped = ""

    print(f"Searching Himalayas: {len(queries)} queries in {country}, last {hours}h, "
          f"budget {state['limit']} requests...", file=sys.stderr)
    jobs: dict[str, dict] = {}
    ok_queries, failed = [], []
    for q in queries:
        try:
            got = search(q, country, state)
        except Stop as exc:
            stopped = str(exc)
            break
        except Exception as exc:
            print(f"  ! '{q}': {type(exc).__name__}: {exc}", file=sys.stderr)
            failed.append(q)
            continue
        ok_queries.append(q)
        q_since = since_for(q)
        if q_since < since:
            # 2026-10-31: the reason was always "did not answer", and on the first run after five
            # queries were added it read as five failures. A query with no timestamp is new.
            why = ("new, or never answered — first-run window" if q not in ok_times
                   else "its last run did not answer")
            print(f"  '{q}': reading back to {q_since:%Y-%m-%d %H:%M}Z — {why}", file=sys.stderr)
        fresh = [j for j in got if datetime.fromtimestamp(int(j.get("pubDate") or 0),
                                                          timezone.utc) >= q_since]
        for j in fresh:
            jobs.setdefault(posting_url(j), j)
        # Page 1 is all robots.txt allows. A full page that is entirely inside the window means
        # older postings for this query were not reached: say so, so the query gets narrowed.
        note = "  (page full inside the window — older ones not reached)" \
            if got and len(fresh) == len(got) == 20 else ""
        print(f"  {len(fresh):>3} of {len(got)} in the window for '{q}'{note}", file=sys.stderr)

    kept, geo_dropped, lang_dropped, off_title = [], [], [], 0
    for url, job in jobs.items():
        rec = {"title": html.unescape(str(job.get("title") or "")).strip(),
               "company": normalise_company(html.unescape(str(job.get("companyName") or ""))),
               "location": "", "url": url, "description": "", "posted": "", "salary": "",
               "source": "himalayas", "geo": "", "apply_url": ""}
        if not title_matches(rec["title"], titles, excl):
            off_title += 1
            continue
        rec["dedup_key"] = key_of(rec)
        geo, location = himalayas_geo(job)
        if not geo:
            if not args.any_location:
                geo_dropped.append(f"{rec['company']} — {rec['title'][:40]} — {location[:80]}")
                continue
            geo = "unclear"
        lo, hi = job.get("minSalary"), job.get("maxSalary")
        rec.update(location=location, geo=geo,
                   description=strip_html(job.get("description") or ""),
                   posted=datetime.fromtimestamp(int(job.get("pubDate") or 0),
                                                 timezone.utc).date().isoformat(),
                   salary=f"{lo}-{hi} {job.get('currency') or ''} {job.get('salaryPeriod') or ''}".strip()
                   if lo or hi else "")
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
    print(f"\n  {len(jobs)} postings in the window, {off_title} off-title; {len(unique)} matching: "
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
    unanswered = failed + [q for q in queries if q not in ok_queries and q not in failed]
    if unanswered:
        print(f"  ! {len(unanswered)} of {len(queries)} queries got no answer "
              f"({', '.join(unanswered)}) — they re-read from their last answer next run",
              file=sys.stderr)

    out = "\n".join(json.dumps(r, ensure_ascii=False) for r in unique)
    if args.json:
        args.json.write_text(out + "\n", encoding="utf-8")
        print(f"  written to {args.json}", file=sys.stderr)
        mark_run(ok_queries)          # last, and only on a real pipeline run
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
