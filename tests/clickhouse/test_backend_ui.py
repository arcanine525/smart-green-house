"""All production views on one ClickHouse pin, including real simulator bytes."""
import base64
import copy
from dataclasses import replace
from datetime import date,datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_energy as fixtures
from test_threshold import revision
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.client import Client


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_UI_TEST')=='1','UI image and isolated ClickHouse required')
class BackendUITest(unittest.TestCase):
    setUp=fixtures.EnergyTest.setUp
    prepare=fixtures.EnergyTest.prepare

    def configure(self):
        before=dict(os.environ)
        self.addCleanup(lambda:(os.environ.clear(),os.environ.update(before)))
        for key in ('PGHOST','PGPORT','PGDATABASE','PGUSER','PGPASSWORD'):os.environ.pop(key,None)
        os.environ.update(GREENHOUSE_BACKEND='clickhouse',GREENHOUSE_RAW_DIR=str(self.raw),GREENHOUSE_CH_COORDINATOR=str(self.root))

    def app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).resolve().parents[2]/'src/greenhouse/app/home.py'),default_timeout=180).run()

    def healthy(self,app):
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertFalse(app.error,[e.value for e in app.error])

    def test_all_historical_screens_filters_quality_csv_and_backend_state(self):
        result,*_=self.prepare(include_climate=True)
        from greenhouse.clickhouse.schedules import register_rule_schedule
        for kind in ('climate','drain'):
            register_rule_schedule(self.client,self.root,revision(self.scope,kind,datetime(2020,3,2),datetime(2020,3,7)))
        self.configure()
        # Any accidental PostgreSQL connection makes this production test fail.
        with patch('psycopg.connect',side_effect=AssertionError('PostgreSQL fallback')):
            app=self.app();self.healthy(app)
            index=next(i for i,v in enumerate(app.selectbox(key='pub_choice').options) if self.scope in v)
            app.selectbox(key='pub_choice').select_index(index).run();self.healthy(app)
            app.date_input(key='window').set_value((date(2020,3,2),date(2020,3,2))).run()
            for screen in ('🍅 Thu hoạch (BR-05)','🌡️ Khí hậu (BR-01)','🧪 EC/pH drain (BR-03)',
                    '⚡ Năng lượng (BR-04)','💶 Chi phí & Kịch bản (BR-06)','🏠 Tổng quan vụ','🛡️ Chất lượng dữ liệu'):
                app.radio(key='screen_choice').set_value(screen).run();self.healthy(app)
                self.assertEqual(app.session_state['_historical_context']['publication_id'],result['publication_id'])
                self.assertEqual(app.session_state['_historical_context']['window'],(date(2020,3,2),date(2020,3,2)))
                if 'BR-' in screen:self.assertTrue(app.get('download_button'),screen)
            app.checkbox(key='quality_load_details').check().run();self.healthy(app)
            self.assertEqual(sum(app.dataframe[0].value['decisions']),308) # 30*10 + 6 + 2
            self.assertEqual(set(app.dataframe[1].value['reason_code']),{'off_mode_unverified','demo_zero_ph_suspect'})
            self.assertTrue(any('CSV chi tiết chất lượng' in b.label for b in app.get('download_button')))
            app.checkbox(key='quality_load_quarantine').check().run();self.healthy(app)
            # A different endpoint namespace must discard stale widgets/intents.
            app.session_state['_warehouse_identity']=('postgres','old','5432','old','old')
            app.session_state['cost_scenario_intent']={'old':'intent'}
            app.run();self.healthy(app)
            self.assertNotIn('cost_scenario_intent',app.session_state)
            self.assertEqual(app.session_state['_warehouse_identity'][0],'clickhouse')

    def test_quality_uses_frozen_audit(self):
        result,*_=self.prepare(include_climate=True,missing_heat=True)
        from greenhouse.clickhouse import quality_overview as q
        args=(self.client,result['publication_id'],'agc-cycle-Automatoes',date(2020,3,2),date(2020,3,6))
        rows=q.measures(*args);self.assertEqual(sum(r['decisions'] for r in rows),334)
        excluded=q.excluded(*args);self.assertEqual(len(excluded),3)
        rejected=q.quarantined(*args);self.assertEqual(len(rejected),1)
        self.assertIn('invalid_numeric',rejected[0]['reason_code'])
        # A later mutable ingestion disposition is outside the frozen audit.
        Repository(self.client,self.root)._insert('source_disposition',[dict(scope_id=self.scope,snapshot_id=self.snapshot,
            source_record_id='later-bogus',source_file='Automatoes/Resources.csv',line_number=9,disposition='quarantine',
            content_hash='0'*64,payload='{}')])
        self.assertEqual(q.quarantined(*args),rejected)

    def make_replay(self,publication):
        from greenhouse.sensor.simulator import SensorSimulator
        from greenhouse.clickhouse.replay_config import from_historical
        from greenhouse.clickhouse.stream_store import migrate as stream_schema
        from greenhouse.clickhouse.replay_publication import migrate as replay_schema,commit_and_publish
        self.replay='ui-replay:'+uuid4().hex
        outbox=self.root/'outbox'
        simulator=SensorSimulator(self.raw/'Automatoes/GreenhouseClimate.csv',self.replay,outbox,
            compartment_id='Automatoes',cycle_id='agc-cycle-Automatoes',snapshot_id=self.snapshot,snapshot_sha='c'*64,
            relative_path='Automatoes/GreenhouseClimate.csv',cursor_interval=5)
        self.assertEqual(simulator.run().expected_unique_events,30)
        self.replay_scope=simulator.data_scope_id
        cfg,_=from_historical(self.client,self.root,publication,replay_id=self.replay,scope_id=self.replay_scope,
            compartment_id='Automatoes',config_version='replay-v1')
        for migrate in (stream_schema,replay_schema):migrate(self.client,self.root)
        rows=[]
        with sqlite3.connect(outbox/'outbox.db') as db:
            for table,idcol in (('telemetry_messages','id'),('control_messages','control_sequence')):
                for rowid,raw in db.execute('SELECT '+idcol+',payload_bytes FROM '+table+' ORDER BY '+idcol):
                    payload=json.loads(raw)
                    rows.append(dict(raw_payload_base64=base64.b64encode(raw).decode(),raw_bytes_len=len(raw),payload_hash=hashlib.sha256(raw).hexdigest(),
                        is_valid=True,payload=payload,replay_id=payload['replay_id'],event_id=payload.get('event_id') or payload['control_message_id'],
                        received_at=datetime.now(timezone.utc).isoformat(),outbox_table=table,outbox_row_id=rowid))
        self.options={k:cfg[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')}
        self.stream='ui-stream:'+uuid4().hex
        return commit_and_publish(self.client,self.root,self.stream,'ui-gen-v1',0,rows,config_version='replay-v1',publication_options=self.options)[1]

    def test_replay_rules_exports_refresh_new_pin_and_auto_fragment(self):
        result,*_=self.prepare(include_climate=True)
        from greenhouse.clickhouse.schedules import register_rule_schedule
        from greenhouse.clickhouse.replay_publication import read_publication,commit_and_publish
        from replay_fixtures import row
        for kind in ('climate','drain'):
            register_rule_schedule(self.client,self.root,revision(self.scope,kind,datetime(2020,3,2),datetime(2020,3,7)))
        publication=self.make_replay(result['publication_id']);self.configure()
        with patch('psycopg.connect',side_effect=AssertionError('PostgreSQL fallback')):
            app=self.app();app.radio(key='screen_choice').set_value('📡 Sensor replay (BR-01/03)').run();self.healthy(app)
            index=next(i for i,v in enumerate(app.selectbox(key='replay_publication').options) if self.replay in v)
            app.selectbox(key='replay_publication').select_index(index).run();self.healthy(app)
            self.assertTrue(any('đối soát' in s.value for s in app.success))
            app.selectbox(key='replay_climate_rule').set_value('climate:'+self.scope+':v1').run()
            app.selectbox(key='replay_drain_rule').set_value('drain:'+self.scope+':v1').run();self.healthy(app)
            self.assertEqual(len(app.get('plotly_chart')),4)
            self.assertTrue(any(b.label=='Tải KPI và episode CSV' for b in app.get('download_button')))
            key=app.session_state['replay_publication']
            app.checkbox(key='replay_auto_refresh').check().run();self.healthy(app)
            app.checkbox(key='replay_auto_refresh').uncheck().run();self.healthy(app)
            meta,events=read_publication(self.client,publication)
            changed=copy.deepcopy(events[0]);changed['payload']['actual_measurements']['Tair']='99'
            newer=commit_and_publish(self.client,self.root,self.stream,'ui-gen-v1',1,[row(changed,'telemetry_messages',999)],
                config_version='replay-v1',publication_options=self.options)[1]
            self.assertNotEqual(newer,publication)
            app.button(key='replay_refresh').click().run();self.healthy(app)
            self.assertEqual(app.session_state['replay_publication'],key)
            self.assertTrue(any(newer in c.value for c in app.caption))
            self.assertTrue(any('Provisional' in w.value for w in app.warning))
            self.assertTrue(read_publication(self.client,publication)[0][-1]['complete'])

    def test_bad_settings_and_unreachable_backend_show_safe_states(self):
        self.configure();os.environ['CH_PORT']='credential-value-must-not-appear'
        app=self.app();self.assertFalse(app.exception)
        self.assertTrue(app.error);self.assertFalse(app.metric)
        self.assertNotIn('credential-value-must-not-appear',str(app))
        os.environ['CH_PORT']='8123';os.environ['CH_HOST']='127.0.0.1'
        app=self.app();self.assertFalse(app.exception)
        self.assertTrue(app.error);self.assertFalse(app.metric)

    def test_empty_unmigrated_and_missing_replay_schema_are_distinct_states(self):
        self.configure();database='test_ui_'+uuid4().hex
        self.client.execute('CREATE DATABASE '+database)
        self.addCleanup(lambda:self.client.execute('DROP DATABASE '+database))
        os.environ['CH_DATABASE']=database
        app=self.app();self.assertFalse(app.exception);self.assertTrue(app.error);self.assertFalse(app.metric)
        isolated=Client(replace(self.client.settings,database=database))
        from greenhouse.clickhouse.schema import migrate
        migrate(isolated,self.root)
        from greenhouse.clickhouse.water import migrate as migrate_water
        migrate_water(isolated,self.root)
        app=self.app();self.healthy(app)
        self.assertTrue(any('Chưa có publication' in e.value for e in app.info))
        app.radio(key='screen_choice').set_value('📡 Sensor replay (BR-01/03)').run();self.healthy(app)
        self.assertTrue(any('Chưa có replay publication' in e.value for e in app.info))

    def test_verified_action_cache_ends_at_connection_close(self):
        result,*_=self.prepare(include_climate=True)
        from greenhouse.clickhouse.registry import publication_envelope
        from greenhouse.clickhouse.water import WaterMart
        client=Client(self.client.settings).begin_action();original=WaterMart.verify;calls=[]
        def verify(mart,*args):calls.append(args[:2]);return original(mart,*args)
        with patch.object(WaterMart,'verify',new=verify):
            first=publication_envelope(client,result['publication_id']);n=len(calls)
            self.assertGreater(n,0)
            self.assertEqual(publication_envelope(client,result['publication_id']),first)
            self.assertEqual(len(calls),n)
            client.close();self.assertIsNone(client._action_cache)
            self.assertEqual(publication_envelope(client,result['publication_id']),first)
            self.assertGreater(len(calls),n)
