#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Render an ATS-safe CV PDF from cv/master_cv.yaml.

Everything rendered comes from master_cv.yaml. This script selects and orders; it never
invents (CLAUDE.md rule 2). Tailoring for a specific vacancy is expressed as a small
`tailor.yaml` inside the vacancy folder, naming which bullets to use — the wording still
comes from the bank.

Usage:
    uv run scripts/render_cv.py --generic
    uv run scripts/render_cv.py --job vacancies/2026-09-08__acme__analytics-engineer
    uv run scripts/render_cv.py --generic --allow-todo      # draft while gaps remain
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
MASTER = jobprofile.master_cv_path()
TEMPLATE = jobprofile.template_path()
CHROME = jobprofile.chrome_path()

SECTION_ORDER = [
    "Professional Summary",
    "Technical Skills",
    "Work Experience",
    "Education",
    "Certifications",
    "Languages",
]

SKILL_GROUP_LABELS = {
    "languages": "Languages",
    "warehouses": "Data Warehouses & Storage",
    "transformation": "Transformation & Orchestration",
    "modeling": "Data Modeling",
    "quality": "Data Quality & Governance",
    "bi": "BI, Cloud & Practice",
    "devops": "Engineering Practice",
}


def esc(s: str) -> str:
    return html.escape(str(s).strip(), quote=False)


# --------------------------------------------------------------------------- TODO gate

def find_todos(node, path="") -> list[str]:
    """Every unresolved TODO/placeholder in the master file, as dotted paths."""
    found = []
    if isinstance(node, dict):
        if node.get("placeholder") is True:
            found.append(f"{path} (placeholder bullet)")
        for k, v in node.items():
            found += find_todos(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += find_todos(v, f"{path}[{i}]")
    elif isinstance(node, str) and node.strip().startswith("TODO"):
        found.append(f"{path}: {node.strip()[:70]}")
    return found


# --------------------------------------------------------------------------- selection

def bullet_id(role_id: str, idx: int) -> str:
    return f"{role_id}-{idx}"


def load_tailor(job_dir: Path | None, cv: dict) -> dict:
    """The selection to render.

    Per-vacancy: the vacancy folder's tailor.yaml. Otherwise master_cv.yaml's
    `default_selection`. There is no "render everything" mode — the CV is always at most
    two pages (CLAUDE.md rule 5), so a selection is always required.
    """
    if job_dir is not None:
        p = job_dir / "tailor.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        print(f"  ! no tailor.yaml in {job_dir}, falling back to default_selection",
              file=sys.stderr)
    return cv.get("default_selection") or {}


def select_bullets(role: dict, tailor: dict) -> list[dict]:
    """Bullets for one role, in the order tailor.yaml asked for.

    Three cases, and the difference between the first two has caught a render out:

      - the role is **absent** from `bullets:`  -> its whole bank is rendered. Historic
        behaviour, kept: older tailor.yaml files omit some roles and expect their bullets
        on the page, and re-rendering those folders must not
        silently produce a different CV.
      - the role is present with an **empty list** -> the role is dropped from the CV. Added
        2026-09-18, when the one-page rule needed a way to say "not this role" and there was
        none: omitting a role added its bullets rather than removing them, and three
        renders in a row went onto page 2 because of it. No file in the repo used an empty
        list, so nothing changes for anything already written.
      - the role is present with ids -> exactly those, in that order.
    """
    rid = role["id"]
    bullets_cfg = tailor.get("bullets") or {}
    live = [
        (bullet_id(rid, i), b)
        for i, b in enumerate(role.get("bullets") or [])
        # retired: a slot kept only so the ids after it keep their numbers (a bullet whose
        # text was lost when the bank was rebuilt); never rendered, never a gap.
        if not b.get("placeholder") and not b.get("retired")
    ]
    if rid in bullets_cfg and not bullets_cfg[rid]:
        return []
    chosen = bullets_cfg.get(rid)
    if not chosen:
        return [b for _, b in live]
    order = {bid: n for n, bid in enumerate(chosen)}
    picked = [(bid, b) for bid, b in live if bid in order]
    picked.sort(key=lambda t: order[t[0]])
    return [b for _, b in picked]


def evidenced_role_ids(cv: dict) -> set[str]:
    return {r["id"] for r in cv.get("experience", [])}


def select_skills(cv: dict, tailor: dict) -> list[tuple[str, list[str]]]:
    """Skills grouped for rendering. Rule 19: drop anything with no evidencing role."""
    valid_roles = evidenced_role_ids(cv)
    emphasis = [s.lower() for s in (tailor.get("emphasise_skills") or [])]
    # A bank may name its own groups: `skill_group_labels: {design_tools: "Design Tools"}`.
    labels = {**SKILL_GROUP_LABELS, **(cv.get("skill_group_labels") or {})}
    out: list[tuple[str, list[str]]] = []
    for group, items in (cv.get("skills") or {}).items():
        names = []
        for it in items or []:
            ev = set(it.get("evidenced_by") or [])
            if not ev & valid_roles:
                print(f"  ! dropped unevidenced skill: {it.get('name')}", file=sys.stderr)
                continue
            name = it["name"]
            # Rule 21: acronym + expansion on first use.
            if it.get("expansion"):
                name = f"{name} ({it['expansion']})"
            names.append(name)
        if not names:
            continue
        if emphasis:  # put job-description matches first, keep the rest
            names.sort(key=lambda n: 0 if n.split(" (")[0].lower() in emphasis else 1)
        out.append((labels.get(group, group.replace("_", " ").title()), names))
    return out


# --------------------------------------------------------------------------- rendering

def build_html(cv: dict, tailor: dict) -> str:
    c = cv["contact"]
    parts: list[str] = []

    headline = tailor.get("headline") or c.get("headline", "")
    parts.append(f'<p class="name">{esc(c["full_name"])}</p>')
    if headline:
        parts.append(f'<p class="headline">{esc(headline)}</p>')

    # Rule 23: each field plain text, LinkedIn spelled out, all in the body. Two lines, not
    # four: on a CV of at most two pages (CLAUDE.md rule 5) every header line costs a bullet.
    line1 = " | ".join(x for x in [c.get("phone"), c.get("email"), c.get("location"),
                                   c.get("work_authorization")] if x)
    parts.append(f'<p class="contact">{esc(line1)}</p>')
    line2 = " | ".join(x for x in [c.get("linkedin"), c.get("github"), c.get("open_to")] if x)
    if line2:
        parts.append(f'<p class="contact">{esc(line2)}</p>')

    variant = (tailor.get("summary_variant")
               or (cv.get("default_selection") or {}).get("summary_variant")
               or next(iter(cv.get("summaries") or {}), ""))
    summary = (cv.get("summaries") or {}).get(variant)
    if summary:
        parts.append("<h2>Professional Summary</h2>")
        parts.append(f'<p class="summary">{esc(" ".join(summary.split()))}</p>')

    skills = select_skills(cv, tailor)
    if skills:
        parts.append("<h2>Technical Skills</h2>")
        for label, names in skills:
            parts.append(
                f'<p class="skills-line"><span class="label">{esc(label)}:</span> '
                f'{esc(", ".join(names))}</p>'
            )

    parts.append("<h2>Work Experience</h2>")
    for role in cv.get("experience", []):
        bullets = select_bullets(role, tailor)
        if not bullets:
            continue
        parts.append('<div class="role">')
        parts.append(
            f'<p class="role-line">{esc(role["title"])} — {esc(role["company"])}</p>'
        )
        meta = f'{role["start"]} – {role["end"]} | {role.get("location", "")}'
        if role.get("context"):
            meta += f' | {role["context"]}'
        parts.append(f'<p class="role-meta">{esc(meta)}</p>')
        for b in bullets:
            # U+2022 lives inside the same paragraph as its text: marker and content stay
            # adjacent in the PDF text layer (rules 9 and 25).
            parts.append(f'<p class="bullet">• {esc(b["text"])}</p>')
        parts.append("</div>")

    edu = [e for e in (cv.get("education") or []) if not str(e.get("degree", "")).startswith("TODO")]
    if edu:
        parts.append('<div class="block"><h2>Education</h2>')
        for e in edu:
            parts.append(f'<p class="role-line">{esc(e["degree"])}</p>')
            tail = " | ".join(
                x for x in [e.get("institution"), e.get("location"), e.get("graduation")] if x
            )
            parts.append(f'<p class="role-meta">{esc(tail)}</p>')
        parts.append("</div>")

    certs = cv.get("certifications") or []
    if certs:
        parts.append("<h2>Certifications</h2>")
        for cert in certs:
            if isinstance(cert, dict):
                txt = " | ".join(str(v) for v in cert.values() if v)
            else:
                txt = str(cert)
            parts.append(f'<p class="bullet">• {esc(txt)}</p>')

    langs = [l for l in (cv.get("languages") or []) if not str(l.get("level", "")).startswith("TODO")]
    if langs:
        parts.append(
            '<div class="block"><h2>Languages</h2><p class="skills-line">'
            + esc(", ".join(f'{l["name"]} — {l["level"]}' for l in langs))
            + "</p></div>"
        )

    tpl = TEMPLATE.read_text(encoding="utf-8")
    return tpl.replace("{{NAME}}", esc(c["full_name"])).replace("{{CONTENT}}", "\n".join(parts))


def html_to_pdf(html_text: str, out_pdf: Path) -> None:
    """Render via Chrome headless --print-to-pdf.

    Chrome writes the PDF and then does not exit (reproduced on Chrome 14x, both
    --headless and --headless=old). So rather than waiting on the process, poll for the
    output file to appear and stop growing, then terminate the process ourselves.
    """
    if not Path(CHROME).exists():
        sys.exit(f"Chrome not found at {CHROME} — needed to produce the PDF text layer.")
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    if out_pdf.exists():
        out_pdf.unlink()

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "cv.html"
        src.write_text(html_text, encoding="utf-8")
        cmd = [
            CHROME,
            "--headless=old",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pdf-header-footer",     # rule 4: nothing in the header/footer region
            f"--user-data-dir={Path(td) / 'profile'}",
            f"--print-to-pdf={out_pdf}",
            src.as_uri(),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 60
            stable, last = 0, -1
            while time.monotonic() < deadline:
                time.sleep(0.4)
                size = out_pdf.stat().st_size if out_pdf.exists() else 0
                if size > 0 and size == last:
                    stable += 1
                    if stable >= 3:          # unchanged for ~1.2s -> finished writing
                        return
                else:
                    stable = 0
                last = size
                if proc.poll() is not None and size > 0:
                    return
            err = (proc.stderr.read() or b"").decode(errors="replace")[-2000:]
            sys.exit(f"Chrome did not produce a PDF within 60s.\n{err}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--generic", action="store_true", help="base CV, all live bullets")
    g.add_argument("--job", type=Path, help="vacancy folder containing tailor.yaml")
    ap.add_argument("-o", "--output", type=Path, help="output PDF path")
    ap.add_argument("--allow-todo", action="store_true",
                    help="render a draft while master_cv.yaml still has gaps")
    ap.add_argument("--keep-html", action="store_true", help="also write the intermediate HTML")
    args = ap.parse_args()

    cv = yaml.safe_load(MASTER.read_text(encoding="utf-8"))

    todos = find_todos(cv)
    if todos:
        print(f"master_cv.yaml has {len(todos)} unresolved gap(s):", file=sys.stderr)
        for t in todos:
            print(f"  - {t}", file=sys.stderr)
        if not args.allow_todo:
            print("\nRefusing to render. Fill them in, or pass --allow-todo for a draft.",
                  file=sys.stderr)
            return 2
        print("--allow-todo set: rendering a DRAFT with gaps omitted.\n", file=sys.stderr)

    tailor = load_tailor(args.job, cv)

    if args.output:
        out = args.output
    elif args.job:
        # Always the same file name (jobprofile.cv_filename: CV_<Surname>.pdf, or
        # contact.cv_filename). A CV named after the company and role announces that it was
        # generated for that one posting; the vacancy folder is what distinguishes one CV
        # from another, and it already does, by date, company and role.
        #
        # tailor.yaml's `output_name` is not read, and a folder that still carries one gets
        # told so rather than silently rendering under it.
        if tailor.get("output_name"):
            print(f"  note: tailor.yaml sets output_name — ignored, the CV name is fixed",
                  file=sys.stderr)
        out = args.job / jobprofile.cv_filename(cv)
    else:
        # The generic CV follows the same rule; the profile's cv/ already says which one it is.
        out = MASTER.parent / jobprofile.cv_filename(cv)

    page = build_html(cv, tailor)
    if args.keep_html:
        out.with_suffix(".html").write_text(page, encoding="utf-8")
    html_to_pdf(page, out)

    size_kb = out.stat().st_size / 1024
    print(f"Wrote {out} ({size_kb:.0f} KB)")
    print(f"Next: uv run scripts/ats_lint.py {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
