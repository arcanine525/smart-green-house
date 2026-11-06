#!/usr/bin/env bash
# Reproducible local correctness and recovery suite. Never resets user data.
set -euo pipefail
cd "$(dirname "$0")/.."
run_id="$(date -u +%Y%m%dT%H%M%SZ)-${RANDOM}"
result_dir="$PWD/tmp/local-tests/$run_id"
mkdir -p "$result_dir"
printf 'Evidence: %s\n' "$result_dir"
printf 'UTC: %s\n' "$(date -u +%FT%TZ)" > "$result_dir/environment.txt"
printf 'HEAD: %s\n' "$(git rev-parse HEAD)" >> "$result_dir/environment.txt"
git status --short >> "$result_dir/environment.txt"
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}' >> "$result_dir/environment.txt"
docker compose version >> "$result_dir/environment.txt"
# Build copies source and tests into the images. No source mounts mask packaging bugs.
docker compose build job app 2>&1 | tee "$result_dir/build.log"
docker compose up -d --wait db 2>&1 | tee "$result_dir/db.log"
printf 'Running unit, contract, pipeline and recovery tests...\n'
docker compose run --rm --no-deps job python -m pytest tests/unit -q --tb=short 2>&1 | tee "$result_dir/unit.log"
printf 'Running usability recorder checks...\n'
docker compose run --rm --no-deps -v "$PWD/scripts:/workspace/scripts:ro" \
  job python -m unittest discover -s tests/tools -p 'test_record_usability.py' -v 2>&1 | tee "$result_dir/tools.log"
printf 'Running UI and export tests...\n'
docker compose --profile test run --rm --no-deps ui-test python -m pytest tests/ui -q --tb=short 2>&1 | tee "$result_dir/ui.log"
printf 'Running isolated sensor acceptance and real process restarts...\n'
./scripts/test_sensor_acceptance.sh 2>&1 | tee "$result_dir/sensor.log"
printf 'All local checks passed. Evidence: %s\n' "$result_dir"
