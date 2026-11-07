"""Pure validated replay inputs and shared slot-row assembly for both engines."""
from decimal import Decimal
from greenhouse.queries.slot_rows import ClimateSlotRow,DrainSlotRow
from greenhouse.sensor.schema import SCHEMA_VERSION,validate_telemetry_envelope
from greenhouse.sensor.event_time import event_slot
CODES=('Tair','CO2air','t_heat_sp','t_vent_sp','co2_sp','t_heat_vip','t_ventlee_vip','co2_vip','EC_drain_PC','pH_drain_PC')


def prepare_events(events,*,replay_id,scope_id,cycle_id,compartment_id,policy_start,policy_end,cycle_end=None):
    prepared, timestamps = [], set()
    for envelope in events:
        validate_telemetry_envelope(envelope)
        if (envelope['schema_version'] != SCHEMA_VERSION or envelope['replay_id'] != replay_id
                or envelope['data_scope_id'] != scope_id or envelope['cycle_id'] != cycle_id
                or envelope['compartment_id'] != compartment_id):
            raise ValueError('mixed replay, scope, cycle or compartment')
        stamp = event_slot(envelope)
        if not (policy_start <= stamp < policy_end or (cycle_end == stamp == policy_end)):
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
    return prepared


def assemble_rows(prepared,verdicts,quality_policy_version):
    climates,drains=[],[]
    for item,quality in zip(prepared,verdicts,strict=True):
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
    return tuple(climates),tuple(drains)
