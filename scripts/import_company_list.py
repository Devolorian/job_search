# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Merge an exported company list into config/companies.yaml.

Written for the "400 companies from Hello New Job!" Airtable list
(https://newhr.org/post/400-companies-from-hellonewjob, 404 records, published 2024-01-17), but
it takes any CSV/TSV carrying a company name, a careers-page URL and a country column.

WHY AN IMPORTER AND NOT A HAND EDIT: `config/companies.yaml` holds ATS *slugs*, not company
names. The list's "Job site" column is the only place a slug can honestly come from — when it
points at one of the eight boards `collect_boards.py` speaks, the slug is right there in the URL
and nothing is guessed. Measured on the first 9 rows of the list, 1 in 9 did (Lightspeed ->
boards.greenhouse.io/lightspeedhq); the rest are own-domain careers pages (miro.com/careers,
careers.semrush.com) that no amount of slug-guessing resolves reliably.

So this script does two separate things and keeps them apart in the output:

  exact    — slug parsed out of an ATS URL. Zero guessing, goes straight in.
  guess    — no ATS URL, slug derived from the company name. Written commented out under a
             `# unresolved:` block, NOT enabled: a wrong guess silently polls a different
             company's board and every posting it returns is noise that looks real.

NETWORK: none. Provider resolution stays where it already lives — `collect_boards.py` probes the
eight providers per slug and caches the answer in `state/ats_boards.json`. Adding N slugs here
costs up to 8 probes each on the next run and nothing thereafter.

COUNTRY FILTER: keep a company only if it hires somewhere that can employ a candidate based in
the EU — any EU/EEA country, or a Country/Remote column that says
worldwide. A company hiring only in the USA, Armenia or Kazakhstan is dropped here rather than
generating postings that Stage 3 would drop one by one, twice a day, forever.

Usage:
    uv run scripts/import_company_list.py --from state/hellonewjob_400.csv --dry-run
    uv run scripts/import_company_list.py --from state/hellonewjob_400.csv
    uv run scripts/import_company_list.py --from state/list.tsv --no-country-filter
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

import jobprofile  # scripts/jobprofile.py

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = jobprofile.COMPANIES

# Slug position in each provider's public URL. Keys match PROVIDERS in collect_boards.py — if a
# provider is added there, add it here or its URLs go to the guess pile.
ATS_PATTERNS = [
    ("greenhouse",      r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"),
    ("greenhouse",      r"greenhouse\.io/embed/job_board\?for=([a-z0-9_-]+)"),
    ("ashby",           r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)"),
    ("lever",           r"jobs\.lever\.co/([a-z0-9_-]+)"),
    ("workable",        r"(?:apply|jobs)\.workable\.com/([a-z0-9_-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)"),
    ("recruitee",       r"([a-z0-9_-]+)\.recruitee\.com"),
    ("personio",        r"([a-z0-9_-]+)\.jobs\.personio\.(?:de|com)"),
    ("teamtailor",      r"([a-z0-9_-]+)\.teamtailor\.com"),
]

# EU/EEA plus the spellings this list actually uses. Switzerland and the UK are deliberately in:
# both employ remotely into the EU often enough that dropping the company outright would lose
# more than it saves, and Stage 3 still applies the real geography rule to each posting.
EU_OK = {
    "spain", "espana", "españa", "portugal", "france", "germany", "deutschland", "netherlands",
    "holland", "belgium", "luxembourg", "ireland", "italy", "austria", "poland", "czech",
    "czechia", "slovakia", "slovenia", "hungary", "romania", "bulgaria", "croatia", "greece",
    "cyprus", "malta", "estonia", "latvia", "lithuania", "finland", "sweden", "denmark",
    "norway", "iceland", "switzerland", "uk", "united kingdom", "england",
    "eu", "europe", "european union",
}
REMOTE_WORDS = re.compile(r"\b(remote|worldwide|anywhere|global)\b", re.I)


def norm_header(name: str) -> str:
    return re.sub(r"[^a-z]+", "", (name or "").lower())


def pick_column(headers: list[str], *wanted: str) -> str | None:
    """Match a column by normalised name, then by substring. The export's headers are not
    guaranteed to be the grid's headers, and 'Job site' has appeared as 'Job Site' and 'jobsite'."""
    norm = {norm_header(h): h for h in headers}
    for w in wanted:
        if norm_header(w) in norm:
            return norm[norm_header(w)]
    for w in wanted:
        for h in headers:
            if norm_header(w) in norm_header(h):
                return h
    return None


def slug_from_url(url: str) -> tuple[str, str] | None:
    for provider, pattern in ATS_PATTERNS:
        m = re.search(pattern, url or "", re.I)
        if m:
            slug = m.group(1).lower().strip("-_.")
            if slug and slug not in {"www", "jobs", "careers", "embed"}:
                return provider, slug
    return None


def guess_slug(company: str) -> str:
    s = re.sub(r"\(.*?\)", " ", company or "")
    s = re.sub(r"\b(inc|llc|ltd|limited|gmbh|bv|sa|ag|oy|ab|plc|group|holding|technologies|tech)\b",
               " ", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def country_ok(country: str, remote: str) -> bool:
    """Keep a company unless it names only countries that cannot employ an EU-based candidate.

    An EMPTY Country column keeps the company. Same principle as the geography rule in
    the profile's match criteria: silence is not evidence of a narrow scope, and the place
    to decide a posting's geography is Stage 3, against the posting, not here against a blank
    cell. Three rows in the 2026-09-21 import are in this state.

    The `Remote` column is a CHECKBOX — its value is the literal "checked", not prose. It is
    deliberately NOT enough on its own to override a stated country: GoPuff is "USA" with
    Remote checked, which means remote inside the USA. Only remote wording that actually says
    worldwide counts.
    """
    if not (country or "").strip():
        return True
    # "Remote" / "Global" can arrive as a REGION token rather than in the Remote column — that is
    # how idagent.pro labels hiring ("Global, Remote", "US, Remote", or bare "Remote"). Reading
    # only the Remote column dropped Miro, GitLab, PandaDoc, Percona and Prisma Labs on
    # 2026-09-21, which is the opposite of what the list was imported for. A US-only company
    # with no remote token still goes.
    if REMOTE_WORDS.search(f"{country} {remote}"):
        return True
    parts = re.split(r"[;,/|]", country.lower())
    if any(p.strip() in EU_OK for p in parts):
        return True
    return "europe" in f"{country} {remote}".lower()


def read_rows(path: Path) -> tuple[list[dict], list[str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample.split("\n")[0] else csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return list(reader), list(reader.fieldnames or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True, help="exported CSV/TSV")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--no-country-filter", action="store_true")
    ap.add_argument("--label", default="Hello New Job! 400 (newhr.org, list dated 2024-01-17)")
    ap.add_argument("--companies", type=Path, default=COMPANIES,
                    help="target companies.yaml; overridable so the write path can be tested "
                         "against a copy instead of the live config")
    args = ap.parse_args()

    rows, headers = read_rows(Path(args.src))
    if not rows:
        print("!! no rows parsed — is the export a CSV with a header row?", file=sys.stderr)
        return 2

    col_name = pick_column(headers, "company", "company list", "name")
    col_site = pick_column(headers, "job site", "jobsite", "careers", "career page", "website")
    col_country = pick_column(headers, "country", "countries", "location")
    col_remote = pick_column(headers, "remote")
    if not col_name:
        print(f"!! no company column found. Headers: {headers}", file=sys.stderr)
        return 2
    print(f"  columns: name={col_name!r} site={col_site!r} country={col_country!r} "
          f"remote={col_remote!r}", file=sys.stderr)

    target = args.companies
    existing = yaml.safe_load(target.read_text(encoding="utf-8"))["companies"]
    have = {str(c.get("slug", "")).lower() for c in existing}

    exact, guesses, dropped_geo, dup, no_name = [], [], [], [], 0
    for r in rows:
        name = (r.get(col_name) or "").strip()
        if not name:
            no_name += 1
            continue
        country = (r.get(col_country) or "").strip() if col_country else ""
        remote = (r.get(col_remote) or "").strip() if col_remote else ""
        if not args.no_country_filter and not country_ok(country, remote):
            dropped_geo.append(f"{name} — {country[:60] or 'no country'}")
            continue
        site = (r.get(col_site) or "").strip() if col_site else ""
        hit = slug_from_url(site)
        if hit:
            provider, slug = hit
            if slug in have:
                dup.append(f"{name} ({slug})")
                continue
            have.add(slug)
            exact.append({"slug": slug, "provider": provider, "name": name,
                          "country": country[:70], "site": site})
        else:
            g = guess_slug(name)
            if g and g in have:
                dup.append(f"{name} ({g}, guessed)")
                continue
            host = urlparse(site).netloc if site else ""
            guesses.append({"slug": g, "name": name, "country": country[:70], "host": host})

    print(f"\n  {len(rows)} rows read", file=sys.stderr)
    print(f"  {len(exact)} with an ATS slug parsed from the URL  <- added", file=sys.stderr)
    print(f"  {len(guesses)} without one                          <- written commented out",
          file=sys.stderr)
    print(f"  {len(dup)} already in companies.yaml", file=sys.stderr)
    print(f"  {len(dropped_geo)} dropped by the country filter", file=sys.stderr)
    if no_name:
        print(f"  {no_name} rows with no company name", file=sys.stderr)
    for e in exact[:15]:
        print(f"      + {e['slug']:24} {e['provider']:16} {e['name'][:34]}", file=sys.stderr)
    if len(exact) > 15:
        print(f"      … and {len(exact) - 15} more", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run: companies.yaml untouched.", file=sys.stderr)
        return 0
    if not exact and not guesses:
        print("\nNothing to add.", file=sys.stderr)
        return 0

    lines = [f"\n  # --- {args.label} " + "-" * max(0, 46 - len(args.label)),
             f"  # Imported {len(exact)} slug(s) parsed straight out of an ATS URL. Country filter: "
             f"{'off' if args.no_country_filter else 'EU/EEA or remote'}."]
    for e in sorted(exact, key=lambda x: x["slug"]):
        note = f"{e['name']} — {e['country']}" if e["country"] else e["name"]
        lines.append(f"  - {{slug: {e['slug']}, provider: {e['provider']}, "
                     f"note: \"{note[:88].replace(chr(34), chr(39))}\"}}")
    if guesses:
        lines += ["",
                  "  # unresolved: no ATS URL in the source, so the slug below is DERIVED FROM THE",
                  "  # NAME and unverified. Left commented out on purpose — a wrong slug polls a",
                  "  # different company's board and its postings look entirely real. To adopt one,",
                  "  # open its careers page, and if the URL carries a supported board, uncomment it",
                  "  # with the real slug."]
        for g in sorted(guesses, key=lambda x: x["slug"])[:500]:
            lines.append(f"  # - {{slug: {g['slug']}, note: \"{g['name'][:50]} | {g['host'][:40]}\"}}")

    text = target.read_text(encoding="utf-8")
    marker = "\n# Slugs that resolved to no board on the last run"
    if marker in text:
        head, tail = text.split(marker, 1)
        text = head.rstrip() + "\n" + "\n".join(lines) + "\n" + marker + tail
    else:
        text = text.rstrip() + "\n" + "\n".join(lines) + "\n"
    target.write_text(text, encoding="utf-8")
    print(f"\nWrote {len(exact)} slug(s) into {target}; "
          f"{len(guesses)} left commented out.", file=sys.stderr)
    print("Next: uv run scripts/collect_boards.py --feeds --json state/collected.jsonl "
          "(it resolves and caches the new slugs)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
