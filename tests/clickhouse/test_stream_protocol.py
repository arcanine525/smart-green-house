"""Late HTTP errors are failures, even after an HTTP 200 and a valid prefix."""
from io import BytesIO
import os
import unittest
from unittest.mock import patch

from greenhouse.clickhouse.client import Client,ClickHouseSettings,ClickHouseError


class StreamProtocolTest(unittest.TestCase):
    @patch('greenhouse.clickhouse.client.urlopen')
    def test_plain_exception_trailer_is_typed_and_closes_prefix(self,send):
        response=BytesIO(b'{"n":1}\n\r\n__exception__\r\nopaque-error-tag\r\npassword=private-error\r\n')
        send.return_value=response
        rows=Client(ClickHouseSettings(password='demo')).iter_query('SELECT 1')
        self.assertEqual(next(rows),{'n':1})
        with self.assertRaises(ClickHouseError) as caught:list(rows)
        self.assertTrue(response.closed)
        self.assertNotIn('private-error',str(caught.exception))

    @patch('greenhouse.clickhouse.client.urlopen')
    def test_complete_json_with_exception_never_returns_partial_data(self,send):
        send.return_value.__enter__.return_value.read.return_value=b'{"data":[{"n":1}],"exception":"password=private-error"}'
        with self.assertRaises(ClickHouseError) as caught:
            Client(ClickHouseSettings(password='demo')).query('SELECT 1')
        self.assertNotIn('private-error',str(caught.exception))

    @patch('greenhouse.clickhouse.client.urlopen')
    def test_legitimate_exception_column_remains_data_with_plain_error_mode(self,send):
        response=BytesIO(b'{"exception":"Code: 395. DB::Exception: legitimate stored text"}\n')
        send.return_value=response
        rows=list(Client(ClickHouseSettings(password='demo')).iter_query('SELECT exception FROM t'))
        self.assertEqual(rows,[{'exception':'Code: 395. DB::Exception: legitimate stored text'}])
        self.assertIn('http_write_exception_in_output_format=0',send.call_args.args[0].full_url)


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated server required')
class RealStreamProtocolTest(unittest.TestCase):
    def test_real_http_200_late_throw_rejects_unverified_prefix(self):
        client=Client(ClickHouseSettings.from_env())
        query="SELECT number,repeat('x',65536) AS payload,throwIf(number=40,'late-stream-fixture') AS fault FROM numbers(80) SETTINGS max_block_size=1,max_threads=1"
        seen=[]
        with self.assertRaises(ClickHouseError):
            for row in client.iter_query(query):
                self.assertNotIn('exception',row)
                seen.append(row['number'])
        self.assertGreater(len(seen),0)
        self.assertLess(len(seen),80)
