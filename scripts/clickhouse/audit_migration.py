"""Audit native version inventory against an archived read-only source export.

This complements (and never replaces) the full publication/KPI parity check.
Output contains counts and fingerprints, not the original metadata dump.
"""
import argparse
from collections import Counter
import json
from pathlib import Path

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.encoding import digest
from greenhouse.clickhouse.repository import Repository
from greenhouse.contracts import CostScenario,from_dict,to_dict


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('metadata','output'):parser.add_argument('--'+key,type=Path,required=True)
    for key in ('publication-id','load-id'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args();metadata=json.loads(args.metadata.read_text())
    if metadata.get('read_only') is not True or metadata.get('backend')!='postgres':
        raise ValueError('Read-only PostgreSQL source export required')
    client=Client(ClickHouseSettings.from_env());repo=Repository(client,None)
    rows=repo.configurations()['rows'];scope=metadata['source_publication']['scope_id']
    index={(r['kind'],r['version_id']):json.loads(r['payload']) for r in rows if r['scope_id']==scope}
    if index.get(('migration_source','postgres:'+metadata['source_publication']['publication_id']))!=metadata:
        raise ValueError('Archived source metadata differs')
    inventory={}
    for kind,source,key in (('area_basis','bases','basis_version_id'),('rule_schedule','rules','rule_version'),
            ('harvest_rule','harvest_rules','rule_version'),('irrigation_rule','irrigation_rules','rule_version'),
            ('tariff','tariffs','tariff_version')):
        expected={value[key]:value for value in metadata[source]}
        for version,value in expected.items():
            if index.get((kind,version))!=value:raise ValueError('Native configuration differs: '+kind+' '+version)
        inventory[kind]={'versions':len(expected),'source_content_hash':digest(expected),'exact_native_match':True}
    # Imported scenarios map only publication/context identity. Every remaining
    # field (including author/time/request/version and exact rates) must match.
    families=set()
    for original in metadata['scenarios']:
        source=from_dict(CostScenario,original);families.add(source.scenario_id)
        envelope=index.get(('cost_scenario',source.scenario_version))
        if envelope is None:raise ValueError('Scenario version missing from native inventory')
        candidate=from_dict(CostScenario,envelope['scenario'])
        if candidate.publication.publication_id!=args.publication_id:
            raise ValueError('Scenario missing from target publication')
        mapped=to_dict(candidate);mapped['publication']=original['publication']
        if mapped['analysis_context'] is not None:
            mapped['analysis_context']['publication_id']=original['analysis_context']['publication_id']
        if mapped!=original:raise ValueError('Scenario field differs from frozen original')
    inventory['scenario']={'versions':len(metadata['scenarios']),'families':len(families),'exact_mapped_match':True}
    audit=repo.load_audit(scope,args.load_id)
    files=[{'source_file':r['source_file'],**json.loads(r['metadata'])} for r in audit['files']]
    counts=client.query('''SELECT record_type,count() AS rows,uniqExact(revision_id) AS unique_revisions
        FROM source_revision WHERE scope_id={scope:String} GROUP BY record_type ORDER BY record_type''',{'scope':scope})
    issues=Counter(r['disposition'] for r in audit['issues'])
    result={'status':'passed','source_publication':metadata['source_publication']['publication_id'],
        'target_publication':args.publication_id,'load_id':args.load_id,'source_metadata_hash':digest(metadata),
        'source_table_counts':{key:len(value) for key,value in metadata['tables'].items()},
        'native_configuration_inventory':inventory,'load_audit_checksum':audit['checksum'],'audited_source_files':len(files),
        'source_revisions':counts,'load_issue_dispositions':dict(issues),
        'source_sensor_schema':'not part of metadata export; audit source information_schema separately',
        'scope':scope}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
