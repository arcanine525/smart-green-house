#!/usr/bin/env bash
# Run from a prepared checkout. No pull/build or production volume access.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${REPRO_ROOT:?Set an absolute, new rehearsal evidence directory}"
: "${REPRO_PROCESSING_IMAGE:?Prepared processing image}"
: "${REPRO_UI_IMAGE:?Prepared UI image}"
: "${REPRO_SENSOR_IMAGE:?Prepared sensor image}"
[[ "$REPRO_ROOT" == /* ]] || { echo 'REPRO_ROOT must be absolute'; exit 2; }
if [[ -n $(git status --porcelain -- src tests docker pyproject.toml compose.yaml 'requirements*.lock' .streamlit) ]]; then
  echo 'Use a checkout with clean application/build sources; commit or isolate changes first' >&2
  exit 2
fi
mkdir -p "$REPRO_ROOT/evidence"
free_kib=$(df -Pk "$REPRO_ROOT" | awk 'END {print $4}')
if (( free_kib < 16 * 1024 * 1024 )); then
  echo 'Need at least 16 GiB host headroom after image preparation for this bounded rehearsal' >&2
  exit 2
fi
for image in "$REPRO_PROCESSING_IMAGE" "$REPRO_UI_IMAGE" "$REPRO_SENSOR_IMAGE"; do
  docker image inspect "$image" >/dev/null
done
project="gh-agc-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
for volume in pg_data outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints; do
  if docker volume inspect "${project}_${volume}" >/dev/null 2>&1; then exit 2; fi
done
[[ ! -e "$REPRO_ROOT/source-project.txt" ]] || { echo 'Refusing existing rehearsal'; exit 2; }
printf '%s\n' "$project" > "$REPRO_ROOT/source-project.txt"
cat > "$REPRO_ROOT/runtime.yaml" <<YAML
services:
  job:
    image: $REPRO_PROCESSING_IMAGE
  app:
    image: $REPRO_UI_IMAGE
  simulator:
    image: $REPRO_SENSOR_IMAGE
  collector:
    image: $REPRO_SENSOR_IMAGE
  stream-worker:
    image: $REPRO_PROCESSING_IMAGE
  test-runner:
    image: $REPRO_PROCESSING_IMAGE
  ui-test:
    image: $REPRO_UI_IMAGE
  snapshot:
    image: $REPRO_PROCESSING_IMAGE
networks:
  frontend:
    internal: true
YAML
export COMPOSE_FILE="$PWD/compose.yaml:$REPRO_ROOT/runtime.yaml"
export COMPOSE_PROJECT_NAME="$project"
export GREENHOUSE_APP_PORT="${REPRO_APP_PORT:-18573}"
export GREENHOUSE_UI_MEMORY=1536m
export GREENHOUSE_APP_MEMORY=1536m
export GREENHOUSE_UI_CPUS=1
export GREENHOUSE_APP_CPUS=1
# Capture provenance without dumping .env or resolved Compose credentials.
git rev-parse HEAD > "$REPRO_ROOT/evidence/runtime-head.txt"
cp "$REPRO_ROOT/evidence/runtime-head.txt" "$REPRO_ROOT/evidence/source-head.txt"
docker image inspect --format '{{.Id}}' "$REPRO_PROCESSING_IMAGE" "$REPRO_UI_IMAGE" "$REPRO_SENSOR_IMAGE" > "$REPRO_ROOT/evidence/runtime-images.txt"
docker compose up -d --wait --no-build --pull never db
for network in "${project}_backend"; do
  [[ $(docker network inspect "$network" --format '{{.Internal}}') == true ]]
  printf '%s internal=true\n' "$network" >> "$REPRO_ROOT/evidence/network.txt"
done
docker compose run --rm --no-deps --pull never -T --interactive=false job python -c 'import socket; s=socket.socket(); s.settimeout(2); code=s.connect_ex(("1.1.1.1",443)); print("external_ip_connect_ex",code); assert code != 0' > "$REPRO_ROOT/evidence/egress.txt"
docker compose --profile tools run --rm --no-deps --pull never -T --interactive=false snapshot > "$REPRO_ROOT/evidence/snapshot.log" 2>&1
docker compose run --rm --no-deps --pull never -T --interactive=false job python -m greenhouse migrate > "$REPRO_ROOT/evidence/migrate.log" 2>&1
docker compose run --rm --no-deps --pull never -T --interactive=false job python -m greenhouse run-pipeline --workflow-id full-offline-rehearsal > "$REPRO_ROOT/evidence/pipeline.log" 2>&1
docker compose run --rm --no-deps --pull never -T --interactive=false job python -m greenhouse register-agc-tariff --apply > "$REPRO_ROOT/evidence/tariff.log" 2>&1
docker compose up -d --wait --no-build --pull never app
[[ $(docker network inspect "${project}_frontend" --format '{{.Internal}}') == true ]]
printf '%s internal=true\n' "${project}_frontend" >> "$REPRO_ROOT/evidence/network.txt"
docker compose exec -T app python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health").read().decode())' > "$REPRO_ROOT/evidence/app-health.txt"
printf 'Batch and app ready; project=%s evidence=%s\n' "$project" "$REPRO_ROOT"
