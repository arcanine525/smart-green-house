import json
import os
from pathlib import Path
import tempfile
import unittest

from greenhouse.clickhouse.client import Client, ClickHouseSettings, ClickHouseError
from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.journal import execute_write, reconcile, PendingWrite


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','explicit isolated server required')
class JournalTest(unittest.TestCase):
    def setUp(self):
        self.client=Client(ClickHouseSettings.from_env())
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)

    def test_lost_ack_keeps_fence_until_actual_query_is_terminal(self):
        real=self.client
        class LostAck:
            def execute(self,sql,**kwargs):
                real.execute(sql,**kwargs)
                raise ClickHouseError('Injected lost response after server completion')
        with writer_lock(self.root):
            with self.assertRaises(ClickHouseError):
                execute_write(LostAck(),self.root,'CREATE TABLE IF NOT EXISTS journal_fixture (v UInt8) ENGINE=MergeTree ORDER BY v')
            self.assertTrue((self.root/'pending-write.json').exists())
            reconcile(self.client,self.root)
            self.assertFalse((self.root/'pending-write.json').exists())

    def test_no_terminal_evidence_does_not_release_fence(self):
        from uuid import uuid4
        marker=self.root/'pending-write.json'
        marker.write_text(json.dumps({'format':1,'query_id':'never-sent-'+uuid4().hex,'sql_hash':'fixture'}))
        with writer_lock(self.root),self.assertRaises(PendingWrite):
            reconcile(self.client,self.root)
        self.assertTrue(marker.exists())

    def test_killed_writer_releases_lock_but_not_pending_server_query_fence(self):
        import multiprocessing
        import time
        from uuid import uuid4
        name='crash_'+uuid4().hex
        self.client.execute('CREATE TABLE '+name+' (v UInt8) ENGINE=MergeTree ORDER BY v')
        context=multiprocessing.get_context('spawn')
        worker=context.Process(target=slow_write_worker,args=(str(self.root),name))
        worker.start()
        try:
            deadline=time.monotonic()+10
            running=False
            while time.monotonic()<deadline:
                marker=self.root/'pending-write.json'
                if marker.exists():
                    query=json.loads(marker.read_text())['query_id']
                    running=bool(self.client.query('SELECT query_id FROM system.processes WHERE query_id={id:String}',{'id':query}))
                    if running:
                        break
                time.sleep(.03)
            self.assertTrue(running,'Did not observe the server-side write before killing the process')
            worker.kill(); worker.join(5)
            self.assertFalse(worker.is_alive())
            blocked=False
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                try:
                    with writer_lock(self.root,self.client):
                        pass
                    break
                except PendingWrite:
                    blocked=True
                    time.sleep(.05)
            # A disconnect may cancel the server query immediately. In that case
            # terminal log evidence permits recovery without an observed wait.
            self.assertFalse((self.root/'pending-write.json').exists())
            count=int(self.client.query('SELECT count() AS n FROM '+name)[0]['n'])
            self.assertIn(count,(0,1))
        finally:
            if worker.is_alive():
                worker.kill(); worker.join()


def slow_write_worker(root,table):
    client=Client(ClickHouseSettings.from_env())
    with writer_lock(root,client):
        execute_write(client,root,'INSERT INTO '+table+' SELECT toUInt8(sleep(2))')
