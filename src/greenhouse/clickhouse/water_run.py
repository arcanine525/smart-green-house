"""BR-02 candidate workflow. Explicit config and scope; no PostgreSQL dependency."""
import argparse
import json
from pathlib import Path

from greenhouse.clickhouse.client import Client, ClickHouseSettings
from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.encoding import canonical_json, digest
from greenhouse.clickhouse.load import load_snapshot
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.water import WaterMart, migrate, validated_config
from greenhouse.clickhouse.quality import AGC_QUALITY_DEFINITION

AGC_SHA='c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68'
AGC_SNAPSHOT='agc-2nd-2019-'+AGC_SHA


def water_configuration(snapshot_id, compartments, *, start='2019-12-16',end='2020-05-30', assumed_basis=False):
    cycles=[{'cycle_id':'agc-cycle-'+c,'compartment_id':c,'compartment_name':c,'start':start,'end':end} for c in compartments]
    bases=[]
    if assumed_basis:
        for compartment in compartments:
            for measure in ('Irr','Drain'):
                bases.append({'compartment_id':compartment,'measure_code':measure,'start':start,'end':end,
                    'metadata':{'basis_status':'assumed_unverified',
                        'basis_version_id':f'AGC_SHARED_BASIS_V1:{compartment}:{measure}',
                        'area_m2':'62.5','basis_kind':'growing_area_assumed',
                        'assumption_id':'AGC_SHARED_BASIS_V1','assumption_version':1,
                        'basis_label':'giả định chưa xác minh'}})
    return validated_config({'snapshot_id':snapshot_id,'source_unit':'L/m²/day',
        'quality_policy_version':'AGC_QUALITY_V1','parser_version':'agc-parser-v1',
        'quality_definition':{k:AGC_QUALITY_DEFINITION[k] for k in ('nonnegative','suspect_zero','off_mode_zero')},
        'cycles':cycles,'bases':bases,
        'evidence':'data-decisions §7; user adopted shared basis, resource extension unverified' if assumed_basis else 'explicit configuration; basis unknown'})


def corrected_membership(repo,scope,load):
    """Freeze latest revisions of the same source keys; preserve original load."""
    with writer_lock(repo.coordinator,repo.client):
        base=repo.load_membership(scope,load)
        latest=repo.canonical_records(scope)
        key=lambda r:(r['record_type'],r['compartment_id'],r['logical_key'])
        keys={key(r) for r in base}
        chosen=[r for r in latest if key(r) in keys]
        ids=sorted(r['revision_id'] for r in chosen)
        if ids==sorted(r['revision_id'] for r in base):
            return load
        new_load=digest([load,ids])
        repo._insert('load_member',[{'scope_id':scope,'load_id':new_load,'revision_id':i} for i in ids])
        audit=repo.load_audit(scope,load)
        repo._insert('load_file',[{'scope_id':scope,'load_id':new_load}|r for r in audit['files']])
        repo._insert('load_issue',[{'scope_id':scope,'load_id':new_load}|r for r in audit['issues']])
        repo._insert('load_receipt',[{'scope_id':scope,'load_id':new_load,'schema_version':'ch-core-v1',
            'input_hash':digest({'base_load':load,'revisions':ids}),
            'counts_json':canonical_json({'canonical':len(ids),'base_load':load}),
            'membership_checksum':digest(ids),'membership_count':len(ids),'audit_checksum':audit['checksum']}])
        return new_load


def run(client,coordinator,raw,output,scope,workflow,config,*,config_version='water-config-v1'):
    validated_config(config)
    from greenhouse.clickhouse.schema import migrate as migrate_core
    from greenhouse.clickhouse.load import verified_snapshot
    migrate_core(client,coordinator)
    migrate(client,coordinator)
    mart=WaterMart(client,coordinator,config_version)
    states=mart.publications()._states(scope,workflow)
    if states:
        prepared=json.loads(next(r['payload'] for r in states if r['stage']=='PREPARING'))
        pin=[r for r in prepared['configuration']['pins'] if r['kind']=='water' and r['version_id']==config_version]
        if len(pin)!=1 or pin[0]['content_hash']!=digest(config):
            raise ValueError('Workflow retry changed water configuration')
        manifest=verified_snapshot(raw)
        if manifest['snapshot_id']!=config['snapshot_id']:
            raise ValueError('Workflow retry changed raw snapshot')
        publication=mart.publications().publish(scope,workflow,prepared['load_id'],build=mart.build)
        return {'published':True,'resumed':True,'load_id':prepared['load_id'],
            'publication_id':publication['publication_id'],'receipts':publication['receipts']}
    result=load_snapshot(client,coordinator,raw,output,scope,kinds={'Resources'})
    repo=Repository(client,coordinator)
    repo.register_configuration('water',scope,config_version,'water:'+scope+':'+config_version,config)
    migrate(client,coordinator)
    load=corrected_membership(repo,scope,result['load_id'])
    mart=WaterMart(client,coordinator,config_version)
    publication=mart.publications().publish(scope,workflow,load,build=mart.build)
    return result | {'published':True,'publication_id':publication['publication_id'],
        'load_id':load,'receipts':publication['receipts']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('raw','output','coordinator'):
        parser.add_argument('--'+flag,type=Path,required=True)
    for flag in ('scope','workflow-id'):
        parser.add_argument('--'+flag,required=True)
    choice=parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--config-json',type=Path)
    choice.add_argument('--agc-shared-basis',action='store_true',help='Adopt explicit unverified shared 62.5 m² basis')
    args=parser.parse_args()
    if args.config_json:
        config=json.loads(args.config_json.read_text())
    else:
        manifest=json.loads((args.raw/'manifest.json').read_text())
        if manifest['snapshot_id']!=AGC_SNAPSHOT or manifest['archive']['sha256']!=AGC_SHA:
            raise ValueError('AGC defaults require the locked snapshot')
        config=water_configuration(AGC_SNAPSHOT,['AICU','Automatoes','Digilog','IUACAAS','Reference','TheAutomators'],assumed_basis=True)
    print(json.dumps(run(Client(ClickHouseSettings.from_env()),args.coordinator,args.raw,args.output,
        args.scope,args.workflow_id,config),ensure_ascii=False))


if __name__=='__main__':
    main()
