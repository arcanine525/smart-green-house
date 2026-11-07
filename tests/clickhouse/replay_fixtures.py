"""Small transport fixtures using the real envelope validators, no MQTT mock."""
import base64
from datetime import datetime,timedelta
import hashlib
import json
from greenhouse.clickhouse.quality import AGC_QUALITY_DEFINITION


def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def event(replay,scope,sequence=1,*,values=None):
    stamp=datetime(2019,12,16)+timedelta(minutes=(sequence-1)*5)
    serial=(stamp-datetime(1899,12,30)).total_seconds()/86400
    actual={'Tair':'30','CO2air':'600'};drain={'EC_drain_PC':'6','pH_drain_PC':'0'}
    controller={'t_heat_sp':'20','t_vent_sp':'25','co2_sp':'0','t_heat_vip':'20','t_ventlee_vip':'25','co2_vip':'500'}
    if values:
        for category in (actual,drain,controller):
            category.update({k:v for k,v in values.items() if k in category})
    all_values=actual|drain|controller
    return dict(schema_version='greenhouse-telemetry-envelope-v1',event_id=hashlib.sha256(f'{replay}:{sequence}'.encode()).hexdigest(),
        data_scope_id=scope,replay_id=replay,device_id='fixture-sensor',cycle_id='fixture-cycle',compartment_id='AICU',
        source_record_id=hashlib.sha256(f'source:{sequence}'.encode()).hexdigest(),sequence=sequence,source_observed_at=stamp.isoformat(),published_at='2026-11-07T00:00:00+00:00',
        source_provenance=dict(snapshot_id='fixture-snapshot',snapshot_sha='a'*64,source_file='AICU/GreenhouseClimate.csv',file_checksum='b'*64,
            line_number=sequence+1,line_end=sequence+1,raw_hash='c'*64,source_time_serial=f'{serial:.5f}',adapter_id='agc-standard',parser_version='agc-parser-v1'),
        payload=dict(actual_measurements=actual,drain_measurements=drain,controller_context=controller,actuators={},
            null_reasons={k:'empty' for k,v in all_values.items() if v is None},category_tags={}),
        units={k:'ppm' if k in ('CO2air','co2_sp','co2_vip') else 'dS/m' if k=='EC_drain_PC' else '1' if k=='pH_drain_PC' else '°C' for k in all_values},fault_tags=[])


def control(replay,sequence,kind,payload):
    return dict(schema_version='greenhouse-control-envelope-v1',control_message_id=hashlib.sha256(f'{replay}:ctrl:{sequence}'.encode()).hexdigest(),
        replay_id=replay,control_sequence=sequence,control_type=kind,published_at='2026-11-07T00:00:00+00:00',payload=payload)


def row(payload,table,row_id):
    raw=encode(payload);identity=payload.get('event_id') or payload['control_message_id']
    return dict(raw_payload_base64=base64.b64encode(raw).decode(),raw_bytes_len=len(raw),payload_hash=hashlib.sha256(raw).hexdigest(),
        is_valid=True,payload=payload,replay_id=payload['replay_id'],event_id=identity,received_at='2026-11-07T00:00:01+00:00',outbox_table=table,outbox_row_id=row_id)


def completed_rows(replay,scope):
    item=event(replay,scope);telemetry=row(item,'telemetry_messages',1)
    checksum=hashlib.sha256(('0'*64+':'+telemetry['payload_hash']).encode()).hexdigest()
    cursor=control(replay,1,'replay_cursor',dict(logical_cursor_line=2,logical_cursor_sequence=1,source_observed_at=item['source_observed_at'],status='completed'))
    manifest=control(replay,2,'completion_manifest',dict(expected_unique_events=1,emitted_unique_events=1,emitted_total_messages=1,payload_checksum=checksum,
        fault_summary=dict(duplicates=0,conflicts=0,delays=0,drops=0),reconciliation_requested=True))
    return [telemetry,row(cursor,'control_messages',1),row(manifest,'control_messages',2)]


def config(replay,scope):
    return dict(replay_id=replay,scope_id=scope,cycle_id='fixture-cycle',compartment_id='AICU',cycle_start='2019-12-16T00:00:00',cycle_end='2019-12-18T00:00:00',
        policy_start='2019-12-16T00:00:00',policy_end='2019-12-18T00:00:00',quality_policy_version='AGC_QUALITY_V1',quality_definition=AGC_QUALITY_DEFINITION,
        snapshot_id='fixture-snapshot',snapshot_sha='a'*64,source_file='AICU/GreenhouseClimate.csv',file_checksum='b'*64)
