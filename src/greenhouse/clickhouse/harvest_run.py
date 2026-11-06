"""Combined water/harvest workflow, preserving prior water-only publications."""
import argparse
import copy
import json
from pathlib import Path

from greenhouse.clickhouse.client import Client, ClickHouseSettings
from greenhouse.clickhouse.harvest import HarvestMart, migrate as migrate_harvest, validated_config
from greenhouse.clickhouse.load import load_snapshot, verified_snapshot
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schema import migrate as migrate_core
from greenhouse.clickhouse.water import WaterMart, migrate as migrate_water
from greenhouse.clickhouse.water_run import water_configuration, corrected_membership, AGC_SNAPSHOT, AGC_SHA
from greenhouse.clickhouse.publication import Publications
from greenhouse.clickhouse.encoding import digest


def harvest_configuration(snapshot_id,compartments,**kwargs):
    water=water_configuration(snapshot_id,compartments,**kwargs)
    config=copy.deepcopy(water); config['source_unit']='kg/m²/harvest'; config['bases']=[]
    for basis in water['bases']:
        for measure in ('ProdA','ProdB'):
            if basis['measure_code']!='Irr':
                continue
            selected=copy.deepcopy(basis); selected['measure_code']=measure
            selected['metadata']['basis_version_id']=selected['metadata']['basis_version_id'].removesuffix(':Irr')+':'+measure
            config['bases'].append(selected)
    return validated_config(config)


def run(client,coordinator,raw,output,scope,workflow,water_config,harvest_config,*,water_version='water-config-v1',harvest_version='harvest-config-v1'):
    validated_config(harvest_config)
    if water_config['cycles']!=harvest_config['cycles'] or water_config['snapshot_id']!=harvest_config['snapshot_id']:
        raise ValueError('Water and harvest scope/cycle mapping must agree')
    migrate_core(client,coordinator); migrate_water(client,coordinator); migrate_harvest(client,coordinator)
    repo=Repository(client,coordinator)
    existing=Publications(client,coordinator)._states(scope,workflow)
    if existing:
        prepared=json.loads(next(r['payload'] for r in existing if r['stage']=='PREPARING'))
        for kind,version,payload in (('water',water_version,water_config),('harvest',harvest_version,harvest_config)):
            pins=[p for p in prepared['configuration']['pins'] if (p['kind'],p['version_id'])==(kind,version)]
            if len(pins)!=1 or pins[0]['content_hash']!=digest(payload):
                raise ValueError('Combined workflow retry changed configuration')
        if verified_snapshot(raw)['snapshot_id']!=water_config['snapshot_id']:
            raise ValueError('Combined workflow retry changed snapshot')
        result={'resumed':True}; load=prepared['load_id']
    else:
        result=load_snapshot(client,coordinator,raw,output,scope,kinds={'Resources','Production'})
        repo.register_configuration('water',scope,water_version,'water:'+scope+':'+water_version,water_config)
        repo.register_configuration('harvest',scope,harvest_version,'harvest:'+scope+':'+harvest_version,harvest_config)
        load=corrected_membership(repo,scope,result['load_id'])
    water=WaterMart(client,coordinator,water_version)
    harvest=HarvestMart(client,coordinator,harvest_version,load)
    publisher=Publications(client,coordinator,bundles={'water':water.verify,'harvest':harvest.verify})
    def build(scope,publication,core):
        water.build(scope,publication,core); harvest.build(scope,publication,core)
    complete=publisher.publish(scope,workflow,load,build=build)
    return result|{'published':True,'publication_id':complete['publication_id'],'load_id':load,'receipts':complete['receipts']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('raw','output','coordinator'):
        parser.add_argument('--'+flag,type=Path,required=True)
    for flag in ('scope','workflow-id'):
        parser.add_argument('--'+flag,required=True)
    parser.add_argument('--agc-shared-basis',action='store_true',required=True)
    args=parser.parse_args()
    source=verified_snapshot(args.raw)
    if source['snapshot_id']!=AGC_SNAPSHOT or source['archive']['sha256']!=AGC_SHA:
        raise ValueError('AGC defaults require locked snapshot')
    compartments=['AICU','Automatoes','Digilog','IUACAAS','Reference','TheAutomators']
    print(json.dumps(run(Client(ClickHouseSettings.from_env()),args.coordinator,args.raw,args.output,args.scope,args.workflow_id,
        water_configuration(AGC_SNAPSHOT,compartments,assumed_basis=True),harvest_configuration(AGC_SNAPSHOT,compartments,assumed_basis=True)),ensure_ascii=False))


if __name__=='__main__':
    main()
