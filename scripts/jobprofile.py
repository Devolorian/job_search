"""Where the candidate's files live, and how the shared config and the candidate's overlay
combine. Imported by every script that reads config or the CV bank; not a script of its own
except for the small CLI at the bottom (no PEP-723 header — the caller already has pyyaml).

The project is split in two:

  shared, in git   config/sources.yaml   source mechanics: endpoints, pacing, limits, markers
                   config/companies.yaml the ATS boards polled
                   rules/                ATS ruleset, example keyword bank
                   templates/cv.html     the ATS-safe CV template
  the candidate    profile/              gitignored; profile.example/ shows the layout
                     search_profile.yaml roles, geography, languages, scoring, tracks
                     routing.yaml        which role is searched on which source
                     sources.yaml        overlay on config/sources.yaml (keywords, lanes,
                                         Telegram folder, spreadsheet id ...)
                     keyword_bank.yaml   optional, replaces rules/keyword_bank.yaml
                     cv/                 master_cv.yaml, story_bank.yaml, form_answers.yaml,
                                         template.html (optional)

The profile directory is $JOB_SEARCH_PROFILE if set, else profile/, else profile.example/
(with a warning — so a fresh clone runs end to end on the example candidate).

The overlay is a deep merge: mappings merge key by key, anything else (lists included) is
replaced by the profile's value. A key set to null in the overlay removes it.
"""
from __future__ import annotations

import copy
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED_SOURCES = ROOT / "config" / "sources.yaml"
COMPANIES = ROOT / "config" / "companies.yaml"
DEFAULT_TEMPLATE = ROOT / "templates" / "cv.html"
DEFAULT_KEYWORD_BANK = ROOT / "rules" / "keyword_bank.yaml"


@lru_cache(maxsize=1)
def profile_dir() -> Path:
    env = os.environ.get("JOB_SEARCH_PROFILE")
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else ROOT / p
    if (ROOT / "profile").is_dir():
        return ROOT / "profile"
    print("  note: no profile/ directory — using profile.example/ (see README.md)",
          file=sys.stderr)
    return ROOT / "profile.example"


def path(*parts: str) -> Path:
    return profile_dir().joinpath(*parts)


def search_profile_path() -> Path:
    return path("search_profile.yaml")


def routing_path() -> Path:
    return path("routing.yaml")


def sources_overlay_path() -> Path:
    return path("sources.yaml")


def master_cv_path() -> Path:
    return path("cv", "master_cv.yaml")


def template_path() -> Path:
    own = path("cv", "template.html")
    return own if own.exists() else DEFAULT_TEMPLATE


def keyword_bank_path() -> Path:
    own = path("keyword_bank.yaml")
    return own if own.exists() else DEFAULT_KEYWORD_BANK


def _read(p: Path) -> dict:
    import yaml  # imported here so scripts without a pyyaml dependency can use the paths
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_search_profile() -> dict:
    return _read(search_profile_path())


def load_routing() -> dict:
    return _read(routing_path())


def load_sources() -> dict:
    """config/sources.yaml with profile/sources.yaml merged over it. The whole document:
    `sources`, `destinations`, `rejected_sources`."""
    return deep_merge(_read(SHARED_SOURCES), _read(sources_overlay_path()))


def load_master_cv() -> dict:
    return _read(master_cv_path())


def spreadsheet_id() -> str:
    return ((load_sources().get("destinations") or {}).get("google_sheets") or {}) \
        .get("spreadsheet_id") or ""


def cv_filename(cv: dict | None = None) -> str:
    """The rendered CV's file name. `contact.cv_filename` if set, else CV_<Surname>.pdf.

    Deliberately carries no company or role: a CV named after the posting tells the recruiter
    it was generated for them, and the vacancy folder already says which one it is."""
    cv = cv if cv is not None else load_master_cv()
    c = cv.get("contact") or {}
    if c.get("cv_filename"):
        return str(c["cv_filename"])
    surname = (str(c.get("full_name") or "CV").split() or ["CV"])[-1]
    surname = re.sub(r"[^A-Za-z0-9_-]+", "", surname) or "Candidate"
    return f"CV_{surname}.pdf"


def geo_home(profile: dict | None = None) -> dict:
    """The candidate's home geography, from search_profile.yaml `geo`:

      geo.home.country         display name ("Spain"), for log lines
      geo.home.city_label      geo label of an office in the home city ("barcelona")
      geo.home.country_label   geo label of an office elsewhere in the country ("spain")
      geo.home.remote_scopes   country / region names in an employer's list of where a remote
                               hire may sit that reach the candidate ("spain", "europe", ...)
      geo.onsite_or_hybrid.accept_locations  substrings naming the home city
      geo.onsite_or_hybrid.accept_country    whole words naming the country or its cities
                                             (read as accept_spain in older profiles)
    """
    profile = profile if profile is not None else load_search_profile()
    g = profile.get("geo") or {}
    h = g.get("home") or {}
    oh = g.get("onsite_or_hybrid") or {}
    return {
        "country_name": h.get("country") or "the home country",
        "city": h.get("city_label") or "city",
        "country": h.get("country_label") or "country",
        "remote_scopes": {str(s).lower() for s in
                          (h.get("remote_scopes") or ["worldwide", "anywhere", "global"])},
        "city_words": [str(w).lower() for w in (oh.get("accept_locations") or [])],
        "country_words": [str(w).lower() for w in
                          (oh.get("accept_country") or oh.get("accept_spain") or [])],
    }


def chrome_path() -> str:
    """Chrome or Chromium for headless --print-to-pdf: $CHROME_PATH, else the usual places."""
    import shutil
    env = os.environ.get("CHROME_PATH")
    if env:
        return env
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if Path(c).exists():
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


@lru_cache(maxsize=1)
def soft_posting_languages() -> tuple[str, ...]:
    """Languages (ISO codes) whose postings pass the collectors' English gate tagged with
    `language: <code>` for the model to judge, instead of being dropped: search_profile.yaml
    `posting_languages.soft`. Only "es" has a detector so far (is_spanish in the collectors)."""
    return tuple(str(x).lower() for x in
                 ((load_search_profile().get("posting_languages") or {}).get("soft") or []))


def timezone() -> str:
    return str(load_search_profile().get("timezone") or "UTC")


DEFAULT_TRACK = {"name": "main", "families": [], "tab": "applications",
                 "stats_tab": "daily_stats", "seen_file": "state/seen.json",
                 "log_suffix": "", "sources": []}


def tracks() -> list[dict]:
    """The search tracks, from search_profile.yaml `tracks`. Each track is processed by its
    own agent and writes its own tracker tab. The first track is the default: it owns every
    role family not claimed by another track, and it alone reads mail.

      name        main / bi / ...           (used in --track and file names)
      families    role families this track owns (empty on the default track = the rest)
      tab         tracker tab               stats_tab  its daily_stats tab
      seen_file   its dedup file            log_suffix logs/DATE<suffix>.md
      sources     extra source files only this track reads (e.g. linkedin_bi)

    With no `tracks` key there is one track, `main`, owning everything."""
    raw = load_search_profile().get("tracks") or []
    if not raw:
        return [dict(DEFAULT_TRACK)]
    out = []
    for i, t in enumerate(raw):
        name = t["name"]
        d = {**DEFAULT_TRACK, "tab": f"{name}_applications", "stats_tab": f"{name}_daily_stats",
             "seen_file": f"state/seen_{name}.json", "log_suffix": f".{name}"}
        if i == 0:
            d = dict(DEFAULT_TRACK)
        d.update(t)
        d["families"] = list(d.get("families") or [])
        d["sources"] = list(d.get("sources") or [])
        out.append(d)
    return out


def track(name: str) -> dict:
    for t in tracks():
        if t["name"] == name:
            return t
    sys.exit(f"unknown track {name!r}; tracks in {search_profile_path()}: "
             f"{[t['name'] for t in tracks()]}")


def track_for_family(family: str | None) -> str:
    """The track a role family belongs to: the first non-default track that lists it, else
    the default track."""
    ts = tracks()
    for t in ts[1:]:
        if family in t["families"]:
            return t["name"]
    return ts[0]["name"]


def _get(doc, dotted: str):
    for part in dotted.split("."):
        if isinstance(doc, dict):
            doc = doc.get(part)
        elif isinstance(doc, list) and part.isdigit():
            doc = doc[int(part)]
        else:
            return None
    return doc


if __name__ == "__main__":
    # For shell scripts:
    #   python scripts/jobprofile.py dir            the profile directory
    #   python scripts/jobprofile.py tracks         one track name per line
    #   python scripts/jobprofile.py track bi KEY   one field of a track
    #   python scripts/jobprofile.py get sources.linkedin.enabled   (merged sources.yaml)
    #   python scripts/jobprofile.py profile KEY    (search_profile.yaml)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dir"
    if cmd == "dir":
        print(profile_dir())
    elif cmd == "tracks":
        print("\n".join(t["name"] for t in tracks()))
    elif cmd == "track":
        v = track(sys.argv[2]).get(sys.argv[3])
        print(" ".join(v) if isinstance(v, list) else ("" if v is None else v))
    elif cmd in ("get", "profile"):
        import yaml
        doc = load_sources() if cmd == "get" else load_search_profile()
        v = _get(doc, sys.argv[2])
        if isinstance(v, bool):
            print("true" if v else "false")
        else:
            print(yaml.safe_dump(v).strip() if isinstance(v, (dict, list)) else ("" if v is None else v))
    else:
        sys.exit(f"unknown command {cmd!r}")
