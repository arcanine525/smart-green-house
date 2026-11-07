"""Explicit, audited value corrections inside one immutable raw snapshot."""
from uuid import uuid4

from psycopg.types.json import Jsonb

from greenhouse.db.migrate import sql
from greenhouse.db.pipeline import decimal_key, digest, lock_scope, require_idle


def correct(conn, scope, source_record_id, record_type, old_value, new_value, actor, evidence, *, correction_id=None):
    """Full measure maps, exact Decimal strings/null. No time/identity/basis edits.

    A caller-supplied correction_id is the idempotency key. The same request
    returns its existing revision. A stale old_value is rejected.
    """
    require_idle(conn)
    if not actor.strip() or not evidence.strip():
        raise ValueError('Correction requires actor and evidence')
    correction_id = correction_id or uuid4().hex
    with conn.transaction():
        lock_scope(conn, scope)
        fields = {row[0] for row in conn.execute('SELECT measure_code FROM config.measure_definition WHERE record_type=%s', (record_type,))}
        if not fields or set(old_value) != fields or set(new_value) != fields:
            raise ValueError('Correction must specify the full subtype measure map')
        old = {k: decimal_key(v) for k, v in old_value.items()}
        new = {k: decimal_key(v) for k, v in new_value.items()}
        if old == new:
            raise ValueError('Correction has no changed value')
        old_payload, new_payload = {'record_type': record_type, 'values': old}, {'record_type': record_type, 'values': new}
        existing = conn.execute('SELECT source_record_id,old_value,new_value,recorded_by,evidence_reference FROM meta.correction WHERE correction_id=%s', (correction_id,)).fetchone()
        if existing:
            if existing != (source_record_id, old_payload, new_payload, actor, evidence):
                raise ValueError('Correction id reused with different payload')
            row = conn.execute('SELECT revision_id FROM core.record_revision WHERE correction_id=%s AND data_scope_id=%s AND record_type=%s', (correction_id, scope, record_type)).fetchone()
            if not row:
                raise ValueError('Correction belongs to another scope')
            return {'correction_id': correction_id, 'revision_id': row[0], 'reused': True}
        revision = conn.execute('SELECT revision_id,revision_number,run_id FROM core.record_revision WHERE data_scope_id=%s AND source_record_id=%s AND record_type=%s ORDER BY revision_number DESC LIMIT 1', (scope, source_record_id, record_type)).fetchone()
        if not revision:
            raise ValueError('No source revision in this scope')
        current = {k: decimal_key(v) for k, v in conn.execute('SELECT measure_code,value FROM core.measure_value WHERE revision_id=%s', (revision[0],))}
        if current != old:
            raise ValueError('Correction old_value is stale')
        version = conn.execute('SELECT coalesce(max(version),0)+1 FROM meta.correction WHERE source_record_id=%s', (source_record_id,)).fetchone()[0]
        conn.execute('INSERT INTO meta.correction VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp())', (correction_id, source_record_id, version, Jsonb(old_payload), Jsonb(new_payload), evidence, actor))
        new_id = digest([scope, source_record_id, record_type, revision[1]+1, correction_id])
        conn.execute(sql('prepare_load'))
        conn.execute(sql('correction_input'), (Jsonb(new), Jsonb({k:'explicit_correction_null' for k,v in new.items() if v is None}), revision[0]))
        conn.execute('INSERT INTO core_batch_revision SELECT %s,data_scope_id,snapshot_id,cycle_id,run_id,source_record_id,record_type,logical_record_key,revision_number+1,%s,observed_at,is_close_boundary,%s FROM core.record_revision WHERE revision_id=%s', (new_id, digest(new_payload), correction_id, revision[0]))
        conn.execute('INSERT INTO core.record_revision SELECT * FROM core_batch_revision')
        conn.execute(sql('correction_derived'))
        conn.execute(sql('finish_load'))
        return {'correction_id': correction_id, 'revision_id': new_id, 'revision_number': revision[1]+1, 'reused': False}
