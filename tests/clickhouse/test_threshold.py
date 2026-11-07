import copy
import csv
from dataclasses import replace
from datetime import date,datetime,timedelta
from decimal import Decimal
import hashlib
import json
import os
import unittest
from uuid import uuid4

import test_water as fixtures
from greenhouse.clickhouse.threshold_run import run,threshold_configuration
from greenhouse.clickhouse.harvest_run import harvest_configuration
from greenhouse.clickhouse.threshold import ClimateAdapter,DrainAdapter,SlotMart
from greenhouse.clickhouse.schedules import register_rule_schedule,read_rule_schedule,save_drain_profile
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.water_run import corrected_membership
from greenhouse.clickhouse.publication import Publications
from greenhouse.contracts import QueryContext,RuleBasis,RuleScheduleRevision,RuleScheduleSegment,rule_segment_sort_key
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries import ClimateService,DrainService


def revision(scope,kind,start,end,*,version=1,split=None):
    segments=[]
    if kind=='climate':
        for basis in (RuleBasis.REQUESTED,RuleBasis.REALIZED):
            for metric in ('temperature','co2'):
                for lo,hi,delta in ((start,end,Decimal(0)),) if split is None else ((start,split,Decimal(0)),(split,end,Decimal(1))):
                    segments.append(RuleScheduleSegment(metric,'climate',basis,lo,hi,delta=delta))
    else:
        for metric,lo,hi in (('ec','2','5'),('ph','5.5','6.5')):
            segments.append(RuleScheduleSegment(metric,'drain_process_computer',RuleBasis.MEASURED,start,end,lower=Decimal(lo),upper=Decimal(hi)))
    family=kind+':'+scope
    return RuleScheduleRevision(family+':v'+str(version),family,version,scope,'Automatoes','Synthetic fixture only',True,'demo_rule',tuple(sorted(segments,key=rule_segment_sort_key)))


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class ThresholdTest(unittest.TestCase):
    setUp=fixtures.WaterTest.setUp

    def prepare(self):
        base={'Tair':'30','CO2air':'500','t_heat_sp':'20','t_vent_sp':'25','co2_sp':'500',
              't_heat_vip':'20','t_ventlee_vip':'25','co2_vip':'500','EC_drain_PC':'6','pH_drain_PC':'7'}
        stamps=[datetime(2020,3,2,23,50),datetime(2020,3,2,23,55),datetime(2020,3,3),datetime(2020,3,3,0,10),datetime(2020,3,3,0,15),datetime(2020,3,3,0,20),datetime(2020,3,7)]
        overrides=[{}, {}, {}, {'co2_sp':'0','pH_drain_PC':'0'}, {'Tair':'22','EC_drain_PC':'2','pH_drain_PC':'5.5'}, {'Tair':'invalid','EC_drain_PC':'-1','pH_drain_PC':'6'},{}]
        path=self.raw/'Automatoes/GreenhouseClimate.csv'
        with path.open('w',newline='') as stream:
            w=csv.writer(stream);w.writerow(HEADERS['GreenhouseClimate'])
            # Deliberately reversed source order, independent of physical blocks.
            for stamp,override in reversed(list(zip(stamps,overrides))):
                values=base|override
                serial=(stamp-datetime(1899,12,30)).total_seconds()/86400
                w.writerow([f'{serial:.5f}']+[values.get(h,'') for h in HEADERS['GreenhouseClimate'][1:]])
        manifest=json.loads((self.raw/'manifest.json').read_text());manifest['files'].append({'path':'Automatoes/GreenhouseClimate.csv','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'csv':{'data_rows':len(stamps)}})
        (self.raw/'manifest.json').write_text(json.dumps(manifest))
        self.harvest=harvest_configuration(self.snapshot,['Automatoes'],start='2020-03-02',end='2020-03-07')
        self.threshold=threshold_configuration(self.snapshot,['Automatoes'],start='2020-03-02',end='2020-03-07')
        self.climate_rule=revision(self.scope,'climate',datetime(2020,3,2),datetime(2020,3,7))
        self.drain_rule=revision(self.scope,'drain',datetime(2020,3,2),datetime(2020,3,7))
        from greenhouse.clickhouse.schema import migrate
        migrate(self.client,self.root)
        for rev in (self.climate_rule,self.drain_rule): register_rule_schedule(self.client,self.root,rev)
        result=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config,self.harvest,self.threshold)
        a=ClimateAdapter(self.client,raw_snapshots={self.snapshot:self.raw});d=DrainAdapter(self.client,raw_snapshots={self.snapshot:self.raw})
        pub=a.pin_publication(self.scope)
        q=QueryContext(self.scope,pub.publication_id,self.snapshot,'agc-cycle-Automatoes',('Automatoes',),date(2020,3,3),date(2020,3,4))
        return result,a,d,pub,q

    def test_duration_episode_quality_off_mode_and_verified_lineage(self):
        result,a,d,pub,q=self.prepare()
        service=ClimateService(a); ev=service.get_evaluation(q,basis=RuleBasis.REQUESTED,rule_version=self.climate_rule.rule_version,use_cache=False)
        temp,co2=ev.result.metric_results
        self.assertEqual((temp.expected_minutes,temp.valid_minutes,temp.out_minutes),(Decimal(1440),Decimal(15),Decimal(10)))
        self.assertEqual((co2.valid_minutes,co2.out_minutes),(Decimal(15),Decimal(0)))
        self.assertIn('sp0_off_mode',{s.reason_code for s in co2.unknown_reasons})
        episodes=[e for e in ev.result.episodes if e.metric=='temperature']
        self.assertEqual([(e.original_minutes,e.clipped_minutes) for e in episodes],[(Decimal(15),Decimal(5)),(Decimal(5),Decimal(5))])
        realized=service.get_evaluation(q,basis=RuleBasis.REALIZED,rule_version=self.climate_rule.rule_version,use_cache=False)
        self.assertEqual(realized.result.metric_results[1].valid_minutes,Decimal(20))
        drain=DrainService(d); out=drain.get_evaluation(q,rule_version=self.drain_rule.rule_version,use_cache=False)
        ec,ph=out.result.metric_results
        self.assertEqual((ec.valid_minutes,ec.out_minutes),(Decimal(15),Decimal(10)))
        self.assertEqual((ph.valid_minutes,ph.out_minutes,ph.suspect_minutes),(Decimal(15),Decimal(5),Decimal(5)))
        joint=out.result.joint_results[0]
        self.assertEqual((joint.jointly_valid_minutes,joint.joint_out_minutes),(Decimal(10),Decimal(5)))
        reviews=drain.get_slot_review(q,rule_version=self.drain_rule.rule_version,window_start=datetime(2020,3,3,0,2),window_end=datetime(2020,3,3,0,20),max_slots=2)
        self.assertTrue(reviews.truncated);self.assertEqual(reviews.window_slots,2)
        facts=drain.slot_facts(q,window_start=datetime(2020,3,3),window_end=datetime(2020,3,3,0,25))
        row=next(r for r in facts.rows if r.slot_start.minute==10)
        self.assertEqual(row.ph_drain,Decimal(0));self.assertEqual(row.measure_quality['pH_drain_PC']['status'],'suspect')
        lineage=drain.get_lineage(pub,revision_id=row.revision_id)
        self.assertTrue(lineage.raw_hash_verified);self.assertEqual(lineage.record_type,'drain')
        self.assertLess(abs((lineage.source_timestamp-row.slot_start).total_seconds()),0.5)
        unknown=drain.get_evaluation(q,use_cache=False)
        self.assertEqual(unknown.result.joint_results[0].jointly_valid_minutes,Decimal(0))
        again=run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config,self.harvest,self.threshold)
        self.assertEqual(again['publication_id'],result['publication_id'])

    def test_new_rule_boundary_and_late_correction_keep_old_publication(self):
        result,a,d,pub,q=self.prepare()
        new_rule=revision(self.scope,'climate',datetime(2020,3,2),datetime(2020,3,7),version=2,split=datetime(2020,3,3))
        register_rule_schedule(self.client,self.root,new_rule)
        self.assertEqual(read_rule_schedule(self.client,new_rule.rule_version),new_rule)
        self.assertEqual(register_rule_schedule(self.client,self.root,new_rule)['outcome'],'reuse')
        with self.assertRaises(ValueError): register_rule_schedule(self.client,self.root,replace(new_rule,evidence_reference='changed'))
        out=ClimateService(a).get_evaluation(q,basis=RuleBasis.REQUESTED,rule_version=new_rule.rule_version,use_cache=False)
        self.assertEqual([e.original_minutes for e in out.result.episodes if e.metric=='temperature'],[Decimal(5),Decimal(5)])
        repo=Repository(self.client,self.root)
        old=next(r for r in repo.load_membership(self.scope,result['load_id']) if r['record_type']=='GreenhouseClimate' and r['source_timestamp']=='2020-03-03T00:00:00')
        record=json.loads(old['payload']);record['run_id']='late-correction';record['values']['Tair']='22'
        repo.append_record(self.scope,record,revision=2,correction_id='late:'+self.scope,correction={'actor':'reviewer','evidence':'fixture late correction','previous_revision':old['revision_id']})
        revised=run(self.client,self.root,self.raw,self.root/'output',self.scope,uuid4().hex,self.config,self.harvest,self.threshold)
        for pid,expected in ((pub.publication_id,Decimal(10)),(revised['publication_id'],Decimal(5))):
            query=replace(q,publication_id=pid)
            ev=ClimateService(ClimateAdapter(self.client)).get_evaluation(query,basis=RuleBasis.REQUESTED,rule_version=self.climate_rule.rule_version,use_cache=False)
            self.assertEqual(ev.result.metric_results[0].out_minutes,expected)
        self.assertEqual(a.publication(pub.publication_id),pub)
        self.assertNotEqual(pub.publication_id,revised['publication_id'])

    def test_profile_atomic_envelope_and_catalog_group(self):
        _,a,d,pub,q=self.prepare()
        cycle=d.catalog_cycles(pub.publication_id)[0]
        args=dict(name='engineer',version=1,ec_lower='2',ec_upper='5',ph_lower='5.5',ph_upper='6.5',provenance='Fixture engineer request')
        revision=save_drain_profile(self.client,self.root,pub,cycle,**args)
        self.assertEqual(save_drain_profile(self.client,self.root,pub,cycle,**args),revision)
        with self.assertRaises(ValueError): save_drain_profile(self.client,self.root,pub,cycle,**(args|{'ec_upper':'6'}))
        opts=a.catalog_schedule_rules(self.scope,'Automatoes',measurement_context='climate',metrics=('temperature','co2'),bases=(RuleBasis.REQUESTED,RuleBasis.REALIZED))
        self.assertEqual([r.rule_version for r in opts],[self.climate_rule.rule_version])
        opts=d.catalog_schedule_rules(self.scope,'Automatoes',measurement_context='drain_process_computer',metrics=('ec','ph'),bases=(RuleBasis.MEASURED,))
        self.assertEqual({r.rule_version for r in opts},{self.drain_rule.rule_version,revision.rule_version})

    def test_slot_reader_keeps_projection_conflicts_after_exact_retry_collapse(self):
        _,adapter,_,publication,context=self.prepare()
        self.client.begin_action()
        original=adapter.read_climate_slots(context,publication)
        parameters={'scope':self.scope,'pub':publication.publication_id}
        row=self.client.query("""SELECT * FROM threshold_measure WHERE scope_id={scope:String}
            AND publication_id={pub:String} AND bundle='climate' AND measure_code='Tair'
            AND slot_start>='2020-03-03' AND slot_start<'2020-03-04'
            AND value_exact IS NOT NULL LIMIT 1""",parameters)[0]
        repository=Repository(self.client,self.root)
        repository._insert('threshold_measure',[row])
        self.assertEqual(adapter.read_climate_slots(context,publication),original)
        # Projection columns do not belong to the row DTO. A compact response
        # must still distinguish full physical tuples that differ only there.
        repository._insert('threshold_measure',[dict(row,value_decimal='999.654321')])
        try:
            with self.assertRaisesRegex(ValueError,'Conflicting threshold slot measure'):
                adapter.read_climate_slots(context,publication)
        finally:
            self.client.close()
            self.client.execute("""ALTER TABLE threshold_measure DELETE WHERE scope_id={scope:String}
                AND publication_id={pub:String} AND bundle='climate' AND measure_code='Tair'
                AND value_decimal=999.654321 SETTINGS mutations_sync=2""",parameters)

    def test_bounded_source_selection_is_pinned_and_tampered_mart_is_refused(self):
        result,a,d,pub,q=self.prepare()
        from greenhouse.clickhouse.threshold import validated_config
        for invalid in (['2020-03-03 12:00:00','2020-03-04T00:00:00'],['2020-03-03T00:00:00+00:00','2020-03-04T00:00:00+00:00']):
            with self.assertRaises(ValueError): validated_config(self.threshold|{'climate_window':invalid})
        selected=copy.deepcopy(self.threshold);selected['climate_window']=['2020-03-03T00:00:00','2020-03-04T00:00:00']
        bounded=run(self.client,self.root,self.raw,self.root/'output',self.scope,uuid4().hex,self.config,self.harvest,selected,threshold_version='bounded-v1')
        self.assertEqual(bounded['counts']['deferred_source_window'],3)
        self.assertEqual(bounded['receipts']['climate']['rows'],4)
        repo=Repository(self.client,self.root)
        audit=repo.load_audit(self.scope,bounded['load_id'])
        file=next(json.loads(r['metadata']) for r in audit['files'] if r['source_file'].endswith('/GreenhouseClimate.csv'))
        self.assertEqual(file['climate_window'],selected['climate_window'])
        self.assertEqual(a.publication(pub.publication_id),pub)
        # Scope-local injected extra row cannot become visible through a fresh pin.
        row=self.client.query("SELECT * FROM threshold_measure WHERE scope_id={scope:String} AND publication_id={pub:String} AND bundle='climate' LIMIT 1",{'scope':self.scope,'pub':bounded['publication_id']})[0]
        row['value_exact']='999';row['value_decimal']='999'
        repo._insert('threshold_measure',[row])
        try:
            with self.assertRaisesRegex(ValueError,'Threshold mart differs'):
                ClimateAdapter(self.client).publication(bounded['publication_id'])
        finally:
            self.client.execute("ALTER TABLE threshold_measure DELETE WHERE scope_id={scope:String} AND publication_id={pub:String} AND bundle='climate' AND value_exact='999' SETTINGS mutations_sync=2",{'scope':self.scope,'pub':bounded['publication_id']})
