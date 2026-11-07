#!/usr/bin/env bash
# Continue rehearse_agc_batch.sh. Back up only its isolated project, then restore
# into a second new project. Retain archives/evidence; never touch user projects.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${REPRO_ROOT:?Set the evidence directory from rehearse_agc_batch.sh}"
source_project=$(cat "$REPRO_ROOT/source-project.txt")
[[ "$source_project" == gh-agc-* ]] || exit 2
restore_project="${source_project}-restored"
[[ ! -e "$REPRO_ROOT/restore-project.txt" ]] || { echo 'Restore already attempted; inspect retained evidence first'; exit 2; }
for volume in pg_data outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints; do
  if docker volume inspect "${restore_project}_${volume}" >/dev/null 2>&1; then exit 2; fi
done
printf '%s\n' "$restore_project" > "$REPRO_ROOT/restore-project.txt"
export COMPOSE_FILE="$PWD/compose.postgres.yaml:$REPRO_ROOT/runtime.yaml"
export GREENHOUSE_APP_PORT="${REPRO_APP_PORT:-18573}"
export GREENHOUSE_UI_MEMORY=1536m GREENHOUSE_APP_MEMORY=1536m
export GREENHOUSE_UI_CPUS=1 GREENHOUSE_APP_CPUS=1
archive_image='postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f'
backup="$REPRO_ROOT/backup"
evidence="$REPRO_ROOT/evidence"
mkdir -p "$backup/volumes" "$REPRO_ROOT/state" "$REPRO_ROOT/restored/state" "$REPRO_ROOT/restored/raw" "$REPRO_ROOT/restored/import"
chmod 777 "$REPRO_ROOT/state" "$REPRO_ROOT/restored/state" "$evidence"
mkdir -p "$evidence/harness"
cp scripts/rehearse_agc_batch.sh scripts/rehearse_agc_restore.sh "$evidence/harness/"
cp -R scripts/repro "$evidence/harness/"
compose() { docker compose -p "$1" "${@:2}"; }
job() { local project="$1"; shift; compose "$project" run --rm --no-deps --pull never -T --interactive=false -v "$PWD/scripts/repro:/repro:ro" -v "$evidence:/evidence" job "$@"; }
ui_check() {
  local project="$1" phase="$2"
  compose "$project" --profile test run --rm --no-deps --pull never -T --interactive=false \
    -e PGOPTIONS='-c default_transaction_read_only=on' \
    -e GREENHOUSE_RAW_DIR=/data/raw -e GREENHOUSE_WALKTHROUGH_PUBLICATION="$publication" \
    -e GREENHOUSE_WALKTHROUGH_OUTPUT="/evidence/ui-$phase.json" \
    -v "$PWD/scripts/repro:/repro:ro" -v "$evidence:/evidence" \
    -v "${GREENHOUSE_RAW_PATH:-$PWD/data/raw}:/data/raw:ro" \
    ui-test python /repro/check_ui.py > "$evidence/ui-$phase.log" 2>&1
}
volume_inventory() {
  local project="$1" volume="$2" phase="$3"
  compose "$project" run --rm --no-deps --pull never -T --interactive=false --user 0 \
    -v "$PWD/scripts/repro:/repro:ro" -v "${project}_${volume}:/inventory:ro" \
    job python /repro/state.py files /inventory > "$evidence/$phase-$volume.json"
}
volumes=(outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints)
sensor_volumes=(simulator_outbox collector_spool raw_ready spark_checkpoints)
paths=(outbox spool ready checkpoint)
source_mounts=()
restore_mounts=()
for i in "${!sensor_volumes[@]}"; do
  volume="${sensor_volumes[$i]}"
  path="${paths[$i]}"
  docker volume create --label "com.docker.compose.project=$source_project" --label "com.docker.compose.volume=$volume" "${source_project}_${volume}" >/dev/null
  docker run --rm --network none -v "${source_project}_${volume}:/target" "$archive_image" chown 10001:10001 /target
  source_mounts+=(-v "${source_project}_${volume}:/state/$path")
  restore_mounts+=(-v "${restore_project}_${volume}:/state/$path")
done
# Verify the prepared toolchain on the isolated network before stopping writers.
job "$source_project" python -m pytest tests/unit -q --tb=short > "$evidence/backend-tests.log" 2>&1
compose "$source_project" --profile test run --rm --no-deps --pull never -T --interactive=false ui-test python -m pytest tests/ui -q --tb=short > "$evidence/ui-tests.log" 2>&1
compose "$source_project" exec -T db sh -c 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT row_to_json(w) FROM meta.workflow_step w ORDER BY started_at"' > "$evidence/workflow-steps.jsonl"
compose "$source_project" exec -T db sh -c 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT pg_database_size(current_database())"' > "$evidence/database-bytes.txt"
# Configure explicit demo profiles/scenario on the freshly built database only.
job "$source_project" python /repro/configure.py > "$evidence/configuration.json"
publication=$(compose "$source_project" exec -T db sh -c 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT publication_id FROM meta.current_publication"')
ui_check "$source_project" before
compose "$source_project" --profile sensor up -d --no-build --pull never --wait broker
compose "$source_project" run --rm --no-deps --pull never -T --interactive=false \
  -v "$PWD/scripts/repro:/repro:ro" -v "$REPRO_ROOT/state:/state" "${source_mounts[@]}" \
  job python /repro/agc_sensor.py seed > "$evidence/sensor-seed.log" 2>&1
# Quiescent checkpoint: seed process/Spark/MQTT clients have exited.
compose "$source_project" stop app broker
job "$source_project" python /repro/state.py database > "$evidence/database-before.json"
compose "$source_project" exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup/database.dump"
[[ -s "$backup/database.dump" ]]
job "$source_project" python /repro/state.py files /data/raw > "$evidence/raw-before.json"
tar -C "${GREENHOUSE_RAW_PATH:-$PWD/data/raw}" -czf "$backup/raw.tar.gz" .
job "$source_project" python /repro/state.py files /data/import > "$evidence/import-before.json"
tar -C "${GREENHOUSE_IMPORT_PATH:-$PWD/data/import}" -czf "$backup/import.tar.gz" .
tar -C "$REPRO_ROOT/state" -czf "$backup/sensor-state.tar.gz" .
for volume in "${volumes[@]}"; do
  volume_inventory "$source_project" "$volume" before
  docker run --rm --network none -v "${source_project}_${volume}:/source:ro" \
    -v "$backup/volumes:/backup" "$archive_image" tar -C /source -czf "/backup/$volume.tar.gz" .
  docker run --rm --network none -v "$backup/volumes:/backup:ro" "$archive_image" tar -tzf "/backup/$volume.tar.gz" >/dev/null
done
compose "$source_project" run --rm --no-deps --pull never -T --interactive=false \
  -v "$PWD/scripts/repro:/repro:ro" -v "$backup:/backup:ro" \
  job python /repro/state.py files /backup > "$evidence/backup-inventory.json"
# Validate archive structure before removing this script's own source test volumes.
docker run --rm --network none -v "$backup:/backup:ro" "$archive_image" pg_restore --list /backup/database.dump > "$evidence/dump-toc.txt"
tar -tzf "$backup/raw.tar.gz" >/dev/null
tar -tzf "$backup/import.tar.gz" >/dev/null
tar -tzf "$backup/sensor-state.tar.gz" >/dev/null
compose "$source_project" --profile '*' down -v --remove-orphans
# Restore at identical container paths/DB identity; only external project differs.
tar -C "$REPRO_ROOT/restored/raw" -xzf "$backup/raw.tar.gz"
tar -C "$REPRO_ROOT/restored/import" -xzf "$backup/import.tar.gz"
tar -C "$REPRO_ROOT/restored/state" -xzf "$backup/sensor-state.tar.gz"
export GREENHOUSE_RAW_PATH="$REPRO_ROOT/restored/raw"
export GREENHOUSE_IMPORT_PATH="$REPRO_ROOT/restored/import"
for volume in "${volumes[@]}"; do
  docker volume create --label "com.docker.compose.project=$restore_project" --label "com.docker.compose.volume=$volume" "${restore_project}_${volume}" >/dev/null
  docker run --rm --network none -v "${restore_project}_${volume}:/target" \
    -v "$backup/volumes:/backup:ro" "$archive_image" tar -C /target -xzf "/backup/$volume.tar.gz"
done
compose "$restore_project" up -d --no-build --pull never --wait db
compose "$restore_project" exec -T db sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error' < "$backup/database.dump"
job "$restore_project" python /repro/state.py database > "$evidence/database-after.json"
job "$restore_project" python /repro/state.py compare /evidence/database-before.json /evidence/database-after.json > "$evidence/database-match.json"
job "$restore_project" python /repro/state.py files /data/raw > "$evidence/raw-after.json"
job "$restore_project" python /repro/state.py files /data/import > "$evidence/import-after.json"
job "$restore_project" python /repro/state.py compare /evidence/import-before.json /evidence/import-after.json > "$evidence/import-match.json"
job "$restore_project" python /repro/state.py compare /evidence/raw-before.json /evidence/raw-after.json > "$evidence/raw-match.json"
for volume in "${volumes[@]}"; do
  volume_inventory "$restore_project" "$volume" after
  job "$restore_project" python /repro/state.py compare "/evidence/before-$volume.json" "/evidence/after-$volume.json" > "$evidence/match-$volume.json"
done
compose "$restore_project" --profile sensor up -d --no-build --pull never --wait broker
compose "$restore_project" run --rm --no-deps --pull never -T --interactive=false \
  -v "$PWD/scripts/repro:/repro:ro" -v "$REPRO_ROOT/restored/state:/state" "${restore_mounts[@]}" \
  job python /repro/agc_sensor.py restore > "$evidence/sensor-restore.log" 2>&1
cp "$REPRO_ROOT/restored/state/restore-result.json" "$evidence/sensor-restore.json"
job "$restore_project" python -m greenhouse run-pipeline --resume full-offline-rehearsal > "$evidence/pipeline-resume.log" 2>&1
ui_check "$restore_project" after
compose "$restore_project" --profile test run --rm --no-deps --pull never -T --interactive=false \
  -e PGOPTIONS='-c default_transaction_read_only=on' \
  -v "$PWD/scripts/repro:/repro:ro" -v "$evidence:/evidence" \
  -v "$GREENHOUSE_RAW_PATH:/data/raw:ro" \
  ui-test python /repro/check_sensor_ui.py > "$evidence/sensor-ui.log" 2>&1
compose "$restore_project" up -d --no-build --pull never --wait app
compose "$restore_project" exec -T app python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health").read().decode())' > "$evidence/app-restored-health.txt"
for network in "${restore_project}_backend" "${restore_project}_frontend"; do
  [[ $(docker network inspect "$network" --format '{{.Internal}}') == true ]]
  printf '%s internal=true\n' "$network" >> "$evidence/network.txt"
done
job "$restore_project" python -c 'import socket; s=socket.socket(); s.settimeout(2); code=s.connect_ex(("1.1.1.1",443)); print("external_ip_connect_ex",code); assert code != 0' > "$evidence/egress-restored.txt"
job "$restore_project" python /repro/summarize.py /evidence > "$evidence/summary-output.log"
# Keep the restored project stopped for inspection. Explicit cleanup is documented.
compose "$restore_project" stop app broker db
printf 'Full AGC restore and replay resume passed; evidence=%s restored_project=%s\n' "$evidence" "$restore_project"
