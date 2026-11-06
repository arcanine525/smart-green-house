"""Real ClickHouse tests enabled explicitly in the isolated stack harness."""
import copy
import hashlib
import json
from pathlib import Path
import os
import tempfile
import unittest
from uuid import uuid4

from greenhouse.clickhouse.client import Client, ClickHouseSettings
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schema import migrate


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','explicit isolated integration stack required')
class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.client=Client(ClickHouseSettings.from_env())
        migrate(self.client)
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo=Repository(self.client,self.directory.name)
        self.scope=uuid4().hex

    def record(self):
        return dict(disposition='accepted',issues=[],source_file='Automatoes/Resources.csv',
            logical_key='2020-03-02',run_id='fixture-run',values={'time':'43892','Irr':'9.36','Drain':None},
            null_reasons={'Drain':'empty'},snapshot_id='fixture-snapshot',source_record_id='fixture-row',
            file_checksum='a'*64,line_number=2,line_end=2,raw_hash='b'*64,
            source_timestamp='2020-03-02T00:00:00',parser_version='agc-parser-v1',is_close_boundary=False)

    def test_retry_conflict_and_correction_preserve_previous_revision(self):
        record=self.record()
        initial,created=self.repo.append_record(self.scope,record)
        self.assertTrue(created)
        retried=copy.deepcopy(record); retried['run_id']='retry-run'
        self.assertEqual(self.repo.append_record(self.scope,retried),(initial,False))
        changed=copy.deepcopy(record); changed['values']['Irr']='10.00'
        with self.assertRaises(ValueError):
            self.repo.append_record(self.scope,changed)
        second,_=self.repo.append_record(self.scope,changed,revision=2,correction_id='audit-fixture',correction={'actor':'fixture','evidence':'source review','previous_revision':initial})
        self.assertNotEqual(initial,second)
        self.assertEqual([r['revision_id'] for r in self.repo.canonical_records(self.scope)],[second])
        self.assertEqual([r['revision_id'] for r in self.repo.canonical_records(self.scope,revision_ids=[initial])],[initial])
        with self.assertRaises(ValueError):
            self.repo.append_record(self.scope,changed,revision=3,correction_id='missing-audit')
        rows=self.client.query('SELECT DISTINCT revision,revision_id FROM source_revision WHERE scope_id={scope:String} ORDER BY revision',{'scope':self.scope})
        self.assertEqual([r['revision'] for r in rows],[1,2])
        measures=self.client.query('SELECT value_exact,projection_status FROM source_measure WHERE revision_id={id:String} AND measure_code=\'Irr\'',{'id':initial})
        self.assertEqual(measures,[{'value_exact':'9.36','projection_status':'exact'}])

    def test_configuration_roundtrip_and_idempotency_conflict(self):
        request='request-'+self.scope
        payload={'rates':[{'rate':'0.1234567890123456789012345678901234567890123456789'}], 'nullable':None}
        self.assertTrue(self.repo.register_configuration('tariff',self.scope,'v1',request,payload))
        self.assertFalse(self.repo.register_configuration('tariff',self.scope,'v1',request,payload))
        with self.assertRaises(ValueError):
            self.repo.register_configuration('tariff',self.scope,'v1',request,{'rates':[]})
        exported=self.repo.configurations()
        self.repo.restore_configurations(exported)
        self.assertEqual(self.repo.configurations(),exported)
        corrupted=copy.deepcopy(exported); corrupted['rows'][0]['payload']='{}'
        with self.assertRaises(ValueError):
            self.repo.restore_configurations(corrupted)

    def test_verified_raw_ingestion_keeps_duplicates_conflicts_and_quarantine(self):
        from greenhouse.clickhouse.load import load_snapshot, verified_snapshot
        root=Path(self.directory.name)
        raw=root/'raw'; (raw/'Automatoes').mkdir(parents=True)
        source=raw/'Automatoes/Resources.csv'
        source.write_text('%Time ,Heat_cons,ElecHigh,ElecLow,CO2_cons,Irr,Drain\n'
            '43892,1,2,3,4,9.36,3.89\n43892,1,2,3,4,9.36,3.89\n'
            '43893,1,2,3,4,2,1\n43893,1,2,3,4,3,1\n43894,1,2,3,4,invalid,1\n')
        manifest={'snapshot_id':'fixture-'+self.scope,'archive':{'sha256':'c'*64},
            'files':[{'path':'Automatoes/Resources.csv','sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                      'csv':{'data_rows':5}}]}
        (raw/'manifest.json').write_text(json.dumps(manifest))
        first=load_snapshot(self.client,self.directory.name,raw,root/'output',self.scope)
        second=load_snapshot(self.client,self.directory.name,raw,root/'output',self.scope)
        self.assertEqual(first['counts'],{'accepted':1,'inserted':1,'quarantine':1,'conflict':2,'duplicate':1})
        self.assertEqual(second['counts']['reused'],1)
        self.assertFalse(second['published'])
        self.assertEqual(len(self.repo.load_membership(self.scope,first['load_id'])),1)
        source.write_text(source.read_text()+'corrupted')
        with self.assertRaises(ValueError):
            verified_snapshot(raw)

    def test_canonical_reader_rejects_conflicting_measure_and_revision(self):
        from greenhouse.clickhouse.client import insert_rows
        revision,_=self.repo.append_record(self.scope,self.record())
        insert_rows(self.client,'source_measure',[{'scope_id':self.scope,'revision_id':revision,
            'measure_code':'Irr','value_exact':'999','value_decimal':'999','projection_status':'exact','null_reason':''}],query_id=uuid4().hex)
        with self.assertRaisesRegex(ValueError,'measures incomplete or conflicting'):
            self.repo.canonical_records(self.scope)

    def test_stale_correction_and_source_identity_edit_are_rejected(self):
        original=self.record()
        revision,_=self.repo.append_record(self.scope,original)
        changed=copy.deepcopy(original); changed['values']['Irr']='10'
        with self.assertRaisesRegex(ValueError,'Stale'):
            self.repo.append_record(self.scope,changed,revision=2,correction_id='stale',
                correction={'actor':'reviewer','evidence':'fixture','previous_revision':'wrong'})
        serial_edit=copy.deepcopy(changed); serial_edit['values']['time']='43893'
        with self.assertRaisesRegex(ValueError,'identity'):
            self.repo.append_record(self.scope,serial_edit,revision=2,correction_id='bad-serial',
                correction={'actor':'reviewer','evidence':'fixture','previous_revision':revision})
        changed['source_timestamp']='2020-03-03T00:00:00'
        with self.assertRaisesRegex(ValueError,'identity'):
            self.repo.append_record(self.scope,changed,revision=2,correction_id='bad-time',
                correction={'actor':'reviewer','evidence':'fixture','previous_revision':revision})

    def test_lossless_measure_null_and_close_boundary_roundtrip(self):
        record=self.record()
        record['values']['Irr']='0.'+'1234567890'*5
        record['is_close_boundary']=True
        revision,_=self.repo.append_record(self.scope,record)
        read=self.repo.canonical_records(self.scope)[0]
        self.assertTrue(read['is_close_boundary'])
        rows=self.client.query('SELECT measure_code,value_exact,value_decimal,projection_status FROM source_measure WHERE revision_id={id:String} ORDER BY measure_code',{'id':revision})
        self.assertEqual(rows[0],{'measure_code':'Drain','value_exact':None,'value_decimal':None,'projection_status':'null'})
        self.assertEqual(rows[1]['value_exact'].rstrip('0'),record['values']['Irr'].rstrip('0'))
        self.assertIsNone(rows[1]['value_decimal'])
        self.assertEqual(rows[1]['projection_status'],'outside_decimal256_projection')
