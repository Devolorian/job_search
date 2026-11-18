---
name: humanize-cv
description: Rewrite CV bullets, summaries, cover letters and LinkedIn profile text (About, experience, headline) so they read like the candidate wrote them, not a chatbot. Use when asked to "humanize", "make it sound less AI", "make it sound like me", or to clean up master_cv.yaml summaries/bullets, a cover_letter.md, or a LinkedIn export. Never adds facts.
argument-hint: "<file | 'linkedin' | pasted text>"
allowed-tools: Read, Edit, Write, Bash, Grep
---

# Humanize CV and LinkedIn text

Target: text a recruiter reads and thinks "a senior data person wrote this, quickly, about work
they actually did". Not "polished", not "impressive" — *specific*. Specificity is the only
thing that reliably separates human career text from generated text, and here it has exactly
one legal source.

Reply in the candidate's language (`profile/CANDIDATE.md`, `assistant_language`; English if
unset). The rewritten CV, letter and LinkedIn text stay in the language they are written for
(English by default).

Pattern catalogue adapted from Wikipedia's *Signs of AI writing* (WikiProject AI Cleanup) and
the MIT-licensed `blader/humanizer` / `humanizer_ru` skills, narrowed to career documents —
those skills target prose and will happily recommend "add a personal anecdote", which in a CV
is fabrication.

## Hard rules (override everything below)

1. **CLAUDE.md rule 2 applies in full.** `profile/cv/master_cv.yaml` is the only source of
   facts. The cure for vague text is a detail *from the bank*, never an invented one. If a
   passage is vague and the bank has nothing more specific, either cut it or ask the candidate
   a question — list the question, do not guess the answer. No new number, tool, team size,
   date, client, or outcome. A rewrite that "sounds more real" because it made something up is
   worse than the AI text it replaced.
2. **No detector tricks.** No homoglyphs, zero-width characters, deliberate typos, odd Unicode
   spacing, or text aimed at a machine reader (CLAUDE.md rule 3). They are detectable, they break
   ATS parsing, and they are exactly the "manipulative" signal rule 3 exists to avoid. The goal
   is writing that is human, not writing that fools a classifier.
3. **Keep the ATS keywords.** Humanizing must not drop a tool name or a term the tailored CV
   was selected for. Rephrase around the keyword; never replace "dbt" with "our transformation
   layer". Expansion conventions stay (`dbt (data build tool)` on first mention).
4. **The candidate's voice, the candidate's English.** Match the language level and register
   recorded in `profile/CANDIDATE.md` (and the `languages` in the master CV): plain, direct
   sentences the candidate can repeat out loud in an interview. Do not upgrade a non-native
   speaker's text to native copywriter English — a CV that sounds more fluent than the person
   on the call is its own red flag.
5. **CV and LinkedIn must agree** on company names, titles, dates and numbers. Recruiters and
   some ATSes (Workday) cross-check. Never "fix" one side's facts in prose — report the
   conflict and let the candidate decide which side is true.

## Register by surface

| Surface | Person | Shape |
|---|---|---|
| CV summary (`summaries.*`) | no "I", no pronoun | 2-3 sentences, what the candidate builds + where + on what |
| CV bullet | implied subject, past tense (present for current role is fine) | verb, object, how, result. One idea. 15-30 words |
| Cover letter | first person, plain | per `prompts/daily_scan.md` 4e, under 220 words |
| LinkedIn About | first person, conversational | short paragraphs, no bullet list of "I specialize in:" |
| LinkedIn experience | first person allowed, terse | 2-5 lines or bullets; context first (what the company is, what the team did) |
| LinkedIn headline | noun phrase | role + 1-2 concrete stack words, no "Expert", no "Passionate" |

## Tells to remove

### Vocabulary
Cut on sight, then rewrite with the plain verb: *spearheaded, orchestrated, leveraged,
utilized, robust, seamless, cutting-edge, innovative, dynamic, passionate, proven track record,
results-driven, synergy, holistic, pivotal, crucial, key (as filler), foster, empower, elevate,
delve, navigate (figurative), landscape, realm, tapestry, testament, journey, ecosystem
(non-technical), actionable insights, data-driven decision-making, strategic (as decoration),
best-in-class, world-class, valuable, significant(ly), various, diverse, effectively,
efficient(ly), ensuring, in order to.*
Replace with what was done: *built, cut, moved, replaced, wrote, ran, hired, fixed, migrated*.

### Structure
- **Inline-header bullets** — "**Team Leadership:** Leading and motivating…". One of the
  strongest single tells in LinkedIn exports. Delete the label, keep the content if it has any.
- **Duty lists instead of work.** "Collaborating with team members to achieve common goals",
  "Monitoring results of implemented changes" — true of every job, therefore says nothing. Cut.
- **Tautology.** "Data-driven decision making: Making data-driven decisions."
- **Uniform rhythm.** Every bullet the same length, same "Verb + noun + resulting in X%"
  template. Vary: some bullets are one short clause, some carry the how.
- **Rule of three.** "availability, reliability and scalability" when only one was meant.
- **Negative parallelism.** "Not just X, but Y", "It's not about X — it's about Y".
- **-ing tails.** "…, ensuring data integrity and availability", "…, driving business value".
- **Signposting / closers.** "I have successfully…", "Overall,…", "In summary,…".
- **Copy-paste across roles.** Identical bullets under two employers read as generated (or
  padded). Each role gets its own facts or fewer bullets.

### Numbers
- **Unmeasurable percentages** — "increased decision-making efficiency by 30%", "boosted team
  productivity by 25%". Nobody measures those; an interviewer will ask how. Keep a number only
  when the bank has it *and* it measures a thing with a unit: hours, rows, systems, people,
  reports. "6 hours to 15 minutes" beats any percentage.
- Round-number clusters (20%, 25%, 30%, 50%) across a profile look invented even when true.

### Typography
- Em dashes: at most one per paragraph, none in bullets; use a comma, colon or a new sentence.
- No bold mid-sentence, no emoji, no Title Case Headings In Prose, straight quotes only.
- Consistent product casing: `dbt` (never DBT), `ClickHouse`, `PostgreSQL`, `Power BI`.

### Signs of a human (what to aim at)
- Concrete nouns from the bank, of the kind *nightly batch load, 9 source systems, warehouse
  sensor logs, field technicians* — whatever the candidate's own bank actually contains.
- A cause stated plainly: "by replacing full reloads with incremental loads".
- Occasional short sentence. Occasional bullet without a number.
- Context a chatbot would not know to give: what the company actually is ("regional retail
  bank", "logistics marketplace, 40k orders a day") — again, only as the bank states it.

## Workflow

1. **Load the facts.** Read `profile/cv/master_cv.yaml`. For LinkedIn input, use
   `profile/cv/linkedin_profile.md` if it exists, or extract text from a LinkedIn PDF
   (`uv run --with pypdf python -c "import pypdf,sys;print('\\n'.join(p.extract_text() for p in pypdf.PdfReader(sys.argv[1]).pages))" <pdf>`
   into the scratchpad; poppler may not be installed). Build the fact list you are allowed to use.
2. **Lint.** `uv run scripts/ai_tells_lint.py <file>` (`--yaml` for `master_cv.yaml`, `-` for stdin). It reports the
   mechanical tells with line numbers. It is a floor, not the judgement — it cannot see
   duty lists or copy-paste meaning.
3. **Cross-check** (LinkedIn or anything outside the bank). Table every disagreement with
   `master_cv.yaml`: names, titles, dates, numbers, tools claimed. These go to the candidate as
   questions *before* any rewrite of the affected section.
4. **Rewrite.** Section by section, following the register table. Prefer cutting to padding.
5. **Fact audit.** For every rewritten line, name the bank entry it comes from. Any number,
   tool, or claim without a source is removed. Show this mapping.
6. **"Obviously AI?" pass.** Re-read the result asking only: what in this still sounds
   generated? Fix that, re-run the linter, aim for zero hard hits.
7. **Output.** Before / after per section, the open questions, and the linter result.
   For `master_cv.yaml`, edit in place and keep the comments; re-render with
   `uv run scripts/render_cv.py --generic` and `ats_lint.py` so the page-limit and keyword rules
   still pass. For LinkedIn, write `profile/cv/LinkedIn_profile.txt` (inside the gitignored
   profile) — plain text, one `=====` block per LinkedIn field, since LinkedIn renders no
   Markdown. Check field limits (headline 220, About 2,600, each description 2,000). The
   candidate pastes it themselves; nothing is posted (CLAUDE.md rule 1).
