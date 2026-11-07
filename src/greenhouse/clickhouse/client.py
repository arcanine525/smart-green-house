"""Synchronous HTTP client using the pinned CPython standard library.

SQL values use ClickHouse server parameters, never string interpolation.
Errors deliberately omit response bodies/URLs/credentials from user logs.
"""
import base64
from decimal import Decimal
from dataclasses import dataclass, field
from http.client import HTTPException
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
        try:
            port=int(env.get('CH_PORT','8123')); timeout=int(env.get('CH_TIMEOUT','120'))
        except (TypeError,ValueError):
            raise ValueError('CH_PORT and CH_TIMEOUT must be integers') from None
        return cls(env.get('CH_HOST', 'clickhouse'), port,
                   env.get('CH_DATABASE', 'greenhouse'), env.get('CH_USER', 'greenhouse'),
                   env.get('CH_PASSWORD', ''), timeout)


class Client:
    def __init__(self, settings):
        self.settings = settings
        self._action_cache = None
        self._shared_proofs=False
        self._shared_proof_hits=0
        self._shared_proof_misses=0

    def begin_action(self,*,shared_proofs=False):
        """Opt-in immutable verification reuse until this caller closes its action."""
        self._action_cache={}
        self._shared_proofs=shared_proofs
        return self

    def ping(self):
        return self.query('SELECT 1 AS ready')

    def close(self):
        """HTTP responses close individually; discard action-owned verified inputs."""
        self._action_cache=None
        self._shared_proofs=False

    def _request(self,sql,parameters,query_id,*,streaming=False):
        settings = self.settings
        query = {'database': settings.database, 'query_id': query_id or uuid4().hex,
                 'async_insert': '0', 'wait_end_of_query': '1',
                 'output_format_json_quote_decimals': '1'}
        if streaming:
            # Large source payload columns otherwise multiply their default
            # ~65k-row blocks across in-order part readers on the 2 GiB server.
            query.update(wait_end_of_query='0',max_threads='1',max_block_size='1024',
                         max_streams_for_merge_tree_reading='1',
                         http_write_exception_in_output_format='0')
        for key, value in (parameters or {}).items():
            if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', key):
                raise ValueError('Invalid SQL parameter name')
            query['param_' + key] = str(value)
        credentials = base64.b64encode(f'{settings.user}:{settings.password}'.encode()).decode()
        return Request(f'http://{settings.host}:{settings.port}/?{urlencode(query)}',
                          data=sql.encode('utf-8'), method='POST',
                          headers={'Authorization': 'Basic ' + credentials,
                                   'Content-Type': 'text/plain; charset=utf-8'})
    def execute(self, sql, parameters=None, *, query_id=None):
        """Synchronous request; never auto-retry an ambiguous write."""
        try:
            request=self._request(sql,parameters,query_id)
            settings=self.settings
            with urlopen(request, timeout=settings.timeout) as response:
                return response.read().decode('utf-8')
        except HTTPError as error:
            status = error.code
            error.close()
            raise ClickHouseError(f'ClickHouse rejected query (HTTP {status}); inspect server query log by query ID') from None
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException):
            raise ClickHouseError('ClickHouse connection failed; write outcome may be unknown, reconcile query ID before retry') from None

    def query(self, sql, parameters=None):
        raw = self.execute(sql.rstrip().rstrip(';') + ' FORMAT JSON', parameters)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ClickHouseError('Invalid or interrupted JSON response; no verified result is available') from None
        if not isinstance(result,dict) or 'exception' in result or not isinstance(result.get('data'),list):
            raise ClickHouseError('ClickHouse returned an incomplete or failed query result')
        return result['data']

    def iter_query(self,sql,parameters=None):
        """Read bounded JSONEachRow frames, closing the response on exhaustion.

        A partial HTTP response never becomes a verified receipt: consumers
        must finish iteration before publishing their materialized proof.
        """
        request=self._request(sql.rstrip().rstrip(';')+' FORMAT JSONEachRow',parameters,None,streaming=True)
        try:
            with urlopen(request,timeout=self.settings.timeout) as response:
                for line in response:
                    if line.strip():
                        try:
                            row=json.loads(line)
                        except (ValueError,UnicodeError):
                            # With plaintext exception framing, a late server
                            # failure cannot masquerade as a data row named
                            # "exception". A prefix is never a complete proof.
                            raise ClickHouseError('ClickHouse stream failed or was malformed; no verified result is available') from None
                        if not isinstance(row,dict):
                            raise ClickHouseError('Invalid ClickHouse row frame; no verified result is available')
                        yield row
        except HTTPError as error:
            status=error.code;error.close()
            raise ClickHouseError(f'ClickHouse rejected query (HTTP {status}); inspect server query log by query ID') from None
        except (URLError,TimeoutError,socket.timeout,OSError,HTTPException):
            raise ClickHouseError('ClickHouse streaming read interrupted; no verified result is available') from None


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


def insert_rows(client, table, rows, *, query_id):
    """Explicit column names + JSONEachRow, exact decimal strings, no auto retry."""
    if not re.fullmatch(r'[a-z_][a-z0-9_]*', table):
        raise ValueError('Invalid table name')
    if not rows:
        return
    from greenhouse.clickhouse.encoding import canonical_json
    client.execute('INSERT INTO ' + table + ' FORMAT JSONEachRow\n' +
                   '\n'.join(canonical_json(row) for row in rows), query_id=query_id)
