"""Full-cycle PG/independent CSV reference, including cycle-boundary episodes."""
import argparse
from datetime import datetime,time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

import psycopg

sys.path.insert(0,'/workspace/tests/clickhouse')
from read_postgres_threshold import demo,metrics,fingerprint
from threshold_oracle import source_rows,evaluate
from greenhouse.settings import Settings
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.contracts import QueryContext,RuleBasis,to_dict
from greenhouse.queries import ClimateService,DrainService
from greenhouse.app import exports,drain_exports
from read_export_reference import csv_projection


def episodes(evaluation):
    return [[e.metric,e.original_start.isoformat(),e.original_end.isoformat(),str(e.original_minutes)] for e in evaluation.result.episodes]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--raw',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();result=[]
    with psycopg.connect(**(Settings.from_env().connection_kwargs()|{'options':'-c default_transaction_read_only=on'})) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        assert connection.execute('SHOW transaction_read_only').fetchone()[0]=='on'
        catalog=PostgresCatalogAdapter(connection)
        publication=PostgresClimateQueryAdapter(connection).publication(args.publication_id)
        for cycle in catalog.catalog_cycles(publication.publication_id):
            lo=datetime.combine(cycle.start,time.min);hi=datetime.combine(cycle.end,time.min)
            raw=source_rows(args.raw/cycle.compartment_id/'GreenhouseClimate.csv',lo,hi)
            q=QueryContext(publication.scope_id,publication.publication_id,publication.snapshot_id,cycle.cycle_id,(cycle.compartment_id,),cycle.start,cycle.end)
            for kind,base,service_type,bases in (('climate',PostgresClimateQueryAdapter,ClimateService,(RuleBasis.REQUESTED,RuleBasis.REALIZED)),
                    ('drain',PostgresDrainQueryAdapter,DrainService,(RuleBasis.MEASURED,))):
                rule=demo(publication.scope_id,cycle.compartment_id,kind,lo,hi)
                class Adapter(base):
                    def schedule(self,version):
                        if version!=rule.rule_version:raise ValueError('Unexpected reference rule')
                        return rule
                adapter=Adapter(connection)
                read=adapter.read_climate_slots if kind=='climate' else adapter.read_drain_slots
                rows=read(q,publication)
                if len(rows)!=len(raw):raise ValueError('Reference slots differ from independent raw source')
                signature=fingerprint(rows,kind)
                service=service_type(adapter)
                for basis in bases:
                    evaluation=service.get_evaluation(q,rule_version=rule.rule_version,use_cache=False,
                        **({'basis':basis} if kind=='climate' else {}))
                    raw_metrics,raw_episodes=evaluate(raw,lo,hi,kind,basis.value,include_boundary_episodes=True)
                    actual=metrics(evaluation);all_episodes=episodes(evaluation)
                    if actual!=raw_metrics:raise ValueError('Full-cycle PG/raw metric mismatch: '+cycle.compartment_id+' '+kind+' '+basis.value)
                    if all_episodes!=raw_episodes:raise ValueError('Full-cycle PG/raw boundary episode mismatch: '+cycle.compartment_id+' '+kind+' '+basis.value)
                    result.append(dict(compartment=cycle.compartment_id,kind=kind,basis=basis.value,rows=len(rows),slot_fingerprint=signature,
                        rule=to_dict(rule),metrics=actual,metric_contracts=to_dict(evaluation.result.metric_results),episodes=all_episodes,
                        joint=to_dict(evaluation.result.joint_results),raw_oracle_match=True,
                        summary_csv=csv_projection(exports.climate_summary_csv(evaluation) if kind=='climate' else drain_exports.summary_csv(evaluation)),
                        episodes_csv=csv_projection(exports.climate_episodes_csv(evaluation,evaluation.result.episodes) if kind=='climate' else
                            drain_exports.episodes_csv(evaluation,evaluation.result.episodes,total=len(evaluation.result.episodes),metric=None,min_minutes=Decimal(0)))))
                    print(json.dumps({'stage':'reference_group','compartment':cycle.compartment_id,'kind':kind,'basis':basis.value,
                        'episodes':len(all_episodes),'raw_oracle_match':True}),flush=True)
        output=dict(status='passed',engine='postgres',read_only=True,isolation='repeatable read',source_publication=to_dict(publication),
            source_window=[min(r['rule']['segments'][0]['start'] for r in result),max(r['rule']['segments'][-1]['end'] for r in result)],
            includes_cycle_boundary_episodes=True,results=result)
        payload=json.dumps(output,sort_keys=True,indent=2,ensure_ascii=False)+'\n'
        args.output.write_text(payload)
        print(json.dumps({'status':'passed','groups':len(result),'episodes':sum(len(r['episodes']) for r in result),'sha256':hashlib.sha256(payload.encode()).hexdigest()}))


if __name__=='__main__':main()
