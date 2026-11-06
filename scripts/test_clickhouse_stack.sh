#!/usr/bin/env bash
# Isolated smoke only; builds/pulls belong to the explicit preparation phase.
set -euo pipefail
cd "$(dirname "$0")/.."
project="gh-ch-smoke-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
root="$PWD/tmp/clickhouse-smoke/$project"
mkdir -p "$root"
compose() { docker compose -p "$project" -f compose.clickhouse.yaml "$@"; }
for volume in ch_data ch_logs; do
  if docker volume inspect "${project}_${volume}" >/dev/null 2>&1; then exit 2; fi
done
export GREENHOUSE_CH_HTTP_PORT="${GREENHOUSE_CH_SMOKE_PORT:-18124}"
compose up -d --wait --no-build --pull never clickhouse > "$root/start.log" 2>&1
for network in backend frontend; do
  [[ $(docker network inspect "${project}_${network}" --format '{{.Internal}}') == true ]]
done
compose run --rm --no-deps --pull never -T --interactive=false ch-client \
  python -m unittest discover -s tests/clickhouse -v > "$root/tests.log" 2>&1
compose run --rm --no-deps --pull never -T --interactive=false ch-client \
  python -m greenhouse.clickhouse.client smoke > "$root/before.json" 2> "$root/before.log"
compose restart clickhouse > "$root/restart.log" 2>&1
compose up -d --wait --no-build --pull never clickhouse >> "$root/restart.log" 2>&1
compose run --rm --no-deps --pull never -T --interactive=false ch-client \
  python -c '
import json, platform, socket
from decimal import Decimal
from greenhouse.clickhouse.client import Client, ClickHouseSettings
c=Client(ClickHouseSettings.from_env())
rows=c.query("SELECT id, value FROM connection_smoke")
assert len(rows)==1 and rows[0]["id"]==1 and isinstance(rows[0]["value"],str) and Decimal(rows[0]["value"])==Decimal("9.36"),rows
s=socket.socket(); s.settimeout(2)
code=s.connect_ex(("1.1.1.1",443)); s.close(); assert code!=0
print(json.dumps({"status":"passed","version":c.query("SELECT version() AS version")[0]["version"],"architecture":platform.machine(),"python":platform.python_version(),"decimal_exact":True,"persisted_rows_after_restart":len(rows),"container_egress_connect_ex":code,"both_networks_internal":True}))
' > "$root/result.json" 2> "$root/after.log"
# Only this invocation owns these volumes. On failure preserve state for inspection.
compose down -v --remove-orphans > "$root/cleanup.log" 2>&1
cat "$root/result.json"
printf 'Evidence: %s\n' "$root"
