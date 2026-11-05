"""Engineer-authored fixed-bound profiles use the shared immutable repository.

No automatic version bump, overwrite or publication change: the caller names
the profile and revision explicitly, supplies provenance, and commits it.
"""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import re

from greenhouse.contracts import (
    QueryContext, RuleBasis, RuleScheduleRevision, RuleScheduleSegment,
    rule_segment_sort_key,
)
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.db.rule_schedule import register_rule_schedule


def save_drain_profile(connection, publication, cycle, *, name, version,
                       ec_lower, ec_upper, ph_lower, ph_upper, provenance):
    """Register one unverified revision; transaction commit belongs to caller.

    Re-saving identical content is idempotent; changing existing content is
    rejected by register_rule_schedule. Both metric intervals cover the chosen
    cycle in source-naive time. Old revisions remain explicitly selectable.
    """
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', name):
        raise ValueError('Tên profile: 1–48 chữ/số ASCII, dấu gạch ngang hoặc gạch dưới')
    if type(version) is not int or version < 1:
        raise ValueError('Version phải là số nguyên dương')
    if not isinstance(provenance, str) or not provenance.strip() or len(provenance) > 400:
        raise ValueError('Cần nguồn/lý do cho profile (1–400 ký tự)')
    adapter = PostgresDrainQueryAdapter(connection)
    pinned = adapter.publication(publication.publication_id)
    if pinned != publication:
        raise ValueError('Profile phải dùng đúng publication đã pin')
    context = QueryContext(publication.scope_id, publication.publication_id,
                           publication.snapshot_id, cycle.cycle_id,
                           (cycle.compartment_id,), cycle.start, cycle.end)
    pinned_cycle = adapter.cycle_window(context)
    if (pinned_cycle.start, pinned_cycle.end) != (cycle.start, cycle.end):
        raise ValueError('Profile phải dùng đúng cửa sổ vụ đã pin')
    try:
        bounds = tuple(Decimal(str(value)) for value in (ec_lower, ec_upper, ph_lower, ph_upper))
    except (InvalidOperation, ValueError):
        raise ValueError('Cận phải là số thập phân hữu hạn') from None
    if not all(value.is_finite() for value in bounds):
        raise ValueError('Cận phải là số thập phân hữu hạn')
    family = f'drain-user:{publication.scope_id}:{cycle.compartment_id}:{name}'
    segments = tuple(sorted((RuleScheduleSegment(
        metric=metric, measurement_context='drain_process_computer',
        basis=RuleBasis.MEASURED, start=datetime.combine(cycle.start, time.min),
        end=datetime.combine(cycle.end, time.min), lower=lower, upper=upper)
        for metric, lower, upper in (('ec', bounds[0], bounds[1]),
                                     ('ph', bounds[2], bounds[3]))), key=rule_segment_sort_key))
    revision = RuleScheduleRevision(
        rule_version=f'{family}:v{version}', rule_id=family, version=version,
        scope_id=publication.scope_id, compartment_id=cycle.compartment_id,
        evidence_reference=provenance.strip(), is_demo=False,
        label='user_unverified', segments=segments)
    register_rule_schedule(connection, revision)
    return revision
