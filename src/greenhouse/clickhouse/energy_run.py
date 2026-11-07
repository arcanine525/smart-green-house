"""Add energy quantities to the combined four-BR publication, or run all at once."""
import argparse
import json
from pathlib import Path

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.energy import EnergyMart,migrate,validated_config
from greenhouse.clickhouse.encoding import digest
from greenhouse.clickhouse.harvest import HarvestMart
from greenhouse.clickhouse.harvest_run import harvest_configuration
from greenhouse.clickhouse.load import load_snapshot,verified_snapshot
from greenhouse.clickhouse.publication import Publications
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schema import migrate as migrate_core
from greenhouse.clickhouse.water import WaterMart,migrate as migrate_water
from greenhouse.clickhouse.harvest import migrate as migrate_harvest
from greenhouse.clickhouse.threshold import SlotMart,migrate as migrate_threshold
from greenhouse.clickhouse.threshold_run import threshold_configuration
from greenhouse.clickhouse.water_run import water_configuration,corrected_membership,AGC_SNAPSHOT,AGC_SHA
from greenhouse.contracts import EffectiveAreaBasis,BasisStatus,ASSUMPTION_LABEL,to_dict
from datetime import date
from decimal import Decimal


def energy_configuration(scope,snapshot,compartments,*,start='2019-12-16',end='2020-05-30',assumed_basis=False):
    config=threshold_configuration(snapshot,compartments,start=start,end=end);config['bases']=[]
    if assumed_basis:
        for comp in compartments:
            for code in ('Heat_cons','ElecHigh','ElecLow','ProdA','ProdB'):
                b=EffectiveAreaBasis(f'AGC_SHARED_BASIS_V1:{comp}:{code}',1,scope,comp,code,Decimal('62.5'),
                    'growing_area_assumed',BasisStatus.ASSUMED,date.fromisoformat(start),date.fromisoformat(end),
                    'docs/implementation/data-decisions.md §7; user adopted shared analysis basis',
                    'AGC_SHARED_BASIS_V1',1,'Shared 62.5 m² analysis basis; not source verification',
                    'Project explicit shared-basis decision',ASSUMPTION_LABEL)
                config['bases'].append(to_dict(b))
    return validated_config(config,scope)


def run(client,coordinator,raw,output,scope,workflow,water_config,harvest_config,threshold_config,energy_config,*,
        water_version='water-config-v1',harvest_version='harvest-config-v1',threshold_version='threshold-config-v1',energy_version='energy-config-v1',base_load=None):
    validated_config(energy_config,scope)
    configs=(('water',water_version,water_config),('harvest',harvest_version,harvest_config),('threshold',threshold_version,threshold_config),('energy',energy_version,energy_config))
    if any((c['snapshot_id'],c['cycles'])!=(energy_config['snapshot_id'],energy_config['cycles']) for _,_,c in configs):raise ValueError('Combined bundle scope/cycles differ')
    for migration in (migrate_core,migrate_water,migrate_harvest,migrate_threshold,migrate):migration(client,coordinator)
    repo=Repository(client,coordinator);states=Publications(client,coordinator)._states(scope,workflow)
    if states:
        prepared=json.loads(next(r['payload'] for r in states if r['stage']=='PREPARING'))
        for kind,version,payload in configs:
            pins=[p for p in prepared['configuration']['pins'] if (p['kind'],p['version_id'])==(kind,version)]
            if len(pins)!=1 or pins[0]['content_hash']!=digest(payload):raise ValueError('Energy retry changed configuration')
        input_pins=[p for p in prepared['configuration']['pins'] if (p['kind'],p['version_id'])==('energy_workflow',workflow)]
        if len(input_pins)!=1 or json.loads(input_pins[0]['payload'])!={'base_load':base_load}:
            raise ValueError('Energy retry changed base load selection')
        if verified_snapshot(raw)['snapshot_id']!=energy_config['snapshot_id']:raise ValueError('Energy retry changed snapshot')
        load=prepared['load_id'];result={'resumed':True}
    else:
        if base_load:
            repo.load_membership(scope,base_load);audit=repo.load_audit(scope,base_load)
            for row in audit['files']:
                if row['source_file'].endswith('/GreenhouseClimate.csv'):
                    selected=json.loads(row['metadata'])
                    if not selected.get('core_selected') or selected.get('climate_window')!=threshold_config.get('climate_window'):
                        raise ValueError('Base load climate selection differs from declared configuration')
            result={'reused_load':True};load=base_load
        else:
            result=load_snapshot(client,coordinator,raw,output,scope,kinds={'Resources','Production','GreenhouseClimate'},climate_window=threshold_config.get('climate_window'))
            load=result['load_id']
        for kind,version,payload in configs:repo.register_configuration(kind,scope,version,kind+':'+scope+':'+version,payload)
        load=corrected_membership(repo,scope,load)
        repo.register_configuration('energy_workflow',scope,workflow,'energy-workflow:'+scope+':'+workflow,{'base_load':base_load})
    # Harvest is built before energy, which verifies common frozen basis identities.
    marts={'water':WaterMart(client,coordinator,water_version),'harvest':HarvestMart(client,coordinator,harvest_version,load),
        'climate':SlotMart(client,coordinator,'climate',threshold_version),'drain':SlotMart(client,coordinator,'drain',threshold_version),'energy':EnergyMart(client,coordinator,energy_version)}
    publisher=Publications(client,coordinator,bundles={k:m.verify for k,m in marts.items()})
    def build(scope,pub,core):
        for mart in marts.values():mart.build(scope,pub,core)
    complete=publisher.publish(scope,workflow,load,build=build)
    return result|{'published':True,'load_id':load,'publication_id':complete['publication_id'],'receipts':complete['receipts']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('raw','output','coordinator'):parser.add_argument('--'+flag,type=Path,required=True)
    for flag in ('scope','workflow-id'):parser.add_argument('--'+flag,required=True)
    parser.add_argument('--agc-shared-basis',action='store_true',required=True)
    parser.add_argument('--energy-version',default='energy-config-v1')
    parser.add_argument('--base-load',help='Reuse an explicit frozen load; no implicit current pointer')
    parser.add_argument('--climate-start');parser.add_argument('--climate-end')
    parser.add_argument('--register-agc-tariff',action='store_true')
    args=parser.parse_args();source=verified_snapshot(args.raw)
    if source['snapshot_id']!=AGC_SNAPSHOT or source['archive']['sha256']!=AGC_SHA:raise ValueError('AGC defaults require locked snapshot')
    comps=['AICU','Automatoes','Digilog','IUACAAS','Reference','TheAutomators'];threshold=threshold_configuration(AGC_SNAPSHOT,comps)
    if bool(args.climate_start)!=bool(args.climate_end):raise ValueError('Both climate bounds required')
    if args.climate_start:threshold['climate_window']=[args.climate_start,args.climate_end]
    client=Client(ClickHouseSettings.from_env())
    result=run(client,args.coordinator,args.raw,args.output,args.scope,args.workflow_id,water_configuration(AGC_SNAPSHOT,comps,assumed_basis=True),
        harvest_configuration(AGC_SNAPSHOT,comps,assumed_basis=True),threshold,energy_configuration(args.scope,AGC_SNAPSHOT,comps,assumed_basis=True),base_load=args.base_load,energy_version=args.energy_version)
    if args.register_agc_tariff:
        from greenhouse.clickhouse.cost import CostAdapter
        result['tariff_version']=CostAdapter(client,args.coordinator).register_historical_tariff(args.scope).tariff_version
    print(json.dumps(result))


if __name__=='__main__':main()
