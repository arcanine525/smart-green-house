"""Read-action proofs must survive reopen and reject physical/config changes."""
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_energy as fixtures
from greenhouse.clickhouse import proof_cache,registry
from greenhouse.clickhouse.encoding import digest,canonical_json
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.client import Client,ClickHouseSettings,ClickHouseError


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class ProofCacheTest(unittest.TestCase):
    prepare=fixtures.EnergyTest.prepare

    def setUp(self):
        admin=Client(ClickHouseSettings.from_env());database='proof_'+uuid4().hex
        admin.execute('CREATE DATABASE '+database)
        self.addCleanup(admin.execute,'DROP DATABASE '+database+' SYNC')
        environment=patch.dict(os.environ,{'CH_DATABASE':database})
        environment.start();self.addCleanup(environment.stop)
        fixtures.EnergyTest.setUp(self)
        self.addCleanup(self.client.close)

    def pin(self,publication):
        self.client.close();self.client.begin_action(shared_proofs=True)
        return registry.publication_envelope(self.client,publication)

    def test_reopen_hit_fresh_config_validation_and_exact_physical_retry_invalidation(self):
        proof_cache.clear();self.addCleanup(proof_cache.clear)
        result,*_=self.prepare(include_climate=True);pub=result['publication_id']
        with patch.object(registry,'_envelope',wraps=registry._envelope) as loader:
            original=self.pin(pub);self.assertEqual(loader.call_count,1)
            again=self.pin(pub);self.assertEqual(again,original);self.assertEqual(loader.call_count,1)
            # Returned envelopes belong to the action, not the shared store.
            again['receipts']['core']['rows']=-1
            self.assertEqual(self.pin(pub),original);self.assertEqual(loader.call_count,1)
            repo=Repository(self.client,self.root)
            repo.register_configuration('query_only',self.scope,'new-version','new-version:'+self.scope,{'value':'new'})
            self.assertEqual(self.pin(pub),original);self.assertEqual(loader.call_count,1)
            row=self.client.query('SELECT * FROM source_measure WHERE scope_id={scope:String} LIMIT 1',{'scope':self.scope})[0]
            repo._insert('source_measure',[row])
            self.assertEqual(self.pin(pub),original);self.assertEqual(loader.call_count,2)
            repo._insert('source_measure',[dict(row,value_exact='999',value_decimal='999',projection_status='exact')])
            with self.assertRaises(ValueError):self.pin(pub)

    def test_new_action_rejects_corrupt_unselected_configuration_and_missing_source_table(self):
        proof_cache.clear();self.addCleanup(proof_cache.clear)
        result,*_=self.prepare();pub=result['publication_id'];self.pin(pub)
        repo=Repository(self.client,self.root)
        repo.register_configuration('query_only',self.scope,'new-version','new-version:'+self.scope,{'value':'new'})
        row=self.client.query("SELECT * FROM configuration_envelope WHERE kind='query_only'")[0]
        row['payload']=canonical_json({'value':'corrupt'});row['content_hash']=digest(json.loads(row['payload']))
        repo._insert('configuration_envelope',[row])
        with self.assertRaisesRegex(ValueError,'Conflicting configuration'):self.pin(pub)
        # Source-table removal must not return a previously verified envelope.
        self.client.execute('DROP TABLE source_measure')
        with self.assertRaises(ClickHouseError):self.pin(pub)

    def test_pending_mutation_blocks_reuse_before_any_changed_part_is_visible(self):
        self.pending_mutation('source_measure')

    def test_change_after_verification_cannot_leak_an_action_cached_envelope(self):
        proof_cache.clear();self.addCleanup(proof_cache.clear)
        result,*_=self.prepare();pub=result['publication_id']
        loader=registry._envelope;repo=Repository(self.client,self.root)
        def change_after_read(client,scope,selected):
            envelope=loader(client,scope,selected)
            row=client.query('SELECT * FROM source_measure WHERE scope_id={scope:String} LIMIT 1',{'scope':scope})[0]
            repo._insert('source_measure',[dict(row,value_exact='999',value_decimal='999',projection_status='exact')])
            return envelope
        with patch.object(registry,'_envelope',side_effect=change_after_read):
            with self.assertRaisesRegex(ValueError,'Warehouse changed|measures incomplete or conflicting'):self.pin(pub)
        self.assertNotIn(('historical-envelope',pub),self.client._action_cache)
        with self.assertRaisesRegex(ValueError,'measures incomplete or conflicting'):
            registry.publication_envelope(self.client,pub)

    def test_pending_configuration_mutation_blocks_reuse_before_registry_changes(self):
        self.pending_mutation('configuration_envelope')

    def pending_mutation(self,table):
        proof_cache.clear();self.addCleanup(proof_cache.clear)
        result,*_=self.prepare();pub=result['publication_id'];self.pin(pub)
        self.client.execute('SYSTEM STOP MERGES '+table)
        try:
            self.client.execute('ALTER TABLE '+table+' DELETE WHERE 0 SETTINGS mutations_sync=0')
            with self.assertRaisesRegex(ValueError,'mutation is pending'):self.pin(pub)
        finally:self.client.execute('SYSTEM START MERGES '+table)
