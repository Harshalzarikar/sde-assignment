#!/usr/bin/env bash
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
exec python -m controller.main "$SIM_URL"
