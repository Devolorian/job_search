#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Collect vacancies from company career-board APIs and open job feeds.

Deterministic collection, done in Python rather than by the model: these are structured
JSON APIs, and having an LLM page through them would be slower, costlier and less reliable.
The model's job starts after this — judging fit, summarising, tailoring the CV.

Sources, in order of measured value (2026-09-07):
  1. Company ATS boards (Greenhouse / Ashby / Lever / Workable). Official public endpoints,
     no auth, no robots.txt conflict. 42 companies probed -> 26 role matches, including
     "Senior Analytics Engineer - Barcelona".
  2. Open job feeds. 469 postings scanned -> 9 role matches, mostly noise. Kept as a
     supplement, disabled by default.

Outputs newline-delimited JSON on stdout, and a summary on stderr.

Usage:
    uv run scripts/collect_boards.py                     # boards only
    uv run scripts/collect_boards.py --feeds             # boards + open feeds
    uv run scripts/collect_boards.py --json out.jsonl
    uv run scripts/collect_boards.py --resolve-only      # refresh the provider cache
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import html
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import role_routing  # scripts/role_routing.py — which roles this source searches

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = jobprofile.COMPANIES
PROFILE = jobprofile.search_profile_path()
CACHE = ROOT / "state" / "ats_boards.json"
OVERRIDES = ROOT / "state" / "board_overrides.json"
DISCOVERED = ROOT / "state" / "discovered_boards.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"}

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


def to_iso(value) -> str:
    """Normalise whatever a provider calls a date into ISO-8601 UTC, or "" if unreadable.

    Every provider was trusted to hand back a date string and stringified as-is until
    2026-09-24, when a Handicap International posting in the run log turned out to carry
    `"posted": "1790180410"`. Two sources do not speak ISO:

      - arbeitnow's `created_at` is epoch SECONDS
      - lever's `createdAt` is epoch MILLISECONDS — 13 digits, which parsed as seconds lands in
        the year 58181, so a naive conversion raises rather than lying quietly

    Everything else (greenhouse, ashby, workable, smartrecruiters, recruitee, personio,
    teamtailor, landing.jobs) already returns ISO-8601, and those pass through untouched. The
    field matters because it is the only way to tell a posting from this week from one that has
    sat on a board for two years, and it is copied into every vacancy folder's source.json.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        raw = str(int(value))
    else:
        raw = str(value).strip()
        if not raw:
            return ""
    if raw.isdigit():
        n = int(raw)
        if n > 10_000_000_000:      # 11+ digits: milliseconds
            n //= 1000
        try:
            return datetime.fromtimestamp(n, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
        return raw          # a date format we do not parse is still better than nothing


def extract_jobs(provider: str, j):
    """Each provider names its list differently; keep the shape-knowledge in one place."""
    if provider in ("lever", "personio", "teamtailor"):
        return j if isinstance(j, list) else []
    if provider == "smartrecruiters":
        return j.get("content") or []
    if provider == "recruitee":
        return j.get("offers") or []
    return j.get("jobs") or []


def looks_like_board(provider: str, j) -> bool:
    """Did this provider answer with ITS OWN payload shape, regardless of how many jobs it holds?

    The difference matters and cost something before 2026-09-21: resolve() used to accept a
    provider only when the board had at least one opening, so a live board with nothing posted
    that week was recorded as "no reachable board", never cached, and re-probed across all eight
    providers on every run — twice a day, forever. Measured on the 2026-09-21 import, three of
    the newly added companies were in exactly that state (one on Personio, Wallarm on
    Recruitee, Virtuozzo on Workable all answer 200 with an empty list), and they were being
    reported alongside genuinely dead boards like BitFury's, whose Greenhouse slug 404s.

    An empty board is a company that is not hiring this week. A 404 is a company that has left.
    Verified 2026-09-21 against a nonsense slug: greenhouse, ashby, lever, workable, recruitee
    and teamtailor all answer 404, and personio 307s away, so for those an answer in the right
    shape IS the board existing.

    SMARTRECRUITERS IS THE EXCEPTION and must keep the old rule: it answers **200 with
    `{"content": []}` for any slug at all**, real or invented. Accepting its empty payload made
    every unresolved company resolve to SmartRecruiters — the first run after this function was
    introduced reported "63 live, 0 with no reachable board", including BitFury, whose board is
    a 404. An empty SmartRecruiters response cannot be told apart from a company that has no
    SmartRecruiters account, so it only counts when it carries at least one posting.
    """
    if provider == "smartrecruiters":
        return bool(extract_jobs(provider, j))
    if provider in ("lever", "personio", "teamtailor"):
        return isinstance(j, list)
    if not isinstance(j, dict):
        return False
    key = {"recruitee": "offers"}.get(provider, "jobs")
    return key in j

FEEDS = {
    "landing.jobs":  "https://landing.jobs/api/v1/jobs?limit=200",
    "arbeitnow":     "https://www.arbeitnow.com/api/job-board-api",
    "remotive":      "https://remotive.com/api/remote-jobs",
    "workingnomads": "https://www.workingnomads.com/api/exposed_jobs/",
    # Added 2026-09-21..24 after measuring seven candidate sources against this project's own
    # title gate. Jobicy filtered to the data bucket and the European geo was the only one worth
    # taking: 36 records, 7 past the title gate, all 7 in Europe or worldwide — a 19% density
    # against 0-10% elsewhere. The unfiltered version returns 50 records of which 13 are US
    # postings the geography gate then throws away, so the filters stay in the URL.
    "jobicy":        "https://jobicy.com/api/v2/remote-jobs?count=50&geo=europe&industry=data-science",
}
# Measured and REJECTED, so they are not re-researched later (all probed 2026-09-24):
#   EURES (europa.eu/eures, the official EU portal, public jv-search endpoint) — reachable and
#     robots-permitted, 1,955 records for "data engineer" in Spain, and with BEST_MATCH sorting
#     exactly 3 of the top 50 pass the title gate: "DATA DATA ENGINEER con certificado de
#     DISCAPACIDAD", "Data Analyst & Logistics (TELETRABAJO)" and a Spanish-speaker crowdsourcing
#     gig. All three are Spanish-language and die on the English-only rule anyway. The portal is
#     dominated by national employment-service listings, so it is the wrong shape for this search.
#   remoteok (?tag=data) — 100 records, 10 past the title gate, ZERO in Europe.
#   himalayas — caps at 20 records a call, 0 past the gate. (Superseded 2026-10-27: its search
#     API now filters by query and country; it is scripts/collect_himalayas.py.)
#   weworkremotely — the programming RSS has no data roles; there is no data category.
#   tecnoempleo, getmanfred, jobfluent — HTML only, no feed or API found.
#   InfoJobs and Adzuna — real APIs, both need a registered key. Adzuna's free tier covers Spain
#     and is the best remaining option if a search ever needs Spanish-language volume.


def fetch(url: str, timeout: int = 20, retries: int = 2):
    """GET and parse JSON, retrying once on a throttle or a transient server error.

    The retry was added 2026-09-21, when companies.yaml went from 63 slugs to 171 and every
    Workable-hosted company went "unreachable" in one run — Semrush, CodeSignal, Grammarly,
    Kaspersky, EPAM, Glovo, Revolut and TravelPerk all answer 200 with their own name when asked
    by hand. Without a retry a throttled request is indistinguishable from a dead board, and a
    company silently drops out of the pipeline for the day.
    """
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


# Board slugs are lowercase ("monzo", "taxdome"), but the slug is the only company name the
# API gives us. Title-casing blindly would produce "Taxdome", so keep the exceptions explicit.
COMPANY_NAMES = {
    "monzo": "Monzo", "preply": "Preply", "eneba": "Eneba", "qonto": "Qonto",
    "taxdome": "TaxDome", "fivetran": "Fivetran", "datadog": "Datadog",
    "supabase": "Supabase", "pennylane": "Pennylane", "grafanalabs": "Grafana Labs",
    "gitlab": "GitLab", "elastic": "Elastic", "typeform": "Typeform", "newrelic": "New Relic",
    "wise": "Wise", "n26": "N26", "bitpanda": "Bitpanda", "oyster": "Oyster",
    "posthog": "PostHog", "airbyte": "Airbyte", "hightouch": "Hightouch",
    "factorialhr": "Factorial", "travelperk": "TravelPerk", "jobandtalent": "Jobandtalent",
    "payhawk": "Payhawk", "personio": "Personio", "cabify": "Cabify", "wallapop": "Wallapop",
    "adevinta": "Adevinta", "seedtag": "Seedtag", "marfeel": "Marfeel", "amenitiz": "Amenitiz",
}


def display_name(slug: str) -> str:
    return COMPANY_NAMES.get(slug.lower(), slug)


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


# --------------------------------------------------------------- normalisation

def normalise(provider: str, company: str, raw: dict) -> dict | None:
    """One shape for every provider, so downstream code never branches on source."""
    if provider == "greenhouse":
        title = raw.get("title", "")
        loc = (raw.get("location") or {}).get("name", "")
        url = raw.get("absolute_url", "")
        desc = strip_html(raw.get("content", ""))
        posted = raw.get("updated_at") or raw.get("first_published")
    elif provider == "ashby":
        title = raw.get("title", "")
        loc = raw.get("location") or ""
        url = raw.get("jobUrl") or raw.get("applyUrl") or ""
        desc = strip_html(raw.get("descriptionHtml") or raw.get("descriptionPlain") or "")
        posted = raw.get("publishedAt")
    elif provider == "lever":
        title = raw.get("text", "")
        loc = (raw.get("categories") or {}).get("location") or ""
        url = raw.get("hostedUrl", "")
        desc = strip_html(raw.get("descriptionPlain") or raw.get("description") or "")
        posted = raw.get("createdAt")
    elif provider == "workable":
        title = raw.get("title", "")
        loc = ", ".join(x for x in [raw.get("city"), raw.get("country")] if x)
        url = raw.get("url") or raw.get("application_url") or ""
        desc = strip_html(raw.get("description", ""))
        posted = raw.get("published_on")
    elif provider == "smartrecruiters":
        title = raw.get("name", "")
        loc = ", ".join(x for x in [(raw.get("location") or {}).get("city"),
                                    (raw.get("location") or {}).get("country")] if x)
        url = raw.get("applyUrl") or raw.get("ref") or ""
        desc = strip_html(json.dumps(raw.get("jobAd") or {}, ensure_ascii=False))
        posted = raw.get("releasedDate")
    elif provider == "recruitee":
        title = raw.get("title", "")
        loc = ", ".join(x for x in [raw.get("city"), raw.get("country")] if x)
        url = raw.get("careers_url") or raw.get("careers_apply_url") or ""
        desc = strip_html(raw.get("description", ""))
        posted = raw.get("published_at")
    elif provider in ("personio", "teamtailor"):
        title = raw.get("name") or raw.get("title") or ""
        loc = raw.get("office") or raw.get("location") or ""
        url = raw.get("url") or raw.get("careers_url") or ""
        desc = strip_html(str(raw.get("description") or raw.get("body") or ""))
        posted = raw.get("createdAt") or raw.get("created_at") or ""
    else:
        return None
    if not title:
        return None
    return {
        "title": title.strip(),
        "company": display_name(company),
        "location": str(loc).strip(),
        "url": url,
        "description": desc,
        "posted": to_iso(posted),
        "source": f"board:{provider}",
    }


def normalise_feed(feed: str, raw: dict) -> dict | None:
    """Feed payloads differ more than the ATS ones; landing.jobs in particular has no
    company field at all - the company is a path segment in the job URL."""
    if feed == "landing.jobs":
        url = raw.get("url") or ""
        m = re.search(r"landing\.jobs/at/([^/]+)/", url)
        # locations is a list of {city, country} dicts, not strings.
        locs = raw.get("locations") or []
        parts = []
        for x in locs if isinstance(locs, list) else []:
            if isinstance(x, dict):
                parts.append(", ".join(v for v in (x.get("city"), x.get("country")) if v))
            else:
                parts.append(str(x))
        loc = " / ".join(p for p in parts if p)
        if raw.get("remote"):
            loc = (loc + " (Remote)").strip()
        salary = ""
        if raw.get("gross_salary_low"):
            salary = (f"{raw['gross_salary_low']}-{raw.get('gross_salary_high','')} "
                      f"{raw.get('currency_code','')}").strip()
        return {
            "title": (raw.get("title") or "").strip(),
            "company": (m.group(1).replace("-", " ").title() if m else ""),
            "location": loc,
            "url": url,
            "description": strip_html(str(raw.get("role_description") or "")
                                      + " " + str(raw.get("main_requirements") or "")),
            "posted": to_iso(raw.get("published_at") or raw.get("created_at")),
            "salary": salary,
            "relocation": bool(raw.get("relocation_paid")),
            "source": "feed:landing.jobs",
        }

    keys = {
        "arbeitnow":     ("title", "company_name", "location", "url"),
        "remotive":      ("title", "company_name", "candidate_required_location", "url"),
        "workingnomads": ("title", "company_name", "location", "url"),
        "jobicy":        ("jobTitle", "companyName", "jobGeo", "url"),
    }[feed]
    tk, ck, lk, uk = keys
    title = raw.get(tk) or ""
    if not title:
        return None
    company = raw.get(ck)
    if isinstance(company, dict):
        company = company.get("name", "")
    # jobicy names these differently from the other three; keep it in the shared path rather
    # than forking a near-identical normaliser.
    desc = raw.get("description") or raw.get("body") or raw.get("jobDescription") or ""
    return {
        "title": str(title).strip(),
        "company": str(company or "").strip(),
        "location": str(raw.get(lk) or "").strip(),
        "url": raw.get(uk) or "",
        "description": strip_html(str(desc)),
        "posted": to_iso(raw.get("created_at") or raw.get("publication_date")
                         or raw.get("pubDate")),
        "source": f"feed:{feed}",
    }


# --------------------------------------------------------------- provider probe

def resolve(slug: str, cached: str | None) -> tuple[str, str | None, int]:
    """Which ATS does this company use? Cached, because it changes maybe once a year."""
    order = [cached] + [p for p in PROVIDERS if p != cached] if cached else list(PROVIDERS)
    # A provider WITH postings always wins. An empty-but-real board is remembered as a fallback
    # and the sweep continues — stopping at the first empty board is what made Glovo, Revolut,
    # TravelPerk, Adevinta, FactorialHR and BitFury all resolve to Workable on 2026-09-21:
    # each has a real but dormant Workable account, and the sweep never reached the board they
    # actually post on. The fallback is still returned when nothing has jobs, so the run can say
    # "alive, not hiring" instead of "no reachable board".
    empty: str | None = None
    for prov in order:
        if not prov:
            continue
        try:
            j = fetch(PROVIDERS[prov].format(slug=slug), timeout=12)
        except Exception:
            continue
        jobs = extract_jobs(prov, j)
        if jobs:
            return slug, prov, len(jobs)
        if empty is None and looks_like_board(prov, j):
            empty = prov
    return slug, empty, 0


def board_jobs(slug: str, prov: str) -> tuple[list[dict], int | None]:
    """Postings for one board, plus the HTTP status when it failed.

    The status is returned rather than only logged so main() can act on a 404: a declared
    provider is trusted and never re-probed, which is right until the slug dies. Nine did
    between 2026-09-21 and 2026-09-22 — Lightspeed, Temporal, Chattermill, BitFury, Rarible,
    Palta, Volka, Improvado and DataMilk all 404 on a board that answered the day before — and
    without this they would have logged the same error twice a day forever.
    """
    try:
        j = fetch(PROVIDERS[prov].format(slug=slug), timeout=25)
    except urllib.error.HTTPError as exc:
        # The status code is the whole diagnosis: 404 means the declared slug is gone and the
        # config needs fixing, 429 means we are polling too hard and the board is fine. Both
        # printed as "HTTPError" for two scheduled runs on 2026-09-22 and looked identical.
        if exc.code != 404:
            print(f"  ! {slug} ({prov}): HTTP {exc.code}", file=sys.stderr)
        return [], exc.code
    except Exception as exc:
        print(f"  ! {slug} ({prov}): {type(exc).__name__}", file=sys.stderr)
        return [], None
    jobs = extract_jobs(prov, j)
    out = []
    for raw in jobs:
        rec = normalise(prov, slug, raw)
        if rec:
            rec["board_slug"] = slug.lower()
            out.append(rec)
    return out, None


# --------------------------------------------------------------- filtering

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feeds", action="store_true", help="also poll the open job feeds")
    ap.add_argument("--resolve-only", action="store_true", help="refresh the provider cache")
    ap.add_argument("--json", type=Path, help="write results as JSONL here")
    ap.add_argument("--all", action="store_true", help="skip the title filter (debugging)")
    args = ap.parse_args()

    companies = yaml.safe_load(COMPANIES.read_text(encoding="utf-8"))["companies"]
    # An explicit `name:` beats the slug as the company name — "CollectlyInc" is a slug, not a name.
    for c in companies:
        if c.get("name"):
            COMPANY_NAMES[c["slug"].lower()] = c["name"]
    profile_raw = jobprofile.load_search_profile()
    # One file, two routing sources (2026-10-30): company boards and open feeds can be given
    # different roles in profile/routing.yaml. `profile` (geo, scoring) is the boards' copy.
    profile = role_routing.apply(profile_raw, "company_boards")
    titles, excl, desc_excl = build_filters(profile)
    feed_titles, _, feed_desc_excl = build_filters(role_routing.apply(profile_raw, "open_feeds"))

    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    providers = cache.get("providers", {})

    # An explicit `provider:` in companies.yaml is TRUSTED, not re-probed. Every one of them was
    # written by scripts/import_company_list.py from an ATS URL that
    # scripts/resolve_board_slugs.py had already verified against the board owner's own name, so
    # probing eight endpoints to rediscover it is pure cost — and at 171 slugs that cost broke
    # things: on 2026-09-21 the sweep throttled and Semrush, CodeSignal, Grammarly, Kaspersky
    # and EPAM were all reported "no reachable board" while answering 200 with their own names
    # when asked one at a time. Only a slug with no declared provider is probed.
    declared = {c["slug"]: c["provider"] for c in companies if c.get("provider")}

    # Boards found by search (scripts/discover_boards.py, since 2026-10-21). The slug came out of
    # the board's own URL and was verified against its API, so it is as trusted as a declared
    # provider; companies.yaml wins on a clash, since it is curated by hand.
    # board_discovery.ignore_slugs is honoured here too (2026-10-31): discover_boards.py only
    # consulted it for NEW hits, so a recruiter added to the list after its board was found
    # (Toptal, remote-recruitment) kept being polled.
    srcs = jobprofile.load_sources()
    ignored = {x.lower() for x in
               ((srcs.get("sources") or {}).get("board_discovery") or {}).get("ignore_slugs", [])}
    discovered = {}
    if DISCOVERED.exists():
        for slug, rec in json.loads(DISCOVERED.read_text(encoding="utf-8"))["boards"].items():
            if slug.lower() in ignored:
                continue
            if slug not in declared and not any(c["slug"] == slug for c in companies):
                discovered[slug] = rec["provider"]
                if rec.get("name"):
                    COMPANY_NAMES.setdefault(slug.lower(), rec["name"])
    declared.update(discovered)
    if discovered:
        print(f"  + {len(discovered)} board(s) from search discovery", file=sys.stderr)

    # Runtime corrections to what companies.yaml declares, learned by the 404 re-probe below and
    # kept in state/ rather than edited into the curated config. Without them the healing is
    # redone every run: the declared provider is stale by definition, so it wins again tomorrow
    # and the same nine boards 404 again. A slug mapped to null is one that 404'd and was found
    # nowhere; it is skipped for a week rather than re-probed across eight providers twice a day.
    overrides = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    stale_after = datetime.now(timezone.utc) - timedelta(days=7)
    skip: set[str] = set()
    for slug, rec in list(overrides.items()):
        when = datetime.fromisoformat(rec["checked_utc"])
        if rec.get("provider"):
            declared[slug] = rec["provider"]
        elif when > stale_after:
            skip.add(slug)
    if skip:
        print(f"  skipping {len(skip)} board(s) confirmed gone within the last 7 days: "
              f"{', '.join(sorted(skip))}", file=sys.stderr)
        declared = {k: v for k, v in declared.items() if k not in skip}
    providers.update(declared)
    slugs = [c["slug"] for c in companies if c["slug"] not in declared and c["slug"] not in skip]
    print(f"{len(declared)} boards declared ({len(discovered)} of them by discovery); "
          f"resolving the remaining {len(slugs)}...", file=sys.stderr)
    resolved, idle, dead = dict(declared), {}, []
    # 6, not 12: at 171 slugs the resolution sweep is ~8x bigger than the list it
    # was tuned for, and Workable started refusing whole batches.
    with cf.ThreadPoolExecutor(6) as ex:
        for slug, prov, n in ex.map(lambda s: resolve(s, providers.get(s)), slugs):
            if prov and n:
                resolved[slug] = prov
            elif prov:
                idle[slug] = prov          # real board, nothing posted right now
            else:
                dead.append(slug)
    print(f"  {len(resolved)} boards to poll ({len(declared)} declared), "
          f"{len(idle)} alive but not hiring, {len(dead)} with no reachable board"
          + (f": {', '.join(dead)}" if dead else ""), file=sys.stderr)
    if idle:
        print(f"      idle: {', '.join(sorted(idle))}", file=sys.stderr)

    # Only a board that actually served postings is cached as THE provider. An idle one is
    # recorded separately: it is a fact worth keeping (the account exists) but it must not
    # become the cached first guess, or the next run stops there again instead of finding the
    # board the company really posts on.
    CACHE.write_text(json.dumps(
        {"resolved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "providers": resolved, "idle": idle, "unreachable": dead}, indent=2) + "\n",
        encoding="utf-8")
    if args.resolve_only:
        return 0

    records, gone = [], []
    with cf.ThreadPoolExecutor(10) as ex:
        for (slug, _prov), (got, status) in zip(resolved.items(),
                                                ex.map(lambda kv: board_jobs(*kv), resolved.items())):
            records += got
            if status == 404:
                gone.append(slug)

    # A declared provider that 404s has a dead slug, so the one thing the declaration bought —
    # skipping the probe — is exactly what must be given back. Re-probe those, once, and use
    # whatever is found; anything still missing is reported so companies.yaml can be corrected.
    if gone:
        print(f"  {len(gone)} declared board(s) returned 404, re-probing: {', '.join(gone)}",
              file=sys.stderr)
        healed = {}
        with cf.ThreadPoolExecutor(6) as ex:
            for slug, prov, n in ex.map(lambda sl: resolve(sl, None), gone):
                if prov:
                    healed[slug] = prov
        for slug, prov in healed.items():
            got, _ = board_jobs(slug, prov)
            records += got
            resolved[slug] = prov
        still = [s for s in gone if s not in healed]
        print(f"      re-probed: {len(healed)} found elsewhere"
              + (f"; {len(still)} still missing: {', '.join(still)}" if still else ""),
              file=sys.stderr)
        for slug in still:
            resolved.pop(slug, None)
            dead.append(slug)
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for slug, prov in healed.items():
            overrides[slug] = {"provider": prov, "checked_utc": now_iso,
                               "was": declared.get(slug, "")}
        for slug in still:
            overrides[slug] = {"provider": None, "checked_utc": now_iso,
                               "was": declared.get(slug, "")}
        OVERRIDES.write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")
        CACHE.write_text(json.dumps(
            {"resolved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "providers": resolved, "idle": idle, "unreachable": dead}, indent=2) + "\n",
            encoding="utf-8")
    print(f"  {len(records)} postings from company boards", file=sys.stderr)

    if args.feeds:
        for name, url in FEEDS.items():
            try:
                j = fetch(url, timeout=25)
            except Exception as exc:
                print(f"  ! feed {name}: {type(exc).__name__}", file=sys.stderr)
                continue
            # landing.jobs and workingnomads return a bare list; the others wrap it.
            if isinstance(j, list):
                items = j
            else:
                items = j.get("data") or j.get("jobs") or []
            got = [r for r in (normalise_feed(name, x) for x in items) if r]
            records += got
            print(f"  {len(got):>5} from feed {name}", file=sys.stderr)

    # Roles that profile/routing.yaml pins to particular boards (data_vendor, 2026-10-30): checked
    # per posting against its board slug, with the family's own title and location exclusions.
    # A posting that matches one is stamped `family:` so triage.py knows what it is.
    scoped = role_routing.board_scoped(profile_raw, "company_boards")

    def scoped_family(r: dict) -> str | None:
        for s in scoped:
            if (r.get("board_slug") in s["boards"]
                    and title_matches(r["title"], s["titles"], excl + s["exclude_titles"])
                    and not (s["drop_re"]
                             and s["drop_re"].search(f"{r['location']} {r['title']}".lower()))):
                return s["name"]
        return None

    kept = []
    for r in records:
        feed = r["source"].startswith("feed:")
        t_list, d_excl = (feed_titles, feed_desc_excl) if feed else (titles, desc_excl)
        general = title_matches(r["title"], t_list, excl)
        fam = None if (general or feed) else scoped_family(r)
        if not args.all and not general and not fam:
            continue
        if fam:
            r["family"] = fam
        if not args.all and excluded_by_description(r, d_excl):
            continue
        r["geo"] = geo_verdict(r, profile)
        if r["geo"] == "reject":
            continue
        r["dedup_key"] = hashlib.sha1(
            f"{r['company'].strip().lower()}|{r['title'].strip().lower()}".encode()
        ).hexdigest()
        kept.append(r)

    seen, unique = set(), []
    for r in kept:
        if r["dedup_key"] in seen:
            continue
        seen.add(r["dedup_key"])
        unique.append(r)

    by_geo = {}
    for r in unique:
        by_geo[r["geo"]] = by_geo.get(r["geo"], 0) + 1
    print(f"\n  {len(unique)} matching, deduplicated: "
          + ", ".join(f"{v} {k}" for k, v in sorted(by_geo.items())), file=sys.stderr)

    out = "\n".join(json.dumps(r, ensure_ascii=False) for r in unique)
    if args.json:
        args.json.write_text(out + "\n", encoding="utf-8")
        print(f"  written to {args.json}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
