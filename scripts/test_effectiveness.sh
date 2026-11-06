#!/usr/bin/env bash
# Automated UI walkthroughs; timings are test timings, not human task times.
set -euo pipefail
cd "$(dirname "$0")/.."
run_id="$(date -u +%Y%m%dT%H%M%SZ)-${RANDOM}"
result_dir="$PWD/tmp/effectiveness/$run_id"
mkdir -p "$result_dir"
chmod 777 "$result_dir"
printf 'Evidence: %s\n' "$result_dir"
docker compose build app
docker compose up -d --wait db
# The XML contains one case and elapsed test time per BR, including fixture setup.
docker compose --profile test run --rm --no-deps \
  -v "$result_dir:/evidence" ui-test python -m pytest -q --tb=short \
  --junitxml=/evidence/effectiveness.xml --durations=6 \
  tests/ui/test_climate_ui.py::test_climate_episode_selects_non_first_and_survives_basis_and_window \
  tests/ui/test_app_ui.py::test_single_day_window_updates_summary \
  tests/ui/test_drain_ui.py::test_drain_filters_preserve_totals_and_invalidate_selection \
  tests/ui/test_energy_ui.py::test_energy_screen_500_over_100_not_per_area_or_ab \
  tests/ui/test_harvest_ui.py::test_rule_demo_marks_only_the_over_threshold_record \
  tests/ui/test_cost_ui.py::test_cost_create_scenario_electric_plus_20_and_delta \
  2>&1 | tee "$result_dir/output.log"
printf 'Automated walkthroughs passed. Evidence: %s\n' "$result_dir"
