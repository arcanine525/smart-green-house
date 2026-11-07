#!/usr/bin/env bash
# Isolated synthetic offline smoke; never touches existing project volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
run_id="$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
project="gh-offline-$run_id"
evidence_dir="$PWD/tmp/offline-smoke/$run_id"
mkdir -p "$evidence_dir"
export COMPOSE_PROJECT_NAME="$project"
export COMPOSE_FILE="$PWD/compose.postgres.yaml:$evidence_dir/offline.yaml"
export SENSOR_TEST_NETWORK="${project}_backend"
# Override host app port; other services remain internal-only.
export GREENHOUSE_APP_PORT="${GREENHOUSE_OFFLINE_TEST_PORT:-18571}"
cat > "$evidence_dir/offline.yaml" <<'YAML'
networks:
  frontend:
    internal: true
YAML
for volume in pg_data outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints; do
  if docker volume inspect "${project}_${volume}" >/dev/null 2>&1; then
    printf 'Refusing existing volume %s_%s\n' "$project" "$volume" >&2
    exit 1
  fi
done
for network in "${project}_backend" "${project}_frontend"; do
  if docker network inspect "$network" >/dev/null 2>&1; then
    printf 'Refusing existing network %s\n' "$network" >&2
    exit 1
  fi
done
if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]; then
  printf 'Refusing existing project containers %s\n' "$project" >&2
  exit 1
fi
# Fail without pulling: setup/build has to happen before this offline smoke.
docker image inspect smart-greenhouse-ui:local smart-greenhouse-processing:local \
  'postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f' \
  'eclipse-mosquitto:2.0.22@sha256:09b62ff32c2e7b64d4b431b9a89236073289b68db273de2b089edeb9b0d92b5c' >/dev/null
cleanup() { docker compose --profile '*' down -v --remove-orphans; }
trap cleanup EXIT INT TERM
printf 'Offline synthetic evidence: %s\n' "$evidence_dir"
docker compose up -d --wait --no-build --pull never db app
for network in "${project}_backend" "${project}_frontend"; do
  [[ $(docker network inspect "$network" --format '{{.Internal}}') == true ]]
  printf '%s internal=true\n' "$network" | tee -a "$evidence_dir/networks.log"
done
docker compose exec -T app python -c "import urllib.request; print('app health:', urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health').read().decode())" | tee "$evidence_dir/app.log"
docker compose run --rm --no-deps --pull never job python -m pytest \
  tests/unit/test_pipeline.py::test_spark_atomic_profile_and_resume tests/unit/test_contracts.py \
  -q --tb=short 2>&1 | tee "$evidence_dir/batch.log"
docker compose --profile test run --rm --no-deps --pull never ui-test \
  python -m pytest tests/ui -q --tb=short 2>&1 | tee "$evidence_dir/ui.log"
./scripts/test_sensor_acceptance.sh 2>&1 | tee "$evidence_dir/sensor.log"
printf 'Offline synthetic smoke passed; not a full AGC rebuild. Evidence: %s\n' "$evidence_dir"
