#!/usr/bin/env bash
# Clone a prepared AGC publication for browser demos; never write to the source.
set -euo pipefail
cd "$(dirname "$0")/.."
source_db="${1:?Pass the prepared AGC source database name}"
[[ "$source_db" =~ ^[a-zA-Z0-9_]+$ ]] || exit 2
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
demo_db="test_walkthrough_${run_id//-/_}"
container="greenhouse-walkthrough-$run_id"
port="${WALKTHROUGH_PORT:-18572}"
result_dir="$PWD/tmp/walkthrough/$run_id"
mkdir -p "$result_dir"
docker compose build app
# PostgreSQL refuses a busy template; do not disconnect other users.
docker compose exec -T db sh -c 'exec psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1' \
  <<< "CREATE DATABASE \"$demo_db\" TEMPLATE \"$source_db\";"
printf '%s\n' "$demo_db" > "$result_dir/database.txt"
GREENHOUSE_APP_PORT="$port" GREENHOUSE_APP_CPUS=1.5 GREENHOUSE_APP_MEMORY=1536m docker compose run -d --no-deps --service-ports \
  --name "$container" -e PGDATABASE="$demo_db" app > "$result_dir/container-id.txt"
git rev-parse HEAD > "$result_dir/source-commit.txt"
git diff --binary > "$result_dir/source.patch"
docker inspect --format '{{.Image}}' "$container" > "$result_dir/image.txt"
docker exec -i "$container" python - < tools/walkthrough/prepare_context.py > "$result_dir/context.json"
for attempt in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$port/_stcore/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$port/_stcore/health" > "$result_dir/health.txt"
printf 'Demo: http://127.0.0.1:%s\nEvidence: %s\nContainer: %s\nDatabase: %s\n' \
  "$port" "$result_dir" "$container" "$demo_db"
