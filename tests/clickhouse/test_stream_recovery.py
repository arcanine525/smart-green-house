"""Host-coordinated real ClickHouse outage and ambiguous ACK recovery."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import time
import unittest

import test_stream_store as fixtures
from greenhouse.clickhouse.client import Client,ClickHouseError
from greenhouse.clickhouse.stream_store import StreamSettings,receipts,commit_batch
from greenhouse.clickhouse.replay_publication import read_publication
from replay_fixtures import row


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_RECOVERY_CONTROL'),'host-controlled isolated services required')
class StreamRecoveryTest(unittest.TestCase):
    setUp=fixtures.StreamStoreTest.setUp

    def marker(self,name):
        root=Path(os.environ['GREENHOUSE_CH_RECOVERY_CONTROL']);root.mkdir(parents=True,exist_ok=True)
        (root/name).write_text('ready\n')

    def wait(self,name):
        path=Path(os.environ['GREENHOUSE_CH_RECOVERY_CONTROL'])/name;deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if path.exists():return
            time.sleep(.1)
        self.fail('Host recovery marker timeout: '+name)

    def test_clickhouse_restart_and_lost_ack_preserve_source_checkpoint_and_old_pin(self):
        from greenhouse.sensor.stream_landing import start_landing
        from pyspark.sql import SparkSession
        ready=self.root/'ready';ready.mkdir();checkpoint=self.root/'checkpoint'
        (ready/'batch-0001.jsonl').write_text('\n'.join(json.dumps(r) for r in self.rows)+'\n')
        settings=StreamSettings(replace(self.client.settings,timeout=2),self.root,self.version)
        query=None
        try:
            query=start_landing(settings,ready,checkpoint,self.stream,'outage-ch-v1',available_now=True,publication_options=self.options,max_files_per_batch=1)
            self.assertTrue(query.awaitTermination(120));self.assertTrue((checkpoint/'commits/0').exists())
            pointer=self.client.query('SELECT DISTINCT publication_id FROM replay_pointer WHERE scope_id={scope:String}',{'scope':self.scope})
            old=pointer[0]['publication_id'];self.assertTrue(read_publication(self.client,old)[0][-1]['complete'])
            query=start_landing(settings,ready,checkpoint,self.stream,'outage-ch-v1',publication_options=self.options,max_files_per_batch=1)
            self.marker('db_ready_to_stop');self.wait('db_stopped')
            changed=copy.deepcopy(self.rows[0]['payload']);changed['payload']['actual_measurements']['Tair']='99'
            delivery=row(changed,'telemetry_messages',2)
            (ready/'batch-0002.jsonl').write_text(json.dumps(delivery)+'\n')
            failed=False
            try:query.awaitTermination(60)
            except Exception:failed=True
            self.assertTrue(failed,'A stopped ClickHouse must fail the batch, not advance checkpoint')
            self.assertFalse((checkpoint/'commits/1').exists())
            self.marker('db_failure_observed');self.wait('db_restarted')
            query=start_landing(settings,ready,checkpoint,self.stream,'outage-ch-v1',available_now=True,publication_options=self.options,max_files_per_batch=1)
            self.assertTrue(query.awaitTermination(120));self.assertTrue((checkpoint/'commits/1').exists())
            self.assertEqual(len(receipts(self.client,stream=self.stream,generation='outage-ch-v1')),2)
            pointers=self.client.query('SELECT DISTINCT generation,publication_id FROM replay_pointer WHERE scope_id={scope:String}',{'scope':self.scope})
            new=max(pointers,key=lambda p:int(p['generation']))['publication_id']
            meta,events=read_publication(self.client,new)
            self.assertNotEqual(old,new);self.assertFalse(meta[-1]['complete']);self.assertFalse(events)
            self.assertTrue(read_publication(self.client,old)[0][-1]['complete'])
            class LostAck(Client):
                once=True
                def execute(client,sql,*args,**kwargs):
                    result=super().execute(sql,*args,**kwargs)
                    if client.once and sql.startswith('INSERT INTO stream_delivery '):
                        client.once=False
                        raise ClickHouseError('Injected lost ACK after server INSERT')
                    return result
            ambiguous=LostAck(self.client.settings)
            with self.assertRaisesRegex(ClickHouseError,'Injected lost ACK'):
                commit_batch(ambiguous,self.root,self.stream,'outage-ch-v1',2,self.rows[:1],scope_id=self.scope,config_version=self.version)
            self.assertEqual(len(receipts(self.client,stream=self.stream,generation='outage-ch-v1')),2)
            # The shared journal reconciles the server query ID before retry.
            self.assertTrue(commit_batch(self.client,self.root,self.stream,'outage-ch-v1',2,self.rows[:1],scope_id=self.scope,config_version=self.version))
            self.assertFalse(commit_batch(self.client,self.root,self.stream,'outage-ch-v1',2,self.rows[:1],scope_id=self.scope,config_version=self.version))
            self.assertEqual(len(receipts(self.client,stream=self.stream,generation='outage-ch-v1')),3)
            report={'status':'passed','actual_clickhouse_stop_start':True,'checkpoint_failed_batch_not_advanced':True,
                'same_generation_resume':True,'ambiguous_server_insert_reconciled':True,'batch_receipts_after_retry':3,'old_complete_pin_unchanged':True,
                'late_conflict_new_publication_incomplete':True,'old_publication':old,'new_publication':new}
            (Path(os.environ['GREENHOUSE_CH_RECOVERY_CONTROL'])/'recovery.json').write_text(json.dumps(report,indent=2)+'\n')
            self.marker('db_recovery_passed')
        finally:
            if query:
                try:query.stop()
                except Exception:pass
            spark=SparkSession.getActiveSession()
            if spark:spark.stop()
