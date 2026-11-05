---
name: deslop
description: Strip generated-text tells from everything a vacancy folder sends out — the rendered CV, cover_letter.md and why_join.txt — and fix them using bank facts only. Run on every folder after rendering (prompts/daily_scan.md 4g), or on demand: "deslop", "remove the AI tells", "clean up the letter". Never adds facts.
argument-hint: "<vacancy folder | 'today' | file>"
allowed-tools: Read, Edit, Write, Bash, Grep, Glob
---

# deslop — the last pass before a folder counts as done

**Every CV the pipeline renders goes through this pass**, and so does every piece of prose that
travels with it. A folder is not done until all three files have **0 hard hits**.

Reply in the candidate's language (`profile/CANDIDATE.md`, `assistant_language`; English if
unset). The files themselves are employer-facing and stay in the posting's language / English.

This skill is the *gate*; `.claude/skills/humanize-cv/SKILL.md` is the *catalogue* — the full
list of tells, the register per surface, and how to rewrite them. Read it once per session
before the first rewrite. The hard rules there apply here unchanged, and the first one is the
whole point: **the cure for vague text is a detail from `profile/cv/master_cv.yaml`, never an
invented one** (CLAUDE.md rule 2). No detector tricks either (rule 3).

## What gets checked, per folder

"The CV PDF" is the file named by `jobprofile.cv_filename()` — `contact.cv_filename` in the
master CV, or `CV_<Surname>.pdf`.

| File | Lint | Who fixes a hit, and how |
|---|---|---|
| the CV PDF | `uv run scripts/ai_tells_lint.py <folder>/<CV PDF>` | The CV is a **selection** from the bank: its text is the bank's text. Fix by changing the selection in `tailor.yaml` (swap the bullet for a sibling that says the same thing plainer), re-render, re-run `ats_lint.py`. |
| `cover_letter.md` | `uv run scripts/ai_tells_lint.py <folder>/cover_letter.md` | Rewrite the sentence in place, then `render_cover_letter.py --job <folder>`. |
| `why_join.txt` | `uv run scripts/ai_tells_lint.py <folder>/why_join.txt` | Rewrite in place. |

**Hard hits must reach zero.** Soft hits are judgement: `stakeholders` in a bullet that really
is about stakeholders is fine; three `-ing` tails in one letter is not.

## The bank is not edited unattended

When a CV hit comes from the wording of a bank bullet or a summary and no sibling bullet avoids
it, the fix belongs in `profile/cv/master_cv.yaml` — which changes every future CV.

- **Interactive session:** propose the reworded bank line to the candidate, with the bullet id,
  and edit only after they agree. Then re-render the folders that use it.
- **Scheduled run (`claude -p`):** never edit `master_cv.yaml`. Keep the folder, note the hit
  and the bullet id in the run log under "Needs your attention", and move on.

## Beyond the linter

The linter finds words and shapes. After it is clean, read each file once more and ask only
"what here still sounds generated?" The usual survivors:

- the same opening in the letter and in `why_join.txt` — they are read side by side;
- enthusiasm the posting does not support ("thrilled", "excited to bring my passion");
- a claim about the company that is not in `job.md` (mission, product, culture) — unknown,
  checkable, and therefore out;
- an abstract sentence with no noun from the bank or the posting in it — cut it.

## Output

Per folder, one line: `<folder> | CV h/s | letter h/s | why_join h/s | changes made`. In a
scheduled run this goes into the run log; on demand, into the reply.
