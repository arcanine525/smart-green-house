"""Real native commands after cutoff, with immutable retries and a new revision."""
import argparse
from contextlib import closing
from dataclasses import replace
from datetime import date,datetime,timezone
from decimal import Decimal
import json
import sys

sys.path[:0]=['/cutover-tools','/repro','/migration']
from state import ROOT,RAW,sealed_read
from common import connect,COORDINATOR,write_json
from native_state import selected_configs
from greenhouse.clickhouse.cost import CostAdapter
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schedules import save_drain_profile
from greenhouse.clickhouse.registry import reference
from greenhouse.clickhouse.energy_run import run
from greenhouse.contracts import (CostScenario,EnergyComponent,QueryContext,ScenarioChange,
    ScenarioCommand,to_dict)
from greenhouse.queries.cost import derive_scenario_rates
from greenhouse.app import wiring


def commands():
    archive,_=sealed_read(ROOT/'cutoff.json')
    with closing(connect(action=True)) as client:
        pub=reference(client,archive['publication']['publication_id']);adapter=CostAdapter(client,COORDINATOR)
        cycle=next(c for c in wiring.catalog_service(client).cycles(pub) if c.compartment_id=='Automatoes')
        profile=save_drain_profile(client,COORDINATOR,pub,cycle,name='cutover-rollback',version=1,
            ec_lower='2.1',ec_upper='5.1',ph_lower='5.4',ph_upper='6.6',
            provenance='Cutover rollback rehearsal; user-unverified demonstration limits')
        baseline=adapter.get_baseline_tariff(pub.scope_id)
        ver='tariff:cutover-rollback-20261107:v1'
        tariff=replace(baseline,tariff_version=ver,tariff_id='cutover-rollback-20261107',version=1,is_baseline=False,
            label='Cutover rollback explicit tariff',source_reference='Audited rollback exercise',
            rates=tuple(replace(r,tariff_version=ver,rate_id=ver+':'+r.component.value) for r in baseline.rates))
        assert adapter.register_tariff(tariff)==tariff
        context=QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cycle.cycle_id,('Automatoes',),date(2020,3,2),date(2020,3,4))
        changes=tuple(ScenarioChange(c,'EUR/kWh',change_percent=Decimal('7.5'))
            for c in (EnergyComponent.ELEC_HIGH,EnergyComponent.ELEC_LOW))
        sid='scenario:cutover-rollback-20261107';sv=sid+':v1'
        scenario=CostScenario(sv,sid,1,'cutover-rollback-20261107',tariff.tariff_version,pub,
            'Cutover electricity +7.5%',derive_scenario_rates(tariff,sv,'Cutover electricity +7.5%',changes),
            changes,author='cutover-rehearsal',created_at=datetime.now(timezone.utc),analysis_context=context)
        assert adapter.register_scenario(scenario)==scenario
        assert adapter.register_scenario(scenario)==scenario
        compared=wiring.cost_service(client).compare_scenarios(context,scenario_version=sv)
        result=dict(status='passed',profile=to_dict(profile),tariff=to_dict(tariff),scenario=to_dict(scenario),
            retries_exact=True,baseline_eur=str(compared.baseline.official_total_cost_eur),
            scenario_eur=str(compared.scenario.official_total_cost_eur))
        write_json(ROOT/'native-commands.json',result);print(json.dumps({'stage':'native_commands','status':'passed'}),flush=True)


def correction():
    archive,_=sealed_read(ROOT/'cutoff.json');scope=archive['publication']['scope_id']
    with closing(connect()) as client:
        row=client.query('''SELECT DISTINCT * FROM source_revision WHERE scope_id={scope:String}
            AND record_type='Resources' AND compartment_id='Automatoes'
            AND substring(source_timestamp,1,10)='2020-03-02' AND revision=1''',{'scope':scope})
        if len(row)!=1:raise ValueError('Expected one original resource row')
        previous=row[0];source=json.loads(previous['payload']);old=dict(source['values'])
        source['values']['Irr']=str(Decimal(source['values']['Irr'])+Decimal('1'))
        source['run_id']='correction:cutover-resource-20261107'
        repo=Repository(client,COORDINATOR)
        options=dict(revision=2,correction_id='cutover-resource-20261107',
            correction={'actor':'cutover-rehearsal','evidence':'Explicit +1 Irr for reverse migration proof',
                'previous_revision':previous['revision_id']})
        rid,created=repo.append_record(scope,source,**options)
        assert created and repo.append_record(scope,source,**options)==(rid,False)
        write_json(ROOT/'native-correction.json',dict(status='passed',scope_id=scope,source_record_id=previous['source_record_id'],
            old_values=old,new_values=source['values'],correction_id=options['correction_id'],revision_id=rid,
            previous_revision=previous['revision_id'],retry_exact=True,published=False))
        print(json.dumps({'stage':'native_correction','status':'passed','revision_id':rid}),flush=True)


def rebuild():
    archive,_=sealed_read(ROOT/'cutoff.json');configs=selected_configs(archive['envelope'])
    with closing(connect()) as client:
        result=run(client,COORDINATOR,RAW,ROOT/'native-output',archive['publication']['scope_id'],
            'cutover-corrected-20261107',*(configs[k]['payload'] for k in ('water','harvest','threshold','energy')),
            **{k+'_version':configs[k]['version'] for k in configs},base_load=archive['envelope']['load_id'])
        write_json(ROOT/'native-corrected-publication.json',result)
        print(json.dumps({'stage':'native_corrected_publication','publication_id':result['publication_id']}),flush=True)


def sensor():
    import os
    os.environ['GREENHOUSE_WALKTHROUGH_REPLAY_RUN']='live-replay-cutover'
    sys.path.insert(0,'/walkthrough')
    import live_replay
    live_replay.ROOT=ROOT/'sensor';live_replay.transport.ROOT=live_replay.ROOT
    live_replay.prepare();live_replay.complete()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['commands','correction','rebuild','sensor'])
    globals()[p.parse_args().mode]()
