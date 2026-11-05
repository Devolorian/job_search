# Scan rules — the candidate-specific part of prompts/daily_scan.md

A FICTIONAL example. Each section names the stage of `prompts/daily_scan.md` it applies to.

## Stage 3 — family rules

- `ux_researcher` passes only when the work is qualitative research with users (interviews,
  usability tests); a survey-analytics or market-research role is a drop.
- A Product Designer role whose core is marketing pages or brand work is a drop — log the phrase.

## Stage 3 — employment type

Every passing posting gets a `- Contract:` line in `job.md`:
- `permanent` → pass.
- `freelance` → pass, but never listed in `## Send today`; note it in the log.
- `unstated` → pass; ask in the first call.

## Stage 3 — language

English postings pass. A German posting passes when the work is German-language; German is the
candidate's native language. Other languages → drop.

## Stage 4 — tailoring

Summary variants: `product_designer` for every family except `design_systems`, which uses
`design_systems`.

## Stage 4e/4f — geography line

Based in Berlin, EU citizen: an EU-remote role or a German office works without sponsorship. One
line in the letter, half a sentence in why_join.

## Stage 5d — `## Send today`

Order by: posted in the last 48 hours; a domain in `domains_of_interest`; Berlin before
remote-EU. The match score breaks ties only.
