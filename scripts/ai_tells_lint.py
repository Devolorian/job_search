#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pypdf>=4"]
# ///
"""Flag the mechanical signs of generated text in CV / cover letter / LinkedIn copy.

A floor for the humanize-cv skill, not the judgement: it finds words and shapes, it cannot
tell a duty list from real work. Hard hits are worth fixing every time; soft hits are worth
a look.

Usage:
    uv run scripts/ai_tells_lint.py vacancies/<dir>/cover_letter.md
    uv run scripts/ai_tells_lint.py vacancies/<dir>/CV_<Surname>.pdf   # the rendered CV text (jobprofile.cv_filename)
    pdftotext -layout "Profile (3).pdf" - | uv run scripts/ai_tells_lint.py -
    uv run scripts/ai_tells_lint.py cv/master_cv.yaml --yaml   # only text:/summaries values
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

# "orchestrat" was a stem until 2026-10-19 and flagged every rendered CV on its skills label
# "Transformation & Orchestration" -- the noun is the technical term for what Airflow does; the
# verb ("orchestrated the rollout") is the tell.
HARD_WORDS = [
    "spearhead", "orchestrated", "orchestrating", "leverag", "utiliz", "robust", "seamless", "cutting-edge",
    "innovative", "dynamic", "passionate", "proven track record", "results-driven", "synerg",
    "holistic", "pivotal", "foster", "empower", "elevat", "delve", "tapestry", "testament",
    "landscape", "realm", "best-in-class", "world-class", "actionable", "data-driven",
    "i specialize in", "i have successfully", "highly skilled",
]
SOFT_WORDS = [
    "strategic", "valuable", "significant", "various", "diverse", "effectively", "efficient",
    "ensuring", "in order to", "key ", "crucial", "journey", "navigate", "ecosystem",
    "stakeholders", "collaborat", "common goals", "end-to-end",
]
CASING = {r"\bDBT\b": "dbt", r"\bClickhouse\b": "ClickHouse",
          r"\bPowerBI\b": "Power BI"}

# A bullet (or bold run) that opens with a short label and a colon. Plain sentences with a
# colon, and one-word labels like "Stack:", are not the tell.
INLINE_HEADER = re.compile(r"^\s*([-•*]\s+(\*\*)?|\*\*)[A-Z][\w&/-]*(\s[\w&/-]+){1,3}(\*\*)?:\s+\S")
NOT_JUST = re.compile(r"\bnot (just|only|merely)\b.{0,60}\bbut\b|\bit'?s not about\b", re.I)
ING_TAIL = re.compile(r",\s+(ensuring|driving|enabling|improving|providing|resulting in|making)\b", re.I)
VAGUE_PCT = re.compile(r"(efficien\w*|productivity|decision[- ]making|alignment|satisfaction)[^.\n]{0,60}\b\d+\s?%"
                       r"|\b\d+\s?%[^.\n]{0,60}(efficien\w*|productivity|decision[- ]making)", re.I)


def load_lines(path: str, yaml_mode: bool) -> list[tuple[int, str]]:
    if path.lower().endswith(".pdf"):
        # The rendered CV, as a reader (or an ATS) gets it. Added 2026-10-19 for the deslop
        # step: the bank text can read fine on its own and still stack into a generated-looking
        # page once a selection puts five same-shaped bullets in a row.
        from pypdf import PdfReader
        text = "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    else:
        text = sys.stdin.read() if path == "-" else Path(path).read_text()
    lines = list(enumerate(text.splitlines(), 1))
    if not yaml_mode:
        return lines
    # Only the prose a reader sees: bullet text and summary bodies, not keys or comments.
    keep, in_block = [], False
    for n, line in lines:
        s = line.strip()
        if s.startswith("#"):
            continue
        if re.match(r"\s*- text:", line):
            keep.append((n, s.split(":", 1)[1].strip().strip('"')))
            in_block = False
        elif re.match(r"\s{2}\w+: >\s*$", line):
            in_block = True
        elif in_block and s and line.startswith("    ") and ":" not in s[:20]:
            keep.append((n, s))
        else:
            in_block = False
    return keep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--yaml", action="store_true", help="lint only the prose values of master_cv.yaml")
    args = ap.parse_args()

    lines = load_lines(args.path, args.yaml)
    hard: list[str] = []
    soft: list[str] = []

    for n, line in lines:
        low = line.lower()
        for w in HARD_WORDS:
            if w in low:
                hard.append(f"{n}: vocabulary '{w}'")
        for w in SOFT_WORDS:
            if w in low:
                soft.append(f"{n}: vocabulary '{w.strip()}'")
        for pat, fix in CASING.items():
            if re.search(pat, line):
                hard.append(f"{n}: casing, write '{fix}'")
        if INLINE_HEADER.match(line) and not re.match(r"^\s*(Achievements|Summary|Tools|Skills)\b", line):
            hard.append(f"{n}: inline-header bullet '{line.strip()[:40]}'")
        if NOT_JUST.search(line):
            hard.append(f"{n}: 'not just X but Y'")
        if VAGUE_PCT.search(line):
            hard.append(f"{n}: percentage on something nobody measures")
        if ING_TAIL.search(line):
            soft.append(f"{n}: -ing tail")
        if line.count("—") > 1:
            soft.append(f"{n}: {line.count('—')} em dashes on one line")
        if "**" in line and not line.lstrip().startswith(("#", "**")):
            soft.append(f"{n}: bold mid-text")

    # Copy-paste across roles: the same sentence twice.
    norm = Counter(re.sub(r"\W+", " ", l.lower()).strip() for _, l in lines if len(l.split()) >= 6)
    for s, c in norm.items():
        if c > 1:
            hard.append(f"repeated {c}x: '{s[:60]}…'")

    # Uniform rhythm across bullets.
    bullets = [len(l.split()) for _, l in lines if l.lstrip().startswith(("-", "•")) and len(l.split()) > 3]
    if len(bullets) >= 6 and statistics.pstdev(bullets) < 3:
        soft.append(f"bullet lengths nearly uniform (sd {statistics.pstdev(bullets):.1f} words)")

    for label, items in (("HARD", hard), ("soft", soft)):
        for i in items:
            print(f"{label}  {i}")
    print(f"\n{len(hard)} hard, {len(soft)} soft")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
