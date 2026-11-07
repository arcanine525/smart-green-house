"""Real Spark driver recovery against an isolated ClickHouse, no PostgreSQL."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

import test_stream_store as fixtures
from greenhouse.clickhouse.replay_publication import read_publication
from greenhouse.clickhouse.stream_store import receipts,StreamSettings


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_STREAM_TEST')=='1','processing image and isolated stream services required')
class StreamIntegrationTest(unittest.TestCase):
    setUp=fixtures.StreamStoreTest.setUp

    def child(self,script,logname,*,expected=0):
        path=self.root/(logname+'.py');path.write_text(script)
        logfile=self.root/(logname+'.log')
        with logfile.open('w') as log:
            process=subprocess.Popen([sys.executable,str(path)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                code=process.wait(timeout=180)
            finally:
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
        self.assertEqual(code,expected,logfile.read_text()[-12000:])
        return logfile.read_text()

    def script(self,ready,checkpoint,*,crash=False):
        arguments=['--backend','clickhouse','--coordinator',str(self.root),'--replay-config-version',self.version,'--ready-dir',str(ready),'--checkpoint-dir',str(checkpoint),'--stream-id',self.stream,'--generation','clickhouse-v1','--available-now']
        for key,value in self.options.items():arguments+=['--'+key.replace('_','-'),value]
        return '''from pathlib import Path
import os
from greenhouse.clickhouse.client import ClickHouseSettings
from greenhouse.clickhouse.stream_store import StreamSettings
from greenhouse.sensor.stream_landing import start_landing
'''+('''import greenhouse.clickhouse.replay_publication as sink
original=sink.commit_and_publish
def lost_checkpoint(*args,**kwargs):
 result=original(*args,**kwargs)
 os._exit(86)
sink.commit_and_publish=lost_checkpoint
''' if crash else '')+f'''
from greenhouse.cli import main
assert main(['stream',*{arguments!r}])==0
from pyspark.sql import SparkSession
spark=SparkSession.getActiveSession()
if spark:spark.stop()
'''

    def test_real_spark_death_after_database_publication_before_checkpoint_and_process_restart(self):
        ready=self.root/'ready';ready.mkdir();checkpoint=self.root/'checkpoint'
        (ready/'batch-0001.jsonl').write_text('\n'.join(json.dumps(r) for r in self.rows)+'\n')
        (ready/'manifest.json').write_text('{}')
        self.child(self.script(ready,checkpoint,crash=True),'driver-crash',expected=86)
        batches=receipts(self.client,stream=self.stream,generation='clickhouse-v1')
        self.assertEqual(len(batches),1);self.assertEqual(int(batches[0]['rows']),3)
        pointers=self.client.query('SELECT DISTINCT publication_id FROM replay_pointer WHERE scope_id={scope:String}',{'scope':self.scope})
        self.assertEqual(len(pointers),1);pub=pointers[0]['publication_id']
        self.assertTrue(read_publication(self.client,pub)[0][-1]['complete'])
        self.assertFalse((checkpoint/'commits/0').exists())
        self.child(self.script(ready,checkpoint),'driver-resume')
        self.assertTrue((checkpoint/'commits/0').exists())
        self.child(self.script(ready,checkpoint),'driver-reopen')
        self.assertEqual(len(receipts(self.client,stream=self.stream,generation='clickhouse-v1')),1)
        count=self.client.query('SELECT count() AS n FROM stream_delivery WHERE stream_id={stream:String}',{'stream':self.stream})[0]['n']
        self.assertEqual(int(count),3)
        meta,events=read_publication(self.client,pub);self.assertTrue(meta[-1]['complete']);self.assertEqual(len(events),1)
        from greenhouse.sensor.stream_landing import start_landing
        marker=json.loads((checkpoint/'.greenhouse-stream-identity.json').read_text())
        self.assertEqual(marker['backend'],'clickhouse')
        # A PG-style marker with the SAME endpoint still cannot alias CH.
        pg_checkpoint=self.root/'old-postgres-checkpoint';pg_checkpoint.mkdir()
        for key in ('backend','configuration_scope','configuration_version','configuration_hash'):marker.pop(key)
        (pg_checkpoint/'.greenhouse-stream-identity.json').write_text(json.dumps(marker,sort_keys=True))
        settings=StreamSettings(self.client.settings,self.root,self.version)
        with self.assertRaisesRegex(ValueError,'checkpoint belongs'):
            start_landing(settings,ready,pg_checkpoint,self.stream,'clickhouse-v1',available_now=True,publication_options=self.options)
        with self.assertRaisesRegex(ValueError,'checkpoint belongs'):
            start_landing(settings,ready,checkpoint,self.stream,'wrong-generation',available_now=True,publication_options=self.options)
