# ATS ruleset

Derived from research on 2025–2026 ATS parsing behaviour (2026-09-07). Every rule below is
either enforced automatically by `scripts/ats_lint.py` (marked **[lint]**) or is a template
invariant baked into `templates/cv.html` (marked **[template]**; a
`profile/cv/template.html` override must keep every one of them).

**Caveat worth keeping in mind:** no ATS vendor publishes a parsing spec, so this space is
reverse-engineered. Where the evidence is vendor-reported rather than independently audited,
it is flagged. The rules below are the convergent core — the parts every source agrees on.

## Why this matters here specifically

A typical CV exported from Google Docs fails rule 25. Its text layer emits bullet glyphs as a
run of separate lines followed by the bullet text, so a linear parser reads:

```
●
●
●
Improved pipeline performance by 30% ...
Modified the data model to improve accuracy ...
Created and maintained 40 reports ...
```

The bullets are detached from their markers and the reading order is scrambled. This is a
Google Docs PDF export artifact. Rule 25 is the regression test against it.

---

## Layout

1. **[template]** Single-column layout only. No sidebars. Taleo reads multi-column layouts
   left-to-right across the whole page, interleaving unrelated columns into nonsense. Lever
   is reported to silently drop sidebar content with no error.
2. **[template][lint]** No tables — including invisible-border tables used for layout.
   Documented failure mode in Workday.
3. **[template][lint]** No text boxes. Often invisible to parsers entirely.
4. **[template][lint]** No content in header/footer regions. Contact info there is skipped
   as boilerplate or extracted once per page and discarded as duplicate noise (specifically
   documented for iCIMS).
5. **[template][lint]** No images, logos, icons, photos, or graphical skill-rating bars.
   A "Python ▓▓▓▓░" bar yields zero extractable signal.
6. **[template]** No charts or graphs.

## Typography

7. **[template][lint]** Font from the system-safe list: Arial, Calibri, Georgia,
   Times New Roman, Cambria, Verdana, Tahoma. Decorative fonts risk substitution on export.
   This project uses **Arial**.
8. **[template][lint]** Body font ≥ 10pt. This project uses 10.5pt body, 11pt name line.
9. **[template][lint]** Bullet character `•` (U+2022) only. No custom icon bullets.
   Google Docs exports often use `●` (U+25CF) — a different, less standard glyph.
10. **[template][lint]** Ligatures disabled (`font-variant-ligatures: none`). PDF export can
    silently map `fi`/`fl`/`ff` to nothing, turning "profile" into "prole" and "workflow"
    into "workow" in the extracted text.
11. **[template][lint]** Date ranges use a spaced en dash `–`. No em dashes glued to digits,
    no exotic Unicode dash variants.

## File

12. **[decision]** PDF only, a project decision. Trade-off recorded: a 2026
    vendor test reported DOCX ~97% parse accuracy vs text-layer PDF ~76% and "designed" PDF
    ~53%. Methodology undisclosed — directional, not authoritative. The template is built to
    land at the top of the PDF band, and rule 25 verifies it per build. `render_cv.py` has a
    `--docx` path stubbed if this is ever revisited.
13. **[lint]** The PDF must have a genuine text layer. Never a scan or a flattened image.

## Structure and headings

14. **[template][lint]** Standard section headings only, exact strings:
    `Professional Summary`, `Technical Skills`, `Work Experience`, `Education`,
    `Certifications`, `Languages`. Parsers match section labels against a known dictionary;
    "My Toolkit" or "Where I've Worked" lands in an unclassified bucket.
    Several 2026 sources claim Skills is now parsed first and mapped to scorecard criteria —
    vendor claim, moderate confidence, but it costs nothing to put Skills high.
15. **[template]** Reverse-chronological order within each section.
16. **[template][lint]** One date format throughout: `Mon YYYY` (e.g. `Dec 2025`), with
    `Present` — capitalised, not "Current" or "Now" — for the ongoing role. Mixed formats are
    what actually break tenure and gap calculation, more than any specific format choice.

## Content

17. **[lint]** Every bullet starts with a past- or present-tense action verb: Led, Built,
    Designed, Implemented, Migrated, Automated, Reduced, Standardised, Established…
    Never "Responsible for", never a bare noun phrase.
18. **[lint]** ≥70% of bullets carry a quantified outcome — %, count, volume, time, money,
    scale. LLM screening layers reportedly score quantified bullets measurably above
    duty-listing language.
19. **[lint]** Every skill claimed in `Technical Skills` is evidenced by at least one bullet
    or an explicit Education/Certification entry. A bare skills list with no supporting
    evidence is weak signal to both the keyword scorer and the LLM reader.
20. **[lint]** Tools named in the target job description appear as the **exact string** the
    posting uses (`Snowflake`, `dbt`, `Airflow`) — proper nouns have no useful synonyms and
    legacy exact-match parsers will not infer them. Only ever for tools genuinely in the
    bullet bank (see CLAUDE.md rule 2).
21. **[template]** Acronym + expansion on first use for major terms:
    `dbt (data build tool)`, `ELT (Extract, Load, Transform)`, `CDC (Change Data Capture)`,
    `SCD (Slowly Changing Dimension)`. Hedges against parsers matching only one form.
22. **[lint]** No hidden text of any kind: no white-on-white, no ≤1px fonts, no off-page
    positioning, no text addressed to an automated reader. Hidden prompt injection aimed at
    LLM screeners has been found in real hiring corpora and is treated as manipulative — it
    gets applications flagged, not advanced. The lint compares extracted text against the
    visible render and fails on any mismatch.
23. **[template][lint]** Contact block as plain body text at the top of page 1: full name,
    phone, email, city/region, LinkedIn as a spelled-out URL. Not an icon-only link — an
    icon with no extractable URL text is a known failure point.

## Length

24. **[lint]** **Exactly one page** — the one-page rule (CLAUDE.md rule 5), overriding the
    research default. The evidence says ATS does not truncate by page count and that 1.5–2
    pages is the norm for a senior IC — so this trades some content for a recruiter-facing
    format that is quick to read. The cost is real and is paid deliberately: it means every
    render is a *selection* from the bullet bank, and the selection has to be good. A
    two-page render is a lint failure, not a warning.

## Extraction integrity

25. **[lint]** **Reading order check.** Re-extract the text from the produced PDF via the
    same path a parser takes and assert it matches the source content in order: each bullet's
    text immediately follows its marker, sections appear in template order, and no run of
    orphaned bullet glyphs exists. This is the rule a Google Docs export violates, and the
    single highest-value check in this file.

## What is deliberately NOT done

- **Chasing a scanner score.** A 2025 cross-tool study found a 15–20 point spread on the
  *identical* resume across major scoring tools. The absolute number is close to meaningless;
  only the direction of change within one tool is informative. Use scanners for "did I miss
  an obvious required keyword", never as a pass/fail oracle.
- **The "ATS auto-rejects 75% of resumes" figure.** Widely repeated, not backed by solid
  evidence. ATS is a pre-filter, not the decision-maker.
- **Keyword stuffing.** Each important keyword should appear naturally 2–3 times across
  different sections with context. Repetition without context reads as stuffing to both the
  frequency scorer and the LLM layer.
