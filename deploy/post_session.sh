#!/bin/sh
# Post-session pipeline (Kubernetes CronJob "post-session", 16:00 IST, Mon-Fri).
# Each step is independent: one failing step is logged and the rest still run.
# Nothing here places orders or changes settings.
set -u
step() { echo "== $*"; "$@" || echo "!! step failed (exit $?): $*"; }

step python -m app.integrated report             # daily integrated report (both arms)
step python -m app.learning ingest               # Phase 13: copy decision records
step python -m app.learning outcomes             # outcomes whose windows have passed
step python -m app.learning review
step python -m app.learning analyse
step python -m app.monitoring run --full         # Phase 15: incl. performance drift
step python -m app.notify daily-summary          # WhatsApp summary (if configured)
step python -m app.ops backup                                       # baseline paper DB
step python -m app.ops backup --db /app/data/paper/ai_paper.db      # AI-assisted arm
step python -m app.ops prune --apply             # retention (never trading records)
exit 0
