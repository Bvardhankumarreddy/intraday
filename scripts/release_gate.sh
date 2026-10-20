#!/bin/sh
# Release / regression gate. Exit 0 only when RELEASABLE.
# Usage: scripts/release_gate.sh [--skip-tests] [--json]
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m app.ops gate "$@"
