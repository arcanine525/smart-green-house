import hashlib
import json

from greenhouse.sensor.collector import BatchManager, DurableSpool
from greenhouse.sensor.replay_state import reconcile
from unit.test_sensor_collector import make_valid_telemetry_payload


def control(sequence, kind, payload):
    return {'schema_version': 'greenhouse-control-envelope-v1',
            'control_message_id': hashlib.sha256(f'replay-101:ctrl:{sequence}'.encode()).hexdigest(),
            'replay_id': 'replay-101', 'control_sequence': sequence,
            'control_type': kind, 'published_at': '2026-11-06T10:00:00+00:00', 'payload': payload}


def rows(tmp_path, messages):
    spool = DurableSpool(tmp_path / 'spool')
    for table, row_id, raw in messages:
        spool.record_delivery('v1/telemetry/replay-101/AICU', raw, '2026-11-06T10:01:00+00:00',
                              outbox_table=table, outbox_row_id=row_id, replay_id='replay-101')
    directory = tmp_path / 'ready'
    batch = BatchManager(spool, directory).pack_batch()
    return [json.loads(line) for line in (directory / batch.file_name).read_text().splitlines()]


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def example_messages():
    raw = make_valid_telemetry_payload()
    digest = hashlib.sha256(('0' * 64 + ':' + hashlib.sha256(raw).hexdigest()).encode()).hexdigest()
    cursor = control(1, 'replay_cursor', {'logical_cursor_line': 2, 'logical_cursor_sequence': 1,
                     'source_observed_at': '2019-12-16T00:00:00', 'status': 'completed'})
    manifest = control(2, 'completion_manifest', {'expected_unique_events': 1, 'emitted_unique_events': 1,
                       'emitted_total_messages': 1, 'payload_checksum': digest,
                       'fault_summary': {'duplicates': 0, 'conflicts': 0, 'delays': 0, 'drops': 0},
                       'reconciliation_requested': True})
    return [('telemetry_messages', 1, raw), ('control_messages', 1, dump(cursor)),
            ('control_messages', 2, dump(manifest))]


def test_out_of_order_control_and_duplicate_reconcile(tmp_path):
    messages = example_messages()
    records = rows(tmp_path, [messages[2], messages[0], messages[1], messages[0]])
    state = reconcile('replay-101', records)
    assert state.complete and state.checksum_verified
    assert state.accepted_events == 1
    assert state.cursor_sequence == 1
    assert state.observed_through == '2019-12-16T00:00:00'


def test_manifest_cannot_complete_without_cursor_or_event(tmp_path):
    messages = example_messages()
    state = reconcile('replay-101', rows(tmp_path, [messages[2]]))
    assert not state.complete and not state.completion_requested
    assert state.control_sequence == 0


def test_conflict_and_gap_prevent_completion(tmp_path):
    messages = example_messages()
    mutated = json.loads(messages[0][2])
    mutated['payload']['actual_measurements']['Tair'] = '99'
    records = rows(tmp_path, messages + [('telemetry_messages', 2, dump(mutated))])
    state = reconcile('replay-101', records)
    assert not state.complete
    assert state.conflicts and state.missing_sequences == (1,)


def test_received_time_compares_instants_not_offset_strings(tmp_path):
    records = rows(tmp_path, example_messages())
    records[0]['received_at'] = '2026-11-06T12:00:00+07:00'
    records[1]['received_at'] = '2026-11-06T06:00:00+00:00'
    records[2]['received_at'] = '2026-11-06T05:30:00+00:00'
    assert reconcile('replay-101', records).received_through == '2026-11-06T06:00:00+00:00'


def test_late_event_fills_gap_after_manifest(tmp_path):
    messages = example_messages()
    all_rows = rows(tmp_path, [messages[1], messages[2], messages[0]])
    before = reconcile('replay-101', all_rows[:2])
    assert before.completion_requested and not before.complete
    assert before.missing_sequences == (1,)
    after = reconcile('replay-101', all_rows)
    assert after.complete and after.missing_sequences == ()


def test_cursor_rejects_invalid_or_aware_source_time():
    import pytest
    from greenhouse.sensor.schema import ValidationError, validate_control_envelope
    payload = {'logical_cursor_line': 2, 'logical_cursor_sequence': 1,
               'source_observed_at': 'not-a-time', 'status': 'running'}
    with pytest.raises(ValidationError):
        validate_control_envelope(control(1, 'replay_cursor', payload))
    payload['source_observed_at'] = '2019-12-16T00:00:00+00:00'
    with pytest.raises(ValidationError, match='source-naive'):
        validate_control_envelope(control(1, 'replay_cursor', payload))


def test_unbounded_cursor_is_rejected_before_allocating_gap_list(tmp_path):
    bad = control(1, 'replay_cursor', {'logical_cursor_line': 2, 'logical_cursor_sequence': 10**12,
                  'source_observed_at': '2019-12-16T00:00:00', 'status': 'running'})
    state = reconcile('replay-101', rows(tmp_path, [('control_messages', 1, dump(bad))]))
    assert not state.complete and state.cursor_sequence == 0 and state.conflicts


def test_manifest_does_not_complete_running_or_missing_cursor_time(tmp_path):
    messages = example_messages()
    cursor = json.loads(messages[1][2])
    cursor['payload']['status'] = 'running'
    running = [messages[0], ('control_messages', 1, dump(cursor)), messages[2]]
    assert not reconcile('replay-101', rows(tmp_path / 'running', running)).complete
    cursor['payload']['status'] = 'completed'
    cursor['payload']['source_observed_at'] = None
    missing_time = [messages[0], ('control_messages', 1, dump(cursor)), messages[2]]
    assert not reconcile('replay-101', rows(tmp_path / 'missing', missing_time)).complete


def test_cursor_time_must_match_last_event_sequence(tmp_path):
    messages = example_messages()
    cursor = json.loads(messages[1][2])
    cursor['payload']['source_observed_at'] = '2019-12-16T00:05:00'
    records = rows(tmp_path, [messages[0], ('control_messages', 1, dump(cursor)), messages[2]])
    state = reconcile('replay-101', records)
    assert state.cursor_mismatch and not state.complete


def test_pause_control_changes_replay_status_until_cursor_resumes(tmp_path):
    paused = control(1, 'pause_state', {'paused_at_line': 2, 'paused_at_sequence': 1, 'reason': 'test'})
    cursor = control(2, 'replay_cursor', {'logical_cursor_line': 2, 'logical_cursor_sequence': 1,
                     'source_observed_at': '2019-12-16T00:00:00', 'status': 'running'})
    records = rows(tmp_path, [('control_messages', 1, dump(paused)), ('control_messages', 2, dump(cursor))])
    assert reconcile('replay-101', records[:1]).replay_status == 'paused'
    assert reconcile('replay-101', records).replay_status == 'running'
