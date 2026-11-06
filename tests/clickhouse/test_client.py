"""Client boundary tests, no warehouse/mock formulas."""
import unittest
from unittest.mock import patch
from urllib.error import URLError
from greenhouse.clickhouse.client import Client, ClickHouseSettings, ClickHouseError


class ClientTest(unittest.TestCase):
    def test_settings_need_no_postgres_credentials(self):
        settings = ClickHouseSettings.from_env({'CH_PASSWORD': 'private-example'})
        self.assertEqual(settings.host, 'clickhouse')
        self.assertNotIn('private-example', repr(settings))

    def test_rejects_url_injection_and_empty_credentials(self):
        for overrides in ({'host': 'x/?password=secret'}, {'database': 'x; DROP DATABASE system'}, {'password': ''}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                ClickHouseSettings(**({'password': 'demo'} | overrides))

    @patch('greenhouse.clickhouse.client.urlopen', side_effect=URLError('password=private-example'))
    def test_failed_write_has_no_retry_or_secret_in_error(self, send):
        with self.assertRaises(ClickHouseError) as caught:
            Client(ClickHouseSettings(password='private-example')).execute('INSERT INTO t VALUES (1)', query_id='attempt-1')
        self.assertEqual(send.call_count, 1)
        self.assertNotIn('private-example', str(caught.exception))
        self.assertIn('unknown', str(caught.exception))

    @patch('greenhouse.clickhouse.client.urlopen')
    def test_parameters_are_transmitted_separately_from_sql(self, send):
        send.return_value.__enter__.return_value.read.return_value = b'{"data":[]}'
        client = Client(ClickHouseSettings(password='demo'))
        client.query('SELECT {text:String} AS text', {'text': "x'; DROP TABLE t;--"})
        request = send.call_args.args[0]
        self.assertNotIn(b'DROP TABLE', request.data)
        self.assertIn('param_text=', request.full_url)
        self.assertNotIn('demo', request.full_url)
        self.assertIn('async_insert=0', request.full_url)


if __name__ == '__main__':
    unittest.main()
