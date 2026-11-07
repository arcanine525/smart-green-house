"""Sensor replay simulator for Greenhouse Climate telemetry.

Streams source Climate rows via historical SourceParser, builds validated versioned
envelopes, runs deterministic fault scheduling, and persists to a transactional outbox
with Merkle hash chaining, bounded memory (< 15 MiB), atomic single-dispatch transactions,
and crash resilience.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

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
    ACTUAL_CLIMATE_COLUMNS,
    ACTUAL_DRAIN_COLUMNS,
    ACTUATOR_COLUMNS,
    CLIMATE_UNITS,
    COLUMN_TO_CATEGORY,
    COMPUTED_ACTUATOR_COLUMNS,
    COMPUTED_CLIMATE_COLUMNS,
    COMPUTED_DRAIN_COLUMNS,
    CONTROL_SCHEMA_VERSION,
    CONTROLLER_REALIZED_COLUMNS,
    CONTROLLER_REQUESTED_COLUMNS,
    DRAIN_COLUMNS,
    SCHEMA_VERSION,
    validate_control_envelope,
    validate_telemetry_envelope,
)

EPOCH = datetime(1899, 12, 30)


STANDARD_AGC_SHA = "c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"
STANDARD_AGC_SNAPSHOT = "agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"


def hash_file_stream(path: Path) -> str:
    """Stream-hash a file in 64 KiB chunks to preserve strict memory bounds."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class BaseClock(ABC):
    @abstractmethod
    def now_utc(self) -> datetime:
        """Return the current offset-aware UTC time."""

    @abstractmethod
    def sleep(self, seconds: float) -> None:
        """Sleep or step virtual time by seconds."""


class WallClock(BaseClock):
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class SimulatedClock(BaseClock):
    """Deterministic virtual clock for reproducible testing."""

    def __init__(self, start_utc: datetime | None = None):
        self._current = start_utc or datetime(2026, 11, 6, 0, 0, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


@dataclass(frozen=True)
class SimulationResult:
    replay_id: str
    data_scope_id: str
    compartment_id: str
    status: str
    total_lines_read: int
    expected_unique_events: int
    emitted_unique_events: int
    emitted_total_messages: int
    payload_checksum: str
    fault_summary: dict[str, Any]
    last_source_line: int
    last_sequence: int
    rejected_source_rows: int = 0


class SensorSimulator:
    """Deterministic Climate sensor replay engine."""

    def __init__(
        self,
        source_path: Path | str,
        replay_id: str,
        outbox_dir: Path | str,
        compartment_id: str = "AICU",
        cycle_id: str | None = None,
        data_scope_id: str | None = None,
        snapshot_id: str = STANDARD_AGC_SNAPSHOT,
        snapshot_sha: str = STANDARD_AGC_SHA,
        relative_path: str | None = None,
        speed: float = 0.0,
        seed: int = 42,
        cursor_interval: int = 10,
        pause_after: int | None = None,
        limit: int | None = None,
        fault_scheduler: FaultScheduler | None = None,
        clock: BaseClock | None = None,
    ):
        self.source_path = Path(source_path)
        if not self.source_path.is_file():
            raise FileNotFoundError(f"Source file not found: {self.source_path}")

        if not isinstance(replay_id, str) or not replay_id.strip():
            raise ValueError("replay_id must be a nonempty string")
        if speed < 0 or not Decimal(str(speed)).is_finite():
            raise ValueError(f"speed must be nonnegative finite float, got {speed}")
        if limit is not None and (limit < 1 or type(limit) is not int):
            raise ValueError(f"limit must be positive integer, got {limit}")
        if pause_after is not None and (pause_after < 1 or type(pause_after) is not int):
            raise ValueError(f"pause_after must be positive integer, got {pause_after}")
        if cursor_interval < 1 or type(cursor_interval) is not int:
            raise ValueError(f"cursor_interval must be positive integer, got {cursor_interval}")

        self.replay_id = replay_id
        self.compartment_id = compartment_id
        self.cycle_id = cycle_id or f"agc-cycle-{compartment_id}"
        self.relative_path = relative_path or f"{compartment_id}/GreenhouseClimate.csv"
        self.speed = speed
        self.seed = seed
        self.cursor_interval = cursor_interval
        self.pause_after = pause_after
        self.limit = limit

        self.outbox = DurableOutbox(outbox_dir)
        # Default operational clock is ALWAYS real UTC WallClock at every speed
        self.clock = clock or WallClock()
        self.faults = fault_scheduler or FaultScheduler(seed=seed)

        # Stream-hash source file (constant memory bounded)
        self.file_checksum = hash_file_stream(self.source_path)

        # Verify source membership against raw snapshot manifest.json
        manifest_path = None
        for candidate in [
            self.source_path.parent.parent / "manifest.json",
            Path(os.environ.get("GREENHOUSE_RAW_DIR", "/data/raw")) / f"agc-2nd-2019/{STANDARD_AGC_SHA}/manifest.json",
            Path(f"/data/raw/agc-2nd-2019/{STANDARD_AGC_SHA}/manifest.json"),
            Path(f"data/raw/agc-2nd-2019/{STANDARD_AGC_SHA}/manifest.json"),
        ]:
            if candidate.is_file():
                manifest_path = candidate
                break

        is_pinned_agc = False
        if manifest_path is not None:
            try:
                manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                archive_sha = manifest_data.get("archive", {}).get("sha256")
                manifest_snap_id = manifest_data.get("snapshot_id")
                for f in manifest_data.get("files", []):
                    if f.get("sha256") == self.file_checksum:
                        is_pinned_agc = True
                        self.snapshot_id = manifest_snap_id or STANDARD_AGC_SNAPSHOT
                        self.snapshot_sha = archive_sha or STANDARD_AGC_SHA
                        break
            except Exception:
                is_pinned_agc = False

        if not is_pinned_agc:
            # Custom test fixture or non-pinned source: truthfully label with distinct custom metadata
            self.snapshot_id = f"custom-source-{self.file_checksum[:16]}"
            self.snapshot_sha = self.file_checksum

        self.data_scope_id = data_scope_id or f"{self.snapshot_id}:sim-{replay_id}"

        # Telemetry & control topic patterns
        self.telemetry_topic = f"v1/telemetry/{self.replay_id}/{self.compartment_id}"
        self.control_topic = f"v1/control/{self.replay_id}"
        self.device_id = f"sim-climate-{self.compartment_id}"

        # Canonical JSON serialized config hash pin
        config_dict = {
            "compartment_id": self.compartment_id,
            "conflict_prob": self.faults.conflict_prob,
            "cursor_interval": self.cursor_interval,
            "cycle_id": self.cycle_id,
            "data_scope_id": self.data_scope_id,
            "delay_prob": self.faults.delay_prob,
            "drop_prob": self.faults.drop_prob,
            "duplicate_prob": self.faults.duplicate_prob,
            "file_checksum": self.file_checksum,
            "limit": self.limit,
            "max_delay_steps": self.faults.max_delay_steps,
            "relative_path": self.relative_path,
            "seed": self.seed,
            "snapshot_id": self.snapshot_id,
            "snapshot_sha": self.snapshot_sha,
            "speed": self.speed,
        }
        self.config_hash = hashlib.sha256(json.dumps(config_dict, sort_keys=True).encode("utf-8")).hexdigest()

    def run(self, resume: bool = False) -> SimulationResult:
        """Run the simulation streaming from the source CSV via SourceParser."""
        checkpoint = self.outbox.initialize_run(
            replay_id=self.replay_id,
            data_scope_id=self.data_scope_id,
            compartment_id=self.compartment_id,
            cycle_id=self.cycle_id,
            source_file=self.relative_path,
            source_file_checksum=self.file_checksum,
            config_hash=self.config_hash,
            resume=resume,
            initial_fault_state=self.faults.snapshot_state(),
        )

        start_line = 2
        sequence = 0
        control_sequence = 0
        expected_unique = 0
        emitted_unique = 0
        emitted_total = 0
        cumulative_hash = INITIAL_CUMULATIVE_HASH
        last_committed_line = 1
        last_source_observed = None
        rejected_source_rows = 0

        if resume and checkpoint is not None:
            start_line = checkpoint.line_number + 1
            sequence = checkpoint.sequence
            control_sequence = checkpoint.control_sequence
            expected_unique = checkpoint.expected_unique_events
            emitted_unique = checkpoint.emitted_unique_events
            emitted_total = checkpoint.emitted_total_messages
            cumulative_hash = checkpoint.cumulative_hash
            last_committed_line = checkpoint.line_number
            last_source_observed = datetime.fromisoformat(checkpoint.last_source_observed) if checkpoint.last_source_observed else None
            rejected_source_rows = checkpoint.rejected_source_rows
            # Restore fault scheduler RNG, delay buffer, and counters
            self.faults.restore_state(checkpoint.fault_state)

        source_desc = Source(
            snapshot_id=self.snapshot_id,
            snapshot_sha=self.snapshot_sha,
            relative_path=self.relative_path,
            file_checksum=self.file_checksum,
            run_id=self.replay_id,
        )
        parser = SourceParser(self.source_path, source_desc)

        events_processed_this_run = 0

        for record in parser.records():
            line_no = record["line_number"]
            if line_no < start_line:
                continue

            # Check global limit: total replayed events across all runs
            if self.limit is not None and sequence >= self.limit:
                break

            # Check if record has unrecoverable time error or off-grid time
            if record["source_timestamp"] is None or record["slot_start"] is None:
                rejected_source_rows += 1
                last_committed_line = line_no
                self.faults._record_audit({
                    "step": self.faults._step,
                    "event_id": record["source_record_id"],
                    "fault_type": "rejected_source",
                    "sequence": sequence,
                    "source_line": line_no,
                    "raw_reference": record.get("raw_reference", f"{self.relative_path}:{line_no}"),
                    "issues": record["issues"],
                })

                # Bounded state: commit checkpoint incrementally if many rejections queue up
                if len(self.faults._pending_audits) >= 50:
                    cumulative_hash, emitted_unique, emitted_total = self.outbox.commit_dispatch_transaction(
                        telemetry_messages=[],
                        control_messages=[],
                        fault_audits=self.faults.pop_pending_audits(),
                        sequence=sequence,
                        line_number=last_committed_line,
                        control_sequence=control_sequence,
                        previous_cumulative_hash=cumulative_hash,
                        expected_unique=expected_unique,
                        emitted_unique=emitted_unique,
                        emitted_total=emitted_total,
                        fault_state=self.faults.snapshot_state(),
                        status="running",
                        last_source_observed=last_source_observed.isoformat() if last_source_observed else None,
                        rejected_source_rows=rejected_source_rows,
                    )
                continue

            sequence += 1
            events_processed_this_run += 1
            expected_unique += 1
            last_committed_line = line_no
            last_source_observed = record["source_timestamp"]

            # Generate deterministic event_id
            event_id = hashlib.sha256(
                f"{self.replay_id}:{sequence}:{record['source_record_id']}".encode("ascii")
            ).hexdigest()

            # Separate categories and map values
            values = record["values"]
            null_reasons = record["null_reasons"]
            issues = record["issues"]
            invalid_numeric_cols = {
                iss["column"] for iss in issues if iss.get("code") == "invalid_numeric" and "column" in iss
            }

            actual_measurements: dict[str, str | None] = {}
            drain_measurements: dict[str, str | None] = {}
            actuators: dict[str, str | None] = {}
            controller_context: dict[str, str | None] = {}
            payload_null_reasons: dict[str, str] = {}
            category_tags: dict[str, str] = {}
            units: dict[str, str] = {}

            for col_name, unit in CLIMATE_UNITS.items():
                units[col_name] = unit
                category = COLUMN_TO_CATEGORY[col_name]
                category_tags[col_name] = category

                val_raw = values.get(col_name)
                val_str = str(val_raw) if val_raw is not None else None
                if val_str is None:
                    if col_name in null_reasons:
                        payload_null_reasons[col_name] = null_reasons[col_name]
                    elif col_name in invalid_numeric_cols:
                        payload_null_reasons[col_name] = "invalid_numeric"
                    else:
                        payload_null_reasons[col_name] = "missing_column"

                if col_name in ACTUAL_CLIMATE_COLUMNS or col_name in COMPUTED_CLIMATE_COLUMNS:
                    actual_measurements[col_name] = val_str
                elif col_name in ACTUAL_DRAIN_COLUMNS or col_name in COMPUTED_DRAIN_COLUMNS:
                    drain_measurements[col_name] = val_str
                elif col_name in ACTUATOR_COLUMNS or col_name in COMPUTED_ACTUATOR_COLUMNS:
                    actuators[col_name] = val_str
                else:
                    controller_context[col_name] = val_str

            published_at = self.clock.now_utc().isoformat()

            source_provenance = {
                "snapshot_id": self.snapshot_id,
                "snapshot_sha": self.snapshot_sha,
                "source_file": self.relative_path,
                "file_checksum": self.file_checksum,
                "line_number": line_no,
                "line_end": record["line_end"],
                "raw_hash": record["raw_hash"],
                "source_time_serial": str(values["time"]),
                "adapter_id": record["adapter_id"],
                "parser_version": record["parser_version"],
                "raw_reference": record.get("raw_reference", f"{self.relative_path}:{line_no}"),
                "time_policy_version": record.get("time_policy_version", "agc-source-naive-grid-v1"),
                "slot_start": record["slot_start"].isoformat() if record.get("slot_start") else None,
                "slot_residual_seconds": str(record["slot_residual_seconds"]) if record.get("slot_residual_seconds") is not None else None,
                "source_issues": record["issues"],
            }

            envelope = {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id,
                "data_scope_id": self.data_scope_id,
                "replay_id": self.replay_id,
                "device_id": self.device_id,
                "compartment_id": self.compartment_id,
                "cycle_id": self.cycle_id,
                "source_record_id": record["source_record_id"],
                "source_provenance": source_provenance,
                "sequence": sequence,
                "source_observed_at": record["source_timestamp"].isoformat(),
                "published_at": published_at,
                "payload": {
                    "actual_measurements": actual_measurements,
                    "drain_measurements": drain_measurements,
                    "actuators": actuators,
                    "controller_context": controller_context,
                    "null_reasons": payload_null_reasons,
                    "category_tags": category_tags,
                },
                "units": units,
                "fault_tags": [],
            }

            validate_telemetry_envelope(envelope)

            # Process through fault scheduler
            emitted_messages = self.faults.process(envelope, self.telemetry_topic)
            audits_for_step = self.faults.pop_pending_audits()

            telemetry_to_commit = []
            if emitted_messages:
                for top, raw_b, eid, orig_s, orig_l in emitted_messages:
                    telemetry_to_commit.append((top, raw_b, eid, orig_s, orig_l))

            controls_to_commit = []

            # Check cursor control message
            if sequence % self.cursor_interval == 0:
                control_sequence += 1
                ctrl_msg = self._build_cursor_envelope(
                    control_sequence=control_sequence,
                    cursor_line=last_committed_line,
                    cursor_seq=sequence,
                    source_time=record["source_timestamp"].isoformat(),
                    status="running",
                )
                validate_control_envelope(ctrl_msg)
                raw_ctrl = json.dumps(ctrl_msg, sort_keys=True, separators=(",", ":")).encode("utf-8")
                controls_to_commit.append((self.control_topic, raw_ctrl, ctrl_msg["control_message_id"], control_sequence, "replay_cursor"))

            # Check pause condition
            is_paused = (self.pause_after is not None and events_processed_this_run >= self.pause_after)
            current_status = "paused" if is_paused else "running"

            if is_paused:
                control_sequence += 1
                pause_msg = {
                    "schema_version": CONTROL_SCHEMA_VERSION,
                    "control_message_id": hashlib.sha256(f"{self.replay_id}:ctrl:{control_sequence}".encode("ascii")).hexdigest(),
                    "replay_id": self.replay_id,
                    "control_sequence": control_sequence,
                    "control_type": "pause_state",
                    "published_at": self.clock.now_utc().isoformat(),
                    "payload": {
                        "paused_at_line": last_committed_line,
                        "paused_at_sequence": sequence,
                        "reason": f"Requested pause after {self.pause_after} events",
                    },
                }
                validate_control_envelope(pause_msg)
                raw_p = json.dumps(pause_msg, sort_keys=True, separators=(",", ":")).encode("utf-8")
                controls_to_commit.append((self.control_topic, raw_p, pause_msg["control_message_id"], control_sequence, "pause_state"))

            # ONE ACID transaction per logical dispatch step
            cumulative_hash, emitted_unique, emitted_total = self.outbox.commit_dispatch_transaction(
                telemetry_messages=telemetry_to_commit,
                control_messages=controls_to_commit,
                fault_audits=audits_for_step,
                sequence=sequence,
                line_number=last_committed_line,
                control_sequence=control_sequence,
                previous_cumulative_hash=cumulative_hash,
                expected_unique=expected_unique,
                emitted_unique=emitted_unique,
                emitted_total=emitted_total,
                fault_state=self.faults.snapshot_state(),
                status=current_status,
                last_source_observed=last_source_observed.isoformat() if last_source_observed else None,
                rejected_source_rows=rejected_source_rows,
            )

            if is_paused:
                self.outbox.export_snapshot_jsonl()
                return SimulationResult(
                    replay_id=self.replay_id,
                    data_scope_id=self.data_scope_id,
                    compartment_id=self.compartment_id,
                    status="paused",
                    total_lines_read=last_committed_line - 1,
                    expected_unique_events=expected_unique,
                    emitted_unique_events=emitted_unique,
                    emitted_total_messages=emitted_total,
                    payload_checksum=cumulative_hash,
                    fault_summary=self.faults.manifest(),
                    last_source_line=last_committed_line,
                    last_sequence=sequence,
                    rejected_source_rows=rejected_source_rows,
                )

            if self.speed > 0:
                self.clock.sleep(300.0 / self.speed)

        # Flush any remaining delayed messages from buffer
        delayed_flushed = self.faults.flush_delayed()
        delayed_telemetry = []
        if delayed_flushed:
            for top, raw_b, eid, orig_s, orig_l in delayed_flushed:
                delayed_telemetry.append((top, raw_b, eid, orig_s, orig_l))

        # Final completion controls
        completion_controls = []
        control_sequence += 1
        final_source_time = last_source_observed.isoformat() if last_source_observed else None
        final_cursor = self._build_cursor_envelope(
            control_sequence=control_sequence,
            cursor_line=last_committed_line,
            cursor_seq=sequence,
            source_time=final_source_time,
            status="completed",
        )
        validate_control_envelope(final_cursor)
        raw_final_c = json.dumps(final_cursor, sort_keys=True, separators=(",", ":")).encode("utf-8")
        completion_controls.append((self.control_topic, raw_final_c, final_cursor["control_message_id"], control_sequence, "replay_cursor"))

        # Pre-calculate what cumulative_hash and distinct event IDs will be after delayed telemetry
        temp_cum_hash = cumulative_hash
        for _, raw_bytes, _, _, _ in delayed_telemetry:
            payload_hash = hashlib.sha256(raw_bytes).hexdigest()
            temp_cum_hash = hashlib.sha256(f"{temp_cum_hash}:{payload_hash}".encode("ascii")).hexdigest()

        # Bounded reconciliation: check only candidate delayed IDs against indexed SQLite outbox
        delayed_eids = list({eid for _, _, eid, _, _ in delayed_telemetry})
        already_in_db = self.outbox.count_existing_event_ids(delayed_eids)
        new_unique_delayed = len(delayed_eids) - already_in_db

        current_unique, current_total = self.outbox.get_telemetry_counts()
        final_unique = current_unique + new_unique_delayed
        final_total = current_total + len(delayed_telemetry)

        control_sequence += 1
        manifest_msg = {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "control_message_id": hashlib.sha256(f"{self.replay_id}:ctrl:{control_sequence}".encode("ascii")).hexdigest(),
            "replay_id": self.replay_id,
            "control_sequence": control_sequence,
            "control_type": "completion_manifest",
            "published_at": self.clock.now_utc().isoformat(),
            "payload": {
                "expected_unique_events": expected_unique,
                "emitted_unique_events": final_unique,
                "emitted_total_messages": final_total,
                "rejected_source_rows": rejected_source_rows,
                "payload_checksum": temp_cum_hash,
                "fault_summary": self.faults.manifest()["fault_counts"],
                "reconciliation_requested": True,
            },
        }
        validate_control_envelope(manifest_msg)
        raw_manifest = json.dumps(manifest_msg, sort_keys=True, separators=(",", ":")).encode("utf-8")
        completion_controls.append((self.control_topic, raw_manifest, manifest_msg["control_message_id"], control_sequence, "completion_manifest"))

        # Commit completion transaction
        cumulative_hash, emitted_unique, emitted_total = self.outbox.commit_dispatch_transaction(
            telemetry_messages=delayed_telemetry,
            control_messages=completion_controls,
            fault_audits=self.faults.pop_pending_audits(),
            sequence=sequence,
            line_number=last_committed_line,
            control_sequence=control_sequence,
            previous_cumulative_hash=cumulative_hash,
            expected_unique=expected_unique,
            emitted_unique=final_unique,
            emitted_total=final_total,
            fault_state=self.faults.snapshot_state(),
            status="completed",
            last_source_observed=last_source_observed.isoformat() if last_source_observed else None,
            rejected_source_rows=rejected_source_rows,
        )

        # Export final JSONL files
        self.outbox.export_snapshot_jsonl()

        return SimulationResult(
            replay_id=self.replay_id,
            data_scope_id=self.data_scope_id,
            compartment_id=self.compartment_id,
            status="completed",
            total_lines_read=last_committed_line - 1,
            expected_unique_events=expected_unique,
            emitted_unique_events=emitted_unique,
            emitted_total_messages=emitted_total,
            payload_checksum=cumulative_hash,
            fault_summary=self.faults.manifest(),
            last_source_line=last_committed_line,
            last_sequence=sequence,
            rejected_source_rows=rejected_source_rows,
        )

    def _build_cursor_envelope(
        self,
        control_sequence: int,
        cursor_line: int,
        cursor_seq: int,
        source_time: str | None,
        status: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "control_message_id": hashlib.sha256(f"{self.replay_id}:ctrl:{control_sequence}".encode("ascii")).hexdigest(),
            "replay_id": self.replay_id,
            "control_sequence": control_sequence,
            "control_type": "replay_cursor",
            "published_at": self.clock.now_utc().isoformat(),
            "payload": {
                "logical_cursor_line": cursor_line,
                "logical_cursor_sequence": cursor_seq,
                "source_observed_at": source_time,
                "status": status,
            },
        }
