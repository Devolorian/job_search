#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Render a vacancy folder's cover_letter.md into the formats an application actually takes.

Markdown is the authoring format — the model writes it, the
same way tailor.yaml is written and then rendered — but nobody accepts a .md in an application.
This produces the two things that are actually used, because one file cannot do both jobs:

  cover_letter.pdf   to attach. Same Arial, same margins and the same ATS constraints as the
                     CV, through the same Chrome --print-to-pdf path, so a recruiter opening
                     both sees one document set rather than two unrelated files.
  cover_letter.txt   to paste. Most forms want the letter in a textarea, and copying out of a
                     PDF drags in broken line breaks and hyphenation. The txt is hard-wrapped
                     at nothing — one paragraph per line — so it survives any textarea.

Not .docx: it would need a new dependency and a second look-and-feel, and the CV next to it is
already a PDF. Not .doc or .rtf: no ATS asks for them and both are worse at everything.
If a specific employer demands .docx, convert that one by hand rather than changing the rule.

The heading line and any Markdown syntax are stripped: a cover letter is prose, and "##" or
"**" reaching a recruiter's screen is the kind of detail that reads as carelessness.

Usage:
    uv run scripts/render_cover_letter.py --job <vacancy_dir>
    uv run scripts/render_cover_letter.py --all        # every folder that has a letter
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import jobprofile  # scripts/jobprofile.py — paths shared by every script

ROOT = Path(__file__).resolve().parent.parent
CHROME = jobprofile.chrome_path()

# Mirrors cv/template.html: Arial, ligatures off, pure black, single column, no header or
# footer. Rules 4, 7, 8, 10 and 23 of rules/ats_rules.md apply to this document too — it is
# parsed by the same software. Slightly looser leading than the CV, because a letter is read
# rather than scanned and has no one-page constraint to fight.
TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title><style>
@page {{ size: A4; margin: 20mm 20mm 18mm 20mm; }}
html, body {{ margin: 0; padding: 0; background: #ffffff; }}
body {{
  font-family: Arial, Helvetica, sans-serif;
  font-variant-ligatures: none; -webkit-font-variant-ligatures: none; font-kerning: none;
  font-size: 11pt; line-height: 1.45; color: #000000;
}}
p {{ margin: 0 0 10pt 0; }}
p.sign {{ margin-top: 16pt; }}
</style></head><body>
{body}
</body></html>
"""


def to_plain(md: str) -> list[str]:
    """Markdown -> paragraphs of plain prose.

    Drops the '# Cover letter — Company, Role' heading: it is a filing label for the folder,
    not part of the letter, and it would read as a title page to whoever opens the PDF.
    """
    text = re.sub(r"^#.*$", "", md, flags=re.M)          # headings
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # bold
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)  # italics
    text = re.sub(r"`(.+?)`", r"\1", text)                # code spans
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)       # links -> label
    paras = []
    for block in re.split(r"\n\s*\n", text):
        # Single newlines inside a paragraph are wrapping in the source file, not line breaks
        # the reader should see. The signature block is the exception — its lines are real.
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if not lines:
            continue
        if len(lines) > 1 and any(re.search(r"@|\+\d", l) for l in lines):
            paras.append("\n".join(lines))
        else:
            paras.append(" ".join(lines))
    return paras


def html_to_pdf(html_text: str, out_pdf: Path) -> None:
    """Chrome writes the PDF and then does not exit, so poll for the file to stop growing
    and terminate the process ourselves. Copied from render_cv.py, which hit this first."""
    if not Path(CHROME).exists():
        sys.exit(f"Chrome not found at {CHROME} — needed to produce the PDF text layer.")
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    if out_pdf.exists():
        out_pdf.unlink()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "letter.html"
        src.write_text(html_text, encoding="utf-8")
        proc = subprocess.Popen([
            CHROME, "--headless=old", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--no-pdf-header-footer",
            f"--user-data-dir={Path(td) / 'profile'}",
            f"--print-to-pdf={out_pdf}", src.as_uri(),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 60
            stable, last = 0, -1
            while time.monotonic() < deadline:
                time.sleep(0.4)
                size = out_pdf.stat().st_size if out_pdf.exists() else 0
                if size > 0 and size == last:
                    stable += 1
                    if stable >= 3:
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


def render(folder: Path) -> bool:
    md = folder / "cover_letter.md"
    if not md.exists():
        print(f"  ! {folder.name}: no cover_letter.md", file=sys.stderr)
        return False
    paras = to_plain(md.read_text(encoding="utf-8"))
    if not paras:
        print(f"  ! {folder.name}: cover_letter.md has no body", file=sys.stderr)
        return False

    (folder / "cover_letter.txt").write_text("\n\n".join(paras) + "\n", encoding="utf-8")

    # Built without f-string escapes on purpose: this file declares requires-python >= 3.11,
    # and a backslash inside an f-string expression is a SyntaxError before 3.12. It parsed
    # here only because uv happened to pick 3.14.
    blocks = []
    for p in paras:
        lines = p.split("\n")
        cls = ' class="sign"' if len(lines) > 1 else ""
        blocks.append("<p" + cls + ">"
                      + "<br>".join(html.escape(l) for l in lines)
                      + "</p>")
    body = "\n".join(blocks)
    out = folder / "cover_letter.pdf"
    html_to_pdf(TEMPLATE.format(title=f"Cover letter — {folder.name}", body=body), out)
    print(f"  {folder.name}: cover_letter.pdf ({out.stat().st_size // 1024} KB) + .txt",
          file=sys.stderr)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--job", type=Path, help="vacancy folder containing cover_letter.md")
    g.add_argument("--all", action="store_true", help="every vacancy folder with a letter")
    args = ap.parse_args()

    if args.job:
        return 0 if render(args.job) else 1
    n = sum(render(d) for d in sorted((ROOT / "vacancies").iterdir())
            if d.is_dir() and (d / "cover_letter.md").exists())
    print(f"\n  {n} letter(s) rendered", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
