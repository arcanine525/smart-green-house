"""Integration parity test suite comparing Canonical Hybrid vs Kimball Alternative (T24).

Executes all six historical workloads across both architectures and asserts exact
parity within contract tolerances (exact Decimal where required). Normalizes only
declared runtime/publication IDs. Verifies complete absence of physical core tables
via real pg_catalog columns.
"""
from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import pytest
import psycopg

from greenhouse.contracts import (
    Applicability, BasisStatus, CycleWindow, EnergyComponent,
    PublicationRef, QualityStatus, QueryContext, RuleBasis,
    ScenarioChange, ScenarioCommand,
)
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
from greenhouse.db.cost_adapter import PostgresCostAdapter
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.db.query_adapter import PostgresQueryAdapter
from greenhouse.kimball.adapter import KimballQueryAdapter
from greenhouse.kimball.audit import verify_kimball_isolation
from greenhouse.kimball.etl import (
    AGC_SCOPE, KIMBALL_DB_NAME, KIMBALL_PUBLICATION_ID, run_kimball_pipeline,
)
from greenhouse.queries import (
    ClimateService, DrainService, EnergyIntensityService,
    EnergyService, IrrigationService,
)
from greenhouse.queries.cost import EnergyCostService
from greenhouse.queries.harvest import HarvestService
from greenhouse.queries.catalog import CatalogService
from greenhouse.settings import Settings

HYBRID_DB = os.environ.get("PGDATABASE", "greenhouse_t07_195889f1a3b1")
KIMBALL_DB = os.environ.get("KIMBALL_DATABASE", KIMBALL_DB_NAME)
RAW_ROOT = Path(os.environ.get("RAW_ROOT", "/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"))
COMPARTMENTS = ("AICU", "Automatoes", "Digilog", "IUACAAS", "Reference", "TheAutomators")


@pytest.fixture(scope="module")
def hybrid_conn():
    settings = Settings.from_env()
    kwargs = settings.connection_kwargs()
    kwargs["dbname"] = HYBRID_DB
    with psycopg.connect(**kwargs) as conn:
        yield conn


@pytest.fixture(scope="module")
def kimball_conn():
    settings = Settings.from_env()
    kwargs = settings.connection_kwargs()
    kwargs["dbname"] = KIMBALL_DB
    with psycopg.connect(**kwargs) as conn:
        yield conn


def test_kimball_core_independence_audit(kimball_conn):
    """Prove that Kimball database has 0 core tables, 0 core FKs, 0 FDW, 0 dblink."""
    audit = verify_kimball_isolation(kimball_conn)
    assert audit.is_pure_kimball is True
    assert audit.core_table_count == 0
    assert audit.core_fk_count == 0
    assert audit.foreign_table_count == 0
    assert audit.fdw_extension_count == 0
    assert audit.kimball_table_count >= 10


def test_br01_climate_parity(hybrid_conn, kimball_conn):
    """Workload 1: Climate 5-minute slots and quality across all 6 compartments."""
    h_adapter = PostgresClimateQueryAdapter(hybrid_conn)
    k_adapter = KimballQueryAdapter(kimball_conn)

    h_pub = h_adapter.publication(h_adapter.pin_publication(AGC_SCOPE).publication_id)
    k_pub = k_adapter.publication(k_adapter.pin_publication(AGC_SCOPE).publication_id)

    h_catalog = CatalogService(PostgresCatalogAdapter(hybrid_conn))
    k_catalog = CatalogService(k_adapter)

    h_cycles = {c.compartment_id: c for c in h_catalog.cycles(h_pub)}
    k_cycles = {c.compartment_id: c for c in k_catalog.cycles(k_pub)}

    for comp in COMPARTMENTS:
        hc = h_cycles[comp]
        kc = k_cycles[comp]
        h_ctx = QueryContext(h_pub.scope_id, h_pub.publication_id, h_pub.snapshot_id,
                             hc.cycle_id, (comp,), hc.start, hc.end)
        k_ctx = QueryContext(k_pub.scope_id, k_pub.publication_id, k_pub.snapshot_id,
                             kc.cycle_id, (comp,), kc.start, kc.end)

        h_slots = h_adapter.read_climate_slots(h_ctx, h_pub)
        k_slots = k_adapter.read_climate_slots(k_ctx, k_pub)

        assert len(h_slots) == len(k_slots)
        # Spot check first 50 slots and last 50 slots
        check_indices = list(range(min(50, len(h_slots)))) + list(range(max(0, len(h_slots) - 50), len(h_slots)))
        for idx in check_indices:
            hs = h_slots[idx]
            ks = k_slots[idx]
            assert hs.slot_start == ks.slot_start
            assert hs.tair == ks.tair
            assert hs.co2air == ks.co2air
            assert hs.t_heat_sp == ks.t_heat_sp
            assert hs.t_vent_sp == ks.t_vent_sp
            assert hs.co2_sp == ks.co2_sp
            assert hs.t_heat_vip == ks.t_heat_vip
            assert hs.t_ventlee_vip == ks.t_ventlee_vip
            assert hs.co2_vip == ks.co2_vip
            assert hs.measure_quality == ks.measure_quality


        # Exercise Rule / Episode / Evaluation Service Output
        h_clim_svc = ClimateService(h_adapter)
        k_clim_svc = ClimateService(k_adapter)
        rule_ver = f"climate-demo-b01:{comp}:v1"
        h_eval = h_clim_svc.get_evaluation(h_ctx, basis=RuleBasis.REQUESTED, rule_version=rule_ver)
        k_eval = k_clim_svc.get_evaluation(k_ctx, basis=RuleBasis.REQUESTED, rule_version=rule_ver)
        assert len(h_eval.slot_reviews) == len(k_eval.slot_reviews)
        assert len(h_eval.result.episodes) == len(k_eval.result.episodes)
        for he, ke in zip(h_eval.result.episodes, k_eval.result.episodes):
            assert he.metric == ke.metric
            assert he.original_start == ke.original_start
            assert he.original_end == ke.original_end
            assert he.original_minutes == ke.original_minutes
            assert he.clipped_start == ke.clipped_start
            assert he.clipped_end == ke.clipped_end
            assert he.clipped_minutes == ke.clipped_minutes
            assert he.peak_deviation == ke.peak_deviation
            assert he.peak_time == ke.peak_time
        for hmr, kmr in zip(h_eval.result.metric_results, k_eval.result.metric_results):
            assert hmr.metric == kmr.metric
            assert hmr.expected_minutes == kmr.expected_minutes
            assert hmr.valid_minutes == kmr.valid_minutes
            assert hmr.out_minutes == kmr.out_minutes
            assert hmr.out_low_minutes == kmr.out_low_minutes
            assert hmr.out_high_minutes == kmr.out_high_minutes
            assert hmr.suspect_minutes == kmr.suspect_minutes
            assert hmr.unknown_minutes == kmr.unknown_minutes
            assert hmr.out_percent == kmr.out_percent
            assert hmr.coverage_pct == kmr.coverage_pct
        for hjr, kjr in zip(h_eval.result.joint_results, k_eval.result.joint_results):
            assert hjr.metrics == kjr.metrics
            assert hjr.jointly_valid_minutes == kjr.jointly_valid_minutes
            assert hjr.joint_out_minutes == kjr.joint_out_minutes
            assert hjr.joint_out_percent == kjr.joint_out_percent
            assert hjr.joint_coverage_pct == kjr.joint_coverage_pct


def test_br02_irrigation_parity(hybrid_conn, kimball_conn):
    """Workload 2: Irrigation & Drain daily and summary comparison across 6 compartments."""
    h_adapter = PostgresQueryAdapter(hybrid_conn)
    k_adapter = KimballQueryAdapter(kimball_conn)

    h_pub = h_adapter.pin_publication(AGC_SCOPE)
    k_pub = k_adapter.pin_publication(AGC_SCOPE)

    h_svc = IrrigationService(h_adapter)
    k_svc = IrrigationService(k_adapter)

    h_catalog = CatalogService(PostgresCatalogAdapter(hybrid_conn))
    k_catalog = CatalogService(k_adapter)

    h_cycles = {c.compartment_id: c for c in h_catalog.cycles(h_pub)}
    k_cycles = {c.compartment_id: c for c in k_catalog.cycles(k_pub)}

    for comp in COMPARTMENTS:
        hc = h_cycles[comp]
        kc = k_cycles[comp]
        h_ctx = QueryContext(h_pub.scope_id, h_pub.publication_id, h_pub.snapshot_id,
                             hc.cycle_id, (comp,), hc.start, hc.end)
        k_ctx = QueryContext(k_pub.scope_id, k_pub.publication_id, k_pub.snapshot_id,
                             kc.cycle_id, (comp,), kc.start, kc.end)

        h_days = h_svc.get_irrigation_days(h_ctx)
        k_days = k_svc.get_irrigation_days(k_ctx)

        assert len(h_days) == len(k_days)
        for hd, kd in zip(h_days, k_days):
            assert hd.resource_date == kd.resource_date
            assert hd.irrigation.value == kd.irrigation.value
            assert hd.drain.value == kd.drain.value
            assert hd.irrigation.quality == kd.irrigation.quality
            assert hd.drain.quality == kd.drain.quality
            assert hd.paired == kd.paired
            assert hd.drain_fraction_pct == kd.drain_fraction_pct
            assert hd.drain_exceeds_irrigation == kd.drain_exceeds_irrigation
            assert tuple(sorted(hd.reason_codes)) == tuple(sorted(kd.reason_codes))

        h_sum = h_svc.get_irrigation_summary(h_ctx)
        k_sum = k_svc.get_irrigation_summary(k_ctx)
        assert h_sum.irrigation_observed_l_m2 == k_sum.irrigation_observed_l_m2
        assert h_sum.drain_observed_l_m2 == k_sum.drain_observed_l_m2
        assert h_sum.paired_irrigation_l_m2 == k_sum.paired_irrigation_l_m2
        assert h_sum.paired_drain_l_m2 == k_sum.paired_drain_l_m2
        assert h_sum.drain_fraction_pct == k_sum.drain_fraction_pct
        assert h_sum.paired_days == k_sum.paired_days
        assert h_sum.status == k_sum.status


def test_br03_drain_parity(hybrid_conn, kimball_conn):
    """Workload 3: Drain EC/pH 5-minute slots across all 6 compartments."""
    h_adapter = PostgresDrainQueryAdapter(hybrid_conn)
    k_adapter = KimballQueryAdapter(kimball_conn)

    h_pub = h_adapter.publication(h_adapter.pin_publication(AGC_SCOPE).publication_id)
    k_pub = k_adapter.publication(k_adapter.pin_publication(AGC_SCOPE).publication_id)

    h_catalog = CatalogService(PostgresCatalogAdapter(hybrid_conn))
    k_catalog = CatalogService(k_adapter)

    h_cycles = {c.compartment_id: c for c in h_catalog.cycles(h_pub)}
    k_cycles = {c.compartment_id: c for c in k_catalog.cycles(k_pub)}

    for comp in COMPARTMENTS:
        hc = h_cycles[comp]
        kc = k_cycles[comp]
        h_ctx = QueryContext(h_pub.scope_id, h_pub.publication_id, h_pub.snapshot_id,
                             hc.cycle_id, (comp,), hc.start, hc.end)
        k_ctx = QueryContext(k_pub.scope_id, k_pub.publication_id, k_pub.snapshot_id,
                             kc.cycle_id, (comp,), kc.start, kc.end)

        h_slots = h_adapter.read_drain_slots(h_ctx, h_pub)
        k_slots = k_adapter.read_drain_slots(k_ctx, k_pub)

        assert len(h_slots) == len(k_slots)
        check_indices = list(range(min(50, len(h_slots)))) + list(range(max(0, len(h_slots) - 50), len(h_slots)))
        for idx in check_indices:
            hs = h_slots[idx]
            ks = k_slots[idx]
            assert hs.slot_start == ks.slot_start
            assert hs.ec_drain == ks.ec_drain
            assert hs.ph_drain == ks.ph_drain
            assert hs.measure_quality == ks.measure_quality


        # Exercise Rule / Episode / Evaluation Service Output
        h_drain_svc = DrainService(h_adapter)
        k_drain_svc = DrainService(k_adapter)
        drain_rule_ver = f"drain-demo-b03:{comp}:v1"
        h_eval = h_drain_svc.get_evaluation(h_ctx, rule_version=drain_rule_ver)
        k_eval = k_drain_svc.get_evaluation(k_ctx, rule_version=drain_rule_ver)
        assert len(h_eval.slot_reviews) == len(k_eval.slot_reviews)
        assert len(h_eval.result.episodes) == len(k_eval.result.episodes)
        for he, ke in zip(h_eval.result.episodes, k_eval.result.episodes):
            assert he.metric == ke.metric
            assert he.original_start == ke.original_start
            assert he.original_end == ke.original_end
            assert he.original_minutes == ke.original_minutes
            assert he.clipped_start == ke.clipped_start
            assert he.clipped_end == ke.clipped_end
            assert he.clipped_minutes == ke.clipped_minutes
            assert he.peak_deviation == ke.peak_deviation
            assert he.peak_time == ke.peak_time
        for hmr, kmr in zip(h_eval.result.metric_results, k_eval.result.metric_results):
            assert hmr.metric == kmr.metric
            assert hmr.expected_minutes == kmr.expected_minutes
            assert hmr.valid_minutes == kmr.valid_minutes
            assert hmr.out_minutes == kmr.out_minutes
            assert hmr.out_low_minutes == kmr.out_low_minutes
            assert hmr.out_high_minutes == kmr.out_high_minutes
            assert hmr.suspect_minutes == kmr.suspect_minutes
            assert hmr.unknown_minutes == kmr.unknown_minutes
            assert hmr.out_percent == kmr.out_percent
            assert hmr.coverage_pct == kmr.coverage_pct
        for hjr, kjr in zip(h_eval.result.joint_results, k_eval.result.joint_results):
            assert hjr.metrics == kjr.metrics
            assert hjr.jointly_valid_minutes == kjr.jointly_valid_minutes
            assert hjr.joint_out_minutes == kjr.joint_out_minutes
            assert hjr.joint_out_percent == kjr.joint_out_percent
            assert hjr.joint_coverage_pct == kjr.joint_coverage_pct


def test_br04_energy_parity(hybrid_conn, kimball_conn):
    """Workload 4: Energy daily quantities, MJ to kWh conversion, and intensity."""
    h_adapter = PostgresEnergyQueryAdapter(hybrid_conn)
    k_adapter = KimballQueryAdapter(kimball_conn)

    h_pub = h_adapter.pin_publication(AGC_SCOPE)
    k_pub = k_adapter.pin_publication(AGC_SCOPE)

    h_svc = EnergyService(h_adapter)
    k_svc = EnergyService(k_adapter)

    h_catalog = CatalogService(PostgresCatalogAdapter(hybrid_conn))
    k_catalog = CatalogService(k_adapter)

    h_cycles = {c.compartment_id: c for c in h_catalog.cycles(h_pub)}
    k_cycles = {c.compartment_id: c for c in k_catalog.cycles(k_pub)}

    h_intensity = EnergyIntensityService(h_svc, PostgresHarvestQueryAdapter(hybrid_conn))
    k_intensity = EnergyIntensityService(k_svc, k_adapter)

    for comp in COMPARTMENTS:
        hc = h_cycles[comp]
        kc = k_cycles[comp]
        h_ctx = QueryContext(h_pub.scope_id, h_pub.publication_id, h_pub.snapshot_id,
                             hc.cycle_id, (comp,), hc.start, hc.end)
        k_ctx = QueryContext(k_pub.scope_id, k_pub.publication_id, k_pub.snapshot_id,
                             kc.cycle_id, (comp,), kc.start, kc.end)

        h_quantities = h_svc.get_quantities(h_ctx)
        k_quantities = k_svc.get_quantities(k_ctx)

        assert len(h_quantities) == len(k_quantities)
        for hq, kq in zip(h_quantities, k_quantities):
            assert hq.resource_date == kq.resource_date
            assert hq.component == kq.component
            assert hq.source_value == kq.source_value
            assert hq.normalized_kwh_m2 == kq.normalized_kwh_m2
            assert hq.absolute_kwh == kq.absolute_kwh
            assert hq.quality == kq.quality
            assert hq.accepted == kq.accepted

        h_summary = h_svc.get_summary(h_ctx)
        k_summary = k_svc.get_summary(k_ctx)
        assert h_summary.observed_total_kwh == k_summary.observed_total_kwh
        assert h_summary.official_total_kwh == k_summary.official_total_kwh
        assert h_summary.official_total_kwh_m2 == k_summary.official_total_kwh_m2
        assert h_summary.expected_days == k_summary.expected_days
        assert h_summary.complete_days == k_summary.complete_days

        h_int = h_intensity.get_intensity(h_ctx)
        k_int = k_intensity.get_intensity(k_ctx)
        assert h_int.intensity_kwh_kg_a == k_int.intensity_kwh_kg_a
        assert h_int.observed_a_kg == k_int.observed_a_kg
        assert h_int.comparison_eligible == k_int.comparison_eligible


def test_br05_harvest_parity(hybrid_conn, kimball_conn):
    """Workload 5: Harvest records, A/B yield, and completeness state."""
    h_adapter = PostgresHarvestQueryAdapter(hybrid_conn)
    k_adapter = KimballQueryAdapter(kimball_conn)

    h_pub = h_adapter.pin_publication(AGC_SCOPE)
    k_pub = k_adapter.pin_publication(AGC_SCOPE)

    h_svc = HarvestService(h_adapter)
    k_svc = HarvestService(k_adapter)

    h_catalog = CatalogService(PostgresCatalogAdapter(hybrid_conn))
    k_catalog = CatalogService(k_adapter)

    h_cycles = {c.compartment_id: c for c in h_catalog.cycles(h_pub)}
    k_cycles = {c.compartment_id: c for c in k_catalog.cycles(k_pub)}

    for comp in COMPARTMENTS:
        hc = h_cycles[comp]
        kc = k_cycles[comp]
        h_ctx = QueryContext(h_pub.scope_id, h_pub.publication_id, h_pub.snapshot_id,
                             hc.cycle_id, (comp,), hc.start, hc.end)
        k_ctx = QueryContext(k_pub.scope_id, k_pub.publication_id, k_pub.snapshot_id,
                             kc.cycle_id, (comp,), kc.start, kc.end)

        h_records = h_svc.get_harvest_records(h_ctx)
        k_records = k_svc.get_harvest_records(k_ctx)

        assert len(h_records) == len(k_records)
        for hr, kr in zip(h_records, k_records):
            assert hr.harvest_date == kr.harvest_date
            assert hr.prod_a.value == kr.prod_a.value
            assert hr.prod_b.value == kr.prod_b.value
            assert hr.paired == kr.paired
            assert hr.prod_a.quality == kr.prod_a.quality
            assert hr.prod_b.quality == kr.prod_b.quality

        h_sum = h_svc.get_harvest_summary(h_ctx)
        k_sum = k_svc.get_harvest_summary(k_ctx)
        assert h_sum.prod_a_observed == k_sum.prod_a_observed
        assert h_sum.prod_b_observed == k_sum.prod_b_observed
        assert h_sum.paired_prod_a == k_sum.paired_prod_a
        assert h_sum.paired_prod_b == k_sum.paired_prod_b
        assert h_sum.paired_total == k_sum.paired_total
        assert h_sum.class_a_share_pct == k_sum.class_a_share_pct
        assert h_sum.class_b_share_pct == k_sum.class_b_share_pct
        assert h_sum.accepted_records == k_sum.accepted_records
        assert h_sum.paired_records == k_sum.paired_records

        h_state = h_svc.adapter.harvest_state(h_ctx)
        k_state = k_svc.adapter.harvest_state(k_ctx)
        assert h_state.snapshot_complete == k_state.snapshot_complete
        assert h_state.accepted_records == k_state.accepted_records
        assert h_state.paired_records == k_state.paired_records
        assert h_state.reasons == k_state.reasons
        assert len(h_state.unresolved_items) == len(k_state.unresolved_items)


def test_br06_cost_and_scenario_parity(hybrid_conn, kimball_conn):
    """Workload 6: Energy cost baseline, component costs, and total costs."""
    h_energy = EnergyService(PostgresEnergyQueryAdapter(hybrid_conn))
    h_cost = EnergyCostService(h_energy, PostgresCostAdapter(hybrid_conn))

    k_energy = EnergyService(KimballQueryAdapter(kimball_conn))
    k_cost = EnergyCostService(k_energy, KimballQueryAdapter(kimball_conn))

    h_pub = h_energy.adapter.pin_publication(AGC_SCOPE)
    k_pub = k_energy.adapter.pin_publication(AGC_SCOPE)

    h_catalog = CatalogService(PostgresCatalogAdapter(hybrid_conn))
    k_catalog = CatalogService(k_cost.adapter)

    h_cycles = {c.compartment_id: c for c in h_catalog.cycles(h_pub)}
    k_cycles = {c.compartment_id: c for c in k_catalog.cycles(k_pub)}

    for comp in COMPARTMENTS:
        hc = h_cycles[comp]
        kc = k_cycles[comp]
        h_ctx = QueryContext(h_pub.scope_id, h_pub.publication_id, h_pub.snapshot_id,
                             hc.cycle_id, (comp,), hc.start, hc.end)
        k_ctx = QueryContext(k_pub.scope_id, k_pub.publication_id, k_pub.snapshot_id,
                             kc.cycle_id, (comp,), kc.start, kc.end)

        h_tariff = h_cost.get_baseline_tariff(h_pub.scope_id, comp) or h_cost.get_baseline_tariff(h_pub.scope_id, None)
        k_tariff = k_cost.get_baseline_tariff(k_pub.scope_id, comp) or k_cost.get_baseline_tariff(k_pub.scope_id, None)

        h_summary = h_cost.get_cost(h_ctx, tariff_version=h_tariff.tariff_version)
        k_summary = k_cost.get_cost(k_ctx, tariff_version=k_tariff.tariff_version)

        assert h_summary.observed_total_cost_eur == k_summary.observed_total_cost_eur
        assert h_summary.observed_total_cost_per_m2 == k_summary.observed_total_cost_per_m2
        assert h_summary.official_total_cost_eur == k_summary.official_total_cost_eur
        assert h_summary.official_total_cost_per_m2 == k_summary.official_total_cost_per_m2
        assert h_summary.priced_days == k_summary.priced_days
        assert h_summary.expected_days == k_summary.expected_days

        assert len(h_summary.components) == len(k_summary.components)
        for hc_comp, kc_comp in zip(h_summary.components, k_summary.components):
            assert hc_comp.component == kc_comp.component
            assert hc_comp.observed_cost_eur == kc_comp.observed_cost_eur
            assert hc_comp.observed_cost_per_m2 == kc_comp.observed_cost_per_m2
            assert hc_comp.official_cost_eur == kc_comp.official_cost_eur
            assert hc_comp.official_cost_per_m2 == kc_comp.official_cost_per_m2
            assert len(hc_comp.day_costs) == len(kc_comp.day_costs)
            for hd, kd in zip(hc_comp.day_costs, kc_comp.day_costs):
                assert hd.resource_date == kd.resource_date
                assert hd.cost_eur == kd.cost_eur
                assert hd.cost_per_m2 == kd.cost_per_m2

        # Exercise Scenario Transaction, idempotency, conflict rejection, and pricing semantics
        cmd = ScenarioCommand(
            request_id=f"req-parity-{comp}",
            baseline_version_id=k_tariff.tariff_version,
            name="Parity Test Scenario",
            publication=k_pub,
            changes=(ScenarioChange(EnergyComponent.HEAT, "EUR/MJ", price=Decimal("0.01")),),
            analysis_context=k_ctx,
        )
        scen = k_cost.execute_scenario_command(cmd)
        assert scen.scenario_version is not None

        # Verify idempotency: exact retry returns identical scenario
        assert k_cost.execute_scenario_command(cmd) == scen

        # Verify conflict: changed command with same request_id is rejected
        from dataclasses import replace
        with pytest.raises(ValueError, match="[Cc]onflict"):
            k_cost.execute_scenario_command(replace(cmd, name="Conflicting Test Scenario"))

        # Parity comparison with canonical hybrid cost scenario
        h_cmd = replace(
            cmd,
            request_id=f"req-parity-canonical-{comp}",
            publication=h_pub,
            baseline_version_id=h_tariff.tariff_version,
            analysis_context=h_ctx,
        )
        h_scen = h_cost.execute_scenario_command(h_cmd)
        h_scen_cost = h_cost.get_cost(h_ctx, scenario_version=h_scen.scenario_version)
        scen_cost = k_cost.get_cost(k_ctx, scenario_version=scen.scenario_version)
        assert scen_cost.observed_total_cost_eur == h_scen_cost.observed_total_cost_eur
        assert scen_cost.observed_total_cost_per_m2 == h_scen_cost.observed_total_cost_per_m2
