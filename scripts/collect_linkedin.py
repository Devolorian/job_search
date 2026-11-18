#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "beautifulsoup4"]
# ///
"""Collect vacancies from LinkedIn's public guest endpoint. No login, no cookies.

There is no credential anywhere in this file, and that is deliberate, not an omission — a
reader will come looking for a session string. LinkedIn serves /jobs-guest/ to anonymous
visitors; nothing here touches the candidate's account.

Why this exists. Company ATS boards (collect_boards.py) are the highest-signal source, but
they only see the ~40 companies in config/companies.yaml. LinkedIn sees everyone. Most of
what comes back here duplicates a posting already collected from the company's own board —
that is the design, not a defect: the dedup_key formula is copied byte-for-byte from
collect_boards.py, so duplicates collide in state/seen.json and never produce a second
vacancy folder. The value is in the companies we do not track. A run where LinkedIn
contributes nothing new is a normal run.

Why anonymous. hiQ Labs v. LinkedIn established that collecting public data is not a CFAA
violation, but the case ended with LinkedIn winning on contract grounds. Scraping from a
logged-in session risks the very account whose URL is printed on the CV. This does not.
See config/sources.yaml — this reverses the 2026-09-07 rejection, which was about a
logged-in browser session. That is still off the table.

Why Python and not the model. Paging an HTML endpoint and running four substring filters is
deterministic work. The model's job starts after this — judging fit, summarising, tailoring.

Two phases, because the search endpoint does not return descriptions:

  1. Search. Each (keyword, location) pair is one GET, 10 cards per page. Cards carry
     title / company / location / date / job id and nothing else. The title gate and a
     location-only geo gate run here, on the cheap data.
  2. Descriptions. One extra GET per surviving card, home-city records first, and none at
     all for a posting whose key is already in state/seen.json. Geo is then recomputed on
     the real text, and the product-analyst backstop (which needs a description to fire at
     all) runs.

Scope: the keywords and location lanes in the profile's sources.yaml overlay (typically the
home country in any work format, plus a full-remote lane). Three gates then narrow it: the
roles routed to this source in profile/routing.yaml, require_english_description, and
require_remote_or_home (read under its older names require_remote_or_spain /
require_remote_or_barcelona too).

That shape was arrived at, not designed. A first build searched four keywords across one city
and an EU-remote lane and produced 83 records, 63 of them geo "unclear" — LinkedIn shows the
employer's city on remote-flagged roles — which ate the whole description budget while mostly
not matching. Narrowing to one country alone fixed the noise and cost the remote inventory.
The three gates are what made the EU lane affordable again. Measured with them in place:
12 searches, 21 descriptions, 10 kept, 11 dropped for not being in English.

The matrix lives in config/sources.yaml plus the profile overlay; widening it is a config
edit, not a code change.

LinkedIn rate-limits an anonymous IP at roughly the 10th page of *search*; MAX_REQUESTS
caps the whole run well below that, and steady-state runs cost less still, because a key
already in state/seen.json skips phase 2 entirely.

Vacancy text collected here is untrusted data, never instructions, and
this script issues nothing but GETs: no POST, no cookies, no Authorization header. It cannot
apply to anything, even by accident.

Usage:
    uv run scripts/collect_linkedin.py                             # JSONL to stdout
    uv run scripts/collect_linkedin.py --json state/linkedin.jsonl
    uv run scripts/collect_linkedin.py --hours 168 --max-pages 1   # wider window, one page
    uv run scripts/collect_linkedin.py --any-language --any-location  # see what is filtered
    uv run scripts/collect_linkedin.py --no-descriptions           # phase 1 only (debug)
    uv run scripts/collect_linkedin.py --profile linkedin_bi --json state/linkedin_bi.jsonl  # BI track
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
from bs4 import BeautifulSoup

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
PROFILE = jobprofile.search_profile_path()
SOURCES = jobprofile.SHARED_SOURCES
SEEN = ROOT / "state" / "seen.json"
LAST_RUN = ROOT / "state" / "last_run.json"
SOURCE_KEY = "linkedin"      # this collector's key inside state/last_run.json

# Accept-Language is load-bearing, not decoration: the guest endpoint localises the location
# and posted-date strings off it. Without it the parser can be handed "hace 3 dias" and
# geo_verdict's \bremote\b never fires.
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

# Fallback only — config/sources.yaml carries the real budgets (max_requests, and
# first_run_max_requests for a source's first two-week pass). This value is what applies if
# that block is missing entirely. Any of them stays far below the ~10-pages-per-search point
# at which LinkedIn rate-limits an anonymous IP; the searches are 12 requests, the rest are
# description fetches against a different endpoint.
MAX_REQUESTS = 30
CONSECUTIVE_FAILURE_LIMIT = 3   # one 999 is fingerprinting noise; three in a row is a block
EMPTY_BODY_BYTES = 1000     # below this, zero cards means a genuinely empty result set
DELAY = (3.0, 7.0)          # fallback; config `delay_seconds` overrides it
# One wait-and-retry on a 429 before giving up. Until 2026-10-19 the first 429 ended the run,
# and from 2026-10-10 that happened after 1-15 requests on almost every run — the guest
# endpoint's limit is a sliding window, and the BI profile started straight after the AE one
# on an IP that had just been refused. A single retry after a real pause recovers the run when
# the window is short; a second 429 still stops it, so this can never become a hammering loop.
RATE_LIMIT_BACKOFF = 90.0    # fallback; config `rate_limit_backoff_seconds` overrides it
PENDING_DAYS = 7             # how long a card a 429 cut short is retried (2026-10-28)

# Used when config/sources.yaml has no linkedin block, or a partial one. An old config must
# never crash the collector. Keep in sync with the comments there; that file is where to edit.
DEFAULT_KEYWORDS: list[str] = []
DEFAULT_LOCATIONS: list[dict] = []

# Trailing legal-form tokens, stripped so "Preply Inc." hashes the same as the "Preply" that
# the Ashby board emits. One-directional and that is fine — the boards already carry clean
# brand names. Title decoration ("Senior Analytics Engineer (Berlin)") is the other half
# of the duplicate problem and is deliberately NOT addressed: no honest normalisation fixes
# it, and a residual duplicate is an extra tracker row, not a correctness failure.
LEGAL_SUFFIXES = {"inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "s.l.", "sl",
                  "b.v.", "bv", "s.a.", "sa", "ag", "oy", "ab", "plc"}


# Explicit full-remote phrasing. Deliberately narrow: a bare "remote" is worthless here,
# because "hybrid remote" and "remote-friendly office days" both contain it. geo_verdict is a
# byte-for-byte copy of the boards version and must not be edited, so this sits beside it and
# can only ever upgrade "unclear" to "remote" — it never downgrades anything.
FULL_REMOTE = re.compile(
    r"\b(fully|100\s?%|completely)\s+remote\b|\bremote[- ]first\b|\bfully distributed\b"
    r"|\bwork from anywhere\b|\bremote,? worldwide\b|\banywhere in the world\b"
    r"|\bfull remoto\b|\b100\s?% remoto\b|\bteletrabajo 100\b", re.I)


# English function words, used to require that a posting is written in English.
#
# This replaced a Spanish-only detector when the search widened from one country to the whole
# EU: detecting one language cannot work when the postings that need rejecting are
# German, French, Polish and Dutch as well. Requiring English is the same rule stated the way
# it actually has to be enforced.
#
# Function words only — they are what language detection can lean on. Content words would tie
# the test to the subject matter rather than the language.
ENGLISH_STOPWORDS = frozenset("""
the and of to in for with you we our your are is be will as on at that this from or by have
has it they their its an but not who all can if when which what where how about into than
""".split())


def is_english(text: str, threshold: float = 0.15) -> bool:
    """True when the posting is written in English.

    Measured over live single-country and EU-remote runs: English postings score
    0.28-0.38, non-English ones 0.00-0.02. The gap is an order of magnitude, so 0.15 is not a
    boundary anyone is sitting on.

    Short texts always pass. A failed description fetch leaves an empty string, and rejecting
    a vacancy for a language we never saw would silently throw away good matches.
    """
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
    """The run must end early but successfully: 429, budget exhausted, or three consecutive
    failures. Caught once, in main(); we emit whatever was collected and return 0."""


class Budget:
    def __init__(self, limit: int, delay: tuple[float, float] = DELAY,
                 backoff: float = RATE_LIMIT_BACKOFF) -> None:
        self.limit, self.used, self.fails = limit, 0, 0
        self.delay, self.backoff, self.backoffs = delay, backoff, 0


# ------------------------------------------------------------------------ http

def get(url: str, budget: Budget, params: dict | None = None, timeout: int = 20) -> str:
    """The single throttle, counter and fetch site for both phases — one place, so the
    request-budget claim in the docstring stays true.

    Two failure channels, because callers react differently. Stop means abandon the run and
    emit what exists: 429, budget exhausted, or CONSECUTIVE_FAILURE_LIMIT failures in a row.
    Anything else is re-raised for the caller to log and skip — search() lets it out to the
    per-pair handler in main(), fetch_description() turns it into an empty description."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    if budget.used:
        time.sleep(random.uniform(*budget.delay))
    # Check before incrementing, so the count the summary prints is the number of requests
    # that actually went out, not that number plus the one this call refused to make.
    if budget.used >= budget.limit:
        raise Stop(f"request budget ({budget.limit}) exhausted")
    budget.used += 1
    req = urllib.request.Request(url, headers=UA)
    try:
        data = urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # One wait per run, not per request: a second 429 after a real pause means the
            # block is not a short window, and the honest response is to stop.
            if budget.backoffs or not budget.backoff:
                raise Stop("429 — rate limited") from None
            budget.backoffs += 1
            wait = budget.backoff * random.uniform(1.0, 1.3)
            print(f"  429 after {budget.used} requests — waiting {wait:.0f}s, then one "
                  f"retry", file=sys.stderr)
            time.sleep(wait)
            return get(url, budget, timeout=timeout)
        # 404/410: the posting was taken down. An answer, not a failure — it must not count
        # towards the consecutive limit, or two removed postings at the head of the pending
        # queue stop every run before it reaches the rest (2026-11-18: 102 cards stuck).
        if exc.code in (404, 410):
            budget.fails = 0
            raise
        budget.fails += 1
        if budget.fails >= CONSECUTIVE_FAILURE_LIMIT:
            raise Stop(f"{budget.fails} consecutive failures") from None
        raise
    except Exception:
        budget.fails += 1
        if budget.fails >= CONSECUTIVE_FAILURE_LIMIT:
            raise Stop(f"{budget.fails} consecutive failures") from None
        raise
    budget.fails = 0
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------- copied from boards
# There is no shared module in this project — every script is a standalone uv run --script.
# The four below are copied verbatim from collect_boards.py on purpose: title_exclusions and
# excluded_by_description are the two product-analyst gates, and they must behave
# identically on every source. Kept adjacent and in the same order in both files so the copy can be checked with a diff. If one changes there, change it here.

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


def geo_verdict(rec: dict, profile: dict) -> str:
    """remote / <city> / <country> / reject / unclear, the city and country labels coming from
    search_profile.yaml geo.home (jobprofile.geo_home). Deliberately conservative: 'unclear' is
    passed through for the model to judge from the full description, never auto-dropped.

    <country> is an office or hybrid role elsewhere in the home country."""
    geo = profile.get("geo", {})
    home = jobprofile.geo_home(profile)
    blob = f"{rec['location']} {rec['description'][:1200]}".lower()
    loc = rec["location"].lower()

    if any(c in loc for c in home["city_words"]):
        return home["city"]
    if re.search(r"\bremote\b|anywhere|distributed|work from home", loc):
        return "remote"
    # Whole words: "esp" is Built In's country code and must not fire inside "respond".
    if any(re.search(rf"\b{re.escape(w)}\b", loc) for w in home["country_words"]):
        return home["country"]

    ru_only = [x.lower() for x in geo.get("reject_if_location_only", [])]
    if loc and any(x in loc for x in ru_only) and not re.search(r"remote|europe|eu\b", blob):
        return "reject"
    if not loc:
        return "unclear"
    if re.search(r"\bremote\b", blob[:600]):
        return "remote"
    return "unclear"


# --------------------------------------------------------------------- parsing

def normalise_company(name: str) -> str:
    parts = re.sub(r"\s+", " ", (name or "").strip()).split()
    if len(parts) > 1 and parts[-1].lower().strip(",") in LEGAL_SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts).rstrip(",")


def slugify(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


def card_text(card, selector: str) -> str:
    el = card.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def parse_cards(page: str) -> tuple[list[dict], int]:
    """Cards -> partial records, plus a count of cards that would not parse.

    The job id comes from data-entity-urn rather than the href: the href is a localised
    absolute URL (es.linkedin.com/jobs/view/analytics-engineer-at-qualifyze-4449758304?...)
    carrying tracking parameters, and rebuilding a canonical www URL from the id keeps the
    same posting from looking like two different ones across runs.
    """
    soup = BeautifulSoup(page, "html.parser")
    out, bad = [], 0
    for card in soup.select("div.base-search-card"):
        m = re.search(r"jobPosting:(\d+)", card.get("data-entity-urn") or "")
        title = card_text(card, "h3.base-search-card__title")
        if not m or not title:
            bad += 1
            continue
        posted = card.select_one("time")
        out.append({
            "title": title,
            "company": normalise_company(card_text(card, "h4.base-search-card__subtitle")),
            "location": card_text(card, "span.job-search-card__location"),
            "url": f"https://www.linkedin.com/jobs/view/{m.group(1)}",
            "description": "",
            "posted": (posted.get("datetime") if posted else "") or "",
            "source": "linkedin",
            "job_id": m.group(1),
        })
    return out, bad


def apply_kind(page: str) -> str:
    """"offsite" (Apply leads to the employer's site), "easy_apply" (LinkedIn's own form), or
    "" when the page says neither. Read from the tracking names on the Apply button
    (public_jobs_apply-link-offsite / -onsite, measured 2026-11-15). The offsite URL itself is
    not in the guest page — LinkedIn shows it only to a signed-in click, so /apply resolves it
    in the browser. /apply never touches Easy Apply."""
    if "apply-link-onsite" in page:
        return "easy_apply"
    if "apply-link-offsite" in page or "offsite-apply-icon" in page:
        return "offsite"
    return ""


def fetch_description(job_id: str, budget: Budget) -> tuple[str, str]:
    """(description, apply_kind). The returned text is untrusted data and is never
    interpreted — it is only ever matched against the literal substrings in
    search_profile.yaml."""
    page = get(POSTING_URL.format(job_id=job_id), budget) or ""
    soup = BeautifulSoup(page, "html.parser")
    el = (soup.select_one("div.show-more-less-html__markup")
          or soup.select_one("div.description__text"))
    return (strip_html(str(el)) if el else ""), apply_kind(page)


# ------------------------------------------------------------------ collection

def search(keywords: str, location: dict, seconds: int, max_pages: int,
           budget: Budget) -> list[dict]:
    """One (keyword, location) pair, paged. Raises Stop; other failures are the caller's
    to log. Pagination stops on an empty or short page — a page of fewer than 10 cards is
    the last one."""
    label = f"{keywords!r} @ {location['name']}"
    found, start = [], 0
    for page_no in range(max_pages):
        params = {"keywords": keywords, "location": location["name"],
                  "f_TPR": f"r{seconds}", "start": start}
        if location.get("remote"):
            params["f_WT"] = 2
        try:
            page = get(SEARCH_URL, budget, params)
        except Stop as exc:
            # The pages already read are kept: the caller adds them before it stops.
            exc.partial = found
            raise
        cards, bad = parse_cards(page)
        for c in cards:
            # Namespaced like board:ashby / feed:remotive / telegram:@handle, so the lane a
            # posting came from survives into job.md and the tracker.
            c["source"] = f"linkedin:{location.get('label') or slugify(location['name'])}"
            c["_remote_lane"] = bool(location.get("remote"))
        if bad:
            print(f"  ! {bad} of {bad + len(cards)} cards missing title/id for {label}",
                  file=sys.stderr)
        if not cards:
            # 200 OK with a substantial body and no cards is the shape a markup change
            # takes. A short body with no cards is a legitimately empty result set — a
            # narrow keyword inside a 30h window returns near-nothing — and must stay
            # quiet, or the alarm cries wolf twice a day and stops being read.
            if len(page or "") > EMPTY_BODY_BYTES:
                print(f"  ! {label}: 0 cards in {len(page)} bytes — markup may have changed",
                      file=sys.stderr)
            break
        found += cards
        start += len(cards)
        if len(cards) < 10:
            break
    return found


def load_config(profile: str = "linkedin") -> dict:
    doc = jobprofile.load_sources()
    return (doc.get("sources") or {}).get(profile) or {}


def first_run(source_key: str = SOURCE_KEY) -> bool:
    """True until this source has completed one run.

    Generalises what telegram already did with
    first_run_lookback_hours: a source that has never run has no history to lean on, so its
    first pass looks back two weeks instead of the usual window. Otherwise a new source is
    switched on and sees only whatever happened to be posted in the last 30 hours, which
    makes it look useless on the day you are deciding whether it is worth keeping.

    The marker is per source key. Deleting `<source>.first_run_utc` from state/last_run.json
    deliberately re-arms it — that is the supported way to re-widen after adding a search
    lane, which is a new source in all but name.
    """
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


def load_seen() -> set[str]:
    """Keys of every track's seen file (jobprofile.tracks()). A key any track has handled
    costs no description request."""
    # Until 2026-11-15 this read seen.json and seen_bi.json only, so with four tracks the
    # data_engineer and part_time keys were never "seen" here: cards they had judged stayed in
    # linkedin_pending.json and had their descriptions re-fetched, out of a 42-request budget.
    keys: set[str] = set()
    paths = {SEEN, SEEN.with_name("seen_bi.json")}
    paths |= {ROOT / t["seen_file"] for t in jobprofile.tracks()}
    for path in paths:
        try:
            keys |= set((json.loads(path.read_text(encoding="utf-8")) or {}).get("entries", {}))
        except Exception:
            pass
    return keys


def main() -> int:
    # --profile picks the config block AND the state key, so a second search (linkedin_bi,
    # 2026-10-05) gets its own first-run marker and its own two-week first pass instead of
    # inheriting the AE lane's history. Parsed first because the other defaults read cfg.
    global SOURCE_KEY
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--profile", default="linkedin")
    SOURCE_KEY = pre.parse_known_args()[0].profile
    cfg = load_config(SOURCE_KEY)
    if not cfg:
        sys.exit(f"no sources.{SOURCE_KEY} block in {SOURCES}")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="linkedin",
                    help="config block in sources.yaml and key in state/last_run.json "
                         "(linkedin, linkedin_bi)")
    ap.add_argument("--json", type=Path, help="write results as JSONL here")
    ap.add_argument("--hours", type=int, default=None,
                    help="freshness window; defaults to lookback_hours, or "
                         "first_run_lookback_hours if this source has never run")
    ap.add_argument("--max-pages", type=int, default=cfg.get("max_pages", 2),
                    help="search pages per keyword/location pair")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="hard cap on HTTP requests; defaults to max_requests, or "
                         "first_run_max_requests on a source's first run")
    ap.add_argument("--no-descriptions", action="store_true",
                    help="skip phase 2 (debugging)")
    ap.add_argument("--ignore-seen", action="store_true",
                    help="fetch descriptions even for postings already in state/seen.json")
    ap.add_argument("--any-location", action="store_true",
                    help="keep postings that are neither full remote nor in the home country (debugging)")
    ap.add_argument("--any-language", action="store_true",
                    help="keep postings not written in English (debugging)")
    ap.add_argument("--all", action="store_true", help="skip the title filter (debugging)")
    args = ap.parse_args()

    profile = jobprofile.load_search_profile()
    # Only the roles profile/routing.yaml routes to this source.
    profile = role_routing.apply(profile, SOURCE_KEY)
    titles, excl, desc_excl = build_filters(profile)
    seen = set() if args.ignore_seen else load_seen()

    # A first run needs a bigger budget as well as a bigger window, or the two-week rule is
    # half a rule: measured 2026-09-11, 336h over both lanes is 12 searches and 54
    # descriptions, and the everyday budget of 45 would truncate it to 33. The postings it
    # missed would never come back either, because the next run is back to 30 hours.
    is_first = first_run(SOURCE_KEY)
    if args.hours is not None:
        hours = args.hours
    elif is_first:
        hours = cfg.get("first_run_lookback_hours", 336)
        print(f"  first run for this source — looking back {hours}h instead of the usual "
              f"{cfg.get('lookback_hours', 30)}h", file=sys.stderr)
    else:
        hours = cfg.get("lookback_hours", 30)

    if args.max_requests is not None:
        max_requests = args.max_requests
    elif is_first:
        max_requests = cfg.get("first_run_max_requests",
                               cfg.get("max_requests", MAX_REQUESTS) * 2)
        print(f"  first run — request budget raised to {max_requests}", file=sys.stderr)
    else:
        max_requests = cfg.get("max_requests", MAX_REQUESTS)

    # The title gate holds only the roles profile/routing.yaml routes to this profile (applied
    # to `profile` above). Narrow on LinkedIn because `keywords` is full-text over title AND
    # description: measured once, "analytics engineer" across one country returned 53 postings,
    # 36 past the full gate, 2 actually titled Analytics Engineer. This used to be
    # `title_families` in the source block. title_exclusions are untouched.
    print(f"  title gate (profile/routing.yaml): {sorted(profile['role_families'])}",
          file=sys.stderr)

    require_english = (cfg.get("require_english_description", True)
                       and not args.any_language)
    # The old key names are still read, so an older config keeps its gate.
    require_geo = (cfg.get("require_remote_or_home",
                           cfg.get("require_remote_or_spain",
                                   cfg.get("require_remote_or_barcelona", True)))
                   and not args.any_location)
    home = jobprofile.geo_home(profile)
    keywords = cfg.get("keywords") or DEFAULT_KEYWORDS
    locations = cfg.get("locations") or DEFAULT_LOCATIONS
    seconds = hours * 3600
    budget = Budget(max_requests, tuple(cfg.get("delay_seconds") or DELAY),
                    float(cfg.get("rate_limit_backoff_seconds", RATE_LIMIT_BACKOFF)))
    stopped = ""

    # ---- phase 1: search listings, on a share of the budget only.
    # Until 2026-11-14 the listings could spend the whole budget: 6 keywords x 2 lanes x up to
    # 6 pages is more than 45 requests, two runs in a row fetched 0 descriptions, and the
    # pending queue meant to be "fetched first" grew to 245 cards, all expiring unread. The
    # rest of the budget is held for phase 2, pending cards first. Running out of the listing
    # share ends the listing, not the run; a 429 or repeated failures still end both.
    full_limit = budget.limit
    share = float(cfg.get("search_share", 0.5))
    if not args.no_descriptions:
        budget.limit = max(1, int(full_limit * share))
    pairs = [(k, loc) for k in keywords for loc in locations]
    print(f"Searching LinkedIn: {len(pairs)} keyword/location pairs, last {hours}h, "
          f"up to {args.max_pages} pages each, budget {full_limit} requests "
          f"({budget.limit} for listings)...", file=sys.stderr)

    cards = []
    for kw, loc in pairs:
        try:
            got = search(kw, loc, seconds, args.max_pages, budget)
        except Stop as exc:
            cards += getattr(exc, "partial", [])
            if budget.used >= budget.limit and budget.limit < full_limit:
                print(f"  listing share used ({budget.limit} requests) — the rest is for "
                      f"descriptions", file=sys.stderr)
            else:
                stopped = str(exc)
            break
        except Exception as exc:
            print(f"  ! {kw} @ {loc['name']}: {type(exc).__name__}", file=sys.stderr)
            continue
        print(f"  {len(got):>5} cards for {kw!r} @ {loc['name']}", file=sys.stderr)
        cards += got

    # The same posting shows up under several keywords. Collapse before spending a request
    # on each description.
    by_id: dict[str, dict] = {}
    for c in cards:
        by_id.setdefault(c["job_id"], c)
    budget.limit = full_limit
    if cards:
        print(f"  {len(cards)} cards, {len(by_id)} distinct postings", file=sys.stderr)
    elif budget.used:
        print("  !! linkedin returned no cards at all this run — check the endpoint or "
              "the markup", file=sys.stderr)

    # Cards a 429 left without a description on an earlier run. Until 2026-10-28 they were
    # emitted blank, never judged, and fell out of the 30h window for good — sixteen
    # linkedin_bi remote-EU records were lost that way on 2026-10-26. They come back here, a
    # week at most, and are fetched first.
    pending_path = ROOT / "state" / f"{SOURCE_KEY}_pending.json"
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else []
    except Exception:
        pending = []
    week_ago = (datetime.now(timezone.utc) - timedelta(days=PENDING_DAYS)).isoformat()
    revived = 0
    for p in pending:
        if p.get("_pending_since", "") < week_ago or key_of(p) in seen:
            continue
        rec = by_id.setdefault(p["job_id"], p)
        rec["_pending_since"] = p["_pending_since"]
        revived += 1
    if revived:
        print(f"  {revived} carried over from a run a 429 cut short", file=sys.stderr)

    # ---- filter on the cheap data, before spending requests on descriptions.
    # geo_verdict on an empty description is strictly conservative: the home-city and
    # remote-in-location branches still fire, and everything it cannot decide lands on
    # 'unclear', which is passed through. It can be less specific; it cannot wrongly drop.
    shortlist = []
    for rec in by_id.values():
        geo0 = geo_verdict(rec, profile)
        if geo0 == "reject":
            continue
        if not args.all and not title_matches(rec["title"], titles, excl):
            continue
        rec["_geo0"] = geo0
        shortlist.append(rec)
    # Home city first, so that if the budget runs out it runs out on the pile that geo
    # cannot resolve anyway, never on a confirmed home-city role.
    # Carried-over cards go first of all: they have already waited one run.
    shortlist.sort(key=lambda r: ("_pending_since" not in r,
                                  {home["city"]: 0, "remote": 1, home["country"]: 2}.get(r["_geo0"], 3)))
    print(f"  {len(shortlist)} pass the title filter", file=sys.stderr)

    # ---- phase 2: descriptions, one request each
    fetched = gone = 0
    if not args.no_descriptions:
        for rec in shortlist:
            # Budget optimisation, NOT a filtering decision: this key is already in
            # state/seen.json, so Stage 2 will drop the record without ever reading the
            # description. Emitting it with an empty one is bit-identical downstream.
            if key_of(rec) in seen:
                continue
            try:
                rec["description"], kind = fetch_description(rec["job_id"], budget)
                if kind:
                    rec["li_apply"] = kind
                fetched += 1
            except Stop as exc:
                stopped = str(exc)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    rec["_gone"] = True    # removed: neither queued again nor emitted
                    gone += 1
                    continue
                print(f"  ! description {rec['job_id']}: HTTPError {exc.code}",
                      file=sys.stderr)
            except Exception as exc:
                print(f"  ! description {rec['job_id']}: {type(exc).__name__}",
                      file=sys.stderr)
    if gone:
        print(f"  {gone} postings removed by LinkedIn (404) — dropped from the queue",
              file=sys.stderr)
    shortlist = [r for r in shortlist if not r.get("_gone")]

    # Whatever is still blank and unseen waits for the next run. Taken before the final
    # filters, which pop job_id.
    carry = [{**{k: v for k, v in r.items() if k != "_geo0"},
              "_pending_since": r.get("_pending_since", now_iso)}
             for r in shortlist
             if not args.no_descriptions and not r["description"] and key_of(r) not in seen]

    # ---- final filters, now that there is a description to read
    kept, lang_dropped, geo_dropped = [], [], []
    for rec in shortlist:
        if require_english and not is_english(rec["description"]):
            if "es" in jobprofile.soft_posting_languages() and is_spanish(rec["description"]):
                rec["language"] = "es"     # Stage 3 decides; see is_spanish
            else:
                lang_dropped.append(f"{rec['company']} — {rec['title'][:40]}")
                continue
        # excluded_by_description needs a description; running it before phase 2 would be a
        # silent no-op that passed every data_analyst-family posting. When the fetch failed
        # it still cannot fire — such a record reaches the model without the backstop, which
        # is acceptable only because Stage 3 of daily_scan.md re-applies the same gate by
        # hand. Failing closed would trade a permanent false negative for a network blip.
        if not args.all and excluded_by_description(rec, desc_excl):
            continue
        rec["geo"] = geo_verdict(rec, profile)
        if rec["geo"] == "unclear" and FULL_REMOTE.search(rec["description"]):
            rec["geo"] = "remote"
        # A posting found through a lane with f_WT=2 carries LinkedIn's own workplace-type
        # flag, set by the employer. That is real evidence the role IS remote — it is not
        # evidence of the remote SCOPE, because LinkedIn shows the employer's city and its
        # flag is happily used for remote-within-one-country roles. So it counts as "remote"
        # here and the scope question goes to Stage 3, where the description gets read. The
        # source field says which lane it came from, so that check is possible.
        if rec.get("_remote_lane") and rec["geo"] == "unclear":
            rec["geo"] = "remote"
        # No location stated at all is worldwide, not a narrow scope (the profile's geography
        # rule: silence is a pass). Distinct from the case below: there a
        # place WAS named and no remote wording was found, which is still a drop. LinkedIn has
        # always supplied a location on every card measured so far (13/13 on 2026-09-18), so
        # this is a guard on the markup changing, not a path the source exercises today. Kept
        # explicit because the alternative is dropping everything silently if it ever does.
        if not rec["location"].strip() and rec["geo"] == "unclear":
            rec["geo"] = "worldwide"
        if rec["geo"] == "reject":
            continue
        # If it is not full remote, only the home city or home country counts — the profile's
        # geography rule, enforced here instead of being left to the model. Strict on purpose:
        # a genuinely remote posting that never says so in words FULL_REMOTE recognises is
        # dropped. That is the intended trade, and it survives the unspecified-location rule
        # unchanged — what that rule covers is a posting
        # that names no place at all, handled above. A record reaching here has a place on it.
        if require_geo and rec["geo"] not in ("remote", home["city"], home["country"], "worldwide"):
            geo_dropped.append(f"{rec['company']} — {rec['location'] or 'location unstated'}")
            continue
        rec.pop("job_id")
        rec.pop("_geo0", None)
        rec.pop("_remote_lane", None)
        rec.pop("_pending_since", None)
        rec["dedup_key"] = key_of(rec)
        kept.append(rec)

    # In-run dedup on the key, the same step collect_boards.py ends with. Distinct job ids
    # are not distinct vacancies: LinkedIn lists one role once per location it is open in,
    # and measured 2026-09-09 the same EY posting came back twice under two ids. Keeping the
    # first is safe because shortlist was sorted home-city-first, so the more specific geo
    # is the one that survives.
    unique, keys = [], set()
    for rec in kept:
        if rec["dedup_key"] in keys:
            continue
        keys.add(rec["dedup_key"])
        unique.append(rec)
    if len(unique) < len(kept):
        print(f"  {len(kept) - len(unique)} same-key duplicates collapsed", file=sys.stderr)
    kept = unique

    counts: dict[str, int] = {home["city"]: 0, home["country"]: 0, "remote": 0,
                              "worldwide": 0, "unclear": 0}
    for r in kept:
        counts[r["geo"]] = counts.get(r["geo"], 0) + 1
    blank = sum(1 for r in kept if not r["description"])
    print(f"\n  {len(kept)} matching: {counts[home['city']]} {home['city']}, "
          f"{counts[home['country']]} {home['country']} (other city), "
          f"{counts['remote']} remote, {counts['worldwide']} worldwide, "
          f"{counts['unclear']} unclear "
          f"({budget.used} requests, {fetched} descriptions)", file=sys.stderr)
    if geo_dropped:
        print(f"  {len(geo_dropped)} dropped as neither full remote nor in {home['country_name']}:",
              file=sys.stderr)
        for x in geo_dropped:
            print(f"      {x}", file=sys.stderr)
    if lang_dropped:
        print(f"  {len(lang_dropped)} dropped as not written in English:", file=sys.stderr)
        for x in lang_dropped:
            print(f"      {x}", file=sys.stderr)
    if blank:
        print(f"  {blank} without a description — already seen, or the fetch failed; "
              f"the model must open the link to score those", file=sys.stderr)
    if stopped:
        print(f"  ! stopped early: {stopped} — results are partial", file=sys.stderr)

    out = "\n".join(json.dumps(r, ensure_ascii=False) for r in kept)
    if args.json:
        args.json.write_text(out + "\n", encoding="utf-8")
        print(f"  written to {args.json}", file=sys.stderr)
        pending_path.write_text(json.dumps(carry, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")
        if carry:
            print(f"  {len(carry)} left without a description, queued in {pending_path.name} "
                  f"for the next run", file=sys.stderr)
        mark_run(SOURCE_KEY)   # last, and only on a real pipeline run
    else:
        print(out)
    return 0


def key_of(rec: dict) -> str:
    """Identical to collect_boards.py, deliberately: a posting found both here and on the
    company's own ATS board must produce the same key and collide in state/seen.json. This
    must never drift — the collision is the entire point of this source."""
    return hashlib.sha1(
        f"{rec['company'].strip().lower()}|{rec['title'].strip().lower()}".encode()
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
