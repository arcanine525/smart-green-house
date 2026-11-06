"""Unit tests for Kimball dimensional contracts and transformation logic (T24)."""
from datetime import date, datetime, timezone
from decimal import Decimal
import pytest

from greenhouse.contracts import (
    Applicability, BasisStatus, CycleWindow, EnergyComponent,
    PublicationRef, QualityStatus, QueryContext, RuleBasis, Versions,
)
from greenhouse.db.climate_mart import BUNDLE_VERSION as CLIMATE_BUNDLE_VERSION
from greenhouse.db.drain_mart import BUNDLE_VERSION as DRAIN_BUNDLE_VERSION
from greenhouse.db.energy_mart import BUNDLE_VERSION as ENERGY_BUNDLE_VERSION
from greenhouse.db.harvest_mart import BUNDLE_VERSION as HARVEST_BUNDLE_VERSION
from greenhouse.db.irrigation_mart import BUNDLE_VERSION as IRRIGATION_BUNDLE_VERSION
from greenhouse.kimball.adapter import KimballQueryAdapter
from greenhouse.kimball.audit import verify_kimball_isolation
from greenhouse.kimball.etl import evaluate_quality, ratio_sig, water_percent, _index_and_classify_records
from greenhouse.kimball.schema import SCHEMA_DDL


def test_kimball_schema_ddl_structure():
    """Verify schema DDL contains all required conformed dimensions, segments, and facts."""
    assert "CREATE SCHEMA IF NOT EXISTS kimball" in SCHEMA_DDL
    assert "kimball.dim_site" in SCHEMA_DDL
    assert "kimball.dim_compartment" in SCHEMA_DDL
    assert "kimball.dim_growing_cycle" in SCHEMA_DDL
    assert "kimball.dim_date" in SCHEMA_DDL
    assert "kimball.dim_area_basis" in SCHEMA_DDL
    assert "kimball.dim_rule_schedule" in SCHEMA_DDL
    assert "kimball.dim_rule_segment" in SCHEMA_DDL
    assert "kimball.dim_tariff_profile" in SCHEMA_DDL
    assert "kimball.dim_tariff_rate" in SCHEMA_DDL
    assert "kimball.dim_scenario" in SCHEMA_DDL
    assert "kimball.fact_climate_slot" in SCHEMA_DDL
    assert "kimball.fact_drain_slot" in SCHEMA_DDL
    assert "kimball.fact_irrigation_day" in SCHEMA_DDL
    assert "kimball.fact_energy_day" in SCHEMA_DDL
    assert "kimball.fact_harvest_record" in SCHEMA_DDL
    assert "kimball.decimal_exponent" in SCHEMA_DDL
    assert "kimball.ratio_sig" in SCHEMA_DDL
    assert "core." not in SCHEMA_DDL  # Zero references to core


def test_quality_policy_evaluation():
    """Test exact AGC_QUALITY_V1 evaluation logic across edge cases."""
    # 1. Accepted positive
    s, a, r = evaluate_quality("Irr", Decimal("15.5"), [], {})
    assert (s, a, r) == ("accepted", "applicable", None)

    # 2. Nonnegative violation (Irr < 0)
    s, a, r = evaluate_quality("Irr", Decimal("-1.0"), [], {})
    assert (s, a, r) == ("invalid", "applicable", "negative_quantity")

    # 3. Missing value
    s, a, r = evaluate_quality("Drain", None, [], {"Drain": "sensor_offline"})
    assert (s, a, r) == ("missing", "unknown", "sensor_offline")

    # 4. Suspect zero (pH = 0)
    s, a, r = evaluate_quality("pH_drain_PC", Decimal("0.0"), [], {})
    assert (s, a, r) == ("suspect", "applicable", "demo_zero_ph_suspect")

    # 5. Off mode zero (co2_sp = 0)
    s, a, r = evaluate_quality("co2_sp", Decimal("0.0"), [], {})
    assert (s, a, r) == ("accepted", "unknown", "off_mode_unverified")

    # 6. Invalid numeric token from parser
    issues = [{"code": "invalid_numeric", "column": "Tair"}]
    s, a, r = evaluate_quality("Tair", None, issues, {})
    assert (s, a, r) == ("invalid", "unknown", "invalid_numeric")


def test_exact_ratio_sig_precision():
    """Verify heat energy conversion from MJ to kWh preserves exact Decimal precision."""
    val_mj = Decimal("2.8557")
    denom = Decimal("3.6")
    res = ratio_sig(val_mj, denom, 50)
    assert res == Decimal("2.8557") / Decimal("3.6")
    # Verify no float drift
    assert str(res).startswith("0.79325")


def test_kimball_audit_helper_mock():
    """Verify that audit checks query real pg_catalog columns."""
    class FakeCursor:
        def __init__(self):
            self.queries = []
        def execute(self, q, params=None):
            self.queries.append(q)
            return self
        def fetchone(self):
            if "nspname = 'kimball'" in self.queries[-1]:
                return (6,)
            return (0,)
        def fetchall(self):
            return [("fact_climate_slot",), ("fact_energy_day",)]
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    audit = verify_kimball_isolation(FakeConn())
    assert audit.is_pure_kimball is True
    assert audit.core_table_count == 0
    assert audit.core_fk_count == 0
    assert audit.foreign_table_count == 0
    assert audit.fdw_extension_count == 0


def test_kimball_adapter_interface():
    """Verify KimballQueryAdapter satisfies application service protocol methods."""
    class DummyConnection:
        info = type("Info", (), {"host": "localhost", "port": 5432, "dbname": "test", "user": "test"})()

    adapter = KimballQueryAdapter(DummyConnection())
    assert hasattr(adapter, "publication")
    assert hasattr(adapter, "cycle_window")
    assert hasattr(adapter, "read_irrigation_days")
    assert hasattr(adapter, "read_harvest_records")
    assert hasattr(adapter, "harvest_state")
    assert hasattr(adapter, "read_climate_slots")
    assert hasattr(adapter, "read_drain_slots")
    assert hasattr(adapter, "schedule")
    assert hasattr(adapter, "read_energy_quantities")
    assert hasattr(adapter, "read_production_bases")
    assert hasattr(adapter, "get_tariff")
    assert hasattr(adapter, "get_baseline_tariff")
    assert hasattr(adapter, "register_tariff")
    assert hasattr(adapter, "register_historical_tariff")
    assert hasattr(adapter, "list_scenarios")
    assert hasattr(adapter, "get_scenario")
    assert hasattr(adapter, "execute_scenario_transaction")
    assert hasattr(adapter, "validate_cycle")
    assert hasattr(adapter, "get_lineage")
    assert hasattr(adapter, "catalog_publications")
    assert hasattr(adapter, "cycles")


def test_pin_publication_gating_contract():
    """Verify pin_publication gates on validated receipts and membership without forcing complete finality."""
    class MockGatedCursor:
        def __init__(self, query_handler):
            self._query_handler = query_handler
            self._current_rows = []
        def execute(self, q, params=None):
            self._current_rows = self._query_handler(q, params)
            return self
        def fetchall(self):
            return self._current_rows
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    class MockGatedConn:
        def __init__(self, handler):
            self._handler = handler
        def cursor(self, row_factory=None):
            return MockGatedCursor(self._handler)

    # 1. Unsealed / empty receipts raises ValueError
    adapter_unsealed = KimballQueryAdapter(MockGatedConn(lambda q, p: []))
    with pytest.raises(ValueError, match="No validated sealed publication"):
        adapter_unsealed.pin_publication("scope:test")

    # 2. Provisional but validated publication with receipts succeeds
    now = datetime(2026, 11, 6, 4, 0, 0, tzinfo=timezone.utc)
    import hashlib
    expected_checksum = hashlib.sha256(b"rev-1\n").hexdigest()
    mock_pub_row = [{
        "publication_id": "pub-provisional-1",
        "data_scope_id": "scope:test",
        "snapshot_id": "snap-1",
        "run_id": "run-1",
        "quality_policy_version": "AGC_QUALITY_V1",
        "parser_version": "1.0",
        "code_version": "v1",
        "config_bundle_hash": "hash123",
        "committed_at": now,
        "finality_status": "provisional",
        "member_count": 1,
        "membership_checksum": expected_checksum,
    }]
    mock_receipts = [
        {"bundle_name": "climate", "bundle_version": CLIMATE_BUNDLE_VERSION, "item_count": 10},
        {"bundle_name": "drain", "bundle_version": DRAIN_BUNDLE_VERSION, "item_count": 10},
        {"bundle_name": "irrigation", "bundle_version": IRRIGATION_BUNDLE_VERSION, "item_count": 10},
        {"bundle_name": "energy", "bundle_version": ENERGY_BUNDLE_VERSION, "item_count": 10},
        {"bundle_name": "harvest", "bundle_version": HARVEST_BUNDLE_VERSION, "item_count": 10},
    ]

    def valid_handler(q, params):
        if "kimball.publication_receipt" in q:
            return mock_receipts
        if "fact_climate_slot" in q and "count(*)" in q:
            return [{"climate": 10, "drain": 10, "irrigation": 10, "energy": 10, "harvest": 10}]
        if "UNION" in q:
            return [{"revision_id": "rev-1"}]
        return mock_pub_row

    adapter_sealed = KimballQueryAdapter(MockGatedConn(valid_handler))
    ref = adapter_sealed.pin_publication("scope:test")
    assert ref.publication_id == "pub-provisional-1"
    assert ref.finality_status == "provisional"


def test_water_ratio_preserves_precision_before_conversion():
    """Verify water_percent preserves exact intermediate digits before 50-digit IEEE rounding."""
    import decimal
    numerator = Decimal("1.123456789012345678901234567890123456789")
    denominator = Decimal("3")
    with decimal.localcontext() as ctx:
        ctx.prec = 100
        scaled = numerator * 100
        ctx.prec = 50
        ctx.rounding = decimal.ROUND_HALF_EVEN
        expected = scaled / denominator
    assert water_percent(numerator, denominator) == expected


def test_two_pass_conflict_index_logic():
    """Verify two-pass indexing withholds conflicting keys while preserving identical duplicates."""
    records = [
        {"logical_key": "k1", "raw_tokens": ["A", "1"], "line_number": 2, "issues": []},
        {"logical_key": "k1", "raw_tokens": ["A", "1"], "line_number": 3, "issues": []},  # Duplicate
        {"logical_key": "k2", "raw_tokens": ["B", "2"], "line_number": 4, "issues": []},  # Will conflict with line 5
        {"logical_key": "k2", "raw_tokens": ["B", "99"], "line_number": 5, "issues": []}, # Conflict
        {"logical_key": "k3", "raw_tokens": ["C", "3"], "line_number": 6, "issues": [{"code": "off_grid_time"}]}, # Quarantine
    ]
    classified = _index_and_classify_records(records)
    dispositions = [disp for _, disp in classified]
    assert dispositions == ["accepted", "duplicate", "conflict", "conflict", "quarantine"]


def test_lineage_measure_units_vocabulary():
    """Verify conformed measure units match full canonical vocabulary without querying core."""
    from greenhouse.kimball.adapter import LINEAGE_MEASURE_UNITS
    assert "kimball.dim_measure_definition" in SCHEMA_DDL

    # Climate (8 measures: 2 ambient + 6 controller setpoints)
    clim = {u.measure_code: u.unit for u in LINEAGE_MEASURE_UNITS["climate"]}
    assert clim == {
        "CO2air": "ppm", "Tair": "°C", "co2_sp": "ppm", "co2_vip": "ppm",
        "t_heat_sp": "°C", "t_heat_vip": "°C", "t_vent_sp": "°C", "t_ventlee_vip": "°C",
    }

    # Drain (2 measures: source codes and dimensionless pH)
    drain = {u.measure_code: u.unit for u in LINEAGE_MEASURE_UNITS["drain"]}
    assert drain == {"EC_drain_PC": "dS/m", "pH_drain_PC": "1"}

    # Resource (6 measures: heat, electricity, CO2, irrigation, drain)
    res = {u.measure_code: u.unit for u in LINEAGE_MEASURE_UNITS["resource"]}
    assert res == {
        "CO2_cons": "kg/m²/day", "Drain": "L/m²/day", "ElecHigh": "kWh/m²/day",
        "ElecLow": "kWh/m²/day", "Heat_cons": "MJ/m²/day", "Irr": "L/m²/day",
    }

    # Harvest (2 measures: raw-source units)
    harv = {u.measure_code: u.unit for u in LINEAGE_MEASURE_UNITS["harvest"]}
    assert harv == {"ProdA": "kg/m²/harvest", "ProdB": "kg/m²/harvest"}
