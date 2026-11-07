"""Deterministic real-MQTT fault matrix with an independent six-slot oracle."""
import base64
import csv
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from uuid import uuid4

import test_stream_store as fixtures
from replay_fixtures import config
from greenhouse.clickhouse.stream_store import StreamSettings,landed_records
from greenhouse.clickhouse.replay_config import register_config
from greenhouse.clickhouse.replay_publication import read_publication
from greenhouse.clickhouse.replay_evaluation import evaluate_publication
from greenhouse.contracts import RuleBasis,RuleScheduleRevision,RuleScheduleSegment
from greenhouse.ingestion.parser import HEADERS


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_MQTT_TEST')=='1','isolated real MQTT required')
class MqttFaultTest(unittest.TestCase):
    setUp=fixtures.StreamStoreTest.setUp

    def exercise(self,fault):
        from greenhouse.sensor.simulator import SensorSimulator
        from greenhouse.sensor.faults import FaultScheduler
        from greenhouse.sensor.collector import MqttCollector
        from greenhouse.sensor.publisher import OutboxPublisher
        from greenhouse.sensor.stream_landing import start_landing
        from pyspark.sql import SparkSession
        source=self.root/'GreenhouseClimate.csv'
        with source.open('w',newline='') as stream:
            w=csv.writer(stream);w.writerow(HEADERS['GreenhouseClimate'])
            for i in range(6):
                values={'Tair':str(Decimal(20)+Decimal(i)/10),'CO2air':'500','EC_drain_PC':'3.2','pH_drain_PC':'6.2',
                    't_heat_sp':'18','t_vent_sp':'20','co2_sp':'500','t_heat_vip':'18','t_ventlee_vip':'20','co2_vip':'500'}
                w.writerow([format(Decimal(43815)+Decimal(i)/288,'.5f')]+[values.get(h,'') for h in HEADERS['GreenhouseClimate'][1:]])
        scheduler=FaultScheduler(seed=3,duplicate_prob=1 if fault=='duplicate' else 0,conflict_prob=1 if fault=='conflict' else 0,
            delay_prob=1 if fault=='delay' else 0,drop_prob=1 if fault=='drop' else 0)
        simulator=SensorSimulator(source,self.replay,self.root/'outbox',compartment_id='AICU',cycle_id='fixture-cycle',snapshot_id='fixture-snapshot',
            snapshot_sha='a'*64,relative_path='AICU/GreenhouseClimate.csv',cursor_interval=2,fault_scheduler=scheduler)
        self.assertEqual(simulator.run().expected_unique_events,6)
        replacement=None
        with sqlite3.connect(self.root/'outbox/outbox.db') as db:
            audit=db.execute('SELECT event_id,sequence,fault_type FROM fault_audit ORDER BY id').fetchall()
            order=[r[0] for r in db.execute('SELECT sequence FROM telemetry_messages ORDER BY id')]
            if fault in ('malformed','invalid-schema'):
                original=db.execute('SELECT payload_bytes FROM telemetry_messages WHERE id=1').fetchone()[0]
                if fault=='malformed':replacement=b'\xff{not-json'
                else:
                    invalid=json.loads(original);invalid['units']['Tair']='wrong-unit';replacement=json.dumps(invalid,sort_keys=True).encode()
                db.execute('UPDATE telemetry_messages SET payload_bytes=?,payload_hash=? WHERE id=1',(replacement,hashlib.sha256(replacement).hexdigest()));db.commit()
        self.scope=simulator.data_scope_id;self.version='fault-v1';self.cfg=config(self.replay,self.scope);self.cfg['file_checksum']=hashlib.sha256(source.read_bytes()).hexdigest();self.cfg['snapshot_id']=simulator.snapshot_id;self.cfg['snapshot_sha']=simulator.snapshot_sha
        register_config(self.client,self.root,self.scope,self.version,self.cfg)
        options={k:self.cfg[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')}
        ready=self.root/'ready';ready.mkdir()
        collector=MqttCollector(spool_dir=self.root/'spool',ready_dir=ready,mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),client_id='fault-coll-'+uuid4().hex[:12],batch_size=4,batch_timeout=.1)
        publisher=OutboxPublisher(outbox_dir=self.root/'outbox',mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),client_id='fault-pub-'+uuid4().hex[:12],max_inflight=10)
        errors=[]
        def collect():
            try:collector.start(poll_interval=.05)
            except Exception as error:errors.append(error)
        worker=threading.Thread(target=collect,daemon=True);query=None
        try:
            worker.start();deadline=time.monotonic()+15
            while time.monotonic()<deadline and not (collector._client and collector._client.is_connected()):time.sleep(.05)
            self.assertTrue(collector._client and collector._client.is_connected(),errors)
            time.sleep(.5);publisher.start();self.assertGreaterEqual(publisher.publish_all_pending(poll_interval=.05),2)
            publisher.stop();collector.stop();worker.join(timeout=5);self.assertFalse(worker.is_alive());self.assertFalse(errors,errors)
            query=start_landing(StreamSettings(self.client.settings,self.root,self.version),ready,self.root/'checkpoint',self.stream,'fault-ch-v1',available_now=True,publication_options=options)
            self.assertTrue(query.awaitTermination(120))
            pointers=self.client.query('SELECT DISTINCT generation,publication_id FROM replay_pointer WHERE scope_id={scope:String}',{'scope':self.scope})
            pub=max(pointers,key=lambda p:int(p['generation']))['publication_id'];metadata,events=read_publication(self.client,pub);state=metadata[-1]
            self.assertTrue(state['completion_requested'])
            if fault in ('duplicate','delay'):
                self.assertTrue(state['complete']);self.assertEqual(len(events),6);self.assertTrue(state['checksum_verified']);self.assertEqual(len(audit),6)
                if fault=='delay':self.assertNotEqual(order,sorted(order))
            elif fault in ('conflict','drop'):
                self.assertFalse(state['complete']);self.assertFalse(events);self.assertEqual(state['missing_sequences'],[1,2,3,4,5,6])
                if fault=='conflict':self.assertEqual(set(state['conflicts']),{r[0] for r in audit})
            else:
                self.assertFalse(state['complete']);self.assertEqual(state['quarantined'],1);self.assertEqual(state['missing_sequences'],[1]);self.assertEqual(len(events),5)
                quarantined=[r for r in landed_records(self.client,self.scope,self.version) if r.get('is_valid') is False]
                self.assertEqual(len(quarantined),1);self.assertEqual(base64.b64decode(quarantined[0]['raw_payload_base64']),replacement)
            start,end=datetime(2019,12,16),datetime(2019,12,18)
            climate=RuleScheduleRevision('fault-climate:'+self.scope,'fault-climate',1,self.scope,'AICU','Six-slot hand oracle',True,'demo_rule',(
                RuleScheduleSegment('temperature','climate',RuleBasis.REQUESTED,start,end,delta=Decimal('.1')),))
            drain=RuleScheduleRevision('fault-drain:'+self.scope,'fault-drain',1,self.scope,'AICU','Six-slot hand oracle',True,'demo_rule',(
                RuleScheduleSegment('ec','drain_process_computer',RuleBasis.MEASURED,start,end,lower=Decimal(2),upper=Decimal(5)),
                RuleScheduleSegment('ph','drain_process_computer',RuleBasis.MEASURED,start,end,lower=Decimal('5.5'),upper=Decimal('6.5'))))
            ev=evaluate_publication(self.client,pub,climate_rule=climate,drain_rule=drain)
            temp=ev.climate.metric_results[0];ec=ev.drain.metric_results[0]
            self.assertEqual(temp.expected_minutes,Decimal(30));self.assertEqual(ec.expected_minutes,Decimal(30))
            if fault in ('conflict','drop'):
                self.assertEqual(temp.valid_minutes,Decimal(0));self.assertEqual(ec.valid_minutes,Decimal(0));self.assertIsNone(temp.out_hours);self.assertIsNone(ec.out_hours)
            else:
                valid=Decimal(25 if replacement else 30);self.assertEqual(temp.valid_minutes,valid);self.assertEqual(ec.valid_minutes,valid)
                self.assertEqual(temp.out_minutes,Decimal(20));self.assertEqual(ec.out_minutes,Decimal(0))
            report={'status':'passed','fault':fault,'events':len(events),'complete':state['complete'],'quarantined':state['quarantined'],
                'missing_sequences':state['missing_sequences'],'source_slots':6,'expected_minutes':'30','temperature_valid_minutes':str(temp.valid_minutes),'temperature_out_minutes':str(temp.out_minutes),
                'raw_quarantine_preserved':replacement is not None,'publication':pub}
            print(json.dumps(report))
            if os.environ.get('GREENHOUSE_CH_FAULT_EVIDENCE'):
                out=Path(os.environ['GREENHOUSE_CH_FAULT_EVIDENCE']);out.mkdir(parents=True,exist_ok=True);(out/(fault+'.json')).write_text(json.dumps(report,indent=2)+'\n')
        finally:
            publisher.stop();collector.stop();worker.join(timeout=5)
            if query:query.stop()
            spark=SparkSession.getActiveSession()
            if spark:spark.stop()

    def test_duplicate(self):self.exercise('duplicate')
    def test_conflict(self):self.exercise('conflict')
    def test_delay_out_of_order(self):self.exercise('delay')
    def test_drop_gap(self):self.exercise('drop')
    def test_malformed_bytes(self):self.exercise('malformed')
    def test_invalid_schema(self):self.exercise('invalid-schema')
