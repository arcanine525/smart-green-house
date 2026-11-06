#!/usr/bin/env bash
# Small, isolated real SQLite WAL + Spark checkpoint backup/restore rehearsal.
set -euo pipefail
cd "$(dirname "$0")/.."
run_id="$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
source_project="gh-sensor-backup-$run_id"
restore_project="gh-sensor-restore-$run_id"
for project in "$source_project" "$restore_project"; do
  for volume in pg_data outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints; do
    if docker volume inspect "${project}_${volume}" >/dev/null 2>&1; then
      printf 'Refusing existing volume: %s_%s\n' "$project" "$volume" >&2; exit 1
    fi
  done
  for network in "${project}_backend" "${project}_frontend"; do
    if docker network inspect "$network" >/dev/null 2>&1; then
      printf 'Refusing existing network: %s\n' "$network" >&2; exit 1
    fi
  done
  if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]; then
    printf 'Refusing existing project containers: %s\n' "$project" >&2; exit 1
  fi
done
cleanup() {
  docker compose -p "$source_project" --profile '*' down -v --remove-orphans
  docker compose -p "$restore_project" --profile '*' down -v --remove-orphans
}
trap cleanup EXIT
backup_dir="$PWD/tmp/sensor-backup-restore/$run_id"
source_dir="$backup_dir/source"
restore_dir="$backup_dir/restored"
mkdir -p "$source_dir" "$restore_dir"
chmod 777 "$source_dir" "$restore_dir"
printf 'Synthetic backup evidence: %s\n' "$backup_dir"
docker compose -p "$source_project" up -d --wait --pull never db
GREENHOUSE_JOB_CPUS=1.0 GREENHOUSE_JOB_MEMORY=1536m docker compose -p "$source_project" run --rm --no-deps --pull never \
  -e GREENHOUSE_RAW_DIR=/state/raw -e PYTHONPATH=/workspace/tests \
  -v "$source_dir:/state" -v "$PWD/tests/backup/sensor_backup_phase.py:/tmp/phase.py:ro" \
  job python /tmp/phase.py seed
# No seed process or Spark writer remains. Archive both SQLite WAL/SHM sets,
# source/manifest and the actual Spark checkpoint together.
for path in outbox/outbox.db-wal outbox/outbox.db-shm spool/spool.db-wal spool/spool.db-shm checkpoint/.greenhouse-stream-identity.json; do
  [[ -s "$source_dir/$path" ]] || { printf 'Missing state: %s\n' "$path" >&2; exit 1; }
done
docker compose -p "$source_project" exec -T db sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/database.dump"
tar -C "$source_dir" -cf "$backup_dir/sensor-state.tar" .
tar -C "$restore_dir" -xf "$backup_dir/sensor-state.tar"
docker compose -p "$restore_project" up -d --wait --pull never db
docker compose -p "$restore_project" exec -T db sh -c \
  'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error' < "$backup_dir/database.dump"
GREENHOUSE_JOB_CPUS=1.0 GREENHOUSE_JOB_MEMORY=1536m docker compose -p "$restore_project" run --rm --no-deps --pull never \
  -e GREENHOUSE_RAW_DIR=/state/raw -e PYTHONPATH=/workspace/tests \
  -v "$restore_dir:/state" -v "$PWD/tests/backup/sensor_backup_phase.py:/tmp/phase.py:ro" \
  job python /tmp/phase.py restore
printf 'Synthetic sensor backup/restore passed. Evidence: %s\n' "$backup_dir"
