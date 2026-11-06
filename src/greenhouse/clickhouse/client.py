"""Synchronous HTTP client using the pinned CPython standard library.

SQL values use ClickHouse server parameters, never string interpolation.
Errors deliberately omit response bodies/URLs/credentials from user logs.
"""
import base64
from decimal import Decimal
from dataclasses import dataclass, field
import json
import os
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


class ClickHouseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClickHouseSettings:
    host: str = 'clickhouse'
    port: int = 8123
    database: str = 'greenhouse'
    user: str = 'greenhouse'
    password: str = field(default='', repr=False)
    timeout: int = 120

    def __post_init__(self):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', self.host):
            raise ValueError('Invalid ClickHouse host')
        if not 1 <= self.port <= 65535 or self.timeout <= 0:
            raise ValueError('Invalid ClickHouse port/timeout')
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', self.database):
            raise ValueError('Invalid ClickHouse database')
        if not self.user or not self.password:
            raise ValueError('ClickHouse user/password are required')

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        return cls(env.get('CH_HOST', 'clickhouse'), int(env.get('CH_PORT', '8123')),
                   env.get('CH_DATABASE', 'greenhouse'), env.get('CH_USER', 'greenhouse'),
                   env.get('CH_PASSWORD', ''), int(env.get('CH_TIMEOUT', '120')))


class Client:
    def __init__(self, settings):
        self.settings = settings

    def execute(self, sql, parameters=None, *, query_id=None):
        """Return raw response; never auto-retry an ambiguous write.

        A caller-supplied query ID is required by the publication coordinator for
        writes. The base client generates one for independent queries/smoke only.
        """
        settings = self.settings
        query = {'database': settings.database, 'query_id': query_id or uuid4().hex,
                 'async_insert': '0', 'wait_end_of_query': '1',
                 'output_format_json_quote_decimals': '1'}
        for key, value in (parameters or {}).items():
            if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', key):
                raise ValueError('Invalid SQL parameter name')
            query['param_' + key] = str(value)
        credentials = base64.b64encode(f'{settings.user}:{settings.password}'.encode()).decode()
        request = Request(f'http://{settings.host}:{settings.port}/?{urlencode(query)}',
                          data=sql.encode('utf-8'), method='POST',
                          headers={'Authorization': 'Basic ' + credentials,
                                   'Content-Type': 'text/plain; charset=utf-8'})
        try:
            with urlopen(request, timeout=settings.timeout) as response:
                return response.read().decode('utf-8')
        except HTTPError as error:
            status = error.code
            error.close()
            raise ClickHouseError(f'ClickHouse rejected query (HTTP {status}); inspect server query log by query ID') from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise ClickHouseError('ClickHouse connection failed; write outcome may be unknown, reconcile query ID before retry') from None

    def query(self, sql, parameters=None):
        result = json.loads(self.execute(sql.rstrip().rstrip(';') + ' FORMAT JSON', parameters))
        return result['data']


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Local ClickHouse connection check')
    parser.add_argument('command', choices=['ping', 'smoke'])
    args = parser.parse_args()
    client = Client(ClickHouseSettings.from_env())
    version = client.query('SELECT version() AS version')[0]['version']
    if args.command == 'smoke':
        # Dedicated small table in a unique smoke database/project only.
        client.execute('CREATE TABLE IF NOT EXISTS connection_smoke (id UInt8, value Decimal(18, 6)) ENGINE=MergeTree ORDER BY id')
        client.execute('INSERT INTO connection_smoke VALUES (1, 9.36)')
        rows = client.query('SELECT id, value FROM connection_smoke WHERE id={id:UInt8}', {'id': 1})
        assert rows and all(isinstance(row['value'], str) and Decimal(row['value']) == Decimal('9.36') for row in rows)
        print(json.dumps({'status': 'passed', 'version': version, 'rows': len(rows), 'decimal_exact': True}))
    else:
        print(json.dumps({'status': 'ok', 'version': version}))


if __name__ == '__main__':
    main()
