"""Six summary workloads with fresh connections, raw samples and exact hashes.

Run engines sequentially with the same resource caps. Cold publication proof,
five warm-ups and thirty measured calls are separate; smoke is never acceptance.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import json
import math
from pathlib import Path
import statistics
import time

from greenhouse.app import wiring
from greenhouse.contracts import QueryContext,RuleBasis
from read_business_reference import fingerprint


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=('postgres','clickhouse'),required=True)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reference',type=Path)
    parser.add_argument('--compartment',default='Automatoes')
    parser.add_argument('--climate-rule',default='climate-demo-b01:Automatoes:v1')
    parser.add_argument('--drain-rule',default='drain-demo-b03:Automatoes:v1')
    parser.add_argument('--warmup',type=int,default=5)
    parser.add_argument('--iterations',type=int,default=30)
    parser.add_argument('--smoke',action='store_true')
    args=parser.parse_args()
    if args.smoke:args.warmup=1;args.iterations=1
    if not args.smoke and (args.warmup<5 or args.iterations<30):
        raise ValueError('Acceptance requires at least five warm-ups and thirty measured calls')
    def connect():
        if args.backend=='clickhouse':
            from greenhouse.clickhouse.client import Client,ClickHouseSettings
            return Client(ClickHouseSettings.from_env()).begin_action(shared_proofs=True)
        import psycopg
        from greenhouse.settings import Settings
        connection=psycopg.connect(**(Settings.from_env().connection_kwargs()|{'options':'-c default_transaction_read_only=on'}))
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        if connection.execute('SHOW transaction_read_only').fetchone()[0]!='on':
            raise ValueError('Benchmark PostgreSQL reads must be enforced read-only')
        return connection
    report={'status':'in_progress','backend':args.backend,'publication_id':args.publication_id,
        'started_at_utc':datetime.now(timezone.utc).isoformat(),'smoke':args.smoke,
        'warmup':args.warmup,'iterations':args.iterations,'fresh_connection_each_call':True,
        'database_os_caches_flushed':False,
        'evaluation_cache':False,'clickhouse_process_proof_cache':args.backend=='clickhouse','workloads':{}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save():args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    save();print(json.dumps({'stage':'cold_publication_started','backend':args.backend}),flush=True)
    begin=time.perf_counter()
    with closing(connect()) as connection:
        publication=wiring.energy_service(connection).adapter.publication(args.publication_id)
        cycles=[c for c in wiring.catalog_service(connection).cycles(publication) if c.compartment_id==args.compartment]
        if len(cycles)!=1:raise ValueError('One explicit benchmark cycle required')
        cycle=cycles[0]
    report['cold_publication_and_catalog_seconds']=time.perf_counter()-begin
    q=QueryContext(publication.scope_id,publication.publication_id,publication.snapshot_id,cycle.cycle_id,
        (cycle.compartment_id,),cycle.start,cycle.end)
    report['workload_definition']={'scope':publication.scope_id,'snapshot':publication.snapshot_id,
        'compartment':cycle.compartment_id,'cycle':cycle.cycle_id,'start':cycle.start.isoformat(),
        'end_exclusive':cycle.end.isoformat(),'climate_basis':'requested',
        'climate_rule':args.climate_rule,'drain_rule':args.drain_rule,'cost':'baseline tariff'}
    reference=json.loads(args.reference.read_text()) if args.reference else None
    if reference and reference['workload_definition']!=report['workload_definition']:
        raise ValueError('Reference workload differs')
    def cost(connection):
        service=wiring.cost_service(connection)
        tariff=service.get_baseline_tariff(publication.scope_id,cycle.compartment_id) or service.get_baseline_tariff(publication.scope_id)
        if tariff is None:raise ValueError('Benchmark baseline tariff missing')
        return service.get_cost(q,tariff_version=tariff.tariff_version)
    workloads={
        'BR01':lambda c:wiring.climate_service(c).get_evaluation(q,basis=RuleBasis.REQUESTED,rule_version=args.climate_rule,use_cache=False),
        'BR02':lambda c:wiring.irrigation_service(c).get_irrigation_summary(q),
        'BR03':lambda c:wiring.drain_service(c).get_evaluation(q,rule_version=args.drain_rule,use_cache=False),
        'BR04':lambda c:wiring.intensity_service(c).get_intensity(q),
        'BR05':lambda c:wiring.harvest_service(c).get_harvest_summary(q),
        'BR06':cost}
    print(json.dumps({'stage':'cold_publication_passed','seconds':report['cold_publication_and_catalog_seconds']}),flush=True)
    for name,operation in workloads.items():
        result={'samples_ms':[],'warmup_ms':[],'proof_cache_hits':0,'proof_cache_misses':0}
        report['workloads'][name]=result
        def call():
            started=time.perf_counter()
            with closing(connect()) as connection:
                value=operation(connection)
                result['proof_cache_hits']+=getattr(connection,'_shared_proof_hits',0)
                result['proof_cache_misses']+=getattr(connection,'_shared_proof_misses',0)
            milliseconds=(time.perf_counter()-started)*1000
            return value,milliseconds
        value,_=call();expected=fingerprint(value)
        if reference and reference['workloads'][name]['fingerprint']!=expected:
            raise ValueError('Cross-engine business fingerprint differs: '+name)
        result['fingerprint']=expected
        for phase,count in (('warmup_ms',args.warmup),('samples_ms',args.iterations)):
            for _ in range(count):
                value,milliseconds=call()
                if fingerprint(value)!=expected:raise ValueError('Business result changed while measuring '+name)
                result[phase].append(milliseconds);save()
        samples=result['samples_ms']
        result.update(p50_ms=statistics.median(samples),p95_ms=sorted(samples)[math.ceil(.95*len(samples))-1])
        result['p95_summary_within_2000ms']=result['p95_ms']<=2000
        save();print(json.dumps({'stage':'workload_measured','workload':name,'p95_ms':result['p95_ms']}),flush=True)
    report.update(status='measured',cross_engine_reference_matched=reference is not None,
        finished_at_utc=datetime.now(timezone.utc).isoformat(),
        is_acceptance_sample_count=not args.smoke,
        all_warm_summary_targets_met=all(v['p95_summary_within_2000ms'] for v in report['workloads'].values()))
    report['processing_cgroup']={}
    for name in ('cpu.max','memory.max','memory.peak'):
        path=Path('/sys/fs/cgroup')/name
        if path.is_file():report['processing_cgroup'][name]=path.read_text().strip()
    save();print(json.dumps({'status':'measured','backend':args.backend,'smoke':args.smoke,
        'all_warm_summary_targets_met':report['all_warm_summary_targets_met']}),flush=True)


if __name__=='__main__':main()
