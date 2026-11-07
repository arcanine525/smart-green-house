"""Serial pinned SQL/service benchmark. Never flush caches or claim cold DB.

Prerequisite: main TEST_CLEAR and no other project jobs. SQL comparisons retain
a first timed SQL sample and descriptive medians. Service acceptance uses five
warmups then30 samples and nearest-rank p95. All were pre-read for parity; no
sample is a first-touch or cold-DB measurement.
Service latency includes Python DTO validation and all gating/portfolio reads.
SQL component/harvest aggregates are quantity-only; their standalone ratios
are NOT accepted KPI without the service completeness/basis eligibility gates.
"""
from collections import defaultdict
from decimal import Decimal
import json,os,statistics,time,math,platform
from pathlib import Path
import psycopg
from greenhouse.app import wiring
from greenhouse.contracts import QueryContext,to_json,quantity_ratio
from greenhouse.settings import Settings
from greenhouse.queries.harvest import exact_sum
ROOT=Path('/benchmark');OUT=Path('/evidence')
PUB=os.environ.get('EXPECTED_PUBLICATION','892b4cd58cdf4d099510974d91fa64ab')


def measured(function):
    started=time.perf_counter(); value=function();return value,(time.perf_counter()-started)*1000


def service_samples(function, encode):
    expected=encode(function())  # explicit parity pre-read, outside timing
    warmups=[]
    for index in range(5):
        value,ms=measured(function);assert encode(value)==expected;warmups.append(ms)
    samples=[]
    for index in range(30):
        value,ms=measured(function);assert encode(value)==expected;samples.append(ms)
    p95=sorted(samples)[math.ceil(.95*len(samples))-1]
    return {'cache_state':'pre-read for parity, then5 warmups; shared cache uncontrolled',
        'warmup_samples_ms':warmups,'measured_samples_ms':samples,'measured_count':len(samples),
        'median_ms':statistics.median(samples),'p95_nearest_rank_ms':p95,
        'target_ms':2000,'p95_target_pass':p95<=2000}


def join(energy,harvest,eligible):
    e=defaultdict(list)
    for cycle,component,kwh in energy:e[cycle].append(kwh)
    a=dict(harvest)
    return {cy:quantity_ratio(exact_sum(e[cy]),a.get(cy)) for cy in eligible}


with psycopg.connect(**Settings.from_env().connection_kwargs(),autocommit=True,
    options='-c default_transaction_read_only=on -c statement_timeout=60000') as conn:
    svc=wiring.intensity_service(conn)
    pub=svc.energy.adapter.publication(PUB)
    cycles=wiring.catalog_service(conn).cycles(pub)
    contexts=tuple(QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cy.cycle_id,(cy.compartment_id,),cy.start,cy.end) for cy in cycles)
    expected=svc.get_portfolio(contexts); eligible=expected.included_cycle_ids
    expected_ratios={m.cycle.cycle_id:m.intensity_kwh_kg_a for m in expected.members if m.comparison_eligible}
    sqls={name:(ROOT/(name+'.sql')).read_text() for name in ('core-energy','core-harvest','mart-energy','mart-harvest')}
    results={}; cached={}; plans={}
    for name,query in sqls.items():
        samples=[]
        for index in range(6):
            rows,ms=measured(lambda:conn.execute(query,{'pub':PUB}).fetchall())
            if name in cached: assert rows==cached[name]
            cached[name]=rows;samples.append(ms)
        results[name]={'first_timed_sql_sample_pre_read_database_ms':samples[0],'warm_samples_ms':samples[1:],
            'warm_median_ms':statistics.median(samples[1:]),'rows':len(rows)}
        plans[name]=conn.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+query,{'pub':PUB}).fetchone()[0]
    assert cached['core-energy']==cached['mart-energy']
    assert cached['core-harvest']==cached['mart-harvest']
    joins=[]
    for index in range(20):
        values,ms=measured(lambda:join(cached['mart-energy'],cached['mart-harvest'],eligible))
        assert values==expected_ratios
        joins.append(ms)
    results['aggregate_join']={'samples_ms':joins,'median_ms':statistics.median(joins),
                               'eligible_rows':len(values),'excluded':expected.excluded_cycle_ids}
    results['service_single_cycle']=service_samples(lambda:svc.get_intensity(contexts[0]),to_json)
    results['service_portfolio']=service_samples(lambda:svc.get_portfolio(contexts),to_json)
    results['service_portfolio'].update(all_rows=len(expected.members),eligible_rows=len(eligible))
    report={'publication_id':PUB,'sql_quantity_parity':True,'service_result_parity':True,'results':results,
        'python':platform.python_version(),'platform':platform.platform(),
        'postgres':conn.execute('SELECT version()').fetchone()[0],
        'limitations':['Serial local Docker caps; no cache flush/restart. All service samples follow parity pre-read.',
            'Standalone SQL aggregations omit acceptance gates; end-to-end service measured separately.',
            'Not pipeline/browser latency; no architecture-wide performance claim.'],
        'memory_peak':Path('/sys/fs/cgroup/memory.peak').read_text().strip(),
        'memory_max':Path('/sys/fs/cgroup/memory.max').read_text().strip(),'cpu_max':Path('/sys/fs/cgroup/cpu.max').read_text().strip()}
    OUT.joinpath('benchmark.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
    OUT.joinpath('benchmark-plans.json').write_text(json.dumps(plans,indent=2,default=str)+'\n')
    print(json.dumps(report,default=str))
