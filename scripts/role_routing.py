"""Which roles are searched on which source. Imported by every collector, triage.py and
wantapply_ingest.py; not a script of its own (no PEP-723 header, no dependencies of its own —
the caller already has pyyaml).

Two pools and a matrix between them:

  roles    — `role_families` in profile/search_profile.yaml
  sources  — the keys under `sources:` in config/sources.yaml
  routing  — profile/routing.yaml. By default every role is searched on every source. Two lists
             change that:
               only_on    a role is searched ONLY on the sources named (and, for
                          company_boards, optionally only on the named boards);
               blacklist  roles taken off the named sources.

Before this, the same idea lived in three places that did not know about each other:
`title_families` per source block in sources.yaml (LinkedIn, LinkedIn BI), a `vendor_roles`
block handled by collect_boards.py alone, and "Data Analyst only from the boards" written in
prose. They are all rows of the matrix now.

Usage in a collector — the only change it needs:

    profile = role_routing.apply(jobprofile.load_search_profile(), "builtin")

`apply` returns a copy of the profile whose `role_families` holds only the roles allowed on
that source; every gate downstream (title match, description_exclusions, scoring families)
then works unchanged. A role restricted to particular boards is never in that copy — on
company_boards it comes from `board_scoped()`, checked per posting against the board slug.
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import yaml

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
ROUTING = jobprofile.routing_path()
SOURCES = jobprofile.SHARED_SOURCES

_cache: dict | None = None


def load() -> dict:
    """config/routing.yaml, validated once per process. Unknown role or source names are
    printed, not fatal: a typo should show in the run log, not take every collector down."""
    global _cache
    if _cache is None:
        _cache = (yaml.safe_load(ROUTING.read_text(encoding="utf-8")) or {}) if ROUTING.exists() else {}
    return _cache


def _blacklisted(role: str, source: str) -> bool:
    return any(source in row.get("sources", [])
               and (row.get("roles") == "*" or role in (row.get("roles") or []))
               and role not in (row.get("except") or [])
               for row in load().get("blacklist") or [])


# Keys under `sources:` that are not vacancy sources: Gmail replies and the search pass that
# finds boards (its boards are polled as company_boards).
NOT_VACANCY_SOURCES = {"mail", "board_discovery"}


def _known_sources() -> set[str]:
    try:
        keys = set(jobprofile.load_sources().get("sources", {}))
    except Exception:
        return set()
    return keys - NOT_VACANCY_SOURCES


def _warn(msg: str) -> None:
    print(f"  ! routing: {msg}", file=sys.stderr)


_validated = False


def _validate(routing: dict, roles: set[str]) -> None:
    global _validated
    if _validated:
        return
    _validated = True
    sources = _known_sources()
    for role, rule in (routing.get("only_on") or {}).items():
        if role not in roles:
            _warn(f"only_on names unknown role {role!r}")
        for s in rule.get("sources", []):
            if sources and s not in sources:
                _warn(f"only_on.{role} names unknown source {s!r}")
    for i, row in enumerate(routing.get("blacklist") or []):
        for s in row.get("sources", []):
            if sources and s not in sources:
                _warn(f"blacklist[{i}] names unknown source {s!r}")
        named = row.get("roles")
        for r in ([] if named == "*" else named or []) + list(row.get("except") or []):
            if r not in roles:
                _warn(f"blacklist[{i}] names unknown role {r!r}")


def allowed(profile: dict, source: str, board: str | None = None) -> list[str]:
    """Role names searched on `source` (and on `board`, for company_boards)."""
    routing = load()
    roles = list((profile.get("role_families") or {}).keys())
    _validate(routing, set(roles))
    only_on = routing.get("only_on") or {}
    out = []
    for role in roles:
        rule = only_on.get(role)
        if rule is not None:
            if source not in rule.get("sources", []):
                continue
            boards = rule.get("boards")
            if boards is not None and (board is None or board.lower() not in
                                       {b.lower() for b in boards}):
                continue
        if _blacklisted(role, source):
            continue
        out.append(role)
    return out


def apply(profile: dict, source: str) -> dict:
    """A copy of `profile` with `role_families` cut to the roles routed to `source`.
    Board-scoped roles are left out here; see board_scoped()."""
    keep = set(allowed(profile, source))
    p = copy.deepcopy(profile)
    p["role_families"] = {k: v for k, v in (profile.get("role_families") or {}).items()
                          if k in keep}
    return p


def board_scoped(profile: dict, source: str = "company_boards") -> list[dict]:
    """Roles that only_on restricts to particular boards of `source`, as
    {name, titles, exclude_titles, drop_re, boards}. collect_boards.py checks each posting
    against these with its board slug."""
    routing = load()
    out = []
    for role, rule in (routing.get("only_on") or {}).items():
        if rule.get("boards") is None or source not in rule.get("sources", []):
            continue
        fam = (profile.get("role_families") or {}).get(role)
        if not fam:
            continue
        if _blacklisted(role, source):   # the blacklist still wins over a board scope
            continue
        drops = [x.lower() for x in fam.get("drop_locations", [])]
        out.append({
            "name": role,
            "titles": [t.lower() for t in fam.get("titles", [])],
            "exclude_titles": [t.lower() for t in fam.get("exclude_titles", [])],
            "drop_re": re.compile(r"\b(" + "|".join(map(re.escape, drops)) + r")\b") if drops else None,
            "boards": {b.lower() for b in rule["boards"]},
        })
    return out


def source_of_record(file_key: str, rec: dict) -> str:
    """The routing source a collected record came from. collected.jsonl carries both company
    boards and open feeds; every other file is one source."""
    if file_key == "boards":
        return "open_feeds" if str(rec.get("source", "")).startswith("feed:") else "company_boards"
    return file_key


if __name__ == "__main__":
    # `uv run --with pyyaml scripts/role_routing.py` prints the matrix.
    prof = jobprofile.load_search_profile()
    srcs = sorted(_known_sources())
    roles = list(prof.get("role_families") or {})
    w = max(len(r) for r in roles)
    print(" " * (w + 2) + "  ".join(s[:10].ljust(10) for s in srcs))
    for r in roles:
        cells = []
        for s in srcs:
            if r in allowed(prof, s):
                cells.append("yes")
            elif any(b["name"] == r for b in board_scoped(prof, s)):
                cells.append("boards*")
            else:
                cells.append("-")
        print(r.ljust(w + 2) + "  ".join(c.ljust(10) for c in cells))
    print("\nboards* = only on the boards listed in profile/routing.yaml only_on")
