import copy
import csv
from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
import unittest
from uuid import uuid4

import test_water as fixtures
from greenhouse.clickhouse.energy_run import run,energy_configuration
from greenhouse.clickhouse.threshold_run import threshold_configuration
from greenhouse.clickhouse.harvest_run import harvest_configuration
from greenhouse.clickhouse.energy import EnergyAdapter
from greenhouse.clickhouse.harvest import HarvestAdapter
from greenhouse.clickhouse.cost import CostAdapter
from greenhouse.clickhouse.repository import Repository
from greenhouse.contracts import QueryContext,ScenarioCommand,ScenarioChange,EnergyComponent
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries.energy import EnergyService
from greenhouse.queries.intensity import EnergyIntensityService
from greenhouse.queries.cost import EnergyCostService


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class EnergyTest(unittest.TestCase):
    setUp=fixtures.WaterTest.setUp

    def prepare(self,*,unknown_basis=False,missing_heat=False,zero_a=False):
        resource=self.raw/'Automatoes/Resources.csv'
        with resource.open('w',newline='') as stream:
            w=csv.writer(stream);w.writerow(HEADERS['Resources'])
            for i in range(5):w.writerow([str(43892+i),'invalid' if missing_heat and i==2 else '36','2','3','4','10','2'])
        prod=self.raw/'Automatoes/Production.csv'
        with prod.open('w',newline='') as stream:
            w=csv.writer(stream);w.writerow(HEADERS['Production']);w.writerow(['43892','0' if zero_a else '1','.5']+['0']*6);w.writerow(['43894','0' if zero_a else '2','.25']+['0']*6)
        manifest=json.loads((self.raw/'manifest.json').read_text());manifest['files']=[{'path':'Automatoes/'+p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'csv':{'data_rows':n}} for p,n in ((resource,5),(prod,2))]
        (self.raw/'manifest.json').write_text(json.dumps(manifest))
        args=dict(start='2020-03-02',end='2020-03-07')
        self.harvest=harvest_configuration(self.snapshot,['Automatoes'],assumed_basis=True,**args)
        self.threshold=threshold_configuration(self.snapshot,['Automatoes'],**args)
        self.energy_config=energy_configuration(self.scope,self.snapshot,['Automatoes'],assumed_basis=True,**args)
        if unknown_basis:self.energy_config['bases']=[b for b in self.energy_config['bases'] if b['measure_code'] in ('ProdA','ProdB')]
        result=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config,self.harvest,self.threshold,self.energy_config)
        adapter=EnergyAdapter(self.client,raw_snapshots={self.snapshot:self.raw});pub=adapter.pin_publication(self.scope)
        context=QueryContext(self.scope,pub.publication_id,self.snapshot,'agc-cycle-Automatoes',('Automatoes',),date(2020,3,2),date(2020,3,7))
        energy=EnergyService(adapter);cost=EnergyCostService(energy,CostAdapter(self.client,self.root))
        tariff=cost.register_historical_tariff(self.scope)
        return result,energy,cost,tariff,pub,context

    def command(self,tariff,pub,context,*,request=None,scenario_id=None):
        changes=tuple(ScenarioChange(c,'EUR/kWh',change_percent=Decimal(20)) for c in (EnergyComponent.ELEC_HIGH,EnergyComponent.ELEC_LOW))
        return ScenarioCommand(request or uuid4().hex,tariff.tariff_version,'Electricity +20%',pub,changes,scenario_id,context)

    def test_quantities_intensity_and_price_scenario_share_exact_frozen_inputs(self):
        result,energy,cost,tariff,pub,context=self.prepare()
        summary=energy.get_summary(context)
        self.assertEqual(summary.official_total_kwh,Decimal('4687.5'))
        self.assertEqual(summary.official_total_kwh_m2,Decimal('75'))
        self.assertEqual(summary.complete_coverage_pct,Decimal(100))
        intensity=EnergyIntensityService(energy,HarvestAdapter(self.client)).get_intensity(context)
        self.assertEqual(intensity.official_a_kg,Decimal('187.5'))
        self.assertEqual(intensity.intensity_kwh_kg_a,Decimal(25))
        self.assertTrue(intensity.assumptions)
        baseline=cost.get_cost(context,tariff_version=tariff.tariff_version)
        self.assertEqual(baseline.official_total_cost_per_m2,Decimal('2.894'))
        self.assertEqual(baseline.official_total_cost_eur,Decimal('180.875'))
        command=self.command(tariff,pub,context);scenario=cost.execute_scenario_command(command)
        comparison=cost.compare_scenarios(context,scenario_version=scenario.scenario_version,baseline_tariff_version=tariff.tariff_version)
        self.assertEqual(comparison.scenario.official_total_cost_per_m2,Decimal('3.174'))
        self.assertEqual(comparison.scenario.official_total_cost_eur,Decimal('198.375'))
        self.assertEqual(comparison.delta_total_cost_eur,Decimal('17.5'))
        self.assertEqual(cost.execute_scenario_command(command),scenario)
        reopened=CostAdapter(self.client,self.root).get_scenario(scenario.scenario_version)
        self.assertEqual(reopened,scenario)
        self.assertEqual(len(cost.list_scenarios(self.scope)),1)
        self.assertEqual(len(cost.compare_scenarios_weekly(comparison)),1)
        row=energy.get_quantities(context)[0]
        self.assertTrue(energy.get_lineage(pub,revision_id=row.revision_id).raw_hash_verified)
        from greenhouse.app import cost_exports
        self.assertTrue(cost_exports.summary_csv(comparison,scenario))
        with self.assertRaises(ValueError):cost.execute_scenario_command(replace(command,name='changed content'))
        second=cost.append_scenario_version(scenario.scenario_id,replace(command,request_id=uuid4().hex,scenario_id=scenario.scenario_id,name='New revision'))
        self.assertEqual(second.version,2);self.assertEqual(cost.get_scenario(scenario.scenario_version),scenario)
        retry=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config,self.harvest,self.threshold,self.energy_config)
        self.assertEqual(retry['publication_id'],result['publication_id'])

    def test_missing_energy_input_and_zero_production_do_not_become_ratios(self):
        _,energy,cost,tariff,pub,context=self.prepare(missing_heat=True,zero_a=True)
        summary=energy.get_summary(context)
        self.assertIsNone(summary.official_total_kwh);self.assertEqual(summary.complete_days,4)
        intensity=EnergyIntensityService(energy,HarvestAdapter(self.client)).get_intensity(context)
        self.assertEqual(intensity.official_a_kg,Decimal(0));self.assertIsNone(intensity.intensity_kwh_kg_a)
        self.assertIsNone(cost.get_cost(context,tariff_version=tariff.tariff_version).official_total_cost_eur)
        quantities=energy.get_quantities(context)
        bad=next(q for q in quantities if q.resource_date==date(2020,3,4) and q.component==EnergyComponent.HEAT)
        self.assertEqual(bad.quality.value,'invalid');self.assertIsNone(bad.absolute_kwh)
        self.assertEqual(next(q for q in quantities if q.resource_date==date(2020,3,4) and q.component==EnergyComponent.ELEC_HIGH).absolute_kwh,Decimal(125))

    def test_unknown_area_preserves_source_quantities_without_absolute_claim(self):
        _,energy,cost,tariff,pub,context=self.prepare(unknown_basis=True)
        summary=energy.get_summary(context)
        self.assertIsNone(summary.official_total_kwh)
        self.assertEqual(summary.components[0].observed_source_per_m2,Decimal(180))
        self.assertIsNone(EnergyIntensityService(energy,HarvestAdapter(self.client)).get_intensity(context).intensity_kwh_kg_a)
        baseline=cost.get_cost(context,tariff_version=tariff.tariff_version)
        self.assertIsNone(baseline.official_total_cost_eur)

    def test_scenario_failure_and_lost_ack_do_not_create_partial_or_duplicate_versions(self):
        from unittest.mock import patch
        from greenhouse.queries.cost import derive_scenario_rates
        _,energy,cost,tariff,pub,context=self.prepare()
        command=self.command(tariff,pub,context)
        with self.assertRaisesRegex(ValueError,'complete derived rates'):
            cost.adapter.execute_scenario_transaction(command,lambda version:derive_scenario_rates(tariff,version,command.name,command.changes)[:2])
        self.assertIsNone(cost.adapter.get_scenario_by_request_id(command.request_id))
        insert=cost.adapter.repo._insert
        def lose_ack(table,rows,**kwargs):
            result=insert(table,rows,**kwargs)
            if table=='configuration_envelope' and any(r['kind']=='cost_scenario' for r in rows):
                raise RuntimeError('Injected loss after complete scenario INSERT')
            return result
        with patch.object(cost.adapter.repo,'_insert',side_effect=lose_ack):
            with self.assertRaisesRegex(RuntimeError,'Injected loss'):
                cost.execute_scenario_command(command)
        # Complete envelope is readable; retry returns exactly it, not version 2.
        saved=cost.adapter.get_scenario_by_request_id(command.request_id)
        self.assertIsNotNone(saved);self.assertEqual(len(saved.rates),3)
        self.assertEqual(cost.execute_scenario_command(command),saved)
        self.assertEqual(saved.version,1);self.assertEqual(len(cost.list_scenarios(self.scope)),1)
        invalid=replace(saved,scenario_id='bad-import:'+self.scope,scenario_version='bad-import:'+self.scope+':v99',version=99,request_id='invalid-import:'+self.scope)
        with self.assertRaises(ValueError):cost.adapter.register_scenario(invalid)
        self.assertIsNone(cost.adapter.get_scenario_by_request_id(invalid.request_id))

    def test_concurrent_commands_serialize_request_and_scenario_version_allocation(self):
        from concurrent.futures import ThreadPoolExecutor
        from greenhouse.clickhouse.client import Client
        _,energy,cost,tariff,pub,context=self.prepare()
        command=self.command(tariff,pub,context)
        def execute(cmd):
            client=Client(self.client.settings)
            service=EnergyCostService(EnergyService(EnergyAdapter(client)),CostAdapter(client,self.root))
            return service.execute_scenario_command(cmd)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first,duplicate=tuple(pool.map(execute,(command,command)))
        self.assertEqual(first,duplicate);self.assertEqual(first.version,1)
        c2=replace(command,request_id=uuid4().hex,scenario_id=first.scenario_id,name='Revision A')
        c3=replace(c2,request_id=uuid4().hex,name='Revision B')
        with ThreadPoolExecutor(max_workers=2) as pool:
            versions=tuple(pool.map(execute,(c2,c3)))
        self.assertEqual(sorted(s.version for s in versions),[2,3])
        self.assertEqual(len(cost.list_scenarios(self.scope)),3)
        self.assertEqual(cost.get_scenario(first.scenario_version),first)

    def test_energy_correction_pins_old_values_and_shared_basis_drift_is_refused(self):
        result,energy,cost,tariff,pub,context=self.prepare()
        repo=Repository(self.client,self.root)
        original=next(r for r in repo.load_membership(self.scope,result['load_id']) if r['record_type']=='Resources' and r['logical_key']=='2020-03-02')
        record=json.loads(original['payload']);record['run_id']='energy-correction';record['values']['Heat_cons']='72'
        repo.append_record(self.scope,record,revision=2,correction_id='heat-correction:'+self.scope,correction={'actor':'reviewer','evidence':'Synthetic heat correction','previous_revision':original['revision_id']})
        newer=run(self.client,self.root,self.raw,self.root/'output',self.scope,uuid4().hex,self.config,self.harvest,self.threshold,self.energy_config)
        self.assertEqual(energy.get_summary(context).official_total_kwh,Decimal('4687.5'))
        changed=EnergyService(EnergyAdapter(self.client)).get_summary(replace(context,publication_id=newer['publication_id']))
        self.assertEqual(changed.official_total_kwh,Decimal('5312.5'))
        self.assertEqual(cost.get_tariff(tariff.tariff_version),tariff)
        # The explicit original load remains the retry input after selecting corrections.
        workflow=uuid4().hex
        chosen=run(self.client,self.root,self.raw,self.root/'output',self.scope,workflow,self.config,self.harvest,self.threshold,self.energy_config,base_load=result['load_id'])
        resumed=run(self.client,self.root,self.raw,self.root/'output',self.scope,workflow,self.config,self.harvest,self.threshold,self.energy_config,base_load=result['load_id'])
        self.assertEqual(chosen['publication_id'],resumed['publication_id'])
        self.assertNotEqual(chosen['load_id'],result['load_id'])
        drift=copy.deepcopy(self.energy_config)
        for b in drift['bases']:
            if b['measure_code']=='ProdA':b['area_m2']='96'
        with self.assertRaisesRegex(ValueError,'basis identity has different content'):
            run(self.client,self.root,self.raw,self.root/'output',self.scope,uuid4().hex,self.config,self.harvest,self.threshold,drift,energy_version='bad-basis')
        self.assertEqual(EnergyAdapter(self.client).publication(pub.publication_id),pub)
