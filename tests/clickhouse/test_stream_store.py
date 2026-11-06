import copy
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schema import migrate as core_schema
from greenhouse.clickhouse.stream_store import migrate as stream_schema,commit_batch,receipts
from greenhouse.clickhouse.replay_publication import migrate as replay_schema,publish_replay,read_publication,commit_and_publish
from greenhouse.clickhouse.replay_config import register_config
from greenhouse.clickhouse.replay_evaluation import evaluate_publication
from greenhouse.contracts import RuleBasis,RuleScheduleRevision,RuleScheduleSegment,rule_segment_sort_key
from replay_fixtures import completed_rows,event,row,config


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class StreamStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.client=Client(ClickHouseSettings.from_env())
        self.scope='replay-scope:'+uuid4().hex;self.replay='replay:'+uuid4().hex;self.version='config-v1';self.stream='stream:'+uuid4().hex
        for migrate in (core_schema,stream_schema,replay_schema):migrate(self.client,self.root)
        self.cfg=config(self.replay,self.scope);register_config(self.client,self.root,self.scope,self.version,self.cfg)
        self.rows=completed_rows(self.replay,self.scope)
        self.options={k:self.cfg[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')}

    def publish(self):return publish_replay(self.client,self.root,config_version=self.version,**self.options)

    def land(self,batch,rows,generation='clickhouse-generation'):
        return commit_batch(self.client,self.root,self.stream,generation,batch,rows,scope_id=self.scope,config_version=self.version)

    def rules(self,kind):
        if kind=='climate':
            segments=[RuleScheduleSegment(m,'climate',RuleBasis.REQUESTED,datetime(2019,12,16),datetime(2019,12,18),delta=Decimal(0)) for m in ('temperature','co2')]
        else:
            segments=[RuleScheduleSegment(m,'drain_process_computer',RuleBasis.MEASURED,datetime(2019,12,16),datetime(2019,12,18),lower=Decimal(lo),upper=Decimal(hi)) for m,lo,hi in (('ec','2','5'),('ph','5.5','6.5'))]
        return RuleScheduleRevision(kind+':'+self.scope,kind,1,self.scope,'AICU','Synthetic fixture only',True,'demo_rule',tuple(sorted(segments,key=rule_segment_sort_key)))

    def test_batch_replay_manifest_quality_and_full_original_publication_pin(self):
        self.assertTrue(self.land(0,reversed(self.rows)))
        self.assertFalse(self.land(0,self.rows))
        pub=self.publish();meta,events=read_publication(self.client,pub)
        self.assertTrue(meta[-1]['complete']);self.assertEqual(len(events),1)
        evaluation=evaluate_publication(self.client,pub,climate_rule=self.rules('climate'),drain_rule=self.rules('drain'))
        self.assertFalse(evaluation.provisional)
        self.assertEqual(evaluation.climate.metric_results[0].out_minutes,Decimal(5))
        self.assertEqual(evaluation.climate.metric_results[1].valid_minutes,Decimal(0))
        self.assertEqual(evaluation.drain.metric_results[0].out_minutes,Decimal(5))
        self.assertEqual(evaluation.drain.metric_results[1].suspect_minutes,Decimal(5))
        changed=copy.deepcopy(events[0]);changed['payload']['actual_measurements']['Tair']='99'
        self.land(1,[row(changed,'telemetry_messages',2)])
        newer=self.publish();self.assertNotEqual(pub,newer)
        newmeta,newevents=read_publication(self.client,newer)
        self.assertFalse(newmeta[-1]['complete']);self.assertTrue(newmeta[-1]['conflicts']);self.assertFalse(newevents)
        oldmeta,oldevents=read_publication(self.client,pub)
        self.assertTrue(oldmeta[-1]['complete']);self.assertEqual(oldevents,events)

    def test_unsealed_prefix_is_invisible_and_same_content_retry_is_safe(self):
        insert=Repository._insert
        def interrupted(repo,table,rows,**kwargs):
            result=insert(repo,table,rows,**kwargs)
            if table=='stream_delivery':raise RuntimeError('Injected crash after delivery write')
            return result
        with patch.object(Repository,'_insert',new=interrupted):
            with self.assertRaisesRegex(RuntimeError,'Injected crash'):self.land(0,self.rows)
        self.assertFalse(receipts(self.client,scope=self.scope))
        register_config(self.client,self.root,self.scope,'unsealed-different-version',self.cfg)
        with self.assertRaisesRegex(ValueError,'unsealed delivery from a different replay configuration'):
            commit_batch(self.client,self.root,self.stream,'clickhouse-generation',1,self.rows,scope_id=self.scope,config_version='unsealed-different-version')
        empty=self.publish();self.assertEqual(read_publication(self.client,empty)[1],())
        different=copy.deepcopy(self.rows);different[0]['received_at']='2026-11-07T00:00:02+00:00'
        with self.assertRaisesRegex(ValueError,'Unsealed batch retry changed'):self.land(0,different)
        self.assertTrue(self.land(0,self.rows))
        pub=self.publish();self.assertTrue(read_publication(self.client,pub)[0][-1]['complete'])
        self.assertEqual(read_publication(self.client,empty)[1],())

    def test_bad_ready_record_is_accounted_and_configuration_generation_cannot_alias(self):
        self.land(0,self.rows+[{'malformed':0.1}])
        meta,events=read_publication(self.client,self.publish())
        self.assertFalse(meta[-1]['complete']);self.assertEqual(meta[-1]['quarantined'],1);self.assertEqual(len(events),1)
        register_config(self.client,self.root,self.scope,'v2',self.cfg)
        with self.assertRaisesRegex(ValueError,'different replay configuration'):
            commit_batch(self.client,self.root,self.stream,'clickhouse-generation',1,self.rows,scope_id=self.scope,config_version='v2')
        with self.assertRaisesRegex(ValueError,'different content'):self.land(0,self.rows)
        self.assertTrue(self.land(0,self.rows,generation='new-explicit-generation'))

    def test_source_provenance_is_bound_to_registered_snapshot_and_file(self):
        altered=copy.deepcopy(self.rows[0]['payload']);altered['source_provenance']['file_checksum']='d'*64
        self.land(0,[row(altered,'telemetry_messages',1),*self.rows[1:]])
        meta,events=read_publication(self.client,self.publish())
        self.assertFalse(meta[-1]['complete']);self.assertTrue(meta[-1]['scope_conflicts']);self.assertFalse(events)
