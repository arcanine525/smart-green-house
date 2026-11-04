"""Engine-independent value contracts. Decimal and clock semantics are explicit."""
from dataclasses import dataclass
from datetime import date, datetime
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

    usable reports only that the sealed irrigation mart bundle exists; it is a
    catalog fact about the publication, never a quality or BR statement.
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

    @property
    def usable(self):
        return self.bundle_version is not None


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
