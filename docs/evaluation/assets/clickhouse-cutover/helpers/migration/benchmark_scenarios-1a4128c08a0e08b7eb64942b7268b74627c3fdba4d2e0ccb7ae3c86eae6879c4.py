"""Actual UI-style scenario create/read/idempotent retry on private benchmark DBs."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import statistics
import time
from uuid import uuid4

from greenhouse.app import wiring
from greenhouse.contracts import QueryContext,ScenarioCommand,ScenarioChange,EnergyComponent
from greenhouse.queries.cost import derive_scenario_rates


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=('postgres','clickhouse'),required=True)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--coordinator',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--warmup',type=int,default=5);parser.add_argument('--iterations',type=int,default=30)
    args=parser.parse_args()
    if args.warmup<5 or args.iterations<30:raise ValueError('At least five warm-ups and thirty samples required')
    def connect():
        if args.backend=='clickhouse':
            from greenhouse.clickhouse.client import Client,ClickHouseSettings
            settings=ClickHouseSettings.from_env()
            if (settings.host,settings.database,settings.user)!=('clickhouse','greenhouse_bench_ch','greenhouse_bench'):
                raise ValueError('Private benchmark endpoint/database/user required')
            if args.coordinator is None:raise ValueError('Explicit coordinator required')
            return Client(settings).begin_action(shared_proofs=True)
        import psycopg
        from greenhouse.settings import Settings
        settings=Settings.from_env()
        if (settings.pg_host,settings.pg_database,settings.pg_user)!=('postgres','greenhouse_bench_pg','greenhouse_bench'):
            raise ValueError('Private benchmark endpoint/database/user required')
        return psycopg.connect(**settings.connection_kwargs(),autocommit=True)
    def adapter(connection):
        if args.backend=='clickhouse':
            from greenhouse.clickhouse.cost import CostAdapter
            return CostAdapter(connection,args.coordinator)
        from greenhouse.db.cost_adapter import PostgresCostAdapter
        return PostgresCostAdapter(connection)
    with closing(connect()) as connection:
        store=adapter(connection);pub=store.publication(args.publication_id)
        cycle=next(c for c in wiring.catalog_service(connection).cycles(pub) if c.compartment_id=='Automatoes')
        q=QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cycle.cycle_id,('Automatoes',),cycle.start,cycle.end)
        baseline=store.get_baseline_tariff(pub.scope_id,cycle.compartment_id) or store.get_baseline_tariff(pub.scope_id)
        initial=len(store.list_scenarios(pub.scope_id))
    family='benchmark-price-'+uuid4().hex
    report={'status':'in_progress','backend':args.backend,'publication_id':pub.publication_id,
        'scenario_family':family,'initial_scenario_versions':initial,'warmup':args.warmup,'iterations':args.iterations,
        'started_at_utc':datetime.now(timezone.utc).isoformat(),'samples':[],'fresh_connection_per_operation':True,
        'clickhouse_proof_cache':'same guarded process cache as production UI','fixture_only':True}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save():args.output.write_text(json.dumps(report,indent=2)+'\n')
    save()
    for index in range(args.warmup+args.iterations):
        percent=Decimal(20+index%5)
        changes=tuple(ScenarioChange(c,'EUR/kWh',change_percent=percent) for c in (EnergyComponent.ELEC_HIGH,EnergyComponent.ELEC_LOW))
        command=ScenarioCommand(family+'-request-'+str(index),baseline.tariff_version,'Benchmark price '+str(percent),pub,changes,family,q)
        def write():
            with closing(connect()) as connection:
                return adapter(connection).execute_scenario_transaction(command,
                    lambda version:derive_scenario_rates(baseline,version,command.name,changes))
        begin=time.perf_counter();scenario=write();write_ms=(time.perf_counter()-begin)*1000
        if scenario.version!=index+1 or scenario.request_id!=command.request_id:raise ValueError('Scenario version/request differs')
        begin=time.perf_counter()
        with closing(connect()) as connection:reopened=adapter(connection).get_scenario(scenario.scenario_version)
        read_ms=(time.perf_counter()-begin)*1000
        if reopened!=scenario:raise ValueError('Scenario reopen differs')
        begin=time.perf_counter();reused=write();retry_ms=(time.perf_counter()-begin)*1000
        if reused!=scenario:raise ValueError('Idempotent retry changed scenario')
        report['samples'].append({'phase':'warmup' if index<args.warmup else 'measured',
            'version':scenario.version,'write_ms':write_ms,'read_ms':read_ms,'retry_ms':retry_ms,
            'same_request_reused':True,'exact_readback':True});save()
    report['summary']={}
    for key in ('write_ms','read_ms','retry_ms'):
        values=[r[key] for r in report['samples'] if r['phase']=='measured']
        report['summary'][key]={'p50':statistics.median(values),'p95':sorted(values)[math.ceil(.95*len(values))-1]}
    with closing(connect()) as connection:final=len(adapter(connection).list_scenarios(pub.scope_id))
    if final!=initial+args.warmup+args.iterations:raise ValueError('Scenario retry inserted extra versions')
    report.update(status='measured',final_scenario_versions=final,finished_at_utc=datetime.now(timezone.utc).isoformat())
    save();print(json.dumps({'status':'measured','backend':args.backend,'summary':report['summary']}),flush=True)


if __name__=='__main__':main()
