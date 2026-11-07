"""Prove native DB reachability, denied external TCP, and read-only raw mounts."""
import json
import os
import socket

from common import STATE, connect, write_json

client = connect()
try:
    assert int(client.query('SELECT 1 AS n')[0]['n']) == 1
finally:
    client.close()
denied = []
for host, port in (('1.1.1.1', 443), ('8.8.8.8', 53)):
    try:
        connection = socket.create_connection((host, port), timeout=2)
    except OSError as error:
        denied.append(dict(host=host, port=port, exception=type(error).__name__))
    else:
        connection.close()
        raise AssertionError('External egress was available')
mounts = {path: bool(os.statvfs(path).f_flag & os.ST_RDONLY) for path in ('/data/raw', '/data/import')}
assert all(mounts.values()), mounts
assert not any(os.environ.get(k) for k in ('PGHOST', 'PGPASSWORD', 'PGDATABASE', 'PGUSER'))
result = dict(status='passed', native_database_reachable=True, external_tcp_denied=denied,
    readonly_mounts=mounts, live_postgres_environment=False,
    scope='Prepared container runtime; image/dependency preparation precedes isolation')
write_json(STATE / 'offline-probe.json', result)
print(json.dumps(result), flush=True)
