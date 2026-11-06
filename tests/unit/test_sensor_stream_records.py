import base64
import json

from greenhouse.sensor.collector import BatchManager, DurableSpool
from greenhouse.sensor.stream_records import decode_ready_record
from unit.test_sensor_collector import make_valid_telemetry_payload


def ready_record(tmp_path, payload):
    spool = DurableSpool(tmp_path / 'spool')
    spool.record_delivery('v1/telemetry/replay-101/AICU', payload, '2026-11-06T10:01:00Z')
    directory = tmp_path / 'ready'
    batch = BatchManager(spool, directory).pack_batch()
    return json.loads((directory / batch.file_name).read_text())


def test_accepts_verified_collector_telemetry(tmp_path):
    raw = make_valid_telemetry_payload()
    result = decode_ready_record(ready_record(tmp_path, raw))
    assert result.disposition == 'accepted'
    assert result.raw == raw
    assert result.replay_id == 'replay-101'
    assert result.identity == json.loads(raw)['event_id']


def test_quarantines_unavailable_legacy_bytes(tmp_path):
    record = ready_record(tmp_path, make_valid_telemetry_payload())
    del record['raw_payload_base64']
    assert decode_ready_record(record).reason == 'raw_payload_unavailable'


def test_quarantines_corruption_and_preserves_bytes(tmp_path):
    record = ready_record(tmp_path, make_valid_telemetry_payload())
    record['raw_payload_base64'] = base64.b64encode(b'altered').decode()
    record['raw_bytes_len'] = len(b'altered')
    result = decode_ready_record(record)
    assert result.reason == 'raw_payload_integrity_mismatch'
    assert result.raw == b'altered'


def test_never_promotes_collector_quarantine(tmp_path):
    record = ready_record(tmp_path, b'not json\xff')
    result = decode_ready_record(record)
    assert result.reason == 'collector_quarantine'
    assert result.raw == b'not json\xff'


def test_rejects_tampered_metadata(tmp_path):
    record = ready_record(tmp_path, make_valid_telemetry_payload())
    record['replay_id'] = 'different-replay'
    assert decode_ready_record(record).reason == 'collector_metadata_mismatch'
