#!/usr/bin/env bash
# Re-run the paired comparison in a fresh, private project. No git actions/prune.
set -euo pipefail
if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo 'Usage: run_engine_benchmark.sh SOURCE_METADATA.json REFERENCES_DIR [NEW_STATE_DIR]' >&2
  exit 2
fi
bench_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$bench_repo"
bench_project="gh-engine-benchmark-$(date -u +%Y%m%d%H%M%S)-$$"
if [[ $# -eq 3 ]]; then
  [[ ! -e "$3" ]] || { echo 'State directory must be new.' >&2; exit 2; }
  mkdir -p "$3"
  bench_state=$(cd -- "$3" && pwd)
else
  mkdir -p tmp
  bench_state=$(mktemp -d "$bench_repo/tmp/engine-benchmark-XXXXXXXX")
fi
python3 - "$bench_state" <<'PY'
import shutil,sys
if shutil.disk_usage(sys.argv[1]).free < 12*2**30:
    raise SystemExit('Need at least 12 GiB free host space; this runner retains both private DB volumes.')
PY
cp "$1" "$bench_state/source-metadata.json"
cp -R "$2" "$bench_state/references"
mkdir -p "$bench_state/pg/output" "$bench_state/ch/output" "$bench_state/ch/coordinator" "$bench_state/logs"
chmod 777 "$bench_state" "$bench_state/pg" "$bench_state/pg/output" "$bench_state/ch" "$bench_state/ch/output" "$bench_state/ch/coordinator"
export GREENHOUSE_BENCH_STATE_PATH="$bench_state"
bench_compose=(compose -f compose.benchmark.yaml -p "$bench_project")
bench_raw=/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68
bench_archive=/data/import/autonomous-greenhouse-challengeagc-2nd-2019.zip
job() {
  local engine=$1 phase=$2 script=$3
  shift 3
  local service=pg-job profile=postgres
  if [[ "$engine" == ch ]]; then service=ch-job; profile=clickhouse; fi
  docker "${bench_compose[@]}" --profile "$profile" run --rm --no-deps --pull never -T --interactive=false \
    "$service" python "/migration/$script" "$@" >"$bench_state/logs/$engine-$phase.log" 2>&1
}
publication() {
  python3 - "$bench_state/$1/lifecycle-fresh.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]));assert r['status']=='measured'
print(r['pipeline_result']['publication_id'])
PY
}
docker "${bench_compose[@]}" --profile postgres up -d --wait postgres broker
job pg fresh benchmark_lifecycle.py --backend postgres --mode fresh --metadata /bench/source-metadata.json \
  --raw "$bench_raw" --archive "$bench_archive" --output /bench/pg/output --report /bench/pg/lifecycle-fresh.json --workflow-id "$bench_project-pg"
bench_pg_pub=$(publication pg)
job pg seed seed_scenarios.py --metadata /bench/source-metadata.json --publication-id "$bench_pg_pub" --output /bench/pg/scenario-seed.json
job pg query benchmark_queries.py --backend postgres --publication-id "$bench_pg_pub" --output /bench/pg/queries.json
job pg scenario benchmark_scenarios.py --backend postgres --publication-id "$bench_pg_pub" --output /bench/pg/scenarios.json
job pg storage benchmark_inventory.py --backend postgres --output /bench/pg/storage-before-sensor.json
job pg sensor benchmark_sensor.py --backend postgres --publication-id "$bench_pg_pub" --raw "$bench_raw" --state /bench/pg/sensor --output /bench/pg/sensor.json
job pg resume benchmark_lifecycle.py --backend postgres --mode rerun --metadata /bench/source-metadata.json \
  --raw "$bench_raw" --archive "$bench_archive" --output /bench/pg/output --report /bench/pg/lifecycle-resume.json --workflow-id "$bench_project-pg"
docker "${bench_compose[@]}" --profile postgres stop postgres
docker "${bench_compose[@]}" --profile clickhouse up -d --wait clickhouse
job ch fresh benchmark_lifecycle.py --backend clickhouse --mode fresh --metadata /bench/source-metadata.json \
  --raw "$bench_raw" --archive "$bench_archive" --output /bench/ch/output --coordinator /bench/ch/coordinator \
  --report /bench/ch/lifecycle-fresh.json --workflow-id "$bench_project-ch"
bench_ch_pub=$(publication ch)
job ch parity check_parity.py --publication-id "$bench_ch_pub" --raw "$bench_raw" --coordinator /bench/ch/coordinator --references /bench/references --output /bench/ch/parity --transient-rules
job ch seed import_scenarios_benchmark.py --metadata /bench/source-metadata.json --proof /bench/ch/parity/parity.json --coordinator /bench/ch/coordinator --output /bench/ch/scenario-import.json
job ch maintenance settle_benchmark_parts.py --output /bench/ch/part-maintenance.json
job ch query benchmark_queries.py --backend clickhouse --publication-id "$bench_ch_pub" --reference /bench/pg/queries.json --output /bench/ch/queries.json
job ch scenario benchmark_scenarios.py --backend clickhouse --publication-id "$bench_ch_pub" --coordinator /bench/ch/coordinator --output /bench/ch/scenarios.json
job ch storage benchmark_inventory.py --backend clickhouse --output /bench/ch/storage-before-sensor.json
job ch sensor benchmark_sensor.py --backend clickhouse --publication-id "$bench_ch_pub" --raw "$bench_raw" --coordinator /bench/ch/coordinator --state /bench/ch/sensor --output /bench/ch/sensor.json --reference /bench/pg/sensor.json
job ch resume benchmark_lifecycle.py --backend clickhouse --mode rerun --metadata /bench/source-metadata.json \
  --raw "$bench_raw" --archive "$bench_archive" --output /bench/ch/output --coordinator /bench/ch/coordinator \
  --report /bench/ch/lifecycle-resume.json --workflow-id "$bench_project-ch"
docker "${bench_compose[@]}" --profile clickhouse stop clickhouse broker
python3 - "$bench_state" "$bench_project" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);r=json.loads((root/'ch/queries.json').read_text())
print(json.dumps({'state':str(root),'private_project_retained':sys.argv[2],
 'query_measurement':r['status'],'all_warm_summary_targets_met':r['all_warm_summary_targets_met']}))
PY
