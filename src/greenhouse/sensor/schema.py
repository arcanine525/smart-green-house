"""Telemetry and control envelope specifications and validation for sensor replay.

Units, categories, and field types are strictly aligned with the canonical
Autonomous Greenhouse Challenge dataset documentation (ReadMe.pdf pp.3,5,7,12).
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "greenhouse-telemetry-envelope-v1"
CONTROL_SCHEMA_VERSION = "greenhouse-control-envelope-v1"

# Authoritative units from AGC 2nd edition ReadMe.pdf pp.3,5,7
CLIMATE_UNITS = {
    # Actual indoor climate
    "Tair": "°C",
    "Rhair": "%",
    "CO2air": "ppm",
    "HumDef": "g/m³",
    "PipeGrow": "°C",
    "PipeLow": "°C",
    # Computed climate (PAR)
    "Tot_PAR": "µmol/m² s",
    "Tot_PAR_Lamps": "µmol/m² s",
    # Actuators
    "AssimLight": "%",
    "EnScr": "%",
    "BlackScr": "%",
    "VentLee": "%",
    "Ventwind": "%",
    # Computed actuator / calibrated dosing
    "co2_dos": "kg/ha hour",
    # Drain / irrigation
    "EC_drain_PC": "dS/m",
    "pH_drain_PC": "1",  # Dimensionless [-]
    "water_sup": "minutes",  # Cumulative minutes of irrigation in a day
    "Cum_irr": "L/m²",  # Cumulative litres of irrigation in a day
    # Controller requested setpoints
    "assim_sp": "%",
    "co2_sp": "ppm",
    "dx_sp": "g/m³",
    "int_blue_sp": "1",  # [0 to 1000] range of LED proportional control
    "int_farred_sp": "1",
    "int_red_sp": "1",
    "int_white_sp": "1",
    "scr_blck_sp": "%",
    "scr_enrg_sp": "%",
    "t_grow_min_sp": "°C",
    "t_heat_sp": "°C",
    "t_rail_min_sp": "°C",
    "t_vent_sp": "°C",
    "water_sup_intervals_sp_min": "minutes",
    "window_pos_lee_sp": "%",
    # Controller realized setpoints (VIP)
    "assim_vip": "%",
    "co2_vip": "ppm",
    "dx_vip": "g/m³",
    "int_blue_vip": "1",
    "int_farred_vip": "1",
    "int_red_vip": "1",
    "int_white_vip": "1",
    "scr_blck_vip": "%",
    "scr_enrg_vip": "%",
    "t_grow_min_vip": "°C",
    "t_heat_vip": "°C",
    "t_rail_min_vip": "°C",
    "t_ventlee_vip": "°C",
    "t_ventwind_vip": "°C",
    "water_sup_intervals_vip_min": "minutes",
    "window_pos_lee_vip": "%",
}

ACTUAL_CLIMATE_COLUMNS = {"Tair", "Rhair", "CO2air", "HumDef", "PipeGrow", "PipeLow"}
COMPUTED_CLIMATE_COLUMNS = {"Tot_PAR", "Tot_PAR_Lamps"}
ACTUATOR_COLUMNS = {"AssimLight", "EnScr", "BlackScr", "VentLee", "Ventwind"}
COMPUTED_ACTUATOR_COLUMNS = {"co2_dos"}
ACTUAL_DRAIN_COLUMNS = {"EC_drain_PC", "pH_drain_PC", "water_sup"}
COMPUTED_DRAIN_COLUMNS = {"Cum_irr"}
DRAIN_COLUMNS = ACTUAL_DRAIN_COLUMNS | COMPUTED_DRAIN_COLUMNS

CONTROLLER_REQUESTED_COLUMNS = {
    "assim_sp", "co2_sp", "dx_sp", "int_blue_sp", "int_farred_sp",
    "int_red_sp", "int_white_sp", "scr_blck_sp", "scr_enrg_sp", "t_grow_min_sp",
    "t_heat_sp", "t_rail_min_sp", "t_vent_sp", "water_sup_intervals_sp_min",
    "window_pos_lee_sp"
}

CONTROLLER_REALIZED_COLUMNS = {
    "assim_vip", "co2_vip", "dx_vip", "int_blue_vip", "int_farred_vip",
    "int_red_vip", "int_white_vip", "scr_blck_vip", "scr_enrg_vip", "t_grow_min_vip",
    "t_heat_vip", "t_rail_min_vip", "t_ventlee_vip", "t_ventwind_vip",
    "water_sup_intervals_vip_min", "window_pos_lee_vip"
}

ALL_COLUMNS = (
    ACTUAL_CLIMATE_COLUMNS
    | COMPUTED_CLIMATE_COLUMNS
    | ACTUATOR_COLUMNS
    | COMPUTED_ACTUATOR_COLUMNS
    | ACTUAL_DRAIN_COLUMNS
    | COMPUTED_DRAIN_COLUMNS
    | CONTROLLER_REQUESTED_COLUMNS
    | CONTROLLER_REALIZED_COLUMNS
)

COLUMN_TO_CATEGORY = {}
for c in ACTUAL_CLIMATE_COLUMNS: COLUMN_TO_CATEGORY[c] = "actual_climate"
for c in COMPUTED_CLIMATE_COLUMNS: COLUMN_TO_CATEGORY[c] = "computed_climate"
for c in ACTUATOR_COLUMNS: COLUMN_TO_CATEGORY[c] = "actuator"
for c in COMPUTED_ACTUATOR_COLUMNS: COLUMN_TO_CATEGORY[c] = "computed_actuator"
for c in ACTUAL_DRAIN_COLUMNS: COLUMN_TO_CATEGORY[c] = "actual_drain"
for c in COMPUTED_DRAIN_COLUMNS: COLUMN_TO_CATEGORY[c] = "computed_drain"
for c in CONTROLLER_REQUESTED_COLUMNS: COLUMN_TO_CATEGORY[c] = "controller_requested"
for c in CONTROLLER_REALIZED_COLUMNS: COLUMN_TO_CATEGORY[c] = "controller_realized"

REQUIRED_TELEMETRY_KEYS = {
    "schema_version",
    "event_id",
    "data_scope_id",
    "replay_id",
    "device_id",
    "compartment_id",
    "cycle_id",
    "source_record_id",
    "source_provenance",
    "sequence",
    "source_observed_at",
    "published_at",
    "payload",
    "units",
    "fault_tags",
}

REQUIRED_PROVENANCE_KEYS = {
    "snapshot_id",
    "snapshot_sha",
    "source_file",
    "file_checksum",
    "line_number",
    "line_end",
    "raw_hash",
    "source_time_serial",
    "adapter_id",
    "parser_version",
}

REQUIRED_PAYLOAD_KEYS = {
    "actual_measurements",
    "drain_measurements",
    "actuators",
    "controller_context",
    "null_reasons",
    "category_tags",
}

CONTROL_TYPES = {"replay_cursor", "pause_state", "completion_manifest"}


class ValidationError(ValueError):
    """Raised when an envelope fails strict schema validation."""


def _check_string(val: Any, field_name: str) -> None:
    if not isinstance(val, str) or not val.strip():
        raise ValidationError(f"Field '{field_name}' must be a non-empty string, got {val!r}")


def _check_decimal_string(val: Any, field_name: str) -> None:
    if val is None:
        return
    if not isinstance(val, str):
        raise ValidationError(f"Field '{field_name}' numeric value must be exact string or None, got {val!r}")
    try:
        parsed = Decimal(val)
        if not parsed.is_finite():
            raise ValidationError(f"Field '{field_name}' is not finite: {val}")
    except InvalidOperation as error:
        raise ValidationError(f"Field '{field_name}' contains invalid decimal string: {val}") from error


def load_telemetry_json_schema() -> dict[str, Any]:
    """Load the machine-readable JSON Schema for telemetry envelopes."""
    schema_path = Path(__file__).parent / "telemetry-envelope.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def load_control_json_schema() -> dict[str, Any]:
    """Load the machine-readable JSON Schema for control envelopes."""
    schema_path = Path(__file__).parent / "control-envelope.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_telemetry_envelope(envelope: dict[str, Any]) -> None:
    """Strictly validate a telemetry envelope dict against schema v1."""
    if not isinstance(envelope, dict):
        raise ValidationError(f"Envelope must be a dict, got {type(envelope).__name__}")

    extra_keys = set(envelope.keys()) - REQUIRED_TELEMETRY_KEYS
    missing_keys = REQUIRED_TELEMETRY_KEYS - set(envelope.keys())
    if extra_keys:
        raise ValidationError(f"Unexpected extra keys in telemetry envelope: {sorted(extra_keys)}")
    if missing_keys:
        raise ValidationError(f"Missing required keys in telemetry envelope: {sorted(missing_keys)}")

    if envelope["schema_version"] != SCHEMA_VERSION:
        raise ValidationError(f"Unsupported schema_version: {envelope['schema_version']!r}, expected {SCHEMA_VERSION!r}")

    for key in ("event_id", "data_scope_id", "replay_id", "device_id", "compartment_id", "cycle_id", "source_record_id"):
        _check_string(envelope[key], key)

    if len(envelope["event_id"]) != 64 or any(c not in "0123456789abcdef" for c in envelope["event_id"]):
        raise ValidationError(f"event_id must be 64-character lowercase hex SHA-256, got {envelope['event_id']}")
    if len(envelope["source_record_id"]) != 64 or any(c not in "0123456789abcdef" for c in envelope["source_record_id"]):
        raise ValidationError("source_record_id must be 64-character lowercase hex SHA-256")

    # Sequence
    seq = envelope["sequence"]
    if type(seq) is not int or seq < 1:
        raise ValidationError(f"Sequence must be a positive integer, got {seq!r}")

    # Source observed at (naive ISO)
    _check_string(envelope["source_observed_at"], "source_observed_at")
    try:
        dt_naive = datetime.fromisoformat(envelope["source_observed_at"])
    except ValueError as err:
        raise ValidationError(f"Invalid ISO source_observed_at: {envelope['source_observed_at']}") from err
    if dt_naive.tzinfo is not None:
        raise ValidationError("source_observed_at must be naive ISO timestamp (no timezone)")

    # Published at (aware ISO)
    _check_string(envelope["published_at"], "published_at")
    try:
        dt_aware = datetime.fromisoformat(envelope["published_at"])
    except ValueError as err:
        raise ValidationError(f"Invalid ISO published_at: {envelope['published_at']}") from err
    if dt_aware.tzinfo is None:
        raise ValidationError("published_at must be timezone-aware ISO timestamp")

    # Source provenance
    prov = envelope["source_provenance"]
    if not isinstance(prov, dict):
        raise ValidationError("source_provenance must be a dict")
    prov_keys = set(prov.keys())
    optional_prov_keys = {
        "source_issues", "raw_reference", "time_policy_version", "slot_start", "slot_residual_seconds"
    }
    prov_required_missing = REQUIRED_PROVENANCE_KEYS - prov_keys
    prov_unknown = prov_keys - REQUIRED_PROVENANCE_KEYS - optional_prov_keys
    if prov_unknown:
        raise ValidationError(f"Unexpected keys in source_provenance: {sorted(prov_unknown)}")
    if prov_required_missing:
        raise ValidationError(f"Missing keys in source_provenance: {sorted(prov_required_missing)}")

    for pkey in ("snapshot_id", "snapshot_sha", "source_file", "file_checksum", "adapter_id", "parser_version"):
        _check_string(prov[pkey], f"source_provenance.{pkey}")
    if len(prov["file_checksum"]) != 64 or not all(c in "0123456789abcdef" for c in prov["file_checksum"]):
        raise ValidationError("source_provenance.file_checksum must be 64-char lowercase hex SHA-256")
    if len(prov["snapshot_sha"]) != 64 or not all(c in "0123456789abcdef" for c in prov["snapshot_sha"]):
        raise ValidationError("source_provenance.snapshot_sha must be 64-char lowercase hex SHA-256")
    if len(prov["raw_hash"]) != 64 or not all(c in "0123456789abcdef" for c in prov["raw_hash"]):
        raise ValidationError("source_provenance.raw_hash must be 64-char lowercase hex SHA-256")

    line_no = prov["line_number"]
    line_end = prov["line_end"]
    if type(line_no) is not int or line_no < 2:
        raise ValidationError(f"line_number must be int >= 2, got {line_no}")
    if type(line_end) is not int or line_end < line_no:
        raise ValidationError(f"line_end must be int >= line_number, got {line_end}")
    _check_decimal_string(prov["source_time_serial"], "source_provenance.source_time_serial")

    if "source_issues" in prov and not isinstance(prov["source_issues"], list):
        raise ValidationError("source_issues must be a list of issue dicts")
    if "raw_reference" in prov and prov["raw_reference"] is not None:
        _check_string(prov["raw_reference"], "source_provenance.raw_reference")
    if "time_policy_version" in prov and prov["time_policy_version"] is not None:
        _check_string(prov["time_policy_version"], "source_provenance.time_policy_version")
    if "slot_start" in prov and prov["slot_start"] is not None:
        _check_string(prov["slot_start"], "source_provenance.slot_start")
    if "slot_residual_seconds" in prov and prov["slot_residual_seconds"] is not None:
        _check_decimal_string(prov["slot_residual_seconds"], "source_provenance.slot_residual_seconds")

    # Payload
    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise ValidationError("payload must be a dict")
    p_extra = set(payload.keys()) - REQUIRED_PAYLOAD_KEYS
    p_missing = REQUIRED_PAYLOAD_KEYS - set(payload.keys())
    if p_extra:
        raise ValidationError(f"Unexpected keys in payload: {sorted(p_extra)}")
    if p_missing:
        raise ValidationError(f"Missing keys in payload: {sorted(p_missing)}")

    actuals = payload["actual_measurements"]
    drains = payload["drain_measurements"]
    actuators = payload["actuators"]
    context = payload["controller_context"]
    nulls = payload["null_reasons"]
    category_tags = payload["category_tags"]

    if not all(isinstance(d, dict) for d in (actuals, drains, actuators, context, nulls, category_tags)):
        raise ValidationError("All payload sections must be dicts")

    # Units validation: check all populated columns have matching units
    units = envelope["units"]
    if not isinstance(units, dict):
        raise ValidationError("units must be a dict")
    for k, u in units.items():
        if k not in CLIMATE_UNITS:
            raise ValidationError(f"Unknown column in units: {k}")
        if u != CLIMATE_UNITS[k]:
            raise ValidationError(f"Missing or mismatched unit for {k}: got {u!r}, expected {CLIMATE_UNITS[k]!r}")

    populated_count = 0
    allowed_categories = {
        "actual_climate", "computed_climate", "actual_drain", "computed_drain",
        "actuator", "computed_actuator", "controller_requested", "controller_realized"
    }
    allowed_null_reasons = {
        "empty", "nan_token", "na_token", "n_a_token", "invalid_numeric", "missing_column", "nonfinite"
    }

    for k, reason in nulls.items():
        if k not in CLIMATE_UNITS:
            raise ValidationError(f"Unknown column in null_reasons: {k}")
        if reason not in allowed_null_reasons:
            raise ValidationError(f"Invalid null_reason {reason!r} for column {k}")

    # Validate actual measurements
    for k, v in actuals.items():
        if k not in ACTUAL_CLIMATE_COLUMNS and k not in COMPUTED_CLIMATE_COLUMNS:
            raise ValidationError(f"Invalid column in actual_measurements: {k}")
        _check_decimal_string(v, f"actual_measurements.{k}")
        if v is None and k not in nulls:
            raise ValidationError(f"Missing actual measurement {k} requires null_reasons entry")
        if v is not None and k in nulls:
            raise ValidationError(f"Present actual measurement {k} cannot have null_reasons entry")
        if v is not None:
            populated_count += 1
            if k not in units or units[k] != CLIMATE_UNITS[k]:
                raise ValidationError(f"Missing or mismatched unit for {k}: got {units.get(k)!r}, expected {CLIMATE_UNITS[k]!r}")

    # Validate drain measurements
    for k, v in drains.items():
        if k not in ACTUAL_DRAIN_COLUMNS and k not in COMPUTED_DRAIN_COLUMNS:
            raise ValidationError(f"Invalid column in drain_measurements: {k}")
        _check_decimal_string(v, f"drain_measurements.{k}")
        if v is None and k not in nulls:
            raise ValidationError(f"Missing drain measurement {k} requires null_reasons entry")
        if v is not None and k in nulls:
            raise ValidationError(f"Present drain measurement {k} cannot have null_reasons entry")
        if v is not None:
            populated_count += 1
            if k not in units or units[k] != CLIMATE_UNITS[k]:
                raise ValidationError(f"Missing or mismatched unit for {k}: got {units.get(k)!r}, expected {CLIMATE_UNITS[k]!r}")

    # Validate actuators
    for k, v in actuators.items():
        if k not in ACTUATOR_COLUMNS and k not in COMPUTED_ACTUATOR_COLUMNS:
            raise ValidationError(f"Invalid column in actuators: {k}")
        _check_decimal_string(v, f"actuators.{k}")
        if v is None and k not in nulls:
            raise ValidationError(f"Missing actuator {k} requires null_reasons entry")
        if v is not None and k in nulls:
            raise ValidationError(f"Present actuator {k} cannot have null_reasons entry")
        if v is not None:
            populated_count += 1
            if k not in units or units[k] != CLIMATE_UNITS[k]:
                raise ValidationError(f"Missing or mismatched unit for {k}: got {units.get(k)!r}, expected {CLIMATE_UNITS[k]!r}")

    # Validate controller context
    for k, v in context.items():
        if k not in CONTROLLER_REQUESTED_COLUMNS and k not in CONTROLLER_REALIZED_COLUMNS:
            raise ValidationError(f"Invalid column in controller_context: {k}")
        _check_decimal_string(v, f"controller_context.{k}")
        if v is None and k not in nulls:
            raise ValidationError(f"Missing controller context {k} requires null_reasons entry")
        if v is not None and k in nulls:
            raise ValidationError(f"Present controller context {k} cannot have null_reasons entry")
        if v is not None:
            populated_count += 1
            if k not in units or units[k] != CLIMATE_UNITS[k]:
                raise ValidationError(f"Missing or mismatched unit for {k}: got {units.get(k)!r}, expected {CLIMATE_UNITS[k]!r}")

    if populated_count > 0 and not units:
        raise ValidationError("Populated measurements cannot have empty units dictionary")

    # Category tags validation
    for k, tag in category_tags.items():
        if tag not in allowed_categories:
            raise ValidationError(f"Invalid category tag {tag!r} for measure {k}")
        if k in COLUMN_TO_CATEGORY and tag != COLUMN_TO_CATEGORY[k]:
            raise ValidationError(f"Category tag mismatch for {k}: got {tag!r}, expected {COLUMN_TO_CATEGORY[k]!r}")

    # Fault tags
    fault_tags = envelope["fault_tags"]
    if not isinstance(fault_tags, list) or not all(isinstance(t, str) for t in fault_tags):
        raise ValidationError("fault_tags must be a list of strings")


def validate_control_envelope(envelope: dict[str, Any]) -> None:
    """Strictly validate a control message envelope against schema v1."""
    if not isinstance(envelope, dict):
        raise ValidationError("Control envelope must be a dict")

    required_control_keys = {
        "schema_version", "control_message_id", "replay_id",
        "control_sequence", "control_type", "published_at", "payload"
    }
    extra_keys = set(envelope.keys()) - required_control_keys
    if extra_keys:
        raise ValidationError(f"Unexpected extra keys in control envelope: {sorted(extra_keys)}")
    missing_keys = required_control_keys - set(envelope.keys())
    if missing_keys:
        raise ValidationError(f"Missing keys in control envelope: {sorted(missing_keys)}")

    if envelope["schema_version"] != CONTROL_SCHEMA_VERSION:
        raise ValidationError(f"Unsupported control schema_version: {envelope['schema_version']}")

    for key in ("control_message_id", "replay_id"):
        _check_string(envelope[key], key)
    if len(envelope["control_message_id"]) != 64 or any(c not in "0123456789abcdef" for c in envelope["control_message_id"]):
        raise ValidationError("control_message_id must be 64-char lowercase hex SHA-256")

    cseq = envelope["control_sequence"]
    if type(cseq) is not int or cseq < 1:
        raise ValidationError(f"control_sequence must be positive int, got {cseq}")

    ctype = envelope["control_type"]
    if ctype not in CONTROL_TYPES:
        raise ValidationError(f"Unknown control_type: {ctype}, expected one of {CONTROL_TYPES}")

    _check_string(envelope["published_at"], "published_at")
    try:
        dt = datetime.fromisoformat(envelope["published_at"])
    except ValueError as err:
        raise ValidationError(f"Invalid ISO published_at in control message: {err}") from err
    if dt.tzinfo is None:
        raise ValidationError("published_at in control message must be offset-aware")

    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise ValidationError("Control payload must be a dict")

    if ctype == "replay_cursor":
        required_cursor = {"logical_cursor_line", "logical_cursor_sequence", "source_observed_at", "status"}
        if set(payload.keys()) != required_cursor:
            raise ValidationError(f"replay_cursor payload keys must be exactly {required_cursor}")
        if payload["status"] not in ("running", "paused", "completed"):
            raise ValidationError(f"Invalid status in replay_cursor: {payload['status']}")
        if type(payload["logical_cursor_line"]) is not int or payload["logical_cursor_line"] < 1:
            raise ValidationError("logical_cursor_line must be int >= 1")
        if type(payload["logical_cursor_sequence"]) is not int or payload["logical_cursor_sequence"] < 0:
            raise ValidationError("logical_cursor_sequence must be int >= 0")
        if payload["source_observed_at"] is not None and not isinstance(payload["source_observed_at"], str):
            raise ValidationError("source_observed_at in replay_cursor must be ISO string or None")
        if payload["source_observed_at"] is not None:
            try:
                source_time = datetime.fromisoformat(payload["source_observed_at"])
            except ValueError as err:
                raise ValidationError("source_observed_at in replay_cursor must be an ISO timestamp") from err
            if source_time.tzinfo is not None:
                raise ValidationError("source_observed_at in replay_cursor must be source-naive")

    elif ctype == "completion_manifest":
        required_manifest = {
            "expected_unique_events", "emitted_unique_events", "emitted_total_messages",
            "payload_checksum", "fault_summary", "reconciliation_requested"
        }
        allowed_manifest = required_manifest | {"rejected_source_rows"}
        if not required_manifest.issubset(set(payload.keys())) or (set(payload.keys()) - allowed_manifest):
            raise ValidationError(f"completion_manifest payload keys must be {sorted(required_manifest)}: got {sorted(payload.keys())}")
        for count_k in ("expected_unique_events", "emitted_unique_events", "emitted_total_messages"):
            val = payload[count_k]
            if type(val) is not int or val < 0:
                raise ValidationError(f"{count_k} must be nonnegative int")
        if "rejected_source_rows" in payload:
            val = payload["rejected_source_rows"]
            if type(val) is not int or val < 0:
                raise ValidationError("rejected_source_rows must be nonnegative int")
        if len(payload["payload_checksum"]) != 64 or any(c not in "0123456789abcdef" for c in payload["payload_checksum"]):
            raise ValidationError("payload_checksum must be 64-char lowercase hex SHA-256")
        if not isinstance(payload["fault_summary"], dict):
            raise ValidationError("fault_summary must be a dict")
        required_faults = {"duplicates", "conflicts", "delays", "drops"}
        if not required_faults.issubset(set(payload["fault_summary"].keys())):
            raise ValidationError(f"fault_summary missing required keys: {sorted(required_faults - set(payload['fault_summary'].keys()))}")
        for fk, fv in payload["fault_summary"].items():
            if type(fv) is not int or fv < 0:
                raise ValidationError(f"fault_summary[{fk}] must be nonnegative int")
        if payload["reconciliation_requested"] is not True:
            raise ValidationError("reconciliation_requested must be True")

    elif ctype == "pause_state":
        required_pause = {"paused_at_line", "paused_at_sequence", "reason"}
        if set(payload.keys()) != required_pause:
            raise ValidationError(f"pause_state payload keys must be exactly {required_pause}")
        if type(payload["paused_at_line"]) is not int or payload["paused_at_line"] < 1:
            raise ValidationError("paused_at_line must be int >= 1")
        if type(payload["paused_at_sequence"]) is not int or payload["paused_at_sequence"] < 0:
            raise ValidationError("paused_at_sequence must be int >= 0")
        _check_string(payload["reason"], "payload.reason")
