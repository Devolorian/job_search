#!/bin/bash
# Install the daily scan as a macOS LaunchAgent.
#
#   scripts/install_schedule.sh                 # 09:20 and 17:20
#   scripts/install_schedule.sh 09:20 13:00 17:20
#   scripts/install_schedule.sh --uninstall
#
# Writes ~/Library/LaunchAgents/com.jobsearch.daily.plist pointing at this checkout's
# scripts/daily_run.sh, and loads it. Re-running replaces it.
#
# Keep the project OUTSIDE ~/Documents, ~/Desktop and ~/Downloads: macOS TCC protects those, and
# a LaunchAgent cannot enter them (the job exits 126). Granting Full Disk Access to /bin/bash
# does not help, because TCC attributes a bare system binary to the responsible process. The
# home directory root is not protected, so ~/job_search needs no permissions at all.
#
# On Linux, use cron instead:  20 9,17 * * *  /path/to/job_search/scripts/daily_run.sh

set -euo pipefail

LABEL="${JOB_SEARCH_LAUNCHD_LABEL:-com.jobsearch.daily}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGDIR="$HOME/.job_search"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $PLIST"
  exit 0
fi

case "$PROJECT" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo "!! $PROJECT is inside a TCC-protected folder; launchd cannot run it. Move it to ~/job_search." >&2
    exit 1 ;;
esac

TIMES=("$@")
[ "${#TIMES[@]}" -eq 0 ] && TIMES=(09:20 17:20)

INTERVALS=""
for t in "${TIMES[@]}"; do
  h="${t%%:*}"; m="${t##*:}"
  INTERVALS="$INTERVALS        <dict><key>Hour</key><integer>$((10#$h))</integer><key>Minute</key><integer>$((10#$m))</integer></dict>
"
done

mkdir -p "$LOGDIR" "$(dirname "$PLIST")"
chmod 700 "$LOGDIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>$PROJECT/scripts/daily_run.sh</string>
    </array>
    <!-- If the Mac is asleep at a fire time, the job runs when it next wakes. -->
    <key>StartCalendarInterval</key>
    <array>
$INTERVALS    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$LOGDIR/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGDIR/launchd.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $PLIST — runs at ${TIMES[*]}"
