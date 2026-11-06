"""Direct ETL pipeline from staging/parser streams into Kimball dimensional schema (T24).

Populates conformed dimensions and dimensional facts directly without intermediate
physical core tables (no core.record_revision, no core.resource_day, etc.).
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import decimal
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from greenhouse.contracts import (
    ASSUMPTION_LABEL, EnergyComponent, RuleBasis, RuleScheduleRevision, RuleScheduleSegment,
)
from greenhouse.db.climate_mart import BUNDLE_VERSION as CLIMATE_BUNDLE_VERSION
from greenhouse.db.demo_rules import (
    CLIMATE_DELTA_CO2, CLIMATE_DELTA_TEMP, CLIMATE_EVIDENCE, CLIMATE_FAMILY_PREFIX,
    DEMO_EC_BOUNDS, DEMO_PH_BOUNDS, DRAIN_EVIDENCE, DRAIN_FAMILY_PREFIX,
)
from greenhouse.db.drain_mart import BUNDLE_VERSION as DRAIN_BUNDLE_VERSION
from greenhouse.db.energy_mart import BUNDLE_VERSION as ENERGY_BUNDLE_VERSION, QUANTITY_VERSION
from greenhouse.db.harvest_mart import BUNDLE_VERSION as HARVEST_BUNDLE_VERSION
from greenhouse.db.irrigation_mart import BUNDLE_VERSION as IRRIGATION_BUNDLE_VERSION
from greenhouse.db.loader import digest_file
from greenhouse.ingestion.parser import PARSER_VERSION, Source, SourceParser
from greenhouse.kimball.schema import SCHEMA_DDL
from greenhouse.settings import Settings

AGC_SHA = "c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"
AGC_SNAPSHOT = "agc-2nd-2019-" + AGC_SHA
AGC_SCOPE = AGC_SNAPSHOT + ":historical"
DEFAULT_ARCHIVE_PATH = Path("/data/import/autonomous-greenhouse-challengeagc-2nd-2019.zip")
KIMBALL_DB_NAME = "greenhouse_kimball_f2b6_t24"
KIMBALL_PUBLICATION_ID = "kimball-pub-f2b6-t24-historical"
KIMBALL_RUN_ID = "kimball-run-f2b6-t24"
KIMBALL_CODE_VERSION = "kimball-pipeline-v1"

POLICY_NONNEGATIVE = {"Irr", "Drain", "ProdA", "ProdB", "EC_drain_PC"}
POLICY_OFF_MODE_ZERO = {"co2_sp", "co2_vip"}
POLICY_SUSPECT_ZERO = {"pH_drain_PC"}

CLIMATE_MEASURES = (
    "Tair", "CO2air", "t_heat_sp", "t_vent_sp", "co2_sp",
    "t_heat_vip", "t_ventlee_vip", "co2_vip",
)
DRAIN_MEASURES = ("EC_drain_PC", "pH_drain_PC")
RESOURCE_MEASURES = ("Heat_cons", "ElecHigh", "ElecLow", "CO2_cons", "Irr", "Drain")
HARVEST_MEASURES = ("ProdA", "ProdB")

COMPARTMENTS = (
    ("AICU", "AICU"),
    ("Automatoes", "Automatoes"),
    ("Digilog", "Digilog"),
    ("IUACAAS", "IUACAAS"),
    ("Reference", "Reference"),
    ("TheAutomators", "TheAutomators"),
)

REQUIRED_CORE_FILES = frozenset(
    f"{comp_id}/{fname}"
    for comp_id, _ in COMPARTMENTS
    for fname in ("GreenhouseClimate.csv", "Resources.csv", "Production.csv")
)

HISTORICAL_WINDOW_START = date(2019, 12, 16)
HISTORICAL_WINDOW_END = date(2020, 5, 30)


def evaluate_quality(measure_code: str, value: Decimal | None, issues: list[dict], null_reasons: dict):
    """Exact AGC_QUALITY_V1 evaluation identical to canonical SQL logic."""
    has_invalid_num = any(
        issue.get("code") == "invalid_numeric" and issue.get("column") == measure_code
        for issue in issues
    )
    if has_invalid_num:
        status = "invalid"
    elif value is None:
        status = "missing"
    elif measure_code in POLICY_NONNEGATIVE and value < 0:
        status = "invalid"
    elif measure_code in POLICY_SUSPECT_ZERO and value == 0:
        status = "suspect"
    else:
        status = "accepted"

    if value is None or (measure_code in POLICY_OFF_MODE_ZERO and value == 0):
        applicability = "unknown"
    else:
        applicability = "applicable"

    if has_invalid_num:
        reason = "invalid_numeric"
    elif value is None:
        reason = null_reasons.get(measure_code, "missing_source_value")
    elif measure_code in POLICY_NONNEGATIVE and value < 0:
        reason = "negative_quantity"
    elif measure_code in POLICY_SUSPECT_ZERO and value == 0:
        reason = "demo_zero_ph_suspect"
    elif measure_code in POLICY_OFF_MODE_ZERO and value == 0:
        reason = "off_mode_unverified"
    else:
        reason = None

    return status, applicability, reason


def ratio_sig(n: Decimal | None, d: Decimal | None, p: int = 50) -> Decimal | None:
    """Exact 50-significant IEEE round half even conversion matching mart.ratio_sig."""
    if n is None or d is None or d <= 0 or n < 0:
        return None
    if n == 0:
        return Decimal(0)
    with decimal.localcontext() as ctx:
        ctx.prec = p
        ctx.rounding = decimal.ROUND_HALF_EVEN
        return n / d


def water_percent(n: Decimal | None, d: Decimal | None) -> Decimal | None:
    """Exact water percent matching mart.water_percent plpgsql function."""
    if n is None or d is None or d <= 0 or n < 0:
        return None
    if n == 0:
        return Decimal(0)
    with decimal.localcontext() as ctx:
        ctx.prec = 100
        scaled = n * Decimal(100)
        ctx.prec = 50
        ctx.rounding = decimal.ROUND_HALF_EVEN
        return scaled / d


def create_isolated_database(admin_settings: Settings, db_name: str = KIMBALL_DB_NAME) -> None:
    """Create isolated database via admin connection, failing on collision."""
    kwargs = admin_settings.connection_kwargs()
    kwargs["dbname"] = "postgres"
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone() is not None:
                raise RuntimeError(f"Database collision: isolated database '{db_name}' already exists.")
            cur.execute(f'CREATE DATABASE "{db_name}"')


def populate_kimball_dimensions(conn, raw_root: Path, scope_id: str = AGC_SCOPE) -> None:
    """Populate conformed dimensions in kimball schema."""
    with conn.transaction():
        # Site
        conn.execute(
            "INSERT INTO kimball.dim_site VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            ("bleiswijk", "WUR Bleiswijk Greenhouse", "source_naive"),
        )

        # Compartments & Cycles
        start_ts = datetime(2019, 12, 16, 0, 0, 0)
        end_ts = datetime(2020, 5, 30, 0, 0, 0)
        for comp_id, comp_name in COMPARTMENTS:
            conn.execute(
                "INSERT INTO kimball.dim_compartment VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (comp_id, "bleiswijk", comp_name),
            )
            cycle_id = f"agc-cycle-{comp_id}"
            conn.execute(
                "INSERT INTO kimball.dim_growing_cycle VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (cycle_id, comp_id, start_ts, end_ts, comp_id),
            )

        # Calendar Dates (2019-12-16 to 2020-05-30)
        curr = HISTORICAL_WINDOW_START
        while curr < HISTORICAL_WINDOW_END:
            conn.execute(
                """
                INSERT INTO kimball.dim_date VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (resource_date) DO NOTHING
                """,
                (curr, curr.isocalendar()[0], curr.isocalendar()[1], curr.isocalendar()[2],
                 curr.timetuple().tm_yday, curr.month, curr.year),
            )
            curr += timedelta(days=1)

        # Area Basis: ALL SIX compartments have 62.5 m² as assumed_unverified (AGC_SHARED_BASIS_V1)
        measures = ("Irr", "Drain", "Heat_cons", "ElecHigh", "ElecLow", "ProdA", "ProdB")
        for comp_id, _ in COMPARTMENTS:
            for m in measures:
                basis_id = f"AGC_SHARED_BASIS_V1:{comp_id}:{m}"
                conn.execute(
                    """
                    INSERT INTO kimball.dim_area_basis
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (basis_version_id) DO NOTHING
                    """,
                    (
                        basis_id, scope_id, comp_id, m, 1, Decimal("62.5"),
                        "growing_area_assumed", "assumed_unverified",
                        HISTORICAL_WINDOW_START, HISTORICAL_WINDOW_END,
                        "AGC_SHARED_BASIS_V1", 1,
                        "Irr/Drain/Heat_cons/ElecHigh/ElecLow/ProdA/ProdB share growing-area per-m² basis 62.5 m²",
                        "ReadMe.pdf p.1/p.9 + user 2026-11-04; resource extension unverified",
                    ),
                )

        # Approved Rule Schedules from demo_rules.py
        # 1. Climate demo schedules (BR-01)
        for comp_id, _ in COMPARTMENTS:
            rule_ver = f"{CLIMATE_FAMILY_PREFIX}:{comp_id}:v1"
            rule_id = f"{CLIMATE_FAMILY_PREFIX}:{comp_id}"
            conn.execute(
                """
                INSERT INTO kimball.dim_rule_schedule
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rule_version) DO NOTHING
                """,
                (rule_ver, rule_id, 1, scope_id, comp_id, CLIMATE_EVIDENCE, True, "demo_rule"),
            )
            # Segments: temp and co2 on both requested and realized bases
            for basis in (RuleBasis.REQUESTED, RuleBasis.REALIZED):
                conn.execute(
                    """
                    INSERT INTO kimball.dim_rule_segment
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (rule_version, metric, measurement_context, basis, source_start, source_end) DO NOTHING
                    """,
                    (rule_ver, "temperature", "climate", basis.value,
                     start_ts, end_ts, CLIMATE_DELTA_TEMP, None, None),
                )
                conn.execute(
                    """
                    INSERT INTO kimball.dim_rule_segment
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (rule_version, metric, measurement_context, basis, source_start, source_end) DO NOTHING
                    """,
                    (rule_ver, "co2", "climate", basis.value,
                     start_ts, end_ts, CLIMATE_DELTA_CO2, None, None),
                )

            # 2. Drain demo schedules (BR-03)
            drain_ver = f"{DRAIN_FAMILY_PREFIX}:{comp_id}:v1"
            drain_id = f"{DRAIN_FAMILY_PREFIX}:{comp_id}"
            conn.execute(
                """
                INSERT INTO kimball.dim_rule_schedule
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rule_version) DO NOTHING
                """,
                (drain_ver, drain_id, 1, scope_id, comp_id, DRAIN_EVIDENCE, True, "demo_rule"),
            )
            # Segments: ec [2, 5] and ph [5.5, 6.5] on measured basis
            conn.execute(
                """
                INSERT INTO kimball.dim_rule_segment
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rule_version, metric, measurement_context, basis, source_start, source_end) DO NOTHING
                """,
                (drain_ver, "ec", "drain_process_computer", RuleBasis.MEASURED.value,
                 start_ts, end_ts, None, DEMO_EC_BOUNDS[0], DEMO_EC_BOUNDS[1]),
            )
            conn.execute(
                """
                INSERT INTO kimball.dim_rule_segment
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rule_version, metric, measurement_context, basis, source_start, source_end) DO NOTHING
                """,
                (drain_ver, "ph", "drain_process_computer", RuleBasis.MEASURED.value,
                 start_ts, end_ts, None, DEMO_PH_BOUNDS[0], DEMO_PH_BOUNDS[1]),
            )

        # 3. Harvest demo rule profiles (BR-05)
        harvest_evidence = (
            "Synthetic demo per docs/br/br_05_harvest-quality-tracking.md §4.2/AC-08: "
            "10% class-B share boundary example; explicitly NOT an AGC standard threshold. "
            "Supersedes the earlier unsupported demo wording (v1)."
        )
        for comp_id, _ in COMPARTMENTS:
            h_ver = f"harvest-demo-b10:{comp_id}:v2"
            h_id = f"harvest-demo-b10:{comp_id}"
            conn.execute(
                """
                INSERT INTO kimball.dim_harvest_rule
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rule_version) DO NOTHING
                """,
                (
                    h_ver, h_id, 2, scope_id, comp_id, Decimal("10"),
                    harvest_evidence, True, "demo_rule", "kg/m²", "%",
                ),
            )

        # Baseline Tariffs from Economics.pdf
        tariff_ver = f"tariff:AGC_2020_REFERENCE_V1:{scope_id}:v1"
        conn.execute(
            """
            INSERT INTO kimball.dim_tariff_profile
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tariff_version) DO NOTHING
            """,
            (tariff_ver, "AGC_2020_REFERENCE_V1", 1, scope_id, "EUR",
             "Economics.pdf pages 1-2", "AGC 2020 Reference Tariff", True, None, None),
        )
        rates = (
            (f"rate:{tariff_ver}:heat", tariff_ver, "heat", "EUR", "EUR/MJ", Decimal("0.0083"),
             HISTORICAL_WINDOW_START, HISTORICAL_WINDOW_END, "Economics.pdf page 2: Heat 0.0083 EUR/MJ"),
            (f"rate:{tariff_ver}:elec_high", tariff_ver, "elec_high", "EUR", "EUR/kWh", Decimal("0.08"),
             HISTORICAL_WINDOW_START, HISTORICAL_WINDOW_END, "Economics.pdf page 1: ElecHigh 0.08 EUR/kWh"),
            (f"rate:{tariff_ver}:elec_low", tariff_ver, "elec_low", "EUR", "EUR/kWh", Decimal("0.04"),
             HISTORICAL_WINDOW_START, HISTORICAL_WINDOW_END, "Economics.pdf page 2: ElecLow 0.04 EUR/kWh"),
        )
        for r in rates:
            conn.execute(
                """
                INSERT INTO kimball.dim_tariff_rate
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rate_id) DO NOTHING
                """,
                r,
            )

        # Per-compartment scoped baselines
        for comp_id, _ in COMPARTMENTS:
            c_tariff_ver = f"tariff:AGC_2020_REFERENCE_V1:{scope_id}:{comp_id}:v1"
            conn.execute(
                """
                INSERT INTO kimball.dim_tariff_profile
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tariff_version) DO NOTHING
                """,
                (c_tariff_ver, "AGC_2020_REFERENCE_V1", 1, scope_id, "EUR",
                 "Economics.pdf pages 1-2", "AGC 2020 Reference Tariff", True, comp_id, None),
            )
            c_rates = (
                (f"rate:{c_tariff_ver}:heat", c_tariff_ver, "heat", "EUR", "EUR/MJ", Decimal("0.0083"),
                 HISTORICAL_WINDOW_START, HISTORICAL_WINDOW_END, "Economics.pdf page 2: Heat 0.0083 EUR/MJ"),
                (f"rate:{c_tariff_ver}:elec_high", c_tariff_ver, "elec_high", "EUR", "EUR/kWh", Decimal("0.08"),
                 HISTORICAL_WINDOW_START, HISTORICAL_WINDOW_END, "Economics.pdf page 1: ElecHigh 0.08 EUR/kWh"),
                (f"rate:{c_tariff_ver}:elec_low", c_tariff_ver, "elec_low", "EUR", "EUR/kWh", Decimal("0.04"),
                 HISTORICAL_WINDOW_START, HISTORICAL_WINDOW_END, "Economics.pdf page 2: ElecLow 0.04 EUR/kWh"),
            )
            for cr in c_rates:
                conn.execute(
                    """
                    INSERT INTO kimball.dim_tariff_rate
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (rate_id) DO NOTHING
                    """,
                    cr,
                )

        measure_defs = (
            ("Tair", "climate", "°C", "ReadMe.pdf pp.3,5,7"),
            ("CO2air", "climate", "ppm", "ReadMe.pdf pp.3,5,7"),
            ("t_heat_sp", "climate", "°C", "ReadMe.pdf pp.3,5,7"),
            ("t_vent_sp", "climate", "°C", "ReadMe.pdf pp.3,5,7"),
            ("co2_sp", "climate", "ppm", "ReadMe.pdf pp.3,5,7"),
            ("t_heat_vip", "climate", "°C", "ReadMe.pdf pp.3,5,7"),
            ("t_ventlee_vip", "climate", "°C", "ReadMe.pdf pp.3,5,7"),
            ("co2_vip", "climate", "ppm", "ReadMe.pdf pp.3,5,7"),
            ("EC_drain_PC", "drain", "dS/m", "ReadMe.pdf pp.4"),
            ("pH_drain_PC", "drain", "1", "ReadMe.pdf pp.4"),
            ("Heat_cons", "resource", "MJ/m²/day", "ReadMe.pdf pp.12–13"),
            ("ElecHigh", "resource", "kWh/m²/day", "ReadMe.pdf pp.12–13"),
            ("ElecLow", "resource", "kWh/m²/day", "ReadMe.pdf pp.12–13"),
            ("CO2_cons", "resource", "kg/m²/day", "ReadMe.pdf pp.12–13"),
            ("Irr", "resource", "L/m²/day", "ReadMe.pdf pp.12–13"),
            ("Drain", "resource", "L/m²/day", "ReadMe.pdf pp.12–13"),
            ("ProdA", "harvest", "kg/m²/harvest", "ReadMe.pdf pp.9"),
            ("ProdB", "harvest", "kg/m²/harvest", "ReadMe.pdf pp.9"),
        )
        for mrow in measure_defs:
            conn.execute(
                """
                INSERT INTO kimball.dim_measure_definition
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (measure_code) DO NOTHING
                """,
                mrow,
            )


def _index_and_classify_records(records: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Two-pass conflict and quarantine index implementing canonical ingest_file semantics.

    Pass 1: Build key index. Colliding keys with different token content are marked conflict.
    Pass 2: Classify disposition (conflict, quarantine, duplicate, accepted).
    """
    index: dict[str, list[Any]] = {}
    for rec in records:
        key = rec.get("logical_key")
        if key is not None:
            content = hashlib.sha256(json.dumps(rec["raw_tokens"], ensure_ascii=False).encode()).hexdigest()
            if key not in index:
                index[key] = [content, rec["line_number"], False]
            elif index[key][0] != content:
                index[key][2] = True

    classified: list[tuple[dict[str, Any], str]] = []
    for rec in records:
        key = rec.get("logical_key")
        if key is not None and index[key][2]:
            disposition = "conflict"
        elif rec.get("issues"):
            disposition = "quarantine"
        elif key is not None and index[key][1] != rec["line_number"]:
            disposition = "duplicate"
        else:
            disposition = "accepted"
        classified.append((rec, disposition))
    return classified


def load_kimball_facts_from_raw(
    conn, raw_root: Path,
    publication_id: str = KIMBALL_PUBLICATION_ID,
    run_id: str = KIMBALL_RUN_ID,
    code_version: str = KIMBALL_CODE_VERSION,
    scope_id: str = AGC_SCOPE,
    archive_path: Path | str | None = DEFAULT_ARCHIVE_PATH,
) -> dict[str, int]:
    """Stream directly from raw snapshot files into kimball fact tables.

    No physical core intermediate tables are created or written.
    Validates manifest and required file checksums before mutation.
    Enforces publication immutability: rejects write if publication_id exists.
    Atomically seals the publication with validated receipts, truthful
    membership checksum, and config bundle hash only upon successful fact loading.
    Analytical finality remains 'provisional' matching AGC scope truth.
    """
    # 1. Mandatory locked AGC archive and extracted snapshot verification via canonical functions
    if archive_path is None:
        raise ValueError("archive_path is required for locked AGC source verification")
    archive_file = Path(archive_path)
    if not archive_file.exists():
        raise FileNotFoundError(f"Locked AGC archive not found: {archive_path}")

    import zipfile
    from greenhouse.ingestion.snapshot import verify_archive, validate_snapshot, archive_members
    verify_archive(archive_file)
    with zipfile.ZipFile(archive_file) as archive:
        validate_snapshot(raw_root, archive, archive_members(archive))

    snapshot_manifest_path = raw_root / "manifest.json"
    if not snapshot_manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest.json in raw root: {raw_root}")
    manifest = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))

    # 2. Pinned source identity check: verify snapshot and archive SHA against locked constants
    snapshot_id = manifest.get("snapshot_id")
    archive_sha = manifest.get("archive", {}).get("sha256")
    if snapshot_id != AGC_SNAPSHOT or archive_sha != AGC_SHA:
        raise ValueError(
            f"Raw snapshot manifest identity mismatch: expected ({AGC_SNAPSHOT}, {AGC_SHA}), "
            f"got ({snapshot_id}, {archive_sha})"
        )

    declared_files = manifest.get("files", [])
    if not declared_files:
        raise ValueError("Raw manifest has no declared files")

    declared_paths = {f["path"]: f for f in declared_files if "path" in f}
    missing_manifest_inventory = REQUIRED_CORE_FILES - set(declared_paths.keys())
    if missing_manifest_inventory:
        raise ValueError(
            f"Raw snapshot manifest is missing required inventory files: {sorted(missing_manifest_inventory)}"
        )

    # 3. Validate required source file inventory and hashes on disk BEFORE any database mutation
    file_checksums: dict[str, str] = {}
    verified_source_files: list[tuple] = []
    now_utc = datetime.now(timezone.utc)
    for f in declared_files:
        rel_path = f["path"]
        if Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
            raise ValueError(f"Unsafe path in manifest: {rel_path}")
        file_path = raw_root / rel_path
        if not file_path.exists():
            raise FileNotFoundError(f"Required source file '{rel_path}' missing from raw snapshot")
        actual_sha = digest_file(file_path)
        if actual_sha != f["sha256"]:
            raise ValueError(f"File checksum mismatch for '{rel_path}': expected {f['sha256']}, got {actual_sha}")
        file_checksums[rel_path] = f["sha256"]
        verified_source_files.append((
            publication_id, snapshot_id, archive_sha, rel_path,
            f["sha256"], f.get("size_bytes"), f.get("csv", {}).get("data_rows"),
            now_utc,
        ))

    # Also verify all REQUIRED_CORE_FILES exist on disk
    for rel_path in REQUIRED_CORE_FILES:
        file_path = raw_root / rel_path
        if not file_path.exists():
            raise FileNotFoundError(f"Required core inventory file '{rel_path}' missing from raw snapshot disk")

    snapshot_sha = archive_sha

    # 3. Check publication immutability: reject write if publication_id already exists
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM kimball.publication WHERE publication_id = %s", (publication_id,))
        if cur.fetchone() is not None:
            raise ValueError(f"Publication '{publication_id}' already exists and is immutable; cannot overwrite.")

    config_payload = {
        "quality_policy": "AGC_QUALITY_V1",
        "parser_version": PARSER_VERSION,
        "code_version": code_version,
        "area_basis": "AGC_SHARED_BASIS_V1",
        "scope_id": scope_id,
        "snapshot_id": snapshot_id,
        "snapshot_sha": snapshot_sha,
    }
    config_bundle_hash = hashlib.sha256(
        json.dumps(config_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    row_counts = {
        "climate_slots": 0,
        "drain_slots": 0,
        "irrigation_days": 0,
        "energy_days": 0,
        "harvest_records": 0,
    }

    irr_rows: list[tuple] = []
    energy_rows: list[tuple] = []
    harv_rows: list[tuple] = []
    climate_rows: list[tuple] = []
    drain_rows: list[tuple] = []
    quarantine_rows: list[tuple] = []
    harvest_state_rows: list[tuple] = []

    # 3. Classify and transform records across compartments
    for comp_id, _ in COMPARTMENTS:
        comp_dir = raw_root / comp_id
        cycle_id = f"agc-cycle-{comp_id}"

        # --- A. Ingest Resources.csv -> fact_irrigation_day & fact_energy_day ---
        res_rel = f"{comp_id}/Resources.csv"
        res_path = comp_dir / "Resources.csv"
        if not res_path.exists():
            raise FileNotFoundError(f"Required source file '{res_rel}' missing from raw snapshot")
        if res_path.exists():
            res_chk = file_checksums[res_rel]
            res_source = Source(snapshot_id, snapshot_sha, res_rel, res_chk, run_id)
            res_parser = SourceParser(res_path, res_source)
            classified_res = _index_and_classify_records(list(res_parser.records()))

            for rec, disp in classified_res:
                if disp != "accepted":
                    reason = rec["issues"][0]["code"] if rec.get("issues") else disp
                    quarantine_rows.append((
                        publication_id, cycle_id, comp_id, rec["source_record_id"],
                        disp, reason, None, res_rel, rec["line_number"], rec["line_end"] or rec["line_number"],
                        rec["raw_reference"], rec["raw_hash"], rec["raw_tokens"][0] if rec["raw_tokens"] else None,
                        rec["source_timestamp"],
                    ))
                    continue

                vals = rec["values"]
                issues = rec["issues"]
                null_reasons = rec["null_reasons"]
                res_date = rec["source_timestamp"].date()
                line_end = rec["line_end"] or rec["line_number"]

                # Irrigation evaluation
                irr_val = vals.get("Irr")
                drn_val = vals.get("Drain")
                iq, ia, ir = evaluate_quality("Irr", irr_val, issues, null_reasons)
                dq, da, dr = evaluate_quality("Drain", drn_val, issues, null_reasons)
                i_acc = (iq == "accepted" and ia == "applicable" and irr_val is not None)
                d_acc = (dq == "accepted" and da == "applicable" and drn_val is not None)
                paired = bool(i_acc and d_acc)

                drain_pct = None
                if paired and irr_val is not None and irr_val > 0 and drn_val is not None:
                    drain_pct = water_percent(drn_val, irr_val)

                drain_exceeds = bool(paired and drn_val is not None and irr_val is not None and drn_val > irr_val)
                reasons = []
                if ir:
                    reasons.append(ir)
                if dr:
                    reasons.append(dr)
                if paired and irr_val == 0:
                    reasons.append("no_irrigation_quantity")
                if paired and drn_val is not None and irr_val is not None and drn_val > irr_val:
                    reasons.append("drain_exceeds_irrigation")
                reasons = sorted(list(set(reasons)))

                rev_id_res = hashlib.md5(f"{scope_id}:{rec['source_record_id']}:resource:1".encode()).hexdigest()
                log_key_res = f"{cycle_id}:{rec['logical_key']}"

                irr_rows.append((
                    publication_id, scope_id, cycle_id, comp_id, res_date,
                    irr_val, drn_val, "L/m²/day",
                    iq, ia, ir, dq, da, dr,
                    f"AGC_SHARED_BASIS_V1:{comp_id}:Irr", Decimal("62.5"), "growing_area_assumed", "assumed_unverified",
                    "AGC_SHARED_BASIS_V1", 1, paired,
                    drain_pct, drain_exceeds, reasons,
                    rec["source_record_id"], res_rel, rec["line_number"], line_end,
                    rec["raw_reference"], rec["raw_hash"], rec["raw_tokens"][0] if rec["raw_tokens"] else None,
                    rec["source_timestamp"], rev_id_res, log_key_res,
                ))

                # Energy components evaluation with exact precision preservation
                energy_comps = (
                    ("heat", "Heat_cons", "MJ/m²/day", vals.get("Heat_cons")),
                    ("elec_high", "ElecHigh", "kWh/m²/day", vals.get("ElecHigh")),
                    ("elec_low", "ElecLow", "kWh/m²/day", vals.get("ElecLow")),
                )
                for comp_name, m_code, s_unit, s_val in energy_comps:
                    eq, ea, er = evaluate_quality(m_code, s_val, issues, null_reasons)
                    e_acc = bool(eq == "accepted" and ea == "applicable" and s_val is not None)

                    norm_kwh = None
                    abs_src = None
                    abs_kwh = None
                    if e_acc and s_val is not None:
                        with decimal.localcontext() as ctx:
                            ctx.prec = 100
                            abs_src = s_val * Decimal("62.5")
                            if comp_name == "heat":
                                norm_kwh = ratio_sig(s_val, Decimal("3.6"), 50)
                                abs_kwh = ratio_sig(abs_src, Decimal("3.6"), 50)
                            else:
                                norm_kwh = s_val
                                abs_kwh = abs_src

                    energy_rows.append((
                        publication_id, scope_id, cycle_id, comp_id, res_date,
                        comp_name, m_code, s_unit, s_val,
                        eq, ea, er, e_acc,
                        norm_kwh, f"AGC_SHARED_BASIS_V1:{comp_id}:{m_code}", 1, Decimal("62.5"),
                        "growing_area_assumed", "assumed_unverified", HISTORICAL_WINDOW_START, HISTORICAL_WINDOW_END,
                        "ReadMe.pdf p.1/p.9 + user 2026-11-04; resource extension unverified",
                        "AGC_SHARED_BASIS_V1", 1,
                        "Irr/Drain/Heat_cons/ElecHigh/ElecLow/ProdA/ProdB share growing-area per-m² basis 62.5 m²",
                        "ReadMe.pdf p.1/p.9 + user 2026-11-04; resource extension unverified",
                        abs_src, abs_kwh,
                        rec["source_record_id"], res_rel, rec["line_number"], line_end,
                        rec["raw_reference"], rec["raw_hash"], rec["raw_tokens"][0] if rec["raw_tokens"] else None,
                        rec["source_timestamp"], rev_id_res, log_key_res,
                    ))

        # --- B. Ingest Production.csv -> fact_harvest_record ---
        prod_rel = f"{comp_id}/Production.csv"
        prod_path = comp_dir / "Production.csv"
        if not prod_path.exists():
            raise FileNotFoundError(f"Required source file '{prod_rel}' missing from raw snapshot")
        if prod_path.exists():
            prod_chk = file_checksums[prod_rel]
            prod_source = Source(snapshot_id, snapshot_sha, prod_rel, prod_chk, run_id)
            prod_parser = SourceParser(prod_path, prod_source)
            raw_prod_records = list(prod_parser.records())
            classified_prod = _index_and_classify_records(raw_prod_records)

            read_cnt = len(classified_prod)
            acc_cnt = 0
            quar_cnt = 0
            paired_cnt = 0
            pa_acc_cnt = 0
            pb_acc_cnt = 0
            event_dates: set[date] = set()
            comp_unresolved: list[dict[str, Any]] = []

            for rec, disp in classified_prod:
                if disp != "accepted":
                    quar_cnt += 1
                    reason = rec["issues"][0]["code"] if rec.get("issues") else disp
                    serial_val = rec["values"].get("time") if rec.get("values") else None
                    quarantine_rows.append((
                        publication_id, cycle_id, comp_id, rec["source_record_id"],
                        disp, reason, None, prod_rel, rec["line_number"], rec["line_end"] or rec["line_number"],
                        rec["raw_reference"], rec["raw_hash"], serial_val,
                        rec["source_timestamp"],
                    ))
                    comp_unresolved.append({
                        "source_record_id": rec["source_record_id"],
                        "disposition": disp,
                        "reason_code": reason,
                        "issue_column": None,
                        "relative_path": prod_rel,
                        "line_number": rec["line_number"],
                        "source_time_serial": str(serial_val) if serial_val is not None else None,
                    })
                    continue

                acc_cnt += 1
                vals = rec["values"]
                issues = rec["issues"]
                null_reasons = rec["null_reasons"]
                h_date = rec["source_timestamp"].date()
                event_dates.add(h_date)
                line_end = rec["line_end"] or rec["line_number"]

                pa_val = vals.get("ProdA")
                pb_val = vals.get("ProdB")
                qa_s, qa_a, qa_r = evaluate_quality("ProdA", pa_val, issues, null_reasons)
                qb_s, qb_a, qb_r = evaluate_quality("ProdB", pb_val, issues, null_reasons)

                if qa_s == "accepted" and qa_a == "applicable" and pa_val is not None:
                    pa_acc_cnt += 1
                if qb_s == "accepted" and qb_a == "applicable" and pb_val is not None:
                    pb_acc_cnt += 1
                is_paired = (qa_s == "accepted" and qa_a == "applicable" and pa_val is not None and
                             qb_s == "accepted" and qb_a == "applicable" and pb_val is not None)
                if is_paired:
                    paired_cnt += 1

                rev_id_harv = hashlib.md5(f"{scope_id}:{rec['source_record_id']}:harvest:1".encode()).hexdigest()
                log_key_harv = f"{cycle_id}:{rec['source_record_id']}"

                harv_rows.append((
                    publication_id, scope_id, cycle_id, comp_id, h_date,
                    rec["source_record_id"], pa_val, pb_val, "kg/m²/harvest", "kg/m²/harvest",
                    qa_s, qa_a, qa_r, qb_s, qb_a, qb_r,
                    f"AGC_SHARED_BASIS_V1:{comp_id}:ProdA", Decimal("62.5"), "assumed_unverified",
                    f"AGC_SHARED_BASIS_V1:{comp_id}:ProdB", Decimal("62.5"), "assumed_unverified",
                    prod_rel, rec["line_number"], line_end,
                    rec["raw_reference"], rec["raw_hash"], rec["raw_tokens"][0] if rec["raw_tokens"] else None,
                    rec["source_timestamp"], rev_id_harv, log_key_harv,
                ))

            reasons = ["unresolved_quarantine_records"] if quar_cnt > 0 else []
            harvest_state_rows.append((
                publication_id, cycle_id, comp_id,
                bool(quar_cnt == 0 and acc_cnt == paired_cnt),
                bool(quar_cnt == 0),
                1, 1, 1, 0,
                read_cnt, acc_cnt, quar_cnt, 0, 0,
                quar_cnt, 0, acc_cnt, paired_cnt,
                pa_acc_cnt, pb_acc_cnt, len(event_dates), len(event_dates),
                reasons, prod_rel, Jsonb(comp_unresolved),
            ))

        # --- C. Ingest GreenhouseClimate.csv -> fact_climate_slot & fact_drain_slot ---
        clim_rel = f"{comp_id}/GreenhouseClimate.csv"
        clim_path = comp_dir / "GreenhouseClimate.csv"
        if not clim_path.exists():
            raise FileNotFoundError(f"Required source file '{clim_rel}' missing from raw snapshot")
        if clim_path.exists():
            clim_chk = file_checksums[clim_rel]
            clim_source = Source(snapshot_id, snapshot_sha, clim_rel, clim_chk, run_id)
            clim_parser = SourceParser(clim_path, clim_source)
            classified_clim = _index_and_classify_records(list(clim_parser.records()))

            for rec, disp in classified_clim:
                if disp != "accepted":
                    reason = rec["issues"][0]["code"] if rec.get("issues") else disp
                    quarantine_rows.append((
                        publication_id, cycle_id, comp_id, rec["source_record_id"],
                        disp, reason, None, clim_rel, rec["line_number"], rec["line_end"] or rec["line_number"],
                        rec["raw_reference"], rec["raw_hash"], rec["raw_tokens"][0] if rec["raw_tokens"] else None,
                        rec["source_timestamp"],
                    ))
                    continue

                vals = rec["values"]
                issues = rec["issues"]
                null_reasons = rec["null_reasons"]
                slot_start = rec["slot_start"]  # Guaranteed valid 5m slot for accepted record
                line_end = rec["line_end"] or rec["line_number"]

                tair = vals.get("Tair")
                co2air = vals.get("CO2air")
                t_heat_sp = vals.get("t_heat_sp")
                t_vent_sp = vals.get("t_vent_sp")
                co2_sp = vals.get("co2_sp")
                t_heat_vip = vals.get("t_heat_vip")
                t_ventlee_vip = vals.get("t_ventlee_vip")
                co2_vip = vals.get("co2_vip")

                clim_mq = {}
                for m in CLIMATE_MEASURES:
                    s, a, r = evaluate_quality(m, vals.get(m), issues, null_reasons)
                    clim_mq[m] = {"status": s, "applicability": a, "reason": r}

                rev_id_clim = hashlib.md5(f"{scope_id}:{rec['source_record_id']}:climate:1".encode()).hexdigest()
                log_key_clim = f"{cycle_id}:{rec['logical_key']}"

                climate_rows.append((
                    publication_id, scope_id, cycle_id, comp_id, slot_start,
                    tair, co2air, t_heat_sp, t_vent_sp, co2_sp,
                    t_heat_vip, t_ventlee_vip, co2_vip,
                    "AGC_QUALITY_V1", json.dumps(clim_mq),
                    rec["source_record_id"], clim_rel, rec["line_number"], line_end,
                    rec["raw_reference"], rec["raw_hash"], rec["raw_tokens"][0] if rec["raw_tokens"] else None,
                    rec["source_timestamp"], rev_id_clim, log_key_clim,
                ))

                ec_drain = vals.get("EC_drain_PC")
                ph_drain = vals.get("pH_drain_PC")
                drain_mq = {}
                for m in DRAIN_MEASURES:
                    s, a, r = evaluate_quality(m, vals.get(m), issues, null_reasons)
                    drain_mq[m] = {"status": s, "applicability": a, "reason": r}

                rev_id_drain = hashlib.md5(f"{scope_id}:{rec['source_record_id']}:drain:1".encode()).hexdigest()
                log_key_drain = f"{cycle_id}:{rec['logical_key']}"

                drain_rows.append((
                    publication_id, scope_id, cycle_id, comp_id, slot_start,
                    ec_drain, ph_drain,
                    "AGC_QUALITY_V1", json.dumps(drain_mq),
                    rec["source_record_id"], clim_rel, rec["line_number"], line_end,
                    rec["raw_reference"], rec["raw_hash"], rec["raw_tokens"][0] if rec["raw_tokens"] else None,
                    rec["source_timestamp"], rev_id_drain, log_key_drain,
                ))

    # 4. Atomically persist publication, source inventory, facts, and receipts in ONE transaction
    with conn.transaction():
        # A. Register Provisional Publication
        conn.execute(
            """
            INSERT INTO kimball.publication (
                publication_id, data_scope_id, snapshot_id, run_id,
                quality_policy_version, parser_version, code_version,
                config_bundle_hash, committed_at, finality_status,
                member_count, membership_checksum
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'provisional', 0, '')
            """,
            (
                publication_id, scope_id, snapshot_id, run_id,
                "AGC_QUALITY_V1", PARSER_VERSION, code_version,
                config_bundle_hash, now_utc,
            ),
        )

        # B. Source files manifest
        with conn.cursor().copy(
            """
            COPY kimball.source_file (
                publication_id, snapshot_id, archive_sha256, relative_path,
                file_checksum, size_bytes, expected_rows, verified_at
            ) FROM STDIN
            """
        ) as sf_copy:
            for sf in verified_source_files:
                sf_copy.write_row(sf)

        # C. Quarantine records (if any)
        if quarantine_rows:
            with conn.cursor().copy(
                """
                COPY kimball.quarantine_record (
                    publication_id, cycle_id, compartment_id, source_record_id,
                    disposition, reason_code, issue_column, relative_path,
                    line_number, line_end, raw_reference, raw_hash,
                    source_time_serial, source_timestamp
                ) FROM STDIN
                """
            ) as q_copy:
                for qr in quarantine_rows:
                    q_copy.write_row(qr)

        # D. Harvest cycle completeness state
        if harvest_state_rows:
            with conn.cursor().copy(
                """
                COPY kimball.harvest_cycle_state (
                    publication_id, cycle_id, compartment_id,
                    snapshot_complete, source_resolved,
                    expected_files, files_present, files_loaded, files_failed,
                    records_read, records_accepted, records_quarantine,
                    records_duplicate, records_conflict, unresolved_records,
                    conflicted_records, accepted_records, paired_records,
                    prod_a_usable_records, prod_b_usable_records,
                    event_a_dates, event_b_dates, reasons,
                    expected_relative_path, unresolved
                ) FROM STDIN
                """
            ) as hstate_copy:
                for hsr in harvest_state_rows:
                    hstate_copy.write_row(hsr)

        # E. Facts COPY
        with conn.cursor().copy(
            """
            COPY kimball.fact_irrigation_day (
                publication_id, data_scope_id, cycle_id, compartment_id, resource_date,
                irrigation_l_m2, drain_l_m2, source_unit,
                irrigation_quality, irrigation_applicability, irrigation_reason,
                drain_quality, drain_applicability, drain_reason,
                basis_version_id, area_m2, basis_kind, basis_status,
                assumption_id, assumption_version, paired,
                drain_fraction_pct, drain_exceeds_irrigation, reason_codes,
                source_record_id, source_file, line_number, line_end,
                raw_reference, raw_hash, source_time_serial, source_timestamp,
                revision_id, logical_record_key
            ) FROM STDIN
            """
        ) as irr_copy:
            for r in irr_rows:
                irr_copy.write_row(r)
        row_counts["irrigation_days"] = len(irr_rows)

        with conn.cursor().copy(
            """
            COPY kimball.fact_energy_day (
                publication_id, data_scope_id, cycle_id, compartment_id, resource_date,
                component, measure_code, source_unit, source_value,
                quality, applicability, reason_code, accepted,
                normalized_kwh_m2, basis_version_id, basis_version, area_m2,
                basis_kind, basis_status, basis_valid_from, basis_valid_to_exclusive,
                basis_evidence, assumption_id, assumption_version,
                assumption_statement, assumption_evidence,
                absolute_source_quantity, absolute_kwh,
                source_record_id, source_file, line_number, line_end,
                raw_reference, raw_hash, source_time_serial, source_timestamp,
                revision_id, logical_record_key
            ) FROM STDIN
            """
        ) as energy_copy:
            for r in energy_rows:
                energy_copy.write_row(r)
        row_counts["energy_days"] = len(energy_rows)

        with conn.cursor().copy(
            """
            COPY kimball.fact_harvest_record (
                publication_id, data_scope_id, cycle_id, compartment_id, harvest_date,
                source_record_id, prod_a_kg_m2, prod_b_kg_m2, prod_a_unit, prod_b_unit,
                prod_a_quality, prod_a_applicability, prod_a_reason,
                prod_b_quality, prod_b_applicability, prod_b_reason,
                prod_a_basis_id, prod_a_area_m2, prod_a_basis_status,
                prod_b_basis_id, prod_b_area_m2, prod_b_basis_status,
                source_file, line_number, line_end,
                raw_reference, raw_hash, source_time_serial, source_timestamp,
                revision_id, logical_record_key
            ) FROM STDIN
            """
        ) as harv_copy:
            for r in harv_rows:
                harv_copy.write_row(r)
        row_counts["harvest_records"] = len(harv_rows)

        with conn.cursor().copy(
            """
            COPY kimball.fact_climate_slot (
                publication_id, data_scope_id, cycle_id, compartment_id, slot_start,
                tair, co2air, t_heat_sp, t_vent_sp, co2_sp,
                t_heat_vip, t_ventlee_vip, co2_vip,
                quality_policy_version, measure_quality,
                source_record_id, source_file, line_number, line_end,
                raw_reference, raw_hash, source_time_serial, source_timestamp,
                revision_id, logical_record_key
            ) FROM STDIN
            """
        ) as clim_copy:
            for r in climate_rows:
                clim_copy.write_row(r)
        row_counts["climate_slots"] = len(climate_rows)

        with conn.cursor().copy(
            """
            COPY kimball.fact_drain_slot (
                publication_id, data_scope_id, cycle_id, compartment_id, slot_start,
                ec_drain, ph_drain,
                quality_policy_version, measure_quality,
                source_record_id, source_file, line_number, line_end,
                raw_reference, raw_hash, source_time_serial, source_timestamp,
                revision_id, logical_record_key
            ) FROM STDIN
            """
        ) as drain_copy:
            for r in drain_rows:
                drain_copy.write_row(r)
        row_counts["drain_slots"] = len(drain_rows)

        # F. Receipts across all 5 bundles
        receipts = (
            ("climate", CLIMATE_BUNDLE_VERSION, None, row_counts["climate_slots"], {"slots": row_counts["climate_slots"]}),
            ("drain", DRAIN_BUNDLE_VERSION, None, row_counts["drain_slots"], {"slots": row_counts["drain_slots"]}),
            ("irrigation", IRRIGATION_BUNDLE_VERSION, None, row_counts["irrigation_days"], {"days": row_counts["irrigation_days"]}),
            ("energy", ENERGY_BUNDLE_VERSION, QUANTITY_VERSION, row_counts["energy_days"], {"days": row_counts["energy_days"]}),
            ("harvest", HARVEST_BUNDLE_VERSION, None, row_counts["harvest_records"], {"records": row_counts["harvest_records"]}),
        )
        for b_name, b_ver, q_ver, cnt, det in receipts:
            conn.execute(
                """
                INSERT INTO kimball.publication_receipt
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (publication_id, b_name, b_ver, q_ver, cnt, Jsonb(det)),
            )

        # G. Compute Member Count and Membership Checksum across all distinct fact revisions
        h_member = hashlib.sha256()
        total_distinct_members = 0
        cur = conn.execute(
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
        for (rev,) in cur.fetchall():
            h_member.update((rev + "\n").encode("utf-8"))
            total_distinct_members += 1
        membership_checksum = h_member.hexdigest()

        # H. Seal Publication with verified counts and checksum
        conn.execute(
            """
            UPDATE kimball.publication
            SET member_count = %s,
                membership_checksum = %s,
                committed_at = clock_timestamp()
            WHERE publication_id = %s
            """,
            (total_distinct_members, membership_checksum, publication_id),
        )

    return row_counts


def run_kimball_pipeline(
    raw_root: Path | Settings | None = None,
    admin_settings: Settings | Path | None = None,
    db_name: str = KIMBALL_DB_NAME,
    publication_id: str = KIMBALL_PUBLICATION_ID,
    run_id: str = KIMBALL_RUN_ID,
    code_version: str = KIMBALL_CODE_VERSION,
    create_db: bool = True,
    archive_path: Path | str | None = DEFAULT_ARCHIVE_PATH,
) -> dict[str, Any]:
    """Execute the end-to-end Kimball pipeline with collision guard.

    Supports flexible positional arguments (raw_root, admin_settings) in either order.
    If create_db=True, creates the database failing on collision.
    If create_db=False, connects to existing database.
    """
    if isinstance(raw_root, Settings) and isinstance(admin_settings, (str, Path)):
        raw_root, admin_settings = Path(admin_settings), raw_root
    elif isinstance(raw_root, (str, Path)) and (admin_settings is None or isinstance(admin_settings, Settings)):
        raw_root = Path(raw_root)
        admin_settings = admin_settings or Settings.from_env()
    else:
        raw_root = Path(raw_root) if raw_root else Path("/data/raw/agc-2nd-2019/" + AGC_SHA)
        admin_settings = admin_settings or Settings.from_env()

    # Step 1: Create isolated DB, failing on collision if create_db is True
    if create_db:
        create_isolated_database(admin_settings, db_name)

    # Step 2: Connect to DB and initialize schema
    kimball_settings_kwargs = admin_settings.connection_kwargs()
    kimball_settings_kwargs["dbname"] = db_name

    with psycopg.connect(**kimball_settings_kwargs, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_DDL)

        # Step 3: Populate dimensions
        populate_kimball_dimensions(conn, raw_root)

        # Step 4: Populate facts directly from raw
        counts = load_kimball_facts_from_raw(
            conn, raw_root,
            publication_id=publication_id,
            run_id=run_id,
            code_version=code_version,
            archive_path=archive_path,
        )

    return {
        "database": db_name,
        "publication_id": publication_id,
        "run_id": run_id,
        "code_version": code_version,
        "counts": counts,
        "status": "complete",
    }
