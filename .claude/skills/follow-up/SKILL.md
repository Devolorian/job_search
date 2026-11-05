---
name: follow-up
description: List applications with no employer reply N days after they were sent and draft a short, factual follow-up for each one worth chasing — for the candidate to send themselves. Never sends, never writes the tracker. Use on "follow-up", "who hasn't replied", "who should I chase", "/follow-up [days]".
argument-hint: "[days, default 10]"
allowed-tools: Read, Write, Bash, Grep, Glob
---

# follow-up — who has gone quiet, and a note the candidate can send

**Rule 1 holds in full: this drafts, the candidate sends.** No email, no LinkedIn message, no
form is touched by the skill.

Reply in the candidate's language (`profile/CANDIDATE.md`, `assistant_language`; English if
unset). The follow-up draft itself is employer-facing: the posting's language / English.

## Run

```bash
uv run scripts/funnel_report.py --followups 10
```

Rows with `Applied on` set, no `First reply on`, not rejected, not `skipped`, sent 10+ days ago.
Ten days is the default because a typical median time to a first reply is a few days;
`funnel-review` re-measures it on the candidate's own tracker. The argument overrides it.

## Triage each row — not every silence is worth a message

Read the folder's `job.md` and `source.json`, and `state/mail.jsonl` for anything from the
company. Then one of:

- **chase** — a direct employer posting (own ATS board, LinkedIn by the employer), sent 10-30 days
  ago, a contact or channel exists.
- **skip** — a recruiter or repost board (Jobgether and the `ignore_slugs` in
  `config/sources.yaml` / `profile/sources.yaml`: there is no employer to write to), a posting
  that has since closed (`curl -sI` on the link: 404 or a redirect to the board's root), or 45+
  days of silence. Say why.
- **check** — a reply may exist that the mail stage did not match (a different sender domain, a
  forwarded ATS mail). Grep `state/mail.jsonl` for the company name and report what you found.

## The draft

Into the folder as `follow_up.txt`, in the posting's language (English by default), 50-80 words,
the candidate's voice (`humanize-cv` catalogue):

- the role and the date they applied — facts from the tracker;
- one line linking the posting's core requirement to one bank fact the CV already carried
  (`tailor.yaml`), not a new pitch;
- a plain question: is the role still open, what is the timeline.

No invented recruiter name — "Hello," unless a name is in `job.md` or a mail from them. No
"just checking in", no "I hope this finds you well", no pressure. Where to send it: the apply page
rarely takes a message, so say where a message could go if the folder shows one (a recruiter in
the posting, a reply-to address in an ATS email) — and if none, say so instead of guessing an
address.

`uv run scripts/ai_tells_lint.py <folder>/follow_up.txt` → 0 hard hits.

## Reply

In the assistant language: a table — company, days, verdict (chase / skip / check) and why, the
draft's path. When the candidate says one was sent, nothing here changes the sheet: the date goes
in Notes by their hand, and a reply that comes back is picked up by the mail stage as usual.
