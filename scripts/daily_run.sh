#!/bin/bash
# Entrypoint for the vacancy scan: by hand on a machine that is not always on (how this one
# runs since 2026-11-13), or on a timer via launchd (scripts/install_schedule.sh).
#
# Runs `claude -p` headlessly in the project directory. Tools are allow-listed explicitly
# rather than bypassing permissions: in headless mode anything outside the list is denied
# instead of prompting, which is what we want for an unattended run.
#
# Phases: 1 collect once for every track · 2 one agent per track, in parallel · 3 Telegram
# cursors, vacancy chat · 4 log review (morning run only) · profile/hooks/post_run.sh if present.

set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || exit 1

DATE="$(date +%Y-%m-%d)"
LOG="$PROJECT/logs/$DATE.run.log"
mkdir -p "$PROJECT/logs"

# launchd hands over a minimal PATH; the tools we need are not on it.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Secrets live outside the project (mode 700), and are loaded here rather than baked into
# the MCP registration so they never sit in a file inside the repo.
for env_file in "$HOME/.job_search"/*.env; do
  [ -f "$env_file" ] && set -a && . "$env_file" && set +a
done

# Profile values (tracks, which sources are on) — scripts/jobprofile.py.
jp() { uv run --quiet --with pyyaml python "$PROJECT/scripts/jobprofile.py" "$@" 2>/dev/null; }
enabled() { [ "$(jp get "sources.$1.enabled")" != "false" ]; }

{
  echo "=================================================================="
  echo "Job search daily run — $(date '+%Y-%m-%d %H:%M:%S %Z') — profile $(jp dir)"
  echo "=================================================================="
} >> "$LOG"

# mcp__claude-in-chrome is deliberately absent: a headless `claude -p` gets no browser tools
# whatever the allow-list says — the Chrome extension pairs with an interactive session and
# launchd does not have one. wantapply is read by the interactive `/wantapply` instead.
ALLOWED_TOOLS='Read,Write,Edit,Glob,Grep,WebFetch,Bash(uv run scripts/*),Bash(python3 scripts/*),Bash(mkdir:*),Bash(date:*),Bash(ls:*),Bash(cat:*),mcp__telegram-mcp,mcp__google-sheets'

timeout_guard() {
  # Bound the run. A stuck browser or MCP handshake must not leave a claude process
  # running until the next fire time.
  local pid=$1 log=$2
  local limit=2400          # 40 minutes, per track
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$limit" ]; then
      echo "!! run exceeded ${limit}s — terminating" >> "$log"
      kill -TERM "$pid" 2>/dev/null
      sleep 10
      kill -KILL "$pid" 2>/dev/null
      return 124
    fi
    sleep 15
    waited=$((waited + 15))
  done
  return 0
}

# One daily_run.sh at a time. The claude processes below are siblings and expected; a second
# daily_run.sh (a manual run overlapping the schedule) is not — it would collect into the same
# files the first is reading. A lock left by a dead run is taken over.
LOCK="$PROJECT/state/.daily_run.lock"
mkdir -p "$PROJECT/state"
if ! mkdir "$LOCK" 2>/dev/null; then
  if kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then
    echo "!! another daily_run.sh is running (pid $(cat "$LOCK/pid")) — exiting" >> "$LOG"
    exit 0
  fi
  echo "  stale lock from pid $(cat "$LOCK/pid" 2>/dev/null) taken over" >> "$LOG"
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

TRACKS=($(jp tracks))
[ "${#TRACKS[@]}" -eq 0 ] && TRACKS=(main)
DEFAULT_TRACK="${TRACKS[0]}"

# ---------------------------------------------------------------------------------------------
# Phase 1 — collect, once, for every track. The collectors run here rather than inside the
# agents: several agents each polling the boards and LinkedIn would multiply the requests and
# race on state/ats_boards.json and last_run.json. Sequential and deterministic; a failing
# collector is recorded, never fatal. A source with `enabled: false` is skipped.
# ---------------------------------------------------------------------------------------------
STATUS="$PROJECT/state/collect_run.json"
# The window reaches back to the previous collection, not a fixed lookback_hours. This machine
# is switched on now and then and the scan is started by hand, never on a timer (candidate,
# 2026-11-13): after three days off, the fixed 30h window lost two days of postings. The gap
# is measured from the previous run's start, plus 6h of overlap (dedup absorbs the repeats);
# under 30h the configured window applies unchanged, and it is capped at the two-week first-run
# lookback. A wide gap also raises LinkedIn's request budget, or the window is half a window.
# Telegram is left alone: its per-chat cursors already cover any gap.
WIN=(); LI_BUDGET=()
if [ -f "$STATUS" ]; then
  GAP_H="$(python3 -c 'import json,sys,math,datetime as d
s=json.load(open(sys.argv[1])).get("started_utc")
t=d.datetime.strptime(s,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=d.timezone.utc) if s else None
print(min(336, math.ceil((d.datetime.now(d.timezone.utc)-t).total_seconds()/3600)+6) if t else "")' "$STATUS" 2>/dev/null)"
  if [ -n "$GAP_H" ] && [ "$GAP_H" -gt 30 ]; then
    WIN=(--hours "$GAP_H")
    [ "$GAP_H" -gt 72 ] && LI_BUDGET=(--max-requests "$(jp get sources.linkedin.first_run_max_requests)")
    echo "  window: ${GAP_H}h, back to the previous collection" >> "$LOG"
  fi
fi
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ENTRIES=""
collect() {
  local name=$1; shift
  local t0=$SECONDS
  echo "--- collect $name: $*" >> "$LOG"
  "$@" >> "$LOG" 2>&1
  local rc=$?
  echo "--- collect $name: exit $rc in $((SECONDS - t0))s" >> "$LOG"
  ENTRIES="$ENTRIES${ENTRIES:+, }\"$name\": {\"rc\": $rc, \"seconds\": $((SECONDS - t0))}"
}
# Board discovery: the only collector that needs a model, because the search is WebSearch. Its
# own small claude -p with a narrow allow-list, before `boards` so a board found this morning is
# polled this morning. discover_boards.py --due makes it a no-op after the first run of the day.
if enabled board_discovery && uv run scripts/discover_boards.py --due >> "$LOG" 2>&1; then
  collect discovery claude -p "$(cat "$PROJECT/prompts/board_discovery.md")" \
    --allowedTools 'WebSearch,Read,Edit(state/_discovery_hits.json),Bash(uv run scripts/discover_boards.py:*)'
fi
enabled company_boards && collect boards uv run scripts/collect_boards.py --feeds --json state/collected.jsonl
enabled linkedin    && collect linkedin    uv run scripts/collect_linkedin.py "${WIN[@]+"${WIN[@]}"}" "${LI_BUDGET[@]+"${LI_BUDGET[@]}"}" --json state/linkedin.jsonl
# Further LinkedIn searches (a track's own `sources`) are kept apart from the first on purpose:
# back to back, the second started on an IP the first had just exhausted, and got a 429 on its
# first request. They run last.
enabled wellfound   && collect wellfound   uv run scripts/collect_wellfound.py "${WIN[@]+"${WIN[@]}"}" --json state/wellfound.jsonl
enabled builtin     && collect builtin     uv run scripts/collect_builtin.py "${WIN[@]+"${WIN[@]}"}" --json state/builtin.jsonl
if enabled euremotejobs; then
  collect euremotejobs uv run scripts/collect_euremotejobs.py "${WIN[@]+"${WIN[@]}"}" --json state/euremotejobs.jsonl
  # Its records name each employer's own ATS: those boards are polled from the next run on.
  collect euremote_boards uv run scripts/discover_boards.py --from state/euremotejobs.jsonl
fi
enabled himalayas   && collect himalayas   uv run scripts/collect_himalayas.py "${WIN[@]+"${WIN[@]}"}" --json state/himalayas.jsonl
TELEGRAM=0
if enabled telegram; then
  TELEGRAM=1
  collect telegram  uv run scripts/collect_telegram.py --json state/telegram.jsonl \
                      --cursors-out state/telegram_cursors.pending.json
fi
for t in "${TRACKS[@]}"; do
  for src in $(jp track "$t" sources); do
    enabled "$src" && collect "$src" uv run scripts/collect_linkedin.py --profile "$src" "${WIN[@]+"${WIN[@]}"}" "${LI_BUDGET[@]+"${LI_BUDGET[@]}"}" --json "state/$src.jsonl"
  done
done
printf '{"started_utc": "%s", "finished_utc": "%s", "collectors": {%s}}\n' \
  "$STARTED" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ENTRIES" > "$STATUS"

# Companies that rejected the candidate go onto profile/excluded_companies.yaml before the tracks
# start, so triage.py never lists their new postings (candidate, 2026-11-16). Never fatal: the
# list as it stands is used.
uv run scripts/rejected_companies.py >> "$LOG" 2>&1 || echo "  rejected_companies failed: exit $?" >> "$LOG"

# ---------------------------------------------------------------------------------------------
# Phase 2 — the tracks, in parallel. Each gets the same prompt with a RUN CONTEXT header naming
# its track; prompts/daily_scan.md "Tracks" says what each one owns.
# ---------------------------------------------------------------------------------------------
track_log() {
  local suffix; suffix="$(jp track "$1" log_suffix)"
  echo "$PROJECT/logs/$DATE$suffix.run.log"
}
run_track() {
  local track=$1
  local log; log="$(track_log "$track")"
  local others=""
  for o in "${TRACKS[@]}"; do [ "$o" != "$track" ] && others="$others${others:+, }$o"; done
  local siblings="No other track runs."
  [ -n "$others" ] && siblings="The other track(s) ($others) run in parallel right now as sibling claude -p processes, by design: never yield to them."
  # today=$DATE: a run started before midnight and still going after it must not name its logs
  # and folders by the wall clock, while this script and the review use the start date.
  local header="RUN CONTEXT (scripts/daily_run.sh, $(date '+%Y-%m-%d %H:%M %Z')):
today=$DATE — use this date for logs/<today>.md and vacancy folder names, not the clock.
track=$track. Collection already ran for every track — see state/collect_run.json; do not run
any collector. $siblings Do not commit Telegram cursors; daily_run.sh does that after all tracks.
---
"
  claude -p "$header$(cat "$PROJECT/prompts/daily_scan.md")" \
    --allowedTools "$ALLOWED_TOOLS" >> "$log" 2>&1 &
  TRACK_PID=$!
}

# Not $(run_track ...): a command substitution is a subshell, the claude process would be its
# child rather than this shell's, and `wait` below could never read its exit code.
PIDS=(); GUARDS=()
for t in "${TRACKS[@]}"; do
  run_track "$t"; PIDS+=("$TRACK_PID")
  timeout_guard "$TRACK_PID" "$(track_log "$t")" &
  GUARDS+=($!)
done
echo "  tracks started: ${TRACKS[*]} (pids ${PIDS[*]})" >> "$LOG"

RCS=(); ALL_OK=1; RC=0
for i in "${!TRACKS[@]}"; do
  wait "${GUARDS[$i]}"; grc=$?
  wait "${PIDS[$i]}" 2>/dev/null; trc=$?
  [ "$grc" -eq 124 ] && trc=124
  RCS+=("${TRACKS[$i]} exit $trc")
  if [ "$trc" -ne 0 ]; then ALL_OK=0; [ "$RC" -eq 0 ] && RC=$trc; fi
done

# ---------------------------------------------------------------------------------------------
# Phase 3 — Telegram cursors, only when every track finished. A track that failed must see the
# same messages again next run, so the pending file is kept and the next read starts from the
# old cursors.
# ---------------------------------------------------------------------------------------------
if [ "$TELEGRAM" -eq 1 ]; then
  if [ "$ALL_OK" -eq 1 ] && [ -f state/telegram_cursors.pending.json ]; then
    uv run scripts/collect_telegram.py --commit-from state/telegram_cursors.pending.json >> "$LOG" 2>&1
  else
    echo "  Telegram cursors NOT committed (${RCS[*]})" >> "$LOG"
  fi
fi

# ---------------------------------------------------------------------------------------------
# Phase 3b — the vacancy chat, when ~/.job_search/telegram_bot.env sets TELEGRAM_BOT_TOKEN. Reactions first, so
# a card reacted to since the last run is marked Applied before anything else; then a card for
# every folder the tracks just made. The bot writes only to the candidate's private chat with
# it. Never fatal, whatever the tracks' exit codes: folders that exist get their cards.
# ---------------------------------------------------------------------------------------------
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  uv run scripts/tg_notify.py sync >> "$LOG" 2>&1 || echo "  tg_notify sync failed: exit $?" >> "$LOG"
  uv run scripts/tg_notify.py post >> "$LOG" 2>&1 || echo "  tg_notify post failed: exit $?" >> "$LOG"
fi

{
  echo ""
  echo "Finished $(date '+%H:%M:%S'): ${RCS[*]}"
} >> "$LOG"

# ---------------------------------------------------------------------------------------------
# Phase 4 — log review, after the morning run only, and once per date (a manual run that
# crossed midnight must not review the day twice). Reads today's logs, fixes what
# prompts/log_review.md allows, writes logs/DATE.review.md. Its own allow-list: no Telegram, no
# WebFetch; scripts may be edited, this file may not. Its exit code never changes this run's.
# ---------------------------------------------------------------------------------------------
HOUR=$((10#$(date +%H)))
if [ "$HOUR" -ge 5 ] && [ "$HOUR" -lt 13 ] && [ ! -f "$PROJECT/logs/$DATE.review.md" ]; then
  REVIEW_LOG="$PROJECT/logs/$DATE.review.run.log"
  echo "  log review started (log logs/$DATE.review.run.log)" >> "$LOG"
  claude -p "RUN CONTEXT (scripts/daily_run.sh, $(date '+%Y-%m-%d %H:%M %Z')): today=$DATE.
---
$(cat "$PROJECT/prompts/log_review.md")" \
    --allowedTools 'Read,Glob,Grep,Edit(logs/*),Edit(scripts/*.py),Edit(state/*),Bash(uv run scripts/*),Bash(python3 -m py_compile:*),Bash(python3 scripts/*),Bash(cp:*),Bash(mkdir:*),Bash(ls:*),Bash(cat:*),Bash(date:*),mcp__google-sheets' \
    >> "$REVIEW_LOG" 2>&1 &
  REVIEW_PID=$!
  timeout_guard "$REVIEW_PID" "$REVIEW_LOG"
  wait "$REVIEW_PID" 2>/dev/null
  echo "  log review finished: exit $? at $(date '+%H:%M:%S')" >> "$LOG"
fi

# The candidate's own last step (a mirror of vacancies/, a notification ...). Never fails the run.
HOOK="$(jp dir)/hooks/post_run.sh"
if [ -f "$HOOK" ]; then
  PROJECT="$PROJECT" LOG="$LOG" bash "$HOOK" >> "$LOG" 2>&1 \
    || echo "  post_run hook failed (non-fatal): exit $?" >> "$LOG"
fi

# Keep 60 days of logs.
find "$PROJECT/logs" -name '*.log' -mtime +60 -delete 2>/dev/null

exit "$RC"
