#!/usr/bin/env bash
# Read-only structural gate; --pre-video explicitly leaves the overall video pending.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/submission_package.py check "$@"
