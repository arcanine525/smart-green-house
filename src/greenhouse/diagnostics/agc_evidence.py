"""Reproduce aggregate observations for data-decisions.md from the locked AGC.

This diagnostic only prints aggregates and source line references. Rounding here
measures a candidate grid; it does not normalize, accept or quarantine records.
"""

import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from statistics import median

from greenhouse.ingestion.inventory import load_verified_manifest
from greenhouse.ingestion.snapshot import AGC

START, END = Decimal(43815), Decimal(43981)
GRID = Decimal(288)
MISSING = {"", "nan", "na", "n/a"}
SETPOINTS = ("co2_sp", "co2_vip", "t_heat_sp", "t_heat_vip", "t_vent_sp", "t_ventlee_vip")


def display(serial):
    return (datetime(1899, 12, 30) + timedelta(days=float(serial))).isoformat(sep=" ")


def number(token):
    value = Decimal(token)
    if not value.is_finite():
        raise InvalidOperation
    return value


def observe(snapshot, entry):
    times, lines, values = [], [], {name: [] for name in SETPOINTS}
    missing = dict.fromkeys(SETPOINTS, 0)
    symbols = {name: {} for name in SETPOINTS}
    with (snapshot / entry["path"]).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, strict=True)
        headers = [name.strip() for name in next(reader)]
        for row in reader:
            times.append(number(row[0]))
            lines.append(reader.line_num)
            for name in SETPOINTS:
                if name not in headers:
                    continue
                token = row[headers.index(name)].strip()
                if token.lower() in MISSING:
                    missing[name] += 1
                    continue
                try:
                    values[name].append(number(token))
                except InvalidOperation:
                    symbols[name][token] = symbols[name].get(token, 0) + 1
    low, high = min(times), max(times)
    outside = [(time, line) for time, line in zip(times, lines) if not START <= time < END]
    result = {
        "path": entry["path"], "rows": len(times),
        "min_date": display(low), "max_date": display(high),
        "min_line": lines[times.index(low)], "max_line": lines[times.index(high)],
        "outside_cycle_rows": len(outside),
        "outside_cycle_lines": [line for _, line in outside],
        "outside_cycle_date_range": [display(min(t for t, _ in outside)), display(max(t for t, _ in outside))] if outside else None,
    }
    if Path(entry["path"]).stem in {"GreenhouseClimate", "GrodanSens", "Weather"}:
        slots = [(time * GRID).to_integral_value() for time in times]
        residuals = [abs(time * GRID - slot) * 300 for time, slot in zip(times, slots)]
        deltas = [b - a for a, b in zip(times, times[1:])]
        unique = set(slots)
        result["candidate_grid"] = {
            "median_raw_minutes": str(median(deltas) * 1440),
            "max_residual_seconds": str(max(residuals)),
            "duplicate_slots": len(slots) - len(unique),
            "missing_slots_in_span": int(max(slots) - min(slots) + 1 - len(unique)),
            "backwards_raw_steps": sum(delta < 0 for delta in deltas),
            "close_boundary_rows": times.count(END),
            "in_cycle_rows": sum(START <= time < END for time in times),
            "slots_on_2020_03_29": sum(Decimal(43919) <= slot / GRID < Decimal(43920) for slot in slots),
        }
    if Path(entry["path"]).stem == "GreenhouseClimate":
        result["setpoints"] = {
            name: {"missing": missing[name], "symbols": symbols[name],
                   "zero": data.count(0), "negative": sum(value < 0 for value in data),
                   "min": str(min(data)) if data else None, "max": str(max(data)) if data else None}
            for name, data in values.items()
        }
    return result


def byte_format(path):
    payload = path.read_bytes()
    crlf = payload.count(b"\r\n")
    return {"utf8_bom": payload.startswith(b"\xef\xbb\xbf"),
            "crlf": crlf, "bare_lf": payload.count(b"\n") - crlf,
            "bare_cr": payload.count(b"\r") - crlf}


def main():
    snapshot = Path("/data/raw") / AGC.dataset / AGC.sha256
    manifest = load_verified_manifest(snapshot)
    if (manifest["snapshot_id"] != f"{AGC.dataset}-{AGC.sha256}"
            or manifest["archive"]["sha256"] != AGC.sha256):
        raise ValueError("Not the locked AGC snapshot")
    selected = {"GreenhouseClimate", "GrodanSens", "Weather", "Resources", "Production"}
    report = {
        "snapshot_id": manifest["snapshot_id"],
        "note": "Aggregates only; epoch/grid are analytical conventions, not verified source timezone/slot semantics.",
        "files": [observe(snapshot, entry) for entry in manifest["files"]
                  if Path(entry["path"]).stem in selected],
        "formats": [{"path": entry["path"], **byte_format(snapshot / entry["path"]), "columns": entry["csv"]["column_count"],
                     "row_width_counts": entry["csv"]["row_width_counts"],
                     "all_empty_column_indices": entry["csv"]["all_empty_column_indices"]}
                    for entry in manifest["files"] if entry["type"] == "csv"],
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
