#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "pypdf", "pdfminer.six", "pdfplumber", "pymupdf", "pypdfium2"]
# ///
"""Enforce rules/ats_rules.md against a produced CV PDF.

The point of linting the *output* rather than the input: an ATS never sees the HTML or the
YAML, it sees whatever falls out of the PDF text layer. So the text is re-extracted through
PDFKit — the same CoreGraphics path a macOS-side parser takes — and checked from there.
Structural facts (fonts, sizes, images) come from pypdf.

Since 2026-11-16 (candidate: "a double, triple ATS check") the text checks also run on two
more independent extractions, because a real ATS never uses PDFKit: pdfminer.six (the layout
engine most open-source resume parsers sit on) and pypdf (a content-stream reader, the way
lightweight parsers read), plus poppler's pdftotext when it is installed. A CV passes only if
every parser reads it the same way: rule 26 compares the words each one extracts, rule 27
asks each one to find every job with its title and dates, and any text check that fails on any
parser fails the CV. Three more engines, the ones current parsers are built on: PyMuPDF
(MuPDF), pypdfium2 (PDFium, Chrome's PDF engine) and pdfplumber (pdfminer with its own line
grouping). Ready-made "ATS simulator" packages were looked at and left out: unmaintained or of
unverifiable origin, and some send the CV to an LLM provider. Apache Tika needs a Java runtime,
which this machine does not have and the project does not install globally.

Usage:
    uv run scripts/ats_lint.py profile/cv/CV_<Surname>.pdf   # name from jobprofile.cv_filename
    uv run scripts/ats_lint.py <vacancy>/CV_*.pdf --jd <vacancy>/job.md
    uv run scripts/ats_lint.py old.pdf --no-master     # audit a CV not built from master_cv
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml
from pypdf import PdfReader

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
MASTER = jobprofile.master_cv_path()

SAFE_FONTS = {"arial", "calibri", "georgia", "timesnewroman", "times", "cambria",
              "verdana", "tahoma", "helvetica", "liberationsans", "arialmt"}

REQUIRED_HEADINGS = ["PROFESSIONAL SUMMARY", "TECHNICAL SKILLS", "WORK EXPERIENCE"]
OPTIONAL_HEADINGS = ["EDUCATION", "CERTIFICATIONS", "LANGUAGES"]

ACTION_VERBS = {
    "led", "built", "designed", "implemented", "migrated", "automated", "reduced",
    "improved", "created", "established", "defined", "standardised", "standardized",
    "delivered", "developed", "optimised", "optimized", "rebuilt", "launched", "owned",
    "drove", "scaled", "stabilised", "stabilized", "reworked", "introduced", "maintained",
    "cut", "grew", "hired", "trained", "mentored", "contributed", "architected",
    "consolidated", "refactored", "modelled", "modeled", "integrated", "orchestrated",
}

# Any bullet-like glyph that is NOT U+2022 (rule 9).
BAD_BULLETS = "●▪■‣⁃·∙◦–✒"

DATE_RE = re.compile(r"\b[A-Z][a-z]{2} \d{4} – (?:Present|[A-Z][a-z]{2} \d{4})\b")
BAD_DATE_RE = re.compile(r"\b\d{1,2}/\d{4}\b|\b\d{4}\s*-\s*\d{4}\b|\b(?:Current|Now|Ongoing)\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w-]+", re.I)
NUMBER_RE = re.compile(r"\d|\bhalf\b|\bdouble\b|\btripled\b")


# Content-stream operators that move the graphics/text state. A Tf operand is in text-space
# units, not points: the rendered size is Tf x the text matrix scale x the current
# transformation matrix scale. Chrome nests a 0.24 cm inside a 3.125 cm (net 0.75, i.e.
# 96dpi CSS px -> 72dpi points), so a raw "14 Tf" is really 10.5pt. Reading Tf alone would
# report every Chrome-built CV as oversized, and mis-measure Word-built ones the other way.
_TOKEN_RE = re.compile(
    r"(?P<q>\bq\b)"
    r"|(?P<Q>\bQ\b)"
    r"|(?P<cm>[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+(?P<cm_d>[-\d.]+)\s+[-\d.]+\s+[-\d.]+\s+cm\b)"
    r"|(?P<Tm>[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+(?P<Tm_d>[-\d.]+)\s+[-\d.]+\s+[-\d.]+\s+Tm\b)"
    r"|(?P<Tf>/[A-Za-z0-9]+\s+(?P<Tf_size>[\d.]+)\s+Tf\b)"
    r"|(?P<show>\b(?:Tj|TJ|'|\")(?![A-Za-z]))"
)


def effective_font_sizes(content: str) -> set[float]:
    """Rendered point sizes of every text run actually drawn in this content stream."""
    sizes: set[float] = set()
    ctm, stack = 1.0, []
    tm, tf = 1.0, None
    for m in _TOKEN_RE.finditer(content):
        if m.group("q"):
            stack.append(ctm)
        elif m.group("Q"):
            ctm = stack.pop() if stack else 1.0
        elif m.group("cm"):
            try:
                ctm *= abs(float(m.group("cm_d"))) or 1.0
            except ValueError:
                pass
        elif m.group("Tm"):
            try:
                tm = abs(float(m.group("Tm_d"))) or 1.0
            except ValueError:
                tm = 1.0
        elif m.group("Tf"):
            tf = float(m.group("Tf_size"))
        elif m.group("show") and tf is not None:
            sizes.add(round(tf * tm * ctm, 2))
    return sizes


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []   # status, rule, name, detail

    def add(self, status: str, rule: str, name: str, detail: str = "") -> None:
        self.rows.append((status, rule, name, detail))

    def ok(self, rule, name, detail=""):   self.add("PASS", rule, name, detail)
    def fail(self, rule, name, detail=""): self.add("FAIL", rule, name, detail)
    def warn(self, rule, name, detail=""): self.add("WARN", rule, name, detail)

    @property
    def failures(self) -> int:
        return sum(1 for r in self.rows if r[0] == "FAIL")

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.rows if r[0] == "WARN")

    def render(self) -> str:
        icon = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN"}
        w = max(len(r[2]) for r in self.rows)
        out = []
        for status, rule, name, detail in self.rows:
            line = f"  [{icon[status]}] r{rule:<2} {name:<{w}}"
            if detail:
                line += f"  {detail}"
            out.append(line)
        return "\n".join(out)


# --------------------------------------------------------------------- extraction

def extract_text_pdfkit(pdf: Path) -> tuple[list[str], int]:
    """Text via macOS PDFKit — the same path a CoreGraphics-based parser takes."""
    js = f'''
    ObjC.import("Quartz");
    var url = $.NSURL.fileURLWithPath({json.dumps(str(pdf.resolve()))});
    var doc = $.PDFDocument.alloc.initWithURL(url);
    if (doc.isNil()) {{ JSON.stringify({{error: "unreadable"}}); }} else {{
      var pages = [];
      for (var i = 0; i < doc.pageCount; i++) {{
        pages.push(ObjC.unwrap(doc.pageAtIndex(i).string));
      }}
      JSON.stringify({{pages: pages}});
    }}
    '''
    res = subprocess.run(["osascript", "-l", "JavaScript", "-e", js],
                         capture_output=True, text=True, timeout=90)
    if res.returncode != 0:
        sys.exit(f"PDFKit extraction failed:\n{res.stderr}")
    data = json.loads(res.stdout.strip())
    if "error" in data:
        sys.exit(f"Could not open {pdf}")
    return data["pages"], len(data["pages"])


def extract_text_pdfminer(pdf: Path, n_pages: int) -> list[str]:
    """pdfminer.six with default layout analysis, one page at a time."""
    from pdfminer.high_level import extract_text
    return [extract_text(str(pdf), page_numbers=[i]) for i in range(n_pages)]


def extract_text_pypdf(reader: PdfReader) -> list[str]:
    return [p.extract_text() or "" for p in reader.pages]


def extract_text_pymupdf(pdf: Path) -> list[str]:
    import pymupdf
    with pymupdf.open(str(pdf)) as doc:
        return [page.get_text() for page in doc]


def extract_text_pdfium(pdf: Path) -> list[str]:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf))
    try:
        return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    finally:
        doc.close()


def extract_text_pdfplumber(pdf: Path) -> list[str]:
    import pdfplumber
    with pdfplumber.open(str(pdf)) as doc:
        return [page.extract_text() or "" for page in doc.pages]


def extract_text_pdftotext(pdf: Path) -> list[str] | None:
    """poppler's pdftotext in reading order, if installed; pages split on form feed."""
    import shutil
    if not shutil.which("pdftotext"):
        return None
    res = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"],
                         capture_output=True, text=True, timeout=60)
    return res.stdout.rstrip("\f").split("\f") if res.returncode == 0 else None


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------- checks

def check_structure(reader: PdfReader, rep: Report) -> None:
    """Fonts, sizes, images, annotations — rules 2, 5, 7, 8, 13."""
    fonts, sizes, images = set(), set(), 0
    for page in reader.pages:
        res = page.get("/Resources") or {}
        for name, font in (res.get("/Font") or {}).items():
            try:
                base = str(font.get_object().get("/BaseFont", ""))
            except Exception:
                continue
            fonts.add(re.sub(r"^/|^[A-Z]{6}\+", "", base))
        for _, xo in (res.get("/XObject") or {}).items():
            try:
                if xo.get_object().get("/Subtype") == "/Image":
                    images += 1
            except Exception:
                pass
        try:
            content = page.get_contents().get_data().decode("latin-1", "replace")
        except Exception:
            content = ""
        sizes |= effective_font_sizes(content)

    bad_fonts = [f for f in fonts
                 if not any(s in f.lower().replace("-", "").replace(" ", "") for s in SAFE_FONTS)]
    if bad_fonts:
        rep.fail("7", "system-safe fonts", f"non-whitelisted: {', '.join(sorted(bad_fonts))}")
    else:
        rep.ok("7", "system-safe fonts", ", ".join(sorted(fonts)) or "none embedded")

    body = [s for s in sizes if s > 0]
    if not body:
        rep.warn("8", "font size >= 10pt", "no Tf operators found; size not verifiable")
    elif min(body) < 10:
        rep.fail("8", "font size >= 10pt", f"smallest is {min(body)}pt")
    else:
        rep.ok("8", "font size >= 10pt", f"range {min(body)}–{max(body)}pt")

    if images:
        rep.fail("5", "no images", f"{images} image XObject(s) embedded")
    else:
        rep.ok("5", "no images")


def check_hidden_text(reader: PdfReader, rep: Report) -> None:
    """Rule 22. White or near-white fill applied to a text run, or a text render mode
    of 3 (invisible). Both are the mechanics behind white-text keyword stuffing and
    hidden prompt injection."""
    hits = []
    for n, page in enumerate(reader.pages, 1):
        try:
            content = page.get_contents().get_data().decode("latin-1", "replace")
        except Exception:
            continue
        for m in re.finditer(r"BT(.*?)ET", content, re.S):
            block = m.group(1)
            if re.search(r"\b3\s+Tr\b", block):
                hits.append(f"p{n}: invisible text render mode (3 Tr)")
            for cm in re.finditer(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg\b", block):
                r, g, b = (float(x) for x in cm.groups())
                if r > 0.9 and g > 0.9 and b > 0.9:
                    hits.append(f"p{n}: near-white text fill rgb({r},{g},{b})")
            for gm in re.finditer(r"(?<![\d.])([\d.]+)\s+g\b", block):
                if float(gm.group(1)) > 0.9:
                    hits.append(f"p{n}: near-white text fill gray {gm.group(1)}")
    if hits:
        rep.fail("22", "no hidden text", "; ".join(sorted(set(hits))[:4]))
    else:
        rep.ok("22", "no hidden text")


def check_text(pages: list[str], rep: Report, master: dict | None) -> None:
    full = "\n".join(pages)
    lines = [l.rstrip() for l in full.splitlines()]
    nonempty = [l for l in lines if l.strip()]

    # -- rule 13: a real text layer
    if len(full.strip()) < 400:
        rep.fail("13", "text layer present", f"only {len(full.strip())} chars extracted")
        return
    rep.ok("13", "text layer present", f"{len(full.strip())} chars")

    # -- rule 14: standard headings, in order
    upper = full.upper()
    missing = [h for h in REQUIRED_HEADINGS if h not in upper]
    if missing:
        rep.fail("14", "standard headings", f"missing: {', '.join(missing)}")
    else:
        positions = [upper.index(h) for h in REQUIRED_HEADINGS]
        if positions != sorted(positions):
            rep.fail("14", "standard headings", "present but out of order")
        else:
            present = [h for h in REQUIRED_HEADINGS + OPTIONAL_HEADINGS if h in upper]
            rep.ok("14", "standard headings", f"{len(present)} found, in order")

    # -- rule 23: contact fields extractable
    got = {"email": bool(EMAIL_RE.search(full)),
           "phone": bool(PHONE_RE.search(full)),
           "linkedin": bool(LINKEDIN_RE.search(full))}
    head = "\n".join(nonempty[:8])
    if not all(got.values()):
        rep.fail("23", "contact extractable",
                 "missing: " + ", ".join(k for k, v in got.items() if not v))
    elif not EMAIL_RE.search(head):
        rep.warn("23", "contact extractable", "email is not in the first 8 lines")
    else:
        rep.ok("23", "contact extractable", EMAIL_RE.search(full).group(0))

    # -- rule 9: bullet glyph
    bad = {c for c in BAD_BULLETS if re.search(rf"^\s*{re.escape(c)}", full, re.M)}
    if bad:
        rep.fail("9", "bullet char U+2022",
                 "line-initial non-standard glyph(s): " + " ".join(f"U+{ord(c):04X}" for c in bad))
    else:
        rep.ok("9", "bullet char U+2022", f"{full.count('•')} bullets")

    # -- rule 25: reading order. Orphan markers mean the marker was emitted as its own
    #    text run, detached from its content. This is the defect in the pre-existing CV.
    orphans = [i for i, l in enumerate(lines) if l.strip() in {"•", "●", "-", "*"}]
    if orphans:
        rep.fail("25", "reading order", f"{len(orphans)} orphaned bullet marker(s) on their own line")
    else:
        detached = sum(1 for l in nonempty if re.fullmatch(r"[•●]\s*", l))
        if detached:
            rep.fail("25", "reading order", f"{detached} marker(s) with no adjacent text")
        else:
            rep.ok("25", "reading order", "every marker is contiguous with its text")

    # -- rule 16: one date format, 'Present' not 'Current'
    good = DATE_RE.findall(full)
    bad_dates = [d for d in BAD_DATE_RE.findall(full) if d]
    if bad_dates:
        rep.fail("16", "date format", f"non-conforming: {', '.join(sorted(set(bad_dates))[:4])}")
    elif not good:
        rep.fail("16", "date format", "no 'Mon YYYY – Mon YYYY' ranges found")
    else:
        rep.ok("16", "date format", f"{len(good)} conforming range(s)")

    # -- rule 10: ligature glyphs survived the export
    lig_words = re.findall(r"\b\w*(?:fi|fl|ff|ffi)\w*\b", full)
    suspects = re.findall(r"\b(?:prole|workow|beneted|conguration|eciency|traf|oce)\b", full, re.I)
    if suspects:
        rep.fail("10", "ligatures intact", f"dropped-glyph artefacts: {', '.join(set(suspects))}")
    else:
        rep.ok("10", "ligatures intact", f"{len(lig_words)} fi/fl/ff word(s) whole")

    # -- rules 17 & 18: bullet quality
    bullets = [l.strip().lstrip("•").strip() for l in nonempty if l.strip().startswith("•")]
    # Wrapped continuation lines are separate lines; only the marker line starts a bullet.
    if bullets:
        bad_verb = [b for b in bullets
                    if b.split()[0].lower().rstrip(",.").strip() not in ACTION_VERBS]
        if bad_verb:
            rep.warn("17", "action-verb openers",
                     f"{len(bad_verb)}/{len(bullets)}: " + " | ".join(b[:38] for b in bad_verb[:3]))
        else:
            rep.ok("17", "action-verb openers", f"all {len(bullets)}")

        # A bullet's numbers may sit on its wrapped continuation line, so measure per
        # bullet block rather than per marker line.
        blocks, cur = [], None
        for l in nonempty:
            if l.strip().startswith("•"):
                if cur is not None:
                    blocks.append(cur)
                cur = l
            elif cur is not None and not l.isupper() and len(l) > 2:
                cur += " " + l
        if cur is not None:
            blocks.append(cur)
        quantified = sum(1 for b in blocks if NUMBER_RE.search(b))
        pct = 100 * quantified / len(blocks) if blocks else 0
        if pct < 70:
            rep.warn("18", "quantified bullets", f"{quantified}/{len(blocks)} = {pct:.0f}% (target 70%)")
        else:
            rep.ok("18", "quantified bullets", f"{quantified}/{len(blocks)} = {pct:.0f}%")
    else:
        rep.fail("17", "action-verb openers", "no bullets found at all")

    # -- rule 22 (content side): nothing extracted that was never authored
    if master:
        authored = norm(" ".join(
            [b.get("text", "") for r in master.get("experience", []) for b in (r.get("bullets") or [])]
            + [s for s in (master.get("summaries") or {}).values()]
        )).lower()
        for b in bullets:
            key = norm(b).lower()[:60]
            if key and key not in authored and not key.startswith("todo"):
                rep.warn("2", "no fabricated content",
                         f"bullet not found verbatim in master_cv.yaml: {b[:50]}…")
                break
        else:
            rep.ok("2", "no fabricated content", "every bullet traces to master_cv.yaml")


def check_length(pages: list[str], rep: Report) -> None:
    """Rule 24: at most two pages (CLAUDE.md rule 5; the candidate moved it from one page to
    two on 2026-11-05, in line with the 1.5-2 page norm the research supports)."""
    n = len(pages)
    if n > 2:
        overflow = len(pages[2].strip())
        rep.fail("24", "at most 2 pages",
                 f"{n} pages; page 3 carries {overflow} chars — cut bullets, do not shrink type")
    elif n == 2:
        rep.ok("24", "at most 2 pages", f"2 pages, last one {len(pages[1].strip())} chars")
    else:
        fill = len(pages[0].strip())
        if fill < 1800:
            rep.warn("24", "at most 2 pages", f"1 page but only {fill} chars — room for more")
        else:
            rep.ok("24", "at most 2 pages", f"{fill} chars")


def check_jd_coverage(pages: list[str], jd_path: Path, rep: Report) -> float:
    """Rule 20. Exact-string coverage of the tools the posting names.

    Terms in search_profile.yaml's `transferable_stack` are excluded from the denominator.
    Those are tools the candidate has not run (Snowflake, Databricks, Looker...) and which
    therefore must never appear in a CV — counting them as coverage misses punishes the
    document for obeying CLAUDE.md rule 2, and buries the misses that are actually
    actionable. They are reported separately, as information, not as a gap to close.
    """
    bank = yaml.safe_load(jobprofile.keyword_bank_path().read_text(encoding="utf-8"))
    terms = set()
    for group, vals in bank.items():
        if group in {"expansions", "negative_signals"}:
            continue
        terms.update(str(v) for v in (vals or []))

    profile_path = jobprofile.search_profile_path()
    transferable = set()
    if profile_path.exists():
        prof = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        for vals in (prof.get("transferable_stack") or {}).values():
            transferable.update(str(v).lower() for v in (vals or []))

    jd = jd_path.read_text(encoding="utf-8", errors="replace").lower()
    cv = "\n".join(pages).lower()
    wanted = sorted({t for t in terms if t.lower() in jd})
    if not wanted:
        rep.warn("20", "JD keyword coverage", "no known vocabulary terms found in the JD")
        return 0.0

    scored = [t for t in wanted if t.lower() not in transferable]
    not_ours = [t for t in wanted if t.lower() in transferable and t.lower() not in cv]

    hit = [t for t in scored if t.lower() in cv]
    miss = [t for t in scored if t.lower() not in cv]
    pct = 100 * len(hit) / len(scored) if scored else 100.0
    detail = f"{len(hit)}/{len(scored)} = {pct:.0f}%"
    if miss:
        detail += " | closable: " + ", ".join(miss[:6]) + ("…" if len(miss) > 6 else "")
    (rep.ok if pct >= 65 else rep.warn)("20", "JD keyword coverage", detail)
    if not_ours:
        rep.ok("20", "asked for, no experience",
               ", ".join(not_ours[:8]) + " — correctly absent")
    return pct


WORD_RE = re.compile(r"[a-z0-9]+(?:[.+#/-][a-z0-9]+)*")


def words(pages: list[str]):
    from collections import Counter
    return Counter(WORD_RE.findall("\n".join(pages).lower().replace("\u00ad", "")))


def experience_section(pages: list[str]) -> str:
    full = "\n".join(pages)
    up = full.upper()
    start = up.find("WORK EXPERIENCE")
    if start < 0:
        return ""
    ends = [up.find(h, start) for h in OPTIONAL_HEADINGS if up.find(h, start) > 0]
    return full[start:min(ends) if ends else len(full)]


def check_parsers(by: dict[str, list[str]], rep: Report, master: dict | None,
                  jd_path: Path | None, base_cov: float | None) -> None:
    """Rules 26-27 and every text check, on each extraction other than PDFKit's."""
    base_name, base = next(iter(by.items()))
    base_words = words(base)
    base_exp = experience_section(base)
    rendered = [e for e in (master or {}).get("experience", [])
                if e.get("company") and e["company"].lower() in base_exp.lower()]
    jobs = [e["company"] for e in rendered]
    titles = [e["title"] for e in rendered if e.get("title") and e["title"].lower() in base_exp.lower()]
    base_dates = len(DATE_RE.findall(base_exp))
    for name, pages in list(by.items())[1:]:
        # -- every text check, as this parser reads the CV; only its failures are reported,
        #    the warnings (verbs, numbers) are the same document's and already listed once.
        sub = Report()
        check_text(pages, sub, master)
        for st, rule, check, detail in sub.rows:
            if st == "FAIL":
                rep.fail(rule, f"{check} [{name}]", detail)
        # -- rule 26: the words agree. A token PDFKit reads and this parser loses is a word
        #    an ATS on that parser will not match (merged words, split hyphenations, glyphs).
        w = words(pages)
        lost = base_words - w
        share = 100 * sum(lost.values()) / max(1, sum(base_words.values()))
        detail = f"{share:.1f}% of {base_name}'s words missing"
        if lost:
            detail += ": " + ", ".join(sorted(lost)[:6])
        if share > 2:
            rep.fail("26", f"parsers agree [{name}]", detail)
        elif share > 0.5:
            rep.warn("26", f"parsers agree [{name}]", detail)
        else:
            rep.ok("26", f"parsers agree [{name}]", detail)
        # -- rule 27: each job found with its dates in the experience section.
        exp = experience_section(pages)
        flat = re.sub(r"\s+", " ", exp).lower()
        missing = [c for c in jobs if c.lower() not in flat]
        lost_titles = [t for t in titles if re.sub(r"\s+", " ", t).lower() not in flat]
        dates = len(DATE_RE.findall(re.sub(r"[ \t]+", " ", exp)))
        if not exp:
            rep.fail("27", f"jobs parsed [{name}]", "no WORK EXPERIENCE section found")
        elif missing or lost_titles or dates < base_dates:
            rep.fail("27", f"jobs parsed [{name}]",
                     f"{len(jobs) - len(missing)}/{len(jobs)} employers, "
                     f"{len(titles) - len(lost_titles)}/{len(titles)} titles, {dates}/{base_dates} date ranges"
                     + (f"; lost: {', '.join(missing + lost_titles)}" if missing or lost_titles else ""))
        else:
            rep.ok("27", f"jobs parsed [{name}]",
                   f"{len(jobs)} employers, {len(titles)} titles, {dates} date ranges")
        # -- rule 20 per parser: the coverage a keyword scorer on this parser would see.
        if jd_path and base_cov is not None:
            cov = check_jd_coverage(pages, jd_path, Report())
            if round(cov) < round(base_cov):
                rep.warn("20", f"JD coverage [{name}]", f"{cov:.0f}% vs {base_cov:.0f}% on {base_name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--jd", type=Path, help="job.md to score keyword coverage against")
    ap.add_argument("--no-master", action="store_true",
                    help="skip the traceability check (for auditing a foreign CV)")
    ap.add_argument("--report", type=Path, help="also write a markdown report here")
    args = ap.parse_args()

    if not args.pdf.exists():
        sys.exit(f"No such file: {args.pdf}")

    pages, _ = extract_text_pdfkit(args.pdf)
    reader = PdfReader(str(args.pdf))
    master = None if args.no_master else yaml.safe_load(MASTER.read_text(encoding="utf-8"))

    rep = Report()
    check_structure(reader, rep)
    check_hidden_text(reader, rep)
    check_text(pages, rep, master)
    check_length(pages, rep)
    coverage = check_jd_coverage(pages, args.jd, rep) if args.jd else None
    by = {"PDFKit": pages, "pdfminer": extract_text_pdfminer(args.pdf, len(reader.pages)),
          "pypdf": extract_text_pypdf(reader), "PyMuPDF": extract_text_pymupdf(args.pdf),
          "PDFium": extract_text_pdfium(args.pdf), "pdfplumber": extract_text_pdfplumber(args.pdf)}
    poppler = extract_text_pdftotext(args.pdf)
    if poppler is not None:
        by["pdftotext"] = poppler
    check_parsers(by, rep, master, args.jd, coverage)

    print(f"\nATS lint — {args.pdf.name}\n")
    print(rep.render())
    verdict = "FAILED" if rep.failures else ("PASSED with warnings" if rep.warnings else "PASSED")
    print(f"\n{verdict}: {rep.failures} failure(s), {rep.warnings} warning(s)\n")

    if args.report:
        md = [f"# ATS report — {args.pdf.name}", "",
              f"Checked against `rules/ats_rules.md` on {__import__('datetime').date.today()}.", "",
              f"**{verdict}** — {rep.failures} failure(s), {rep.warnings} warning(s)", ""]
        if coverage is not None:
            md.append(f"**JD keyword coverage: {coverage:.0f}%**\n")
        md += ["| | Rule | Check | Detail |", "|---|---|---|---|"]
        md += [f"| {s} | {r} | {n} | {d} |" for s, r, n, d in rep.rows]
        args.report.write_text("\n".join(md) + "\n", encoding="utf-8")
        print(f"Report written to {args.report}")

    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
