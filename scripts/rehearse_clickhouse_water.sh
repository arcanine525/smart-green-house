#!/usr/bin/env bash
# Full locked Resources slice, existing production UI, isolated ClickHouse only.
set -euo pipefail
cd "$(dirname "$0")/.."
project="gh-ch-water-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
root="$PWD/tmp/clickhouse-water/$project"
mkdir -p "$root/state/coordinator"
chmod 777 "$root/state" "$root/state/coordinator"
free_kib=$(df -Pk "$root" | awk 'END {print $4}')
(( free_kib >= 4 * 1024 * 1024 )) || { echo 'Need 4 GiB rehearsal headroom'; exit 2; }
export GREENHOUSE_CH_APP_PORT="${GREENHOUSE_CH_WATER_APP_PORT:-18582}"
compose() { docker compose -f compose.clickhouse.yaml -p "$project" "$@"; }
for volume in ch_data ch_logs; do
  if docker volume inspect "${project}_${volume}" >/dev/null 2>&1; then exit 2; fi
done
printf '%s\n' "$project" > "$root/project.txt"
git rev-parse HEAD > "$root/source-head.txt"
compose up -d --no-build --pull never --wait clickhouse > "$root/start.log" 2>&1
[[ $(docker network inspect "${project}_backend" --format '{{.Internal}}') == true ]]
compose run --rm --no-deps --pull never -T --interactive=false \
  -v "$PWD/data/raw:/data/raw:ro" -v "$root/state:/candidate" ch-client \
  python -m greenhouse.clickhouse.water_run \
  --raw /data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68 \
  --output /candidate/output --coordinator /candidate/coordinator \
  --scope agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68:historical \
  --workflow-id water-agc-rehearsal --agc-shared-basis > "$root/pipeline.json" 2> "$root/pipeline.log"
compose --profile test run --rm --no-deps --pull never -T --interactive=false \
  -v "$PWD/data/raw:/data/raw:ro" ch-ui-test \
  python tests/clickhouse/check_agc_water.py > "$root/agc-check.json" 2> "$root/agc-check.log"
compose --profile app up -d --no-build --pull never ch-app ch-web > "$root/app.log" 2>&1
compose exec -T ch-app python -c 'import time,urllib.request
for attempt in range(60):
 try:
  value=urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health",timeout=2).read().decode(); assert value=="ok"; print(value); break
 except OSError: time.sleep(1)
else: raise AssertionError("UI health failed")' > "$root/app-health.txt"
# Retain the test database and outputs, stopped, for inspection/continuation.
compose exec -T ch-app python -c 'import socket,json; s=socket.socket(); s.settimeout(2); code=s.connect_ex(("1.1.1.1",443)); s.close(); print(json.dumps({"app_external_connect_ex":code})); assert code!=0' > "$root/app-egress.json"
compose stop ch-web ch-app clickhouse > "$root/stop.log" 2>&1
cat "$root/agc-check.json"
printf 'Retained rehearsal: %s\n' "$root"
