#!/usr/bin/env bash
# Installs (or updates) a crontab entry that runs the pipeline once a day.
# Usage: ./scripts/setup_cron.sh [HOUR] [MINUTE]
#   e.g. ./scripts/setup_cron.sh 6 30   -> runs daily at 06:30
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOUR="${1:-6}"
MINUTE="${2:-30}"
RUN_SCRIPT="$SCRIPT_DIR/scripts/daily_run.sh"
MARKER="# ti-pipeline-daily"

chmod +x "$RUN_SCRIPT"

CRON_LINE="$MINUTE $HOUR * * * $RUN_SCRIPT $MARKER"

( crontab -l 2>/dev/null | grep -v "$MARKER" ; echo "$CRON_LINE" ) | crontab -

echo "Installed cron job: runs daily at ${HOUR}:$(printf '%02d' "$MINUTE")"
echo "Current crontab:"
crontab -l
