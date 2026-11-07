"""Seed frozen scenario workload on the disposable PostgreSQL benchmark only.

This is fixture preparation, not a migration/cutover acceptance claim. Preserve
source author/time and map publication/context explicitly to the measured copy.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from greenhouse.settings import Settings
from greenhouse.contracts import CostScenario,PublicationRef,from_dict,to_dict
from greenhouse.db.cost_adapter import PostgresCostAdapter
from greenhouse.queries.cost import derive_scenario_rates
from greenhouse.clickhouse.encoding import digest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata',type=Path,required=True)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();settings=Settings.from_env()
    if (settings.pg_host,settings.pg_database,settings.pg_user)!=('postgres','greenhouse_bench_pg','greenhouse_bench'):
        raise ValueError('This fixture writer requires the isolated benchmark service/database/user')
    metadata=json.loads(args.metadata.read_text())
    if metadata.get('backend')!='postgres' or metadata.get('read_only') is not True:
        raise ValueError('Frozen read-only source export required')
    source=from_dict(PublicationRef,metadata['source_publication']);expected={};families={}
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        adapter=PostgresCostAdapter(connection);target=adapter.publication(args.publication_id)
        if (source.scope_id,source.snapshot_id)!=(target.scope_id,target.snapshot_id):raise ValueError('Different scope/snapshot')
        for original in sorted((from_dict(CostScenario,v) for v in metadata['scenarios']),key=lambda s:(s.scenario_id,s.version)):
            if original.publication!=source:raise ValueError('Different source reference')
            context=original.analysis_context
            if context is not None:
                context.matches(source);context=replace(context,publication_id=target.publication_id)
                if not adapter.validate_cycle(context.scope_id,context.snapshot_id,context.cycle_id,context.compartment_ids[0]):
                    raise ValueError('Scenario cycle missing in target')
            scenario=replace(original,publication=target,analysis_context=context)
            baseline=adapter.get_tariff(scenario.baseline_tariff_version)
            if baseline is None or scenario.rates!=derive_scenario_rates(baseline,scenario.scenario_version,scenario.name,scenario.changes):
                raise ValueError('Source rates disagree with target baseline/changes')
            if scenario.scenario_version in expected:raise ValueError('Duplicate scenario version')
            expected[scenario.scenario_version]=scenario;families.setdefault(scenario.scenario_id,[]).append(scenario.version)
        for versions in families.values():
            if versions!=list(range(1,max(versions)+1)):raise ValueError('Source scenario family has gaps')
        existing={s.scenario_version:s for s in adapter.list_scenarios(target.scope_id)}
        if any(expected.get(version)!=value for version,value in existing.items()):raise ValueError('Target contains different or extra fixture scenarios')
        inserted=0
        for scenario in expected.values():
            if scenario.scenario_version in existing:continue
            changes=to_dict(scenario.changes)
            command={'request_id':scenario.request_id,'scenario_id':scenario.scenario_id,'version':scenario.version,
                'name':scenario.name,'baseline_version_id':scenario.baseline_tariff_version,
                'publication_id':target.publication_id,'changes':changes}
            if scenario.analysis_context is not None:command['analysis_context']=to_dict(scenario.analysis_context)
            connection.execute('''INSERT INTO config.cost_scenario
                (scenario_version,scenario_id,version,request_id,baseline_tariff_version,publication_id,name,author,changes,command_payload,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (scenario.scenario_version,scenario.scenario_id,scenario.version,scenario.request_id,scenario.baseline_tariff_version,
                 target.publication_id,scenario.name,scenario.author,Jsonb(changes),Jsonb(command),scenario.created_at))
            for rate in scenario.rates:
                connection.execute('''INSERT INTO config.cost_scenario_rate
                    (scenario_rate_id,scenario_version,component,currency,price_unit,rate,valid_from,valid_to_exclusive,source_reference)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (rate.rate_id,scenario.scenario_version,rate.component.value,rate.currency,rate.price_unit,rate.rate,
                     rate.valid_from,rate.valid_to_exclusive,rate.source_reference))
            inserted+=1
        for version,scenario in expected.items():
            if adapter.get_scenario(version)!=scenario:raise ValueError('Seeded scenario differs from frozen mapped DTO')
    report={'status':'seeded_benchmark_fixture','scenario_versions':len(expected),'families':len(families),
        'inserted':inserted,'reused':len(expected)-inserted,'source_metadata_hash':digest(metadata),
        'source_publication':source.publication_id,'target_publication':target.publication_id,
        'preserves_request_author_time_rates_context':True,'fixture_only':True}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
