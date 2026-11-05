# job_search

A job-search pipeline run by [Claude Code](https://claude.com/claude-code). Several times a day it
collects vacancies from company career boards, LinkedIn's public guest endpoint, remote-job
boards and Telegram channels. It filters them against your profile and builds a folder for each
match, containing:

- an ATS-safe, one-page CV selected from **your own** fact bank;
- a cover letter;
- a "why do you want to join" answer.

It then logs every vacancy in a Google Sheets tracker, which it updates from employers' replies
in your Gmail.

**It never applies, never sends and never invents.** You read the folder and apply by hand.
Every line of every CV traces back to `profile/cv/master_cv.yaml`, and a linter checks that it
does.

## Quick start

Requirements:
- [uv](https://docs.astral.sh/uv/);
- [Claude Code](https://claude.com/claude-code);
- Google Chrome or Chromium, used for PDF rendering (or set `CHROME_PATH`);
- macOS for the bundled scheduler. On Linux, use cron.

```bash
git clone <this repo> ~/job_search && cd ~/job_search
claude                                  # then, inside Claude Code:
/setup-profile                          # after dropping your CV + LinkedIn PDF into profile/inbox/
```

Without a `profile/`, everything runs on `profile.example/`, a fictional Product Designer in
Berlin. That lets you try the pipeline first:

```bash
uv run scripts/render_cv.py --generic -o /tmp/cv.pdf     # the example CV
uv run scripts/collect_himalayas.py --max-requests 2     # a dry collector run (writes nothing)
/job-scan --dry-run                                      # the full scan, no Sheets writes
```

## Your profile

Everything about you lives in `profile/`. It is gitignored and never leaves your machine.

| File | What it is |
|---|---|
| `inbox/` | your CV (PDF/DOCX) and LinkedIn → More → Save to PDF, read by `/setup-profile` |
| `cv/master_cv.yaml` | contact, summaries, skills, every bullet you can defend in an interview |
| `search_profile.yaml` | roles and titles, geography, languages, scoring, tracks |
| `routing.yaml` | which roles are searched on which source |
| `sources.yaml` | search keywords, lanes, Telegram folder, spreadsheet id (merged over `config/sources.yaml`) |
| `CANDIDATE.md` | your rules and decisions, and the language Claude replies in |
| `scan_rules.md` | your extra gates: contract type, language, how "Send today" is ordered |
| `cv/story_bank.yaml`, `cv/form_answers.yaml` | interview stories; standing answers to form questions |
| `keyword_bank.yaml` | optional vocabulary for your profession (default: data roles) |
| `hooks/post_run.sh` | optional, runs after every scheduled run |

`JOB_SEARCH_PROFILE=/path/to/profile` uses a profile stored somewhere else.

## Connections

Secrets go in `~/.job_search/` (mode 700), never in the repo.

- **Google Sheets (tracker).** Create a service account and save its JSON key outside the repo.
  Put `SERVICE_ACCOUNT_PATH=/path/to/key.json` in `~/.job_search/sheets.env`. Create an empty
  spreadsheet called "Job Search Tracker", share it with the service account's email, then run
  `uv run scripts/setup_sheets.py`. It creates the tabs and formulas and writes the id into
  `profile/sources.yaml`.
- **Gmail (employer replies).** Create an OAuth client of type Desktop app and save it as
  `~/.job_search/gmail_client.json`. Then run `uv run scripts/collect_mail.py --auth` once. The
  token is `gmail.readonly`; the script refuses a token with any other scope.
- **Telegram (optional).** Create an API id and hash at my.telegram.org and a Telethon session
  string. Put them in `~/.job_search/telegram.env` as `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and
  `TELEGRAM_SESSION_STRING`. Put the job channels in a Telegram folder and name that folder in
  `profile/sources.yaml` (`telegram.folder`, `enabled: true`). For the MCP server, set
  `TELEGRAM_MCP_DIR` to a checkout of chigwell/telegram-mcp. It runs read-only.
- **Vacancy chat (optional).** A bot of your own posts a card for each new folder into your
  private chat with it. React to a card to mark the application as sent. Set
  `TELEGRAM_BOT_TOKEN` in `~/.job_search/telegram_bot.env`, then run
  `uv run scripts/tg_notify.py setup`.
- **wantapply (optional).** Run `/wantapply` interactively, with the Claude browser extension
  connected.

## Schedule

```bash
scripts/install_schedule.sh 09:20 17:20      # macOS LaunchAgent; --uninstall to remove
```

Keep the checkout outside `~/Documents`, `~/Desktop` and `~/Downloads`, because launchd cannot
enter them. Each run does the following, in order:

1. Collects once.
2. Runs one Claude agent per track in parallel, following `prompts/daily_scan.md`.
3. Commits the Telegram cursors.
4. Posts the vacancy-chat cards.
5. After the morning run only, reviews the day's logs (`prompts/log_review.md`).

Logs go to `logs/` and vacancy folders to `vacancies/`.

## How it decides

- **Title gates**: role families, exclusions, a description backstop, and per-source routing.
- **Geography**: remote worldwide passes. Remote with no country named anywhere passes too; a
  posting is dropped only when it states a narrower scope. Onsite and hybrid pass only in your
  home city or country.
- **Language**: English postings pass. Others are dropped, unless your profile tags a language
  for review.
- **Score**: weights in `search_profile.yaml`. Above `viable` the posting gets a folder, CV,
  letter and tracker row; above `strong` it is shortlisted.

The details, and the reasons behind them, are in `CLAUDE.md`. Every collector's docstring
records what was measured about its source.

## Skills (interactive)

| Skill | What it does |
|---|---|
| `/setup-profile` | builds `profile/` from your documents |
| `/job-scan` | runs the daily scan on demand |
| `/interview-prep`, `/interview-debrief` | prepare for an interview, and record what was learned |
| `/follow-up` | drafts follow-ups for silent applications; you send them |
| `/funnel-review` | shows what converts and proposes config changes |
| `/form-answers` | drafts answers to an application form's extra questions |
| `/humanize-cv`, `deslop` | removes generated-text tells, using bank facts only |

## Licence

MIT, see `LICENSE`.
