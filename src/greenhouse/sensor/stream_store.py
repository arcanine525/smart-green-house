"""Transactional landing of replay deliveries; no live publication yet."""
import hashlib
import json

from psycopg.types.json import Jsonb
from greenhouse.sensor.stream_records import decode_ready_record


def commit_batch(connection, stream_id, generation, batch_id, rows):
    """Atomically land a micro-batch; replay with identical content is a no-op.

    Event variants are retained under replay and identity. Callers must exclude
    identities with multiple hashes until they are resolved; first arrival does
    not win. This landing function does not claim to publish derived KPIs.
    """
    if not stream_id or not generation or type(batch_id) is not int or batch_id < 0:
        raise ValueError('stream identity, checkpoint generation and non-negative batch ID required')
    rows = tuple(rows)  # retain replay identities even if a caller passes an iterator
    # Sort canonical lines so Spark partition order cannot change retry identity.
    encoded = sorted(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False) for row in rows)
    digest = hashlib.sha256(('\n'.join(encoded) + '\n').encode('utf-8')).hexdigest()
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('stream:' + stream_id,))
        replay_ids = sorted({row['replay_id'] for row in rows if isinstance(row, dict)
                             and isinstance(row.get('replay_id'), str) and row['replay_id']})
        for replay_id in replay_ids:
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('replay:' + replay_id,))
        previous = connection.execute('SELECT content_sha256 FROM sensor_stream.processed_batch WHERE stream_id=%s AND checkpoint_generation=%s AND batch_id=%s', (stream_id, generation, batch_id)).fetchone()
        if previous:
            if previous[0] != digest:
                raise ValueError('batch ID reused with different content')
            return False
        connection.execute('INSERT INTO sensor_stream.processed_batch(stream_id, checkpoint_generation, batch_id, content_sha256) VALUES (%s,%s,%s,%s)', (stream_id, generation, batch_id, digest))
        for ordinal, line in enumerate(encoded):
            row = json.loads(line)
            result = decode_ready_record(row)
            connection.execute('INSERT INTO sensor_stream.delivery VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (stream_id, generation, batch_id, ordinal, Jsonb(row), result.disposition, result.reason, result.replay_id, result.identity, result.payload_hash))
            if result.disposition == 'accepted':
                connection.execute('INSERT INTO sensor_stream.event_variant(replay_id, identity, payload_hash, envelope) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', (result.replay_id, result.identity, result.payload_hash, Jsonb(result.payload)))
    return True
