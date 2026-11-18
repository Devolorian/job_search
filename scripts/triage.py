#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""The mechanical half of Stages 2-4a of prompts/daily_scan.md, so a run stops rewriting it.

Before it existed, `scripts/` held ~270 throwaway helpers
(`_tmp_seen_*`, `_tmp_triage_*`, `_tmp_mkfolder_*`, `_tmp_show_*`) and `state/` dozens of
`_new_records_*.json`: every run wrote the same three programs again — list what is new, record
the decisions in the seen file, make the folder — and each rewrite was a fresh chance to get the
seen-file shape or the track split wrong. This script is those three programs, once.

It decides nothing. The title, geography, language and score calls stay the model's (Stage 3);
this only reads, lists and writes what the model decided.

    triage.py new    --track <name>                  # list unseen records, save them numbered
    triage.py show   <saved.json> <idx> [<idx> ...]  # full record, description included
    triage.py seen   --track <name> --from <saved.json> --decisions <decisions.json>
    triage.py folder --from <saved.json> --idx N --score S --status New|Shortlist \
                     --family F --why "..." [--location ...] [--work-format ...] ...

Track rules, as in daily_scan.md: a record's family is resolved from its title against
`profile/search_profile.yaml` (strongest weight wins), and the family decides the track
(`jobprofile.track_for_family`; tracks come from `jobprofile.tracks()`). A non-default track
dedups against the default track's seen file and its own, and writes only its own; the default
track never sees another track's records. An untitled record (a Telegram message) is listed on both
tracks — the model reads the text and the track it belongs to handles it.

A collector whose exit code in `state/collect_run.json` is non-zero is not read: its file may be
the previous run's.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import yaml

import role_routing  # scripts/role_routing.py — which roles each source searches

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

# collector name in collect_run.json -> its output file
SOURCES = {
    "boards": "collected.jsonl",
    "linkedin": "linkedin.jsonl",
    "wellfound": "wellfound.jsonl",
    "builtin": "builtin.jsonl",
    "euremotejobs": "euremotejobs.jsonl",
    "himalayas": "himalayas.jsonl",
    "telegram": "telegram.jsonl",
    "wantapply": "wantapply.jsonl",  # written by /wantapply, never by daily_run.sh
}
TRACKS = jobprofile.tracks()
DEFAULT_TRACK = TRACKS[0]["name"]
# Sources a track reads alone (search_profile.yaml tracks[].sources, e.g. a second LinkedIn
# search with that track's keywords): source -> track.
TRACK_ONLY = {s: t["name"] for t in TRACKS for s in t["sources"]}
for _s in TRACK_ONLY:
    SOURCES.setdefault(_s, f"{_s}.jsonl")
SEEN = {t["name"]: ROOT / t["seen_file"] for t in TRACKS}


def load_profile() -> dict:
    return jobprofile.load_search_profile()


def load_families() -> dict[str, dict]:
    return load_profile().get("role_families") or {}


def family(title: str, fams: dict[str, dict]) -> str | None:
    t = (title or "").lower()
    hits = [(f.get("weight", 0), name) for name, f in fams.items()
            if any(s.lower() in t for s in f.get("titles", []))]
    return max(hits)[1] if hits else None


def seen_keys(track: str) -> set[str]:
    # Every track reads the default track's seen file, plus its own.
    files = [SEEN[DEFAULT_TRACK]] + ([SEEN[track]] if track != DEFAULT_TRACK else [])
    keys: set[str] = set()
    for f in files:
        if f.exists():
            s = json.loads(f.read_text())
            keys |= set(s.get("entries", s) if isinstance(s, dict) else s)
    return keys


def failed_collectors() -> set[str]:
    p = STATE / "collect_run.json"
    if not p.exists():
        return set()
    runs = json.loads(p.read_text()).get("collectors", {})
    return {name for name, r in runs.items() if r.get("rc") not in (0, None)}


def cmd_new(a) -> None:
    prof = load_profile()
    fams = prof.get("role_families") or {}
    routed: dict[str, dict] = {}   # routing source -> the roles config/routing.yaml gives it
    keys = seen_keys(a.track)
    failed = failed_collectors()
    # profile/excluded_companies.yaml (candidate, 2026-11-16): a company that rejected the
    # candidate, or one they listed by hand, gets no folder. Dropped here, before the model,
    # and not written to seen — taking a company off the list brings its postings back.
    excluded = jobprofile.excluded_companies()
    dropped_excl: dict[str, int] = {}
    by_key: dict[str, dict] = {}
    for name, fname in SOURCES.items():
        if name in TRACK_ONLY and TRACK_ONLY[name] != a.track:
            continue
        path = STATE / fname
        if name in failed:
            print(f"# {fname}: collector rc != 0 — not read, log it as a failed source")
            continue
        if not path.exists():
            print(f"# {fname}: missing")
            continue
        total = new = other = blocked = 0
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            total += 1
            if r.get("dedup_key") in keys:
                continue
            if r.get("company") and jobprofile.norm_company(r["company"]) in excluded:
                dropped_excl[r["company"]] = dropped_excl.get(r["company"], 0) + 1
                continue
            # Roles are resolved against what config/routing.yaml routes to this record's
            # source. A title that matches only roles routed elsewhere is blocked:
            # not listed, not marked seen — the source may be given that role later.
            src = role_routing.source_of_record(name, r)
            if src not in routed:
                routed[src] = role_routing.apply(prof, src)["role_families"]
            fam = family(r.get("title", ""), routed[src]) if r.get("title") else None
            fam = fam or r.get("family")   # collect_boards stamps board-scoped roles
            if r.get("title") and not fam and family(r["title"], fams):
                blocked += 1
                continue
            r["_family"] = fam
            if fam and jobprofile.track_for_family(fam) != a.track:
                other += 1
                continue
            r["_file"] = fname
            k = r.get("dedup_key") or f"{fname}:{total}"
            if k in by_key:
                # Same posting from two sources: keep the company board's link (canonical,
                # does not expire), remember the other.
                kept = by_key[k]
                if r.get("source", "").startswith("board:") and not kept.get("source", "").startswith("board:"):
                    r["_also"] = kept.get("_also", []) + [kept.get("url")]
                    by_key[k] = r
                else:
                    kept.setdefault("_also", []).append(r.get("url"))
                continue
            by_key[k] = r
            new += 1
        print(f"# {fname}: {total} records, {new} new for {a.track}, {other} other track"
              + (f", {blocked} routed off this source" if blocked else ""))

    if dropped_excl:
        print(f"# excluded companies (profile/excluded_companies.yaml), not listed: "
              + ", ".join(f"{c} x{n}" if n > 1 else c for c, n in sorted(dropped_excl.items())))
    recs = list(by_key.values())
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out = ROOT / a.save if a.save else STATE / f"_triage_{a.track}_{stamp}.json"
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=1))
    print(f"# saved {len(recs)} records -> {out.relative_to(ROOT)}")
    for i, r in enumerate(recs):
        desc = r.get("description") or ""
        print(f"#{i} [{r['_file'].split('.')[0]}] {r.get('company') or '?'} | {r.get('title') or '(untitled)'}"
              f" | {r.get('location') or '-'} | geo={r.get('geo', '-')} | fam={r['_family'] or '?'}"
              f" | src={r.get('source')} | desc={len(desc)} | posted={str(r.get('posted'))[:10]}"
              f" | {r.get('url')}" + (f" | also={len(r['_also'])}" if r.get("_also") else ""))


def load_saved(path: str) -> list[dict]:
    return json.loads(Path(path).read_text())


def cmd_show(a) -> None:
    recs = load_saved(a.saved)
    for i in a.idx:
        r = recs[i]
        print(f"===== #{i} {r.get('company')} | {r.get('title')}")
        for k, v in r.items():
            if k != "description":
                print(f"{k}: {v}")
        desc = r.get("description") or ""
        print("--- description" + (f" (first {a.chars} of {len(desc)})" if len(desc) > a.chars else ""))
        print(desc[: a.chars])


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))  # the seen files' own layout
    os.replace(tmp, path)


def cmd_seen(a) -> None:
    """decisions.json: [{"idx": 3, "reason": "DROP geography: ..."}, ...]. `idx` points into the
    saved file; a Telegram message that is not one vacancy can also be given by key, with
    company/title/url/source spelled out."""
    recs = load_saved(a.saved)
    decisions = json.loads(Path(a.decisions).read_text())
    path = SEEN[a.track]
    seen = json.loads(path.read_text()) if path.exists() else {"entries": {}}
    ent = seen.setdefault("entries", {})
    today = dt.date.today().isoformat()
    added = skipped = 0
    for d in decisions:
        if not d.get("reason"):
            sys.exit(f"decision without a reason: {d}")
        r = recs[d["idx"]] if "idx" in d else d
        key = d.get("key") or r.get("dedup_key")
        if not key:
            sys.exit(f"no dedup_key for {d}")
        if key in ent:
            skipped += 1
            continue
        ent[key] = {
            "first_seen": today,
            "source": d.get("source") or r.get("source", ""),
            "company": d.get("company", r.get("company", "")),
            "title": d.get("title", r.get("title", "")),
            "url": d.get("url") or r.get("url", ""),
            "reason": d["reason"],
        }
        added += 1
    write_json_atomic(path, seen)
    print(f"{path.relative_to(ROOT)}: {added} added, {skipped} already there, {len(ent)} total")


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:40].strip("-")


def source_line(src: str) -> str:
    if src.startswith("board:"):
        return f"Company board ({src.split(':', 1)[1]})"
    if src.startswith("linkedin"):
        return f"LinkedIn ({src})"
    return src


def cmd_folder(a) -> None:
    r = load_saved(a.saved)[a.idx]
    company = a.company or r.get("company") or ""
    title = a.title or r.get("title") or ""
    if not company or not title:
        sys.exit("company and title are required (pass --company/--title for an untitled record)")
    found = a.found or dt.date.today().isoformat()
    folder = ROOT / "vacancies" / f"{found}__{slug(company)}__{slug(title)}"
    if folder.exists():
        sys.exit(f"exists: {folder.relative_to(ROOT)}")
    link = a.link or r.get("apply_url") or r.get("url") or ""
    location = a.location or r.get("location") or "not stated"
    work_format = a.work_format or r.get("work_format") or "not stated"
    salary = a.salary or r.get("salary") or "not disclosed"

    folder.mkdir(parents=True)
    rec = {k: v for k, v in r.items() if not k.startswith("_")}
    rec.update({
        "title": title, "company": company, "location": location, "apply_url": link,
        "work_format": work_format, "salary": salary, "match_score": a.score,
        "status": a.status, "role_family": a.family, "found": found,
    })
    if r.get("_also"):
        rec["other_links"] = r["_also"]
    (folder / "source.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    (folder / "job.md").write_text(
        f"# {title} — {company}\n\n"
        f"- Source: {a.source_line or source_line(r.get('source', ''))}\n"
        f"- Link: {link}\n"
        f"- Found: {found}\n"
        f"- Location: {location}          - Work format: {work_format}\n"
        f"- Salary: {salary}\n"
        f"- Match score: {a.score}/100     - Role family: {a.family}\n\n"
        f"## Why this matches\n{a.why.strip()}\n\n"
        f"## Full description\n{(r.get('description') or '').strip()}\n"
    )
    print(folder.relative_to(ROOT))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="list records not in the seen file(s), save them numbered")
    n.add_argument("--track", choices=[t["name"] for t in TRACKS], required=True)
    n.add_argument("--save", help="output path (default state/_triage_<track>_<stamp>.json)")
    n.set_defaults(fn=cmd_new)

    s = sub.add_parser("show", help="print full records from a saved list")
    s.add_argument("saved")
    s.add_argument("idx", type=int, nargs="+")
    s.add_argument("--chars", type=int, default=6000)
    s.set_defaults(fn=cmd_show)

    d = sub.add_parser("seen", help="record decisions in this track's seen file")
    d.add_argument("--track", choices=[t["name"] for t in TRACKS], required=True)
    d.add_argument("--from", dest="saved", required=True)
    d.add_argument("--decisions", required=True)
    d.set_defaults(fn=cmd_seen)

    f = sub.add_parser("folder", help="make the vacancy folder: source.json + job.md")
    f.add_argument("--from", dest="saved", required=True)
    f.add_argument("--idx", type=int, required=True)
    f.add_argument("--score", type=int, required=True)
    f.add_argument("--status", choices=["New", "Shortlist"], required=True)
    f.add_argument("--family", required=True)
    f.add_argument("--why", required=True, help="the 'Why this matches' paragraph")
    for opt in ("company", "title", "location", "work-format", "salary", "link", "source-line", "found"):
        f.add_argument(f"--{opt}")
    f.set_defaults(fn=cmd_folder)

    a = p.parse_args()
    os.chdir(ROOT)
    a.fn(a)


if __name__ == "__main__":
    main()
