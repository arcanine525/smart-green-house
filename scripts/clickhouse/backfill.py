"""Locked AGC backfill using a read-only PG metadata export and bounded marts.

This creates a candidate only. Scenario/publication mapping and parity must be
verified separately before cutover; no PostgreSQL connection is used here.
"""
import argparse
import json
from pathlib import Path
import zipfile

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.cli import migrate_all
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.energy_run import run
from greenhouse.clickhouse.cost import CostAdapter
from greenhouse.clickhouse.schedules import register_rule_schedule
from greenhouse.clickhouse.encoding import digest
from greenhouse.contracts import EffectiveAreaBasis,RuleScheduleRevision,HarvestRuleProfile,IrrigationRuleProfile,TariffProfile,from_dict
from greenhouse.ingestion.snapshot import verify_archive,validate_snapshot,archive_members


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('metadata','raw','archive','output','coordinator'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--workflow-id',required=True)
    args=parser.parse_args();metadata=json.loads(args.metadata.read_text())
    if metadata['format']!=1 or metadata['backend']!='postgres' or not metadata['read_only']:
        raise ValueError('Read-only source metadata export required')
    source=metadata['source_publication'];scope=source['scope_id'];config=metadata['configurations']
    verify_archive(args.archive)
    with zipfile.ZipFile(args.archive) as archive:
        manifest=validate_snapshot(args.raw,archive,archive_members(archive))
    if source['snapshot_id']!=manifest['snapshot_id'] or any(c['snapshot_id']!=source['snapshot_id'] for c in config.values()):
        raise ValueError('Source publication, metadata config and raw snapshot must agree')
    if config['threshold'].get('climate_window') or config['threshold'].get('storage_version')!='threshold-stream-v2':
        raise ValueError('Full backfill requires full source selection and bounded mart version')
    print(json.dumps({'stage':'source_verified','source_publication':source['publication_id'],'metadata_hash':digest(metadata)}),flush=True)
    client=Client(ClickHouseSettings.from_env());migrate_all(client,args.coordinator);repo=Repository(client,args.coordinator)
    repo.register_configuration('migration_source',scope,'postgres:'+source['publication_id'],'pg-source:'+digest(metadata),metadata)
    for value in metadata['bases']:
        basis=from_dict(EffectiveAreaBasis,value)
        repo.register_configuration('area_basis',scope,basis.basis_version_id,'pg-basis:'+basis.basis_version_id,value)
    for value in metadata['rules']:
        register_rule_schedule(client,args.coordinator,from_dict(RuleScheduleRevision,value))
    for kind,cls,key in (('harvest_rule',HarvestRuleProfile,'harvest_rules'),('irrigation_rule',IrrigationRuleProfile,'irrigation_rules')):
        for value in metadata[key]:
            rule=from_dict(cls,value)
            repo.register_configuration(kind,scope,rule.rule_version,'pg-'+kind+':'+rule.rule_version,value)
    costs=CostAdapter(client,args.coordinator)
    for value in metadata['tariffs']:costs.register_tariff(from_dict(TariffProfile,value))
    print(json.dumps({'stage':'metadata_archived','basis_versions':len(metadata['bases']),'rule_versions':len(metadata['rules']),
        'tariff_versions':len(metadata['tariffs']),'pending_scenario_versions':len(metadata['scenarios'])}),flush=True)
    result=run(client,args.coordinator,args.raw,args.output,scope,args.workflow_id,
        *(config[k] for k in ('water','harvest','threshold','energy')),
        water_version='pg-water-v1',harvest_version='pg-harvest-v1',threshold_version='pg-threshold-stream-v2',energy_version='pg-energy-v1')
    print(json.dumps({'stage':'pipeline_complete',**result}),flush=True)


if __name__=='__main__':main()
