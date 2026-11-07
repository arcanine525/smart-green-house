import csv
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
import unittest
from uuid import uuid4

import test_water as fixtures
from greenhouse.clickhouse.harvest_run import run,harvest_configuration
from greenhouse.clickhouse.water_run import water_configuration
from greenhouse.clickhouse.harvest import HarvestAdapter
from greenhouse.clickhouse.water import WaterAdapter
from greenhouse.contracts import QueryContext
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries.harvest import HarvestService


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class HarvestTest(unittest.TestCase):
    setUp=fixtures.WaterTest.setUp

    def prepare(self,complete=False,missing=False):
        rows=[['43892','1','0.5'],['43894','2','0.25']] if complete else [['43892','1','0.5'],['43894','2',''],['43895','invalid','0.25']]
        manifest=json.loads((self.raw/'manifest.json').read_text())
        for comp,data in (('Automatoes',rows),('Reference',[['43000','1000','2000']])):
            path=self.raw/comp/'Production.csv'; path.parent.mkdir(exist_ok=True)
            with path.open('w',newline='') as stream:
                writer=csv.writer(stream); writer.writerow(HEADERS['Production'])
                for row in data: writer.writerow(row+['0']*6)
            manifest['files'].append({'path':comp+'/Production.csv','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'csv':{'data_rows':len(data)}})
        (self.raw/'manifest.json').write_text(json.dumps(manifest))
        kwargs={'start':'2020-03-02','end':'2020-03-07','assumed_basis':True}
        water=water_configuration(self.snapshot,['Automatoes','Reference']+(['Missing'] if missing else []),**kwargs)
        harvest=harvest_configuration(self.snapshot,['Automatoes','Reference']+(['Missing'] if missing else []),**kwargs)
        result=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,water,harvest)
        adapter=HarvestAdapter(self.client,raw_snapshots={self.snapshot:self.raw})
        publication=adapter.pin_publication(self.scope)
        context=QueryContext(self.scope,publication.publication_id,self.snapshot,'agc-cycle-Automatoes',('Automatoes',),date(2020,3,2),date(2020,3,7))
        return result,adapter,publication,context

    def test_incomplete_snapshot_retains_a_and_quarantined_reference_without_zero(self):
        result,adapter,publication,context=self.prepare()
        service=HarvestService(adapter); summary=service.get_harvest_summary(context)
        self.assertEqual(summary.prod_a_observed,Decimal('3'))
        self.assertEqual(summary.prod_b_observed,Decimal('.75'))
        self.assertEqual(summary.paired_total,Decimal('1.5'))
        self.assertEqual(summary.cumulative_a,Decimal('3'))
        self.assertFalse(summary.completeness.snapshot_complete)
        self.assertEqual(summary.completeness.unresolved_records,1)
        days=service.get_harvest_days(context)
        self.assertIsNone(days[1].prod_a)
        reference=QueryContext(self.scope,publication.publication_id,self.snapshot,'agc-cycle-Reference',('Reference',),context.start,context.end)
        quarantined=service.get_harvest_summary(reference)
        self.assertIsNone(quarantined.prod_a_observed)
        self.assertFalse(quarantined.completeness.source_resolved)
        self.assertEqual(quarantined.completeness.unresolved_records,1)
        issue=quarantined.completeness.unresolved_items[0]
        audit=service.get_unresolved_source(reference,issue,rule_grain='record')
        self.assertTrue(audit.record_verified)
        self.assertIn('1000',audit.tokens)
        original_manifest=(self.raw/'manifest.json').read_text()
        changed=json.loads(original_manifest); changed['archive']['sha256']='d'*64
        (self.raw/'manifest.json').write_text(json.dumps(changed))
        denied=service.get_unresolved_source(reference,issue,rule_grain='record')
        self.assertFalse(denied.record_verified); self.assertFalse(denied.tokens)
        (self.raw/'manifest.json').write_text(original_manifest)
        self.assertEqual(adapter.publication(publication.publication_id),WaterAdapter(self.client).publication(publication.publication_id))

    def test_complete_no_event_day_is_known_zero_and_cumulative_has_cycle_origin(self):
        _,adapter,publication,context=self.prepare(complete=True)
        service=HarvestService(adapter)
        summary=service.get_harvest_summary(context)
        self.assertTrue(summary.completeness.snapshot_complete)
        self.assertEqual(summary.prod_a_observed,Decimal('3'))
        days=service.get_harvest_days(context)
        self.assertEqual(days[1].prod_a,Decimal('0'))
        self.assertFalse(days[1].record_present)
        zoom=QueryContext(self.scope,publication.publication_id,self.snapshot,context.cycle_id,context.compartment_ids,date(2020,3,4),date(2020,3,5))
        cumulative=service.get_harvest_summary(zoom)
        self.assertEqual(cumulative.prod_a_observed,Decimal('2'))
        self.assertEqual(cumulative.cumulative_a,Decimal('3'))
        self.assertEqual(cumulative.cumulative_origin,date(2020,3,2))
        self.assertTrue(service.get_harvest_weekly(context))

    def test_new_correction_resolves_numeric_issue_without_changing_old_snapshot(self):
        from greenhouse.clickhouse.repository import Repository
        from greenhouse.clickhouse.water_run import corrected_membership
        from greenhouse.clickhouse.harvest import HarvestMart
        from greenhouse.clickhouse.water import WaterMart
        from greenhouse.clickhouse.publication import Publications
        result,adapter,publication,context=self.prepare()
        repo=Repository(self.client,self.root)
        original=next(r for r in repo.load_membership(self.scope,result['load_id']) if r['record_type']=='Production' and 'invalid_numeric' in r['payload'])
        record=json.loads(original['payload']); record['run_id']='corrected-harvest'; record['values']['ProdA']='4'
        repo.append_record(self.scope,record,revision=2,correction_id=uuid4().hex,
            correction={'actor':'fixture reviewer','evidence':'typed correction','previous_revision':original['revision_id']})
        load=corrected_membership(repo,self.scope,result['load_id'])
        water=WaterMart(self.client,self.root,'water-config-v1'); harvest=HarvestMart(self.client,self.root,'harvest-config-v1',load)
        publisher=Publications(self.client,self.root,bundles={'water':water.verify,'harvest':harvest.verify})
        def build(scope,pub,core): water.build(scope,pub,core); harvest.build(scope,pub,core)
        revised=publisher.publish(self.scope,uuid4().hex,load,build=build)
        old=HarvestService(adapter).get_harvest_summary(context)
        self.assertEqual(old.completeness.unresolved_records,1)
        new_context=QueryContext(self.scope,revised['publication_id'],self.snapshot,context.cycle_id,context.compartment_ids,context.start,context.end)
        new=HarvestService(HarvestAdapter(self.client)).get_harvest_summary(new_context)
        self.assertEqual(new.prod_a_observed,Decimal('7'))
        self.assertEqual(new.completeness.unresolved_records,0)
        self.assertFalse(new.completeness.snapshot_complete) # missing B record remains.

    def test_missing_expected_source_is_unknown_with_no_phantom_record(self):
        _,adapter,publication,context=self.prepare(missing=True)
        missing=QueryContext(self.scope,publication.publication_id,self.snapshot,'agc-cycle-Missing',('Missing',),context.start,context.end)
        summary=HarvestService(adapter).get_harvest_summary(missing)
        self.assertIsNone(summary.prod_a_observed)
        self.assertEqual(summary.completeness.accepted_records,0)
        self.assertEqual(summary.completeness.files_present,0)
        self.assertIn('missing_expected_source_file',summary.completeness.reasons)

    def test_frozen_source_audit_corruption_is_rejected_before_reading_kpi(self):
        from greenhouse.clickhouse.client import insert_rows
        result,adapter,publication,context=self.prepare()
        insert_rows(self.client,'load_issue',[{'scope_id':self.scope,'load_id':result['load_id'],
            'source_record_id':'unexpected','source_file':'Reference/Production.csv','disposition':'quarantine','payload':'{}'}],query_id=uuid4().hex)
        try:
            with self.assertRaisesRegex(ValueError,'audit checksum mismatch'):
                HarvestAdapter(self.client).publication(publication.publication_id)
        finally:
            # Repair only the deliberately injected row in this disposable test
            # scope; later catalog tests must not inherit this corruption.
            self.client.execute("ALTER TABLE load_issue DELETE WHERE scope_id={scope:String} AND source_record_id='unexpected' SETTINGS mutations_sync=2",{'scope':self.scope})
