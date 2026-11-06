#!/usr/bin/env bash
# Small, isolated SQLite WAL + Spark checkpoint named-volume restore rehearsal.
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
archive_image='postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f'
volumes=(simulator_outbox collector_spool raw_ready spark_checkpoints)
paths=(outbox spool ready checkpoint)
source_mounts=()
restore_mounts=()
for i in "${!volumes[@]}"; do
  volume="${volumes[$i]}"
  path="${paths[$i]}"
  for project in "$source_project" "$restore_project"; do
    docker volume create --label "com.docker.compose.project=$project" \
      --label "com.docker.compose.volume=$volume" "${project}_${volume}" >/dev/null
  done
  docker run --rm --network none -v "${source_project}_${volume}:/target" "$archive_image" \
    chown 10001:10001 /target
  source_mounts+=(-v "${source_project}_${volume}:/state/$path")
  restore_mounts+=(-v "${restore_project}_${volume}:/state/$path")
done
for project in "$source_project" "$restore_project"; do
  docker volume create --label "com.docker.compose.project=$project" \
    --label 'com.docker.compose.volume=broker_state' "${project}_broker_state" >/dev/null
done
printf 'Synthetic backup evidence: %s\n' "$backup_dir"
docker compose -p "$source_project" up -d --wait --pull never db
docker compose -p "$source_project" --profile sensor up -d --no-deps --wait --pull never broker
docker compose -p "$source_project" exec -T broker \
  mosquitto_pub -h 127.0.0.1 -t greenhouse/restore-test -m retained-synthetic -q 1 -r
GREENHOUSE_JOB_CPUS=1.0 GREENHOUSE_JOB_MEMORY=1536m docker compose -p "$source_project" run --rm --no-deps --pull never \
  -e GREENHOUSE_RAW_DIR=/state/raw -e PYTHONPATH=/workspace/tests \
  -v "$source_dir:/state" "${source_mounts[@]}" -v "$PWD/tests/backup/sensor_backup_phase.py:/tmp/phase.py:ro" \
  job python /tmp/phase.py seed
# No seed process or Spark writer remains. Archive both SQLite WAL/SHM sets,
# source/manifest and the actual Spark checkpoint together.
docker compose -p "$source_project" stop broker
docker compose -p "$source_project" exec -T db sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/database.dump"
tar -C "$source_dir" -cf "$backup_dir/sensor-state.tar" .
tar -C "$restore_dir" -xf "$backup_dir/sensor-state.tar"
mkdir -p "$backup_dir/volumes"
for volume in "${volumes[@]}"; do
  docker run --rm --network none -v "${source_project}_${volume}:/source:ro" \
    -v "$backup_dir/volumes:/backup" "$archive_image" tar -C /source -cf "/backup/$volume.tar" .
  docker run --rm --network none -v "${restore_project}_${volume}:/target" \
    -v "$backup_dir/volumes:/backup:ro" "$archive_image" tar -C /target -xf "/backup/$volume.tar"
done
docker run --rm --network none -v "${source_project}_broker_state:/source:ro" \
  -v "$backup_dir/volumes:/backup" "$archive_image" tar -C /source -cf /backup/broker_state.tar .
docker run --rm --network none -v "${restore_project}_broker_state:/target" \
  -v "$backup_dir/volumes:/backup:ro" "$archive_image" tar -C /target -xf /backup/broker_state.tar
docker compose -p "$restore_project" up -d --wait --pull never db
docker compose -p "$restore_project" exec -T db sh -c \
  'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error' < "$backup_dir/database.dump"
docker compose -p "$restore_project" --profile sensor up -d --no-deps --wait --pull never broker
docker compose -p "$restore_project" exec -T broker \
  mosquitto_sub -h 127.0.0.1 -t greenhouse/restore-test -C 1 -W 5 \
  | tee "$backup_dir/broker-restore.txt" | grep -Fx 'retained-synthetic'
GREENHOUSE_JOB_CPUS=1.0 GREENHOUSE_JOB_MEMORY=1536m docker compose -p "$restore_project" run --rm --no-deps --pull never \
  -e GREENHOUSE_RAW_DIR=/state/raw -e PYTHONPATH=/workspace/tests \
  -v "$restore_dir:/state" "${restore_mounts[@]}" -v "$PWD/tests/backup/sensor_backup_phase.py:/tmp/phase.py:ro" \
  job python /tmp/phase.py restore
printf 'Synthetic sensor backup/restore passed. Evidence: %s\n' "$backup_dir"
