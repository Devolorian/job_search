---
name: funnel-review
description: Weekly look at what converts — which sources, role families, score bands and geographies turn into replies and interviews, which only into rejections, and which postings the candidate turned down by hand. Proposes changes to profile/search_profile.yaml, profile/routing.yaml and the sources config; never applies them. Use on "funnel review", "what converts", "review the funnel", "/funnel-review".
argument-hint: "[--since YYYY-MM-DD]"
allowed-tools: Read, Write, Bash, Grep, Glob
---

# funnel-review — does the score predict anything?

The pipeline scores every posting and the tracker records what happened to it; this is the step
that compares the two. Advisory only — it never edits config, the profile, prompts, the tracker
or the bank.

## Run

```bash
uv run scripts/funnel_report.py --json state/_funnel_rows.json
```

Read-only by construction (`spreadsheets.readonly`). The report gives counts by Source, family,
score band, geography and track over **applied rows only**, and a table of rows the candidate did
not send, with the reason they wrote into `Stage reached` (e.g. `language`, `location`,
`domain`…).

`--since` is applied by filtering `state/_funnel_rows.json` on `Date added`.

## What to look for

1. **Score vs outcome.** Does 80+ reach interview more often than 60-79? If not, which weight
   is carrying postings that go nowhere — open three or four rejected 80+ folders' `job.md` and
   say what they share (seniority? domain? a stack word?).
2. **Source quality.** Replies and interviews per applied row, per source. A source with many rows
   and none sent is a different problem (the candidate does not trust it) from one with many sent
   and no replies (the employers do not answer).
3. **Hand rejections are the gates' misses.** Each reason with 2+ rows is a candidate rule:
   say which gate would have caught it and what it would also have caught. Check it against the
   current CLAUDE.md, `profile/CANDIDATE.md` and `profile/scan_rules.md` first — a reason may
   predate a rule that has since changed (e.g. rows skipped for a city from before that city
   started passing the geography gate are a reason to ask whether that decision holds, not to
   reverse it).
4. **Time to reply.** Median days to first reply sets the follow-up threshold (`follow-up` skill).
5. **Small numbers.** With fewer than ~10 applied rows in a group, say so and draw no conclusion.
   One interview is an anecdote.

## Output

`logs/funnel_<YYYY-MM-DD>.md`, written in the candidate's language (`profile/CANDIDATE.md`,
`assistant_language`; English if unset) — the headings below are given in English and are
written in that language: the report's tables, then

```
## What the data shows   3-5 bullets, each with its numbers
## Proposals             each: the change (file, key, old → new), the evidence, what it would
                         also have dropped or let through, and "your call"
## What it cannot show   what the data cannot answer yet, and what would answer it
```

Reply with the 2-3 findings worth acting on and the path. Nothing in `config/` or `profile/`
changes until the candidate says yes to a specific proposal; then edit it as agreed and add a
dated comment explaining why, in the style of the file.

## Not this skill's job

- Fixing tracker cells (blank Company, a wrong stage) — `prompts/log_review.md` does that from the
  run logs, and mail does stages.
- Writing to the sheet at all.
