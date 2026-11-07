#!/usr/bin/env bash
# Only this invocation's candidate ClickHouse/broker/volumes are restarted.
set -euo pipefail
cd "$(dirname "$0")/.."
project="gh-ch-sensor-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
root="$PWD/tmp/clickhouse-sensor/$project"
mkdir -p "$root/db" "$root/broker"
chmod 777 "$root" "$root/db" "$root/broker"
compose() { docker compose -f compose.clickhouse.yaml -p "$project" --profile sensor "$@"; }
for suffix in ch_data ch_logs ch_broker; do
  if docker volume inspect "${project}_${suffix}" >/dev/null 2>&1; then exit 2; fi
done
printf '%s\n' "$project" > "$root/project.txt"
compose up -d --wait --no-build --pull never clickhouse ch-broker > "$root/start.log" 2>&1
[[ $(docker network inspect "${project}_backend" --format '{{.Internal}}') == true ]]
test_pid=''
wait_marker() {
  local marker=$1
  for _ in {1..180}; do
    [[ -f $marker ]] && return 0
    if [[ -n $test_pid ]] && ! kill -0 "$test_pid" 2>/dev/null; then
      wait "$test_pid" || true
      printf 'Test exited before marker: %s\n' "$marker" >&2
      return 1
    fi
    sleep 1
  done
  printf 'Recovery marker timeout: %s\n' "$marker" >&2
  return 1
}
run_test() {
  local pattern=$1 control=$2 log=$3
  compose run --rm --no-deps --pull never -T --interactive=false \
    -v "$PWD/src:/workspace/src:ro" -v "$PWD/tests:/workspace/tests:ro" -v "$control:/control" \
    -e PYTHONPATH=/workspace/src:/workspace/tests -e GREENHOUSE_CH_INTEGRATION=1 \
    -e GREENHOUSE_CH_RECOVERY_CONTROL=/control \
    -e GREENHOUSE_CH_MQTT_TEST=1 -e GREENHOUSE_CH_BROKER_RECOVERY=1 \
    -e GREENHOUSE_CH_SENSOR_EVIDENCE=/control/mqtt-recovery.json \
    ch-stream python -m unittest discover -s tests/clickhouse -p "$pattern" -v > "$log" 2>&1 &
  test_pid=$!
}
run_test test_stream_recovery.py "$root/db" "$root/db.log"
wait_marker "$root/db/db_ready_to_stop"
compose stop --timeout 10 clickhouse > "$root/db-stop.log" 2>&1
printf 'stopped\n' > "$root/db/db_stopped"
wait_marker "$root/db/db_failure_observed"
compose up -d --wait --no-build --pull never clickhouse > "$root/db-restart.log" 2>&1
printf 'restarted\n' > "$root/db/db_restarted"
wait "$test_pid"
test_pid=''
[[ -f $root/db/db_recovery_passed ]]
run_test test_mqtt_stream.py "$root/broker" "$root/broker.log"
wait_marker "$root/broker/broker_ready_to_stop"
compose stop --timeout 10 ch-broker > "$root/broker-stop.log" 2>&1
printf 'stopped\n' > "$root/broker/broker_stopped"
wait_marker "$root/broker/broker_failure_observed"
compose up -d --wait --no-build --pull never ch-broker > "$root/broker-restart.log" 2>&1
printf 'restarted\n' > "$root/broker/broker_restarted"
wait "$test_pid"
test_pid=''
[[ -f $root/broker/mqtt-recovery.json ]]
compose down -v --remove-orphans > "$root/cleanup.log" 2>&1
cat "$root/db/recovery.json" "$root/broker/mqtt-recovery.json"
printf 'Sensor evidence: %s\n' "$root"
# Failures retain only this project for diagnosis; no data or services outside it.
