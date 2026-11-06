"""Real Streamlit replay view against an isolated migrated PostgreSQL database."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

import psycopg
from streamlit.testing.v1 import AppTest

from greenhouse.sensor.stream_store import commit_batch
from greenhouse.sensor.replay_publication import publish_replay

SCREEN = '📡 Sensor replay (BR-01/03)'


def delivery(payload, table, row_id):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return {'delivery_id': row_id, 'topic': 'v1/example', 'received_at': '2020-01-01T00:00:00+00:00',
        'payload_hash': hashlib.sha256(raw).hexdigest(), 'message_type': 'control' if table == 'control_messages' else 'telemetry',
        'is_valid': True, 'quarantine_reason': None, 'outbox_table': table, 'outbox_row_id': row_id,
        'replay_id': payload['replay_id'], 'compartment_id': 'AICU',
        'event_id': payload.get('event_id', payload.get('control_message_id')),
        'raw_bytes_len': len(raw), 'raw_payload_base64': base64.b64encode(raw).decode(), 'payload': payload}


def make_replay(connection, *, paused=False):
    replay = 'ui-' + uuid4().hex[:12]
    snapshot = 'agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68'
    scope = snapshot + ':sim-' + replay
    event = {'schema_version': 'greenhouse-telemetry-envelope-v1', 'event_id': 'a' * 64,
        'data_scope_id': scope, 'replay_id': replay, 'device_id': 'sim-AICU', 'compartment_id': 'AICU',
        'cycle_id': 'agc-cycle-AICU', 'source_record_id': 'b' * 64,
        'source_provenance': {'snapshot_id': snapshot, 'snapshot_sha': 'c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68',
            'source_file': 'AICU/GreenhouseClimate.csv', 'file_checksum': 'c' * 64, 'line_number': 2,
            'line_end': 2, 'raw_hash': 'd' * 64, 'source_time_serial': '43815',
            'adapter_id': 'agc-standard', 'parser_version': 'agc-parser-v1'},
        'sequence': 1, 'source_observed_at': '2019-12-16T00:00:00',
        'published_at': datetime.now(timezone.utc).isoformat(),
        'payload': {'actual_measurements': {'Tair': '21.5'}, 'drain_measurements': {}, 'actuators': {},
                    'controller_context': {}, 'null_reasons': {}, 'category_tags': {'Tair': 'actual_climate'}},
        'units': {'Tair': '°C'}, 'fault_tags': []}
    sample = delivery(event, 'telemetry_messages', 1)
    checksum = hashlib.sha256(('0' * 64 + ':' + sample['payload_hash']).encode()).hexdigest()
    def control(sequence, kind, payload):
        return {'schema_version': 'greenhouse-control-envelope-v1',
            'control_message_id': hashlib.sha256(f'{replay}:ctrl:{sequence}'.encode()).hexdigest(),
            'replay_id': replay, 'control_sequence': sequence, 'control_type': kind,
            'published_at': datetime.now(timezone.utc).isoformat(), 'payload': payload}
    cursor = control(1, 'replay_cursor', {'logical_cursor_line': 2, 'logical_cursor_sequence': 1,
                     'source_observed_at': '2019-12-16T00:00:00', 'status': 'running' if paused else 'completed'})
    manifest = control(2, 'completion_manifest', {'expected_unique_events': 1, 'emitted_unique_events': 1,
        'emitted_total_messages': 1, 'payload_checksum': checksum,
        'fault_summary': {'duplicates': 0, 'conflicts': 0, 'delays': 0, 'drops': 0}, 'reconciliation_requested': True})
    if paused:
        manifest = control(2, 'pause_state', {'paused_at_line': 2, 'paused_at_sequence': 1, 'reason': 'test pause'})
    commit_batch(connection, 'ui-' + replay, 'v1', 0, [sample,
        delivery(cursor, 'control_messages', 1), delivery(manifest, 'control_messages', 2)])
    publication = publish_replay(connection, replay_id=replay, scope_id=scope, cycle_id='agc-cycle-AICU',
        compartment_id='AICU', quality_policy_version='AGC_QUALITY_V1')
    return publication


def test_replay_screen_with_no_historical_publication(empty_database, app_path, monkeypatch):
    with psycopg.connect(**empty_database, autocommit=True) as connection:
        from datetime import datetime
        from decimal import Decimal
        from greenhouse.contracts import RuleBasis, RuleScheduleRevision, RuleScheduleSegment
        from greenhouse.db.rule_schedule import register_rule_schedule
        snapshot = 'agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68'
        rule_id = 'ui-replay-rule-' + uuid4().hex[:12]
        rule = RuleScheduleRevision(rule_id, rule_id, 1, snapshot + ':historical', 'AICU',
            'UI test', True, 'demo_rule', (RuleScheduleSegment('temperature', 'climate', RuleBasis.REQUESTED,
              datetime(2019,12,16), datetime(2020,5,30), delta=Decimal('1')),))
        register_rule_schedule(connection, rule)
        publication = make_replay(connection)
    for name, value in empty_database.items():
        variable = {'host': 'PGHOST', 'port': 'PGPORT', 'dbname': 'PGDATABASE',
                    'user': 'PGUSER', 'password': 'PGPASSWORD'}.get(name)
        if variable:
            monkeypatch.setenv(variable, str(value))
    app = AppTest.from_file(app_path, default_timeout=120).run()
    app.radio(key='screen_choice').set_value(SCREEN).run()
    assert not app.exception
    assert any(item.label == 'Trạng thái replay' and item.value == 'completed' for item in app.metric)
    assert any(item.label == 'Tín hiệu collector' and item.value == 'Stale' for item in app.metric)
    assert any(publication in item.value for item in app.caption)
    assert any('đối soát' in item.value for item in app.success)
    assert len(app.dataframe) >= 1
    app.selectbox(key='replay_climate_rule').set_value(rule_id).run()
    assert not app.exception and app.selectbox(key='replay_climate_rule').value == rule_id
    assert any(button.label == 'Tải KPI và episode CSV' for button in app.download_button)
    app.checkbox(key='replay_auto_refresh').check().run()
    assert not app.exception
    app.checkbox(key='replay_auto_refresh').uncheck().run()
    assert not app.exception

    # A late conflicting variant replaces current membership without mutating
    # the previously pinned publication. Refresh should show provisional state.
    from greenhouse.sensor.replay_publication import read_publication
    with psycopg.connect(**empty_database, autocommit=True) as connection:
        metadata, events = read_publication(connection, publication)
        event = events[0]
        event['payload']['actual_measurements']['Tair'] = '99'
        commit_batch(connection, 'ui-' + event['replay_id'], 'v1', 1,
                     [delivery(event, 'telemetry_messages', 2)])
        changed = publish_replay(connection, replay_id=event['replay_id'], scope_id=event['data_scope_id'],
            cycle_id=event['cycle_id'], compartment_id=event['compartment_id'], quality_policy_version='AGC_QUALITY_V1')
        assert changed != publication
    app.button(key='replay_refresh').click().run()
    assert not app.exception
    assert any('Provisional' in item.value for item in app.warning)
    assert any('Chưa có measurement' in item.value for item in app.info)


def test_paused_replay_is_provisional(empty_database, app_path, monkeypatch):
    with psycopg.connect(**empty_database, autocommit=True) as connection:
        publication = make_replay(connection, paused=True)
    for name, value in empty_database.items():
        variable = {'host': 'PGHOST', 'port': 'PGPORT', 'dbname': 'PGDATABASE',
                    'user': 'PGUSER', 'password': 'PGPASSWORD'}.get(name)
        if variable:
            monkeypatch.setenv(variable, str(value))
    app = AppTest.from_file(app_path, default_timeout=120).run()
    app.radio(key='screen_choice').set_value(SCREEN).run()
    assert not app.exception
    assert any(publication in item.value for item in app.caption)
    assert any(item.label == 'Trạng thái replay' and item.value == 'paused' for item in app.metric)
    assert any('Provisional' in item.value for item in app.warning)

    # Simulate a resumed publisher: a new control is received now and yields a
    # new pinned publication. This tests UI semantics, not broker restart.
    from greenhouse.sensor.replay_publication import read_publication
    with psycopg.connect(**empty_database, autocommit=True) as connection:
        metadata, events = read_publication(connection, publication)
        replay = metadata[0]
        payload = {'schema_version': 'greenhouse-control-envelope-v1',
            'control_message_id': hashlib.sha256(f'{replay}:ctrl:3'.encode()).hexdigest(),
            'replay_id': replay, 'control_sequence': 3, 'control_type': 'replay_cursor',
            'published_at': datetime.now(timezone.utc).isoformat(),
            'payload': {'logical_cursor_line': 2, 'logical_cursor_sequence': 1,
                        'source_observed_at': '2019-12-16T00:00:00', 'status': 'running'}}
        resumed = delivery(payload, 'control_messages', 3)
        resumed['received_at'] = datetime.now(timezone.utc).isoformat()
        commit_batch(connection, 'ui-' + replay, 'v1', 1, [resumed])
        publish_replay(connection, replay_id=replay, scope_id=metadata[1], cycle_id=metadata[2],
                       compartment_id=metadata[3], quality_policy_version=metadata[4])
    app.button(key='replay_refresh').click().run()
    assert not app.exception
    assert any(item.label == 'Trạng thái replay' and item.value == 'running' for item in app.metric)
    assert any(item.label == 'Tín hiệu collector' and item.value == 'Đang nhận' for item in app.metric)
