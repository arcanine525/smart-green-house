"""Validate collector records before a streaming batch can use their identities.

Legacy ready files lack exact payload bytes. They are deliberately quarantined:
re-serializing their parsed JSON cannot verify the collector's original hash.
"""
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from greenhouse.sensor.event_time import event_slot, normalize_cursor
from greenhouse.sensor.schema import (
    CONTROL_SCHEMA_VERSION, SCHEMA_VERSION, ValidationError,
    validate_control_envelope, validate_telemetry_envelope,
)


@dataclass(frozen=True)
class ReadyRecord:
    disposition: str
    reason: str | None
    replay_id: str | None
    identity: str | None
    payload_hash: str | None
    payload: dict[str, Any] | None
    raw: bytes | None


def decode_ready_record(record: Any) -> ReadyRecord:
    """Check transport integrity and the envelope; never promote quarantine.

    Returns an identity scoped by replay for valid telemetry/control records.
    A caller must retain the original collector row for every quarantine result.
    """
    def reject(reason, raw=None):
        return ReadyRecord('quarantine', reason, None, None, None, None, raw)

    if not isinstance(record, dict):
        return reject('invalid_collector_record')
    encoded = record.get('raw_payload_base64')
    if not isinstance(encoded, str):
        return reject('raw_payload_unavailable')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return reject('invalid_raw_payload_encoding')
    if len(raw) != record.get('raw_bytes_len') or hashlib.sha256(raw).hexdigest() != record.get('payload_hash'):
        return reject('raw_payload_integrity_mismatch', raw)
    if record.get('is_valid') is not True:
        return reject('collector_quarantine', raw)
    try:
        payload = json.loads(raw.decode('utf-8'))
        if not isinstance(payload, dict):
            return reject('invalid_envelope', raw)
        version = payload.get('schema_version')
        if version == SCHEMA_VERSION:
            validate_telemetry_envelope(payload)
            event_slot(payload)
            identity = payload['event_id']
        elif version == CONTROL_SCHEMA_VERSION:
            validate_control_envelope(payload)
            if payload['control_type'] == 'replay_cursor' and payload['payload']['source_observed_at'] is not None:
                normalize_cursor(payload['payload']['source_observed_at'])
            identity = payload['control_message_id']
        else:
            return reject('unsupported_schema', raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return reject('invalid_envelope', raw)
    if record.get('payload') != payload or record.get('replay_id') != payload['replay_id'] or record.get('event_id') != identity:
        return reject('collector_metadata_mismatch', raw)
    return ReadyRecord('accepted', None, payload['replay_id'], identity, record['payload_hash'], payload, raw)
