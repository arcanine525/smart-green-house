#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Explicit optional PostgreSQL configuration; the default stack is ClickHouse.
export COMPOSE_FILE="$PWD/compose.postgres.yaml"
run_id="$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
source_project="gh-backup-test-$run_id"
restore_project="gh-restore-test-$run_id"
for project in "$source_project" "$restore_project"; do
  for volume in pg_data outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints; do
    if docker volume inspect "${project}_${volume}" >/dev/null 2>&1; then
      printf 'Refusing existing volume: %s_%s\n' "$project" "$volume" >&2
      exit 1
    fi
  done
  if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]; then
    printf 'Refusing existing project containers: %s\n' "$project" >&2
    exit 1
  fi
done
cleanup() {
  docker compose -p "$source_project" --profile '*' down -v --remove-orphans
  docker compose -p "$restore_project" --profile '*' down -v --remove-orphans
}
trap cleanup EXIT
archive_image='postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f'
backup_dir="$PWD/tmp/backup-smoke/$run_id"
mkdir -p "$backup_dir/volumes" "$backup_dir/source-raw/synthetic"
printf 'synthetic manifest\n' > "$backup_dir/source-raw/synthetic/manifest.json"
docker compose -p "$source_project" up -d --wait --pull never db
docker compose -p "$source_project" exec -T db sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE TABLE demo_restore (id integer PRIMARY KEY, note text); INSERT INTO demo_restore VALUES (1, '\''synthetic'\'');"'
docker compose -p "$source_project" exec -T db sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/database.dump"
cp -a "$backup_dir/source-raw" "$backup_dir/raw"
volume=simulator_outbox
docker volume create --label "com.docker.compose.project=$source_project" --label "com.docker.compose.volume=$volume" "${source_project}_${volume}" >/dev/null
docker run --rm --network none -v "${source_project}_${volume}:/target" "$archive_image" sh -c "printf 'synthetic spool\\n' > /target/outbox-marker"
docker run --rm --network none -v "${source_project}_${volume}:/source:ro" -v "$backup_dir/volumes:/backup" "$archive_image" tar -C /source -cf "/backup/${volume}.tar" .
docker compose -p "$restore_project" up -d --wait --pull never db
docker compose -p "$restore_project" exec -T db sh -c \
  'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error' < "$backup_dir/database.dump"
docker volume create --label "com.docker.compose.project=$restore_project" --label "com.docker.compose.volume=$volume" "${restore_project}_${volume}" >/dev/null
docker run --rm --network none -v "${restore_project}_${volume}:/target" -v "$backup_dir/volumes:/backup:ro" "$archive_image" tar -C /target -xf "/backup/${volume}.tar"
docker compose -p "$restore_project" exec -T db sh -c \
  'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT id || '\''|'\'' || note FROM demo_restore"' | grep -Fx '1|synthetic'
docker run --rm --network none -v "${restore_project}_${volume}:/target:ro" "$archive_image" cat /target/outbox-marker | grep -Fx 'synthetic spool'
cmp "$backup_dir/source-raw/synthetic/manifest.json" "$backup_dir/raw/synthetic/manifest.json"
printf 'Synthetic logical DB, raw and volume backup/restore passed. Evidence: %s\n' "$backup_dir"
