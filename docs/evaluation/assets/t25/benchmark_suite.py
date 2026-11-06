"""Comprehensive Benchmark Suite for Hybrid vs. Kimball Architecture and Technology Evaluation (T25).

Revised per MAIN REVIEW:
1. Static API audit aligned exactly with working test_kimball_parity.py:
   - ClimateService.get_evaluation(..., basis=RuleBasis.REQUESTED, rule_version=..., use_cache=False)
   - DrainService.get_evaluation(..., rule_version=..., use_cache=False)
   - Discloses use_cache=False to evaluate genuine PostgreSQL database engine execution over warm shared buffers
2. Owned staging subdirectory for Hybrid:
   - Uses BENCH_OUTPUT_ROOT (/data/output/t25_bench) so storage audit measures ONLY this run's staging artifacts
3. Fair timer boundaries & locked archive verification:
   - Explicitly runs verify_archive + validate_snapshot for Hybrid too before pipeline execution
   - Symmetrical lifecycle measurements:
     * Hybrid: Fresh build, Rerun reuse, Clean rebuild (greenhouse_hybrid_rebuild_f2b6_t25)
     * Kimball: Fresh build, Rerun rejection, Clean rebuild (greenhouse_kimball_rebuild_f2b6_t25)
4. Specific Kimball rerun rejection assertion:
   - Asserts f"Publication '{publication_id}' already exists and is immutable; cannot overwrite." in error message
5. Technology loading evaluation:
   - Up to 48,000 real climate records (no padding) matching fact_climate_slot schema
   - Full content hash verification (unrestricted numeric, naive timestamp, jsonb) against original source
   - Uniquely named temporary tables
   - psycopg COPY row API vs Bounded Batch INSERT (500 and 1000)
6. Incremental per-stage persistence and optional --smoke mode for fast sanity pre-flight.
"""

import argparse
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from importlib.metadata import version as package_version
import json
import math
import os
from pathlib import Path
import platform
import statistics
import shutil
import time
from typing import Any, Callable
from uuid import uuid4
import zipfile

import psycopg
from psycopg.rows import dict_row

from greenhouse.contracts import (
    Applicability, BasisStatus, CycleWindow, EnergyComponent,
    PublicationRef, QualityStatus, QueryContext, RuleBasis,
    ScenarioChange, ScenarioCommand,
)
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
from greenhouse.db.cost_adapter import PostgresCostAdapter
from greenhouse.db.demo_rules import seed_climate, seed_drain, seed as seed_harvest
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.db.migrate import migrate
from greenhouse.db.pipeline import run_pipeline
from greenhouse.db.query_adapter import PostgresQueryAdapter
from greenhouse.ingestion.parser import PARSER_VERSION
from greenhouse.ingestion.snapshot import AGC, archive_members, validate_snapshot, verify_archive
from greenhouse.kimball.adapter import KimballQueryAdapter
from greenhouse.kimball.audit import verify_kimball_isolation
from greenhouse.kimball.etl import (
    AGC_SCOPE, COMPARTMENTS, KIMBALL_PUBLICATION_ID, create_isolated_database,
    run_kimball_pipeline,
)
from greenhouse.queries import (
    ClimateService, DrainService, EnergyIntensityService,
    EnergyService, IrrigationService,
)
from greenhouse.queries.cost import EnergyCostService
from greenhouse.queries.harvest import HarvestService
from greenhouse.queries.catalog import CatalogService
from greenhouse.settings import Settings

BENCH_RUN_ID = os.environ.get("BENCH_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
HYBRID_BENCH_DB = os.environ.get("HYBRID_BENCH_DB", "greenhouse_hybrid_bench_t25_" + BENCH_RUN_ID)
HYBRID_REBUILD_DB = os.environ.get("HYBRID_REBUILD_DB", "greenhouse_hybrid_rebuild_t25_" + BENCH_RUN_ID)
KIMBALL_BENCH_DB = os.environ.get("KIMBALL_BENCH_DB", "greenhouse_kimball_bench_t25_" + BENCH_RUN_ID)
KIMBALL_REBUILD_DB = os.environ.get("KIMBALL_REBUILD_DB", "greenhouse_kimball_rebuild_t25_" + BENCH_RUN_ID)

HYBRID_BASELINE_DB = os.environ.get("HYBRID_BASELINE_DB", "greenhouse_t07_195889f1a3b1")
KIMBALL_BASELINE_DB = os.environ.get("KIMBALL_BASELINE_DB", "greenhouse_kimball_f2b6_t24_c2_docs")

RAW_ROOT = Path(os.environ.get("RAW_ROOT", "/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"))
ARCHIVE_PATH = Path(os.environ.get("ARCHIVE_PATH", "/data/import/autonomous-greenhouse-challengeagc-2nd-2019.zip"))
BENCH_OUTPUT_ROOT = Path(os.environ.get("BENCH_OUTPUT_ROOT", "/data/output/t25_bench"))
OUT_DIR = Path(os.environ.get("OUTPUT_DIR", "docs/evaluation/assets/t25"))

PRIMARY_COMPARTMENT = "Automatoes"


def canonical_encode(val: Any) -> str:
    return json.dumps(val, sort_keys=True, separators=(",", ":"), default=str)


def norm_dec(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, (int, float, Decimal)):
        s = format(val, "f") if isinstance(val, Decimal) else str(val)
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        if s == "-0":
            s = "0"
        return s
    return str(val)


def digest_object(val: Any) -> str:
    return hashlib.sha256(canonical_encode(val).encode("utf-8")).hexdigest()


def canonicalize_field_value(val: Any) -> Any:
    # Typed, lossless encoding: SQL NULL must not collide with a literal string.
    if val is None:
        return ["null"]
    if isinstance(val, (dict, list)):
        return ["json", json.dumps(val, sort_keys=True, separators=(",", ":"), default=str)]
    if isinstance(val, (int, float, Decimal)):
        return ["number", norm_dec(val)]
    if isinstance(val, (datetime, date)):
        return ["timestamp", val.isoformat()]
    return ["text", str(val)]


def canonicalize_row(row: list[Any] | tuple[Any, ...]) -> list[Any]:
    return [canonicalize_field_value(x) for x in row]


class IncrementalBenchmarkRecorder:
    def __init__(self, out_file: Path):
        self.out_file = out_file
        self.data: dict[str, Any] = {
            "task": "T25",
            "title": "Smart Greenhouse Architecture and Technology Benchmark",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "in_progress",
            "stages": {},
        }
        self.persist()

    def record_stage(self, stage_name: str, payload: Any):
        self.data["stages"][stage_name] = payload
        self.data.setdefault("stage_script_sha256", {})[stage_name] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.data["last_updated_at"] = datetime.now(timezone.utc).isoformat()
        self.persist()

    def retain_stage(self, name, prior):
        self.data["stages"][name] = prior["stages"][name]
        self.data.setdefault("stage_script_sha256", {})[name] = prior["stage_script_sha256"][name]
        self.persist()

    def record_error(self, stage_name: str, error: Exception):
        self.data["status"] = "failed"
        self.data["failed_stage"] = stage_name
        self.data["error_type"] = type(error).__name__
        self.data["error_message"] = str(error)
        self.data["failed_at"] = datetime.now(timezone.utc).isoformat()
        self.persist()

    def mark_complete(self):
        self.data["status"] = "completed"
        self.data["completed_at"] = datetime.now(timezone.utc).isoformat()
        self.persist()

    def persist(self):
        self.out_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.out_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, indent=2, default=str) + "\n")
        temporary.replace(self.out_file)


def time_callable(func: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    res = func()
    duration_ms = (time.perf_counter() - start) * 1000.0
    return res, duration_ms


def sample_workload_metrics(
    readiness_func: Callable[[], Any],
    query_func: Callable[[], Any],
    warmups: int = 5,
    samples_count: int = 30
) -> dict[str, Any]:
    # 1. First-adapter readiness (initialization, catalog, and publication receipt verification)
    _, first_touch_ms = time_callable(readiness_func)

    # 2. Warm-up iterations on initialized adapter
    warmup_times = []
    for _ in range(warmups):
        _, ms = time_callable(query_func)
        warmup_times.append(ms)

    # 3. Measured steady-state iterations
    measured_times = []
    for _ in range(samples_count):
        _, ms = time_callable(query_func)
        measured_times.append(ms)

    sorted_samples = sorted(measured_times)
    p95_idx = math.ceil(0.95 * len(sorted_samples)) - 1
    p95_val = sorted_samples[p95_idx]

    return {
        "cache_state_description": "Separate preparation callback, not a cold database or identical full first request; warm samples use warm buffers and disable climate/drain result caching.",
        "first_touch_initialization_ms": round(first_touch_ms, 3),
        "warmup_iterations_count": warmups,
        "warmup_samples_ms": [round(x, 3) for x in warmup_times],
        "measured_count": samples_count,
        "measured_samples_ms": [round(x, 3) for x in measured_times],
        "median_ms": round(statistics.median(measured_times), 3),
        "mean_ms": round(statistics.mean(measured_times), 3),
        "min_ms": round(min(measured_times), 3),
        "max_ms": round(max(measured_times), 3),
        "p95_nearest_rank_ms": round(p95_val, 3),
        "target_ms": 2000.0,
        "p95_target_pass": p95_val <= 2000.0,
    }


def collect_system_metadata() -> dict[str, Any]:
    settings = Settings.from_env()
    kwargs = settings.connection_kwargs()
    kwargs["dbname"] = "postgres"
    with psycopg.connect(**kwargs) as conn:
        pg_version = conn.execute("SELECT version()").fetchone()[0]

    cgroup_info = {}
    for p, key in (
        ("/sys/fs/cgroup/memory.max", "cgroup_memory_max_bytes"),
        ("/sys/fs/cgroup/memory.peak", "cgroup_memory_peak_bytes"),
        ("/sys/fs/cgroup/cpu.max", "cgroup_cpu_max"),
    ):
        path = Path(p)
        if path.is_file():
            try:
                cgroup_info[key] = path.read_text().strip()
            except Exception:
                cgroup_info[key] = "unreadable"

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "packages": {p: package_version(p) for p in ("psycopg", "pyspark")},
        "archive_sha256": json.loads((RAW_ROOT / "manifest.json").read_text())["archive"]["sha256"],
        "platform": platform.platform(),
        "processor": platform.processor(),
        "postgres_version": pg_version,
        "source_snapshot": str(RAW_ROOT),
        "raw_manifest_sha256": hashlib.sha256((RAW_ROOT / "manifest.json").read_bytes()).hexdigest(),
        "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "parser_version": PARSER_VERSION,
        "quality_policy": "AGC_QUALITY_V1",
        "area_basis": "AGC_SHARED_BASIS_V1;62.5m2;assumed_unverified",
        "cgroup": cgroup_info,
        "container_processing_caps": {"cpu": "1.5", "memory": "2GiB"},
        "container_db_caps": {"cpu": "0.5", "memory": "512MiB"},
    }


def setup_hybrid_metadata_and_rules(conn, scope_id: str):
    """Seed demo rules and baseline tariffs matching Kimball metadata using official setup interfaces."""
    seed_climate(conn, apply=True, scope_id=scope_id)
    seed_drain(conn, apply=True, scope_id=scope_id)
    seed_harvest(conn, apply=True, scope_id=scope_id)
    cost_ad = PostgresCostAdapter(conn)
    cost_ad.register_historical_tariff(scope_id)
    for comp_id, _ in COMPARTMENTS:
        cost_ad.register_historical_tariff(scope_id, comp_id)


def benchmark_pipeline_lifecycle(recorder) -> dict[str, Any]:
    """Six actual sequential executions; persist every completed measurement."""
    admin = Settings.from_env()
    BENCH_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(BENCH_OUTPUT_ROOT).free
    if free < 8 * 1024**3:
        raise RuntimeError("Benchmark requires at least 8 GiB free on the Compose data filesystem before starting; no data was deleted.")
    run_id = uuid4().hex[:8]
    paths = {name: BENCH_OUTPUT_ROOT / (name + "_" + run_id)
             for name in ("fresh", "rerun", "rebuild")}
    result = {"hybrid": {}, "kimball": {}, "run_id": run_id,
              "run_count_per_path": 1, "measurement_order": [], "initial_data_filesystem_free_bytes": free,
              "timer_boundary": "Includes verification, schema/database creation, transform, publication and required demo metadata; excludes query evaluation."}

    def save(name):
        result["measurement_order"].append(name)
        recorder.record_stage("pipeline_progress", result)
        print("  Completed lifecycle:", name, flush=True)

    def hybrid(db_name, out, create=True):
        start = time.perf_counter()
        if create:
            create_isolated_database(admin, db_name)
        verify_archive(ARCHIVE_PATH)
        with zipfile.ZipFile(ARCHIVE_PATH) as archive:
            validate_snapshot(RAW_ROOT, archive, archive_members(archive))
        kwargs = admin.connection_kwargs() | {"dbname": db_name}
        with psycopg.connect(**kwargs, autocommit=True) as conn:
            migrations = migrate(conn)
            value = run_pipeline(conn, snapshot=RAW_ROOT, output=out, scope=AGC_SCOPE,
                                 policy="AGC_QUALITY_V1", workflow="bench-" + uuid4().hex)
        with psycopg.connect(**kwargs) as conn:
            setup_hybrid_metadata_and_rules(conn, AGC_SCOPE)
        return value, time.perf_counter() - start, migrations

    h = result["hybrid"]
    h["fresh_database"] = HYBRID_BENCH_DB
    h["rebuild_database"] = HYBRID_REBUILD_DB
    h["output_paths"] = {k: str(v) for k, v in paths.items()}
    print("  [Pipeline 1/6] Fresh Hybrid", HYBRID_BENCH_DB, flush=True)
    value, elapsed, migrations = hybrid(HYBRID_BENCH_DB, paths["fresh"])
    h.update(publication_id=value["publication_id"], fresh_pipeline_duration_seconds=elapsed,
             migrations_applied_count=len(migrations["applied"]))
    save("hybrid_fresh")
    print("  [Pipeline 2/6] Hybrid rerun", flush=True)
    value, elapsed, _ = hybrid(HYBRID_BENCH_DB, paths["rerun"], create=False)
    assert value["publication_id"] == h["publication_id"]
    h.update(rerun_reuse_duration_seconds=elapsed, rerun_reused_publication_identical=True)
    save("hybrid_rerun")
    with psycopg.connect(**(admin.connection_kwargs() | {"dbname": HYBRID_BENCH_DB})) as conn:
        fresh_db_bytes = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    free = shutil.disk_usage(BENCH_OUTPUT_ROOT).free
    if free < fresh_db_bytes + 2 * 1024**3:
        raise RuntimeError("Insufficient headroom for another Hybrid database plus 2 GiB reserve; completed measurements are checkpointed.")
    print("  [Pipeline 3/6] Fresh Hybrid rebuild", HYBRID_REBUILD_DB, flush=True)
    value, elapsed, _ = hybrid(HYBRID_REBUILD_DB, paths["rebuild"])
    h.update(clean_rebuild_duration_seconds=elapsed, rebuild_publication_id=value["publication_id"])
    save("hybrid_rebuild")

    k = result["kimball"]
    k.update(fresh_database=KIMBALL_BENCH_DB, rebuild_database=KIMBALL_REBUILD_DB)
    def kimball(db_name, create):
        start = time.perf_counter()
        value = run_kimball_pipeline(raw_root=RAW_ROOT, admin_settings=admin,
                    db_name=db_name, create_db=create, archive_path=ARCHIVE_PATH)
        return value, time.perf_counter() - start
    print("  [Pipeline 4/6] Fresh Kimball", KIMBALL_BENCH_DB, flush=True)
    value, elapsed = kimball(KIMBALL_BENCH_DB, True)
    k.update(publication_id=value["publication_id"], fresh_pipeline_duration_seconds=elapsed)
    save("kimball_fresh")
    print("  [Pipeline 5/6] Kimball rerun rejection", flush=True)
    start = time.perf_counter()
    try:
        kimball(KIMBALL_BENCH_DB, False)
    except ValueError as exc:
        expected = f"Publication '{k['publication_id']}' already exists and is immutable; cannot overwrite."
        if str(exc) != expected:
            raise
        k.update(rerun_rejection_duration_seconds=time.perf_counter() - start,
                 publication_immutability_enforced=True, rerun_specific_rejection_message=str(exc))
    else:
        raise AssertionError("Kimball unexpectedly overwrote existing publication")
    save("kimball_rerun")
    print("  [Pipeline 6/6] Fresh Kimball rebuild", KIMBALL_REBUILD_DB, flush=True)
    value, elapsed = kimball(KIMBALL_REBUILD_DB, True)
    k.update(clean_rebuild_duration_seconds=elapsed, rebuild_publication_id=value["publication_id"])
    save("kimball_rebuild")
    return result


def verify_full_workload_correctness_and_parity(h_conn, k_conn, include_scenario_write: bool = True) -> dict[str, Any]:
    """Execute deep verification across all 6 BRs and return canonical result checksums.
    
    If include_scenario_write is False, tests only read parity across all 6 BRs (including BR-06
    baseline pricing) and skips scenario creation writes to protect read-only baseline DBs.
    """
    h_clim_adapter = PostgresClimateQueryAdapter(h_conn)
    k_adapter = KimballQueryAdapter(k_conn)

    h_pub = h_clim_adapter.publication(h_clim_adapter.pin_publication(AGC_SCOPE).publication_id)
    k_pub = k_adapter.publication(k_adapter.pin_publication(AGC_SCOPE).publication_id)

    h_catalog = CatalogService(PostgresCatalogAdapter(h_conn))
    k_catalog = CatalogService(k_adapter)

    h_cycles = {c.compartment_id: c for c in h_catalog.cycles(h_pub)}
    k_cycles = {c.compartment_id: c for c in k_catalog.cycles(k_pub)}

    hc = h_cycles[PRIMARY_COMPARTMENT]
    kc = k_cycles[PRIMARY_COMPARTMENT]

    h_ctx = QueryContext(h_pub.scope_id, h_pub.publication_id, h_pub.snapshot_id,
                         hc.cycle_id, (PRIMARY_COMPARTMENT,), hc.start, hc.end)
    k_ctx = QueryContext(k_pub.scope_id, k_pub.publication_id, k_pub.snapshot_id,
                         kc.cycle_id, (PRIMARY_COMPARTMENT,), kc.start, kc.end)

    checksums = {}

    # BR-01: Full climate slot array and full episode review (use_cache=False)
    h_clim_svc = ClimateService(h_clim_adapter)
    k_clim_svc = ClimateService(k_adapter)
    rule_ver = f"climate-demo-b01:{PRIMARY_COMPARTMENT}:v1"

    h_slots = h_clim_adapter.read_climate_slots(h_ctx, h_pub)
    k_slots = k_adapter.read_climate_slots(k_ctx, k_pub)
    assert len(h_slots) == len(k_slots) and len(h_slots) > 0

    h_eval = h_clim_svc.get_evaluation(h_ctx, basis=RuleBasis.REQUESTED, rule_version=rule_ver, use_cache=False)
    k_eval = k_clim_svc.get_evaluation(k_ctx, basis=RuleBasis.REQUESTED, rule_version=rule_ver, use_cache=False)
    assert len(h_eval.result.episodes) == len(k_eval.result.episodes)
    assert len(h_eval.result.metric_results) == len(k_eval.result.metric_results)
    assert len(h_eval.result.joint_results) == len(k_eval.result.joint_results)

    h_slot_summary = [
        (
            s.slot_start.isoformat(),
            norm_dec(s.tair),
            norm_dec(s.co2air),
            norm_dec(s.t_heat_sp),
            norm_dec(s.t_vent_sp),
            norm_dec(s.co2_sp),
            norm_dec(s.t_heat_vip),
            norm_dec(s.t_ventlee_vip),
            norm_dec(s.co2_vip),
            s.measure_quality,
        )
        for s in h_slots
    ]
    k_slot_summary = [
        (
            s.slot_start.isoformat(),
            norm_dec(s.tair),
            norm_dec(s.co2air),
            norm_dec(s.t_heat_sp),
            norm_dec(s.t_vent_sp),
            norm_dec(s.co2_sp),
            norm_dec(s.t_heat_vip),
            norm_dec(s.t_ventlee_vip),
            norm_dec(s.co2_vip),
            s.measure_quality,
        )
        for s in k_slots
    ]
    assert h_slot_summary == k_slot_summary

    h_ep_summary = [
        (
            e.metric,
            e.original_start.isoformat() if e.original_start else None,
            e.original_end.isoformat() if e.original_end else None,
            e.original_minutes,
            e.clipped_start.isoformat() if e.clipped_start else None,
            e.clipped_end.isoformat() if e.clipped_end else None,
            e.clipped_minutes,
            norm_dec(e.peak_deviation),
            e.peak_time.isoformat() if e.peak_time else None,
        )
        for e in h_eval.result.episodes
    ]
    k_ep_summary = [
        (
            e.metric,
            e.original_start.isoformat() if e.original_start else None,
            e.original_end.isoformat() if e.original_end else None,
            e.original_minutes,
            e.clipped_start.isoformat() if e.clipped_start else None,
            e.clipped_end.isoformat() if e.clipped_end else None,
            e.clipped_minutes,
            norm_dec(e.peak_deviation),
            e.peak_time.isoformat() if e.peak_time else None,
        )
        for e in k_eval.result.episodes
    ]
    assert h_ep_summary == k_ep_summary

    h_mr_summary = [
        (
            mr.metric,
            mr.expected_minutes,
            mr.valid_minutes,
            mr.out_minutes,
            mr.out_low_minutes,
            mr.out_high_minutes,
            mr.suspect_minutes,
            mr.unknown_minutes,
            norm_dec(mr.out_percent),
            norm_dec(mr.coverage_pct),
        )
        for mr in h_eval.result.metric_results
    ]
    k_mr_summary = [
        (
            mr.metric,
            mr.expected_minutes,
            mr.valid_minutes,
            mr.out_minutes,
            mr.out_low_minutes,
            mr.out_high_minutes,
            mr.suspect_minutes,
            mr.unknown_minutes,
            norm_dec(mr.out_percent),
            norm_dec(mr.coverage_pct),
        )
        for mr in k_eval.result.metric_results
    ]
    assert h_mr_summary == k_mr_summary

    h_jr_summary = [
        (
            jr.metrics,
            jr.jointly_valid_minutes,
            jr.joint_out_minutes,
            norm_dec(jr.joint_out_percent),
            norm_dec(jr.joint_coverage_pct),
        )
        for jr in h_eval.result.joint_results
    ]
    k_jr_summary = [
        (
            jr.metrics,
            jr.jointly_valid_minutes,
            jr.joint_out_minutes,
            norm_dec(jr.joint_out_percent),
            norm_dec(jr.joint_coverage_pct),
        )
        for jr in k_eval.result.joint_results
    ]
    assert h_jr_summary == k_jr_summary

    checksums["BR-01"] = {
        "digest": digest_object({
            "slots": h_slot_summary,
            "episodes": h_ep_summary,
            "metrics": h_mr_summary,
            "joint": h_jr_summary,
        }),
        "slot_count": len(h_slots),
        "episodes_count": len(h_eval.result.episodes),
        "metrics_count": len(h_eval.result.metric_results),
        "joint_count": len(h_eval.result.joint_results),
        "parity_verified": True,
    }

    # BR-02: Irrigation days & summary
    h_water_svc = IrrigationService(PostgresQueryAdapter(h_conn))
    k_water_svc = IrrigationService(k_adapter)

    h_days = h_water_svc.get_irrigation_days(h_ctx)
    k_days = k_water_svc.get_irrigation_days(k_ctx)
    h_sum = h_water_svc.get_irrigation_summary(h_ctx)
    k_sum = k_water_svc.get_irrigation_summary(k_ctx)
    assert len(h_days) == len(k_days)

    h_w_summary = {
        "observed_irr": norm_dec(h_sum.irrigation_observed_l_m2),
        "observed_drain": norm_dec(h_sum.drain_observed_l_m2),
        "paired_irrigation_l_m2": norm_dec(h_sum.paired_irrigation_l_m2),
        "paired_drain_l_m2": norm_dec(h_sum.paired_drain_l_m2),
        "drain_fraction_pct": norm_dec(h_sum.drain_fraction_pct),
        "paired_days": h_sum.paired_days,
        "status": str(h_sum.status.value if hasattr(h_sum.status, "value") else h_sum.status),
        "days": [
            (
                d.resource_date.isoformat(),
                norm_dec(d.irrigation.value),
                norm_dec(d.drain.value),
                str(d.irrigation.quality.value if hasattr(d.irrigation.quality, "value") else d.irrigation.quality),
                str(d.drain.quality.value if hasattr(d.drain.quality, "value") else d.drain.quality),
                d.paired,
                norm_dec(d.drain_fraction_pct),
                d.drain_exceeds_irrigation,
                tuple(sorted(d.reason_codes)),
            )
            for d in h_days
        ],
    }
    k_w_summary = {
        "observed_irr": norm_dec(k_sum.irrigation_observed_l_m2),
        "observed_drain": norm_dec(k_sum.drain_observed_l_m2),
        "paired_irrigation_l_m2": norm_dec(k_sum.paired_irrigation_l_m2),
        "paired_drain_l_m2": norm_dec(k_sum.paired_drain_l_m2),
        "drain_fraction_pct": norm_dec(k_sum.drain_fraction_pct),
        "paired_days": k_sum.paired_days,
        "status": str(k_sum.status.value if hasattr(k_sum.status, "value") else k_sum.status),
        "days": [
            (
                d.resource_date.isoformat(),
                norm_dec(d.irrigation.value),
                norm_dec(d.drain.value),
                str(d.irrigation.quality.value if hasattr(d.irrigation.quality, "value") else d.irrigation.quality),
                str(d.drain.quality.value if hasattr(d.drain.quality, "value") else d.drain.quality),
                d.paired,
                norm_dec(d.drain_fraction_pct),
                d.drain_exceeds_irrigation,
                tuple(sorted(d.reason_codes)),
            )
            for d in k_days
        ],
    }
    assert h_w_summary == k_w_summary
    checksums["BR-02"] = {
        "digest": digest_object(h_w_summary),
        "day_count": len(h_days),
        "irrigation_total": norm_dec(h_sum.irrigation_observed_l_m2),
        "drain_total": norm_dec(h_sum.drain_observed_l_m2),
        "parity_verified": True,
    }

    # BR-03: Drain slots & episodes (use_cache=False)
    h_drain_adapter = PostgresDrainQueryAdapter(h_conn)
    h_drain_svc = DrainService(h_drain_adapter)
    k_drain_svc = DrainService(k_adapter)
    drain_rule_ver = f"drain-demo-b03:{PRIMARY_COMPARTMENT}:v1"

    h_d_slots = h_drain_adapter.read_drain_slots(h_ctx, h_pub)
    k_d_slots = k_adapter.read_drain_slots(k_ctx, k_pub)
    h_d_eval = h_drain_svc.get_evaluation(h_ctx, rule_version=drain_rule_ver, use_cache=False)
    k_d_eval = k_drain_svc.get_evaluation(k_ctx, rule_version=drain_rule_ver, use_cache=False)
    assert len(h_d_slots) == len(k_d_slots)
    assert len(h_d_eval.result.episodes) == len(k_d_eval.result.episodes)
    assert len(h_d_eval.result.metric_results) == len(k_d_eval.result.metric_results)
    assert len(h_d_eval.result.joint_results) == len(k_d_eval.result.joint_results)

    h_d_slots_summary = [
        (s.slot_start.isoformat(), norm_dec(s.ec_drain), norm_dec(s.ph_drain), s.measure_quality)
        for s in h_d_slots
    ]
    k_d_slots_summary = [
        (s.slot_start.isoformat(), norm_dec(s.ec_drain), norm_dec(s.ph_drain), s.measure_quality)
        for s in k_d_slots
    ]
    assert h_d_slots_summary == k_d_slots_summary

    h_d_ep_summary = [
        (
            e.metric,
            e.original_start.isoformat() if e.original_start else None,
            e.original_end.isoformat() if e.original_end else None,
            e.original_minutes,
            e.clipped_start.isoformat() if e.clipped_start else None,
            e.clipped_end.isoformat() if e.clipped_end else None,
            e.clipped_minutes,
            norm_dec(e.peak_deviation),
            e.peak_time.isoformat() if e.peak_time else None,
        )
        for e in h_d_eval.result.episodes
    ]
    k_d_ep_summary = [
        (
            e.metric,
            e.original_start.isoformat() if e.original_start else None,
            e.original_end.isoformat() if e.original_end else None,
            e.original_minutes,
            e.clipped_start.isoformat() if e.clipped_start else None,
            e.clipped_end.isoformat() if e.clipped_end else None,
            e.clipped_minutes,
            norm_dec(e.peak_deviation),
            e.peak_time.isoformat() if e.peak_time else None,
        )
        for e in k_d_eval.result.episodes
    ]
    assert h_d_ep_summary == k_d_ep_summary

    h_d_mr_summary = [
        (
            mr.metric,
            mr.expected_minutes,
            mr.valid_minutes,
            mr.out_minutes,
            mr.out_low_minutes,
            mr.out_high_minutes,
            mr.suspect_minutes,
            mr.unknown_minutes,
            norm_dec(mr.out_percent),
            norm_dec(mr.coverage_pct),
        )
        for mr in h_d_eval.result.metric_results
    ]
    k_d_mr_summary = [
        (
            mr.metric,
            mr.expected_minutes,
            mr.valid_minutes,
            mr.out_minutes,
            mr.out_low_minutes,
            mr.out_high_minutes,
            mr.suspect_minutes,
            mr.unknown_minutes,
            norm_dec(mr.out_percent),
            norm_dec(mr.coverage_pct),
        )
        for mr in k_d_eval.result.metric_results
    ]
    assert h_d_mr_summary == k_d_mr_summary

    h_d_jr_summary = [
        (
            jr.metrics,
            jr.jointly_valid_minutes,
            jr.joint_out_minutes,
            norm_dec(jr.joint_out_percent),
            norm_dec(jr.joint_coverage_pct),
        )
        for jr in h_d_eval.result.joint_results
    ]
    k_d_jr_summary = [
        (
            jr.metrics,
            jr.jointly_valid_minutes,
            jr.joint_out_minutes,
            norm_dec(jr.joint_out_percent),
            norm_dec(jr.joint_coverage_pct),
        )
        for jr in k_d_eval.result.joint_results
    ]
    assert h_d_jr_summary == k_d_jr_summary

    checksums["BR-03"] = {
        "digest": digest_object({
            "slots": h_d_slots_summary,
            "episodes": h_d_ep_summary,
            "metrics": h_d_mr_summary,
            "joint": h_d_jr_summary,
        }),
        "slot_count": len(h_d_slots),
        "episodes_count": len(h_d_eval.result.episodes),
        "metrics_count": len(h_d_eval.result.metric_results),
        "joint_count": len(h_d_eval.result.joint_results),
        "parity_verified": True,
    }

    # BR-04: Energy quantities & intensity
    h_energy_adapter = PostgresEnergyQueryAdapter(h_conn)
    h_energy_svc = EnergyService(h_energy_adapter)
    k_energy_svc = EnergyService(k_adapter)
    h_int_svc = EnergyIntensityService(h_energy_svc, PostgresHarvestQueryAdapter(h_conn))
    k_int_svc = EnergyIntensityService(k_energy_svc, k_adapter)

    h_q = h_energy_svc.get_quantities(h_ctx)
    k_q = k_energy_svc.get_quantities(k_ctx)
    h_int = h_int_svc.get_intensity(h_ctx)
    k_int = k_int_svc.get_intensity(k_ctx)
    h_summary = h_energy_svc.get_summary(h_ctx)
    k_summary = k_energy_svc.get_summary(k_ctx)

    assert len(h_q) == len(k_q)
    assert h_int.intensity_kwh_kg_a == k_int.intensity_kwh_kg_a
    assert h_int.observed_a_kg == k_int.observed_a_kg
    assert h_int.comparison_eligible == k_int.comparison_eligible
    assert h_summary.observed_total_kwh == k_summary.observed_total_kwh
    assert h_summary.official_total_kwh == k_summary.official_total_kwh
    assert h_summary.official_total_kwh_m2 == k_summary.official_total_kwh_m2
    assert h_summary.expected_days == k_summary.expected_days
    assert h_summary.complete_days == k_summary.complete_days

    h_e_summary = {
        "intensity": norm_dec(h_int.intensity_kwh_kg_a),
        "observed_a_kg": norm_dec(h_int.observed_a_kg),
        "comparison_eligible": h_int.comparison_eligible,
        "observed_total_kwh": norm_dec(h_summary.observed_total_kwh),
        "official_total_kwh": norm_dec(h_summary.official_total_kwh),
        "official_total_kwh_m2": norm_dec(h_summary.official_total_kwh_m2),
        "expected_days": h_summary.expected_days,
        "complete_days": h_summary.complete_days,
        "quantities": [
            (
                q.resource_date.isoformat(),
                str(q.component.value if hasattr(q.component, "value") else q.component),
                norm_dec(q.source_value),
                norm_dec(q.normalized_kwh_m2),
                norm_dec(q.absolute_kwh),
                str(q.quality.value if hasattr(q.quality, "value") else q.quality),
                q.accepted,
            )
            for q in h_q
        ],
    }
    k_e_summary = {
        "intensity": norm_dec(k_int.intensity_kwh_kg_a),
        "observed_a_kg": norm_dec(k_int.observed_a_kg),
        "comparison_eligible": k_int.comparison_eligible,
        "observed_total_kwh": norm_dec(k_summary.observed_total_kwh),
        "official_total_kwh": norm_dec(k_summary.official_total_kwh),
        "official_total_kwh_m2": norm_dec(k_summary.official_total_kwh_m2),
        "expected_days": k_summary.expected_days,
        "complete_days": k_summary.complete_days,
        "quantities": [
            (
                q.resource_date.isoformat(),
                str(q.component.value if hasattr(q.component, "value") else q.component),
                norm_dec(q.source_value),
                norm_dec(q.normalized_kwh_m2),
                norm_dec(q.absolute_kwh),
                str(q.quality.value if hasattr(q.quality, "value") else q.quality),
                q.accepted,
            )
            for q in k_q
        ],
    }
    assert h_e_summary == k_e_summary
    checksums["BR-04"] = {
        "digest": digest_object(h_e_summary),
        "quantities_count": len(h_q),
        "intensity_kwh_kg_a": norm_dec(h_int.intensity_kwh_kg_a),
        "parity_verified": True,
    }

    # BR-05: Harvest records & cycle state
    h_harv_adapter = PostgresHarvestQueryAdapter(h_conn)
    h_harv_svc = HarvestService(h_harv_adapter)
    k_harv_svc = HarvestService(k_adapter)

    h_recs = h_harv_svc.get_harvest_records(h_ctx)
    k_recs = k_harv_svc.get_harvest_records(k_ctx)
    h_h_sum = h_harv_svc.get_harvest_summary(h_ctx)
    k_h_sum = k_harv_svc.get_harvest_summary(k_ctx)
    h_state = h_harv_svc.adapter.harvest_state(h_ctx)
    k_state = k_harv_svc.adapter.harvest_state(k_ctx)

    assert len(h_recs) == len(k_recs)
    assert h_h_sum.prod_a_observed == k_h_sum.prod_a_observed
    assert h_h_sum.prod_b_observed == k_h_sum.prod_b_observed
    assert h_h_sum.paired_prod_a == k_h_sum.paired_prod_a
    assert h_h_sum.paired_prod_b == k_h_sum.paired_prod_b
    assert h_h_sum.paired_total == k_h_sum.paired_total
    assert h_h_sum.class_a_share_pct == k_h_sum.class_a_share_pct
    assert h_h_sum.class_b_share_pct == k_h_sum.class_b_share_pct
    assert h_h_sum.accepted_records == k_h_sum.accepted_records
    assert h_h_sum.paired_records == k_h_sum.paired_records
    assert h_state.snapshot_complete == k_state.snapshot_complete
    assert h_state.accepted_records == k_state.accepted_records
    assert h_state.paired_records == k_state.paired_records
    assert h_state.reasons == k_state.reasons
    assert len(h_state.unresolved_items) == len(k_state.unresolved_items)

    h_h_summary = {
        "prod_a_observed": norm_dec(h_h_sum.prod_a_observed),
        "prod_b_observed": norm_dec(h_h_sum.prod_b_observed),
        "paired_prod_a": norm_dec(h_h_sum.paired_prod_a),
        "paired_prod_b": norm_dec(h_h_sum.paired_prod_b),
        "paired_total": norm_dec(h_h_sum.paired_total),
        "class_a_share_pct": norm_dec(h_h_sum.class_a_share_pct),
        "class_b_share_pct": norm_dec(h_h_sum.class_b_share_pct),
        "accepted_records": h_h_sum.accepted_records,
        "paired_records": h_h_sum.paired_records,
        "snapshot_complete": h_state.snapshot_complete,
        "state_accepted_records": h_state.accepted_records,
        "state_paired_records": h_state.paired_records,
        "reasons": list(h_state.reasons),
        "unresolved_items_count": len(h_state.unresolved_items),
        "records": [
            (
                r.harvest_date.isoformat(),
                norm_dec(r.prod_a.value),
                norm_dec(r.prod_b.value),
                r.paired,
                str(r.prod_a.quality.value if hasattr(r.prod_a.quality, "value") else r.prod_a.quality),
                str(r.prod_b.quality.value if hasattr(r.prod_b.quality, "value") else r.prod_b.quality),
            )
            for r in h_recs
        ],
    }
    k_h_summary = {
        "prod_a_observed": norm_dec(k_h_sum.prod_a_observed),
        "prod_b_observed": norm_dec(k_h_sum.prod_b_observed),
        "paired_prod_a": norm_dec(k_h_sum.paired_prod_a),
        "paired_prod_b": norm_dec(k_h_sum.paired_prod_b),
        "paired_total": norm_dec(k_h_sum.paired_total),
        "class_a_share_pct": norm_dec(k_h_sum.class_a_share_pct),
        "class_b_share_pct": norm_dec(k_h_sum.class_b_share_pct),
        "accepted_records": k_h_sum.accepted_records,
        "paired_records": k_h_sum.paired_records,
        "snapshot_complete": k_state.snapshot_complete,
        "state_accepted_records": k_state.accepted_records,
        "state_paired_records": k_state.paired_records,
        "reasons": list(k_state.reasons),
        "unresolved_items_count": len(k_state.unresolved_items),
        "records": [
            (
                r.harvest_date.isoformat(),
                norm_dec(r.prod_a.value),
                norm_dec(r.prod_b.value),
                r.paired,
                str(r.prod_a.quality.value if hasattr(r.prod_a.quality, "value") else r.prod_a.quality),
                str(r.prod_b.quality.value if hasattr(r.prod_b.quality, "value") else r.prod_b.quality),
            )
            for r in k_recs
        ],
    }
    assert h_h_summary == k_h_summary
    checksums["BR-05"] = {
        "digest": digest_object(h_h_summary),
        "records_count": len(h_recs),
        "paired_total_kg": norm_dec(h_h_sum.paired_total),
        "parity_verified": True,
    }

    # BR-06: Baseline cost & scenario execution
    h_cost_adapter = PostgresCostAdapter(h_conn)
    h_cost_svc = EnergyCostService(h_energy_svc, h_cost_adapter)
    k_cost_svc = EnergyCostService(k_energy_svc, k_adapter)

    h_tariff = h_cost_svc.get_baseline_tariff(h_pub.scope_id, PRIMARY_COMPARTMENT) or h_cost_svc.get_baseline_tariff(h_pub.scope_id, None)
    k_tariff = k_cost_svc.get_baseline_tariff(k_pub.scope_id, PRIMARY_COMPARTMENT) or k_cost_svc.get_baseline_tariff(k_pub.scope_id, None)

    h_b_cost = h_cost_svc.get_cost(h_ctx, tariff_version=h_tariff.tariff_version)
    k_b_cost = k_cost_svc.get_cost(k_ctx, tariff_version=k_tariff.tariff_version)
    assert h_b_cost.observed_total_cost_eur == k_b_cost.observed_total_cost_eur
    assert h_b_cost.observed_total_cost_per_m2 == k_b_cost.observed_total_cost_per_m2
    assert h_b_cost.official_total_cost_eur == k_b_cost.official_total_cost_eur
    assert h_b_cost.official_total_cost_per_m2 == k_b_cost.official_total_cost_per_m2
    assert h_b_cost.priced_days == k_b_cost.priced_days
    assert h_b_cost.expected_days == k_b_cost.expected_days

    h_c_summary = {
        "baseline_cost_eur": norm_dec(h_b_cost.observed_total_cost_eur),
        "observed_total_cost_per_m2": norm_dec(h_b_cost.observed_total_cost_per_m2),
        "official_total_cost_eur": norm_dec(h_b_cost.official_total_cost_eur),
        "official_total_cost_per_m2": norm_dec(h_b_cost.official_total_cost_per_m2),
        "priced_days": h_b_cost.priced_days,
        "expected_days": h_b_cost.expected_days,
        "components": [
            (
                str(c.component.value if hasattr(c.component, "value") else c.component),
                norm_dec(c.observed_cost_eur),
                norm_dec(c.observed_cost_per_m2),
                norm_dec(c.official_cost_eur),
                norm_dec(c.official_cost_per_m2),
                [(dc.resource_date.isoformat(), norm_dec(dc.cost_eur), norm_dec(dc.cost_per_m2)) for dc in c.day_costs],
            )
            for c in h_b_cost.components
        ],
    }
    k_c_summary = {
        "baseline_cost_eur": norm_dec(k_b_cost.observed_total_cost_eur),
        "observed_total_cost_per_m2": norm_dec(k_b_cost.observed_total_cost_per_m2),
        "official_total_cost_eur": norm_dec(k_b_cost.official_total_cost_eur),
        "official_total_cost_per_m2": norm_dec(k_b_cost.official_total_cost_per_m2),
        "priced_days": k_b_cost.priced_days,
        "expected_days": k_b_cost.expected_days,
        "components": [
            (
                str(c.component.value if hasattr(c.component, "value") else c.component),
                norm_dec(c.observed_cost_eur),
                norm_dec(c.observed_cost_per_m2),
                norm_dec(c.official_cost_eur),
                norm_dec(c.official_cost_per_m2),
                [(dc.resource_date.isoformat(), norm_dec(dc.cost_eur), norm_dec(dc.cost_per_m2)) for dc in c.day_costs],
            )
            for c in k_b_cost.components
        ],
    }

    if include_scenario_write:
        # Write scenario only on fresh owned benchmark DBs
        scen_cmd_k = ScenarioCommand(
            request_id="bench-scen-k-" + uuid4().hex[:8],
            baseline_version_id=k_tariff.tariff_version,
            name="Benchmark Scenario Kimball",
            publication=k_pub,
            changes=(ScenarioChange(EnergyComponent.HEAT, "EUR/MJ", price=Decimal("0.0095")),),
            analysis_context=k_ctx,
        )
        k_scen = k_cost_svc.execute_scenario_command(scen_cmd_k)
        k_scen_cost = k_cost_svc.get_cost(k_ctx, scenario_version=k_scen.scenario_version)

        scen_cmd_h = replace(
            scen_cmd_k,
            request_id="bench-scen-h-" + uuid4().hex[:8],
            baseline_version_id=h_tariff.tariff_version,
            name="Benchmark Scenario Hybrid",
            publication=h_pub,
            analysis_context=h_ctx,
        )
        h_scen = h_cost_svc.execute_scenario_command(scen_cmd_h)
        h_scen_cost = h_cost_svc.get_cost(h_ctx, scenario_version=h_scen.scenario_version)

        assert h_scen_cost.observed_total_cost_eur == k_scen_cost.observed_total_cost_eur
        assert h_scen_cost.observed_total_cost_per_m2 == k_scen_cost.observed_total_cost_per_m2
        assert h_scen_cost.official_total_cost_eur == k_scen_cost.official_total_cost_eur
        assert h_scen_cost.official_total_cost_per_m2 == k_scen_cost.official_total_cost_per_m2
        assert h_scen_cost.priced_days == k_scen_cost.priced_days

        h_c_summary["scenario"] = {
            "observed_total_cost_eur": norm_dec(h_scen_cost.observed_total_cost_eur),
            "observed_total_cost_per_m2": norm_dec(h_scen_cost.observed_total_cost_per_m2),
            "official_total_cost_eur": norm_dec(h_scen_cost.official_total_cost_eur),
            "official_total_cost_per_m2": norm_dec(h_scen_cost.official_total_cost_per_m2),
            "priced_days": h_scen_cost.priced_days,
            "components": [
                (
                    str(c.component.value if hasattr(c.component, "value") else c.component),
                    norm_dec(c.observed_cost_eur),
                    norm_dec(c.observed_cost_per_m2),
                    norm_dec(c.official_cost_eur),
                    norm_dec(c.official_cost_per_m2),
                    [(dc.resource_date.isoformat(), norm_dec(dc.cost_eur), norm_dec(dc.cost_per_m2)) for dc in c.day_costs],
                )
                for c in h_scen_cost.components
            ],
        }
        k_c_summary["scenario"] = {
            "observed_total_cost_eur": norm_dec(k_scen_cost.observed_total_cost_eur),
            "observed_total_cost_per_m2": norm_dec(k_scen_cost.observed_total_cost_per_m2),
            "official_total_cost_eur": norm_dec(k_scen_cost.official_total_cost_eur),
            "official_total_cost_per_m2": norm_dec(k_scen_cost.official_total_cost_per_m2),
            "priced_days": k_scen_cost.priced_days,
            "components": [
                (
                    str(c.component.value if hasattr(c.component, "value") else c.component),
                    norm_dec(c.observed_cost_eur),
                    norm_dec(c.observed_cost_per_m2),
                    norm_dec(c.official_cost_eur),
                    norm_dec(c.official_cost_per_m2),
                    [(dc.resource_date.isoformat(), norm_dec(dc.cost_eur), norm_dec(dc.cost_per_m2)) for dc in c.day_costs],
                )
                for c in k_scen_cost.components
            ],
        }

    assert h_c_summary == k_c_summary
    checksums["BR-06"] = {
        "digest": digest_object(h_c_summary),
        "baseline_cost_eur": norm_dec(h_b_cost.observed_total_cost_eur),
        "priced_days": h_b_cost.priced_days,
        "parity_verified": True,
    }
    if include_scenario_write:
        checksums["BR-06"]["scenario_cost_eur"] = norm_dec(k_scen_cost.observed_total_cost_eur)

    return checksums


def benchmark_workload_latencies(h_conn, k_conn, warmups: int = 5, samples_count: int = 30) -> dict[str, Any]:
    results = {}

    h_clim_adapter = PostgresClimateQueryAdapter(h_conn)
    k_adapter = KimballQueryAdapter(k_conn)

    h_pub = h_clim_adapter.publication(h_clim_adapter.pin_publication(AGC_SCOPE).publication_id)
    k_pub = k_adapter.publication(k_adapter.pin_publication(AGC_SCOPE).publication_id)

    h_catalog = CatalogService(PostgresCatalogAdapter(h_conn))
    k_catalog = CatalogService(k_adapter)

    h_cycles = {c.compartment_id: c for c in h_catalog.cycles(h_pub)}
    k_cycles = {c.compartment_id: c for c in k_catalog.cycles(k_pub)}

    hc = h_cycles[PRIMARY_COMPARTMENT]
    kc = k_cycles[PRIMARY_COMPARTMENT]

    h_ctx = QueryContext(h_pub.scope_id, h_pub.publication_id, h_pub.snapshot_id,
                         hc.cycle_id, (PRIMARY_COMPARTMENT,), hc.start, hc.end)
    k_ctx = QueryContext(k_pub.scope_id, k_pub.publication_id, k_pub.snapshot_id,
                         kc.cycle_id, (PRIMARY_COMPARTMENT,), kc.start, kc.end)

    # ---------------- BR-01: Climate (use_cache=False) ----------------
    h_clim_svc = ClimateService(h_clim_adapter)
    k_clim_svc = ClimateService(k_adapter)
    rule_ver = f"climate-demo-b01:{PRIMARY_COMPARTMENT}:v1"

    def q_br01_h():
        slots = h_clim_adapter.read_climate_slots(h_ctx, h_pub)
        ev = h_clim_svc.get_evaluation(h_ctx, basis=RuleBasis.REQUESTED, rule_version=rule_ver, use_cache=False)
        return len(slots), len(ev.result.episodes)

    def q_br01_k():
        slots = k_adapter.read_climate_slots(k_ctx, k_pub)
        ev = k_clim_svc.get_evaluation(k_ctx, basis=RuleBasis.REQUESTED, rule_version=rule_ver, use_cache=False)
        return len(slots), len(ev.result.episodes)

    results["BR-01_Climate"] = {
        "description": "5-minute slot time series reading + temperature/CO2 violation evaluation (use_cache=False)",
        "compartment": PRIMARY_COMPARTMENT,
        "hybrid": sample_workload_metrics(lambda: PostgresClimateQueryAdapter(h_conn).read_climate_slots(h_ctx, h_pub), q_br01_h, warmups, samples_count),
        "kimball": sample_workload_metrics(lambda: KimballQueryAdapter(k_conn).read_climate_slots(k_ctx, k_pub), q_br01_k, warmups, samples_count),
    }

    # ---------------- BR-02: Water ----------------
    h_water_svc = IrrigationService(PostgresQueryAdapter(h_conn))
    k_water_svc = IrrigationService(k_adapter)

    def q_br02_h():
        days = h_water_svc.get_irrigation_days(h_ctx)
        s = h_water_svc.get_irrigation_summary(h_ctx)
        return len(days), s.drain_fraction_pct

    def q_br02_k():
        days = k_water_svc.get_irrigation_days(k_ctx)
        s = k_water_svc.get_irrigation_summary(k_ctx)
        return len(days), s.drain_fraction_pct

    results["BR-02_Water"] = {
        "description": "Daily irrigation & drain pairing, balance summary, and drain fraction %",
        "compartment": PRIMARY_COMPARTMENT,
        "hybrid": sample_workload_metrics(lambda: IrrigationService(PostgresQueryAdapter(h_conn)).get_irrigation_summary(h_ctx), q_br02_h, warmups, samples_count),
        "kimball": sample_workload_metrics(lambda: IrrigationService(KimballQueryAdapter(k_conn)).get_irrigation_summary(k_ctx), q_br02_k, warmups, samples_count),
    }

    # ---------------- BR-03: Drain (use_cache=False) ----------------
    h_drain_adapter = PostgresDrainQueryAdapter(h_conn)
    h_drain_svc = DrainService(h_drain_adapter)
    k_drain_svc = DrainService(k_adapter)
    drain_rule_ver = f"drain-demo-b03:{PRIMARY_COMPARTMENT}:v1"

    def q_br03_h():
        slots = h_drain_adapter.read_drain_slots(h_ctx, h_pub)
        ev = h_drain_svc.get_evaluation(h_ctx, rule_version=drain_rule_ver, use_cache=False)
        return len(slots), len(ev.result.episodes)

    def q_br03_k():
        slots = k_adapter.read_drain_slots(k_ctx, k_pub)
        ev = k_drain_svc.get_evaluation(k_ctx, rule_version=drain_rule_ver, use_cache=False)
        return len(slots), len(ev.result.episodes)

    results["BR-03_Drain"] = {
        "description": "5-minute EC/pH slot readings + out-of-band episode evaluations (use_cache=False)",
        "compartment": PRIMARY_COMPARTMENT,
        "hybrid": sample_workload_metrics(lambda: PostgresDrainQueryAdapter(h_conn).read_drain_slots(h_ctx, h_pub), q_br03_h, warmups, samples_count),
        "kimball": sample_workload_metrics(lambda: KimballQueryAdapter(k_conn).read_drain_slots(k_ctx, k_pub), q_br03_k, warmups, samples_count),
    }

    # ---------------- BR-04: Energy ----------------
    h_energy_adapter = PostgresEnergyQueryAdapter(h_conn)
    h_energy_svc = EnergyService(h_energy_adapter)
    k_energy_svc = EnergyService(k_adapter)
    h_int_svc = EnergyIntensityService(h_energy_svc, PostgresHarvestQueryAdapter(h_conn))
    k_int_svc = EnergyIntensityService(k_energy_svc, k_adapter)

    def q_br04_h():
        q = h_energy_svc.get_quantities(h_ctx)
        i = h_int_svc.get_intensity(h_ctx)
        return len(q), i.intensity_kwh_kg_a

    def q_br04_k():
        q = k_energy_svc.get_quantities(k_ctx)
        i = k_int_svc.get_intensity(k_ctx)
        return len(q), i.intensity_kwh_kg_a

    results["BR-04_Energy"] = {
        "description": "Daily heat/electricity quantity normalization, official total, and specific yield intensity",
        "compartment": PRIMARY_COMPARTMENT,
        "hybrid": sample_workload_metrics(lambda: EnergyService(PostgresEnergyQueryAdapter(h_conn)).get_quantities(h_ctx), q_br04_h, warmups, samples_count),
        "kimball": sample_workload_metrics(lambda: EnergyService(KimballQueryAdapter(k_conn)).get_quantities(k_ctx), q_br04_k, warmups, samples_count),
    }

    # ---------------- BR-05: Harvest ----------------
    h_harv_adapter = PostgresHarvestQueryAdapter(h_conn)
    h_harv_svc = HarvestService(h_harv_adapter)
    k_harv_svc = HarvestService(k_adapter)

    def q_br05_h():
        r = h_harv_svc.get_harvest_records(h_ctx)
        s = h_harv_svc.get_harvest_summary(h_ctx)
        st = h_harv_svc.adapter.harvest_state(h_ctx)
        return len(r), s.paired_total, st.snapshot_complete

    def q_br05_k():
        r = k_harv_svc.get_harvest_records(k_ctx)
        s = k_harv_svc.get_harvest_summary(k_ctx)
        st = k_harv_svc.adapter.harvest_state(k_ctx)
        return len(r), s.paired_total, st.snapshot_complete

    results["BR-05_Harvest"] = {
        "description": "Harvest record pairing, class A/B share calculation, and snapshot completeness gate",
        "compartment": PRIMARY_COMPARTMENT,
        "hybrid": sample_workload_metrics(lambda: HarvestService(PostgresHarvestQueryAdapter(h_conn)).get_harvest_records(h_ctx), q_br05_h, warmups, samples_count),
        "kimball": sample_workload_metrics(lambda: HarvestService(KimballQueryAdapter(k_conn)).get_harvest_records(k_ctx), q_br05_k, warmups, samples_count),
    }

    # ---------------- BR-06: Cost ----------------
    h_cost_adapter = PostgresCostAdapter(h_conn)
    h_cost_svc = EnergyCostService(h_energy_svc, h_cost_adapter)
    k_cost_svc = EnergyCostService(k_energy_svc, k_adapter)

    h_tariff = h_cost_svc.get_baseline_tariff(h_pub.scope_id, PRIMARY_COMPARTMENT) or h_cost_svc.get_baseline_tariff(h_pub.scope_id, None)
    k_tariff = k_cost_svc.get_baseline_tariff(k_pub.scope_id, PRIMARY_COMPARTMENT) or k_cost_svc.get_baseline_tariff(k_pub.scope_id, None)

    def q_br06_h():
        c = h_cost_svc.get_cost(h_ctx, tariff_version=h_tariff.tariff_version)
        return c.observed_total_cost_eur, c.priced_days

    def q_br06_k():
        c = k_cost_svc.get_cost(k_ctx, tariff_version=k_tariff.tariff_version)
        return c.observed_total_cost_eur, c.priced_days

    results["BR-06_Cost"] = {
        "description": "Baseline component prices applied to daily energy quantities across the historical season",
        "compartment": PRIMARY_COMPARTMENT,
        "hybrid": sample_workload_metrics(lambda: EnergyCostService(h_energy_svc, PostgresCostAdapter(h_conn)).get_cost(h_ctx, tariff_version=h_tariff.tariff_version), q_br06_h, warmups, samples_count),
        "kimball": sample_workload_metrics(lambda: EnergyCostService(k_energy_svc, KimballQueryAdapter(k_conn)).get_cost(k_ctx, tariff_version=k_tariff.tariff_version), q_br06_k, warmups, samples_count),
    }

    return results


def benchmark_storage_footprint(h_conn, k_conn, pipeline_meta) -> dict[str, Any]:
    query = """
    SELECT 
        schemaname,
        tablename,
        pg_total_relation_size(quote_ident(schemaname) || '.' || quote_ident(tablename)) AS total_bytes,
        pg_relation_size(quote_ident(schemaname) || '.' || quote_ident(tablename)) AS table_bytes,
        pg_indexes_size(quote_ident(schemaname) || '.' || quote_ident(tablename)) AS index_bytes
    FROM pg_tables
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY total_bytes DESC;
    """
    with h_conn.cursor(row_factory=dict_row) as cur:
        h_rows = cur.execute(query).fetchall()
        h_db_size = h_conn.execute("SELECT pg_database_size(%s)", (HYBRID_BENCH_DB,)).fetchone()[0]

    with k_conn.cursor(row_factory=dict_row) as cur:
        k_rows = cur.execute(query).fetchall()
        k_db_size = k_conn.execute("SELECT pg_database_size(%s)", (KIMBALL_BENCH_DB,)).fetchone()[0]

    def aggregate(rows):
        res = defaultdict(lambda: {"total_bytes": 0, "table_bytes": 0, "index_bytes": 0})
        for r in rows:
            s = r["schemaname"]
            res[s]["total_bytes"] += r["total_bytes"]
            res[s]["table_bytes"] += r["table_bytes"]
            res[s]["index_bytes"] += r["index_bytes"]
        return dict(res)

    h_schemas = aggregate(h_rows)
    k_schemas = aggregate(k_rows)

    # Disk artifact storage measurement ONLY in owned benchmark directory
    artifact_bytes = {name: sum(p.stat().st_size for p in Path(folder).rglob("*") if p.is_file())
                      for name, folder in pipeline_meta["hybrid"]["output_paths"].items()}
    hybrid_staging_bytes = artifact_bytes["fresh"]

    return {
        "hybrid": {
            "database_name": HYBRID_BENCH_DB,
            "database_size_bytes": h_db_size,
            "staging_artifact_disk_bytes": hybrid_staging_bytes,
            "artifact_bytes_by_lifecycle_path": artifact_bytes,
            "workflow_count": h_conn.execute("SELECT count(*) FROM meta.workflow_run").fetchone()[0],
            "publication_count": h_conn.execute("SELECT count(*) FROM meta.publication_seal").fetchone()[0],
            "schema_aggregates": h_schemas,
            "detailed_tables": h_rows,
        },
        "kimball": {
            "database_name": KIMBALL_BENCH_DB,
            "database_size_bytes": k_db_size,
            "staging_artifact_disk_bytes": 0,
            "schema_aggregates": k_schemas,
            "detailed_tables": k_rows,
        },
        "ratio_kimball_to_hybrid_db_size": round(k_db_size / h_db_size, 4),
        "total_storage_saved_bytes": h_db_size + hybrid_staging_bytes - k_db_size,
    }


def benchmark_technology_loading(kimball_conn) -> dict[str, Any]:
    kimball_conn.commit()  # Exclude prior metadata/scenario transactions from load timing.
    select_query = """
    SELECT 
        publication_id, data_scope_id, cycle_id, compartment_id, slot_start,
        tair, co2air, t_heat_sp, t_vent_sp, co2_sp, t_heat_vip, t_ventlee_vip, co2_vip,
        quality_policy_version, measure_quality,
        source_record_id, source_file, line_number, line_end, raw_reference, raw_hash,
        source_time_serial, source_timestamp, revision_id, logical_record_key
    FROM kimball.fact_climate_slot
    WHERE compartment_id = %s
    ORDER BY slot_start
    LIMIT 48000
    """
    with kimball_conn.cursor(row_factory=dict_row) as cur:
        sample_rows = cur.execute(select_query, (PRIMARY_COMPARTMENT,)).fetchall()

    row_count = len(sample_rows)
    assert 0 < row_count <= 48000, f"Expected nonempty real source rows, got {row_count}"

    columns = [
        "publication_id", "data_scope_id", "cycle_id", "compartment_id", "slot_start",
        "tair", "co2air", "t_heat_sp", "t_vent_sp", "co2_sp", "t_heat_vip", "t_ventlee_vip", "co2_vip",
        "quality_policy_version", "measure_quality",
        "source_record_id", "source_file", "line_number", "line_end", "raw_reference", "raw_hash",
        "source_time_serial", "source_timestamp", "revision_id", "logical_record_key"
    ]

    source_tuples = [
        tuple(r[col] if col != "measure_quality" else (json.dumps(r[col]) if isinstance(r[col], dict) else str(r[col])) for col in columns)
        for r in sample_rows
    ]
    source_content_digest = digest_object([canonicalize_row([row[c] for c in columns]) for row in sample_rows])

    ddl_template = """
    CREATE TEMP TABLE {table_name} (
        publication_id text NOT NULL,
        data_scope_id text NOT NULL,
        cycle_id text NOT NULL,
        compartment_id text NOT NULL,
        slot_start timestamp NOT NULL,
        tair numeric,
        co2air numeric,
        t_heat_sp numeric,
        t_vent_sp numeric,
        co2_sp numeric,
        t_heat_vip numeric,
        t_ventlee_vip numeric,
        co2_vip numeric,
        quality_policy_version text NOT NULL,
        measure_quality jsonb NOT NULL,
        source_record_id text NOT NULL,
        source_file text NOT NULL,
        line_number integer NOT NULL,
        line_end integer,
        raw_reference text NOT NULL,
        raw_hash text NOT NULL,
        source_time_serial numeric,
        source_timestamp timestamp NOT NULL,
        revision_id text NOT NULL,
        logical_record_key text NOT NULL,
        PRIMARY KEY (publication_id, cycle_id, slot_start)
    );
    """

    results = {"evaluated_rows": row_count, "requested_row_limit": 48000, "source_content_digest": source_content_digest,
               "run_count_per_method": 1, "timer_boundary": "Load plus commit; excludes DDL, source read/preparation and content verification.",
               "table_kind": "Temporary tables with identical schema and primary key; not a WAL/replication durability throughput benchmark."}

    def verify_table(t_name: str) -> tuple[int, str]:
        with kimball_conn.cursor() as cur:
            cnt = cur.execute(f"SELECT count(*) FROM {t_name}").fetchone()[0]
            col_list = ", ".join(columns)
            cur.execute(f"SELECT {col_list} FROM {t_name} ORDER BY slot_start")
            rows = cur.fetchall()
            digest = digest_object([canonicalize_row(row) for row in rows])
            return cnt, digest

    # Method 1: PostgreSQL COPY FROM STDIN row streaming via psycopg copy row API
    table_copy = f"_t25_bench_copy_{uuid4().hex[:8]}"
    kimball_conn.execute(ddl_template.format(table_name=table_copy))

    copy_start = time.perf_counter()
    col_str = ", ".join(columns)
    with kimball_conn.cursor() as cur:
        with cur.copy(f"COPY {table_copy} ({col_str}) FROM STDIN") as copy:
            for row in source_tuples:
                copy.write_row(row)
    kimball_conn.commit()
    copy_ms = (time.perf_counter() - copy_start) * 1000.0

    copy_count, copy_digest = verify_table(table_copy)
    assert copy_count == row_count
    assert copy_digest == source_content_digest, "COPY row content mismatch against source"

    results["copy_from_stdin"] = {
        "duration_ms": round(copy_ms, 3),
        "throughput_rows_per_sec": round(row_count / (copy_ms / 1000.0), 1),
        "inserted_rows": copy_count,
        "content_digest": copy_digest,
        "content_matches_source": True,
    }

    # Method 2: Bounded Batch INSERT (Chunk size 500)
    table_b500 = f"_t25_bench_b500_{uuid4().hex[:8]}"
    kimball_conn.execute(ddl_template.format(table_name=table_b500))

    val_placeholders = "(" + ", ".join(["%s"] * len(columns)) + ")"
    insert_base = f"INSERT INTO {table_b500} ({col_str}) VALUES "

    b500_start = time.perf_counter()
    chunk_500 = 500
    with kimball_conn.cursor() as cur:
        for i in range(0, row_count, chunk_500):
            batch = source_tuples[i:i + chunk_500]
            placeholders = ", ".join([val_placeholders] * len(batch))
            flattened = [item for sub in batch for item in sub]
            cur.execute(insert_base + placeholders, flattened)
    kimball_conn.commit()
    b500_ms = (time.perf_counter() - b500_start) * 1000.0

    b500_count, b500_digest = verify_table(table_b500)
    assert b500_count == row_count
    assert b500_digest == source_content_digest, "Batch 500 row content mismatch against source"

    results["batch_insert_500"] = {
        "duration_ms": round(b500_ms, 3),
        "throughput_rows_per_sec": round(row_count / (b500_ms / 1000.0), 1),
        "inserted_rows": b500_count,
        "content_digest": b500_digest,
        "content_matches_source": True,
    }

    # Method 3: Bounded Batch INSERT (Chunk size 1000)
    table_b1000 = f"_t25_bench_b1000_{uuid4().hex[:8]}"
    kimball_conn.execute(ddl_template.format(table_name=table_b1000))

    b1000_start = time.perf_counter()
    chunk_1000 = 1000
    with kimball_conn.cursor() as cur:
        for i in range(0, row_count, chunk_1000):
            batch = source_tuples[i:i + chunk_1000]
            placeholders = ", ".join([val_placeholders] * len(batch))
            flattened = [item for sub in batch for item in sub]
            cur.execute(insert_base.replace(table_b500, table_b1000) + placeholders, flattened)
    kimball_conn.commit()
    b1000_ms = (time.perf_counter() - b1000_start) * 1000.0

    b1000_count, b1000_digest = verify_table(table_b1000)
    assert b1000_count == row_count
    assert b1000_digest == source_content_digest, "Batch 1000 row content mismatch against source"

    results["batch_insert_1000"] = {
        "duration_ms": round(b1000_ms, 3),
        "throughput_rows_per_sec": round(row_count / (b1000_ms / 1000.0), 1),
        "inserted_rows": b1000_count,
        "content_digest": b1000_digest,
        "content_matches_source": True,
    }

    results["empirical_speedup_copy_vs_batch_500"] = round(b500_ms / copy_ms, 2)
    results["empirical_speedup_copy_vs_batch_1000"] = round(b1000_ms / copy_ms, 2)

    results["transaction_and_error_handling_analysis"] = {
        "single_transaction_reality": "When multiple batches/chunks execute within a single PostgreSQL transaction, any constraint violation or syntax error rolls back the entire transaction. Chunk-level isolation is not inherent to batch INSERT unless SAVEPOINTs or independent transactions are explicitly committed per chunk.",
        "copy_tradeoffs": "COPY FROM STDIN bypasses statement parsing and query tree creation in the PostgreSQL backend, delivering peak streaming throughput. However, COPY is all-or-nothing: a single malformed row aborts the entire stream.",
        "batch_insert_tradeoffs": "Batch INSERT allows selective chunk commits or savepoint-based retries if wrapped in per-batch error handlers, but incurs significant SQL statement parsing, parameter binding, and AST generation overhead for every batch.",
    }

    kimball_conn.execute(f"DROP TABLE IF EXISTS {table_copy}")
    kimball_conn.execute(f"DROP TABLE IF EXISTS {table_b500}")
    kimball_conn.execute(f"DROP TABLE IF EXISTS {table_b1000}")
    kimball_conn.commit()

    return results


def run_smoke_test():
    """Fast bounded sanity smoke over baseline DBs to verify imports, signatures, and queries in strict read-only mode."""
    print("=== Running Pre-flight Benchmark Smoke Test (Strict Read-Only) ===")
    settings = Settings.from_env()

    h_kw = settings.connection_kwargs()
    h_kw["dbname"] = HYBRID_BASELINE_DB
    h_kw["options"] = "-c default_transaction_read_only=on"

    k_kw = settings.connection_kwargs()
    k_kw["dbname"] = KIMBALL_BASELINE_DB
    k_kw["options"] = "-c default_transaction_read_only=on"

    with psycopg.connect(**h_kw, autocommit=True) as h_conn, psycopg.connect(**k_kw, autocommit=True) as k_conn:
        print("  [Smoke 1/3] Checking decoupling isolation on baseline Kimball DB...")
        audit = verify_kimball_isolation(k_conn)
        assert audit.is_pure_kimball is True

        print("  [Smoke 2/3] Checking 6 BR read parity & result digests on baseline DBs (no scenario writes)...")
        parity = verify_full_workload_correctness_and_parity(h_conn, k_conn, include_scenario_write=False)
        for br, d in parity.items():
            print(f"    {br}: Verified digest={d['digest'][:16]}...")

        print("  [Smoke 3/3] Checking workload query execution (1 warmup + 2 samples)...")
        sample_res = benchmark_workload_latencies(h_conn, k_conn, warmups=1, samples_count=2)
        for br, d in sample_res.items():
            print(f"    {br}: Hybrid med={d['hybrid']['median_ms']}ms | Kimball med={d['kimball']['median_ms']}ms")

    print("=== Pre-flight Benchmark Smoke Test PASSED (100% Read-Only, Baseline Preserved) ===")


def main():
    global HYBRID_BENCH_DB, KIMBALL_BENCH_DB, HYBRID_REBUILD_DB, KIMBALL_REBUILD_DB
    parser = argparse.ArgumentParser(description="T25 Benchmark Suite")
    parser.add_argument("--smoke", action="store_true", help="Run bounded smoke test over baseline DBs without pipeline builds")
    parser.add_argument("--analysis-from", type=Path, help="Reuse a completed lifecycle checkpoint; rerun all parity/query/storage/technology checks without claiming new lifecycle measurements")
    parser.add_argument("--continue-from", type=Path, help="Retain completed lifecycle and latency samples; revalidate parity, then repeat storage and technology after a later-stage failure")
    args = parser.parse_args()
    if args.analysis_from and args.continue_from:
        parser.error("Choose one checkpoint mode")
    prior_path = args.analysis_from or args.continue_from
    prior = json.loads(prior_path.read_text()) if prior_path else None
    if prior:
        lifecycle = prior["stages"]["pipeline_lifecycle"]
        assert len(lifecycle["measurement_order"]) == 6
        HYBRID_BENCH_DB = lifecycle["hybrid"]["fresh_database"]
        HYBRID_REBUILD_DB = lifecycle["hybrid"]["rebuild_database"]
        KIMBALL_BENCH_DB = lifecycle["kimball"]["fresh_database"]
        KIMBALL_REBUILD_DB = lifecycle["kimball"]["rebuild_database"]


    if args.smoke:
        run_smoke_test()
        return

    print("==================================================================")
    print("  Smart Greenhouse Architecture & Technology Benchmark Suite (T25)")
    print("==================================================================")

    out_file = OUT_DIR / "benchmark_results.json"
    if out_file.exists():
        previous = out_file.with_name("benchmark_results.previous-" + uuid4().hex[:8] + ".json")
        previous.write_bytes(out_file.read_bytes())
    recorder = IncrementalBenchmarkRecorder(out_file)

    try:
        # STAGE 1: System Metadata
        print("\n[Stage 1/6] Discovering system environment & container cgroup caps...")
        sys_meta = collect_system_metadata()
        if prior:
            original = prior["stages"]["system_metadata"]
            for key in ("raw_manifest_sha256", "postgres_version", "python_version", "packages", "quality_policy", "area_basis"):
                assert sys_meta[key] == original[key], key
            for key in ("cgroup_memory_max_bytes", "cgroup_cpu_max"):
                assert sys_meta["cgroup"][key] == original["cgroup"][key], key
            recorder.data["started_at"] = prior["started_at"]
            recorder.data["resource_measurement_scope"] = "final_runtime covers continuation process only; peak of original lifecycle process was not captured before failure"
            recorder.record_stage("continuation_initial_runtime", sys_meta)
            recorder.retain_stage("system_metadata", prior)
        else:
            recorder.record_stage("system_metadata", sys_meta)
        print("  PostgreSQL: " + sys_meta["postgres_version"].splitlines()[0])
        print(f"  Python: {sys_meta['python_version']} on {sys_meta['platform']}")

        # STAGE 2: Pipeline Lifecycle Benchmarks
        print("\n[Stage 2/6] Executing actual pipeline lifecycle builds & reruns...")
        if prior:
            assert sys_meta["raw_manifest_sha256"] == prior["stages"]["system_metadata"]["raw_manifest_sha256"]
            pipeline_meta = prior["stages"]["pipeline_lifecycle"]
            recorder.record_stage("lifecycle_reuse", {"original_started_at": prior["started_at"], "original_script_sha256": prior.get("stage_script_sha256", {}).get("pipeline_lifecycle"), "checkpoint_sha256": hashlib.sha256(prior_path.read_bytes()).hexdigest()})
        else:
            pipeline_meta = benchmark_pipeline_lifecycle(recorder)
        if prior:
            recorder.retain_stage("pipeline_lifecycle", prior)
        else:
            recorder.record_stage("pipeline_lifecycle", pipeline_meta)
        print(f"  Hybrid Fresh Pipeline:   {pipeline_meta['hybrid']['fresh_pipeline_duration_seconds']}s")
        print(f"  Hybrid Rerun Reuse:      {pipeline_meta['hybrid']['rerun_reuse_duration_seconds']}s")
        print(f"  Hybrid Clean Rebuild:    {pipeline_meta['hybrid']['clean_rebuild_duration_seconds']}s")
        print(f"  Kimball Fresh Pipeline:  {pipeline_meta['kimball']['fresh_pipeline_duration_seconds']}s")
        print(f"  Kimball Rerun Immut:     {pipeline_meta['kimball']['rerun_rejection_duration_seconds']}s (rejected: {pipeline_meta['kimball']['publication_immutability_enforced']})")
        print(f"  Kimball Clean Rebuild:   {pipeline_meta['kimball']['clean_rebuild_duration_seconds']}s")

        # Connect to newly built fresh benchmark databases for query & storage testing
        admin_settings = Settings.from_env()
        h_kwargs = admin_settings.connection_kwargs()
        h_kwargs["dbname"] = HYBRID_BENCH_DB

        k_kwargs = admin_settings.connection_kwargs()
        k_kwargs["dbname"] = KIMBALL_BENCH_DB

        with psycopg.connect(**h_kwargs) as h_conn, psycopg.connect(**k_kwargs) as k_conn:
            assert PostgresClimateQueryAdapter(h_conn).pin_publication(AGC_SCOPE).publication_id == pipeline_meta["hybrid"]["publication_id"]
            assert KimballQueryAdapter(k_conn).pin_publication(AGC_SCOPE).publication_id == pipeline_meta["kimball"]["publication_id"]
            # STAGE 3: Workload Full Correctness & Checksums
            print("\n[Stage 3/6] Verifying full semantic parity and computing canonical checksums across all 6 BRs...")
            parity_meta = verify_full_workload_correctness_and_parity(h_conn, k_conn)
            if args.continue_from:
                for br, value in parity_meta.items():
                    assert value["digest"] == prior["stages"]["workload_parity_verification"][br]["digest"], br
            h_conn.commit()
            k_conn.commit()
            recorder.record_stage("workload_parity_verification", parity_meta)
            for br, d in parity_meta.items():
                print(f"  {br}: Verified=True (Checksum: {d['digest'][:16]}...)")

            # STAGE 4: Workload Latency Benchmarks (6 BRs, 5 warmups, 30 samples)
            print("\n[Stage 4/6] Sampling 6 BR query workloads (fresh adapter touch, 5 warmups, 30 steady-state samples with use_cache=False)...")
            if args.continue_from:
                latencies_meta = prior["stages"]["workload_latencies"]
                assert len(latencies_meta) == 6
                for pair in latencies_meta.values():
                    for arch in ("hybrid", "kimball"):
                        assert pair[arch]["measured_count"] == 30
                        assert len(pair[arch]["measured_samples_ms"]) == 30
                        assert len(pair[arch]["warmup_samples_ms"]) == 5
                recorder.retain_stage("workload_latencies", prior)
                print("  Retained original latency samples after parity revalidation; no new query timing claimed.")
            else:
                latencies_meta = benchmark_workload_latencies(h_conn, k_conn, warmups=5, samples_count=30)
                recorder.record_stage("workload_latencies", latencies_meta)
            for br, res in latencies_meta.items():
                h_p95 = res["hybrid"]["p95_nearest_rank_ms"]
                k_p95 = res["kimball"]["p95_nearest_rank_ms"]
                h_med = res["hybrid"]["median_ms"]
                k_med = res["kimball"]["median_ms"]
                print(f"  {br}: Hybrid p95={h_p95}ms (med={h_med}ms) | Kimball p95={k_p95}ms (med={k_med}ms) | Target Met: {res['kimball']['p95_target_pass']}")

            # STAGE 5: Database Storage Footprint Comparison
            print("\n[Stage 5/6] Auditing database and staging storage footprints on comparable fresh DBs...")
            storage_meta = benchmark_storage_footprint(h_conn, k_conn, pipeline_meta)
            recorder.record_stage("storage_footprint", storage_meta)
            print(f"  Hybrid Fresh DB:  {storage_meta['hybrid']['database_size_bytes']:,} bytes ({storage_meta['hybrid']['database_size_bytes']/(1024*1024):.1f} MiB)")
            print(f"  Hybrid Staging:   {storage_meta['hybrid']['staging_artifact_disk_bytes']:,} bytes ({storage_meta['hybrid']['staging_artifact_disk_bytes']/(1024*1024):.1f} MiB)")
            print(f"  Kimball Fresh DB: {storage_meta['kimball']['database_size_bytes']:,} bytes ({storage_meta['kimball']['database_size_bytes']/(1024*1024):.1f} MiB)")
            print(f"  Kimball / Hybrid DB Ratio: {storage_meta['ratio_kimball_to_hybrid_db_size']:.2%}")

            csv_path = OUT_DIR / "storage_footprint.csv"
            with csv_path.open("w") as f:
                f.write("architecture,schema,table,total_bytes,table_bytes,index_bytes\n")
                for r in storage_meta["hybrid"]["detailed_tables"]:
                    f.write(f"hybrid,{r['schemaname']},{r['tablename']},{r['total_bytes']},{r['table_bytes']},{r['index_bytes']}\n")
                for r in storage_meta["kimball"]["detailed_tables"]:
                    f.write(f"kimball,{r['schemaname']},{r['tablename']},{r['total_bytes']},{r['table_bytes']},{r['index_bytes']}\n")
            print(f"  [OK] Exported {csv_path}")

            # STAGE 6: Technology Loading Comparison (COPY vs Batch INSERT)
            print("\n[Stage 6/6] Benchmarking data ingestion technology (COPY vs Batch INSERT 500/1000)...")
            tech_meta = benchmark_technology_loading(k_conn)
            recorder.record_stage("technology_evaluation", tech_meta)
            print(f"  COPY FROM STDIN:  {tech_meta['copy_from_stdin']['duration_ms']} ms ({tech_meta['copy_from_stdin']['throughput_rows_per_sec']:,} rows/s)")
            print(f"  Batch INSERT 500: {tech_meta['batch_insert_500']['duration_ms']} ms ({tech_meta['batch_insert_500']['throughput_rows_per_sec']:,} rows/s) [{tech_meta['empirical_speedup_copy_vs_batch_500']}x slower]")
            print(f"  Batch INSERT 1000:{tech_meta['batch_insert_1000']['duration_ms']} ms ({tech_meta['batch_insert_1000']['throughput_rows_per_sec']:,} rows/s) [{tech_meta['empirical_speedup_copy_vs_batch_1000']}x slower]")

            tech_path = OUT_DIR / "technology_comparison.json"
            tech_path.write_text(json.dumps(tech_meta, indent=2, default=str) + "\n")
            print(f"  [OK] Exported {tech_path}")

        recorder.record_stage("final_runtime", collect_system_metadata())
        recorder.mark_complete()
        print("\n==================================================================")
        print("  Benchmark Suite Completed Successfully! All 6 Stages Passed.")
        print("==================================================================")

    except Exception as e:
        print(f"\n[ERROR] Benchmark failed: {e}")
        import traceback
        traceback.print_exc()
        recorder.record_stage("final_runtime", collect_system_metadata())
        recorder.record_error("execution", e)
        raise


if __name__ == "__main__":
    main()
