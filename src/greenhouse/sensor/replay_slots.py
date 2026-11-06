"""Map validated replay envelopes to existing BR-01/03 slot contracts.

The shared PostgreSQL quality function is also used by historical loading and
correction. The caller supplies an immutable snapshot of uncontested events.
"""

from psycopg.types.json import Jsonb


CODES = ('Tair', 'CO2air', 't_heat_sp', 't_vent_sp', 'co2_sp',
         't_heat_vip', 't_ventlee_vip', 'co2_vip', 'EC_drain_PC', 'pH_drain_PC')


def build_replay_rows(connection, events, *, replay_id, scope_id, cycle_id,
                      compartment_id, quality_policy_version, cycle_end=None):
    """Build typed rows with the same per-measure quality as batch.

    Reject duplicate slot timestamps and mixed scopes; neither can be safely
    interpreted as a correction without an explicit resolution decision.
    """
    if not all((replay_id, scope_id, cycle_id, compartment_id, quality_policy_version)):
        raise ValueError('replay, scope, cycle, compartment and quality version required')
    policy = connection.execute(
        'SELECT definition, valid_from, valid_to_exclusive FROM config.quality_policy_version WHERE policy_version_id=%s',
        (quality_policy_version,),
    ).fetchone()
    if not policy:
        raise ValueError('unknown quality policy')
    from greenhouse.sensor.slot_inputs import prepare_events,assemble_rows
    prepared=prepare_events(events,replay_id=replay_id,scope_id=scope_id,cycle_id=cycle_id,
        compartment_id=compartment_id,policy_start=policy[1],policy_end=policy[2],cycle_end=cycle_end)
    quality_maps=[]
    for offset in range(0, len(prepared), 250):
        chunk = prepared[offset:offset + 250]
        verdicts = connection.execute('''
          SELECT x.ordinal, jsonb_object_agg(c.code,
            sensor_stream.measure_verdict(%s::jsonb, x.item->'issues', x.item->'nulls', c.code,
                (x.item->'values'->>c.code)::numeric))
          FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY x(item, ordinal)
          CROSS JOIN unnest(%s::text[]) c(code)
          GROUP BY x.ordinal ORDER BY x.ordinal
        ''', (Jsonb(policy[0]), Jsonb(chunk), list(CODES))).fetchall()
        quality_maps.extend(quality for _,quality in verdicts)
    return assemble_rows(prepared,quality_maps,quality_policy_version)
