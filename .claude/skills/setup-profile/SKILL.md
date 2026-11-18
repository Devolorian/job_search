---
name: setup-profile
description: Build or update the candidate's profile/ from the documents in profile/inbox/ (a CV as PDF/DOCX/TXT, a LinkedIn "Save to PDF" export) and a short interview — master_cv.yaml with the bullet bank, search_profile.yaml (roles, geography, languages, tracks), routing.yaml, the sources overlay, CANDIDATE.md, scan_rules.md, story_bank.yaml and form_answers.yaml. Use on "set up my profile", "/setup-profile", "I added my CV", "onboard me", or when profile/ does not exist yet. Never invents a fact.
argument-hint: "[--update]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# setup-profile — from a CV and a LinkedIn PDF to a working profile

The pipeline knows the candidate only through `profile/`. This skill builds it from what the
candidate drops into `profile/inbox/`, plus their answers to a few questions. `profile.example/`
is the reference for every file's shape — read it first, and keep its schema exactly (the scripts
read those keys).

Reply in the language the candidate writes in; ask which one they want the pipeline to use and
record it as `assistant_language` in `CANDIDATE.md`. Everything written into `profile/` — YAML and
Markdown alike — is in English, except quotes of the candidate's own words.

## The one rule that matters

**Facts come from the documents and from the candidate, never from you** (CLAUDE.md rule 2).
Every employer, title, date, tool, metric, degree in `master_cv.yaml` must be traceable to a line
in an inbox document or to something the candidate said in this session. When the CV and the
LinkedIn export disagree (dates, titles, spelling of a name), do not pick one — ask. A bullet you
reworded for clarity, or one whose figure you are not sure you read correctly, gets
`needs_confirmation: true`. Nothing is "improved" with a number the documents do not contain.

## Steps

1. **Inputs.** List `profile/inbox/`. Read every file: PDFs with `uv run --with pypdf python -c
   "..."` (or the Read tool), DOCX with `uv run --with python-docx python -c "..."`. If the
   inbox is empty, say what to put there (CV, LinkedIn → More → Save to PDF) and stop. If
   `profile/` already exists, this is an update: read the current files, and change only what
   the new documents or answers justify — never drop a bullet, story or confirmed detail
   silently; list what you would remove and ask.

2. **`profile/cv/master_cv.yaml`**, in `profile.example/cv/master_cv.yaml`'s schema:
   - `contact` — name, email, phone, location, LinkedIn URL from the documents; ask for work
     authorisation and `open_to` in the candidate's words.
   - `experience` — every role from both documents, newest first, a short lowercase `id` per
     role (the company's name, e.g. `acme`). Bullets: one achievement each, past tense, verb
     first, the figures exactly as written. LinkedIn often carries roles or detail the CV
     dropped: take them, marked `needs_confirmation: true`. The bank is a superset — keep every
     bullet, the CV (at most two pages) is a selection from it.
   - `skills` — grouped, every skill with `evidenced_by:` naming the role ids where the
     documents show it used. A skill no role evidences does not go in.
   - `summaries` — one per kind of role the candidate targets (keys are free; the scan rules
     map role families to them), 2-3 sentences each, built only from the bank.
   - `default_selection`, `education`, `certifications`, `languages`.
   Then render and lint it: `uv run scripts/render_cv.py --generic --allow-todo` and
   `uv run scripts/ats_lint.py profile/cv/CV_*.pdf`. Fix the selection until it fits two pages.

3. **Ask, briefly, what the documents cannot say** — one message, grouped, with your proposed
   answer for each so the candidate can just confirm:
   - target roles and the title variants they would accept (→ `role_families`), titles that
     must never pass (→ `title_exclusions`), seniority;
   - home city and country; remote scope; whether onsite/hybrid counts and where (→ `geo`);
   - employment type (permanent / contract / either), languages they work in (→ `languages`,
     `posting_languages`);
   - domains they want or have depth in (→ `domains_of_interest`);
   - salary expectation and notice period — only if they want standing answers
     (→ `form_answers.yaml`; otherwise `TODO`);
   - one track or several (e.g. a separate search for a second profession) (→ `tracks`).

4. **Write the rest**, each from `profile.example/`'s version:
   - `search_profile.yaml` — role families with weights, exclusions, `geo.home` (labels are
     lowercase single words), `geo.onsite_or_hybrid`, scoring (keep the example weights unless
     asked), `transferable_stack` (tools close to theirs — score only), `timezone`, `tracks`.
   - `routing.yaml` — keep LinkedIn to the core titles (its keywords search descriptions too).
   - `sources.yaml` — LinkedIn keywords and location lanes, wellfound lanes, Himalayas country
     and queries, board-discovery queries; switch off what does not apply (Built In without a
     listing for their city, Telegram until they set it up, wantapply if they do not use it).
   - `keyword_bank.yaml` — only when the profession is not covered by `rules/keyword_bank.yaml`
     (data roles): the tools and practices postings in their field name, as a vocabulary for
     recognising postings, not as claims.
   - `CANDIDATE.md` — `assistant_language`, who they are in three lines, what a match is, and a
     `Decisions` section that starts with today's choices, dated.
   - `scan_rules.md` — the Stage 3 gates in their words: family rules, employment type with the
     `Contract:` line, language rule, which summary variant per family, the geography line for
     letters, how `## Send today` is ordered.
   - `cv/story_bank.yaml` — 3-5 draft stories from the strongest bullets, `open_questions`
     listing what only the candidate can tell (`interview-debrief` fills them later).
   - `cv/form_answers.yaml` — what they answered; everything else `TODO`.

5. **Check** — `uv run --with pyyaml scripts/role_routing.py` prints a sensible matrix;
   `uv run --with pyyaml python scripts/jobprofile.py tracks` lists the tracks;
   `uv run scripts/collect_himalayas.py --max-requests 2` (no `--json`, writes nothing) returns
   on-target titles.

## Reply

What was built, the bullets marked `needs_confirmation` (ask the candidate to confirm or correct
each), the `TODO`s left, and the next steps: `scripts/setup_sheets.py` for the tracker
(service account in `~/.job_search/sheets.env`), `collect_mail.py --auth` for Gmail,
`scripts/install_schedule.sh` for the schedule — see README.md.
