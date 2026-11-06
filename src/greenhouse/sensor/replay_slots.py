"""Map validated replay envelopes to existing BR-01/03 slot contracts.

The shared PostgreSQL quality function is also used by historical loading and
correction. The caller supplies an immutable snapshot of uncontested events.
"""
from datetime import datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from greenhouse.db.climate_adapter import ClimateSlotRow
from greenhouse.db.drain_adapter import DrainSlotRow
from greenhouse.sensor.schema import SCHEMA_VERSION, validate_telemetry_envelope
from greenhouse.sensor.event_time import event_slot

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
    prepared, timestamps = [], set()
    for envelope in events:
        validate_telemetry_envelope(envelope)
        if (envelope['schema_version'] != SCHEMA_VERSION or envelope['replay_id'] != replay_id
                or envelope['data_scope_id'] != scope_id or envelope['cycle_id'] != cycle_id
                or envelope['compartment_id'] != compartment_id):
            raise ValueError('mixed replay, scope, cycle or compartment')
        stamp = event_slot(envelope)
        if not (policy[1] <= stamp < policy[2] or (cycle_end == stamp == policy[2])):
            raise ValueError('quality policy outside effective interval')
        if stamp in timestamps:
            raise ValueError('duplicate source slot requires explicit conflict resolution')
        timestamps.add(stamp)
        payload = envelope['payload']
        values = {}
        for category in ('actual_measurements', 'drain_measurements', 'controller_context'):
            values.update(payload[category])
        prepared.append({'event': envelope, 'values': values,
                         'issues': envelope['source_provenance'].get('source_issues', []),
                         'nulls': payload['null_reasons']})
    climates, drains = [], []
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
        for item, (_, quality) in zip(chunk, verdicts, strict=True):
            event, values = item['event'], item['values']
            value = lambda key: Decimal(values[key]) if values.get(key) is not None else None
            common = dict(slot_start=event_slot(event),
                          revision_id=event['event_id'], logical_key=event['source_record_id'],
                          quality_policy_version=quality_policy_version, correction_id=None,
                          correction_version=None, measure_quality=quality)
            climates.append(ClimateSlotRow(**common, tair=value('Tair'), co2air=value('CO2air'),
                t_heat_sp=value('t_heat_sp'), t_vent_sp=value('t_vent_sp'), co2_sp=value('co2_sp'),
                t_heat_vip=value('t_heat_vip'), t_ventlee_vip=value('t_ventlee_vip'), co2_vip=value('co2_vip')))
            drains.append(DrainSlotRow(**common, ec_drain=value('EC_drain_PC'), ph_drain=value('pH_drain_PC')))
    return tuple(climates), tuple(drains)
