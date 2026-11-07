"""Validated engineer-authored profile construction, independent of storage."""
from datetime import datetime,time
from decimal import Decimal,InvalidOperation
import re
from greenhouse.contracts import RuleBasis,RuleScheduleRevision,RuleScheduleSegment,rule_segment_sort_key


def build_drain_profile(publication,cycle,*,name,version,ec_lower,ec_upper,ph_lower,ph_upper,provenance):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', name):
        raise ValueError('Tên profile: 1–48 chữ/số ASCII, dấu gạch ngang hoặc gạch dưới')
    if type(version) is not int or version < 1:
        raise ValueError('Version phải là số nguyên dương')
    if not isinstance(provenance, str) or not provenance.strip() or len(provenance) > 400:
        raise ValueError('Cần nguồn/lý do cho profile (1–400 ký tự)')
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
    return revision
