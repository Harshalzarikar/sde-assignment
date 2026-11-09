#!/usr/bin/env bash
# Starts the controller. Contract: ./run.sh <SIM_URL> runs until the scenario finishes.
# Needs Python 3.10+ with httpx (already in requirements.txt). State lives in state/controller.db,
# so killing and re-running this script resumes where it stopped.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
mkdir -p state
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  PY=python3
fi
exec "$PY" -m controller run --url "$SIM_URL" --db state/controller.db
