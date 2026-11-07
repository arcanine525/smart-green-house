"""Conservative replay progress derived from landed collector deliveries.

A completion request is not proof of complete replay. This module reports
missing and conflicting evidence; it never fills gaps with inferred values.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

from greenhouse.sensor.schema import SCHEMA_VERSION, CONTROL_SCHEMA_VERSION
from greenhouse.sensor.stream_records import decode_ready_record
from greenhouse.sensor.event_time import event_slot, normalize_cursor


@dataclass(frozen=True)
class ReplayState:
    replay_id: str
    observed_through: str | None
    cursor_sequence: int
    control_sequence: int
    received_through: str | None
    replay_status: str
    expected_events: int | None
    emitted_unique_events: int | None
    emitted_total_messages: int | None
    rejected_source_rows: int | None
    fault_summary: dict | None
    accepted_events: int
    missing_sequences: tuple[int, ...]
    conflicts: tuple[str, ...]
    quarantined: int
    checksum_verified: bool
    completion_requested: bool
    cursor_mismatch: bool
    complete: bool


def reconcile(replay_id, records):
    """Reconcile distinct transport rows, control order and payload checksum.

    Cursor advances only through contiguous, non-conflicting control sequences.
    Identical MQTT redeliveries are deduplicated by their outbox row identity;
    outbox identity collisions fail closed. Completeness additionally requires
    all source event sequences, a matching manifest and no quarantined rows.
    """
    if not replay_id:
        raise ValueError('replay_id is required')
    variants, controls, transport = {}, {}, {}
    quarantined, received = 0, []
    for row in records:
        if not isinstance(row, dict) or row.get('replay_id') != replay_id:
            continue
        if row.get('received_at'):
            try:
                stamp = datetime.fromisoformat(row['received_at'])
                if stamp.tzinfo is not None:
                    received.append(stamp.astimezone(timezone.utc))
            except (TypeError, ValueError):
                pass
        result = decode_ready_record(row)
        if result.disposition != 'accepted':
            quarantined += 1
            continue
        payload = result.payload
        identity = result.identity
        variants.setdefault(identity, {})[result.payload_hash] = payload
        if payload['schema_version'] == CONTROL_SCHEMA_VERSION:
            controls.setdefault(payload['control_sequence'], set()).add(identity)
        if payload['schema_version'] == SCHEMA_VERSION:
            key = (row.get('outbox_table'), row.get('outbox_row_id'))
            if key[0] == 'telemetry_messages' and type(key[1]) is int and key[1] > 0:
                transport.setdefault(key[1], set()).add(result.payload_hash)
    conflicts = {identity for identity, values in variants.items() if len(values) != 1}
    cursor_sequence = control_sequence = 0
    observed = None
    cursor_status = None
    replay_status = 'unknown'
    manifest = None
    while control_sequence + 1 in controls:
        seq = control_sequence + 1
        ids = controls[seq]
        if len(ids) != 1 or next(iter(ids)) in conflicts:
            conflicts.update(ids)
            break
        identity = next(iter(ids))
        envelope = next(iter(variants[identity].values()))
        if envelope['control_type'] == 'replay_cursor':
            cursor = envelope['payload']['logical_cursor_sequence']
            if cursor < cursor_sequence or cursor > 1_000_000:
                conflicts.add(identity)
                break
            cursor_sequence = cursor
            observed = envelope['payload']['source_observed_at']
            cursor_status = envelope['payload']['status']
            replay_status = cursor_status
        elif envelope['control_type'] == 'pause_state':
            replay_status = 'paused'
        elif envelope['control_type'] == 'completion_manifest':
            if manifest is not None:
                conflicts.add(identity)
                break
            manifest = envelope['payload']
        control_sequence = seq
    telemetry = [next(iter(v.values())) for identity, v in variants.items()
                 if identity not in conflicts and len(v) == 1
                 and next(iter(v.values()))['schema_version'] == SCHEMA_VERSION]
    sequence_owners = {}
    for event in telemetry:
        sequence_owners.setdefault(event['sequence'], set()).add(event['event_id'])
    for owners in sequence_owners.values():
        if len(owners) > 1:
            conflicts.update(owners)
    covered = {seq for seq, owners in sequence_owners.items() if len(owners) == 1 and not owners & conflicts}
    missing = tuple(i for i in range(1, cursor_sequence + 1) if i not in covered)
    expected = manifest['expected_unique_events'] if manifest else None
    cursor_mismatch = False
    if observed is not None and cursor_sequence in sequence_owners and len(sequence_owners[cursor_sequence]) == 1:
        final_identity = next(iter(sequence_owners[cursor_sequence]))
        if final_identity not in conflicts:
            final_event = next(iter(variants[final_identity].values()))
            cursor_mismatch = normalize_cursor(observed) != event_slot(final_event)
    checksum_verified = False
    if manifest and len(transport) == manifest['emitted_total_messages'] and all(len(v) == 1 for v in transport.values()):
        cumulative = '0' * 64
        for row_id in sorted(transport):
            cumulative = hashlib.sha256(f'{cumulative}:{next(iter(transport[row_id]))}'.encode('ascii')).hexdigest()
        checksum_verified = cumulative == manifest['payload_checksum']
    complete = bool(manifest and cursor_status == 'completed' and (expected == 0 or observed is not None)
                    and checksum_verified and not cursor_mismatch and not missing and not conflicts and not quarantined
                    and cursor_sequence == expected == len(covered)
                    and manifest['emitted_unique_events'] == len(covered))
    return ReplayState(replay_id, observed, cursor_sequence, control_sequence,
                       max(received).isoformat() if received else None, replay_status, expected,
                       manifest['emitted_unique_events'] if manifest else None,
                       manifest['emitted_total_messages'] if manifest else None,
                       manifest.get('rejected_source_rows', 0) if manifest else None,
                       manifest['fault_summary'] if manifest else None, len(covered),
                       missing, tuple(sorted(conflicts)), quarantined, checksum_verified,
                       manifest is not None, cursor_mismatch, complete)


def load_replay_state(connection, replay_id):
    """Read one consistent SQL result and derive state for the requested replay.

    The rows are scoped by the collector transport replay ID, including
    quarantined payloads. No historical warehouse publication is touched.
    """
    if not replay_id:
        raise ValueError('replay_id is required')
    rows = connection.execute(
        "SELECT record FROM sensor_stream.delivery WHERE record->>'replay_id' = %s",
        (replay_id,),
    ).fetchall()
    return reconcile(replay_id, (row[0] for row in rows))
