import os
import unittest
from uuid import uuid4

from greenhouse.clickhouse.publication import Publications
from greenhouse.clickhouse.encoding import digest, canonical_json
from greenhouse.clickhouse.coordinator import writer_lock
import test_repository as repository_fixtures


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','explicit isolated server required')
class PublicationTest(unittest.TestCase):
    setUp=repository_fixtures.RepositoryTest.setUp
    record=repository_fixtures.RepositoryTest.record

    def load(self):
        revision,_=self.repo.append_record(self.scope,self.record())
        load=uuid4().hex
        with writer_lock(self.directory.name,self.client):
            self.repo._insert('load_member',[{'scope_id':self.scope,'load_id':load,'revision_id':revision}])
            self.repo._insert('load_receipt',[{'scope_id':self.scope,'load_id':load,'schema_version':'ch-core-v1',
                'input_hash':digest('fixture'),'counts_json':canonical_json({'accepted':1}),
                'membership_checksum':digest([revision]),'membership_count':1}])
        return load

    def test_resume_every_transition_and_pin_previous_complete(self):
        publisher=Publications(self.client,self.directory.name)
        load=self.load()
        for stage in ('PREPARING','WRITING','VERIFIED','COMPLETE'):
            workflow='workflow-'+uuid4().hex
            previous=None
            try:
                previous=publisher.pin(self.scope)
            except ValueError:
                pass
            with self.assertRaises(RuntimeError):
                publisher.publish(self.scope,workflow,load,fail_at=stage)
            if stage!='COMPLETE':
                if previous:
                    self.assertEqual(publisher.pin(self.scope),previous)
                else:
                    with self.assertRaises(ValueError):
                        publisher.pin(self.scope)
            complete=publisher.publish(self.scope,workflow,load)
            self.assertEqual(publisher.publish(self.scope,workflow,load),complete)
            self.assertEqual(publisher.pin(self.scope),complete)
            if previous:
                self.assertEqual(publisher.pin(self.scope,publication_id=previous['publication_id']),previous)

    def test_failed_bundle_is_invisible_until_fixed_and_reverified(self):
        load=self.load()
        bad={'fail':True}
        def validator(scope,publication,core):
            if bad['fail']:
                raise ValueError('Injected incomplete typed mart')
            return {'version':'fixture-bundle-v1','rows':len(core),'checksum':digest([r['revision_id'] for r in core])}
        publisher=Publications(self.client,self.directory.name,bundles={'fixture':validator})
        workflow=uuid4().hex
        with self.assertRaises(ValueError):
            publisher.publish(self.scope,workflow,load)
        with self.assertRaises(ValueError):
            publisher.pin(self.scope)
        bad['fail']=False
        complete=publisher.publish(self.scope,workflow,load)
        self.assertIn('fixture',complete['receipts'])
        self.assertEqual(publisher.pin(self.scope),complete)
        bad['fail']=True
        with self.assertRaises(ValueError):
            publisher.pin(self.scope)

    def test_retry_cannot_change_load_or_required_bundle_registry(self):
        publisher=Publications(self.client,self.directory.name)
        load=self.load(); workflow=uuid4().hex
        complete=publisher.publish(self.scope,workflow,load)
        with self.assertRaises(ValueError):
            publisher.publish(self.scope,workflow,'another-load')
        self.repo.register_configuration('rule',self.scope,'v2',uuid4().hex,{'value':'2'})
        self.assertEqual(publisher.pin(self.scope),complete)

    def test_two_process_publishers_share_lock_and_one_workflow_identity(self):
        import multiprocessing
        context=multiprocessing.get_context('spawn')
        queue=context.Queue()
        load=self.load(); workflow=uuid4().hex
        workers=[context.Process(target=publish_worker,args=(self.directory.name,self.scope,workflow,load,queue)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
            if worker.is_alive():
                worker.kill(); worker.join()
                self.fail('Concurrent publisher did not finish')
            self.assertEqual(worker.exitcode,0)
        results=[queue.get(timeout=2) for _ in workers]
        self.assertTrue(all(r[0]=='ok' for r in results),results)
        self.assertEqual(results[0][1],results[1][1])


    def test_partial_typed_bundle_build_retries_without_double_count(self):
        from decimal import Decimal
        self.client.execute('CREATE TABLE IF NOT EXISTS typed_bundle_fixture (scope_id String, publication_id String, amount Decimal(18,6)) ENGINE=MergeTree ORDER BY (scope_id,publication_id)')
        load=self.load(); failed={'once':True}
        def build(scope,publication,core):
            self.repo._insert('typed_bundle_fixture',[{'scope_id':scope,'publication_id':publication,'amount':'9.36'}])
            if failed['once']:
                failed['once']=False
                raise RuntimeError('Injected crash after typed mart write')
        def verify(scope,publication,core):
            rows=self.client.query('SELECT DISTINCT amount FROM typed_bundle_fixture WHERE scope_id={scope:String} AND publication_id={pub:String}',{'scope':scope,'pub':publication})
            if len(rows)!=1 or Decimal(rows[0]['amount'])!=Decimal('9.36'):
                raise ValueError('Typed bundle mismatch')
            return {'version':'typed-fixture-v1','rows':1,'checksum':digest(['9.36'])}
        publisher=Publications(self.client,self.directory.name,bundles={'typed':verify})
        workflow=uuid4().hex
        with self.assertRaises(RuntimeError):
            publisher.publish(self.scope,workflow,load,build=build)
        with self.assertRaises(ValueError):
            publisher.pin(self.scope)
        complete=publisher.publish(self.scope,workflow,load,build=build)
        self.assertEqual(complete['receipts']['typed']['rows'],1)
        self.assertEqual(publisher.pin(self.scope),complete)
        physical=int(self.client.query('SELECT count() AS n FROM typed_bundle_fixture WHERE scope_id={scope:String}',{'scope':self.scope})[0]['n'])
        self.assertEqual(physical,2)


def publish_worker(root,scope,workflow,load,queue):
    from greenhouse.clickhouse.client import Client, ClickHouseSettings
    try:
        result=Publications(Client(ClickHouseSettings.from_env()),root).publish(scope,workflow,load)
        queue.put(('ok',result['publication_id']))
    except Exception as error:
        queue.put(('error',str(error)))
