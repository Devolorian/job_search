# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Find the ATS board behind a company's own careers page, and prove it is the right company.

THE PROBLEM THIS SOLVES. Every curated company list worth importing gives an own-domain careers
URL (jetbrains.com/careers, careers.semrush.com) rather than an ATS link, and
`config/companies.yaml` needs an ATS slug. Guessing the slug from the display name is what left
209 entries of the 2026-09-21 hellonewjob import commented out: a guessed slug that happens to
exist polls a DIFFERENT company's board and its postings look entirely real.

Two things make guessing safe enough to automate:

  1. THE DOMAIN, NOT THE NAME. `careers.semrush.com` gives "semrush"; "VOLKA GAMES" gives
     "volkagames". The domain is what companies actually register with an ATS, and it is not a
     marketing string. Candidates are derived from the careers URL first, the name second.
  2. VERIFICATION. Greenhouse, Workable, SmartRecruiters and Recruitee all return the board
     OWNER'S NAME. When it does not match the company we asked about, the slug is rejected
     outright — that is exactly the wrong-board case, caught rather than imported.

For the providers that return no owner name (lever, ashby, personio, teamtailor) a slug is
accepted only when it came from the company's own domain AND the board carries at least one
posting. A name-derived slug is never accepted unverified; it is reported as `unverified` and
left for a human.

Crawling the careers page for an ATS link was tried first and abandoned: measured 2026-09-21 on
12 companies, zero hits. Those pages are marketing shells and the board link is rendered by
JavaScript — ClickHouse (Ashby) and GitLab (Greenhouse) both came back empty.

Input: JSON array of {name, url} (url = careers page, optional) or a CSV with those columns.
Output: a CSV in the shape scripts/import_company_list.py reads, carrying a REAL ATS URL.

Usage:
  uv run scripts/resolve_board_slugs.py --from list.json --out state/resolved.csv
  uv run scripts/resolve_board_slugs.py --from list.json --out out.csv --limit 40
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

UA = {"User-Agent": "Mozilla/5.0 (compatible; job-search-pipeline/1.0)"}

# Same eight boards collect_boards.py speaks, and the URL each slug lives at publicly.
PROVIDERS = {
    "greenhouse":      ("https://boards-api.greenhouse.io/v1/boards/{slug}", "https://boards.greenhouse.io/{slug}"),
    "ashby":           ("https://api.ashbyhq.com/posting-api/job-board/{slug}", "https://jobs.ashbyhq.com/{slug}"),
    "lever":           ("https://api.lever.co/v0/postings/{slug}?mode=json", "https://jobs.lever.co/{slug}"),
    "workable":        ("https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true", "https://apply.workable.com/{slug}/"),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=10", "https://jobs.smartrecruiters.com/{slug}"),
    "recruitee":       ("https://{slug}.recruitee.com/api/offers/", "https://{slug}.recruitee.com"),
    "personio":        ("https://{slug}.jobs.personio.de/search.json", "https://{slug}.jobs.personio.de"),
    "teamtailor":      ("https://{slug}.teamtailor.com/jobs.json", "https://{slug}.teamtailor.com"),
}

# Hosts that are never a company's own domain, so never a slug source.
NOT_OWN_DOMAIN = {"linkedin.com", "www.linkedin.com", "t.me", "telegram.org", "facebook.com",
                  "twitter.com", "x.com", "notion.so", "airtable.com", "docs.google.com",
                  # job platforms and accelerators: the host belongs to THEM, not to the company
                  "ycombinator.com", "www.ycombinator.com", "workatastartup.com",
                  "www.workatastartup.com", "angel.co", "wellfound.com", "hh.ru", "indeed.com",
                  "glassdoor.com", "notion.site", "getro.com", "jobs.gem.com"}


def fetch_json(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8", "replace"))


def norm(s: str) -> str:
    """Comparison form for company names: letters and digits only, lowercased."""
    s = re.sub(r"\(.*?\)", " ", s or "")
    s = re.sub(r"\b(inc|llc|ltd|limited|gmbh|bv|sa|ag|oy|ab|plc|corp|co|group|holding|"
               r"technologies|technology|tech|labs|software|games|studio|studios)\b", " ", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def candidates(name: str, url: str) -> list[tuple[str, str]]:
    """(slug, origin) candidates, best first. origin is 'domain' or 'name' and decides how much
    proof the slug needs before it is accepted."""
    out: list[tuple[str, str]] = []
    host = (urlparse(url).netloc or "").lower().strip()
    if host and host not in NOT_OWN_DOMAIN:
        # Singular forms matter: "career.softserveinc.com" left un-stripped yields the slug
        # "career", which is a real Lever board belonging to neither SoftServe nor Luxoft — both
        # landed on it on 2026-09-21 before the related-slug rule rejected them.
        core = re.sub(r"^(www|careers?|jobs?|apply|hr|work|talent|join|team|people)\.", "", host)
        core = core.split(".")[0]
        if len(core) > 2:
            out.append((core, "domain"))
            if "-" in core:
                out.append((core.replace("-", ""), "domain"))
    n = norm(name)
    if n and all(n != s for s, _ in out):
        out.append((n, "name"))
    plain = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if plain and all(plain != s for s, _ in out):
        out.append((plain, "name"))
    return out[:4]


def board_owner(provider: str, payload) -> str | None:
    """The company name the board itself reports, when the provider gives one."""
    try:
        if provider == "greenhouse":
            return payload.get("name")
        if provider == "workable":
            return payload.get("name")
        if provider == "smartrecruiters":
            c = (payload.get("content") or [{}])[0].get("company") or {}
            return c.get("name")
        if provider == "recruitee":
            o = (payload.get("offers") or [{}])[0]
            return o.get("company_name")
    except Exception:
        return None
    return None


def count_jobs(provider: str, payload) -> int:
    try:
        if provider in ("lever", "personio", "teamtailor"):
            return len(payload) if isinstance(payload, list) else 0
        if provider == "smartrecruiters":
            return len(payload.get("content") or [])
        if provider == "recruitee":
            return len(payload.get("offers") or [])
        if provider == "greenhouse":
            return 0          # the board endpoint carries no job list; only the owner name
        return len(payload.get("jobs") or [])
    except Exception:
        return 0


def probe(company: dict, delay: float) -> dict:
    name, url = company.get("name", ""), company.get("url", "")
    want = norm(name)
    best_unverified = None
    for slug, origin in candidates(name, url):
        for provider, (api, public) in PROVIDERS.items():
            try:
                payload = fetch_json(api.format(slug=slug))
            except Exception:
                continue
            finally:
                time.sleep(delay)
            owner = board_owner(provider, payload)
            jobs = count_jobs(provider, payload)
            if owner:
                got = norm(owner)
                if got and want and (got == want or got.startswith(want) or want.startswith(got)):
                    return {**company, "slug": slug, "provider": provider, "jobs": jobs,
                            "ats_url": public.format(slug=slug), "verdict": "verified",
                            "evidence": f"board owner {owner!r}"}
                continue                      # a name that does not match IS the wrong board
            # An unverifiable provider (lever/ashby/personio/teamtailor return no owner name)
            # is only believed when the slug ALSO resembles the company name. Without that,
            # 2026-09-21 produced "Farel -> ashby/ycombinator, 8 jobs": the list's careers URL
            # pointed at Y Combinator's board, the domain rule read "ycombinator" as Farel's own
            # domain, and the result was a different company's postings wearing Farel's name.
            related = bool(want) and (want in norm(slug) or norm(slug) in want)
            if jobs and origin == "domain" and related and best_unverified is None:
                best_unverified = {**company, "slug": slug, "provider": provider, "jobs": jobs,
                                   "ats_url": public.format(slug=slug), "verdict": "domain-match",
                                   "evidence": f"slug from own domain, {jobs} posting(s), "
                                               f"{provider} returns no owner name"}
    if best_unverified:
        return best_unverified
    return {**company, "slug": "", "provider": "", "jobs": 0, "ats_url": "",
            "verdict": "not found", "evidence": ""}


def load(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if text.lstrip().startswith("["):
        rows = json.loads(text)
        return [{"name": r.get("name") or r.get("Company") or "",
                 "url": r.get("url") or r.get("Job site") or "",
                 "regions": r.get("regions") or r.get("Country") or ""} for r in rows]
    rdr = csv.DictReader(text.splitlines())
    out = []
    for r in rdr:
        keys = {k.lower().strip(): k for k in r}
        out.append({"name": r.get(keys.get("company", keys.get("name", ""))) or "",
                    "url": r.get(keys.get("job site", keys.get("url", ""))) or "",
                    "regions": r.get(keys.get("country", keys.get("regions", ""))) or ""})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--out", required=True, help="CSV for scripts/import_company_list.py")
    ap.add_argument("--limit", type=int, help="probe only the first N (for a costing run)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--delay", type=float, default=0.15, help="pause after each request")
    args = ap.parse_args()

    rows = load(Path(args.src))
    if args.limit:
        rows = rows[:args.limit]
    print(f"probing {len(rows)} companies (up to 8 providers x 4 slug candidates each)",
          file=sys.stderr)

    results = []
    with cf.ThreadPoolExecutor(args.workers) as ex:
        for i, res in enumerate(ex.map(lambda c: probe(c, args.delay), rows), 1):
            results.append(res)
            if res["verdict"] != "not found":
                print(f"  [{i:3}/{len(rows)}] {res['name'][:26]:26} -> {res['provider']:15} "
                      f"{res['slug']:22} {res['verdict']} ({res['jobs']} jobs)", file=sys.stderr)

    ok = [r for r in results if r["verdict"] != "not found"]
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Company", "Job site", "Country", "Remote", "Verdict", "Evidence", "Jobs"])
        for r in ok:
            w.writerow([r["name"], r["ats_url"], r.get("regions", ""), "", r["verdict"],
                        r["evidence"], r["jobs"]])
    by = {}
    for r in results:
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    print(f"\n  {by}", file=sys.stderr)
    print(f"  {len(ok)} row(s) written to {out}", file=sys.stderr)
    print(f"  next: uv run scripts/import_company_list.py --from {out} --dry-run", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
