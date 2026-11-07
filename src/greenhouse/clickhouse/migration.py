"""Import frozen source scenarios only after a verified publication parity gate.

Original source DTOs stay in the pinned migration archive. Native scenario IDs,
versions, requests, rates, author and timestamp are preserved; the publication
and its analysis-context ID are explicitly mapped to the proven candidate.
"""
from dataclasses import replace
import json

from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.cost import CostAdapter,validate_rates
from greenhouse.clickhouse.encoding import digest
from greenhouse.clickhouse.repository import Repository
from greenhouse.contracts import CostScenario,PublicationRef,ScenarioCommand,from_dict,to_dict
from greenhouse.queries.cost import derive_scenario_rates


def import_scenarios(client,coordinator,metadata,proof):
    if metadata.get('format')!=1 or metadata.get('backend')!='postgres' or metadata.get('read_only') is not True:
        raise ValueError('Verified read-only source export required')
    source=from_dict(PublicationRef,metadata['source_publication'])
    if proof.get('status')!='passed' or proof.get('all_six_brs') is not True or proof.get('postgres_publication')!=source.publication_id:
        raise ValueError('Passing six-BR parity proof for this source publication required')
    target_id=proof['clickhouse_publication'];source_hash=digest(metadata)
    repo=Repository(client,coordinator);cost=CostAdapter(client,coordinator)
    with writer_lock(coordinator,client):
        # Fresh adapter verification happens before native writes. The shared
        # fence excludes supported source/config writers for this whole import.
        target=cost.publication(target_id)
        if (target.scope_id,target.snapshot_id)!=(source.scope_id,source.snapshot_id):
            raise ValueError('Source and candidate scope/snapshot differ')
        envelope=cost._envelope(target_id)
        if set(envelope['required_bundles'])!={'water','harvest','climate','drain','energy'}:
            raise ValueError('Candidate must contain all six business requirements')
        archive=[p for p in envelope['configuration']['pins'] if p['kind']=='migration_source'
            and p['version_id']=='postgres:'+source.publication_id]
        if len(archive)!=1 or archive[0]['content_hash']!=source_hash:
            raise ValueError('Source export differs from candidate frozen migration archive')
        scenarios=[from_dict(CostScenario,value) for value in metadata['scenarios']]
        if len({s.scenario_version for s in scenarios})!=len(scenarios) or len({s.request_id for s in scenarios})!=len(scenarios):
            raise ValueError('Repeated source scenario version or request')
        expected={};families={}
        for original in sorted(scenarios,key=lambda s:(s.scenario_id,s.version)):
            if original.publication!=source:raise ValueError('Scenario references another unproven source publication')
            context=original.analysis_context
            if context is not None:
                context.matches(source)
                context=replace(context,publication_id=target_id)
                cost.cycle_window(context)
            candidate=replace(original,publication=target,analysis_context=context)
            baseline=cost.get_tariff(candidate.baseline_tariff_version)
            if baseline is None or baseline.data_scope_id!=target.scope_id:raise ValueError('Migrated scenario baseline is absent or foreign')
            validate_rates(candidate.scenario_version,'EUR',candidate.rates)
            if candidate.rates!=derive_scenario_rates(baseline,candidate.scenario_version,candidate.name,candidate.changes):
                raise ValueError('Source scenario rates differ from its frozen baseline/changes')
            expected[candidate.scenario_version]=candidate
            families.setdefault(candidate.scenario_id,[]).append(candidate.version)
        for versions in families.values():
            if versions!=list(range(1,max(versions)+1)):raise ValueError('Source scenario version chain has gaps')
        existing=cost.list_scenarios(target.scope_id)
        for saved in existing:
            if saved.scenario_id in families and expected.get(saved.scenario_version)!=saved:
                raise ValueError('Target scenario family contains divergent content or extra versions')
        for family in families:
            versions=sorted(s.version for s in existing if s.scenario_id==family)
            if versions!=list(range(1,len(versions)+1)):raise ValueError('Existing import is not a complete version prefix')
        inserted=0
        for candidate in expected.values():
            command=ScenarioCommand(candidate.request_id,candidate.baseline_tariff_version,candidate.name,target,
                candidate.changes,candidate.scenario_id,candidate.analysis_context)
            inserted+=repo._register_configuration_locked('cost_scenario',target.scope_id,candidate.scenario_version,
                'scenario-command:'+candidate.request_id,{'scenario':to_dict(candidate),'command':to_dict(command)})
        for version,candidate in expected.items():
            if cost.get_scenario(version)!=candidate:raise ValueError('Imported scenario readback differs from source mapping')
        mapping={'source_publication':to_dict(source),'target_publication':to_dict(target),
            'source_metadata_hash':source_hash,'parity_proof_hash':digest(proof),
            'scenario_versions':sorted(expected),'mapping_policy':'publication/context ID only; preserve all remaining source scenario fields'}
        identity='postgres:'+source.publication_id+':'+source_hash
        repo._register_configuration_locked('migration_publication_map',target.scope_id,identity,'pg-map:'+identity,mapping)
        return {'status':'imported','scenario_versions':len(expected),'inserted':inserted,'reused':len(expected)-inserted,
            'source_metadata_hash':source_hash,'mapping_hash':digest(mapping),'source_publication':source.publication_id,'target_publication':target_id}


def main():
    import argparse
    from pathlib import Path
    from greenhouse.clickhouse.client import Client,ClickHouseSettings
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('metadata','proof','coordinator'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    result=import_scenarios(Client(ClickHouseSettings.from_env()),args.coordinator,
        json.loads(args.metadata.read_text()),json.loads(args.proof.read_text()))
    print(json.dumps(result))


if __name__=='__main__':main()
