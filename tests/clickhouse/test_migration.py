"""Synthetic migration protocol: no claim about real AGC or a six-BR gate."""
import copy
from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal
import os
import unittest
from uuid import uuid4

import test_energy as fixtures
from greenhouse.clickhouse.migration import import_scenarios
from greenhouse.clickhouse.energy_run import run
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.cost import CostAdapter
from greenhouse.contracts import CostScenario,ScenarioChange,EnergyComponent,to_dict
from greenhouse.queries.cost import derive_scenario_rates


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class MigrationTest(unittest.TestCase):
    setUp=fixtures.EnergyTest.setUp
    prepare=fixtures.EnergyTest.prepare

    def test_preserve_source_versions_request_author_time_retry_and_reject_bad_proof(self):
        _,_,_,tariff,original,context=self.prepare()
        source=replace(original,publication_id='pg-fixture:'+uuid4().hex)
        source_context=replace(context,publication_id=source.publication_id)
        scenarios=[];family='pg-source-family:'+uuid4().hex
        for version,percent in ((1,20),(2,30)):
            changes=tuple(ScenarioChange(c,'EUR/kWh',change_percent=Decimal(percent)) for c in (EnergyComponent.ELEC_HIGH,EnergyComponent.ELEC_LOW))
            identity=family+':v'+str(version);name='Source price version '+str(version)
            scenarios.append(CostScenario(identity,family,version,'pg-request:'+identity,tariff.tariff_version,source,name,
                derive_scenario_rates(tariff,identity,name,changes),changes,author='source fixture author',
                created_at=datetime(2020,1,1,tzinfo=timezone.utc),analysis_context=source_context))
        metadata={'format':1,'backend':'postgres','read_only':True,'source_publication':to_dict(source),'scenarios':[to_dict(s) for s in scenarios]}
        Repository(self.client,self.root).register_configuration('migration_source',self.scope,'postgres:'+source.publication_id,'source:'+self.scope,metadata)
        target=run(self.client,self.root,self.raw,self.root/'output',self.scope,uuid4().hex,self.config,self.harvest,self.threshold,self.energy_config)
        proof={'status':'passed','all_six_brs':True,'postgres_publication':source.publication_id,'clickhouse_publication':target['publication_id']}
        with self.assertRaises(ValueError):import_scenarios(self.client,self.root,metadata,dict(proof,status='in_progress'))
        result=import_scenarios(self.client,self.root,metadata,proof)
        self.assertEqual((result['scenario_versions'],result['inserted']),(2,2))
        self.assertEqual(import_scenarios(self.client,self.root,metadata,proof)['reused'],2)
        adapter=CostAdapter(self.client,self.root)
        for original in scenarios:
            saved=adapter.get_scenario(original.scenario_version)
            self.assertEqual(saved.author,original.author);self.assertEqual(saved.created_at,original.created_at)
            self.assertEqual(saved.request_id,original.request_id);self.assertEqual(saved.rates,original.rates)
            self.assertEqual(saved.publication.publication_id,target['publication_id'])
            self.assertEqual(saved.analysis_context.publication_id,target['publication_id'])
        drift=copy.deepcopy(metadata);drift['scenarios'][0]['name']='drift'
        with self.assertRaises(ValueError):import_scenarios(self.client,self.root,drift,proof)
