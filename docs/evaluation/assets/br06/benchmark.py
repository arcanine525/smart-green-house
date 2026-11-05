"""Serial pinned SQL/service benchmark for BR-06 energy cost and scenarios.

Prerequisites: isolated measurement, no other project jobs.
Follows established standard:
- 5 warmups then 30 measured samples using nearest-rank p95 (target <= 2000 ms)
- Explicit parity pre-read outside timing
- Distinguishes read workloads from write workloads
- Preserves honest metadata and resource limits (no cold DB claims)
"""
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import platform
import statistics
import time
from uuid import uuid4
import psycopg

from greenhouse.app import wiring
from greenhouse.contracts import (
    EnergyComponent, PublicationRef, QueryContext, ScenarioChange, ScenarioCommand,
    to_json,
)
from greenhouse.settings import Settings

ROOT = Path('/scripts')
OUT = Path(os.environ.get('EVIDENCE_DIR', '/evidence'))
EXPECTED_PUB = os.environ.get('EXPECTED_PUBLICATION', '892b4cd58cdf4d099510974d91fa64ab')
RAW = Path('/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68')


def measured(function):
    started = time.perf_counter()
    value = function()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return value, elapsed_ms


def benchmark_workload(function, latency_type='query', encode=None, iterations=30, warmups_count=5):
    if encode:
        expected = encode(function())  # Pre-read outside timing

    warmups = []
    for _ in range(warmups_count):
        val, ms = measured(function)
        if encode:
            assert encode(val) == expected
        warmups.append(ms)

    samples = []
    for _ in range(iterations):
        val, ms = measured(function)
        if encode:
            assert encode(val) == expected
        samples.append(ms)

    p95 = sorted(samples)[math.ceil(0.95 * len(samples)) - 1]
    return {
        'latency_type': latency_type,
        'first_warmup_observation_ms': warmups[0] if warmups else None,
        'warmup_samples_ms': warmups,
        'measured_samples_ms': samples,
        'measured_count': len(samples),
        'min_ms': min(samples),
        'max_ms': max(samples),
        'median_ms': statistics.median(samples),
        'p95_nearest_rank_ms': p95,
        'target_ms': 2000,
        'p95_target_pass': p95 <= 2000,
    }


def run_benchmark():
    results = {}

    with psycopg.connect(**Settings.from_env().connection_kwargs()) as conn:
        cost_svc = wiring.cost_service(conn, {EXPECTED_PUB: RAW})
        pub = cost_svc.energy.adapter.publication(EXPECTED_PUB)
        catalog = wiring.catalog_service(conn)
        cycles = catalog.cycles(pub)
        aicu_cycle = next(c for c in cycles if c.compartment_name == 'AICU')

        context = QueryContext(
            pub.scope_id, pub.publication_id, pub.snapshot_id,
            aicu_cycle.cycle_id, (aicu_cycle.compartment_id,),
            aicu_cycle.start, aicu_cycle.end,
        )

        # Baseline tariff registration outside timing
        baseline_tariff = cost_svc.register_historical_tariff(pub.scope_id, aicu_cycle.compartment_id)
        conn.commit()

        # Seed test scenario outside timing
        seed_cmd = ScenarioCommand(
            request_id=f'bench-seed-{uuid4().hex}',
            baseline_version_id=baseline_tariff.tariff_version,
            name='Benchmark Seed Scenario',
            publication=pub,
            changes=(
                ScenarioChange(EnergyComponent.ELEC_HIGH, 'EUR/kWh', change_percent=Decimal('20.0')),
            ),
            analysis_context=context,
        )
        seed_scenario = cost_svc.execute_scenario_command(seed_cmd)
        conn.commit()

        # 1. READ: Baseline get_cost (Single cycle full window, 166 days)
        results['read_baseline_cost'] = benchmark_workload(
            lambda: cost_svc.get_cost(context),
            latency_type='query',
            encode=to_json,
        )

        # 2. READ: Compare scenarios (Baseline vs +20% scenario, 166 days)
        results['read_compare_scenarios'] = benchmark_workload(
            lambda: cost_svc.compare_scenarios(
                context,
                scenario_version=seed_scenario.scenario_version,
                baseline_tariff_version=baseline_tariff.tariff_version,
            ),
            latency_type='query',
            encode=to_json,
        )

        # 3. READ: list_scenarios for scope
        results['read_list_scenarios'] = benchmark_workload(
            lambda: cost_svc.list_scenarios(pub.scope_id),
            latency_type='query',
            encode=lambda s: len(s),
        )

        # 4. WRITE: execute_scenario_command + commit (durable write latency)
        def run_durable_write(command):
            cost_svc.execute_scenario_command(command)
            conn.commit()

        write_warmups = []
        for i in range(5):
            cmd = ScenarioCommand(
                request_id=f'bench-warmup-{uuid4().hex}',
                baseline_version_id=baseline_tariff.tariff_version,
                name=f'Warmup Write {i}',
                publication=pub,
                changes=(ScenarioChange(EnergyComponent.ELEC_HIGH, 'EUR/kWh', change_percent=Decimal('10.0')),),
                analysis_context=context,
            )
            _, ms = measured(lambda: run_durable_write(cmd))
            write_warmups.append(ms)

        write_samples = []
        for i in range(30):
            cmd = ScenarioCommand(
                request_id=f'bench-write-{uuid4().hex}',
                baseline_version_id=baseline_tariff.tariff_version,
                name=f'Benchmark Write {i}',
                publication=pub,
                changes=(ScenarioChange(EnergyComponent.ELEC_HIGH, 'EUR/kWh', change_percent=Decimal('15.0')),),
                analysis_context=context,
            )
            _, ms = measured(lambda: run_durable_write(cmd))
            write_samples.append(ms)

        p95_write = sorted(write_samples)[math.ceil(0.95 * len(write_samples)) - 1]
        results['write_scenario_command_durable'] = {
            'latency_type': 'durable_write',
            'first_warmup_observation_ms': write_warmups[0] if write_warmups else None,
            'warmup_samples_ms': write_warmups,
            'measured_samples_ms': write_samples,
            'measured_count': len(write_samples),
            'min_ms': min(write_samples),
            'max_ms': max(write_samples),
            'median_ms': statistics.median(write_samples),
            'p95_nearest_rank_ms': p95_write,
            'target_ms': 2000,
            'p95_target_pass': p95_write <= 2000,
        }

    from datetime import datetime, timezone
    report = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'source_state': os.environ.get('TESTED_STATE_SHA', '6313060c2b2b07c861224052e889395798c4e241c4fcac6e758dbb4f50015bf8'),
        'platform': f'{platform.system()} {platform.machine()}',
        'publication_id': EXPECTED_PUB,
        'baseline_tariff_version': baseline_tariff.tariff_version,
        'scenario_version': seed_scenario.scenario_version,
        'environment': 'test-runner / processing-env caps 2GiB 1.5CPU, DB 512MiB 0.5CPU',
        'cache_label': 'warm PostgreSQL connection / warm buffer cache; first warmup observation recorded separately',
        'workloads': results,
    }

    (OUT / 'benchmark.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({
        'status': 'BENCHMARK_COMPLETE',
        'read_baseline_p95': results['read_baseline_cost']['p95_nearest_rank_ms'],
        'read_compare_p95': results['read_compare_scenarios']['p95_nearest_rank_ms'],
        'write_p95': results['write_scenario_command_durable']['p95_nearest_rank_ms'],
        'all_passed': all(w['p95_target_pass'] for w in results.values()),
    }))


if __name__ == '__main__':
    run_benchmark()
