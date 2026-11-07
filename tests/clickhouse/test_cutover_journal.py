"""Crash boundaries, durable accepted writes, namespace and rollback guards."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from greenhouse.clickhouse.client import Client, ClickHouseSettings, ClickHouseError
from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse import cutover_journal as journal
from greenhouse.clickhouse.journal import execute_write, reconcile


class JournalUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.client=Client(ClickHouseSettings(password='local-fixture'))
        journal.enable(self.client,self.root,'fixture',{'publication':'locked','checksum':'fixture'})

    def test_backup_captures_committed_wal_and_chain_detects_changed_sql(self):
        journal.before_write(self.client,self.root,'one','INSERT INTO source_revision VALUES (1)')
        journal.terminal(self.root,'one','QueryFinish','response')
        # Hold a reader while SQLite backup copies the committed WAL state.
        db=sqlite3.connect(self.root/journal.NAME)
        self.addCleanup(db.close)
        db.execute('BEGIN');db.execute('SELECT * FROM session').fetchall()
        destination=self.root/'snapshot';destination.mkdir()
        receipt=journal.backup(self.root,destination/journal.NAME)
        self.assertGreater(receipt['bytes'],0)
        self.assertEqual(journal.inspect(self.root),journal.inspect(destination))
        with sqlite3.connect(destination/journal.NAME) as changed:
            changed.execute("UPDATE intent SET payload='{}'")
        with self.assertRaises(journal.CutoverJournalError):journal.inspect(destination)

    def test_unresolved_intent_blocks_all_new_writes(self):
        journal.before_write(self.client,self.root,'before-marker-crash','INSERT INTO stream_delivery VALUES (1)')
        with self.assertRaises(journal.CutoverJournalError):journal.inspect(self.root)
        with self.assertRaises(journal.CutoverJournalError):
            journal.before_write(self.client,self.root,'two','INSERT INTO source_revision VALUES (2)')
        self.assertIsNone(journal.inspect(self.root,require_terminal=False)['records'][0]['outcome'])

    def test_required_missing_journal_never_disables_rollback_protection(self):
        empty=self.root/'empty';empty.mkdir()
        with patch.dict(os.environ,{'GREENHOUSE_CH_REQUIRE_CUTOVER_JOURNAL':'1'}):
            with self.assertRaises(journal.CutoverJournalError):
                journal.before_write(self.client,empty,'one','INSERT INTO source_revision VALUES (1)')

    def test_paused_writes_and_wrong_namespace_fail_closed_even_for_derived_marts(self):
        foreign=Client(replace(self.client.settings,database='foreign'))
        with self.assertRaises(journal.CutoverJournalError):journal.before_write(foreign,self.root,'one','SELECT 1')
        self.assertIsNone(journal.before_write(self.client,self.root,'derived','INSERT INTO water_measure VALUES (1)'))
        journal.set_mode(self.client,self.root,'paused')
        with self.assertRaises(journal.CutoverJournalError):
            journal.before_write(self.client,self.root,'derived','INSERT INTO water_measure VALUES (1)')
        with self.assertRaises(journal.CutoverJournalError):
            journal.enable(self.client,self.root,'fixture',{'publication':'different'})
        journal.set_mode(self.client,self.root,'active')
        self.assertEqual(journal.inspect(self.root)['mode'],'active')


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server opt-in')
class JournalIntegrationTest(JournalUnitTest):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.client=Client(ClickHouseSettings.from_env())
        journal.enable(self.client,self.root,'integration',{'publication':'isolated-fixture'})

    def test_lost_response_recovers_accepted_write_before_any_later_acknowledgment(self):
        real=self.client
        class LostAck:
            settings=real.settings
            def execute(self,sql,**kwargs):
                result=real.execute(sql,**kwargs)
                # Intention must already be committed outside the CH volume.
                state=journal.inspect(self_root,require_terminal=False)
                if not state['records'] or state['records'][-1]['outcome'] is not None:
                    raise AssertionError('Write intent was not durable before sending')
                raise ClickHouseError('Injected lost acknowledgment')
        self_root=self.root
        name='cutover_fixture_'+uuid4().hex
        try:
            with writer_lock(self.root):
                with self.assertRaises(ClickHouseError):
                    execute_write(LostAck(),self.root,'CREATE TABLE '+name+' (v UInt8) ENGINE=MergeTree ORDER BY v')
                self.assertTrue((self.root/'pending-write.json').exists())
                reconcile(real,self.root)
                execute_write(real,self.root,'INSERT INTO '+name+' VALUES (9)')
            state=journal.inspect(self.root)
            self.assertEqual([r['outcome']['status'] for r in state['records']],['QueryFinish','QueryFinish'])
            self.assertIn('query_log',state['records'][0]['outcome']['evidence'])
            self.assertEqual(real.query('SELECT v FROM '+name),[{'v':9}])
            self.assertFalse((self.root/'pending-write.json').exists())
        finally:real.execute('DROP TABLE IF EXISTS '+name)
