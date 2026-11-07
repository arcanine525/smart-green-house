"""Retain the completed restored replay audit before disposing its test DB.

The original physical backup remains the restore cut point. This separate local
native archive preserves post-resume stream/seal/pointer rows and schema, never
claims to be that earlier physical snapshot. Keep its payload outside Git.
"""
from contextlib import closing
import json

from common import STATE, connect, write_json
from greenhouse.clickhouse.encoding import digest

with closing(connect()) as client:
    tables = {}
    for name in ('stream_delivery', 'stream_batch', 'replay_member', 'replay_seal', 'replay_pointer'):
        schema = client.query('SELECT create_table_query FROM system.tables WHERE database=currentDatabase() AND name={name:String}', {'name':name})
        assert len(schema) == 1
        rows = client.query('SELECT * FROM ' + name)
        rows.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(',', ':')))
        tables[name] = dict(schema=schema[0]['create_table_query'], rows=rows, checksum=digest(rows))
    archive = dict(format='greenhouse-native-replay-audit-v1', backend='clickhouse', tables=tables)
    write_json(STATE / 'completed-replay-audit.json', {'archive':archive, 'sha256':digest(archive)})
    print(json.dumps(dict(status='passed', backend='clickhouse', archive_sha256=digest(archive),
        table_rows={name:len(value['rows']) for name,value in tables.items()})),flush=True)
