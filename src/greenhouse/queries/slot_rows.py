"""Immutable adapter-neutral source rows; services own evaluation semantics."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from greenhouse.contracts import RuleBasis

@dataclass(frozen=True)
class ClimateSlotRow:
    """One frozen mart slot (values + ALL per-measure quality verdicts +
    source identity). Not a public contract: the typed ThresholdSlot rows
    are built by the pure service layer."""
    slot_start: datetime
    revision_id: str
    logical_key: str
    quality_policy_version: str
    correction_id: str | None
    correction_version: int | None
    tair: Decimal | None
    co2air: Decimal | None
    t_heat_sp: Decimal | None
    t_vent_sp: Decimal | None
    co2_sp: Decimal | None
    t_heat_vip: Decimal | None
    t_ventlee_vip: Decimal | None
    co2_vip: Decimal | None
    measure_quality: dict


@dataclass(frozen=True)
class DrainSlotRow:
    """One frozen mart slot (values + the policy's drain measure quality
    triplets + source identity). Not a public contract: the typed
    ThresholdSlot rows are built by the pure service layer."""
    slot_start: datetime
    revision_id: str
    logical_key: str
    quality_policy_version: str
    correction_id: str | None
    correction_version: int | None
    ec_drain: Decimal | None
    ph_drain: Decimal | None
    measure_quality: dict


@dataclass(frozen=True)
class ScheduleRevisionOption:
    """Selectable immutable schedule revision for the UI (identity + keys
    only; segments load through the repository at evaluation time)."""
    rule_version: str
    rule_id: str
    version: int
    evidence_reference: str
    is_demo: bool
    label: str
    keys: tuple[tuple[str, str, RuleBasis], ...]
    segment_count: int

    def __post_init__(self):
        if self.segment_count < 1:
            raise ValueError('A schedule option represents an existing revision with segments')

    @property
    def demo_flag(self):
        return self.is_demo


