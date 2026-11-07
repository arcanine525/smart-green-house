"""Disk-backed repeated reads and streamed receipt compatibility, no PG writes."""
import copy
from datetime import date,timedelta
from decimal import Decimal
from io import BytesIO
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from greenhouse.clickhouse.rows import VerifiedRows
from greenhouse.clickhouse.client import Client,ClickHouseSettings
import test_repository as repository_fixture
import test_energy as energy_fixture


class RowsTest(unittest.TestCase):
    def test_independent_iterators_exact_values_slices_and_sealing(self):
        rows=VerifiedRows();self.addCleanup(rows.close)
        for i in range(10):rows.append({'id':i,'exact':str(Decimal(i)/Decimal(10)),'missing':None})
        with self.assertRaises(ValueError):rows[0]
        rows.seal();a,b=iter(rows),iter(rows)
        self.assertEqual(next(a),next(b));self.assertEqual(next(a)['id'],1)
        self.assertEqual(next(b)['id'],1);self.assertEqual(rows[-1]['id'],9)
        self.assertEqual([r['id'] for r in rows[3:5]],[3,4])
        with self.assertRaises(ValueError):rows.append({})
        self.assertEqual(len(rows),10)

    @patch('greenhouse.clickhouse.client.urlopen')
    def test_http_stream_closes_and_keeps_decimal_strings(self,send):
        response=BytesIO(b'{"n":"1.234567890123456789","null":null}\n{"n":"0","null":null}\n')
        send.return_value=response
        result=list(Client(ClickHouseSettings(password='demo')).iter_query('SELECT 1'))
        url=send.call_args.args[0].full_url
        self.assertIn('wait_end_of_query=0',url);self.assertIn('max_block_size=1024',url)
        self.assertIn('max_streams_for_merge_tree_reading=1',url)
        self.assertTrue(response.closed);self.assertEqual(result[0]['n'],'1.234567890123456789')
        self.assertIsNone(result[1]['null'])


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class BoundedCoreTest(unittest.TestCase):
    setUp=repository_fixture.RepositoryTest.setUp
    record=repository_fixture.RepositoryTest.record

    def test_interrupted_read_never_returns_a_partially_verified_membership(self):
        from greenhouse.clickhouse.client import ClickHouseError
        revision,_=self.repo.append_record(self.scope,self.record())
        stored=self.client.query('SELECT * FROM source_revision WHERE scope_id={scope:String}',{'scope':self.scope})[0]
        def interrupted(*args,**kwargs):
            yield stored
            raise ClickHouseError('Controlled partial stream failure')
        with patch.object(self.client,'iter_query',side_effect=interrupted):
            with self.assertRaisesRegex(ClickHouseError,'partial stream failure'):
                self.repo.canonical_records(self.scope)
        self.assertEqual([r['revision_id'] for r in self.repo.canonical_records(self.scope)],[revision])

    def test_large_canonical_membership_is_repeatable_and_keeps_physical_retry_semantics(self):
        records=[]
        for i in range(4200):
            r=copy.deepcopy(self.record());day=date(2020,3,2)+timedelta(days=i)
            r.update(logical_key=day.isoformat(),source_timestamp=day.isoformat()+'T00:00:00',source_record_id='large:'+str(i))
            r['values']['time']=str(43892+i);records.append(r)
        records[-1]['values']['Irr']='0.'+'1234567890'*5
        for offset in range(0,len(records),512):
            self.repo.append_initial_records(self.scope,(record for record in records[offset:offset+512]))
        rows=self.repo.canonical_records(self.scope);self.addCleanup(rows.close)
        self.assertIsInstance(rows,VerifiedRows);self.assertEqual(len(rows),4200)
        first=rows[0];last=rows[-1]
        self.assertEqual(rows.revision(last['revision_id']),last)
        with self.assertRaises(ValueError):rows.revision('absent')
        self.assertEqual(first['source_timestamp'],'2020-03-02T00:00:00')
        self.assertEqual(self.repo.append_initial_records(self.scope,records[:1]),[(first['revision_id'],False)])
        # Exact physical retries collapse before ordinal checks, independent of merges.
        self.repo._insert('source_revision',[first])
        newer=self.repo.canonical_records(self.scope);self.addCleanup(newer.close)
        self.assertEqual(len(newer),4200);self.assertEqual(newer[-1],last)
        picked=self.repo.canonical_records(self.scope,revision_ids=[last['revision_id'],first['revision_id']])
        self.assertEqual({r['revision_id'] for r in picked},{first['revision_id'],last['revision_id']})
        measures=self.client.query('SELECT * FROM source_measure WHERE scope_id={scope:String} AND revision_id={id:String}',
            {'scope':self.scope,'id':last['revision_id']})
        self.assertTrue(any(m['projection_status']=='outside_decimal256_projection' and m['value_decimal'] is None for m in measures))
        self.repo._insert('source_measure',[measures[0]])
        retry=self.repo.canonical_records(self.scope);self.addCleanup(retry.close)
        self.assertEqual(retry[-1],last)
        self.repo._insert('source_measure',[dict(measures[0],value_exact='999',value_decimal='999',projection_status='exact')])
        with self.assertRaisesRegex(ValueError,'measures incomplete or conflicting'):
            self.repo.canonical_records(self.scope)


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class StreamedMartTest(unittest.TestCase):
    setUp=energy_fixture.EnergyTest.setUp
    prepare=energy_fixture.EnergyTest.prepare

    def test_new_streamed_receipts_keep_old_pin_and_reject_new_mart_conflict(self):
        result,*_=self.prepare(include_climate=True)
        from greenhouse.clickhouse.energy_run import run
        from greenhouse.clickhouse.registry import publication_envelope
        from greenhouse.clickhouse.threshold import ClimateAdapter
        config=copy.deepcopy(self.threshold);config['storage_version']='threshold-stream-v2'
        newer=run(self.client,self.root,self.raw,self.root/'output',self.scope,uuid4().hex,self.config,self.harvest,config,self.energy_config,
            threshold_version='threshold-stream-v2',base_load=result['load_id'])
        pin=publication_envelope(self.client,newer['publication_id'])
        for kind in ('climate','drain'):
            self.assertEqual(pin['receipts'][kind]['rows'],30)
            self.assertEqual(pin['receipts'][kind]['checksum_algorithm'],'ordered-jsonl-sha256-v2')
        old=publication_envelope(self.client,result['publication_id'])
        self.assertNotIn('checksum_algorithm',old['receipts']['climate'])
        mart=ClimateAdapter(self.client).mart
        row=mart.rows('threshold_measure',self.scope,newer['publication_id'])[0]
        duplicate=dict(row);self.repo=mart.repo;self.repo.coordinator=self.root
        self.repo._insert('threshold_measure',[duplicate])
        self.assertEqual(publication_envelope(self.client,newer['publication_id']),pin)
        row['value_exact']='999';row['value_decimal']='999'
        self.repo._insert('threshold_measure',[row])
        with self.assertRaises(ValueError):publication_envelope(self.client,newer['publication_id'])
        self.assertEqual(publication_envelope(self.client,result['publication_id']),old)
