"""End-to-end real local MQTT → SQLite spool/raw-ready → Spark → ClickHouse."""
import csv
from dataclasses import replace
from datetime import date,datetime,timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unittest
from uuid import uuid4

import test_stream_store as fixtures
from greenhouse.clickhouse.client import Client
from greenhouse.clickhouse.stream_store import StreamSettings
from greenhouse.clickhouse.replay_publication import read_publication
from greenhouse.clickhouse.replay_evaluation import evaluate_publication,map_historical_rule,build_replay_rows
from greenhouse.clickhouse.replay_config import from_historical
from greenhouse.clickhouse.threshold_run import run as run_batch,threshold_configuration
from greenhouse.clickhouse.water_run import water_configuration
from greenhouse.clickhouse.harvest_run import harvest_configuration
from greenhouse.clickhouse.threshold import ClimateAdapter,DrainAdapter
from greenhouse.clickhouse.schedules import register_rule_schedule
from greenhouse.contracts import QueryContext,CycleWindow,IntradayContext,RuleBasis
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries.climate import build_slots as climate_slots
from greenhouse.queries.drain import build_slots as drain_slots
from greenhouse.queries.rule_duration import evaluate_rule_schedule


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_MQTT_TEST')=='1','explicit isolated broker/processing runtime required')
class MqttStreamTest(unittest.TestCase):
    setUp=fixtures.StreamStoreTest.setUp
    rules=fixtures.StreamStoreTest.rules

    def test_real_mqtt_sqlite_spool_and_spark_match_batch_and_hand_oracle(self):
        from greenhouse.sensor.simulator import SensorSimulator
        from greenhouse.sensor.collector import MqttCollector
        from greenhouse.sensor.publisher import OutboxPublisher
        from greenhouse.sensor.stream_landing import start_landing
        from pyspark.sql import SparkSession
        raw=self.root/'raw';folder=raw/'AICU';folder.mkdir(parents=True);source=folder/'GreenhouseClimate.csv'
        with source.open('w',newline='') as stream:
            w=csv.writer(stream);w.writerow(HEADERS['GreenhouseClimate'])
            for i in range(30):
                values={'Tair':'30' if i<10 else '22','CO2air':'500','t_heat_sp':'20','t_vent_sp':'25',
                    'co2_sp':'0' if i==5 else '500','t_heat_vip':'20','t_ventlee_vip':'25','co2_vip':'500',
                    'EC_drain_PC':'6' if i<10 else '3','pH_drain_PC':'0' if i==5 else '6'}
                w.writerow([format(Decimal(43815)+Decimal(i)/Decimal(288),'.5f')]+[values.get(h,'') for h in HEADERS['GreenhouseClimate'][1:]])
        checksum=hashlib.sha256(source.read_bytes()).hexdigest()
        (raw/'manifest.json').write_text(json.dumps({'snapshot_id':'fixture-snapshot','archive':{'sha256':'a'*64},'files':[{'path':'AICU/GreenhouseClimate.csv','sha256':checksum,'csv':{'data_rows':30}}]}))
        historical='historical:'+uuid4().hex;kwargs=dict(start='2019-12-16',end='2019-12-18')
        batch=run_batch(self.client,self.root,raw,self.root/'batch-output',historical,uuid4().hex,
            water_configuration('fixture-snapshot',['AICU'],**kwargs),harvest_configuration('fixture-snapshot',['AICU'],**kwargs),threshold_configuration('fixture-snapshot',['AICU'],**kwargs))
        simulator=SensorSimulator(source,self.replay,self.root/'outbox',compartment_id='AICU',cycle_id='agc-cycle-AICU',
            snapshot_id='fixture-snapshot',snapshot_sha='a'*64,relative_path='AICU/GreenhouseClimate.csv',cursor_interval=5)
        simulation=simulator.run();self.assertEqual(simulation.expected_unique_events,30)
        self.scope=simulator.data_scope_id;self.version='mqtt-v1'
        self.cfg,_=from_historical(self.client,self.root,batch['publication_id'],replay_id=self.replay,scope_id=self.scope,compartment_id='AICU',config_version=self.version)
        self.options={k:self.cfg[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')}
        climate_rule=replace(self.rules('climate'),scope_id=historical,rule_version='climate-source:'+historical,rule_id='climate-source')
        drain_rule=replace(self.rules('drain'),scope_id=historical,rule_version='drain-source:'+historical,rule_id='drain-source')
        for rule in (climate_rule,drain_rule):register_rule_schedule(self.client,self.root,rule)
        ready=self.root/'ready';ready.mkdir()
        collector_id='ch-coll-'+uuid4().hex[:12];publisher_id='ch-pub-'+uuid4().hex[:12]
        collector=MqttCollector(spool_dir=self.root/'spool',ready_dir=ready,mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),
            client_id=collector_id,clean_session=False,batch_size=5,batch_timeout=.2)
        publisher=OutboxPublisher(outbox_dir=self.root/'outbox',mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),client_id=publisher_id,max_inflight=10)
        recovering=os.environ.get('GREENHOUSE_CH_BROKER_RECOVERY')=='1'
        control=Path(os.environ['GREENHOUSE_CH_RECOVERY_CONTROL']) if recovering else None
        def mark(name):
            (control/name).write_text('ready\n')
        def wait_marker(name):
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                if (control/name).exists():return
                time.sleep(.1)
            raise AssertionError('Host broker recovery marker timeout: '+name)
        if recovering:
            original_publish=publisher.publish_single_item;submitted=set()
            def paced_publish(item):
                result=original_publish(item)
                if result and item.outbox_table=='telemetry_messages':
                    submitted.add(item.event_id)
                    if len(submitted)==5 and not (control/'broker_ready_to_stop').exists():
                        mark('broker_ready_to_stop');wait_marker('broker_stopped')
                return result
            publisher.publish_single_item=paced_publish
        pending_before_restart=None;ready_preserved=True;expected_disconnects=[]
        errors=[]
        def collect():
            try:collector.start(poll_interval=.05)
            except Exception as error:errors.append(error)
        def send():
            try:publisher.publish_all_pending(poll_interval=.05)
            except Exception as error:errors.append(error)
        collect_thread=threading.Thread(target=collect,daemon=True);send_thread=threading.Thread(target=send,daemon=True)
        query=None;publication=None
        try:
            query=start_landing(StreamSettings(self.client.settings,self.root,self.version),ready,self.root/'checkpoint',self.stream,'mqtt-ch-v1',publication_options=self.options,max_files_per_batch=20)
            collect_thread.start();deadline=time.monotonic()+15
            while time.monotonic()<deadline and not (collector._client and collector._client.is_connected()):time.sleep(.05)
            self.assertTrue(collector._client and collector._client.is_connected(),errors)
            time.sleep(.5);publisher.start();send_thread.start()
            if recovering:
                wait_marker('broker_stopped')
                publisher.stop();send_thread.join(timeout=5);collector.stop();collect_thread.join(timeout=5)
                self.assertFalse(send_thread.is_alive());self.assertFalse(collect_thread.is_alive())
                unexpected=[e for e in errors if not (isinstance(e,RuntimeError) and 'not currently connected' in str(e))]
                self.assertFalse(unexpected,unexpected)
                expected_disconnects.extend(str(e) for e in errors)
                errors.clear()  # Only the explicitly checked, deliberately disconnected old processes.

                pending_before_restart=len(publisher.fetch_unconfirmed_telemetry(limit=50))
                self.assertGreater(pending_before_restart,0)
                self.assertTrue(list((self.root/'outbox').glob('*.db')) and list((self.root/'spool').glob('*.db')))
                saved_ready={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ready.glob('batch-*.jsonl')}
                self.assertTrue(saved_ready,'At least one durable raw-ready batch must predate restart')
                mark('broker_failure_observed');wait_marker('broker_restarted')
                collector=MqttCollector(spool_dir=self.root/'spool',ready_dir=ready,mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),
                    client_id=collector_id,clean_session=False,batch_size=5,batch_timeout=.2)
                publisher=OutboxPublisher(outbox_dir=self.root/'outbox',mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),client_id=publisher_id,max_inflight=10)
                collect_thread=threading.Thread(target=collect,daemon=True);send_thread=threading.Thread(target=send,daemon=True)
                collect_thread.start();deadline=time.monotonic()+15
                while time.monotonic()<deadline and not (collector._client and collector._client.is_connected()):time.sleep(.05)
                self.assertTrue(collector._client and collector._client.is_connected())
                time.sleep(.5);publisher.start();send_thread.start()
                ready_preserved=all((ready/name).is_file() and hashlib.sha256((ready/name).read_bytes()).hexdigest()==hashed for name,hashed in saved_ready.items())
                self.assertTrue(ready_preserved)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                self.assertFalse(errors,errors);self.assertTrue(query.isActive,str(query.exception()))
                pointers=self.client.query('SELECT DISTINCT generation,publication_id FROM replay_pointer WHERE scope_id={scope:String}',{'scope':self.scope})
                if pointers:
                    publication=max(pointers,key=lambda p:int(p['generation']))['publication_id']
                    meta,events=read_publication(self.client,publication)
                    if meta[-1]['complete'] and len(events)==30 and not send_thread.is_alive():break
                time.sleep(.1)
            else:self.fail('MQTT/Spark replay did not reconcile by deadline')
            self.assertFalse(errors,errors)
            meta,events=read_publication(self.client,publication)
            self.assertTrue(meta[-1]['checksum_verified']);self.assertEqual(meta[-1]['missing_sequences'],[])
            self.assertEqual(meta[-1]['quarantined'],0)
            replay=evaluate_publication(self.client,publication,climate_rule=map_historical_rule(self.client,publication,climate_rule.rule_version),
                drain_rule=map_historical_rule(self.client,publication,drain_rule.rule_version))
            start,end=datetime(2019,12,16),datetime(2019,12,16,2,30)
            q=QueryContext(historical,batch['publication_id'],'fixture-snapshot','agc-cycle-AICU',('AICU',),date(2019,12,16),date(2019,12,17))
            ca,da=ClimateAdapter(self.client),DrainAdapter(self.client);cp=ca.publication(batch['publication_id']);dp=da.publication(batch['publication_id'])
            crows=ca.read_climate_slots(q,cp,window_start=start,window_end=end);drows=da.read_drain_slots(q,dp,window_start=start,window_end=end)
            cycle=CycleWindow('agc-cycle-AICU','AICU',date(2019,12,16),date(2019,12,18))
            cctx=IntradayContext(historical,cycle,('AICU',),start,end,'climate',RuleBasis.REQUESTED)
            dctx=IntradayContext(historical,cycle,('AICU',),start,end,'drain_process_computer',RuleBasis.MEASURED)
            bc=evaluate_rule_schedule(cctx,(climate_rule,),climate_slots(crows,RuleBasis.REQUESTED),('temperature','co2'))
            bd=evaluate_rule_schedule(dctx,(drain_rule,),drain_slots(drows),('ec','ph'))
            self.assertEqual(replay.climate.metric_results,bc.metric_results);self.assertEqual(replay.drain.metric_results,bd.metric_results)
            self.assertEqual(replay.climate.episodes,bc.episodes);self.assertEqual(replay.drain.episodes,bd.episodes)
            self.assertEqual(replay.climate.joint_results,bc.joint_results);self.assertEqual(replay.drain.joint_results,bd.joint_results)
            self.assertEqual(bc.metric_results[0].expected_minutes,Decimal(150));self.assertEqual(bc.metric_results[0].out_minutes,Decimal(50))
            self.assertEqual(bc.metric_results[1].valid_minutes,Decimal(145));self.assertEqual(bd.metric_results[0].out_minutes,Decimal(50))
            self.assertEqual(bd.metric_results[1].suspect_minutes,Decimal(5));self.assertEqual(bd.joint_results[0].joint_out_minutes,Decimal(45))
            streamed_c,streamed_d=build_replay_rows(events,self.cfg)
            self.assertEqual(len(crows),len(streamed_c));self.assertEqual(len(drows),len(streamed_d))
            for historical_row,replay_row in zip(crows,streamed_c,strict=True):
                self.assertEqual(historical_row.slot_start,replay_row.slot_start)
                for code,quality in historical_row.measure_quality.items():self.assertEqual(replay_row.measure_quality[code],quality)
            evidence={'status':'passed','source_events':30,'raw_ready_files':len(list(ready.glob('batch-*.jsonl'))),'replay_publication':publication,
                'batch_publication':batch['publication_id'],'expected_minutes':'150','temperature_out_minutes':'50','ec_out_minutes':'50','ph_suspect_minutes':'5','joint_out_minutes':'45',
                'expected_old_process_disconnects':len(expected_disconnects),'broker_restart':recovering,'collector_publisher_restart':recovering,'ready_files_preserved':ready_preserved,'pending_before_restart':pending_before_restart,
                'complete':meta[-1]['complete'],'checksum_verified':meta[-1]['checksum_verified'],'batch_stream_metrics_episodes_quality_match':True,'postgres_env_present':any(os.environ.get(k) for k in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'))}
            print(json.dumps(evidence))
            if os.environ.get('GREENHOUSE_CH_SENSOR_EVIDENCE'):Path(os.environ['GREENHOUSE_CH_SENSOR_EVIDENCE']).write_text(json.dumps(evidence,indent=2)+'\n')
        finally:
            publisher.stop();collector.stop();collect_thread.join(timeout=5)
            if send_thread.ident:send_thread.join(timeout=5)
            if query:query.stop()
            spark=SparkSession.getActiveSession()
            if spark:spark.stop()
