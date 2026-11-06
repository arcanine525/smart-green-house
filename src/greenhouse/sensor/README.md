# Greenhouse Sensor Simulator and Telemetry Ingestion (T32)

Deterministic sensor replay simulator and versioned envelope specification for Greenhouse Climate telemetry.

## Architecture and Scope

The sensor simulator replays historical `GreenhouseClimate.csv` datasets (or synthetic fixtures) into versioned telemetry and control envelopes designed for downstream MQTT ingestion (T33) and Structured Streaming (T34).

Key architectural boundaries:
- **Explicit Data Scope & Isolation**: Each replay assigns an isolated `data_scope_id` and `replay_id` (`{snapshot_id}:sim-{replay_id}`). It never modifies historical database publications or baseline snapshots.
- **Simulated Device Identity**: Devices are explicitly marked as simulated (e.g. `sim-climate-AICU`), never confused with real physical hardware.
- **Strict Separation of Categories (Authoritative from ReadMe.pdf)**:
  - Actual indoor climate (`Tair`, `Rhair`, `CO2air`, `HumDef`, `PipeGrow`, `PipeLow`)
  - Computed climate (`Tot_PAR` [µmol/m² s], `Tot_PAR_Lamps` [µmol/m² s])
  - Actuators (`AssimLight` [%], `EnScr` [%], `BlackScr` [%], `VentLee` [%], `Ventwind` [%])
  - Computed actuator (`co2_dos` [kg/ha hour] calibrated dosing)
  - Drain / irrigation (`EC_drain_PC` [dS/m], `pH_drain_PC` [1], `water_sup` [minutes], `Cum_irr` [L/m²])
  - Controller requested setpoints (`t_heat_sp`, `co2_sp`, `dx_sp` [g/m³], `water_sup_intervals_sp_min` [minutes], etc.)
  - Controller realized setpoints / VIP (`t_heat_vip`, `co2_vip`, `dx_vip` [g/m³], `water_sup_intervals_vip_min` [minutes], etc.)
- **Historical SourceParser Reuse**: Reuses `SourceParser` directly to guarantee identical physical line tracking, raw text, raw hashes, and source record IDs without parser drift. Missing values strictly retain their explicit lexical null reasons (`empty`, `nan_token`, `na_token`, `n_a_token`); no synthetic imputation or NaN tokens in JSON.

## Wire Schemas

Machine-readable JSON Schemas:
- Telemetry: `src/greenhouse/sensor/telemetry-envelope.schema.json`
- Control: `src/greenhouse/sensor/control-envelope.schema.json`

### 1. Telemetry Envelope (`greenhouse-telemetry-envelope-v1`)

```json
{
  "schema_version": "greenhouse-telemetry-envelope-v1",
  "event_id": "8f3b...",
  "data_scope_id": "agc-2nd-2019-...:sim-replay-001",
  "replay_id": "replay-001",
  "device_id": "sim-climate-AICU",
  "compartment_id": "AICU",
  "cycle_id": "agc-cycle-AICU",
  "source_record_id": "3a7c...",
  "source_provenance": {
    "snapshot_id": "agc-2nd-2019-c19d385e...",
    "snapshot_sha": "c19d385e...",
    "source_file": "AICU/GreenhouseClimate.csv",
    "file_checksum": "a4d2...",
    "line_number": 2,
    "line_end": 2,
    "raw_hash": "b2f1...",
    "source_time_serial": "43815.0034722222",
    "adapter_id": "agc-standard",
    "parser_version": "agc-parser-v1",
    "source_issues": []
  },
  "sequence": 1,
  "source_observed_at": "2019-12-16T00:05:00",
  "published_at": "2026-11-06T02:50:00+00:00",
  "payload": {
    "actual_measurements": {
      "Tair": "21.6",
      "Rhair": "78.2",
      "CO2air": "650",
      "Tot_PAR": "45.0"
    },
    "drain_measurements": {
      "EC_drain_PC": "3.2",
      "pH_drain_PC": "6.1",
      "water_sup": "0.0",
      "Cum_irr": "0.0"
    },
    "actuators": {
      "AssimLight": "100",
      "EnScr": "0",
      "BlackScr": "0",
      "VentLee": "15",
      "Ventwind": "10",
      "co2_dos": "12.5"
    },
    "controller_context": {
      "t_heat_sp": "18.0",
      "t_heat_vip": "18.5",
      "dx_sp": "4.5"
    },
    "null_reasons": {},
    "category_tags": {
      "Tair": "actual_climate",
      "Tot_PAR": "computed_climate",
      "EC_drain_PC": "actual_drain",
      "AssimLight": "actuator",
      "co2_dos": "computed_actuator",
      "t_heat_sp": "controller_requested"
    }
  },
  "units": {
    "Tair": "°C",
    "Tot_PAR": "µmol/m² s",
    "EC_drain_PC": "dS/m",
    "pH_drain_PC": "1",
    "water_sup": "minutes",
    "co2_dos": "kg/ha hour",
    "dx_sp": "g/m³"
  },
  "fault_tags": []
}
```

### 2. Control Messages (`greenhouse-control-envelope-v1`)

Published on a distinct topic (`v1/control/{replay_id}`):
- `replay_cursor`: tracks monotone logical progress (`logical_cursor_line`, `logical_cursor_sequence`). The cursor is strictly monotonic and never inferred from out-of-order event timestamps.
- `pause_state`: captures state at clean pause boundaries.
- `completion_manifest`: requests end-to-end reconciliation:
  - `expected_unique_events`
  - `emitted_unique_events`
  - `emitted_total_messages`
  - `payload_checksum`: deterministic Merkle hash chain over emitted messages
  - `fault_summary`: tally of duplicates, drops, delays, conflicts injected
  - `reconciliation_requested`: `true`

## Deterministic Fault Injection

The `FaultScheduler` provides reproducible fault testing:
- **Duplicate**: emits the same event twice with identical event ID and exact wire bytes.
- **Conflict**: emits the original event PLUS a divergent variant with the same `event_id`, tagged with `"injected_conflict"` in `fault_tags` and logged in the fault audit trail.
- **Delay / Reorder**: holds messages in a bounded buffer and emits them later, testing out-of-order transport without mutating event contents.
- **Drop**: skips message transmission and logs the intentional gap; `expected_unique_events` increments but `emitted_unique_events` does not.

## Transactional Persistence & Crash Recovery

- **SQLite Authoritative Transaction Store (`outbox.db`)**:
  - The SQLite database is the authoritative committed transport and checkpoint store.
  - WAL journal mode with `PRAGMA synchronous = FULL` ensures power-loss durable commits backed by fsync.
  - Tail reconciliation: upon crash recovery, automatically rolls back any uncommitted messages past the last valid checkpoint.
  - Exact retry lookup: `get_event_for_retry(event_id)` retrieves the exact wire bytes and original `published_at` timestamp without regeneration.
  - Replay isolation: strictly prevents accidental outbox reuse under a new non-resume run; requires explicit `--resume` and verifies configuration and scope hashes.
- **JSONL Export Snapshots**:
  - `telemetry_outbox.jsonl` and `control_outbox.jsonl` are atomic snapshot exports generated from `outbox.db` at pause and completion boundaries via streaming `fetchmany(500)` temporary-file replacement.
  - After any process interruption or crash, the JSONL snapshots can be regenerated directly from the authoritative SQLite database.
- **Durable Source Rejection Auditing & Reconciliation**:
  - Source rows with off-grid timestamps (residual > 0.5s) or unrecoverable time errors are rejected and recorded in SQLite `fault_audit` with their parser issues, line numbers, and raw provenance references.
  - Cumulative `rejected_source_rows` are recorded in each SQLite `checkpoints` row and accurately restored upon `--resume`.
  - At every boundary (pause, resume, completion, crash rollback), the cumulative `rejected_source_rows` count strictly reconciles across `SimulationResult`, `completion_manifest`, and SQLite `fault_audit`.
  - Rejections are processed incrementally with bounded pending state (chunked checkpoints every 50 records) to preserve the 128 MiB cgroup cap.
  - Defined handling for all-rejected, trailing-rejected, and empty source files preserves monotonic cursor progress and emits truthful control envelopes without invalid timestamps or crashes.
  - Backward compatibility: pre-fix `outbox.db` instances missing `rejected_source_rows` column are automatically migrated via schema alteration and reconstructed from persisted `fault_audit` records.

- **Bounded Memory**:
  - Input CSV files are stream-hashed in 64 KiB chunks rather than loaded into memory.
  - Records are streamed row-by-row via `SourceParser` and inserted into SQLite.
  - Operates comfortably within the 128 MiB container memory cgroup cap (`--memory=128m --cpus=0.2`).
- **Transport Scope & T33 Deferral**:
  - T32 implements local outbox transactional persistence, envelope formatting, and fault injection.
  - Full broker transport policies, persistent disk queues, and MQTT network backpressure are deferred to T33.

## CLI Usage

```bash
# Basic simulation of AICU climate data
python -m greenhouse simulate --compartment AICU --limit 50 --outbox-dir /data/outbox

# Deterministic fault injection with seed
python -m greenhouse simulate --compartment AICU --seed 42 --duplicate-rate 0.05 --conflict-rate 0.02 --outbox-dir tmp/outbox

# Pause after 20 events, then resume
python -m greenhouse simulate --compartment AICU --pause-after 20 --replay-id replay-demo --outbox-dir tmp/outbox
python -m greenhouse simulate --resume --outbox-dir tmp/outbox

# Verify retry lookup stability
python -m greenhouse simulate --outbox-dir tmp/outbox --retry-event <EVENT_ID>
```
