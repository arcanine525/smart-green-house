"""Atomically seal immutable replay membership and advance a replay pointer."""

from psycopg.types.json import Jsonb

from greenhouse.sensor.replay_state import load_replay_state
from greenhouse.sensor.schema import SCHEMA_VERSION
from greenhouse.sensor.event_time import normalize_cursor


def publish_replay(connection, *, replay_id, scope_id, cycle_id, compartment_id, quality_policy_version):
    """Publish only uncontested telemetry emitted by the confirmed replay cursor.

    Input writers and publishers serialize on the same per-replay advisory lock.
    Conflicted identities are absent from the sealed membership, so a late
    conflict produces a new publication instead of retaining a first arrival.
    """
    if not all((replay_id, scope_id, cycle_id, compartment_id, quality_policy_version)):
        raise ValueError('explicit replay, scope, cycle, compartment and quality policy required')
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('replay:' + replay_id,))
        policy = connection.execute('SELECT valid_from,valid_to_exclusive FROM config.quality_policy_version WHERE policy_version_id=%s', (quality_policy_version,)).fetchone()
        if not policy:
            raise ValueError('unknown quality policy')
        cycle = connection.execute('SELECT start_source_time,end_source_time_exclusive FROM core.growing_cycle WHERE cycle_id=%s AND compartment_id=%s', (cycle_id, compartment_id)).fetchone()
        if not cycle:
            raise ValueError('unknown replay cycle or compartment')
        state = load_replay_state(connection, replay_id)
        variants = connection.execute('''SELECT identity,payload_hash,envelope
            FROM sensor_stream.uncontested_event WHERE replay_id=%s
              AND envelope->>'schema_version'=%s ORDER BY identity''', (replay_id, SCHEMA_VERSION)).fetchall()
        from greenhouse.sensor.publication_plan import plan_publication
        publication_id,metadata,members=plan_publication(state,variants,cycle,policy,replay_id=replay_id,
            scope_id=scope_id,cycle_id=cycle_id,compartment_id=compartment_id,quality_policy_version=quality_policy_version)
        state_data=metadata['state'];checksum=metadata['membership_checksum']
        observed=normalize_cursor(state.observed_through) if state.observed_through else None
        inserted = connection.execute('''INSERT INTO sensor_stream.publication
          (publication_id,replay_id,data_scope_id,cycle_id,compartment_id,quality_policy_version,
           cursor_sequence,observed_through,received_through,state)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING publication_id''',
          (publication_id, replay_id, scope_id, cycle_id, compartment_id, quality_policy_version,
           state.cursor_sequence, observed, state.received_through, Jsonb(state_data))).fetchone()
        if inserted:
            for identity, payload_hash in members:
                connection.execute('INSERT INTO sensor_stream.publication_event VALUES (%s,%s,%s,%s)',
                                   (publication_id, replay_id, identity, payload_hash))
            connection.execute('INSERT INTO sensor_stream.publication_seal VALUES (%s,%s,%s,%s)',
                               (publication_id, replay_id, len(members), checksum))
        connection.execute('''INSERT INTO sensor_stream.current_publication VALUES (%s,%s)
            ON CONFLICT (replay_id) DO UPDATE SET publication_id=excluded.publication_id''',
            (replay_id, publication_id))
        return publication_id


def read_publication(connection, publication_id):
    """Read an explicit immutable publication and its pinned envelopes."""
    publication = connection.execute('''SELECT p.replay_id,p.data_scope_id,p.cycle_id,p.compartment_id,
        p.quality_policy_version,p.cursor_sequence,p.observed_through,p.received_through,p.state
        FROM sensor_stream.publication p JOIN sensor_stream.publication_seal s USING(publication_id,replay_id)
        WHERE p.publication_id=%s''', (publication_id,)).fetchone()
    if not publication:
        raise ValueError('unknown or unsealed replay publication')
    events = connection.execute('''SELECT v.envelope FROM sensor_stream.publication_event m
        JOIN sensor_stream.event_variant v USING(replay_id,identity,payload_hash)
        WHERE m.publication_id=%s ORDER BY v.envelope->>'source_observed_at',m.identity''',
        (publication_id,)).fetchall()
    return publication, tuple(row[0] for row in events)
