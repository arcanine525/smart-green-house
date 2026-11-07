"""The real streamed verifier must fit 64 MiB without globally sorting all cycles."""
from datetime import datetime,timedelta
import os
import unittest

from greenhouse.clickhouse.client import Client
from greenhouse.clickhouse.threshold import SlotMart,CLIMATE
import test_repository as fixtures


class ReadBudgetClient(Client):
    def _request(self,*args,**kwargs):
        request=super()._request(*args,**kwargs)
        request.full_url+='&max_memory_usage=67108864'
        return request


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class ThresholdMemoryTest(unittest.TestCase):
    setUp=fixtures.RepositoryTest.setUp

    def test_all_cycles_verified_with_bounded_read_and_identical_receipt(self):
        scope=self.scope;pub='bounded-stream-fixture';bundle='climate';count=8192
        cycles=[dict(scope_id=scope,publication_id=pub,bundle=bundle,
            cycle_id='cycle-'+str(5-i),compartment_id='Comp'+str(i),
            compartment_name='Comp'+str(i),start='2020-01-01',end='2020-03-01') for i in range(6)]
        header=dict(scope_id=scope,publication_id=pub,bundle=bundle,config_version='fixture-stream')
        from greenhouse.clickhouse.threshold import migrate
        migrate(self.client,self.directory.name)
        mart=SlotMart(self.client,self.directory.name,bundle)
        mart.repo._insert('threshold_header',[header]);mart.repo._insert('threshold_cycle',cycles)
        codes=sorted(CLIMATE)
        sql="""INSERT INTO threshold_measure SELECT
            {scope:String},{pub:String},'climate',concat('cycle-',toString(5-comp)),
            concat('Comp',toString(comp)),toInt64(1577836800000000)+toInt64(slot)*300000000,
            replaceOne(toString(toDateTime(1577836800+slot*300,'UTC')),' ','T'),
            concat('revision-',toString(comp),'-',toString(slot)),
            concat('logical-',toString(comp),'-',toString(slot)),
            'AGC_QUALITY_V1','',toUInt32(0),code,'1.23456789',
            CAST('1.23456789','Nullable(Decimal(76,38))'),'exact','accepted','applicable',NULL
            FROM (SELECT number AS slot FROM numbers(8192)) AS slots
            CROSS JOIN (SELECT number AS comp FROM numbers(6)) AS compartments
            ARRAY JOIN {codes:Array(String)} AS code"""
        self.client.execute(sql,{'scope':scope,'pub':pub,'codes':str(codes)})
        def expected():
            for cycle in cycles:
                comp=int(cycle['compartment_id'][-1])
                for slot in range(count):
                    for code in codes:
                        yield dict(scope_id=scope,publication_id=pub,bundle=bundle,
                            cycle_id=cycle['cycle_id'],compartment_id=cycle['compartment_id'],
                            slot_clock_us=1577836800000000+slot*300000000,
                            slot_start=(datetime(2020,1,1)+timedelta(minutes=slot*5)).isoformat(),
                            revision_id=f'revision-{comp}-{slot}',logical_record_key=f'logical-{comp}-{slot}',
                            quality_policy_version='AGC_QUALITY_V1',correction_id='',correction_version=0,
                            measure_code=code,value_exact='1.23456789',value_decimal='1.23456789',
                            projection_status='exact',quality='accepted',applicability='applicable',reason=None)
        pin={'version_id':'fixture-stream','content_hash':'a'*64}
        baseline=mart._verify_stream(scope,pub,header,cycles,expected(),pin,[header])
        bounded=SlotMart(ReadBudgetClient(self.client.settings),self.directory.name,bundle)
        result=bounded._verify_stream(scope,pub,header,cycles,expected(),pin,[header])
        self.assertEqual(result,baseline)
        self.assertEqual((result['rows'],result['cycles']),(count*6,6))

    def test_unconfigured_cycle_or_compartment_cannot_hide_from_verifier(self):
        from greenhouse.clickhouse.threshold import migrate
        migrate(self.client,self.directory.name)
        for field,value in (('cycle_id','unconfigured-cycle'),('compartment_id','unconfigured-compartment')):
            with self.subTest(field=field):
                scope=self.scope;pub='guard-'+field;bundle='climate'
                cycle=dict(scope_id=scope,publication_id=pub,bundle=bundle,cycle_id='cycle-0',
                    compartment_id='Comp0',compartment_name='Comp0',start='2020-01-01',end='2020-03-01')
                header=dict(scope_id=scope,publication_id=pub,bundle=bundle,config_version='fixture-stream')
                rows=[dict(scope_id=scope,publication_id=pub,bundle=bundle,cycle_id=cycle['cycle_id'],
                    compartment_id=cycle['compartment_id'],slot_clock_us=1577836800000000,
                    slot_start='2020-01-01T00:00:00',revision_id='fixture-revision',logical_record_key='fixture-logical',
                    quality_policy_version='AGC_QUALITY_V1',correction_id='',correction_version=0,
                    measure_code=code,value_exact='1',value_decimal='1',projection_status='exact',
                    quality='accepted',applicability='applicable',reason=None) for code in sorted(CLIMATE)]
                mart=SlotMart(self.client,self.directory.name,bundle)
                mart.repo._insert('threshold_header',[header]);mart.repo._insert('threshold_cycle',[cycle])
                mart.repo._insert('threshold_measure',rows)
                pin={'version_id':'fixture-stream','content_hash':'a'*64}
                def verify():return mart._verify_stream(scope,pub,header,[cycle],iter(rows),pin,[header])
                original=verify()
                mart.repo._insert('threshold_measure',[rows[0]])
                self.assertEqual(verify(),original)
                mart.repo._insert('threshold_measure',[rows[0]|{field:value}])
                with self.assertRaises(ValueError):verify()
