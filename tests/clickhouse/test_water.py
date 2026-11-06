import copy
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from greenhouse.clickhouse.client import Client, ClickHouseSettings
from greenhouse.clickhouse.water_run import run, water_configuration
from greenhouse.clickhouse.water import WaterAdapter
from greenhouse.contracts import QueryContext
from greenhouse.queries.irrigation import IrrigationService


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','explicit isolated server required')
class WaterTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.raw=self.root/'raw'; (self.raw/'Automatoes').mkdir(parents=True)
        source=self.raw/'Automatoes/Resources.csv'
        source.write_text('%Time ,Heat_cons,ElecHigh,ElecLow,CO2_cons,Irr,Drain\n'
            '43892,1,2,3,4,9.36,3.89\n43893,1,2,3,4,0,0\n43894,1,2,3,4,invalid,1\n43895,1,2,3,4,-1,2\n')
        self.snapshot='water-fixture-'+uuid4().hex
        manifest={'snapshot_id':self.snapshot,'archive':{'sha256':'c'*64},'files':[{'path':'Automatoes/Resources.csv',
            'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'csv':{'data_rows':4}}]}
        (self.raw/'manifest.json').write_text(json.dumps(manifest))
        self.scope=uuid4().hex; self.workflow=uuid4().hex
        self.config=water_configuration(self.snapshot,['Automatoes'],start='2020-03-02',end='2020-03-07',assumed_basis=True)
        self.client=Client(ClickHouseSettings.from_env())

    def test_full_source_to_service_csv_lineage_and_rerun(self):
        result=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config)
        adapter=WaterAdapter(self.client,raw_snapshots={self.snapshot:self.raw})
        service=IrrigationService(adapter); publication=service.pin_publication(self.scope)
        self.assertEqual(publication.publication_id,result['publication_id'])
        context=QueryContext(self.scope,publication.publication_id,self.snapshot,'agc-cycle-Automatoes',('Automatoes',),date(2020,3,2),date(2020,3,7))
        summary=service.get_irrigation_summary(context)
        self.assertEqual(summary.irrigation_observed_l_m2,Decimal('9.36'))
        self.assertEqual(summary.drain_observed_l_m2,Decimal('6.89'))
        self.assertEqual(summary.paired_days,2)
        self.assertEqual(summary.expected_days,5)
        self.assertEqual(summary.paired_coverage_pct,Decimal('40'))
        from decimal import localcontext
        with localcontext() as decimal_context:
            decimal_context.prec=50
            self.assertEqual(summary.drain_fraction_pct,Decimal('3.89')*100/Decimal('9.36'))
        days=service.get_irrigation_days(context)
        self.assertEqual(days[2].irrigation.reason_code,'invalid_numeric')
        self.assertEqual(days[2].drain.value,Decimal('1'))
        self.assertEqual(days[3].irrigation.reason_code,'negative_quantity')
        self.assertIsNone(days[4].irrigation.value)
        from greenhouse.app.wiring import water_action
        from greenhouse.app.exports import day_csv
        action=water_action(service,publication,context)
        exported=day_csv(action).decode('utf-8-sig')
        self.assertIn('invalid_numeric',exported)
        self.assertIn('9.36',exported)
        lineage=service.get_lineage(publication,revision_id=days[0].revision_id)
        self.assertTrue(lineage.raw_hash_verified)
        self.assertEqual(lineage.first_run_id,result['load_id'])
        again=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config)
        self.assertTrue(again['resumed'])
        self.assertEqual(again['publication_id'],result['publication_id'])

    def test_correction_is_selected_for_new_publication_old_pin_stays_unchanged(self):
        from greenhouse.clickhouse.repository import Repository
        from greenhouse.clickhouse.water_run import corrected_membership
        from greenhouse.clickhouse.water import WaterMart
        result=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config)
        repo=Repository(self.client,self.root)
        original=next(r for r in repo.load_membership(self.scope,result['load_id']) if r['logical_key']=='2020-03-02')
        payload=json.loads(original['payload']); payload['run_id']='correction-run'
        payload['values']['Irr']='10'
        repo.append_record(self.scope,payload,revision=2,correction_id='review-'+self.scope,
            correction={'actor':'fixture reviewer','evidence':'fixture correction','previous_revision':original['revision_id']})
        new_load=corrected_membership(repo,self.scope,result['load_id'])
        mart=WaterMart(self.client,self.root,'water-config-v1')
        new=mart.publications().publish(self.scope,uuid4().hex,new_load,build=mart.build)
        adapter=WaterAdapter(self.client)
        old=adapter.publication(result['publication_id']); current=adapter.publication(new['publication_id'])
        def amount(publication):
            context=QueryContext(self.scope,publication.publication_id,self.snapshot,'agc-cycle-Automatoes',('Automatoes',),date(2020,3,2),date(2020,3,3))
            return IrrigationService(adapter).get_irrigation_summary(context).irrigation_observed_l_m2
        self.assertEqual(amount(old),Decimal('9.36'))
        self.assertEqual(amount(current),Decimal('10'))

    def test_quality_policy_version_cannot_hide_definition_drift(self):
        from greenhouse.clickhouse.water import validated_config
        broken=copy.deepcopy(self.config)
        broken['quality_definition']['nonnegative']=[]
        with self.assertRaisesRegex(ValueError,'definition drift'):
            validated_config(broken)

    def test_catalog_does_not_fallback_to_old_water_when_new_complete_lacks_bundle(self):
        from greenhouse.clickhouse.publication import Publications
        result=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config)
        bare=Publications(self.client,self.root).publish(self.scope,uuid4().hex,result['load_id'])
        options=WaterAdapter(self.client).catalog_publications()
        selected=next(r for r in options if r.scope_id==self.scope)
        self.assertEqual(selected.publication_id,bare['publication_id'])
        self.assertFalse(selected.usable)
