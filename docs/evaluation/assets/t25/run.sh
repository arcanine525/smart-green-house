#!/usr/bin/env bash
# Reproduce measurements using the existing local Compose stack; never recreate DB.
set -euo pipefail
repo=$(cd -- "$(dirname -- "$0")/../../../.." && pwd)
run_id=${BENCH_RUN_ID:-$(date -u +%Y%m%d_%H%M%S)}
if [[ ! "$run_id" =~ ^[a-zA-Z0-9_]{1,20}$ ]]; then
  echo 'BENCH_RUN_ID must contain 1–20 letters, digits or underscores.' >&2
  exit 2
fi
if [[ ! -f "$repo/.env" ]]; then
  echo 'Configure the repository .env and start its database using the README first.' >&2
  exit 2
fi
db_id=$(docker compose --env-file "$repo/.env" -f "$repo/compose.yaml" --project-directory "$repo" ps -q db)
if [[ -z "$db_id" ]]; then
  echo 'The existing database service must be running before benchmarking.' >&2
  exit 2
fi
db_limits=$(docker inspect --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}' "$db_id")
if [[ "$db_limits" != '536870912 500000000' ]]; then
  echo 'Benchmark requires the existing DB at 512 MiB / 0.5 CPU; configure it separately first.' >&2
  exit 2
fi
out=${BENCH_EVIDENCE_DIR:-"$repo/tmp/benchmarks/$run_id"}
mkdir -p "$out"
out=$(cd "$out" && pwd)
if [[ -e "$out/benchmark_results.json" || -e "$out/cache-comparison.json" ]]; then
  echo 'Evidence already exists; select a new BENCH_RUN_ID or BENCH_EVIDENCE_DIR.' >&2
  exit 2
fi
job="greenhouse-benchmark-$run_id"
if docker container inspect "$job" >/dev/null 2>&1; then
  echo 'Container name collision; select a new BENCH_RUN_ID.' >&2
  exit 2
fi
docker_args=(run --rm --no-deps --name "$job"
  -e PYTHONPATH=/review-src -e PYTHONUNBUFFERED=1
  -e BENCH_RUN_ID="$run_id" -e OUTPUT_DIR=/results
  -e BENCH_OUTPUT_ROOT="/data/output/t25_bench/$run_id"
  -v "$repo/src:/review-src:ro"
  -v "$repo/docs/evaluation/assets/t25:/bench:ro"
  -v "$out:/results")
python_args=(python /bench/benchmark_suite.py)
case "${BENCH_MODE:-full}" in
  full) ;;
  cache)
    measurement_file=${BENCH_MEASUREMENTS_PATH:?Set BENCH_MEASUREMENTS_PATH to a completed full benchmark JSON}
    if [[ ! -f "$measurement_file" ]]; then
      echo 'Completed benchmark JSON does not exist.' >&2
      exit 2
    fi
    measurement_file=$(cd "$(dirname "$measurement_file")" && pwd)/$(basename "$measurement_file")
    docker_args+=(-v "$measurement_file:/measurement-input.json:ro")
    python_args=(python /bench/cache_benchmark.py --measurements /measurement-input.json)
    ;;
  *) echo 'BENCH_MODE must be full or cache.' >&2; exit 2 ;;
esac
GREENHOUSE_JOB_CPUS=1.5 GREENHOUSE_JOB_MEMORY=2g docker compose \
  --env-file "$repo/.env" -f "$repo/compose.yaml" --project-directory "$repo" \
  "${docker_args[@]}" job "${python_args[@]}" "$@" 2>&1 | tee "$out/execution.log"
printf 'Evidence: %s\n' "$out"
