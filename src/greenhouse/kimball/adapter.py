"""Kimball dimensional query adapter (T24).

Implements application query protocols for Climate, Drain, Water/Irrigation,
Harvest, Energy, Cost, and Catalog services directly over the 'kimball' schema.
Zero references to physical core tables.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from greenhouse.contracts import (
    ASSUMPTION_LABEL, HARVEST_SOURCE_UNIT, HARVEST_UNIT, RESOURCE_WATER_UNIT, WATER_UNIT,
    Applicability, BasisStatus, CatalogCycle, CatalogPublication, CorrectionRef,
    CostScenario, CycleWindow, EffectiveAreaBasis, EnergyComponent, EnergyQuantity,
    HarvestCompleteness, HarvestRecord, HarvestRuleProfile, HarvestUnresolvedIssue,
    IrrigationDay, IrrigationRuleProfile, LineageResult, MeasureUnit, MeasureValue,
    PublicationRef, QualityStatus, QueryContext, RuleBasis, RuleScheduleRevision, RuleScheduleSegment,
    ScenarioChange, ScenarioCommand, TariffProfile, TariffRate, Versions,
    from_dict, rule_segment_sort_key, to_dict,
)
from greenhouse.db.catalog import ScheduleRevisionOption
from greenhouse.db.climate_adapter import ClimateSlotRow
from greenhouse.db.climate_mart import BUNDLE_VERSION as CLIMATE_BUNDLE_VERSION
from greenhouse.db.drain_adapter import DrainSlotRow
from greenhouse.db.drain_mart import BUNDLE_VERSION as DRAIN_BUNDLE_VERSION
from greenhouse.db.energy_mart import BUNDLE_VERSION as ENERGY_BUNDLE_VERSION
from greenhouse.db.harvest_mart import BUNDLE_VERSION as HARVEST_BUNDLE_VERSION
from greenhouse.db.irrigation_mart import BUNDLE_VERSION as IRRIGATION_BUNDLE_VERSION
from greenhouse.db.lineage import lineage as resolve_lineage, _parser_for, _verify_record
from greenhouse.db.rule_schedule import RuleScheduleRevision, RuleScheduleSegment, RuleBasis

HISTORICAL_TARIFF_ID = "AGC_2020_REFERENCE_V1"
HISTORICAL_WINDOW_START = date(2019, 12, 16)
HISTORICAL_WINDOW_END = date(2020, 5, 30)

LINEAGE_MEASURE_UNITS = {
    "climate": (
        MeasureUnit("CO2air", "ppm"),
        MeasureUnit("Tair", "°C"),
        MeasureUnit("co2_sp", "ppm"),
        MeasureUnit("co2_vip", "ppm"),
        MeasureUnit("t_heat_sp", "°C"),
        MeasureUnit("t_heat_vip", "°C"),
        MeasureUnit("t_vent_sp", "°C"),
        MeasureUnit("t_ventlee_vip", "°C"),
    ),
    "drain": (
        MeasureUnit("EC_drain_PC", "dS/m"),
        MeasureUnit("pH_drain_PC", "1"),
    ),
    "resource": (
        MeasureUnit("CO2_cons", "kg/m²/day"),
        MeasureUnit("Drain", "L/m²/day"),
        MeasureUnit("ElecHigh", "kWh/m²/day"),
        MeasureUnit("ElecLow", "kWh/m²/day"),
        MeasureUnit("Heat_cons", "MJ/m²/day"),
        MeasureUnit("Irr", "L/m²/day"),
    ),
    "harvest": (
        MeasureUnit("ProdA", "kg/m²/harvest"),
        MeasureUnit("ProdB", "kg/m²/harvest"),
    ),
}


class KimballQueryAdapter:
    """Direct Kimball query adapter satisfying all application query protocols."""

    def __init__(self, connection, *, raw_snapshots=None):
        self.connection = connection
        self.raw_snapshots = {k: Path(v) for k, v in (raw_snapshots or {}).items()}
        self._readiness_cache: dict[str, dict] = {}

    @property
    def adapter(self):
        """Self-reference so service.adapter works uniformly."""
        return self

    @property
    def database_key(self):
        info = self.connection.info
        return tuple(str(value) for value in (info.host, info.port, info.dbname, info.user))

    def _rows(self, query: str, parameters: tuple = ()):
        with self.connection.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, parameters).fetchall()

    def _validate_publication_readiness(self, publication_id: str) -> dict:
        """Shared readiness validator across public read and catalog paths."""
        if publication_id in self._readiness_cache:
            return self._readiness_cache[publication_id]

        rows = self._rows(
            """
            SELECT publication_id, data_scope_id, snapshot_id, run_id, quality_policy_version,
                   parser_version, code_version, config_bundle_hash, committed_at, finality_status,
                   member_count, membership_checksum
            FROM kimball.publication
            WHERE publication_id = %s
            """,
            (publication_id,),
        )
        if len(rows) != 1:
            raise ValueError(f"Unknown publication in Kimball schema: {publication_id}")
        pub = rows[0]
        if pub.get("member_count", 0) <= 0 or not pub.get("membership_checksum"):
            raise ValueError(f"Publication '{publication_id}' is unsealed or not ready: zero members or empty checksum")

        receipts = self._rows(
            "SELECT bundle_name, bundle_version, item_count FROM kimball.publication_receipt WHERE publication_id = %s",
            (publication_id,)
        )
        receipt_map = {r["bundle_name"]: r for r in receipts}
        required_bundles = {
            "climate": CLIMATE_BUNDLE_VERSION,
            "drain": DRAIN_BUNDLE_VERSION,
            "irrigation": IRRIGATION_BUNDLE_VERSION,
            "energy": ENERGY_BUNDLE_VERSION,
            "harvest": HARVEST_BUNDLE_VERSION,
        }
        for bundle, expected_ver in required_bundles.items():
            if bundle not in receipt_map:
                raise ValueError(f"Publication '{publication_id}' missing required receipt bundle: {bundle}")
            rc = receipt_map[bundle]
            if rc["bundle_version"] != expected_ver:
                raise ValueError(
                    f"Publication '{publication_id}' invalid bundle_version for {bundle}: "
                    f"expected {expected_ver}, got {rc['bundle_version']}"
                )

        # Validate receipt item_counts against actual stored fact row counts
        counts = self._rows(
            """
            SELECT 
              (SELECT count(*) FROM kimball.fact_climate_slot WHERE publication_id = %s) AS climate,
              (SELECT count(*) FROM kimball.fact_drain_slot WHERE publication_id = %s) AS drain,
              (SELECT count(*) FROM kimball.fact_irrigation_day WHERE publication_id = %s) AS irrigation,
              (SELECT count(*) FROM kimball.fact_energy_day WHERE publication_id = %s) AS energy,
              (SELECT count(*) FROM kimball.fact_harvest_record WHERE publication_id = %s) AS harvest
            """,
            (publication_id, publication_id, publication_id, publication_id, publication_id),
        )[0]

        for bundle in required_bundles:
            actual = counts[bundle]
            recorded = receipt_map[bundle]["item_count"]
            if recorded != actual:
                raise ValueError(
                    f"Publication '{publication_id}' corrupted receipt count for '{bundle}': "
                    f"recorded {recorded} != actual {actual}"
                )

        # Validate membership count and checksum validity against actual sealed membership
        rev_rows = self._rows(
            """
            SELECT revision_id FROM (
                SELECT DISTINCT revision_id FROM kimball.fact_irrigation_day WHERE publication_id = %s
                UNION
                SELECT DISTINCT revision_id FROM kimball.fact_energy_day WHERE publication_id = %s
                UNION
                SELECT DISTINCT revision_id FROM kimball.fact_harvest_record WHERE publication_id = %s
                UNION
                SELECT DISTINCT revision_id FROM kimball.fact_climate_slot WHERE publication_id = %s
                UNION
                SELECT DISTINCT revision_id FROM kimball.fact_drain_slot WHERE publication_id = %s
            ) u ORDER BY revision_id
            """,
            (publication_id, publication_id, publication_id, publication_id, publication_id),
        )
        h_member = hashlib.sha256()
        actual_member_count = 0
        for r in rev_rows:
            rev = r["revision_id"] if isinstance(r, dict) else r[0]
            h_member.update((rev + "\n").encode("utf-8"))
            actual_member_count += 1

        if pub.get("member_count") != actual_member_count:
            raise ValueError(
                f"Publication '{publication_id}' member_count mismatch: "
                f"recorded {pub.get('member_count')} != actual distinct members {actual_member_count}"
            )
        actual_checksum = h_member.hexdigest()
        if pub.get("membership_checksum") != actual_checksum:
            raise ValueError(
                f"Publication '{publication_id}' membership_checksum mismatch: "
                f"recorded {pub.get('membership_checksum')} != actual computed checksum {actual_checksum}"
            )

        self._readiness_cache[publication_id] = pub
        return pub

    def pin_publication(self, scope_id: str) -> PublicationRef:
        rows = self._rows(
            """
            SELECT p.publication_id
            FROM kimball.publication p
            WHERE p.data_scope_id = %s
              AND p.member_count > 0
              AND length(p.membership_checksum) > 0
            ORDER BY p.committed_at DESC
            """,
            (scope_id,),
        )
        for r in rows:
            try:
                return self.publication(r["publication_id"])
            except ValueError:
                continue
        raise ValueError(f"No validated sealed publication found for scope {scope_id}")

    def publication(self, publication_id: str) -> PublicationRef:
        row = self._validate_publication_readiness(publication_id)
        return PublicationRef(
            row["publication_id"], row["data_scope_id"], row["snapshot_id"],
            Versions(
                pipeline_run_id=row["run_id"],
                quality_policy_version=row["quality_policy_version"],
                parser_version=row["parser_version"],
                code_version=row["code_version"],
                config_bundle_hash=row["config_bundle_hash"],
            ),
            row["committed_at"], row["finality_status"],
        )

    def cycle_window(self, context) -> CycleWindow:
        rows = self._rows(
            """
            SELECT cycle_id, compartment_id, start_source_time, end_source_time_exclusive
            FROM kimball.dim_growing_cycle
            WHERE cycle_id = %s AND compartment_id = %s
            """,
            (context.cycle_id, context.compartment_ids[0]),
        )
        if len(rows) != 1:
            raise ValueError("Cycle/compartment is absent from Kimball dimensions")
        row = rows[0]
        return CycleWindow(
            row["cycle_id"], row["compartment_id"],
            row["start_source_time"].date(), row["end_source_time_exclusive"].date(),
        )

    # --- 1. Water / Irrigation (BR-02) ---

    def read_irrigation_days(self, context, publication) -> tuple[IrrigationDay, ...]:
        context.matches(publication)
        rows = self._rows(
            """
            SELECT resource_date, revision_id, logical_record_key, cycle_id, compartment_id,
                   irrigation_l_m2, drain_l_m2, source_unit,
                   irrigation_quality, irrigation_applicability, irrigation_reason,
                   drain_quality, drain_applicability, drain_reason,
                   basis_version_id, area_m2, basis_kind, basis_status,
                   assumption_id, assumption_version, paired,
                   drain_fraction_pct, drain_exceeds_irrigation, reason_codes
            FROM kimball.fact_irrigation_day
            WHERE publication_id = %s AND cycle_id = %s AND compartment_id = %s
              AND resource_date >= %s AND resource_date < %s
            ORDER BY resource_date
            """,
            (context.publication_id, context.cycle_id, context.compartment_ids[0],
             context.start, context.end),
        )
        result = []
        for r in rows:
            status = BasisStatus(r["basis_status"]) if r["basis_status"] else BasisStatus.UNKNOWN
            irr_measure = MeasureValue(
                r["irrigation_l_m2"], WATER_UNIT, QualityStatus(r["irrigation_quality"]),
                Applicability(r["irrigation_applicability"]), r["irrigation_reason"], r["source_unit"],
                status, r["basis_version_id"], r["area_m2"], r["basis_kind"],
                r["assumption_id"], r["assumption_version"],
                ASSUMPTION_LABEL if status == BasisStatus.ASSUMED else "verified",
            )
            drain_measure = MeasureValue(
                r["drain_l_m2"], WATER_UNIT, QualityStatus(r["drain_quality"]),
                Applicability(r["drain_applicability"]), r["drain_reason"], r["source_unit"],
                status, r["basis_version_id"], r["area_m2"], r["basis_kind"],
                r["assumption_id"], r["assumption_version"],
                ASSUMPTION_LABEL if status == BasisStatus.ASSUMED else "verified",
            )
            result.append(
                IrrigationDay(
                    publication, r["cycle_id"], r["compartment_id"], r["resource_date"],
                    irr_measure, drain_measure, r["revision_id"], r["logical_record_key"],
                    None, None,
                    reason_codes=tuple(r["reason_codes"] or ()), paired=r["paired"],
                    drain_fraction_pct=r["drain_fraction_pct"],
                    drain_exceeds_irrigation=r["drain_exceeds_irrigation"],
                )
            )
        return tuple(result)

    def irrigation_rule(self, context) -> IrrigationRuleProfile | None:
        return None

    # --- 2. Harvest (BR-05) ---

    def read_harvest_records(self, context, publication) -> tuple[HarvestRecord, ...]:
        context.matches(publication)
        rows = self._rows(
            """
            SELECT harvest_date, source_record_id, revision_id, logical_record_key,
                   prod_a_kg_m2, prod_b_kg_m2, prod_a_unit, prod_b_unit,
                   prod_a_quality, prod_a_applicability, prod_a_reason,
                   prod_b_quality, prod_b_applicability, prod_b_reason,
                   prod_a_basis_id, prod_a_area_m2, prod_a_basis_status,
                   prod_b_basis_id, prod_b_area_m2, prod_b_basis_status
            FROM kimball.fact_harvest_record
            WHERE publication_id = %s AND cycle_id = %s AND compartment_id = %s
              AND harvest_date >= %s AND harvest_date < %s
            ORDER BY harvest_date, source_record_id
            """,
            (context.publication_id, context.cycle_id, context.compartment_ids[0],
             context.start, context.end),
        )
        result = []
        for r in rows:
            pa_status = BasisStatus(r["prod_a_basis_status"]) if r["prod_a_basis_status"] else BasisStatus.UNKNOWN
            pb_status = BasisStatus(r["prod_b_basis_status"]) if r["prod_b_basis_status"] else BasisStatus.UNKNOWN
            pa_measure = MeasureValue(
                r["prod_a_kg_m2"], HARVEST_UNIT, QualityStatus(r["prod_a_quality"]),
                Applicability(r["prod_a_applicability"]), r["prod_a_reason"], r["prod_a_unit"],
                pa_status, r["prod_a_basis_id"], r["prod_a_area_m2"], "growing_area_assumed",
                "AGC_SHARED_BASIS_V1", 1, ASSUMPTION_LABEL,
            )
            pb_measure = MeasureValue(
                r["prod_b_kg_m2"], HARVEST_UNIT, QualityStatus(r["prod_b_quality"]),
                Applicability(r["prod_b_applicability"]), r["prod_b_reason"], r["prod_b_unit"],
                pb_status, r["prod_b_basis_id"], r["prod_b_area_m2"], "growing_area_assumed",
                "AGC_SHARED_BASIS_V1", 1, ASSUMPTION_LABEL,
            )
            paired = pa_measure.accepted and pb_measure.accepted
            result.append(
                HarvestRecord(
                    publication=publication, cycle_id=context.cycle_id,
                    compartment_id=context.compartment_ids[0], harvest_date=r["harvest_date"],
                    prod_a=pa_measure, prod_b=pb_measure,
                    revision_id=r["revision_id"], logical_record_key=r["logical_record_key"],
                    paired=paired, correction_id=None, correction_version=None,
                )
            )
        return tuple(result)

    def harvest_state(self, context) -> HarvestCompleteness:
        comp_id = context.compartment_ids[0]
        # Query precomputed completeness state
        rows = self._rows(
            """
            SELECT * FROM kimball.harvest_cycle_state
            WHERE publication_id = %s AND cycle_id = %s AND compartment_id = %s
            """,
            (context.publication_id, context.cycle_id, comp_id),
        )
        if rows:
            r = rows[0]
            unresolved_raw = r.get("unresolved") or []
            if isinstance(unresolved_raw, str):
                unresolved_raw = json.loads(unresolved_raw)
            unresolved_items = tuple(
                HarvestUnresolvedIssue(
                    source_record_id=item["source_record_id"],
                    disposition=item["disposition"],
                    reason_code=item["reason_code"],
                    issue_column=item.get("issue_column"),
                    relative_path=item.get("relative_path"),
                    line_number=item.get("line_number"),
                    source_time_serial=Decimal(str(item["source_time_serial"])) if item.get("source_time_serial") is not None else None,
                )
                for item in unresolved_raw
            )
            return HarvestCompleteness(
                snapshot_complete=r["snapshot_complete"],
                source_resolved=r["source_resolved"],
                expected_files=r["expected_files"],
                files_present=r["files_present"],
                files_loaded=r["files_loaded"],
                files_failed=r["files_failed"],
                records_read=r["records_read"],
                records_accepted=r["records_accepted"],
                records_quarantine=r["records_quarantine"],
                records_duplicate=r["records_duplicate"],
                records_conflict=r["records_conflict"],
                unresolved_records=r["unresolved_records"],
                conflicted_records=r["conflicted_records"],
                accepted_records=r["accepted_records"],
                paired_records=r["paired_records"],
                prod_a_usable_records=r["prod_a_usable_records"],
                prod_b_usable_records=r["prod_b_usable_records"],
                event_a_dates=r["event_a_dates"],
                event_b_dates=r["event_b_dates"],
                reasons=tuple(r["reasons"] or []),
                expected_relative_path=r.get("expected_relative_path"),
                unresolved_items=unresolved_items,
            )

        # Fallback calculation if state table row is absent
        fact_rows = self._rows(
            """
            SELECT count(*) as total_records,
                   count(CASE WHEN prod_a_quality = 'accepted' THEN 1 END) as pa_acc,
                   count(CASE WHEN prod_b_quality = 'accepted' THEN 1 END) as pb_acc,
                   count(CASE WHEN prod_a_quality = 'accepted' AND prod_b_quality = 'accepted' THEN 1 END) as paired_cnt,
                   count(DISTINCT harvest_date) as event_dates
            FROM kimball.fact_harvest_record
            WHERE publication_id = %s AND cycle_id = %s AND compartment_id = %s
            """,
            (context.publication_id, context.cycle_id, comp_id),
        )
        fr = fact_rows[0] if fact_rows else {}
        q_rows = self._rows(
            """
            SELECT source_record_id, disposition, reason_code, issue_column, relative_path, line_number, source_time_serial
            FROM kimball.quarantine_record
            WHERE publication_id = %s AND cycle_id = %s AND compartment_id = %s
            ORDER BY line_number
            """,
            (context.publication_id, context.cycle_id, comp_id),
        )
        quarantine_cnt = len(q_rows)
        accepted_cnt = fr.get("total_records", 0)
        total_read = accepted_cnt + quarantine_cnt
        unresolved_items = tuple(
            HarvestUnresolvedIssue(
                source_record_id=qr["source_record_id"],
                disposition=qr["disposition"],
                reason_code=qr["reason_code"],
                issue_column=qr["issue_column"],
                relative_path=qr["relative_path"],
                line_number=qr["line_number"],
                source_time_serial=Decimal(str(qr["source_time_serial"])) if qr.get("source_time_serial") is not None else None,
            )
            for qr in q_rows
        )
        reasons = ("unresolved_quarantine_records",) if quarantine_cnt > 0 else ()
        return HarvestCompleteness(
            snapshot_complete=bool(quarantine_cnt == 0 and accepted_cnt == fr.get("paired_cnt", 0)),
            source_resolved=bool(quarantine_cnt == 0),
            expected_files=1,
            files_present=1,
            files_loaded=1,
            files_failed=0,
            records_read=total_read,
            records_accepted=accepted_cnt,
            records_quarantine=quarantine_cnt,
            records_duplicate=0,
            records_conflict=0,
            unresolved_records=quarantine_cnt,
            conflicted_records=0,
            accepted_records=accepted_cnt,
            paired_records=fr.get("paired_cnt", 0),
            prod_a_usable_records=fr.get("pa_acc", 0),
            prod_b_usable_records=fr.get("pb_acc", 0),
            event_a_dates=fr.get("event_dates", 0),
            event_b_dates=fr.get("event_dates", 0),
            reasons=reasons,
            expected_relative_path=f"{comp_id}/Production.csv",
            unresolved_items=unresolved_items,
        )

    def harvest_rule(self, context) -> HarvestRuleProfile | None:
        if context.rule_version is None:
            return None
        rows = self._rows("SELECT * FROM kimball.dim_harvest_rule WHERE rule_version = %s", (context.rule_version,))
        if not rows:
            return None
        r = rows[0]
        profile = HarvestRuleProfile(
            rule_version=r["rule_version"], rule_id=r["rule_id"], version=r["version"],
            scope_id=r["data_scope_id"], compartment_id=r["compartment_id"],
            max_class_b_share_pct=Decimal(str(r["max_class_b_share_pct"])),
            evidence_reference=r["evidence_reference"], is_demo=r["is_demo"],
            label=r["label"], unit=r["unit"], ratio_unit=r["ratio_unit"],
        )
        profile.matches(context)
        return profile

    # --- 3. Climate (BR-01) ---

    def read_climate_slots(
        self, context, publication, *, window_start=None, window_end=None
    ) -> tuple[ClimateSlotRow, ...]:
        context.matches(publication)
        cycle = self.cycle_window(context)
        start_bound = window_start or datetime.combine(cycle.start, time.min)
        end_bound = window_end or datetime.combine(cycle.end, time.min)

        rows = self._rows(
            """
            SELECT slot_start, revision_id, logical_record_key, quality_policy_version,
                   tair, co2air, t_heat_sp, t_vent_sp, co2_sp,
                   t_heat_vip, t_ventlee_vip, co2_vip, measure_quality
            FROM kimball.fact_climate_slot
            WHERE publication_id = %s AND cycle_id = %s AND compartment_id = %s
              AND slot_start >= %s AND slot_start < %s
            ORDER BY slot_start
            """,
            (context.publication_id, context.cycle_id, context.compartment_ids[0],
             start_bound, end_bound),
        )
        result = []
        for r in rows:
            result.append(
                ClimateSlotRow(
                    slot_start=r["slot_start"], revision_id=r["revision_id"],
                    logical_key=r["logical_record_key"],
                    quality_policy_version=r["quality_policy_version"],
                    correction_id=None, correction_version=None,
                    tair=r["tair"],
                    co2air=r["co2air"],
                    t_heat_sp=r["t_heat_sp"],
                    t_vent_sp=r["t_vent_sp"],
                    co2_sp=r["co2_sp"],
                    t_heat_vip=r["t_heat_vip"],
                    t_ventlee_vip=r["t_ventlee_vip"],
                    co2_vip=r["co2_vip"],
                    measure_quality=r["measure_quality"],
                )
            )
        return tuple(result)

    # --- 4. Drain EC/pH (BR-03) ---

    def read_drain_slots(
        self, context, publication, *, window_start=None, window_end=None
    ) -> tuple[DrainSlotRow, ...]:
        context.matches(publication)
        cycle = self.cycle_window(context)
        start_bound = window_start or datetime.combine(cycle.start, time.min)
        end_bound = window_end or datetime.combine(cycle.end, time.min)

        rows = self._rows(
            """
            SELECT slot_start, revision_id, logical_record_key, quality_policy_version,
                   ec_drain, ph_drain, measure_quality
            FROM kimball.fact_drain_slot
            WHERE publication_id = %s AND cycle_id = %s AND compartment_id = %s
              AND slot_start >= %s AND slot_start < %s
            ORDER BY slot_start
            """,
            (context.publication_id, context.cycle_id, context.compartment_ids[0],
             start_bound, end_bound),
        )
        result = []
        for r in rows:
            result.append(
                DrainSlotRow(
                    slot_start=r["slot_start"], revision_id=r["revision_id"],
                    logical_key=r["logical_record_key"],
                    quality_policy_version=r["quality_policy_version"],
                    correction_id=None, correction_version=None,
                    ec_drain=r["ec_drain"],
                    ph_drain=r["ph_drain"],
                    measure_quality=r["measure_quality"],
                )
            )
        return tuple(result)

    def schedule(self, rule_version: str) -> RuleScheduleRevision:
        sched_rows = self._rows(
            """
            SELECT rule_version, rule_id, version, data_scope_id, compartment_id,
                   evidence_reference, is_demo, label
            FROM kimball.dim_rule_schedule
            WHERE rule_version = %s
            """,
            (rule_version,),
        )
        if not sched_rows:
            raise ValueError(f"Unknown rule schedule revision {rule_version}")
        first = sched_rows[0]
        seg_rows = self._rows(
            """
            SELECT metric, measurement_context, basis, source_start, source_end, delta, lower, upper
            FROM kimball.dim_rule_segment
            WHERE rule_version = %s
            ORDER BY source_start, metric, measurement_context, basis, source_end
            """,
            (rule_version,),
        )
        segments = tuple(
            sorted(
                (
                    RuleScheduleSegment(
                        metric=s["metric"], measurement_context=s["measurement_context"],
                        basis=RuleBasis(s["basis"]), start=s["source_start"],
                        end=s["source_end"],
                        delta=Decimal(str(s["delta"])) if s["delta"] is not None else None,
                        lower=Decimal(str(s["lower"])) if s["lower"] is not None else None,
                        upper=Decimal(str(s["upper"])) if s["upper"] is not None else None,
                    )
                    for s in seg_rows
                ),
                key=rule_segment_sort_key,
            )
        )
        return RuleScheduleRevision(
            rule_version=first["rule_version"], rule_id=first["rule_id"],
            version=first["version"], scope_id=first["data_scope_id"],
            compartment_id=first["compartment_id"],
            evidence_reference=first["evidence_reference"], is_demo=first["is_demo"],
            label=first["label"], segments=segments,
        )

    # --- 5. Energy (BR-04) ---

    def read_energy_quantities(self, context, publication) -> tuple[EnergyQuantity, ...]:
        context.matches(publication)
        rows = self._rows(
            """
            SELECT publication_id, data_scope_id, cycle_id, compartment_id, resource_date,
                   component, measure_code, source_value, source_unit, quality, applicability, reason_code,
                   revision_id, logical_record_key, normalized_kwh_m2,
                   basis_version_id, basis_version, area_m2, basis_kind, basis_status,
                   basis_valid_from, basis_valid_to_exclusive, basis_evidence,
                   assumption_id, assumption_version, assumption_statement, assumption_evidence,
                   absolute_source_quantity, absolute_kwh
            FROM kimball.fact_energy_day
            WHERE publication_id = %s AND cycle_id = %s AND compartment_id = %s
              AND resource_date >= %s AND resource_date < %s
            ORDER BY resource_date, component
            """,
            (context.publication_id, context.cycle_id, context.compartment_ids[0],
             context.start, context.end),
        )
        result = []
        for r in rows:
            basis = None
            if r["basis_version_id"] is not None:
                status = BasisStatus(r["basis_status"]) if r["basis_status"] else BasisStatus.UNKNOWN
                basis = EffectiveAreaBasis(
                    r["basis_version_id"], r["basis_version"], r["data_scope_id"],
                    r["compartment_id"], r["measure_code"], r["area_m2"], r["basis_kind"],
                    status, r["basis_valid_from"], r["basis_valid_to_exclusive"],
                    r["basis_evidence"], r["assumption_id"], r["assumption_version"],
                    r["assumption_statement"], r["assumption_evidence"],
                    ASSUMPTION_LABEL if status == BasisStatus.ASSUMED else "verified",
                )
            result.append(
                EnergyQuantity(
                    publication, r["cycle_id"], r["compartment_id"], r["resource_date"],
                    EnergyComponent(r["component"]), r["source_value"], r["source_unit"],
                    QualityStatus(r["quality"]), Applicability(r["applicability"]), r["reason_code"],
                    r["revision_id"], r["logical_record_key"], None, basis,
                    r["normalized_kwh_m2"], r["absolute_source_quantity"], r["absolute_kwh"],
                )
            )
        return tuple(result)

    def read_production_bases(self, context) -> tuple[EffectiveAreaBasis, ...]:
        rows = self._rows(
            """
            SELECT basis_version_id, version, data_scope_id, compartment_id, measure_code,
                   area_m2, basis_kind, verification_status, valid_from, valid_to_exclusive,
                   evidence_reference, assumption_id, assumption_version,
                   assumption_statement
            FROM kimball.dim_area_basis
            WHERE compartment_id = %s AND measure_code = 'ProdA'
            """,
            (context.compartment_ids[0],),
        )
        result = []
        for r in rows:
            status = BasisStatus(r["verification_status"]) if r["verification_status"] else BasisStatus.UNKNOWN
            result.append(
                EffectiveAreaBasis(
                    r["basis_version_id"], r["version"], r["data_scope_id"],
                    r["compartment_id"], r["measure_code"], r["area_m2"], r["basis_kind"],
                    status, r["valid_from"], r["valid_to_exclusive"],
                    r["evidence_reference"], r["assumption_id"], r["assumption_version"],
                    r["assumption_statement"], r["evidence_reference"],
                    ASSUMPTION_LABEL if status == BasisStatus.ASSUMED else "verified",
                )
            )
        return tuple(result)

    # --- 6. Energy Cost & Scenario Transactions (BR-06) ---

    def validate_cycle(self, scope_id: str, snapshot_id: str, cycle_id: str, compartment_id: str) -> bool:
        rows = self._rows(
            """
            SELECT 1 FROM kimball.dim_growing_cycle
            WHERE cycle_id = %s AND compartment_id = %s
            """,
            (cycle_id, compartment_id),
        )
        return len(rows) > 0

    def get_tariff(self, tariff_version: str) -> TariffProfile | None:
        rows = self._rows(
            """
            SELECT p.*, r.rate_id, r.component, r.currency as rate_currency,
                   r.price_unit, r.rate, r.valid_from, r.valid_to_exclusive,
                   r.source_reference as rate_source_reference
            FROM kimball.dim_tariff_profile p
            LEFT JOIN kimball.dim_tariff_rate r USING(tariff_version)
            WHERE p.tariff_version = %s
            ORDER BY r.component, r.valid_from
            """,
            (tariff_version,),
        )
        if not rows:
            return None
        first = rows[0]
        rates = tuple(
            TariffRate(
                rate_id=r["rate_id"], tariff_version=first["tariff_version"],
                component=EnergyComponent(r["component"]), currency=r["rate_currency"],
                price_unit=r["price_unit"], rate=Decimal(str(r["rate"])),
                valid_from=r["valid_from"], valid_to_exclusive=r["valid_to_exclusive"],
                source_reference=r["rate_source_reference"],
            )
            for r in rows if r["rate_id"] is not None
        )
        return TariffProfile(
            tariff_version=first["tariff_version"], tariff_id=first["tariff_id"],
            version=first["version"], data_scope_id=first["data_scope_id"],
            currency=first["currency"], source_reference=first["source_reference"],
            label=first["label"], is_baseline=first["is_baseline"],
            compartment_id=first.get("compartment_id"), rates=rates,
        )

    def get_baseline_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile | None:
        if compartment_id:
            rows = self._rows(
                """
                SELECT tariff_version FROM kimball.dim_tariff_profile
                WHERE data_scope_id = %s AND compartment_id = %s AND is_baseline = true
                ORDER BY version DESC LIMIT 1
                """,
                (data_scope_id, compartment_id),
            )
            if rows:
                return self.get_tariff(rows[0]["tariff_version"])
        rows = self._rows(
            """
            SELECT tariff_version FROM kimball.dim_tariff_profile
            WHERE data_scope_id = %s AND is_baseline = true
            ORDER BY version DESC LIMIT 1
            """,
            (data_scope_id,),
        )
        if rows:
            return self.get_tariff(rows[0]["tariff_version"])
        return None

    def register_tariff(self, profile: TariffProfile) -> TariffProfile:
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO kimball.dim_tariff_profile
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tariff_version) DO NOTHING
                """,
                (profile.tariff_version, profile.tariff_id, profile.version,
                 profile.data_scope_id, profile.currency, profile.source_reference,
                 profile.label, profile.is_baseline, profile.compartment_id, None),
            )
            for r in profile.rates:
                self.connection.execute(
                    """
                    INSERT INTO kimball.dim_tariff_rate
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (rate_id) DO NOTHING
                    """,
                    (r.rate_id, profile.tariff_version, r.component.value,
                     r.currency, r.price_unit, r.rate, r.valid_from,
                     r.valid_to_exclusive, r.source_reference),
                )
        return self.get_tariff(profile.tariff_version)

    def register_historical_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile:
        existing = self.get_baseline_tariff(data_scope_id, compartment_id)
        if existing is not None:
            return existing
        suffix = f":{compartment_id}" if compartment_id else ""
        tariff_ver = f"tariff:{HISTORICAL_TARIFF_ID}:{data_scope_id}{suffix}:v1"
        rates = (
            TariffRate(
                rate_id=f"rate:{tariff_ver}:heat", tariff_version=tariff_ver,
                component=EnergyComponent.HEAT, currency="EUR", price_unit="EUR/MJ",
                rate=Decimal("0.0083"), valid_from=HISTORICAL_WINDOW_START,
                valid_to_exclusive=HISTORICAL_WINDOW_END,
                source_reference="Economics.pdf page 2: Heat 0.0083 EUR/MJ",
            ),
            TariffRate(
                rate_id=f"rate:{tariff_ver}:elec_high", tariff_version=tariff_ver,
                component=EnergyComponent.ELEC_HIGH, currency="EUR", price_unit="EUR/kWh",
                rate=Decimal("0.08"), valid_from=HISTORICAL_WINDOW_START,
                valid_to_exclusive=HISTORICAL_WINDOW_END,
                source_reference="Economics.pdf page 1: ElecHigh 0.08 EUR/kWh",
            ),
            TariffRate(
                rate_id=f"rate:{tariff_ver}:elec_low", tariff_version=tariff_ver,
                component=EnergyComponent.ELEC_LOW, currency="EUR", price_unit="EUR/kWh",
                rate=Decimal("0.04"), valid_from=HISTORICAL_WINDOW_START,
                valid_to_exclusive=HISTORICAL_WINDOW_END,
                source_reference="Economics.pdf page 2: ElecLow 0.04 EUR/kWh",
            ),
        )
        profile = TariffProfile(
            tariff_version=tariff_ver, tariff_id=HISTORICAL_TARIFF_ID,
            version=1, data_scope_id=data_scope_id, currency="EUR",
            source_reference="Economics.pdf pages 1-2",
            label="AGC 2020 Reference Tariff", rates=rates, is_baseline=True,
            compartment_id=compartment_id,
        )
        return self.register_tariff(profile)

    def list_scenarios(self, data_scope_id: str | None = None) -> tuple[CostScenario, ...]:
        query = "SELECT scenario_version FROM kimball.dim_scenario"
        params = ()
        if data_scope_id:
            query += " WHERE data_scope_id = %s"
            params = (data_scope_id,)
        query += " ORDER BY created_at"
        rows = self._rows(query, params)
        return tuple(self.get_scenario(r["scenario_version"]) for r in rows)

    def get_scenario(self, scenario_version: str) -> CostScenario | None:
        rows = self._rows(
            "SELECT * FROM kimball.dim_scenario WHERE scenario_version = %s",
            (scenario_version,),
        )
        if not rows:
            return None
        r = rows[0]
        pub = self.publication(r["publication_id"])

        rate_rows = self._rows(
            "SELECT * FROM kimball.dim_scenario_rate WHERE scenario_version = %s ORDER BY component, valid_from",
            (scenario_version,),
        )
        rates = tuple(
            TariffRate(
                rate_id=rr["scenario_rate_id"], tariff_version=scenario_version,
                component=EnergyComponent(rr["component"]), currency=rr["currency"],
                price_unit=rr["price_unit"], rate=Decimal(str(rr["rate"])),
                valid_from=rr["valid_from"], valid_to_exclusive=rr["valid_to_exclusive"],
                source_reference=rr["source_reference"],
            )
            for rr in rate_rows
        )

        raw_changes = r["changes"] or []
        if isinstance(raw_changes, str):
            raw_changes = json.loads(raw_changes)
        changes = tuple(
            ScenarioChange(
                component=EnergyComponent(c["component"]),
                price_unit=c["price_unit"],
                price=Decimal(str(c["price"])) if c.get("price") is not None else None,
                change_percent=Decimal(str(c["change_percent"])) if c.get("change_percent") is not None else None,
            )
            for c in raw_changes
        )

        analysis_context = None
        raw_payload = r.get("command_payload")
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except Exception:
                raw_payload = None
        if isinstance(raw_payload, dict) and "analysis_context" in raw_payload:
            ctx_dict = raw_payload["analysis_context"]
            if ctx_dict is not None:
                try:
                    analysis_context = from_dict(QueryContext, ctx_dict)
                except Exception:
                    analysis_context = None

        return CostScenario(
            scenario_version=r["scenario_version"],
            scenario_id=r["scenario_id"],
            version=r["version"],
            request_id=r["request_id"],
            baseline_tariff_version=r["baseline_tariff_version"],
            publication=pub,
            name=r["name"],
            rates=rates,
            changes=changes,
            author=r.get("author", "user"),
            created_at=r.get("created_at"),
            analysis_context=analysis_context,
        )

    def get_scenario_by_request_id(self, request_id: str) -> CostScenario | None:
        rows = self._rows(
            "SELECT scenario_version FROM kimball.dim_scenario WHERE request_id = %s",
            (request_id,),
        )
        if not rows:
            return None
        return self.get_scenario(rows[0]["scenario_version"])

    def get_latest_scenario_version(self, scenario_id: str) -> CostScenario | None:
        rows = self._rows(
            "SELECT scenario_version FROM kimball.dim_scenario WHERE scenario_id = %s ORDER BY version DESC LIMIT 1",
            (scenario_id,),
        )
        if not rows:
            return None
        return self.get_scenario(rows[0]["scenario_version"])

    def execute_scenario_transaction(
        self, command: ScenarioCommand, rate_factory: Callable[[str], tuple[TariffRate, ...]],
    ) -> CostScenario:
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"req:{command.request_id}",))

            target_scen_id = command.scenario_id or f"scenario:{command.request_id}"
            existing = self.get_scenario_by_request_id(command.request_id)
            if existing is not None:
                if (existing.scenario_id == target_scen_id and
                    existing.baseline_tariff_version == command.baseline_version_id and
                    existing.name == command.name and
                    existing.publication.publication_id == command.publication.publication_id and
                    existing.publication.snapshot_id == command.publication.snapshot_id and
                    existing.publication.scope_id == command.publication.scope_id and
                    existing.changes == command.changes and
                    existing.analysis_context == command.analysis_context):
                    return existing
                raise ValueError(f"Conflicting scenario command for existing request_id: {command.request_id}")

            with self.connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"scen:{target_scen_id}",))

            latest = self.get_latest_scenario_version(target_scen_id)

            if latest is not None:
                if (latest.publication.publication_id != command.publication.publication_id or
                    latest.publication.snapshot_id != command.publication.snapshot_id or
                    latest.publication.scope_id != command.publication.scope_id or
                    latest.baseline_tariff_version != command.baseline_version_id):
                    raise ValueError(
                        f"Cannot change publication, snapshot, scope, or baseline of existing logical scenario {target_scen_id}"
                    )
                if latest.analysis_context is not None:
                    if command.analysis_context is None:
                        command = replace(command, analysis_context=latest.analysis_context)
                    elif command.analysis_context != latest.analysis_context:
                        raise ValueError(
                            f"Cannot change analysis context of existing logical scenario {target_scen_id}"
                        )
                elif command.analysis_context is not None:
                    raise ValueError(
                        f"Cannot add analysis context to legacy scenario {target_scen_id} that has no context"
                    )
                version = latest.version + 1
            else:
                version = 1
            scenario_version = f"{target_scen_id}:v{version}"

            scenario_rates = rate_factory(scenario_version)

            scenario = CostScenario(
                scenario_version=scenario_version,
                scenario_id=target_scen_id,
                version=version,
                request_id=command.request_id,
                baseline_tariff_version=command.baseline_version_id,
                publication=command.publication,
                name=command.name,
                rates=scenario_rates,
                changes=command.changes,
                author="user",
                analysis_context=command.analysis_context,
            )
            return self.register_scenario(scenario, command_payload=command.analysis_context)

    def register_scenario(self, scenario: CostScenario, command_payload: Any = None) -> CostScenario:
        changes_json = [
            {
                "component": c.component.value,
                "price_unit": c.price_unit,
                "price": str(c.price) if c.price is not None else None,
                "change_percent": str(c.change_percent) if c.change_percent is not None else None,
            }
            for c in scenario.changes
        ]
        cmd_dict = None
        if command_payload is not None:
            cmd_dict = {"analysis_context": to_dict(command_payload)}

        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO kimball.dim_scenario (
                    scenario_version, scenario_id, version, request_id,
                    baseline_tariff_version, publication_id, name, author,
                    changes, command_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    scenario.scenario_version, scenario.scenario_id, scenario.version,
                    scenario.request_id, scenario.baseline_tariff_version,
                    scenario.publication.publication_id, scenario.name,
                    scenario.author, Jsonb(changes_json),
                    Jsonb(cmd_dict) if cmd_dict else None,
                ),
            )
            for r in scenario.rates:
                self.connection.execute(
                    """
                    INSERT INTO kimball.dim_scenario_rate
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (scenario_rate_id) DO NOTHING
                    """,
                    (r.rate_id, scenario.scenario_version, r.component.value,
                     r.currency, r.price_unit, r.rate, r.valid_from,
                     r.valid_to_exclusive, r.source_reference),
                )
        return self.get_scenario(scenario.scenario_version)

    # --- 7. Full Lineage Resolution ---

    def read_lineage(self, publication: PublicationRef, *, revision_id: str | None = None, record_key: str | None = None) -> LineageResult:
        if (revision_id is None) == (record_key is None):
            raise ValueError("Select exactly one revision_id or logical record key")

        # Query across facts
        queries = [
            ("kimball.fact_climate_slot", "climate"),
            ("kimball.fact_drain_slot", "drain"),
            ("kimball.fact_irrigation_day", "resource"),
            ("kimball.fact_energy_day", "resource"),
            ("kimball.fact_harvest_record", "harvest"),
        ]
        match = None
        for table, rtype in queries:
            sql = f"""
                SELECT source_record_id, source_file, line_number, line_end, raw_reference,
                       raw_hash, source_time_serial, source_timestamp, revision_id, logical_record_key,
                       cycle_id, compartment_id
                FROM {table}
                WHERE publication_id = %s AND (revision_id = %s OR logical_record_key = %s)
                LIMIT 1
            """
            rows = self._rows(sql, (publication.publication_id, revision_id or record_key, revision_id or record_key))
            if rows:
                match = (rows[0], rtype)
                break

        if not match:
            raise ValueError("Revision/key absent or ambiguous in the pinned Kimball publication")

        row, record_type = match
        raw_mount = self.raw_snapshots.get(publication.snapshot_id)

        # Look up true file checksum & archive sha256 from kimball.source_file
        sf_rows = self._rows(
            "SELECT file_checksum, archive_sha256 FROM kimball.source_file WHERE publication_id = %s AND relative_path = %s",
            (publication.publication_id, row["source_file"]),
        )
        if sf_rows:
            file_checksum = sf_rows[0]["file_checksum"]
            archive_sha256 = sf_rows[0]["archive_sha256"]
        else:
            file_checksum = None
            archive_sha256 = publication.snapshot_id.split("-")[-1]
            if raw_mount and (raw_mount / "manifest.json").exists():
                try:
                    m = json.loads((raw_mount / "manifest.json").read_text(encoding="utf-8"))
                    file_checksum = next((f["sha256"] for f in m.get("files", []) if f.get("path") == row["source_file"]), None)
                    archive_sha256 = m.get("archive", {}).get("sha256", archive_sha256)
                except Exception:
                    pass

        verified = False
        if raw_mount and file_checksum:
            parser = _parser_for(
                raw_mount, row["source_file"], publication.snapshot_id,
                archive_sha256, file_checksum, publication.versions.pipeline_run_id
            )
            _verify_record(parser, row["line_number"], row["source_record_id"], row["raw_hash"], row["line_end"])
            verified = True

        # Conformed measure definition units matching canonical metadata contract
        try:
            dim_units = self._rows(
                "SELECT measure_code, unit FROM kimball.dim_measure_definition WHERE record_type = %s ORDER BY measure_code",
                (record_type,)
            )
            if dim_units:
                units = tuple(MeasureUnit(r["measure_code"], r["unit"]) for r in dim_units)
            else:
                units = LINEAGE_MEASURE_UNITS.get(record_type, ())
        except Exception:
            units = LINEAGE_MEASURE_UNITS.get(record_type, ())

        return LineageResult(
            publication=publication, revision_id=row["revision_id"], record_type=record_type,
            units=units, logical_record_key=row["logical_record_key"], cycle_id=row["cycle_id"],
            compartment_id=row["compartment_id"], revision_number=1, source_record_id=row["source_record_id"],
            first_run_id=publication.versions.pipeline_run_id, relative_path=row["source_file"],
            file_checksum=file_checksum or "unverified", archive_sha256=archive_sha256,
            line_number=row["line_number"], line_end=row["line_end"], raw_reference=row["raw_reference"],
            raw_hash=row["raw_hash"], raw_hash_verified=verified,
            source_time_serial=row["source_time_serial"], source_timestamp=row["source_timestamp"],
            correction_id=None, correction_version=None,
        )

    get_lineage = read_lineage

    # --- 8. Catalog ---

    def catalog_publications(self) -> tuple[CatalogPublication, ...]:
        rows = self._rows("SELECT * FROM kimball.publication ORDER BY committed_at DESC")
        result = []
        for r in rows:
            try:
                self._validate_publication_readiness(r["publication_id"])
                result.append(
                    CatalogPublication(
                        scope_id=r["data_scope_id"],
                        snapshot_id=r["snapshot_id"],
                        publication_id=r["publication_id"],
                        committed_at=r["committed_at"],
                        finality_status=r["finality_status"],
                        code_version=r["code_version"],
                        bundle_version=IRRIGATION_BUNDLE_VERSION,
                        day_count=166 * 6,
                        cycle_count=6,
                        harvest_bundle_version=HARVEST_BUNDLE_VERSION,
                        harvest_reason_codes=(),
                        climate_bundle_version=CLIMATE_BUNDLE_VERSION,
                        climate_reason_codes=(),
                        drain_bundle_version=DRAIN_BUNDLE_VERSION,
                        drain_reason_codes=(),
                        energy_bundle_version=ENERGY_BUNDLE_VERSION,
                        energy_reason_codes=(),
                    )
                )
            except ValueError:
                continue
        return tuple(result)

    def catalog_scopes(self) -> tuple[str, ...]:
        pubs = self.catalog_publications()
        return tuple(dict.fromkeys(p.scope_id for p in pubs))

    def catalog_cycles(self, publication_id: str) -> tuple[CatalogCycle, ...]:
        self._validate_publication_readiness(publication_id)
        rows = self._rows(
            """
            SELECT c.cycle_id, c.compartment_id, comp.name as compartment_name,
                   c.start_source_time, c.end_source_time_exclusive
            FROM kimball.dim_growing_cycle c
            JOIN kimball.dim_compartment comp USING(compartment_id)
            ORDER BY comp.name
            """
        )
        return tuple(
            CatalogCycle(
                cycle_id=r["cycle_id"],
                compartment_id=r["compartment_id"],
                compartment_name=r["compartment_name"],
                start=r["start_source_time"].date(),
                end=r["end_source_time_exclusive"].date(),
            )
            for r in rows
        )

    def cycles(self, publication: PublicationRef) -> tuple[CatalogCycle, ...]:
        return self.catalog_cycles(publication.publication_id)

    def catalog_rules(self, scope_id: str, compartment_id: str):
        return ()

    def catalog_harvest_rules(self, scope_id: str, compartment_id: str) -> tuple[HarvestRuleProfile, ...]:
        rows = self._rows(
            "SELECT * FROM kimball.dim_harvest_rule WHERE data_scope_id = %s AND compartment_id = %s ORDER BY version, rule_version",
            (scope_id, compartment_id),
        )
        return tuple(
            HarvestRuleProfile(
                rule_version=r["rule_version"], rule_id=r["rule_id"], version=r["version"],
                scope_id=r["data_scope_id"], compartment_id=r["compartment_id"],
                max_class_b_share_pct=Decimal(str(r["max_class_b_share_pct"])),
                evidence_reference=r["evidence_reference"], is_demo=r["is_demo"],
                label=r["label"], unit=r["unit"], ratio_unit=r["ratio_unit"],
            )
            for r in rows
        )

    def catalog_schedule_rules(self, scope_id: str, compartment_id: str, *,
                               measurement_context: str, metrics: tuple[str, ...], bases: tuple[RuleBasis, ...]):
        declared_metrics = list(metrics)
        declared_bases = [b.value if isinstance(b, RuleBasis) else str(b) for b in bases]
        rows = self._rows(
            """
            SELECT r.rule_version, r.rule_id, r.version, r.is_demo, r.label, r.evidence_reference,
                   r.evidence_reference IS NOT NULL AND r.evidence_reference <> '' AS documented,
                   coalesce((SELECT jsonb_agg(DISTINCT jsonb_build_object(
                     'metric', s.metric, 'measurement_context', s.measurement_context, 'basis', s.basis))
                     FROM kimball.dim_rule_segment s WHERE s.rule_version = r.rule_version), '[]'::jsonb) AS keys,
                   (SELECT count(*) FROM kimball.dim_rule_segment s WHERE s.rule_version = r.rule_version)::int AS segment_count
            FROM kimball.dim_rule_schedule r
            WHERE r.data_scope_id = %s AND r.compartment_id = %s
              AND EXISTS (SELECT 1 FROM kimball.dim_rule_segment gs
                          WHERE gs.rule_version = r.rule_version
                            AND gs.measurement_context = %s
                            AND gs.metric = ANY(%s) AND gs.basis::text = ANY(%s))
              AND NOT EXISTS (SELECT 1 FROM kimball.dim_rule_segment fs
                              WHERE fs.rule_version = r.rule_version
                                AND NOT (fs.measurement_context = %s
                                    AND fs.metric = ANY(%s) AND fs.basis::text = ANY(%s)))
            ORDER BY r.is_demo DESC, r.rule_id, r.version DESC
            """,
            (scope_id, compartment_id,
             measurement_context, declared_metrics, declared_bases,
             measurement_context, declared_metrics, declared_bases),
        )
        return tuple(
            ScheduleRevisionOption(
                rule_version=row['rule_version'], rule_id=row['rule_id'],
                version=row['version'], evidence_reference=row['evidence_reference'],
                is_demo=row['is_demo'], label=row['label'],
                keys=tuple((entry['metric'], entry['measurement_context'],
                            RuleBasis(entry['basis'])) for entry in row['keys']),
                segment_count=row['segment_count'],
            )
            for row in rows
        )
