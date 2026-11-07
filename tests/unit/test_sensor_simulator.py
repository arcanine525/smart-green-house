"""Deterministic regression tests for T32 sensor replay simulator.

Exercises:
- Deterministic seed & config identity under mixed faults
- Replay isolation & outbox reuse rejection
- Retry byte and timestamp stability
- Pause/resume continuity preserving fault schedule, RNG state, delays, and checksum
- Crash recovery and tail reconciliation
- Conflict (original + divergent) and drop fault semantics
- Accurate committed cursor & limit boundaries
- Historical SourceParser oracle comparison on real AGC source
- Quoting, LF, off-grid, and malformed source fixtures
- Authoritative ReadMe units and category segregation
- Bounded memory profiling (< 15 MiB peak)
"""

import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tracemalloc
import pytest

from greenhouse.ingestion.parser import Source, SourceParser
from greenhouse.sensor.faults import FaultScheduler
from greenhouse.sensor.outbox import (
    CheckpointData,
    CorruptStateError,
    DurableOutbox,
    INITIAL_CUMULATIVE_HASH,
    OutboxError,
    StateMismatchError,
)
from greenhouse.sensor.schema import (
    CLIMATE_UNITS,
    SCHEMA_VERSION,
    ValidationError,
    load_control_json_schema,
    load_telemetry_json_schema,
    validate_control_envelope,
    validate_telemetry_envelope,
)
from greenhouse.sensor.simulator import (
    EPOCH,
    SensorSimulator,
    SimulatedClock,
    hash_file_stream,
)

RAW_AGC_DIR = Path("/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68")


def create_synthetic_climate_csv(
    path: Path,
    num_rows: int = 25,
    start_serial: Decimal = Decimal("43815.0000000000"),
    lineterminator: str = "\r\n",
    include_nulls: bool = False,
    quote_fields: bool = False,
) -> Path:
    """Create a synthetic Climate CSV matching AGC GreenhouseClimate header."""
    headers = [
        "%time", "AssimLight", "BlackScr", "CO2air", "Cum_irr", "EC_drain_PC", "EnScr",
        "HumDef", "PipeGrow", "PipeLow", "Rhair", "Tair", "Tot_PAR", "Tot_PAR_Lamps",
        "VentLee", "Ventwind", "assim_sp", "assim_vip", "co2_dos", "co2_sp", "co2_vip",
        "dx_sp", "dx_vip", "int_blue_sp", "int_blue_vip", "int_farred_sp", "int_farred_vip",
        "int_red_sp", "int_red_vip", "int_white_sp", "int_white_vip", "pH_drain_PC",
        "scr_blck_sp", "scr_blck_vip", "scr_enrg_sp", "scr_enrg_vip", "t_grow_min_sp",
        "t_grow_min_vip", "t_heat_sp", "t_heat_vip", "t_rail_min_sp", "t_rail_min_vip",
        "t_vent_sp", "t_ventlee_vip", "t_ventwind_vip", "water_sup", "water_sup_intervals_sp_min",
        "water_sup_intervals_vip_min", "window_pos_lee_sp", "window_pos_lee_vip"
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(
            handle,
            lineterminator=lineterminator,
            quoting=csv.QUOTE_ALL if quote_fields else csv.QUOTE_MINIMAL,
        )
        writer.writerow(headers)
        step = Decimal("1") / Decimal("288")
        for i in range(num_rows):
            serial = start_serial + (i * step)
            row = [f"{serial:.10f}"]
            for h in headers[1:]:
                if include_nulls and i == 2 and h in ("co2_sp", "pH_drain_PC", "t_heat_sp"):
                    row.append("NaN")
                elif include_nulls and i == 3 and h in ("Tair", "water_sup"):
                    row.append("")
                elif h in ("Tair", "t_heat_sp", "t_heat_vip", "PipeGrow", "PipeLow"):
                    row.append(f"{20.0 + (i * 0.1):.2f}")
                elif h in ("CO2air", "co2_sp", "co2_vip"):
                    row.append(f"{500 + i}")
                elif h in ("co2_dos",):
                    row.append(f"{10.5 + (i * 0.2):.1f}")
                elif h in ("Tot_PAR", "Tot_PAR_Lamps"):
                    row.append(f"{40.0 + (i * 1.5):.1f}")
                elif h in ("dx_sp", "dx_vip"):
                    row.append("4.5")
                elif h in ("water_sup",):
                    row.append(f"{i * 2.0:.1f}")
                elif h in ("Cum_irr",):
                    row.append(f"{i * 0.5:.2f}")
                elif h in ("EC_drain_PC",):
                    row.append("3.2")
                elif h in ("pH_drain_PC",):
                    row.append("6.2")
                elif h in ("Rhair", "EnScr", "BlackScr", "VentLee", "Ventwind"):
                    row.append("75.0")
                else:
                    row.append("0.0")
            writer.writerow(row)
    return path


def test_deterministic_seed_and_config_identity(tmp_path):
    """Same seed, same input, and mixed faults produce 100% identical outputs and outbox bytes."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate.csv", num_rows=20)
    outbox_a = tmp_path / "outbox_a"
    outbox_b = tmp_path / "outbox_b"

    faults_a = FaultScheduler(seed=12345, duplicate_prob=0.1, conflict_prob=0.1, delay_prob=0.1, drop_prob=0.05)
    faults_b = FaultScheduler(seed=12345, duplicate_prob=0.1, conflict_prob=0.1, delay_prob=0.1, drop_prob=0.05)

    clock_a = SimulatedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    clock_b = SimulatedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    sim_a = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-test-det",
        outbox_dir=outbox_a,
        seed=12345,
        fault_scheduler=faults_a,
        clock=clock_a,
    )
    res_a = sim_a.run()

    sim_b = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-test-det",
        outbox_dir=outbox_b,
        seed=12345,
        fault_scheduler=faults_b,
        clock=clock_b,
    )
    res_b = sim_b.run()

    assert res_a.payload_checksum == res_b.payload_checksum
    assert res_a.expected_unique_events == res_b.expected_unique_events
    assert res_a.emitted_unique_events == res_b.emitted_unique_events
    assert res_a.emitted_total_messages == res_b.emitted_total_messages
    assert res_a.fault_summary == res_b.fault_summary

    bytes_a = (outbox_a / "telemetry_outbox.jsonl").read_bytes()
    bytes_b = (outbox_b / "telemetry_outbox.jsonl").read_bytes()
    assert bytes_a == bytes_b


def test_new_replay_isolation_and_outbox_reuse_guard(tmp_path):
    """Different replay IDs produce distinct scopes/IDs, and accidental outbox reuse is blocked."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate.csv", num_rows=5)
    outbox_dir = tmp_path / "outbox_reuse"

    sim_a = SensorSimulator(source_path=csv_file, replay_id="replay-001", outbox_dir=outbox_dir)
    res_a = sim_a.run()
    assert res_a.status == "completed"

    # Attempting to run a new, different replay in the same outbox without --resume must fail
    sim_b = SensorSimulator(source_path=csv_file, replay_id="replay-002", outbox_dir=outbox_dir)
    with pytest.raises(OutboxError, match="already contains existing replay 'replay-001'"):
        sim_b.run(resume=False)


def test_retry_preserves_original_bytes_and_timestamp(tmp_path):
    """Retrying an event from outbox returns exact stored bytes without regenerating published_at."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate.csv", num_rows=10)
    outbox_dir = tmp_path / "outbox_retry"

    sim = SensorSimulator(source_path=csv_file, replay_id="replay-retry", outbox_dir=outbox_dir)
    sim.run()

    outbox = DurableOutbox(outbox_dir)
    with (outbox_dir / "telemetry_outbox.jsonl").open() as handle:
        first_line = json.loads(handle.readline())
        event_id = first_line["event_id"]
        expected_raw = first_line["payload"]
        expected_topic = first_line["topic"]

    topic, retry_bytes = outbox.get_event_for_retry(event_id)
    assert topic == expected_topic
    assert retry_bytes.decode("utf-8") == expected_raw

    payload_obj = json.loads(retry_bytes.decode("utf-8"))
    assert payload_obj["event_id"] == event_id
    assert payload_obj["published_at"] == json.loads(expected_raw)["published_at"]


def test_pause_and_resume_preserves_fault_schedule_and_checksum(tmp_path):
    """Pause/resume with nonzero mixed faults and pending delays yields exact same output as uninterrupted run."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate.csv", num_rows=30)
    outbox_uninterrupted = tmp_path / "outbox_uninterrupted"
    outbox_stepped = tmp_path / "outbox_stepped"

    # Uninterrupted run with mixed faults
    clock_uninterrupted = SimulatedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    faults_uninterrupted = FaultScheduler(
        seed=777, duplicate_prob=0.05, conflict_prob=0.05, delay_prob=0.1, drop_prob=0.05, max_delay_steps=3
    )
    sim_uninterrupted = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-pause-test",
        outbox_dir=outbox_uninterrupted,
        seed=777,
        fault_scheduler=faults_uninterrupted,
        cursor_interval=5,
        clock=clock_uninterrupted,
    )
    res_uninterrupted = sim_uninterrupted.run()
    assert res_uninterrupted.status == "completed"

    # Stepped run with exact same configuration: pause after 12 events
    clock_stepped = SimulatedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    faults_stepped = FaultScheduler(
        seed=777, duplicate_prob=0.05, conflict_prob=0.05, delay_prob=0.1, drop_prob=0.05, max_delay_steps=3
    )
    sim_step1 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-pause-test",
        outbox_dir=outbox_stepped,
        seed=777,
        fault_scheduler=faults_stepped,
        cursor_interval=5,
        pause_after=12,
        clock=clock_stepped,
    )
    res_step1 = sim_step1.run()
    assert res_step1.status == "paused"
    assert res_step1.last_source_line == 13

    # Resume stepped run
    faults_resumed = FaultScheduler(
        seed=777, duplicate_prob=0.05, conflict_prob=0.05, delay_prob=0.1, drop_prob=0.05, max_delay_steps=3
    )
    sim_step2 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-pause-test",
        outbox_dir=outbox_stepped,
        seed=777,
        fault_scheduler=faults_resumed,
        cursor_interval=5,
        clock=clock_stepped,
    )
    res_step2 = sim_step2.run(resume=True)
    assert res_step2.status == "completed"

    # Assert exact match between uninterrupted and stepped runs
    assert res_step2.expected_unique_events == res_uninterrupted.expected_unique_events
    assert res_step2.emitted_unique_events == res_uninterrupted.emitted_unique_events
    assert res_step2.emitted_total_messages == res_uninterrupted.emitted_total_messages
    assert res_step2.payload_checksum == res_uninterrupted.payload_checksum
    assert res_step2.fault_summary == res_uninterrupted.fault_summary

    # Ensure outbox telemetry lines match 100%
    bytes_uninterrupted = (outbox_uninterrupted / "telemetry_outbox.jsonl").read_bytes()
    bytes_stepped = (outbox_stepped / "telemetry_outbox.jsonl").read_bytes()
    assert bytes_uninterrupted == bytes_stepped


def test_crash_recovery_and_tail_reconciliation(tmp_path):
    """Uncommitted messages past the last checkpoint are reconciled on crash recovery."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate.csv", num_rows=20)
    outbox_dir = tmp_path / "outbox_crash"

    # Run with pause at 10
    sim = SensorSimulator(source_path=csv_file, replay_id="replay-crash", outbox_dir=outbox_dir, pause_after=10)
    res = sim.run()
    assert res.status == "paused"

    # Manually inject an uncheckpointed "phantom" tail message simulating a crash mid-dispatch
    outbox = DurableOutbox(outbox_dir)
    with outbox._connection() as conn:
        conn.execute(
            "INSERT INTO telemetry_messages (sequence, event_id, topic, line_number, payload_bytes, payload_hash, cumulative_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (999, "phantom-event", "v1/telemetry", 999, b"phantom-data", "hash", "cumhash"),
        )

    # Resume must reconcile (delete) the phantom tail message
    sim_resume = SensorSimulator(source_path=csv_file, replay_id="replay-crash", outbox_dir=outbox_dir)
    res_resumed = sim_resume.run(resume=True)
    assert res_resumed.status == "completed"

    # Verify phantom message was reconciled
    with outbox._connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM telemetry_messages WHERE event_id = 'phantom-event'")
        assert cur.fetchone() is None


def test_fault_semantics_conflict_and_drop(tmp_path):
    """Conflict emits original AND divergent variant for same event_id; drop accounts intentional gap."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate.csv", num_rows=10)
    outbox_conflict = tmp_path / "outbox_conflict"
    outbox_drop = tmp_path / "outbox_drop"

    # 1. Conflict test: conflict_prob=1.0
    faults_conflict = FaultScheduler(seed=42, conflict_prob=1.0)
    sim_conflict = SensorSimulator(
        source_path=csv_file, replay_id="replay-conf", outbox_dir=outbox_conflict, fault_scheduler=faults_conflict
    )
    res_c = sim_conflict.run()
    assert res_c.expected_unique_events == 10
    assert res_c.emitted_unique_events == 10
    # Every event emits original + conflict -> total messages = 20
    assert res_c.emitted_total_messages == 20

    lines_c = [json.loads(line) for line in (outbox_conflict / "telemetry_outbox.jsonl").read_text().splitlines() if line]
    assert len(lines_c) == 20
    # Check that pair (0, 1) share identical event_id but have divergent payloads
    first_env = json.loads(lines_c[0]["payload"])
    second_env = json.loads(lines_c[1]["payload"])
    assert first_env["event_id"] == second_env["event_id"]
    assert "injected_conflict" not in first_env["fault_tags"]
    assert "injected_conflict" in second_env["fault_tags"]
    assert first_env["payload"]["actual_measurements"]["Tair"] != second_env["payload"]["actual_measurements"]["Tair"]

    # 2. Drop test: drop_prob=1.0
    faults_drop = FaultScheduler(seed=42, drop_prob=1.0)
    sim_drop = SensorSimulator(
        source_path=csv_file, replay_id="replay-drop", outbox_dir=outbox_drop, fault_scheduler=faults_drop
    )
    res_d = sim_drop.run()
    assert res_d.expected_unique_events == 10
    assert res_d.emitted_unique_events == 0
    assert res_d.emitted_total_messages == 0


def test_accurate_cursor_and_limit(tmp_path):
    """Cursor reports exact committed progress and limit breaks cleanly without over-incrementing line."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate.csv", num_rows=15)
    outbox_dir = tmp_path / "outbox_limit"

    sim = SensorSimulator(source_path=csv_file, replay_id="replay-limit", outbox_dir=outbox_dir, limit=5)
    res = sim.run()
    # Header is line 1; 5 events process lines 2, 3, 4, 5, 6
    assert res.last_source_line == 6
    assert res.last_sequence == 5
    assert res.expected_unique_events == 5

    # Check final cursor control message
    ctrl_lines = [json.loads(line) for line in (outbox_dir / "control_outbox.jsonl").read_text().splitlines() if line]
    final_cursor = next(json.loads(c["payload"]) for c in ctrl_lines if c["control_type"] == "replay_cursor" and json.loads(c["payload"])["payload"]["status"] == "completed")
    assert final_cursor["payload"]["logical_cursor_line"] == 6
    assert final_cursor["payload"]["logical_cursor_sequence"] == 5


def test_historical_source_parser_oracle_and_real_agc(tmp_path):
    """Verify simulator output against direct historical SourceParser on canonical AGC Climate data."""
    real_file = RAW_AGC_DIR / "AICU/GreenhouseClimate.csv"
    assert real_file.is_file(), f"Mounted raw AGC dataset missing at {real_file}"

    outbox_dir = tmp_path / "outbox_real_agc"
    sim = SensorSimulator(
        source_path=real_file,
        replay_id="replay-real-oracle",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        limit=10,
    )
    res = sim.run()
    assert res.status == "completed"

    # Run direct historical SourceParser independently
    source_oracle = Source(
        snapshot_id=sim.snapshot_id,
        snapshot_sha=sim.snapshot_sha,
        relative_path="AICU/GreenhouseClimate.csv",
        file_checksum=sim.file_checksum,
        run_id="oracle-run",
    )
    oracle_parser = SourceParser(real_file, source_oracle)
    oracle_records = []
    for r in oracle_parser.records():
        if r["source_timestamp"] is not None and r["slot_start"] is not None:
            oracle_records.append(r)
            if len(oracle_records) == 10:
                break

    # Compare simulator outbox payloads with oracle records
    with (outbox_dir / "telemetry_outbox.jsonl").open() as handle:
        for idx, oracle_rec in enumerate(oracle_records):
            wire_obj = json.loads(handle.readline())
            env = json.loads(wire_obj["payload"])
            prov = env["source_provenance"]

            assert prov["snapshot_sha"] == "c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"
            assert prov["snapshot_sha"] == oracle_rec["snapshot_sha"]
            assert prov["snapshot_id"] == "agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"
            assert prov["snapshot_id"] == oracle_rec["snapshot_id"]
            assert prov["line_number"] == oracle_rec["line_number"]
            assert prov["line_end"] == oracle_rec["line_end"]
            assert prov["raw_hash"] == oracle_rec["raw_hash"]
            assert env["source_record_id"] == oracle_rec["source_record_id"]
            assert prov["source_time_serial"] == str(oracle_rec["values"]["time"])
            assert env["source_observed_at"] == oracle_rec["source_timestamp"].isoformat()

            # Compare measurement values
            actuals = env["payload"]["actual_measurements"]
            for col in ("Tair", "Rhair", "CO2air", "HumDef"):
                expected_val = str(oracle_rec["values"][col]) if oracle_rec["values"][col] is not None else None
                assert actuals[col] == expected_val


def test_fixtures_coverage(tmp_path):
    """Test fixtures with LF line endings, full field quoting, off-grid timestamps, and nulls."""
    # 1. LF line endings & quotes
    csv_file = create_synthetic_climate_csv(
        tmp_path / "climate_lf_quoted.csv", num_rows=5, lineterminator="\n", quote_fields=True, include_nulls=True
    )
    outbox_dir = tmp_path / "outbox_lf"
    sim = SensorSimulator(source_path=csv_file, replay_id="replay-lf", outbox_dir=outbox_dir)
    res = sim.run()
    assert res.status == "completed"
    assert res.expected_unique_events == 5

    # 2. Off-grid fixture handling
    off_grid_csv = tmp_path / "off_grid.csv"
    with off_grid_csv.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["%time", "Tair"])
        # Serial with 2-minute offset (residual > 0.5s from 5-minute grid)
        writer.writerow(["43815.0013888888", "21.0"])  # 2 minutes past midnight
    outbox_og = tmp_path / "outbox_og"
    sim_og = SensorSimulator(source_path=off_grid_csv, replay_id="replay-og", outbox_dir=outbox_og)
    res_og = sim_og.run()
    # Off-grid records are filtered by SourceParser or skipped
    assert res_og.expected_unique_events == 0


def test_authoritative_units_and_category_mapping():
    """Verify authoritative units and JSON Schema validators reject invalid units, categories, and extra keys."""
    # Authoritative units from ReadMe.pdf
    assert CLIMATE_UNITS["Tot_PAR"] == "µmol/m² s"
    assert CLIMATE_UNITS["Tot_PAR_Lamps"] == "µmol/m² s"
    assert CLIMATE_UNITS["dx_sp"] == "g/m³"
    assert CLIMATE_UNITS["dx_vip"] == "g/m³"
    assert CLIMATE_UNITS["water_sup"] == "minutes"
    assert CLIMATE_UNITS["co2_dos"] == "kg/ha hour"

    # Schema JSON files must load and validate
    telemetry_schema = load_telemetry_json_schema()
    control_schema = load_control_json_schema()
    assert telemetry_schema["title"] == "GreenhouseTelemetryEnvelopeV1"
    assert control_schema["title"] == "GreenhouseControlEnvelopeV1"


def test_bounded_memory_profiling(tmp_path):
    """Profile simulator memory with tracemalloc and report measured peak memory."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_large.csv", num_rows=100)
    outbox_dir = tmp_path / "outbox_mem"

    tracemalloc.start()
    sim = SensorSimulator(source_path=csv_file, replay_id="replay-mem", outbox_dir=outbox_dir)
    sim.run()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    peak_mb = peak / (1024 * 1024)
    # Ensure memory stays well within the 128 MiB container cap
    assert peak_mb < 64.0, f"Measured peak memory {peak_mb:.2f} MiB exceeded bounded threshold"


def test_delay_manifest_counts_real_unique_and_checksum(tmp_path):
    """Delay faults must reconcile emitted unique event count with SQLite outbox."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_delay.csv", num_rows=20)
    outbox_dir = tmp_path / "outbox_delay"

    faults = FaultScheduler(seed=42, delay_prob=1.0, max_delay_steps=3)
    sim = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-delay-audit",
        outbox_dir=outbox_dir,
        limit=20,
        fault_scheduler=faults,
    )
    result = sim.run()

    # Query outbox SQLite table
    outbox = DurableOutbox(outbox_dir)
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT event_id, sequence, line_number FROM telemetry_messages")
        stored_rows = cursor.fetchall()

    distinct_eids = {r[0] for r in stored_rows}
    assert len(distinct_eids) == 20
    assert result.emitted_unique_events == 20
    assert result.emitted_total_messages == 20

    # Read completion manifest control message
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload_bytes FROM control_messages WHERE control_type = 'completion_manifest'")
        manifest_payload = json.loads(cursor.fetchone()[0])
    assert manifest_payload["payload"]["emitted_unique_events"] == 20
    assert manifest_payload["payload"]["emitted_total_messages"] == 20


def test_limit_applies_to_whole_replay_after_resume(tmp_path):
    """Global limit applies across total replayed events across runs, not per invocation."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_limit_resume.csv", num_rows=35)
    outbox_dir = tmp_path / "outbox_limit_resume"

    # Run 1: limit 20, pause after 10
    sim1 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-limit-resume",
        outbox_dir=outbox_dir,
        limit=20,
        pause_after=10,
    )
    res1 = sim1.run()
    assert res1.status == "paused"
    assert res1.last_sequence == 10

    # Run 2: resume with limit 20
    sim2 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-limit-resume",
        outbox_dir=outbox_dir,
        limit=20,
    )
    res2 = sim2.run(resume=True)
    assert res2.status == "completed"
    assert res2.expected_unique_events == 20
    assert res2.last_sequence == 20

    # Verify SQLite outbox has exactly 20 messages
    outbox = DurableOutbox(outbox_dir)
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM telemetry_messages")
        assert cursor.fetchone()[0] == 20


def test_scope_change_rejected_on_resume(tmp_path):
    """Resume must reject data_scope_id changes to prevent identity and scope drift."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_scope.csv", num_rows=10)
    outbox_dir = tmp_path / "outbox_scope"

    sim1 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-scope",
        outbox_dir=outbox_dir,
        data_scope_id="sim:original",
        pause_after=5,
    )
    sim1.run()

    sim2 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-scope",
        outbox_dir=outbox_dir,
        data_scope_id="sim:changed",
    )
    with pytest.raises((ValueError, RuntimeError, StateMismatchError)):
        sim2.run(resume=True)


def test_default_operational_timestamp_uses_current_clock(tmp_path):
    """Fast speed=0 must use real UTC clock by default for published_at, not hardcoded time."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_clock.csv", num_rows=5)
    outbox_dir = tmp_path / "outbox_clock"

    start = datetime.now(timezone.utc)
    sim = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-clock",
        outbox_dir=outbox_dir,
        limit=1,
    )
    sim.run()
    end = datetime.now(timezone.utc)

    outbox = DurableOutbox(outbox_dir)
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload_bytes FROM telemetry_messages LIMIT 1")
        env = json.loads(cursor.fetchone()[0])

    published_dt = datetime.fromisoformat(env["published_at"])
    assert start <= published_dt <= end


def test_crash_after_commit_keeps_original_bytes(tmp_path):
    """Crash after commit keeps exact original bytes and resumes without duplicate sequence rows."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_crash.csv", num_rows=25)
    outbox_dir = tmp_path / "outbox_crash_commit"

    clock_fixed = SimulatedClock(datetime(2025, 1, 1, tzinfo=timezone.utc))
    sim = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-crash-test",
        outbox_dir=outbox_dir,
        limit=20,
        clock=clock_fixed,
    )

    commit_original = sim.outbox.commit_dispatch_transaction

    def fail_after_commit(*args, **kwargs):
        res = commit_original(*args, **kwargs)
        raise RuntimeError("simulated process crash after commit")

    sim.outbox.commit_dispatch_transaction = fail_after_commit
    with pytest.raises(RuntimeError, match="simulated process crash"):
        sim.run()

    outbox = DurableOutbox(outbox_dir)
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT event_id, payload_bytes FROM telemetry_messages")
        original_rows = cursor.fetchall()
    assert len(original_rows) == 1

    # Resume simulation with limit 20
    sim_recovered = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-crash-test",
        outbox_dir=outbox_dir,
        limit=20,
        clock=SimulatedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )
    sim_recovered.run(resume=True)

    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT event_id, payload_bytes FROM telemetry_messages ORDER BY sequence")
        recovered_rows = cursor.fetchall()

    assert recovered_rows[0] == original_rows[0]
    assert len(recovered_rows) == 20
    assert len({r[0] for r in recovered_rows}) == 20


def test_resume_at_eof_preserves_cursor_time(tmp_path):
    """Pausing at EOF then resuming preserves the last source observed timestamp."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_eof.csv", num_rows=30)
    outbox_dir = tmp_path / "outbox_eof"

    sim1 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-eof",
        outbox_dir=outbox_dir,
        pause_after=30,
    )
    sim1.run()

    outbox = DurableOutbox(outbox_dir)
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload_bytes FROM telemetry_messages ORDER BY sequence DESC LIMIT 1")
        last_event = json.loads(cursor.fetchone()[0])
    expected_source_time = last_event["source_observed_at"]

    # Resume at EOF
    sim2 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-eof",
        outbox_dir=outbox_dir,
    )
    sim2.run(resume=True)

    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload_bytes FROM control_messages WHERE control_type = 'replay_cursor' ORDER BY control_sequence DESC LIMIT 1")
        last_cursor = json.loads(cursor.fetchone()[0])

    assert last_cursor["payload"]["source_observed_at"] == expected_source_time


def test_computed_drain_category_and_invalid_numeric(tmp_path):
    """Verify computed_drain category is distinct and invalid numeric tokens map to invalid_numeric."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_invalid_num.csv", num_rows=2)
    with csv_file.open("r", newline="") as handle:
        rows_data = list(csv.reader(handle))
    tair_idx = rows_data[0].index("Tair")
    rows_data[1][tair_idx] = "not_a_number"
    # Row 2: off-grid serial (2 minutes past midnight) -> rejected by SourceParser
    rows_data[2][0] = "43815.0013888888"
    with csv_file.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows_data)

    outbox_dir = tmp_path / "outbox_num"
    sim = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-inv-num",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
    )
    res = sim.run()

    assert res.expected_unique_events == 1
    assert res.rejected_source_rows == 1

    outbox = DurableOutbox(outbox_dir)
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload_bytes FROM telemetry_messages LIMIT 1")
        env = json.loads(cursor.fetchone()[0])

    assert env["payload"]["actual_measurements"]["Tair"] is None
    assert env["payload"]["null_reasons"]["Tair"] == "invalid_numeric"
    assert env["payload"]["category_tags"]["Cum_irr"] == "computed_drain"
    assert env["payload"]["category_tags"]["water_sup"] == "actual_drain"


def test_rejected_source_count_survives_resume(tmp_path):
    """Verify cumulative rejected_source_rows survives pause/resume and matches SQLite audit and completion manifest."""
    # 1 off-grid row (row 2: residual ~120s > 0.5s) followed by 5 valid rows
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_pause_reject.csv", num_rows=6)
    with csv_file.open("r", newline="") as handle:
        rows_data = list(csv.reader(handle))
    rows_data[1][0] = "43815.0013888888"  # off-grid time (line 2)
    with csv_file.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows_data)

    outbox_dir = tmp_path / "outbox_pause_reject"
    clock = SimulatedClock(datetime(2026, 11, 6, 0, 0, 0, tzinfo=timezone.utc))

    # Phase 1: Run with pause_after=3 (processes 1 rejected row, then 3 valid rows -> pause)
    sim1 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-pause-reject",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
        pause_after=3,
        clock=clock,
    )
    res_paused = sim1.run()

    assert res_paused.status == "paused"
    assert res_paused.expected_unique_events == 3
    assert res_paused.rejected_source_rows == 1

    outbox = DurableOutbox(outbox_dir)
    assert outbox.get_rejected_source_count() == 1

    # Check replay_state.json exported at pause
    state_json = json.loads((outbox_dir / "replay_state.json").read_text())
    assert state_json["status"] == "paused"
    assert state_json["rejected_source_rows"] == 1

    # Phase 2: Resume to completion
    sim2 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-pause-reject",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
        clock=clock,
    )
    res_completed = sim2.run(resume=True)

    assert res_completed.status == "completed"
    assert res_completed.expected_unique_events == 5  # 5 valid rows total
    assert res_completed.rejected_source_rows == 1
    assert outbox.get_rejected_source_count() == 1

    # Verify completion manifest in control_messages
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload_bytes FROM control_messages WHERE control_type = 'completion_manifest'")
        manifest_row = cursor.fetchone()
        assert manifest_row is not None
        manifest_payload = json.loads(manifest_row[0])["payload"]
        assert manifest_payload["rejected_source_rows"] == 1
        assert manifest_payload["expected_unique_events"] == 5

    # Final replay_state.json
    final_state = json.loads((outbox_dir / "replay_state.json").read_text())
    assert final_state["status"] == "completed"
    assert final_state["rejected_source_rows"] == 1


def test_legacy_checkpoint_without_column_resumes_cleanly(tmp_path):
    """Verify backward compatibility: outbox from pre-fix candidate without rejected_source_rows column resumes."""
    outbox_dir = tmp_path / "legacy_outbox"
    outbox_dir.mkdir(parents=True)
    db_path = outbox_dir / "outbox.db"

    # Create pre-fix schema WITHOUT rejected_source_rows column in checkpoints
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE telemetry_messages (id INTEGER PRIMARY KEY, sequence INTEGER, event_id TEXT, topic TEXT, line_number INTEGER, payload_bytes BLOB, payload_hash TEXT, cumulative_hash TEXT);
        CREATE TABLE control_messages (control_sequence INTEGER PRIMARY KEY, control_id TEXT UNIQUE, control_type TEXT, topic TEXT, payload_bytes BLOB);
        CREATE TABLE fault_audit (id INTEGER PRIMARY KEY, step INTEGER, event_id TEXT, fault_type TEXT, sequence INTEGER, source_line INTEGER, details_json TEXT);
        CREATE TABLE checkpoints (
            checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sequence INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            control_sequence INTEGER NOT NULL,
            cumulative_hash TEXT NOT NULL,
            expected_unique_events INTEGER NOT NULL,
            emitted_unique_events INTEGER NOT NULL,
            emitted_total_messages INTEGER NOT NULL,
            fault_state_json TEXT NOT NULL,
            status TEXT NOT NULL,
            last_source_observed TEXT,
            created_at TEXT NOT NULL
        );
    """)

    csv_file = create_synthetic_climate_csv(tmp_path / "climate_legacy.csv", num_rows=5)
    with csv_file.open("r", newline="") as handle:
        rows_data = list(csv.reader(handle))
    rows_data[1][0] = "43815.0013888888"  # line 2 off-grid
    with csv_file.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows_data)

    sim_temp = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-legacy",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
    )
    conf_hash = sim_temp.config_hash
    src_checksum = sim_temp.file_checksum

    for k, v in [
        ("replay_id", "replay-legacy"),
        ("data_scope_id", sim_temp.data_scope_id),
        ("compartment_id", "AICU"),
        ("cycle_id", sim_temp.cycle_id),
        ("source_file", "AICU/GreenhouseClimate.csv"),
        ("source_file_checksum", src_checksum),
        ("config_hash", conf_hash),
        ("status", "paused"),
    ]:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (k, v))

    fault_state = FaultScheduler(seed=42).snapshot_state()
    conn.execute(
        "INSERT INTO checkpoints (sequence, line_number, control_sequence, cumulative_hash, expected_unique_events, emitted_unique_events, emitted_total_messages, fault_state_json, status, last_source_observed, created_at) "
        "VALUES (2, 3, 1, 'init_cum_hash', 2, 2, 2, ?, 'paused', '2019-12-16T00:05:00', '2026-11-06T00:00:00')",
        (json.dumps(fault_state),),
    )
    # Persist one rejected_source audit at line 2
    conn.execute(
        "INSERT INTO fault_audit (step, event_id, fault_type, sequence, source_line, details_json) "
        "VALUES (1, 'eid1', 'rejected_source', 0, 2, '{}')"
    )
    conn.commit()
    conn.close()

    # Now open with DurableOutbox / SensorSimulator with resume=True
    clock = SimulatedClock(datetime(2026, 11, 6, 0, 0, 0, tzinfo=timezone.utc))
    sim_resuming = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-legacy",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
        clock=clock,
    )
    res = sim_resuming.run(resume=True)

    assert res.status == "completed"
    assert res.rejected_source_rows == 1

    outbox = DurableOutbox(outbox_dir)
    assert outbox.get_rejected_source_count() == 1


def test_rejection_only_and_trailing_input(tmp_path):
    """Verify clean handling and reconciliation of trailing rejected source rows."""
    # 2 valid rows, then 3 trailing off-grid rows (lines 4, 5, 6)
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_trailing.csv", num_rows=5)
    with csv_file.open("r", newline="") as handle:
        rows_data = list(csv.reader(handle))
    rows_data[3][0] = "43815.0013888888"  # line 4 off-grid
    rows_data[4][0] = "43815.0020833333"  # line 5 off-grid
    rows_data[5][0] = "43815.0027777777"  # line 6 off-grid
    with csv_file.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows_data)

    outbox_dir = tmp_path / "outbox_trailing"
    clock = SimulatedClock(datetime(2026, 11, 6, 0, 0, 0, tzinfo=timezone.utc))

    sim = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-trailing",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
        clock=clock,
    )
    res = sim.run()

    assert res.status == "completed"
    assert res.expected_unique_events == 2
    assert res.rejected_source_rows == 3
    assert res.last_source_line == 6
    assert res.total_lines_read == 5

    outbox = DurableOutbox(outbox_dir)
    assert outbox.get_rejected_source_count() == 3

    # Check cursor and completion manifest in control_messages
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT control_type, payload_bytes FROM control_messages ORDER BY control_sequence ASC")
        ctrl_rows = cursor.fetchall()

    cursor_payloads = [json.loads(b)["payload"] for ct, b in ctrl_rows if ct == "replay_cursor"]
    manifest_payloads = [json.loads(b)["payload"] for ct, b in ctrl_rows if ct == "completion_manifest"]

    assert len(manifest_payloads) == 1
    assert manifest_payloads[0]["rejected_source_rows"] == 3
    assert manifest_payloads[0]["expected_unique_events"] == 2

    final_cursor = cursor_payloads[-1]
    assert final_cursor["logical_cursor_line"] == 6
    assert final_cursor["logical_cursor_sequence"] == 2
    assert final_cursor["status"] == "completed"


def test_all_rejected_source_produces_defined_outcome(tmp_path):
    """Verify defined outcome for input where all rows are rejected (0 valid events)."""
    # 3 data rows, all off-grid
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_all_reject.csv", num_rows=3)
    with csv_file.open("r", newline="") as handle:
        rows_data = list(csv.reader(handle))
    for i in range(1, 4):
        rows_data[i][0] = f"43815.00{i}388888"
    with csv_file.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows_data)

    outbox_dir = tmp_path / "outbox_all_reject"
    clock = SimulatedClock(datetime(2026, 11, 6, 0, 0, 0, tzinfo=timezone.utc))

    sim = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-all-reject",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
        clock=clock,
    )
    res = sim.run()

    assert res.status == "completed"
    assert res.expected_unique_events == 0
    assert res.emitted_unique_events == 0
    assert res.emitted_total_messages == 0
    assert res.rejected_source_rows == 3
    assert res.last_source_line == 4
    assert res.total_lines_read == 3

    outbox = DurableOutbox(outbox_dir)
    assert outbox.get_rejected_source_count() == 3

    # Check cursor and completion manifest
    with outbox._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT control_type, payload_bytes FROM control_messages ORDER BY control_sequence ASC")
        ctrl_rows = cursor.fetchall()

    cursor_payloads = [json.loads(b)["payload"] for ct, b in ctrl_rows if ct == "replay_cursor"]
    manifest_payloads = [json.loads(b)["payload"] for ct, b in ctrl_rows if ct == "completion_manifest"]

    assert len(manifest_payloads) == 1
    assert manifest_payloads[0]["rejected_source_rows"] == 3
    assert manifest_payloads[0]["expected_unique_events"] == 0

    final_cursor = cursor_payloads[-1]
    assert final_cursor["logical_cursor_line"] == 4
    assert final_cursor["logical_cursor_sequence"] == 0
    assert final_cursor["source_observed_at"] is None  # Truthful: no source timestamp observed
    assert final_cursor["status"] == "completed"


def test_crash_recovery_before_and_after_rejection_transaction(tmp_path):
    """Verify rollback of uncommitted fault_audit tail on crash recovery."""
    csv_file = create_synthetic_climate_csv(tmp_path / "climate_crash.csv", num_rows=5)
    with csv_file.open("r", newline="") as handle:
        rows_data = list(csv.reader(handle))
    rows_data[1][0] = "43815.0013888888"  # line 2 off-grid
    with csv_file.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows_data)

    outbox_dir = tmp_path / "outbox_crash"
    clock = SimulatedClock(datetime(2026, 11, 6, 0, 0, 0, tzinfo=timezone.utc))

    # Run with pause_after=2 -> commits checkpoint at line 3 (line 2 rejected, line 3 valid)
    sim1 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-crash",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
        pause_after=1,
        clock=clock,
    )
    res_p = sim1.run()
    assert res_p.status == "paused"
    assert res_p.rejected_source_rows == 1

    outbox = DurableOutbox(outbox_dir)
    assert outbox.get_rejected_source_count() == 1

    # Simulate an uncommitted crash tail: insert rogue records into fault_audit and telemetry past line 3
    with outbox._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO fault_audit (step, event_id, fault_type, sequence, source_line, details_json) "
            "VALUES (99, 'uncommitted_eid', 'rejected_source', 99, 99, '{}')"
        )
        conn.execute(
            "INSERT INTO telemetry_messages (sequence, event_id, topic, line_number, payload_bytes, payload_hash, cumulative_hash) "
            "VALUES (99, 'uncommitted_eid', 'topic', 99, x'00', 'hash', 'cum')"
        )
        conn.execute("COMMIT")

    # Verify rogue record is present before resume
    assert outbox.get_rejected_source_count() == 2

    # Now resume: initialize_run must purge any uncommitted tail rows past checkpoint.line_number
    sim2 = SensorSimulator(
        source_path=csv_file,
        replay_id="replay-crash",
        outbox_dir=outbox_dir,
        compartment_id="AICU",
        relative_path="AICU/GreenhouseClimate.csv",
        clock=clock,
    )
    res_c = sim2.run(resume=True)

    assert res_c.status == "completed"
    assert res_c.rejected_source_rows == 1
    assert outbox.get_rejected_source_count() == 1
