"""Engine-independent value contracts. Decimal and clock semantics are explicit."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from pathlib import PurePosixPath

WATER_UNIT = 'L/m²'
RESOURCE_WATER_UNIT = 'L/m²/day'
METRIC_VERSION = 'br02-summary-v1'
HARVEST_UNIT = 'kg/m²'
HARVEST_SOURCE_UNIT = 'kg/m²/harvest'
HARVEST_METRIC_VERSION = 'br05-summary-v1'
ASSUMPTION_LABEL = 'giả định chưa xác minh'


class QualityStatus(StrEnum):
    ACCEPTED = 'accepted'
    MISSING = 'missing'
    INVALID = 'invalid'
    SUSPECT = 'suspect'


class Applicability(StrEnum):
    APPLICABLE = 'applicable'
    UNKNOWN = 'unknown'


class BasisStatus(StrEnum):
    VERIFIED = 'verified'
    ASSUMED = 'assumed_unverified'
    UNKNOWN = 'unknown'
    INCOMPATIBLE = 'incompatible'


class RuleStatus(StrEnum):
    NOT_CONFIGURED = 'not_configured'
    CONFIGURED = 'configured'
    WITHIN_RANGE = 'within_range'
    OUTSIDE_RANGE = 'outside_range'
    UNKNOWN = 'unknown'


class ResultStatus(StrEnum):
    COMPLETE = 'complete'
    PARTIAL = 'partial'
    UNKNOWN = 'unknown'


class TimeBasis(StrEnum):
    SOURCE_NAIVE = 'source_naive'


def identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Identifier must be a nonempty string')


def finite(value, *, nullable=False, nonnegative=False):
    if value is None and nullable:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or (nonnegative and value < 0):
        raise ValueError('Expected finite Decimal' + (' >= 0' if nonnegative else ''))


def calendar_window(start, end):
    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError('Window requires whole dates with start < end (half-open)')


def clock(value, *, aware):
    if not isinstance(value, datetime) or (value.utcoffset() is not None) != aware:
        raise ValueError('Expected offset-aware operational time' if aware else 'Expected naive source time')


def checksum(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Expected lowercase SHA-256')


def tuple_of(value, kind):
    if not isinstance(value, tuple) or any(not isinstance(item, kind) for item in value):
        raise ValueError('Expected immutable tuple of contract values')


def exact_add(left, right):
    """Decimal addition in a context whose precision always covers both
    boundaries, so invariants compare against the EXACT sum and never the
    ambient (28-digit) rounding decision."""
    integer_digits = max(max(value.adjusted() + 1, 1) for value in (left, right))
    fractional_digits = max(max(-value.as_tuple().exponent, 0)
                            for value in (left, right))
    with localcontext() as ctx:
        ctx.prec = max(50, integer_digits + fractional_digits + 2)
        return left + right


@dataclass(frozen=True)
class CorrectionRef:
    correction_id: str
    version: int

    def __post_init__(self):
        identifier(self.correction_id)
        if type(self.version) is not int or self.version < 1:
            raise ValueError('Invalid correction version')


@dataclass(frozen=True)
class MeasureUnit:
    measure_code: str
    unit: str

    def __post_init__(self):
        identifier(self.measure_code); identifier(self.unit)


@dataclass(frozen=True)
class Versions:
    pipeline_run_id: str
    quality_policy_version: str
    parser_version: str
    code_version: str
    config_bundle_hash: str
    metric_version: str = METRIC_VERSION
    rule_version: str | None = None
    tariff_version: str | None = None
    scenario_version: str | None = None

    def __post_init__(self):
        for value in (self.pipeline_run_id, self.quality_policy_version, self.parser_version,
                      self.code_version, self.config_bundle_hash, self.metric_version):
            identifier(value)
        for value in (self.rule_version, self.tariff_version, self.scenario_version):
            if value is not None:
                identifier(value)


@dataclass(frozen=True)
class PublicationRef:
    publication_id: str
    scope_id: str
    snapshot_id: str
    versions: Versions
    committed_at: datetime
    finality_status: str
    time_basis: TimeBasis = TimeBasis.SOURCE_NAIVE

    def __post_init__(self):
        for value in (self.publication_id, self.scope_id, self.snapshot_id):
            identifier(value)
        clock(self.committed_at, aware=True)
        if self.finality_status not in ('provisional', 'complete'):
            raise ValueError('Unknown publication finality')
        if not isinstance(self.versions, Versions) or not isinstance(self.time_basis, TimeBasis):
            raise ValueError('Invalid publication metadata')


@dataclass(frozen=True)
class IrrigationRuleProfile:
    rule_version: str
    rule_id: str
    version: int
    scope_id: str
    compartment_id: str
    min_drain_pct: Decimal
    max_drain_pct: Decimal
    min_irrigation_l_m2: Decimal
    max_irrigation_l_m2: Decimal
    evidence_reference: str
    is_demo: bool
    label: str
    unit: str = WATER_UNIT
    ratio_unit: str = '%'

    def __post_init__(self):
        for value in (self.rule_version, self.rule_id, self.scope_id, self.compartment_id,
                      self.evidence_reference, self.label):
            identifier(value)
        if type(self.version) is not int or self.version < 1 or type(self.is_demo) is not bool:
            raise ValueError('Invalid rule version/demo flag')
        for value in (self.min_drain_pct, self.max_drain_pct, self.min_irrigation_l_m2, self.max_irrigation_l_m2):
            finite(value, nonnegative=True)
        if self.min_drain_pct > self.max_drain_pct or self.min_irrigation_l_m2 > self.max_irrigation_l_m2:
            raise ValueError('Rule bounds must be ordered')
        if self.unit != WATER_UNIT or self.ratio_unit != '%' or (self.is_demo and self.label != 'demo_rule'):
            raise ValueError('Rule requires source quantity units and an explicit demo label')

    def matches(self, context):
        if (self.scope_id, self.compartment_id) != (context.scope_id, context.compartment_ids[0]):
            raise ValueError('Rule profile is outside the requested scope/compartment')


@dataclass(frozen=True)
class HarvestRuleProfile:
    """Immutable optional class-B threshold: one explicit version per
    scope/compartment. There is NO default; a profile is used only when a
    caller passes its rule_version (demo 10% must carry the demo label)."""
    rule_version: str
    rule_id: str
    version: int
    scope_id: str
    compartment_id: str
    max_class_b_share_pct: Decimal
    evidence_reference: str
    is_demo: bool
    label: str
    unit: str = HARVEST_UNIT
    ratio_unit: str = '%'

    def __post_init__(self):
        for value in (self.rule_version, self.rule_id, self.scope_id, self.compartment_id,
                      self.evidence_reference, self.label):
            identifier(value)
        if type(self.version) is not int or self.version < 1 or type(self.is_demo) is not bool:
            raise ValueError('Invalid rule version/demo flag')
        finite(self.max_class_b_share_pct, nonnegative=True)
        if self.max_class_b_share_pct > 100:
            raise ValueError('Class-B threshold must stay within percent range')
        if self.unit != HARVEST_UNIT or self.ratio_unit != '%' or (self.is_demo and self.label != 'demo_rule'):
            raise ValueError('Rule requires kg/m² units and an explicit demo label')

    def matches(self, context):
        if (self.scope_id, self.compartment_id) != (context.scope_id, context.compartment_ids[0]):
            raise ValueError('Rule profile is outside the requested scope/compartment')


@dataclass(frozen=True)
class QueryContext:
    scope_id: str
    publication_id: str
    snapshot_id: str
    cycle_id: str
    compartment_ids: tuple[str, ...]
    start: date
    end: date
    rule_version: str | None = None

    def __post_init__(self):
        if self.rule_version is not None:
            identifier(self.rule_version)
        for value in (self.scope_id, self.publication_id, self.snapshot_id, self.cycle_id):
            identifier(value)
        if not isinstance(self.compartment_ids, tuple) or len(self.compartment_ids) != 1:
            raise ValueError('One cycle/compartment per context; compare separate contexts')
        identifier(self.compartment_ids[0])
        calendar_window(self.start, self.end)

    def matches(self, publication):
        if (self.publication_id, self.scope_id, self.snapshot_id) != (
                publication.publication_id, publication.scope_id, publication.snapshot_id):
            raise ValueError('Context/publication identity mismatch')


@dataclass(frozen=True)
class CycleWindow:
    cycle_id: str
    compartment_id: str
    start: date
    end: date

    def __post_init__(self):
        identifier(self.cycle_id); identifier(self.compartment_id)
        calendar_window(self.start, self.end)


@dataclass(frozen=True)
class CatalogPublication:
    """One filter option: the current sealed publication of a data scope.

    usable reports only that the sealed irrigation mart bundle exists, and
    harvest_usable only that the frozen harvest mart bundle expected by this
    release exists; both are catalog facts, never quality or BR statements.
    """
    scope_id: str
    snapshot_id: str
    publication_id: str
    committed_at: datetime
    finality_status: str
    code_version: str
    bundle_version: str | None = None
    day_count: int | None = None
    cycle_count: int | None = None
    harvest_bundle_version: str | None = None
    harvest_reason_codes: tuple[str, ...] = ()
    climate_bundle_version: str | None = None
    climate_reason_codes: tuple[str, ...] = ()
    drain_bundle_version: str | None = None
    drain_reason_codes: tuple[str, ...] = ()

    def __post_init__(self):
        for value in (self.scope_id, self.snapshot_id, self.publication_id, self.code_version):
            identifier(value)
        clock(self.committed_at, aware=True)
        if self.finality_status not in ('provisional', 'complete'):
            raise ValueError('Unknown publication finality')
        if (self.bundle_version is None) != (self.day_count is None) or (self.bundle_version is None) != (self.cycle_count is None):
            raise ValueError('Mart bundle receipt fields must travel together')
        for value in (self.day_count, self.cycle_count):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('Invalid mart bundle count')
        tuple_of(self.harvest_reason_codes, str)
        if self.harvest_bundle_version is not None and self.harvest_reason_codes:
            raise ValueError('A present harvest bundle carries no absence reasons')
        tuple_of(self.climate_reason_codes, str)
        if self.climate_bundle_version is not None and self.climate_reason_codes:
            raise ValueError('A present climate bundle carries no absence reasons')
        tuple_of(self.drain_reason_codes, str)
        if self.drain_bundle_version is not None and self.drain_reason_codes:
            raise ValueError('A present drain bundle carries no absence reasons')

    @property
    def usable(self):
        return self.bundle_version is not None

    @property
    def harvest_usable(self):
        return self.harvest_bundle_version is not None

    @property
    def climate_usable(self):
        return self.climate_bundle_version is not None

    @property
    def drain_usable(self):
        return self.drain_bundle_version is not None


@dataclass(frozen=True)
class CatalogCycle:
    """Published water-mart cycle window offered to application filters."""
    cycle_id: str
    compartment_id: str
    compartment_name: str
    start: date
    end: date

    def __post_init__(self):
        for value in (self.cycle_id, self.compartment_id, self.compartment_name):
            identifier(value)
        calendar_window(self.start, self.end)


@dataclass(frozen=True)
class MeasureValue:
    value: Decimal | None
    unit: str
    quality: QualityStatus
    applicability: Applicability
    reason_code: str | None = None
    source_unit: str | None = None
    basis_status: BasisStatus = BasisStatus.UNKNOWN
    basis_version_id: str | None = None
    area_m2: Decimal | None = None
    basis_kind: str | None = None
    assumption_id: str | None = None
    assumption_version: int | None = None
    basis_label: str | None = None

    def __post_init__(self):
        finite(self.value, nullable=True)
        identifier(self.unit)
        for value in (self.reason_code, self.source_unit):
            if value is not None:
                identifier(value)
        if not isinstance(self.quality, QualityStatus) or not isinstance(self.applicability, Applicability) or not isinstance(self.basis_status, BasisStatus):
            raise ValueError('Measure statuses must be contract enums')
        if self.quality == QualityStatus.ACCEPTED and self.value is None:
            raise ValueError('Accepted measure requires a value')
        if self.quality == QualityStatus.MISSING and self.value is not None:
            raise ValueError('Missing measure must be NULL')
        if self.quality != QualityStatus.ACCEPTED or self.applicability == Applicability.UNKNOWN:
            identifier(self.reason_code)
        if self.basis_status in (BasisStatus.VERIFIED, BasisStatus.ASSUMED):
            identifier(self.basis_version_id); identifier(self.basis_kind)
            finite(self.area_m2, nonnegative=True)
            if self.area_m2 == 0:
                raise ValueError('Area basis must be positive')
            if self.basis_status == BasisStatus.ASSUMED:
                identifier(self.assumption_id)
                if type(self.assumption_version) is not int or self.assumption_version < 1 or self.basis_label != ASSUMPTION_LABEL:
                    raise ValueError('Assumed basis requires version and visible label')
            elif self.assumption_id is not None or self.assumption_version is not None or self.basis_label != 'verified':
                raise ValueError('Verified basis metadata is inconsistent')
        elif any(v is not None for v in (self.basis_version_id, self.area_m2, self.basis_kind,
                                         self.assumption_id, self.assumption_version, self.basis_label)):
            raise ValueError('Unknown/incompatible measure basis cannot claim a version')

    @property
    def accepted(self):
        return self.quality == QualityStatus.ACCEPTED and self.applicability == Applicability.APPLICABLE and self.value is not None


@dataclass(frozen=True)
class IrrigationDay:
    publication: PublicationRef
    cycle_id: str
    compartment_id: str
    resource_date: date
    irrigation: MeasureValue
    drain: MeasureValue
    revision_id: str | None = None
    logical_record_key: str | None = None
    correction_id: str | None = None
    correction_version: int | None = None
    rule_status: RuleStatus = RuleStatus.NOT_CONFIGURED
    rule_version: str | None = None
    reason_codes: tuple[str, ...] = ()
    paired: bool = False
    drain_fraction_pct: Decimal | None = None
    drain_exceeds_irrigation: bool | None = None
    rule_profile: IrrigationRuleProfile | None = None

    def __post_init__(self):
        if not isinstance(self.publication, PublicationRef) or not all(isinstance(m, MeasureValue) for m in (self.irrigation, self.drain)):
            raise ValueError('Invalid day publication/measure contracts')
        identifier(self.cycle_id); identifier(self.compartment_id)
        tuple_of(self.reason_codes, str)
        if type(self.resource_date) is not date:
            raise ValueError('Resource grain is a whole source date')
        for measure in (self.irrigation, self.drain):
            if measure.unit != WATER_UNIT or measure.source_unit not in (None, RESOURCE_WATER_UNIT):
                raise ValueError('BR-02 requires L/m² daily quantities (source L/m²/day)')
            if measure.accepted and measure.value < 0:
                raise ValueError('Accepted water quantity cannot be negative')
        if not isinstance(self.rule_status, RuleStatus):
            raise ValueError('Invalid day rule status')
        if self.rule_status == RuleStatus.NOT_CONFIGURED:
            if self.rule_version is not None or self.rule_profile is not None:
                raise ValueError('Unconfigured day cannot claim a rule')
        elif (self.rule_status not in (RuleStatus.WITHIN_RANGE, RuleStatus.OUTSIDE_RANGE, RuleStatus.UNKNOWN)
              or not isinstance(self.rule_profile, IrrigationRuleProfile)
              or self.rule_version != self.rule_profile.rule_version
              or (self.publication.scope_id, self.compartment_id) != (self.rule_profile.scope_id, self.rule_profile.compartment_id)):
            raise ValueError('Day rule metadata does not match its profile')
        if (self.revision_id is None) != (self.logical_record_key is None):
            raise ValueError('Revision identity/key must travel together')
        for value in (self.revision_id, self.logical_record_key, self.correction_id):
            if value is not None:
                identifier(value)
        if (self.correction_id is None) != (self.correction_version is None):
            raise ValueError('Correction identity/version must travel together')
        if self.correction_version is not None and (type(self.correction_version) is not int or self.correction_version < 1):
            raise ValueError('Invalid correction version')
        finite(self.drain_fraction_pct, nullable=True, nonnegative=True)
        if type(self.paired) is not bool or (self.drain_exceeds_irrigation is not None and type(self.drain_exceeds_irrigation) is not bool):
            raise ValueError('Invalid day flags')


@dataclass(frozen=True)
class IrrigationSummary:
    publication: PublicationRef
    context: QueryContext
    window_start: date
    window_end: date
    expected_days: int
    irrigation_observed_l_m2: Decimal | None
    drain_observed_l_m2: Decimal | None
    paired_irrigation_l_m2: Decimal | None
    paired_drain_l_m2: Decimal | None
    drain_fraction_pct: Decimal | None
    irrigation_coverage_pct: Decimal
    drain_coverage_pct: Decimal
    paired_coverage_pct: Decimal
    irrigation_accepted_days: int
    drain_accepted_days: int
    paired_days: int
    unknown_days: int
    suspect_days: int
    drain_exceeds_irrigation_days: int
    status: ResultStatus
    comparison_eligible: bool
    basis_status: BasisStatus
    basis_label: str | None
    basis_version_ids: tuple[str, ...]
    assumption_ids: tuple[str, ...]
    assumption_versions: tuple[int, ...]
    corrections: tuple[CorrectionRef, ...]
    rule_status: RuleStatus = RuleStatus.NOT_CONFIGURED
    rule_version: str | None = None
    outside_rule_days: int | None = None
    unit: str = WATER_UNIT
    ratio_unit: str = '%'
    reason_codes: tuple[str, ...] = ()
    rule_profile: IrrigationRuleProfile | None = None
    rule_unknown_days: int | None = None
    iso_year: int | None = None
    iso_week: int | None = None

    def __post_init__(self):
        self.context.matches(self.publication)
        calendar_window(self.window_start, self.window_end)
        if self.expected_days != (self.window_end - self.window_start).days or self.expected_days < 1:
            raise ValueError('Expected day denominator does not match window')
        if not self.context.start <= self.window_start < self.window_end <= self.context.end:
            raise ValueError('Effective window must be inside the requested window')
        for value in (self.expected_days, self.irrigation_accepted_days, self.drain_accepted_days,
                      self.paired_days, self.unknown_days, self.suspect_days, self.drain_exceeds_irrigation_days):
            if type(value) is not int or not 0 <= value <= self.expected_days:
                raise ValueError('Invalid day count')
        if self.paired_days > min(self.irrigation_accepted_days, self.drain_accepted_days):
            raise ValueError('Paired count exceeds accepted count')
        if not isinstance(self.status, ResultStatus) or not isinstance(self.basis_status, BasisStatus) or type(self.comparison_eligible) is not bool:
            raise ValueError('Invalid summary statuses')
        for value in (self.reason_codes, self.basis_version_ids, self.assumption_ids):
            tuple_of(value, str)
        tuple_of(self.assumption_versions, int)
        tuple_of(self.corrections, CorrectionRef)
        if self.basis_status == BasisStatus.ASSUMED and (self.basis_label != ASSUMPTION_LABEL or not self.assumption_ids or not self.assumption_versions):
            raise ValueError('Assumed summary must retain assumption version and visible label')
        for value in (self.irrigation_observed_l_m2, self.drain_observed_l_m2,
                      self.paired_irrigation_l_m2, self.paired_drain_l_m2, self.drain_fraction_pct):
            finite(value, nullable=True, nonnegative=True)
        for value in (self.irrigation_coverage_pct, self.drain_coverage_pct, self.paired_coverage_pct):
            finite(value, nonnegative=True)
            if value > 100:
                raise ValueError('Coverage exceeds 100 percent')
        if self.unit != WATER_UNIT or self.ratio_unit != '%':
            raise ValueError('Incorrect summary units')
        if self.rule_status == RuleStatus.NOT_CONFIGURED and isinstance(self.rule_status, RuleStatus):
            if any(v is not None for v in (self.rule_version, self.outside_rule_days, self.rule_profile, self.rule_unknown_days)):
                raise ValueError('Rule outcome unavailable without a configured profile')
        elif (self.rule_status != RuleStatus.CONFIGURED or not isinstance(self.rule_status, RuleStatus)
              or not isinstance(self.rule_profile, IrrigationRuleProfile) or self.rule_version != self.rule_profile.rule_version
              or self.rule_version != self.context.rule_version):
            raise ValueError('Summary rule metadata does not match request/profile')
        else:
            self.rule_profile.matches(self.context)
            for n in (self.outside_rule_days, self.rule_unknown_days):
                if type(n) is not int or not 0 <= n <= self.expected_days:
                    raise ValueError('Invalid rule day count')
        if (self.iso_year is None) != (self.iso_week is None):
            raise ValueError('ISO year/week must travel together')
        if self.iso_year is not None and (self.iso_year, self.iso_week) != tuple(self.window_start.isocalendar())[:2]:
            raise ValueError('ISO week does not match summary window')

    @property
    def correction_ids(self):
        return tuple(c.correction_id for c in self.corrections)


@dataclass(frozen=True)
class HarvestRecord:
    """One published harvest record: grain is the source record, never a day.

    Several accepted records may share a harvest date and none may be collapsed.
    paired follows the two MeasureValues (both accepted); reason codes keep the
    per-measure states traceable.
    """
    publication: PublicationRef
    cycle_id: str
    compartment_id: str
    harvest_date: date
    prod_a: MeasureValue
    prod_b: MeasureValue
    revision_id: str
    logical_record_key: str
    correction_id: str | None = None
    correction_version: int | None = None
    paired: bool = False
    reason_codes: tuple[str, ...] = ()
    rule_status: RuleStatus = RuleStatus.NOT_CONFIGURED
    rule_version: str | None = None
    rule_profile: HarvestRuleProfile | None = None
    class_b_share_pct: Decimal | None = None

    def __post_init__(self):
        if not isinstance(self.publication, PublicationRef) or not all(
                isinstance(m, MeasureValue) for m in (self.prod_a, self.prod_b)):
            raise ValueError('Invalid harvest record publication/measure contracts')
        identifier(self.cycle_id); identifier(self.compartment_id)
        identifier(self.revision_id); identifier(self.logical_record_key)
        tuple_of(self.reason_codes, str)
        if type(self.harvest_date) is not date:
            raise ValueError('Harvest records use whole source dates')
        for measure in (self.prod_a, self.prod_b):
            if measure.unit != HARVEST_UNIT or measure.source_unit != HARVEST_SOURCE_UNIT:
                raise ValueError('Harvest measures require kg/m² with source kg/m²/harvest')
        if not isinstance(self.rule_status, RuleStatus):
            raise ValueError('Invalid harvest record rule status')
        if self.rule_status == RuleStatus.NOT_CONFIGURED:
            if self.rule_version is not None or self.rule_profile is not None:
                raise ValueError('Unconfigured record cannot claim a rule')
        elif (self.rule_status not in (RuleStatus.WITHIN_RANGE, RuleStatus.OUTSIDE_RANGE, RuleStatus.UNKNOWN)
              or not isinstance(self.rule_profile, HarvestRuleProfile)
              or self.rule_version != self.rule_profile.rule_version
              or (self.publication.scope_id, self.compartment_id) != (
                  self.rule_profile.scope_id, self.rule_profile.compartment_id)):
            raise ValueError('Record rule metadata does not match its profile')
        finite(self.class_b_share_pct, nullable=True, nonnegative=True)
        a, b = self.prod_a.value, self.prod_b.value
        if self.class_b_share_pct is not None:
            paired_sum = a + b if (a is not None and b is not None) else None
            if not (self.paired and paired_sum not in (None, 0)):
                raise ValueError('A record share requires its own paired nonzero measures')
        if type(self.paired) is not bool or self.paired != (self.prod_a.accepted and self.prod_b.accepted):
            raise ValueError('Paired flag must follow both measure acceptance states')
        if (self.correction_id is None) != (self.correction_version is None):
            raise ValueError('Correction identity/version must travel together')
        if self.correction_version is not None and (type(self.correction_version) is not int or self.correction_version < 1):
            raise ValueError('Invalid correction version')


@dataclass(frozen=True)
class HarvestUnresolvedIssue:
    """Frozen provenance of ONE unresolved quarantine/conflict issue of a
    source row, for future drilldown. Keyed by SOURCE identity + issue
    (parsed issue code/column); raw quarantined numbers are never copied
    here, so an unresolved row stays NULL, not an invented amount.
    """
    source_record_id: str
    disposition: str
    reason_code: str
    issue_column: str | None = None
    relative_path: str | None = None
    line_number: int | None = None
    source_time_serial: Decimal | None = None

    def __post_init__(self):
        identifier(self.source_record_id)
        identifier(self.reason_code)
        if self.disposition not in ('quarantine', 'conflict'):
            raise ValueError('Unresolved issue disposition must be quarantine or conflict')
        if self.issue_column is not None:
            identifier(self.issue_column)
        if self.relative_path is not None:
            identifier(self.relative_path)
        if self.line_number is not None and (type(self.line_number) is not int or self.line_number < 2):
            raise ValueError('Unresolved issue line number starts at the first data line')
        finite(self.source_time_serial, nullable=True)


@dataclass(frozen=True)
class UnresolvedSourceAudit:
    """Authorized read-only audit of ONE frozen unresolved source row.

    A caller may only request an identity that is a member of the pinned
    publication's frozen unresolved issue set for the same snapshot. Raw
    values appear ONLY after file+record verification against immutable
    metadata; a missing/drifted raw stays UNVERIFIED with empty tokens —
    never a fabricated value. These audit values are explanation only; they
    never recompute a KPI.
    """
    source_record_id: str
    snapshot_id: str
    reason_code: str
    disposition: str | None = None
    issue_column: str | None = None
    relative_path: str | None = None
    line_number: int | None = None
    source_time_serial: Decimal | None = None
    headers: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    file_verified: bool = False
    record_verified: bool = False
    note: str | None = None
    publication: PublicationRef | None = None
    cycle_id: str | None = None
    compartment_id: str | None = None
    requested_start: date | None = None
    requested_end: date | None = None
    rule_version: str | None = None
    rule_grain: str | None = None

    def __post_init__(self):
        identifier(self.source_record_id); identifier(self.snapshot_id)
        identifier(self.reason_code)
        if self.disposition not in (None, 'quarantine', 'conflict'):
            raise ValueError('Audit disposition must be quarantine or conflict when set')
        if self.rule_grain not in (None, 'record', 'day', 'week'):
            raise ValueError('Audit rule grain must be record, day or week when set')
        if self.rule_version is not None:
            identifier(self.rule_version)
        if (type(self.file_verified) is not bool or type(self.record_verified) is not bool):
            raise ValueError('Audit verification flags must be booleans')
        if not self.file_verified and self.record_verified:
            raise ValueError('Record cannot verify when the file does not')
        if self.file_verified:
            identifier(self.relative_path)
            if type(self.line_number) is not int or self.line_number < 2:
                raise ValueError('A verified audit needs its physical line')
        if self.record_verified:
            if not self.headers or not self.tokens or len(self.headers) != len(self.tokens):
                raise ValueError('A verified audit exposes exactly the raw header/token row')
            if self.note is not None:
                raise ValueError('Verified audit carries no unavailability note')
        elif self.tokens:
            raise ValueError('Unverified audit exposes no raw values (no fabrications)')
        if not self.record_verified and not self.file_verified and self.note is None:
            raise ValueError('An unverified audit must state why')
        if self.source_time_serial is not None:
            finite(self.source_time_serial)
        if self.publication is not None:
            if not isinstance(self.publication, PublicationRef):
                raise ValueError('Audit publication must be a PublicationRef contract')
        for id_name in (self.cycle_id, self.compartment_id):
            if id_name is not None:
                identifier(id_name)
        for boundary in (self.requested_start, self.requested_end):
            if boundary is not None and type(boundary) is not date:
                raise ValueError('Audit requested window uses whole source dates')
        if self.requested_start is not None and self.requested_end is not None \
                and self.requested_start >= self.requested_end:
            raise ValueError('Audit requested window must be a half-open range')


@dataclass(frozen=True)
class HarvestCompleteness:
    """Frozen verification inputs of one cycle in the pinned publication.

    snapshot_complete is canonical (source resolution AND accepted records all
    paired on both measures); source_resolved is the structural part only.
    Record counts are never a time-coverage claim (BR-05 §3.8).
    """
    snapshot_complete: bool
    source_resolved: bool
    expected_files: int
    files_present: int
    files_loaded: int
    files_failed: int
    records_read: int | None
    records_accepted: int | None
    records_quarantine: int | None
    records_duplicate: int | None
    records_conflict: int | None
    unresolved_records: int
    conflicted_records: int
    accepted_records: int
    paired_records: int
    prod_a_usable_records: int
    prod_b_usable_records: int
    event_a_dates: int
    event_b_dates: int
    reasons: tuple[str, ...]
    expected_relative_path: str | None = None
    unresolved_items: tuple[HarvestUnresolvedIssue, ...] = ()

    def __post_init__(self):
        if type(self.snapshot_complete) is not bool or type(self.source_resolved) is not bool:
            raise ValueError('Completeness states must be booleans')
        if self.snapshot_complete and not self.source_resolved:
            raise ValueError('Snapshot completeness requires resolved sources')
        if self.source_resolved and not self.snapshot_complete:
            if self.accepted_records == self.paired_records:
                raise ValueError('Resolved sources with all records paired cannot stay incomplete')
        if self.snapshot_complete:
            if self.unresolved_records or self.conflicted_records:
                raise ValueError('Complete snapshot cannot keep unresolved records')
            if self.accepted_records != self.paired_records:
                raise ValueError('Complete snapshot requires all accepted records paired')
        for value in (self.expected_files, self.files_present, self.files_loaded, self.files_failed,
                      self.unresolved_records, self.conflicted_records, self.accepted_records,
                      self.paired_records, self.prod_a_usable_records, self.prod_b_usable_records,
                      self.event_a_dates, self.event_b_dates):
            if type(value) is not int or value < 0:
                raise ValueError('Completeness counters must be nonnegative integers')
        for value in (self.records_read, self.records_accepted, self.records_quarantine,
                      self.records_duplicate, self.records_conflict):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('File counters must be nonnegative integers or NULL')
        for value in (self.prod_a_usable_records, self.prod_b_usable_records, self.paired_records):
            if value > self.accepted_records:
                raise ValueError('Record subsets cannot exceed accepted records')
        tuple_of(self.reasons, str)
        if self.expected_relative_path is not None:
            identifier(self.expected_relative_path)
        tuple_of(self.unresolved_items, HarvestUnresolvedIssue)
        if self.snapshot_complete and 'ingestion_outputs_not_consumable' not in self.reasons and (
                self.files_present < self.expected_files):
            raise ValueError('Complete snapshot needs its expected files present')


@dataclass(frozen=True)
class HarvestQualityCounts:
    """Quality partition of the SELECTED window's real record population per
    measure (service-computed from exactly these records). Deliberately NOT
    the whole-cycle completeness and NOT the frozen unresolved issues: this
    window subset states nothing about other populations and a no-event day
    is never claimed as coverage."""
    population_records: int
    prod_a_accepted: int
    prod_a_missing: int
    prod_a_invalid: int
    prod_a_suspect: int
    prod_b_accepted: int
    prod_b_missing: int
    prod_b_invalid: int
    prod_b_suspect: int

    def __post_init__(self):
        counts = (self.population_records, self.prod_a_accepted, self.prod_a_missing,
                  self.prod_a_invalid, self.prod_a_suspect, self.prod_b_accepted,
                  self.prod_b_missing, self.prod_b_invalid, self.prod_b_suspect)
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError('Quality counts are nonnegative integers')
        for name in ('a', 'b'):
            parts = (getattr(self, f'prod_{name}_accepted'), getattr(self, f'prod_{name}_missing'),
                     getattr(self, f'prod_{name}_invalid'), getattr(self, f'prod_{name}_suspect'))
            if sum(parts) != self.population_records:
                raise ValueError('Quality counts must partition the record population')


@dataclass(frozen=True)
class HarvestSummary:
    """Period harvest summary: observed totals per measure, paired totals and
    weighted shares on the SAME paired records. Navigation cumulatives run
    from `cumulative_origin` (the cycle start, always present) and are
    computed only from records with cycle.start <= harvest_date <
    effective_end: records after the window never contribute an amount or a
    basis label. `cumulative_a` sums accepted ProdA values (canonical BR-04);
    `cumulative_total` sums A+B over PAIRED records ONLY — two INDEPENDENT
    measure sets, each following the no-usable vs verified-empty rules: a
    measure set with no usable contribution is a known 0 under a complete
    snapshot, else NULL (never fabricated). The basis label is drawn solely
    from contributing records. Zero vs unknown follows completeness: an empty
    window in a complete snapshot is a known 0.
    """
    publication: PublicationRef
    context: QueryContext
    window_start: date
    window_end: date
    prod_a_observed: Decimal | None
    prod_b_observed: Decimal | None
    paired_prod_a: Decimal | None
    paired_prod_b: Decimal | None
    paired_total: Decimal | None
    class_a_share_pct: Decimal | None
    class_b_share_pct: Decimal | None
    accepted_records: int
    paired_records: int
    completeness: HarvestCompleteness
    cumulative_a: Decimal | None
    cumulative_total: Decimal | None
    cumulative_origin: date | None
    cumulative_basis_label: str | None
    status: ResultStatus
    unit: str = HARVEST_UNIT
    metric_version: str = HARVEST_METRIC_VERSION
    iso_year: int | None = None
    iso_week: int | None = None
    reason_codes: tuple[str, ...] = ()
    rule_status: RuleStatus = RuleStatus.NOT_CONFIGURED
    rule_version: str | None = None
    rule_profile: HarvestRuleProfile | None = None
    outside_rule_records: int | None = None
    rule_unknown_records: int | None = None
    record_quality: HarvestQualityCounts | None = None

    @property
    def time_basis(self):
        return self.publication.time_basis

    @property
    def versions(self):
        return self.publication.versions

    def __post_init__(self):
        self.context.matches(self.publication)
        calendar_window(self.window_start, self.window_end)
        if not self.context.start <= self.window_start < self.window_end <= self.context.end:
            raise ValueError('Effective window must be inside the requested window')
        for value in (self.accepted_records, self.paired_records):
            if type(value) is not int or value < 0:
                raise ValueError('Record counts must be nonnegative integers')
        if self.paired_records > self.accepted_records:
            raise ValueError('Paired records cannot exceed accepted records')
        for value in (self.prod_a_observed, self.prod_b_observed, self.paired_prod_a,
                      self.paired_prod_b, self.paired_total, self.cumulative_a,
                      self.cumulative_total):
            finite(value, nullable=True, nonnegative=True)
        if self.accepted_records == 0:
            for value in (self.prod_a_observed, self.prod_b_observed):
                if value is not None and value != 0:
                    raise ValueError('An empty window only reports NULL or known zero totals')
        if self.paired_records == 0:
            if self.paired_prod_a is not None or self.paired_prod_b is not None or self.paired_total is not None:
                raise ValueError('No paired records may not claim paired totals')
            if self.class_a_share_pct is not None or self.class_b_share_pct is not None:
                raise ValueError('Shares require a nonzero paired denominator')
        else:
            if self.paired_prod_a is None or self.paired_prod_b is None or self.paired_total is None:
                raise ValueError('Paired records imply paired totals')
            if self.paired_total != exact_add(self.paired_prod_a, self.paired_prod_b):
                raise ValueError('Paired total must equal the paired measure sums')
            if self.paired_total == 0:
                if self.class_a_share_pct is not None or self.class_b_share_pct is not None:
                    raise ValueError('Zero paired mass keeps shares NULL')
            else:
                if self.class_a_share_pct is None or self.class_b_share_pct is None:
                    raise ValueError('A positive paired denominator yields both shares')
                for value in (self.class_a_share_pct, self.class_b_share_pct):
                    finite(value, nonnegative=True)
                    if value > 100:
                        raise ValueError('Share exceeds 100 percent')
        if self.cumulative_basis_label is not None and self.cumulative_a is None:
            raise ValueError('Cumulative basis label requires an amount')
        if self.cumulative_origin is not None and self.window_start < self.cumulative_origin:
            raise ValueError('Cumulative origin must not come after the window')
        if not isinstance(self.status, ResultStatus) or not isinstance(self.completeness, HarvestCompleteness):
            raise ValueError('Invalid summary status/completeness contracts')
        if not isinstance(self.rule_status, RuleStatus):
            raise ValueError('Invalid summary rule status')
        if self.rule_status == RuleStatus.NOT_CONFIGURED:
            if any(value is not None for value in (self.rule_version, self.rule_profile,
                                                   self.outside_rule_records, self.rule_unknown_records)):
                raise ValueError('Rule outcome unavailable without a configured profile')
        elif (self.rule_status not in (RuleStatus.WITHIN_RANGE, RuleStatus.OUTSIDE_RANGE, RuleStatus.UNKNOWN)
              or not isinstance(self.rule_profile, HarvestRuleProfile)
              or self.rule_version != self.rule_profile.rule_version
              or self.rule_version != self.context.rule_version
              or (self.publication.scope_id, self.context.compartment_ids[0]) != (
                  self.rule_profile.scope_id, self.rule_profile.compartment_id)):
            raise ValueError('Summary rule metadata does not match request/profile')
        else:
            self.rule_profile.matches(self.context)
            for count in (self.outside_rule_records, self.rule_unknown_records):
                if type(count) is not int or count < 0:
                    raise ValueError('Invalid rule record count')
            if self.outside_rule_records + self.rule_unknown_records > self.accepted_records:
                raise ValueError('Rule record counters cannot exceed the accepted records')
        if self.record_quality is not None:
            if not isinstance(self.record_quality, HarvestQualityCounts):
                raise ValueError('Record quality counts must be the typed counts')
            if self.record_quality.population_records != self.accepted_records:
                raise ValueError(
                    'Quality counts must match the effective window record population')
        tuple_of(self.reason_codes, str)
        if self.unit != HARVEST_UNIT:
            raise ValueError('Harvest period requires kg/m²')
        if (self.iso_year is None) != (self.iso_week is None):
            raise ValueError('ISO year/week must travel together')


@dataclass(frozen=True)
class HarvestDay:
    """Calendar row for display: day sums of accepted records, or an explicit
    known-zero fill (complete snapshot) / unknown-NULL row (incomplete). A day
    never claims a source row that does not exist."""
    publication: PublicationRef
    cycle_id: str
    compartment_id: str
    harvest_date: date
    prod_a: Decimal | None
    prod_b: Decimal | None
    record_count: int
    record_present: bool
    paired_records: int
    paired_prod_a: Decimal | None
    paired_prod_b: Decimal | None
    class_a_share_pct: Decimal | None
    class_b_share_pct: Decimal | None
    basis_status: BasisStatus
    basis_label: str | None
    reason_codes: tuple[str, ...] = ()
    cumulative_a: Decimal | None = None
    cumulative_total: Decimal | None = None
    cumulative_origin: date | None = None
    rule_status: RuleStatus = RuleStatus.NOT_CONFIGURED
    rule_version: str | None = None
    rule_profile: HarvestRuleProfile | None = None

    def __post_init__(self):
        if not isinstance(self.publication, PublicationRef):
            raise ValueError('Harvest day requires the pinned publication')
        identifier(self.cycle_id); identifier(self.compartment_id)
        if type(self.harvest_date) is not date:
            raise ValueError('Harvest day uses whole source dates')
        for value in (self.prod_a, self.prod_b, self.paired_prod_a, self.paired_prod_b,
                      self.cumulative_a, self.cumulative_total):
            finite(value, nullable=True, nonnegative=True)
        if self.cumulative_origin is not None and self.harvest_date < self.cumulative_origin:
            raise ValueError('Cumulative origin must not come after the day')
        if type(self.record_count) is not int or self.record_count < 0:
            raise ValueError('Day record count must be nonnegative')
        if type(self.record_present) is not bool or self.record_present != (self.record_count > 0):
            raise ValueError('record_present must follow the accepted record count')
        if type(self.paired_records) is not int or not 0 <= self.paired_records <= self.record_count:
            raise ValueError('Paired day records must fit the accepted count')
        if self.paired_records == 0:
            if self.paired_prod_a is not None or self.paired_prod_b is not None:
                raise ValueError('No paired records may not claim paired sums')
            if self.class_a_share_pct is not None or self.class_b_share_pct is not None:
                raise ValueError('Day shares require a positive paired denominator')
        else:
            if self.paired_prod_a is None or self.paired_prod_b is None:
                raise ValueError('Paired records imply paired sums')
            total = exact_add(self.paired_prod_a, self.paired_prod_b)
            if total == 0:
                if self.class_a_share_pct is not None or self.class_b_share_pct is not None:
                    raise ValueError('Zero day mass keeps shares NULL')
            elif self.class_a_share_pct is None or self.class_b_share_pct is None:
                raise ValueError('Positive day mass yields both shares')
        if not isinstance(self.basis_status, BasisStatus):
            raise ValueError('Day basis status must be a contract enum')
        if not isinstance(self.rule_status, RuleStatus):
            raise ValueError('Invalid harvest day rule status')
        if self.rule_status == RuleStatus.NOT_CONFIGURED:
            if self.rule_version is not None or self.rule_profile is not None:
                raise ValueError('Unconfigured day cannot claim a rule')
        elif (self.rule_status not in (RuleStatus.WITHIN_RANGE, RuleStatus.OUTSIDE_RANGE, RuleStatus.UNKNOWN)
              or not isinstance(self.rule_profile, HarvestRuleProfile)
              or self.rule_version != self.rule_profile.rule_version
              or (self.publication.scope_id, self.compartment_id) != (
                  self.rule_profile.scope_id, self.rule_profile.compartment_id)):
            raise ValueError('Day rule metadata does not match its profile')
        if self.basis_status in (BasisStatus.VERIFIED, BasisStatus.ASSUMED) and self.basis_label is None:
            raise ValueError('A known basis needs its visible label')
        if self.basis_status not in (BasisStatus.VERIFIED, BasisStatus.ASSUMED) and self.basis_label is not None:
            raise ValueError('Unknown basis cannot carry a label')
        tuple_of(self.reason_codes, str)


@dataclass(frozen=True)
class LineageResult:
    publication: PublicationRef
    revision_id: str
    record_type: str
    units: tuple[MeasureUnit, ...]
    logical_record_key: str
    cycle_id: str
    compartment_id: str
    revision_number: int
    source_record_id: str
    first_run_id: str
    relative_path: str
    file_checksum: str
    archive_sha256: str
    line_number: int
    line_end: int
    raw_reference: str
    raw_hash: str
    raw_hash_verified: bool
    source_time_serial: Decimal | None
    source_timestamp: datetime
    correction_id: str | None = None
    correction_version: int | None = None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.publication, PublicationRef) or self.record_type not in ('resource', 'harvest', 'climate', 'drain'):
            raise ValueError('Invalid lineage publication/type')
        tuple_of(self.units, MeasureUnit)
        tuple_of(self.reason_codes, str)
        for value in (self.revision_id, self.logical_record_key, self.cycle_id, self.compartment_id, self.source_record_id, self.first_run_id):
            identifier(value)
        for value in (self.file_checksum, self.archive_sha256, self.raw_hash):
            checksum(value)
        if self.revision_number < 1 or self.line_number < 2 or self.line_end < self.line_number:
            raise ValueError('Invalid revision/physical line range')
        finite(self.source_time_serial, nullable=True)
        clock(self.source_timestamp, aware=False)


class EnergyComponent(StrEnum):
    HEAT = 'heat'
    ELEC_HIGH = 'elec_high'
    ELEC_LOW = 'elec_low'


ENERGY_QUANTITY_VERSION = 'energy-quantity-v1'
ENERGY_MEASURES = {EnergyComponent.HEAT: 'Heat_cons', EnergyComponent.ELEC_HIGH: 'ElecHigh',
                   EnergyComponent.ELEC_LOW: 'ElecLow'}


def exact_product(left, right):
    """Finite source quantity x area without ambient Decimal truncation."""
    with localcontext() as ctx:
        ctx.prec = max(50, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 2)
        return left * right


def energy_kwh(value, component):
    """Only the MJ/3.6 normalization rounds: 50 significant digits, half-even."""
    if value is None:
        return None
    if component != EnergyComponent.HEAT:
        return value
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = 'ROUND_HALF_EVEN'
        return value / Decimal('3.6')


@dataclass(frozen=True)
class EffectiveAreaBasis:
    """Publication-frozen, measure-specific effective area and evidence."""
    basis_version_id: str
    version: int
    scope_id: str
    compartment_id: str
    measure_code: str
    area_m2: Decimal
    basis_kind: str
    status: BasisStatus
    valid_from: date
    valid_to_exclusive: date
    evidence_reference: str
    assumption_id: str | None = None
    assumption_version: int | None = None
    assumption_statement: str | None = None
    assumption_evidence: str | None = None
    basis_label: str = 'verified'

    def __post_init__(self):
        for value in (self.basis_version_id, self.scope_id, self.compartment_id,
                      self.measure_code, self.basis_kind, self.evidence_reference):
            identifier(value)
        if type(self.version) is not int or self.version < 1:
            raise ValueError('Invalid effective basis version')
        finite(self.area_m2, nonnegative=True)
        if not self.area_m2:
            raise ValueError('Effective area must be positive')
        calendar_window(self.valid_from, self.valid_to_exclusive)
        if self.status == BasisStatus.ASSUMED:
            for value in (self.assumption_id, self.assumption_statement, self.assumption_evidence):
                identifier(value)
            if type(self.assumption_version) is not int or self.assumption_version < 1:
                raise ValueError('Assumed effective basis requires a version')
        elif self.status != BasisStatus.VERIFIED or any(value is not None for value in (
                self.assumption_id, self.assumption_version, self.assumption_statement, self.assumption_evidence)):
            raise ValueError('Known effective basis needs verified or explicit assumed provenance')
        if self.basis_label != (ASSUMPTION_LABEL if self.status == BasisStatus.ASSUMED else 'verified'):
            raise ValueError('Effective basis requires its visible verification label')

    @property
    def label(self):
        return self.basis_label


@dataclass(frozen=True)
class EnergyQuantity:
    """Same immutable day/component quantity identity for BR-04 and BR-06.

    Source value is MJ/m² or kWh/m² on this source day. Accepted quantity
    zero stays zero; unusable quality stays visible but never contributes.
    """
    publication: PublicationRef
    cycle_id: str
    compartment_id: str
    resource_date: date
    component: EnergyComponent
    source_value: Decimal | None
    source_unit: str
    quality: QualityStatus
    applicability: Applicability
    reason_code: str | None
    revision_id: str | None
    logical_record_key: str | None
    correction: CorrectionRef | None
    basis: EffectiveAreaBasis | None
    normalized_kwh_m2: Decimal | None
    absolute_source_quantity: Decimal | None
    absolute_kwh: Decimal | None
    quantity_version: str = ENERGY_QUANTITY_VERSION

    def __post_init__(self):
        if not isinstance(self.publication, PublicationRef) or not isinstance(self.component, EnergyComponent):
            raise ValueError('Invalid energy publication/component')
        identifier(self.cycle_id); identifier(self.compartment_id)
        if type(self.resource_date) is not date or self.quantity_version != ENERGY_QUANTITY_VERSION:
            raise ValueError('Invalid energy date/quantity version')
        expected_unit = 'MJ/m²/day' if self.component == EnergyComponent.HEAT else 'kWh/m²/day'
        if self.source_unit != expected_unit:
            raise ValueError('Unexpected component source unit')
        if not isinstance(self.quality, QualityStatus) or not isinstance(self.applicability, Applicability):
            raise ValueError('Energy quality/applicability must be contract enums')
        finite(self.source_value, nullable=True)
        if self.quality == QualityStatus.ACCEPTED and self.source_value is None:
            raise ValueError('Accepted energy measure requires a value')
        if self.quality == QualityStatus.MISSING and self.source_value is not None:
            raise ValueError('Missing energy measure must be NULL')
        if not self.accepted:
            identifier(self.reason_code)
        elif self.source_value < 0:
            raise ValueError('Accepted energy cannot be negative')
        if (self.revision_id is None) != (self.logical_record_key is None):
            raise ValueError('Energy revision/key must travel together')
        for value in (self.revision_id, self.logical_record_key):
            if value is not None:
                identifier(value)
        if self.revision_id is None and (self.source_value is not None or self.quality != QualityStatus.MISSING):
            raise ValueError('Absent resource day must remain missing')
        if self.correction is not None and (not isinstance(self.correction, CorrectionRef) or self.revision_id is None):
            raise ValueError('Correction requires a source revision')
        if self.basis is not None:
            if not isinstance(self.basis, EffectiveAreaBasis) or (
                    self.basis.scope_id, self.basis.compartment_id, self.basis.measure_code) != (
                    self.publication.scope_id, self.compartment_id, ENERGY_MEASURES[self.component]):
                raise ValueError('Effective basis is outside the quantity scope/measure')
            if not self.basis.valid_from <= self.resource_date < self.basis.valid_to_exclusive:
                raise ValueError('Effective basis is outside the quantity day')
        expected_source = (exact_product(self.source_value, self.basis.area_m2)
                           if self.accepted and self.basis is not None else None)
        expected_normalized = energy_kwh(self.source_value, self.component) if self.accepted else None
        for value in (self.normalized_kwh_m2, self.absolute_source_quantity, self.absolute_kwh):
            finite(value, nullable=True, nonnegative=True)
        if (self.normalized_kwh_m2, self.absolute_source_quantity, self.absolute_kwh) != (
                expected_normalized, expected_source, energy_kwh(expected_source, self.component)):
            raise ValueError('Energy quantities differ from source/area/normalization contract')

    @property
    def accepted(self):
        return self.quality == QualityStatus.ACCEPTED and self.applicability == Applicability.APPLICABLE

    @property
    def quantity_id(self):
        return (self.publication.publication_id, self.cycle_id, self.compartment_id,
                self.resource_date, self.component, self.revision_id, self.quantity_version)


@dataclass(frozen=True)
class EnergyBasisGate:
    absolute_compatible: bool
    per_m2_compatible: bool
    status: BasisStatus
    label: str | None
    reason_codes: tuple[str, ...]

    def __post_init__(self):
        if type(self.absolute_compatible) is not bool or type(self.per_m2_compatible) is not bool:
            raise ValueError('Basis compatibility flags must be booleans')
        if not isinstance(self.status, BasisStatus) or (self.per_m2_compatible and not self.absolute_compatible):
            raise ValueError('Invalid basis gate')
        expected = ASSUMPTION_LABEL if self.status == BasisStatus.ASSUMED else 'verified' if self.status == BasisStatus.VERIFIED else None
        if self.label != expected:
            raise ValueError('Basis gate label must expose its status')
        tuple_of(self.reason_codes, str)


@dataclass(frozen=True)
class EnergyBasisTotal:
    basis: EffectiveAreaBasis | None
    accepted_days: int
    source_quantity_per_m2: Decimal | None
    normalized_kwh_m2: Decimal | None
    absolute_source_quantity: Decimal | None
    absolute_kwh: Decimal | None

    def __post_init__(self):
        if self.basis is not None and not isinstance(self.basis, EffectiveAreaBasis):
            raise ValueError('Invalid grouped energy basis')
        if type(self.accepted_days) is not int or self.accepted_days < 0:
            raise ValueError('Invalid grouped accepted day count')
        for value in (self.source_quantity_per_m2, self.normalized_kwh_m2,
                      self.absolute_source_quantity, self.absolute_kwh):
            finite(value, nullable=True, nonnegative=True)


@dataclass(frozen=True)
class EnergyComponentTotal:
    component: EnergyComponent
    expected_days: int
    accepted_days: int
    absolute_days: int
    coverage_pct: Decimal
    observed_source_per_m2: Decimal | None
    observed_kwh_m2: Decimal | None
    observed_absolute_source: Decimal | None
    observed_absolute_kwh: Decimal | None
    official_absolute_kwh: Decimal | None
    basis_totals: tuple[EnergyBasisTotal, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.component, EnergyComponent) or any(type(n) is not int for n in (
                self.expected_days, self.accepted_days, self.absolute_days)):
            raise ValueError('Invalid energy component/counts')
        if not 0 <= self.absolute_days <= self.accepted_days <= self.expected_days or self.expected_days < 1:
            raise ValueError('Energy day counts are inconsistent')
        finite(self.coverage_pct, nonnegative=True)
        if self.coverage_pct > 100:
            raise ValueError('Energy coverage exceeds 100%')
        for value in (self.observed_source_per_m2, self.observed_kwh_m2, self.observed_absolute_source,
                      self.observed_absolute_kwh, self.official_absolute_kwh):
            finite(value, nullable=True, nonnegative=True)
        if self.official_absolute_kwh is not None and self.absolute_days != self.expected_days:
            raise ValueError('Official component quantity requires complete absolute days')
        tuple_of(self.basis_totals, EnergyBasisTotal); tuple_of(self.reason_codes, str)


@dataclass(frozen=True)
class EnergySummary:
    publication: PublicationRef
    context: QueryContext
    window_start: date
    window_end: date
    expected_days: int
    complete_days: int
    complete_coverage_pct: Decimal
    incomplete_dates: tuple[date, ...]
    components: tuple[EnergyComponentTotal, ...]
    observed_total_kwh: Decimal | None
    complete_day_total_kwh: Decimal | None
    official_total_kwh: Decimal | None
    official_total_kwh_m2: Decimal | None
    basis_gate: EnergyBasisGate
    reason_codes: tuple[str, ...]
    quantity_version: str = ENERGY_QUANTITY_VERSION

    def __post_init__(self):
        if not isinstance(self.publication, PublicationRef) or not isinstance(self.context, QueryContext):
            raise ValueError('Invalid energy summary pin')
        self.context.matches(self.publication); calendar_window(self.window_start, self.window_end)
        if self.expected_days != (self.window_end-self.window_start).days or not 0 <= self.complete_days <= self.expected_days:
            raise ValueError('Invalid energy summary day counts')
        tuple_of(self.components, EnergyComponentTotal); tuple_of(self.incomplete_dates, date)
        if tuple(item.component for item in self.components) != tuple(EnergyComponent):
            raise ValueError('Energy summary needs each independent component exactly once')
        if len(self.incomplete_dates) != self.expected_days-self.complete_days:
            raise ValueError('Incomplete energy dates do not match coverage')
        if not isinstance(self.basis_gate, EnergyBasisGate) or self.quantity_version != ENERGY_QUANTITY_VERSION:
            raise ValueError('Invalid shared quantity/basis version')
        for value in (self.observed_total_kwh, self.complete_day_total_kwh,
                      self.official_total_kwh, self.official_total_kwh_m2):
            finite(value, nullable=True, nonnegative=True)
        finite(self.complete_coverage_pct, nonnegative=True)
        if self.official_total_kwh is not None and (self.complete_days != self.expected_days or not self.basis_gate.absolute_compatible):
            raise ValueError('Official total needs full quantity coverage and compatible basis')
        if self.official_total_kwh_m2 is not None and (self.complete_days != self.expected_days or not self.basis_gate.per_m2_compatible):
            raise ValueError('Official per-area total needs a common fixed basis')
        tuple_of(self.reason_codes, str)


@dataclass(frozen=True)
class ScenarioChange:
    component: EnergyComponent
    price_unit: str
    price: Decimal | None = None
    change_percent: Decimal | None = None

    def __post_init__(self):
        if not isinstance(self.component, EnergyComponent):
            raise ValueError('Unknown energy component')
        expected = 'EUR/MJ' if self.component == EnergyComponent.HEAT else 'EUR/kWh'
        if self.price_unit != expected or (self.price is None) == (self.change_percent is None):
            raise ValueError('Specify exactly one price or percent change with source price unit')
        finite(self.price, nullable=True, nonnegative=True)
        finite(self.change_percent, nullable=True)
        if self.change_percent is not None and self.change_percent < -100:
            raise ValueError('Percent change would create a negative rate')


@dataclass(frozen=True)
class ScenarioCommand:
    request_id: str
    baseline_version_id: str
    name: str
    publication: PublicationRef
    changes: tuple[ScenarioChange, ...]

    def __post_init__(self):
        for value in (self.request_id, self.baseline_version_id, self.name):
            identifier(value)
        if not isinstance(self.publication, PublicationRef):
            raise ValueError('Scenario requires a pinned publication')
        tuple_of(self.changes, ScenarioChange)
        if not self.changes or len({c.component for c in self.changes}) != len(self.changes):
            raise ValueError('Scenario changes must contain distinct components')


@dataclass(frozen=True)
class SourceIdentity:
    snapshot_id: str
    source_record_id: str
    relative_path: str
    line_number: int
    line_end: int
    raw_hash: str

    def __post_init__(self):
        identifier(self.snapshot_id); identifier(self.source_record_id)
        path = PurePosixPath(self.relative_path)
        if not self.relative_path or path.is_absolute() or '..' in path.parts or '\\' in self.relative_path:
            raise ValueError('Invalid relative source path')
        if type(self.line_number) is not int or type(self.line_end) is not int or self.line_number < 2 or self.line_end < self.line_number:
            raise ValueError('Invalid source line range')
        checksum(self.raw_hash)


@dataclass(frozen=True)
class SinkValue:
    measure_code: str
    value: Decimal | None
    unit: str

    def __post_init__(self):
        identifier(self.measure_code); identifier(self.unit)
        finite(self.value, nullable=True)


@dataclass(frozen=True)
class SinkRecord:
    source: SourceIdentity
    observed_at: datetime
    values: tuple[SinkValue, ...]
    event_id: str | None = None
    published_at: datetime | None = None
    source_time_serial: Decimal | None = None

    def __post_init__(self):
        if not isinstance(self.source, SourceIdentity):
            raise ValueError('Sink record requires stable source identity')
        tuple_of(self.values, SinkValue)
        clock(self.observed_at, aware=False)
        finite(self.source_time_serial, nullable=True)
        if self.event_id is not None:
            identifier(self.event_id)
        if self.published_at is not None:
            clock(self.published_at, aware=True)
        if not self.values or len({v.measure_code for v in self.values}) != len(self.values):
            raise ValueError('Sink record requires distinct measure codes')


"""Shared immutable rule schedules and the pure threshold duration engine
(BR-01 climate and BR-03 drain nutrition; T14). Design points:

* One immutable revision MAY carry segments for several metrics; inside one
  revision the same (metric, measurement_context, basis) key may not overlap
  in time, but a different explicitly selected revision may cover the same
  historical interval (update rule version => new content key).
* Segments distinguish tolerance DELTA modes (temperature/CO2 bounds that
  derive per slot from setpoints) from fixed lower/upper bounds (EC/pH);
  `delta` and `lower/upper` never mix.
* Durations evaluate on the 5-minute slot grid (context cadence), with
  caller-declared requested metrics kept explicit: a metric without any
  applicable segment stays `rule_not_configured` with its expected duration
  and NULL public out KPI — never silently omitted.
* Episodes build on the full cycle before query clipping, break on
  in-range/unknown/gap/segment change, and keep original plus clipped facts.
"""
RULE_SCHEDULE_METRICS = ('temperature', 'co2', 'ec', 'ph')
RULE_METRIC_UNITS = {'temperature': '°C', 'co2': 'ppm', 'ec': 'dS/m', 'ph': 'pH'}
UNKNOWN_REASON_PRIORITY = (
    'rule_not_configured', 'missing_input', 'quality_invalid', 'quality_suspect',
    'sp0_off_mode', 'inverted_bounds')


class RuleBasis(StrEnum):
    """Which side of the metric a segment rules: climate thresholds apply to
    a setpoint basis (requested/realized); nutrition thresholds measure the
    drain measure itself."""
    REQUESTED = 'requested'
    REALIZED = 'realized'
    MEASURED = 'measured'


class RuleDeviationDirection(StrEnum):
    LOW = 'low'
    HIGH = 'high'


def _naive(dt):
    if not isinstance(dt, datetime) or dt.tzinfo is not None:
        raise ValueError('Rule schedule time must be a naive source timestamp')
    return dt


@dataclass(frozen=True)
class RuleScheduleSegment:
    """[start, end) source-naive interval for ONE metric within a revision.

    Temperature/CO2 segments carry `delta` (bounds are computed per slot
    from the slot's own setpoints, so stored deltas never overload fixed
    bounds); EC/pH segments carry fixed `lower/upper`. Context+basis stay
    explicit; equality on segment boundaries sits inside a segment."""
    metric: str
    measurement_context: str
    basis: RuleBasis
    start: datetime
    end: datetime
    delta: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None

    def __post_init__(self):
        if self.metric not in RULE_SCHEDULE_METRICS:
            raise ValueError('Rule metric must be one of ' + str(RULE_SCHEDULE_METRICS))
        identifier(self.measurement_context)
        _naive(self.start); _naive(self.end)
        if self.start >= self.end:
            raise ValueError('Segment interval must stay half-open and nonempty')
        if self.unit != RULE_METRIC_UNITS[self.metric]:
            raise ValueError('Rule segment unit must match the metric unit')
        if self.basis is None or not isinstance(self.basis, RuleBasis):
            raise ValueError('Rule segment basis must be an explicit RuleBasis')
        if self.metric in ('temperature', 'co2'):
            if self.basis == RuleBasis.MEASURED:
                raise ValueError('Climate thresholds rule a requested/realized basis, not measured')
            if self.delta is None or self.lower is not None or self.upper is not None:
                raise ValueError('Temperature/CO2 segments carry delta and no fixed bounds')
            finite(self.delta, nonnegative=True)
        else:
            if self.basis != RuleBasis.MEASURED:
                raise ValueError('EC/pH thresholds rule the measured basis')
            if self.lower is None or self.upper is None or self.delta is not None:
                raise ValueError('EC/pH segments carry fixed bounds and no delta')
            finite(self.lower, nonnegative=self.metric == 'ec')
            finite(self.upper, nonnegative=True)
            if self.lower > self.upper:
                raise ValueError('Fixed bounds must be ordered (parity check; equality inside)')

    @property
    def unit(self):
        return RULE_METRIC_UNITS[self.metric]

    @property
    def content_id(self):
        return (self.metric, self.measurement_context, self.basis.value, self.start,
                self.end, self.delta, self.lower, self.upper)


def rule_segment_sort_key(segment: RuleScheduleSegment):
    return (segment.start, segment.metric, segment.measurement_context,
            segment.basis.value, segment.end, segment.delta, segment.lower, segment.upper)


@dataclass(frozen=True)
class RuleScheduleRevision:
    """One immutable user-visible schedule version. Content checks happen in
    the contract (ordering, one-key non-overlap) AND at the database level
    (overlap rejection), so repository bypasses stay impossible."""
    rule_version: str
    rule_id: str
    version: int
    scope_id: str
    compartment_id: str
    evidence_reference: str
    is_demo: bool
    label: str
    segments: tuple[RuleScheduleSegment, ...]

    def __post_init__(self):
        for value in (self.rule_version, self.rule_id, self.scope_id, self.compartment_id,
                      self.evidence_reference, self.label):
            identifier(value)
        if type(self.version) is not int or self.version < 1 or type(self.is_demo) is not bool:
            raise ValueError('Invalid schedule revision version/demo flag')
        if not self.segments or not isinstance(self.segments, tuple):
            raise ValueError('A schedule revision needs at least one segment')
        tuple_of(self.segments, RuleScheduleSegment)
        order = [rule_segment_sort_key(segment) for segment in self.segments]
        if order != sorted(order):
            raise ValueError('Revision segments must be canonically sorted')
        # Same (metric, context, basis) key must never overlap — validated
        # against the LAST end seen for that key, not merely the globally
        # adjacent segment: temperature/co2/temperature interleavings would
        # otherwise hide the temperature overlap.
        last_end = {}
        for segment in self.segments:
            key = (segment.metric, segment.measurement_context, segment.basis.value)
            previous_end = last_end.get(key)
            if previous_end is not None and segment.start < previous_end:
                raise ValueError('Schedule segments overlap for the same metric/context/basis')
            last_end[key] = segment.end if previous_end is None else max(previous_end, segment.end)
        if self.is_demo and self.label != 'demo_rule':
            raise ValueError('Demo schedule revision must carry the demo_label')

    def matches(self, scope_id, compartment_ids):
        if (self.scope_id,) != (scope_id,) or self.compartment_id != compartment_ids[0]:
            raise ValueError('Schedule revision is outside the requested scope/compartment')


@dataclass(frozen=True)
class IntradayContext:
    """Source-naive intraday evaluation context for threshold durations.

    The DECLARED evaluation group (measurement_context + basis) is shared by
    every requested metric of one evaluation: BR-01 climate runs declare
    climate/requested or climate/realized; BR-03 drains declare drain/
    measured. Slots and selected segments must match this group; other
    contexts/bases are separate evaluations — a mixed-context OR (say
    temperature+EC) is not constructed.KEPT separate from the day-grain
    QueryContext: T15/T16 wrap it with the pinned publication/versions."""
    scope_id: str
    cycle: CycleWindow
    compartment_ids: tuple[str, ...]
    start: datetime
    end: datetime
    measurement_context: str
    basis: RuleBasis
    cadence_minutes: int = 5
    time_basis: TimeBasis = TimeBasis.SOURCE_NAIVE

    def __post_init__(self):
        identifier(self.scope_id)
        identifier(self.measurement_context)
        if not isinstance(self.basis, RuleBasis):
            raise ValueError('Declared evaluation basis must be an explicit RuleBasis')
        if not isinstance(self.cycle, CycleWindow):
            raise ValueError('Intraday context pins a CycleWindow contract')
        if not isinstance(self.compartment_ids, tuple) or len(self.compartment_ids) != 1:
            raise ValueError('One cycle/compartment per intraday context')
        if self.compartment_ids[0] != self.cycle.compartment_id:
            raise ValueError('Intraday compartment must match the pinned cycle')
        if self.time_basis != TimeBasis.SOURCE_NAIVE:
            raise ValueError('Threshold durations require the source_naive time basis')
        if type(self.cadence_minutes) is not int or self.cadence_minutes <= 0:
            raise ValueError('Intraday cadence must be a positive minute count')
        _naive(self.start); _naive(self.end)
        cycle_start = datetime.combine(self.cycle.start, midnight())
        cycle_end = datetime.combine(self.cycle.end, midnight())
        if not (cycle_start <= self.start < self.end <= cycle_end):
            raise ValueError('Requested intraday window must sit inside the cycle window')

    @property
    def effective_start(self):
        return self.start

    @property
    def cadence(self):
        return timedelta(minutes=self.cadence_minutes)


def midnight():
    return datetime.min.time()


@dataclass(frozen=True)
class ThresholdSlot:
    """One cadence slot of typed metric inputs ([start, start+cadence)).

    `quality` states THIS metric's own verdict — a suspect measure never
    takes another metric out of the jointly valid set. Climate metrics may
    carry setpoints (CO2 uses `setpoint`; temperature needs BOTH the
    heating `lower_setpoint` and ventilation `upper_setpoint`); nutrition
    metrics only carry the measured `actual`."""
    start: datetime
    metric: str
    measurement_context: str
    basis: RuleBasis
    actual: Decimal | None = None
    setpoint: Decimal | None = None
    lower_setpoint: Decimal | None = None
    upper_setpoint: Decimal | None = None
    quality: QualityStatus = QualityStatus.ACCEPTED

    def __post_init__(self):
        if self.metric not in RULE_SCHEDULE_METRICS:
            raise ValueError('Slot metric must be one of ' + str(RULE_SCHEDULE_METRICS))
        identifier(self.measurement_context)
        if not isinstance(self.basis, RuleBasis):
            raise ValueError('Slot basis must be an explicit RuleBasis')
        _naive(self.start)
        if self.quality == QualityStatus.MISSING:
            if self.actual is not None:
                raise ValueError('A missing measure cannot claim an actual value')
        elif self.metric in ('ec', 'ph'):
            finite(self.actual, nullable=True, nonnegative=self.metric == 'ec')
            if (self.setpoint is not None or self.lower_setpoint is not None
                    or self.upper_setpoint is not None):
                raise ValueError('Nutrition slots carry no setpoints')
        else:
            finite(self.setpoint, nullable=True)
            finite(self.actual, nullable=True)
            finite(self.lower_setpoint, nullable=True)
            finite(self.upper_setpoint, nullable=True)
            finite(self.setpoint, nullable=True)


@dataclass(frozen=True)
class UnknownReasonShare:
    """One unknown-duration reason with its total minutes (sums are exact)."""
    reason_code: str
    minutes: Decimal

    def __post_init__(self):
        identifier(self.reason_code)
        finite(self.minutes, nonnegative=True)


@dataclass(frozen=True)
class RuleMetricDuration:
    """Per-metric exact durations over the effective window. Public out
    KPIs turn NULL exactly when no slot was valid (kept also as Decimal
    zero only internally); unknown minutes partition by one reason."""
    metric: str
    measurement_context: str
    basis: RuleBasis
    rule_version: str | None
    expected_minutes: Decimal
    valid_minutes: Decimal
    out_minutes: Decimal
    out_low_minutes: Decimal
    out_high_minutes: Decimal
    suspect_minutes: Decimal
    unknown_minutes: Decimal
    unknown_reasons: tuple[UnknownReasonShare, ...]
    out_hours: Decimal | None
    out_percent: Decimal | None
    coverage_pct: Decimal

    def __post_init__(self):
        finite(self.out_hours, nullable=True, nonnegative=True)
        finite(self.out_percent, nullable=True, nonnegative=True)
        finite(self.coverage_pct, nonnegative=True)
        if self.coverage_pct > 100:
            raise ValueError('Coverage percentage must stay within 100')

    @property
    def unit(self):
        return RULE_METRIC_UNITS[self.metric]


@dataclass(frozen=True)
class RuleMetricApplied:
    """Contributing rule identity for one requested metric (None entries
    mark requested-but-not-applied metrics; never silently dropped)."""
    metric: str
    measurement_context: str
    basis: RuleBasis
    rule_version: str | None
    unit: str

    def __post_init__(self):
        if self.metric not in RULE_SCHEDULE_METRICS:
            raise ValueError('Metric must be one of ' + str(RULE_SCHEDULE_METRICS))
        identifier(self.measurement_context)
        if not isinstance(self.basis, RuleBasis):
            raise ValueError('Applied basis must be an explicit RuleBasis')
        if self.rule_version is not None:
            identifier(self.rule_version)
        if self.unit != RULE_METRIC_UNITS[self.metric]:
            raise ValueError('Applied unit must match the metric unit')


@dataclass(frozen=True)
class RuleJointDuration:
    """'Metric A or B out' over jointly valid slots ONLY: the union of
    violations inside that intersection is summed; separate per-metric
    hours are never added."""
    metrics: tuple[str, ...]
    measurement_context: str
    basis: RuleBasis
    contributions: tuple[RuleMetricApplied, ...]
    jointly_valid_minutes: Decimal
    joint_out_minutes: Decimal
    joint_out_percent: Decimal | None
    joint_coverage_pct: Decimal

    def __post_init__(self):
        if len(set(self.metrics)) != len(self.metrics) or len(self.metrics) < 2:
            raise ValueError('Joint report needs at least two distinct metrics')
        identifier(self.measurement_context)
        if not isinstance(self.basis, RuleBasis):
            raise ValueError('Joint basis must be an explicit RuleBasis')
        for contribution in self.contributions:
            if contribution.metric not in self.metrics:
                raise ValueError('Joint contribution references an unrequested metric')
            if contribution.basis != self.basis:
                raise ValueError('Joint contributions must share the pinned basis')
            if contribution.measurement_context != self.measurement_context:
                raise ValueError('Joint contributions must share the measurement context')


@dataclass(frozen=True)
class RuleSlotDeviation:
    """Per-slot deviation inside an episode: direction, exact value distance
    to the violated bound, and the slot time (deterministic peak source)."""
    start: datetime
    direction: RuleDeviationDirection
    deviation: Decimal

    def __post_init__(self):
        if not isinstance(self.direction, RuleDeviationDirection):
            raise ValueError('Slot deviation needs an explicit direction')
        if self.deviation is not None:
            finite(self.deviation, nonnegative=True)


@dataclass(frozen=True)
class RuleEpisode:
    """Continuous out run built on the FULL cycle (never clipped frames
    first), then intersected with the requested window. Original start/end
    and duration stay immutable; the clipped overlap reports exactly the
    requested view; both directions inside one run belong to this run."""
    metric: str
    measurement_context: str
    basis: RuleBasis
    rule_version: str
    slots: tuple[RuleSlotDeviation, ...]
    original_start: datetime
    original_end: datetime
    original_minutes: Decimal
    clipped_start: datetime | None
    clipped_end: datetime | None
    clipped_minutes: Decimal | None
    directions: tuple[RuleDeviationDirection, ...]
    peak_deviation: Decimal
    peak_time: datetime

    def __post_init__(self):
        if self.metric not in RULE_SCHEDULE_METRICS:
            raise ValueError('Episode metric must be one of ' + str(RULE_SCHEDULE_METRICS))
        identifier(self.measurement_context)
        if not isinstance(self.basis, RuleBasis):
            raise ValueError('Episode basis must be an explicit RuleBasis')
        identifier(self.rule_version)
        if not isinstance(self.slots, tuple) or not self.slots:
            raise ValueError('An episode carries at least one violating slot')
        tuple_of(self.slots, RuleSlotDeviation)
        for value in (self.original_start, self.original_end, self.peak_time):
            _naive(value)
        if self.original_start < self.original_end:
            finite(self.original_minutes, nonnegative=True)
        # Clipped facts may be absent (zero overlap) exactly when flagged so.
        clipped_states = (self.clipped_start is None, self.clipped_end is None,
                          self.clipped_minutes is None)
        if len(set(clipped_states)) > 1:
            raise ValueError('Clipped facts must be set together or not at all')
        if self.clipped_start is not None:
            _naive(self.clipped_start); _naive(self.clipped_end)
            finite(self.clipped_minutes, nonnegative=True)
        tuple_of(self.directions, RuleDeviationDirection)
        if len(set(self.directions)) != len(self.directions):
            raise ValueError('Episode directions are a deterministic set')
        finite(self.peak_deviation, nonnegative=True)
        if self.peak_deviation != max(slot.deviation for slot in self.slots):
            raise ValueError('Episode peak deviation must match the slot values')


@dataclass(frozen=True)
class RuleDurationResult:
    """Aggregated threshold evaluation. Identity metadata (publication,
    versions) is pinned by the caller when available; the engine never
    invents it."""
    scope_id: str
    cycle_id: str
    compartment_id: str
    requested_start: datetime
    requested_end: datetime
    effective_start: datetime
    effective_end: datetime
    requested_metrics: tuple[str, ...]
    applications: tuple[RuleMetricApplied, ...]
    metric_results: tuple[RuleMetricDuration, ...]
    joint_results: tuple[RuleJointDuration, ...]
    episodes: tuple[RuleEpisode, ...]
    publication: PublicationRef | None = None
    versions: Versions | None = None
    time_basis: TimeBasis = TimeBasis.SOURCE_NAIVE

    def __post_init__(self):
        for value in (self.scope_id, self.cycle_id, self.compartment_id):
            identifier(value)
        for value in (self.requested_start, self.requested_end,
                      self.effective_start, self.effective_end):
            _naive(value)
        if not self.requested_metrics or len(set(self.requested_metrics)) != len(self.requested_metrics):
            raise ValueError('Requested metrics must be explicit, nonempty and distinct')
        if self.publication is not None and not isinstance(self.publication, PublicationRef):
            raise ValueError('Result publication must be a PublicationRef contract when set')
        if self.versions is not None and not isinstance(self.versions, Versions):
            raise ValueError('Result versions must be a Versions contract when set')
        tuple_of(self.applications, RuleMetricApplied)
        tuple_of(self.metric_results, RuleMetricDuration)
        tuple_of(self.joint_results, RuleJointDuration)
        tuple_of(self.episodes, RuleEpisode)
        if len(self.applications) != len(self.requested_metrics):
            raise ValueError('Every requested metric needs an explicit application entry')
        if tuple((applied.metric for applied in self.applications)) != tuple(self.requested_metrics):
            raise ValueError('Application list must follow the requested metrics')
        if any(applied.metric not in self.requested_metrics for applied in self.applications):
            raise ValueError('Application list must follow the requested metrics')
        if self.time_basis != TimeBasis.SOURCE_NAIVE:
            raise ValueError('Threshold result requires the source_naive basis')


RULE_SLOT_CLASSES = (
    'valid', 'out_low', 'out_high', 'rule_not_configured', 'missing_input',
    'quality_invalid', 'quality_suspect', 'sp0_off_mode', 'inverted_bounds')


@dataclass(frozen=True)
class RuleSlotReview:
    """Per-slot trace for one declared-group evaluation: actual, applied
    bounds, quality verdict and exact classification from the SAME engine
    call path (no duplicated formulas in UI/SQL). Climate metrics expose the
    setpoint they were judged against via `lower/upper` (requested basis:
    heating−delta/ventilation+delta or derived CO2 setpoints; nutrition:
    fixed segment bounds); unknown classes carry None bounds by semantics,
    not a fabricated threshold."""
    start: datetime
    metric: str
    measurement_context: str
    basis: RuleBasis
    rule_version: str | None
    actual: Decimal | None
    lower: Decimal | None
    upper: Decimal | None
    quality: QualityStatus | None
    class_code: str
    deviation: Decimal
    direction: RuleDeviationDirection | None
    minutes: Decimal
    # Optional source facts (T15 export preflight): the ORIGINAL frozen
    # selected setpoint values and the frozen per-measure quality note of
    # THIS slot/metric, independent of evaluation-input masking; None when
    # the provider cannot supply them (defaults keep old callers intact).
    source_lower: Decimal | None = None
    source_upper: Decimal | None = None
    source_setpoint: Decimal | None = None
    source_quality: str | None = None

    def __post_init__(self):
        if self.metric not in RULE_SCHEDULE_METRICS:
            raise ValueError('Review metric must be one of ' + str(RULE_SCHEDULE_METRICS))
        identifier(self.measurement_context)
        if not isinstance(self.basis, RuleBasis):
            raise ValueError('Review basis must be an explicit RuleBasis')
        if self.rule_version is not None:
            identifier(self.rule_version)
        # Actual/bounds may be signed finite (cold setpoints/temps, negative
        # raw EC later stay truthful); only deviation/minutes are counts.
        for value in (self.actual, self.lower, self.upper, self.source_lower,
                      self.source_upper, self.source_setpoint):
            finite(value, nullable=True)
        if self.source_quality is not None and not isinstance(self.source_quality, str):
            raise ValueError('Review source_quality must be a str note')
        if self.class_code not in RULE_SLOT_CLASSES:
            raise ValueError('Review class must be one of ' + str(RULE_SLOT_CLASSES))
        if (self.direction is None) != (self.class_code not in ('out_low', 'out_high')):
            raise ValueError('Review direction must be present exactly for out classes')
        if self.class_code in ('out_low', 'out_high') and not self.deviation:
            raise ValueError('Out-of-range review rows carry the exact deviation')
        if self.quality is not None and not isinstance(self.quality, QualityStatus):
            raise ValueError('Review quality must be a QualityStatus contract')

    @property
    def unit(self):
        return RULE_METRIC_UNITS[self.metric]

    @property
    def unknown_reason(self):
        return self.class_code if self.class_code not in ('valid', 'out_low', 'out_high') else None


@dataclass(frozen=True)
class ClimateEvaluation:
    """One BR-01 evaluation over the pinned publication/mart: typed inputs
    and the T14 oracle result plus per-slot traces. The declared basis stays
    requested/realized (climate); draining is handled separately (T16/T17).

    An explicitly selected schedule revision may cover only PART of the
    request: uncovered metrics stay `rule_not_configured` with joint valid 0
    (approved T14 contract) — a partial selection is not an error, but a
    selection that applies NO revision to any metric never carries versions."""
    publication: PublicationRef
    context: QueryContext
    basis: RuleBasis
    requested_rule_version: str | None
    result: RuleDurationResult
    slot_reviews: tuple[RuleSlotReview, ...]
    # Optional provenance of the SELECTED revision, verbatim from the
    # registered rule (never inferred from the rule_version string; None with
    # defaults keeps no-rule selections and old construction sites intact).
    rule_id: str | None = None
    rule_label: str | None = None
    rule_is_demo: bool | None = None
    rule_evidence_reference: str | None = None

    def __post_init__(self):
        if not isinstance(self.publication, PublicationRef):
            raise ValueError('Climate evaluation pins a PublicationRef')
        if not isinstance(self.context, QueryContext):
            raise ValueError('Climate evaluation pins a QueryContext')
        if self.publication.publication_id != self.context.publication_id:
            raise ValueError('Evaluation publication and context must share the pinned ID')
        if self.basis not in (RuleBasis.REQUESTED, RuleBasis.REALIZED):
            raise ValueError('BR-01 evaluates the requested or the realized basis')
        if not isinstance(self.result, RuleDurationResult):
            raise ValueError('Climate evaluation carries the typed engine result')
        if self.rule_id is not None:
            identifier(self.rule_id)
        if self.rule_label is not None:
            identifier(self.rule_label)
        if self.rule_is_demo is not None and not isinstance(self.rule_is_demo, bool):
            raise ValueError('Climate evaluation rule_is_demo must be bool or None')
        if self.rule_evidence_reference is not None \
                and not isinstance(self.rule_evidence_reference, str):
            raise ValueError('Climate evaluation rule evidence must be str or None')
        if self.requested_rule_version is None \
                and any(applied.rule_version is not None for applied in self.result.applications):
            raise ValueError('No-rule selection may not carry applied rule versions')
        tuple_of(self.slot_reviews, RuleSlotReview)
        if self.slot_reviews:
            # Bounded detail: whichever reviews a caller decides to carry
            # (none for the summary step) must agree with THIS evaluation's
            # declared identity — never a foreign window trace.
            unknown = any(review.metric not in self.result.requested_metrics
                          or review.basis != self.basis
                          or review.measurement_context
                          != self.result.metric_results[0].measurement_context
                          for review in self.slot_reviews)
            if unknown:
                raise ValueError('Slot reviews must carry this evaluation metrics/basis')

    @property
    def requested_context(self):
        return self.context

    @property
    def compartment_id(self):
        return self.context.compartment_ids[0]


@dataclass(frozen=True)
class SinkBatch:
    request_id: str
    batch_id: str
    run_id: str
    scope_id: str
    snapshot_id: str
    parser_version: str
    received_at: datetime
    records: tuple[SinkRecord, ...]

    def __post_init__(self):
        for value in (self.request_id, self.batch_id, self.run_id, self.scope_id, self.snapshot_id, self.parser_version):
            identifier(value)
        clock(self.received_at, aware=True)
        tuple_of(self.records, SinkRecord)
        if any(r.source.snapshot_id != self.snapshot_id for r in self.records):
            raise ValueError('Sink batch cannot mix raw snapshots')


@dataclass(frozen=True)
class DrainEvaluation:
    """One BR-03 evaluation over the pinned publication/mart: typed EC/pH
    inputs (fixed-bounds measured basis) and the T14 oracle result plus
    bounded per-slot traces. measurement_context stays the exact core
    identity 'drain_process_computer'; the declared basis is ALWAYS
    RuleBasis.MEASURED (EC/pH have no requested/realized duality)."""
    publication: PublicationRef
    context: QueryContext
    basis: RuleBasis
    requested_rule_version: str | None
    result: RuleDurationResult
    slot_reviews: tuple[RuleSlotReview, ...]
    # Optional provenance of the SELECTED revision, verbatim from the
    # registered rule (never inferred from the rule_version string).
    rule_id: str | None = None
    rule_label: str | None = None
    rule_is_demo: bool | None = None
    rule_evidence_reference: str | None = None

    def __post_init__(self):
        if not isinstance(self.publication, PublicationRef):
            raise ValueError('Drain evaluation pins a PublicationRef')
        if not isinstance(self.context, QueryContext):
            raise ValueError('Drain evaluation pins a QueryContext')
        if self.publication.publication_id != self.context.publication_id:
            raise ValueError('Evaluation publication and context must share the pinned ID')
        if self.basis is not RuleBasis.MEASURED:
            raise ValueError('BR-03 evaluates the measured basis only')
        if not isinstance(self.result, RuleDurationResult):
            raise ValueError('Drain evaluation carries the typed engine result')
        if any(result.measurement_context != 'drain_process_computer'
               for result in self.result.metric_results):
            raise ValueError('Drain evaluation keeps the exact drain_process_computer context')
        if self.rule_id is not None:
            identifier(self.rule_id)
        if self.rule_label is not None:
            identifier(self.rule_label)
        if self.rule_is_demo is not None and not isinstance(self.rule_is_demo, bool):
            raise ValueError('Drain evaluation rule_is_demo must be bool or None')
        if self.rule_evidence_reference is not None \
                and not isinstance(self.rule_evidence_reference, str):
            raise ValueError('Drain evaluation rule evidence must be str or None')
        if self.requested_rule_version is None \
                and any(applied.rule_version is not None for applied in self.result.applications):
            raise ValueError('No-rule selection may not carry applied rule versions')
        tuple_of(self.slot_reviews, RuleSlotReview)
