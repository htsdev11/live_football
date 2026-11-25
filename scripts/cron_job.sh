#!/usr/bin/env bash
# Run one FotMob ingestion job from cron.
#
#   scripts/cron_job.sh <job> [extra manage.py arguments]
#
#   scripts/cron_job.sh sync_fotmob_live
#   scripts/cron_job.sh sync_fotmob_fixtures --past-days 1 --future-days 3
#
# - Finds the project from this script's own location, so cron needs no `cd`
#   and the same file works on any machine.
# - Uses venv/ (server) or .venv/ (local), whichever exists.
# - Takes a per-job lock: if the previous run of the same job is still going,
#   this run is skipped instead of piling up.
# - Appends timestamped output to logs/<job>.log.
# - Only runs the ingestion jobs listed below, never arbitrary manage.py
#   commands.

set -uo pipefail

ALLOWED_JOBS="sync_fotmob_live sync_fotmob_coverage sync_fotmob_fixtures daily_fixture_reconcile weekly_backfill"

JOB="${1:-}"
if [ -z "$JOB" ]; then
    echo "usage: $0 <job> [args...]   jobs: $ALLOWED_JOBS" >&2
    exit 2
fi
shift

case " $ALLOWED_JOBS " in
    *" $JOB "*) ;;
    *) echo "unknown job '$JOB'; allowed: $ALLOWED_JOBS" >&2; exit 2 ;;
esac

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -x "$APP/venv/bin/python" ]; then
    PY="$APP/venv/bin/python"
elif [ -x "$APP/.venv/bin/python" ]; then
    PY="$APP/.venv/bin/python"
else
    echo "no virtualenv found at $APP/venv or $APP/.venv" >&2
    exit 1
fi

LOG_DIR="$APP/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$JOB.log"
LOCK="/tmp/live_football_${JOB}.lock"

stamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }

# File descriptor 9 holds the lock for as long as this script runs.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(stamp)] $JOB skipped: previous run still in progress" >> "$LOG"
    exit 0
fi

cd "$APP" || exit 1

{
    echo "[$(stamp)] start $JOB $*"
    started=$(date +%s)
    "$PY" manage.py "$JOB" "$@"
    rc=$?
    echo "[$(stamp)] end $JOB rc=$rc ($(( $(date +%s) - started ))s)"
    echo
} >> "$LOG" 2>&1

exit $rc
