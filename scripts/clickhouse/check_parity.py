"""Verify full candidate contracts, raw lineage, CSV and all threshold episodes.

One read action verifies the frozen publication once, then all reads reuse that
same proof. This harness is a correctness check, never a latency benchmark.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sys

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.registry import reference
from greenhouse.clickhouse.threshold import ClimateAdapter,DrainAdapter
from greenhouse.clickhouse.schedules import register_rule_schedule
from greenhouse.contracts import QueryContext,RuleBasis,RuleScheduleRevision,from_dict,to_dict
from greenhouse.queries import ClimateService,DrainService
from greenhouse.app import exports,drain_exports
from read_business_reference import project as business
from read_lineage_reference import project as lineage
from read_export_reference import project as csv_exports,csv_projection

sys.path.insert(0,'/workspace/tests/clickhouse')
from read_postgres_threshold import metrics,fingerprint
from read_parity_reference import episodes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication-id',required=True)
    for name in ('raw','coordinator','references','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();refs=args.references
    thresholds=json.loads((refs/'postgres-full-cycle-csv-reference.json').read_text())
    expected_business=json.loads((refs/'postgres-business-reference.json').read_text())
    expected_lineage=json.loads((refs/'postgres-lineage-reference.json').read_text())
    expected_csv=json.loads((refs/'postgres-csv-reference.json').read_text())
    source=thresholds['source_publication'];expected_id=source['publication_id']
    for value in (expected_business,expected_lineage,expected_csv):
        if not value['read_only'] or value['publication_id']!=expected_id:raise ValueError('Mixed source references')
    report={'status':'in_progress','postgres_publication':expected_id,'clickhouse_publication':args.publication_id,'threshold_groups':[]}
    args.output.mkdir(parents=True,exist_ok=True)
    def save():
        (args.output/'parity.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    def check(label,actual,expected):
        (args.output/(label+'.json')).write_text(json.dumps(actual,indent=2,ensure_ascii=False)+'\n')
        if actual!=expected:
            report['status']='failed';report['failed_component']=label;save()
            raise ValueError('Parity mismatch: '+label+'; compare saved actual and source reference')
        report[label+'_match']=True;save();print(json.dumps({'stage':label,'status':'passed'}),flush=True)
    client=Client(ClickHouseSettings.from_env())
    # Register explicit ephemeral-reference rules before opening the read action.
    for row in thresholds['results']:register_rule_schedule(client,args.coordinator,from_dict(RuleScheduleRevision,row['rule']))
    with closing(client.begin_action()):
        pub=reference(client,args.publication_id)
        if (pub.scope_id,pub.snapshot_id)!=(source['scope_id'],source['snapshot_id']):raise ValueError('Candidate source scope/snapshot differs')
        report['publication_verified']=True;save()
        print(json.dumps({'stage':'candidate_publication_verified','publication':pub.publication_id}),flush=True)
        check('business',business(client,pub.publication_id,args.raw)['results'],expected_business['results'])
        check('csv',csv_exports(client,pub.publication_id),expected_csv['results'])
        check('lineage',lineage(client,pub.publication_id,args.raw),expected_lineage['results'])
        adapters={'climate':ClimateAdapter(client),'drain':DrainAdapter(client)}
        cycles={c.compartment_id:c for c in adapters['climate'].catalog_cycles(pub.publication_id)}
        for row in thresholds['results']:
            kind=row['kind'];cycle=cycles[row['compartment']];adapter=adapters[kind]
            q=QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cycle.cycle_id,(cycle.compartment_id,),cycle.start,cycle.end)
            read=adapter.read_climate_slots if kind=='climate' else adapter.read_drain_slots
            slots=read(q,pub)
            if len(slots)!=row['rows'] or fingerprint(slots,kind)!=row['slot_fingerprint']:
                raise ValueError('Full source value/quality fingerprint differs: '+row['compartment']+' '+kind)
            # Reuse the just-read frozen rows for duration evaluation rather
            # than transferring the same whole-cycle facts twice in this test.
            class FrozenSlots:
                def __getattr__(self,name):return getattr(adapter,name)
                def read_climate_slots(self,context,publication):
                    context.matches(publication);return slots
                def read_drain_slots(self,context,publication):
                    context.matches(publication);return slots
            service=(ClimateService if kind=='climate' else DrainService)(FrozenSlots())
            evaluation=service.get_evaluation(q,rule_version=row['rule']['rule_version'],use_cache=False,
                **({'basis':RuleBasis(row['basis'])} if kind=='climate' else {}))
            actual={'metrics':metrics(evaluation),'metric_contracts':to_dict(evaluation.result.metric_results),
                'episodes':episodes(evaluation),'joint':to_dict(evaluation.result.joint_results),
                'summary_csv':csv_projection(exports.climate_summary_csv(evaluation) if kind=='climate' else drain_exports.summary_csv(evaluation)),
                'episodes_csv':csv_projection(exports.climate_episodes_csv(evaluation,evaluation.result.episodes) if kind=='climate' else
                    drain_exports.episodes_csv(evaluation,evaluation.result.episodes,total=len(evaluation.result.episodes),metric=None,min_minutes=0))}
            label='threshold-'+row['compartment']+'-'+kind+'-'+row['basis']
            check(label,actual,{k:row[k] for k in actual})
            report['threshold_groups'].append(dict(compartment=row['compartment'],kind=kind,basis=row['basis'],episodes=len(actual['episodes']),slots=len(slots),
                slot_fingerprint=row['slot_fingerprint'],matches_postgres_and_raw=True))
            save()
        report.update(status='passed',all_six_brs=True,includes_cycle_boundary_episodes=True,
            episodes_compared=sum(g['episodes'] for g in report['threshold_groups']))
        save();print(json.dumps({'status':'passed','threshold_groups':len(report['threshold_groups']),'episodes':report['episodes_compared']}))


if __name__=='__main__':main()
