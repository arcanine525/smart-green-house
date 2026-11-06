"""Read-only follow-up: existing Hybrid result cache on the same full workloads.

Run after benchmark_suite.py has completed, in a separate capped processing job.
No new cache is added to either architecture. Kimball's absent database namespace
is reported explicitly; use_cache=True would not enable caching there.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import psycopg
import benchmark_suite as bench


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--measurements', type=Path, default=Path('/bench/benchmark_results.json'))
    parser.add_argument('--output', type=Path, default=Path('/results/cache-comparison.json'))
    args = parser.parse_args()
    recorded = json.loads(args.measurements.read_text())
    assert recorded['status'] == 'completed'
    lifecycle = recorded['stages']['pipeline_lifecycle']
    kwargs = bench.Settings.from_env().connection_kwargs()
    kwargs |= {'dbname': lifecycle['hybrid']['fresh_database'], 'options': '-c default_transaction_read_only=on'}
    result = {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(),
              'source_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'measurement_input_sha256': hashlib.sha256(args.measurements.read_bytes()).hexdigest(),
              'database': kwargs['dbname'], 'read_only': True, 'workloads': {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def persist():
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    persist()
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        for br, adapter_type, service_type, prefix in (
            ('BR-01_Climate', bench.PostgresClimateQueryAdapter, bench.ClimateService, 'climate-demo-b01'),
            ('BR-03_Drain', bench.PostgresDrainQueryAdapter, bench.DrainService, 'drain-demo-b03'),
        ):
            adapter = adapter_type(conn)
            assert adapter.database_key is not None
            pub = adapter.publication(adapter.pin_publication(bench.AGC_SCOPE).publication_id)
            assert pub.publication_id == lifecycle['hybrid']['publication_id']
            catalog = bench.CatalogService(bench.PostgresCatalogAdapter(conn))
            cycle = next(c for c in catalog.cycles(pub) if c.compartment_id == bench.PRIMARY_COMPARTMENT)
            ctx = bench.QueryContext(pub.scope_id, pub.publication_id, pub.snapshot_id,
                                     cycle.cycle_id, (cycle.compartment_id,), cycle.start, cycle.end)
            service = service_type(adapter)
            options = {'rule_version': f'{prefix}:{cycle.compartment_id}:v1'}
            if br.startswith('BR-01'):
                options['basis'] = bench.RuleBasis.REQUESTED
                read_slots = adapter.read_climate_slots
            else:
                read_slots = adapter.read_drain_slots
            expected = service.get_evaluation(ctx, use_cache=False, **options)

            def query():
                slots = read_slots(ctx, pub)
                evaluation = service.get_evaluation(ctx, use_cache=True, **options)
                return slots, evaluation

            start = time.perf_counter()
            _, first = query()  # First cached evaluation is a real cache miss.
            first_miss_ms = (time.perf_counter() - start) * 1000
            assert first == expected, 'Enabling cache changed complete evaluation result'
            _, second = query()
            assert second is first, 'Existing cache was not actually hit'
            metrics = bench.sample_workload_metrics(query, query, warmups=5, samples_count=30)
            metrics['cache_state_description'] = 'Existing Hybrid result cache warmed for an identical publication/rule/context; every sample still reads all slots. First cache miss recorded separately.'
            result['workloads'][br] = {
                'uncached_reference': recorded['stages']['workload_latencies'][br]['hybrid'],
                'first_cache_miss_ms': round(first_miss_ms, 3),
                'cached': metrics, 'complete_evaluation_unchanged': True, 'cache_hit_identity_verified': True,
            }
            persist()
            print(br, 'cached p95 ms', metrics['p95_nearest_rank_ms'], flush=True)
    kwargs['dbname'] = lifecycle['kimball']['fresh_database']
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        namespace = getattr(bench.KimballQueryAdapter(conn), 'database_key', None)
        result['kimball_cache'] = {'database_namespace_available': namespace is not None,
                                   'measured': False, 'reason': 'This follow-up measures the existing Hybrid cache only; no Kimball cache implementation is added.'}
    result['status'] = 'completed'
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    result['runtime'] = bench.collect_system_metadata()
    persist()


if __name__ == '__main__':
    main()
