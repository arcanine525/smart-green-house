"""Water + harvest + climate + drain publication using one pinned source load."""
import argparse
import json
from pathlib import Path

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.encoding import digest
from greenhouse.clickhouse.harvest import HarvestMart,migrate as migrate_harvest
from greenhouse.clickhouse.harvest_run import harvest_configuration
from greenhouse.clickhouse.load import load_snapshot,verified_snapshot
from greenhouse.clickhouse.publication import Publications
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schema import migrate as migrate_core
from greenhouse.clickhouse.threshold import SlotMart,migrate as migrate_threshold,validated_config
from greenhouse.clickhouse.water import WaterMart,migrate as migrate_water
from greenhouse.clickhouse.water_run import water_configuration,corrected_membership,AGC_SNAPSHOT,AGC_SHA


def threshold_configuration(snapshot,compartments,**kwargs):
    water=water_configuration(snapshot,compartments,**kwargs)
    return validated_config({k:v for k,v in water.items() if k not in ('bases','source_unit')})


def run(client,coordinator,raw,output,scope,workflow,water_config,harvest_config,threshold_config,*,
        water_version='water-config-v1',harvest_version='harvest-config-v1',threshold_version='threshold-config-v1'):
    validated_config(threshold_config)
    configs=(('water',water_version,water_config),('harvest',harvest_version,harvest_config),('threshold',threshold_version,threshold_config))
    if any((c['snapshot_id'],c['cycles'])!=(threshold_config['snapshot_id'],threshold_config['cycles']) for _,_,c in configs):
        raise ValueError('Combined bundle scope/cycle mapping must agree')
    for migration in (migrate_core,migrate_water,migrate_harvest,migrate_threshold): migration(client,coordinator)
    repo=Repository(client,coordinator)
    states=Publications(client,coordinator)._states(scope,workflow)
    if states:
        prepared=json.loads(next(r['payload'] for r in states if r['stage']=='PREPARING'))
        for kind,version,payload in configs:
            pins=[p for p in prepared['configuration']['pins'] if (p['kind'],p['version_id'])==(kind,version)]
            if len(pins)!=1 or pins[0]['content_hash']!=digest(payload):
                raise ValueError('Threshold workflow retry changed configuration')
        if verified_snapshot(raw)['snapshot_id']!=threshold_config['snapshot_id']:
            raise ValueError('Threshold workflow retry changed source snapshot')
        result={'resumed':True}; load=prepared['load_id']
    else:
        result=load_snapshot(client,coordinator,raw,output,scope,kinds={'Resources','Production','GreenhouseClimate'},
            climate_window=threshold_config.get('climate_window'))
        for kind,version,payload in configs:
            repo.register_configuration(kind,scope,version,kind+':'+scope+':'+version,payload)
        load=corrected_membership(repo,scope,result['load_id'])
    water=WaterMart(client,coordinator,water_version); harvest=HarvestMart(client,coordinator,harvest_version,load)
    climate=SlotMart(client,coordinator,'climate',threshold_version); drain=SlotMart(client,coordinator,'drain',threshold_version)
    marts={'water':water,'harvest':harvest,'climate':climate,'drain':drain}
    publisher=Publications(client,coordinator,bundles={k:m.verify for k,m in marts.items()})
    def build(scope,pub,core):
        for mart in marts.values(): mart.build(scope,pub,core)
    complete=publisher.publish(scope,workflow,load,build=build)
    return result|{'published':True,'load_id':load,'publication_id':complete['publication_id'],'receipts':complete['receipts']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('raw','output','coordinator'): parser.add_argument('--'+flag,type=Path,required=True)
    for flag in ('scope','workflow-id'): parser.add_argument('--'+flag,required=True)
    parser.add_argument('--agc-shared-basis',action='store_true',required=True)
    parser.add_argument('--climate-start',help='Explicit bounded rehearsal, source-naive ISO datetime')
    parser.add_argument('--climate-end')
    args=parser.parse_args()
    source=verified_snapshot(args.raw)
    if source['snapshot_id']!=AGC_SNAPSHOT or source['archive']['sha256']!=AGC_SHA:
        raise ValueError('AGC defaults require locked snapshot')
    comps=['AICU','Automatoes','Digilog','IUACAAS','Reference','TheAutomators']
    config=threshold_configuration(AGC_SNAPSHOT,comps)
    if bool(args.climate_start)!=bool(args.climate_end): raise ValueError('Both climate bounds required')
    if args.climate_start: config['climate_window']=[args.climate_start,args.climate_end]
    print(json.dumps(run(Client(ClickHouseSettings.from_env()),args.coordinator,args.raw,args.output,args.scope,args.workflow_id,
        water_configuration(AGC_SNAPSHOT,comps,assumed_basis=True),harvest_configuration(AGC_SNAPSHOT,comps,assumed_basis=True),config)))


if __name__=='__main__': main()
