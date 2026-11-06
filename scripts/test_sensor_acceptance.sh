#!/usr/bin/env bash
# Sensor acceptance using only test-owned Docker containers/volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
root_dir=$PWD
run_id="$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
network_name=${SENSOR_TEST_NETWORK:-smart-greenhouse_backend}
broker_name="greenhouse-recovery-broker-${run_id}"
db_name="greenhouse-recovery-db-${run_id}"
runner_name="greenhouse-recovery-runner-${run_id}"
evidence_dir="$root_dir/tmp/sensor-recovery/$run_id"
mkdir -p "$evidence_dir/db" "$evidence_dir/broker"
printf 'Evidence directory: %s\n' "$evidence_dir"
chmod 777 "$evidence_dir/db" "$evidence_dir/broker"
test_pid=''
stats_pid=''
cleanup() {
    if [[ -n $test_pid ]]; then kill "$test_pid" 2>/dev/null || true; fi
    if [[ -n $stats_pid ]]; then kill "$stats_pid" 2>/dev/null || true; fi
    for container in "$runner_name" "$broker_name" "$db_name"; do
        label=$(docker inspect --format '{{index .Config.Labels "greenhouse.test.run"}}' "$container" 2>/dev/null || true)
        if [[ $label == "$run_id" ]]; then docker rm -f -v "$container" >/dev/null 2>&1 || true; fi
    done
}
trap cleanup EXIT INT TERM
wait_marker() {
    local marker=$1
    for _ in {1..120}; do
        [[ -f $marker ]] && return 0
        if ! kill -0 "$test_pid" 2>/dev/null; then
            wait "$test_pid" || true
            printf 'Test exited before marker: %s\n' "$marker" >&2
            return 1
        fi
        sleep 1
    done
    printf 'Timed out waiting for marker: %s\n' "$marker" >&2
    return 1
}
docker network inspect "$network_name" >/dev/null
# These exact names and labels are unique to this invocation.
docker run -d --name "$db_name" --label "greenhouse.test.run=$run_id" --network "$network_name" \
    --cpus 0.5 --memory 512m -e POSTGRES_USER=greenhouse -e POSTGRES_DB=greenhouse \
    -e POSTGRES_PASSWORD=replay-local-test \
    postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f >/dev/null
docker run -d --name "$broker_name" --label "greenhouse.test.run=$run_id" --network "$network_name" \
    --cpus 0.1 --memory 128m \
    --mount "type=bind,src=$root_dir/docker/mosquitto/mosquitto.conf,dst=/mosquitto/config/mosquitto.conf,readonly" \
    eclipse-mosquitto:2.0.22@sha256:09b62ff32c2e7b64d4b431b9a89236073289b68db273de2b089edeb9b0d92b5c >/dev/null
for _ in {1..60}; do
    if docker exec "$db_name" pg_isready -q -U greenhouse -d greenhouse; then break; fi
    sleep 1
done
docker exec "$db_name" pg_isready -q -U greenhouse -d greenhouse
docker exec "$broker_name" mosquitto_pub -h 127.0.0.1 -t test/health -m ready
run_standard() {
    local cpu=$1 memory=$2 test_case=$3 log=$4
    GREENHOUSE_JOB_CPUS="$cpu" GREENHOUSE_JOB_MEMORY="$memory" docker compose --env-file "$root_dir/.env" run \
        --rm --no-deps --name "$runner_name" --label "greenhouse.test.run=$run_id" \
        -v "$root_dir/src:/workspace/src:ro" -v "$root_dir/tests:/workspace/tests:ro" -v "$evidence_dir:/evidence" \
        -e PYTHONPATH=/workspace/src:/workspace/tests -e PGHOST="$db_name" -e PGDATABASE=greenhouse \
        -e PGUSER=greenhouse -e PGPASSWORD=replay-local-test -e MQTT_HOST="$broker_name" \
        -e GREENHOUSE_SENSOR_EVIDENCE=/evidence/live.json -e GREENHOUSE_SENSOR_ACCEPTANCE=1 \
        job python -m pytest "$test_case" -q --tb=line >"$log" 2>&1
}
# Sample only while the real-MQTT baseline test executes. Memory maxima are
# sampled service values, not guaranteed component peaks.
docker stats --format '{{json .}}' "$broker_name" "$db_name" >"$evidence_dir/service-stats.jsonl" &
stats_pid=$!
printf 'Running real-MQTT parity and latency...\n'
run_standard 1.0 1536m tests/integration/test_sensor_live_replay.py::test_real_broker_replay_matches_batch_and_records_latency "$evidence_dir/live.log"
kill "$stats_pid" 2>/dev/null || true
wait "$stats_pid" 2>/dev/null || true
stats_pid=''
printf 'Checking Spark process restart...\n'
run_standard 1.0 1536m tests/integration/test_sensor_live_replay.py::test_stream_process_restart_uses_same_checkpoint "$evidence_dir/spark-restart.log"
printf 'Running MQTT fault matrix and late-event check...\n'
run_standard 1.0 1536m tests/integration/test_sensor_fault_matrix.py "$evidence_dir/fault-matrix.log"
printf 'Checking broker redelivery and receipts...\n'
run_standard 0.2 128m tests/integration/test_real_broker.py "$evidence_dir/mqtt-faults.log"
run_test() {
    local test_case=$1 control_dir=$2 log=$3
    GREENHOUSE_JOB_CPUS=1.0 GREENHOUSE_JOB_MEMORY=1536m docker compose --env-file "$root_dir/.env" run \
        --rm --no-deps --name "$runner_name" --label "greenhouse.test.run=$run_id" \
        -v "$root_dir/src:/workspace/src:ro" -v "$root_dir/tests:/workspace/tests:ro" -v "$control_dir:/control" \
        -e PYTHONPATH=/workspace/src:/workspace/tests -e PGHOST="$db_name" -e PGDATABASE=greenhouse \
        -e PGUSER=greenhouse -e PGPASSWORD=replay-local-test -e MQTT_HOST="$broker_name" \
        -e GREENHOUSE_RECOVERY_CONTROL=/control job python -m pytest "$test_case" -q --tb=line >"$log" 2>&1 &
    test_pid=$!
}
printf 'Checking database restart...\n'
run_test tests/integration/test_sensor_recovery_external.py::test_postgresql_restart_replays_uncommitted_spark_batch "$evidence_dir/db" "$evidence_dir/db.log"
wait_marker "$evidence_dir/db/db_ready_to_stop"
docker stop --timeout 10 "$db_name" >/dev/null
printf 'stopped\n' > "$evidence_dir/db/db_stopped"
wait_marker "$evidence_dir/db/db_failure_observed"
docker start "$db_name" >/dev/null
for _ in {1..60}; do
    if docker exec "$db_name" pg_isready -q -U greenhouse -d greenhouse; then break; fi
    sleep 1
done
docker exec "$db_name" pg_isready -q -U greenhouse -d greenhouse
printf 'restarted\n' > "$evidence_dir/db/db_restarted"
wait "$test_pid"
test_pid=''
[[ -f "$evidence_dir/db/db_recovery_passed" ]]
printf 'Checking broker, collector and simulator restart...\n'
run_test tests/integration/test_sensor_recovery_external.py::test_broker_collector_and_simulator_restart "$evidence_dir/broker" "$evidence_dir/broker.log"
wait_marker "$evidence_dir/broker/broker_ready_to_restart"
docker stop --timeout 10 "$broker_name" >/dev/null
docker start "$broker_name" >/dev/null
for _ in {1..30}; do
    if docker exec "$broker_name" mosquitto_pub -h 127.0.0.1 -t test/health -m ready; then break; fi
    sleep 1
done
docker exec "$broker_name" mosquitto_pub -h 127.0.0.1 -t test/health -m ready
printf 'restarted\n' > "$evidence_dir/broker/broker_restarted"
wait "$test_pid"
test_pid=''
[[ -f "$evidence_dir/broker/broker_recovery_passed" ]]
printf 'Sensor acceptance passed. Evidence: %s\n' "$evidence_dir"
