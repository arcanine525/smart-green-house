"""Shared conservative replay membership plan for PostgreSQL and ClickHouse."""
from dataclasses import asdict
import hashlib
import json
from greenhouse.sensor.event_time import event_slot,normalize_cursor


def plan_publication(state,variants,cycle,policy,*,replay_id,scope_id,cycle_id,compartment_id,quality_policy_version):
    observed = normalize_cursor(state.observed_through) if state.observed_through else None
    if observed is not None and observed.tzinfo is not None:
        raise ValueError('replay cursor must use source-naive time')
    cursor_outside_cycle = bool(observed and not cycle[0] <= observed <= cycle[1])
    members, slots, scope_conflicts, time_conflicts = [], {}, [], []
    snapshots = {(event['source_provenance']['snapshot_id'], event['source_provenance']['snapshot_sha'])
                 for _, _, event in variants
                 if (event['data_scope_id'], event['cycle_id'], event['compartment_id']) == (scope_id, cycle_id, compartment_id)}
    for identity, payload_hash, event in variants:
        if ((event['data_scope_id'], event['cycle_id'], event['compartment_id']) != (scope_id, cycle_id, compartment_id)
                or len(snapshots) > 1):
            scope_conflicts.append(identity)
            continue
        when = event_slot(event)
        if not (cycle[0] <= when <= cycle[1]) or not (policy[0] <= when < policy[1] or when == cycle[1] == policy[1]):
            time_conflicts.append(identity)
            continue
        if observed is None or cursor_outside_cycle or event['sequence'] > state.cursor_sequence or when > observed:
            continue
        slots.setdefault(when, []).append((identity, payload_hash))
    slot_conflicts = []
    for entries in slots.values():
        if len(entries) > 1:
            slot_conflicts.extend(identity for identity, _ in entries)
        else:
            members.extend(entries)
    members.sort()
    state_data = asdict(state)
    state_data['slot_conflicts'] = sorted(slot_conflicts)
    state_data['scope_conflicts'] = sorted(scope_conflicts)
    state_data['time_conflicts'] = sorted(time_conflicts)
    state_data['cursor_outside_cycle'] = cursor_outside_cycle
    if slot_conflicts or scope_conflicts or time_conflicts or cursor_outside_cycle:
        state_data['complete'] = False
    checksum = hashlib.sha256(json.dumps(members, separators=(',', ':')).encode()).hexdigest()
    metadata = {'replay_id': replay_id, 'scope_id': scope_id, 'cycle_id': cycle_id,
                'compartment_id': compartment_id, 'quality_policy_version': quality_policy_version,
                'state': state_data, 'membership_checksum': checksum}
    publication_id = hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return publication_id,metadata,members
