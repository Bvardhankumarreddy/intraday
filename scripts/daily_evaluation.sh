#!/bin/zsh
# Daily live paper-trading evaluation - started by launchd at 08:55 on weekdays.
# SIMULATED orders only: this never places real orders.
#
# - Holidays/weekends: the app checks the NSE calendar and exits by itself.
# - Keeps the Mac awake for the session (caffeinate).
# - If the run stops early (crash, provider outage abort), it is restarted;
#   restarts resume from the database without duplicating anything.
# - After the session, writes the cumulative evaluation report.
# Logs: data/logs/evaluation-YYYY-MM-DD.log

set -u
ROOT="/Applications/Projects/Intraday/nse-intraday-agent"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/evaluation-$(TZ=Asia/Kolkata date +%Y-%m-%d).log"
cd "$ROOT" || exit 1

ist_hhmm() { TZ=Asia/Kolkata date +%H%M; }

{
  echo "=== $(date) daily evaluation start ==="
  attempt=0
  while true; do
    attempt=$((attempt + 1))
    /usr/bin/caffeinate -i "$PY" -m app.evaluation run
    code=$?
    echo "=== run attempt $attempt exited with code $code at $(date) ==="
    if [ "$code" -eq 0 ]; then break; fi
    if [ "$code" -eq 2 ]; then
      echo "No active evaluation - start one with: python -m app.evaluation start --name ..."
      break
    fi
    if [ "$(ist_hhmm)" -ge 1535 ] || [ "$attempt" -ge 10 ]; then
      echo "Not restarting (after market close or $attempt attempts)."
      break
    fi
    sleep 60
  done
  "$PY" -m app.evaluation sync || true
  "$PY" -m app.evaluation cumulative || true
  echo "=== $(date) daily evaluation done ==="
} >> "$LOG" 2>&1
