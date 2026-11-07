"""Pure source adapters and lexical typing; no database or business quality rules."""

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path

PARSER_VERSION = "agc-parser-v1"
TIME_POLICY = "agc-source-naive-grid-v1"
CYCLE_POLICY = "agc-production-window-v1"
EPOCH = datetime(1899, 12, 30)
START, END = Decimal(43815), Decimal(43981)
NULLS = {"": "empty", "nan": "nan_token", "na": "na_token", "n/a": "n_a_token"}
HEADERS = {
    "Resources": "%Time ,Heat_cons,ElecHigh,ElecLow,CO2_cons,Irr,Drain".split(","),
    "Production": "%time,ProdA,ProdB,avg_nr_harvested_trusses,Truss development time ,Nr_fruits_ClassA,Weight_fruits_ClassA,Nr_fruits_ClassB,Weight_fruits_ClassB".split(","),
    "GreenhouseClimate": "%time,AssimLight,BlackScr,CO2air,Cum_irr,EC_drain_PC,EnScr,HumDef,PipeGrow,PipeLow,Rhair,Tair,Tot_PAR,Tot_PAR_Lamps,VentLee,Ventwind,assim_sp,assim_vip,co2_dos,co2_sp,co2_vip,dx_sp,dx_vip,int_blue_sp,int_blue_vip,int_farred_sp,int_farred_vip,int_red_sp,int_red_vip,int_white_sp,int_white_vip,pH_drain_PC,scr_blck_sp,scr_blck_vip,scr_enrg_sp,scr_enrg_vip,t_grow_min_sp,t_grow_min_vip,t_heat_sp,t_heat_vip,t_rail_min_sp,t_rail_min_vip,t_vent_sp,t_ventlee_vip,t_ventwind_vip,water_sup,water_sup_intervals_sp_min,water_sup_intervals_vip_min,window_pos_lee_sp,window_pos_lee_vip".split(","),
    "LabAnalysis": "%Time,irr_PH,irr_EC,irr_NH4,irr_K,irr_Na,irr_Ca,irr_Mg,irr_Si,irr_NO3,irr_Cl,irr_SO4,irr_HCO3,irr_PO4,irr_Fe,irr_Mn,irr_Zn,irr_B,irr_Cu,irr_Mo,drain_PH,drain_EC,drain_NH4,drain_K,drain_Na,drain_Ca,drain_Mg,drain_Si,drain_NO3,drain_Cl,drain_SO4,drain_HCO3,drain_PO4,drain_Fe,drain_Mn,drain_Zn,drain_B,drain_Cu,drain_Mo".split(","),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalize_header(name):
    trimmed = " ".join(name.split())
    return "time" if trimmed in ("%time", "%Time") else trimmed


@dataclass(frozen=True)
class Source:
    snapshot_id: str
    snapshot_sha: str
    relative_path: str
    file_checksum: str
    run_id: str


class TrackedLines:
    """Capture the exact decoded physical lines consumed by one CSV attempt."""
    def __init__(self, handle):
        self.handle, self.parts = handle, []

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self.handle)
        self.parts.append(line)
        return line


class SourceParser:
    def __init__(self, path, source):
        self.path, self.source = Path(path), source
        self.kind = Path(source.relative_path).stem
        if self.kind not in HEADERS:
            raise ValueError("Unsupported source type; must be deferred")
        self.adapter_id, self.adapter_version = "agc-standard", 1
        self.header, self.header_text, self.file_issues = [], "", []
        with self.path.open("rb") as raw:
            self.bom = raw.read(3) == b"\xef\xbb\xbf"
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            tracked = TrackedLines(handle)
            reader = csv.reader(tracked, strict=True)
            try:
                self.header = next(reader)
            except (StopIteration, csv.Error):
                self.file_issues.append({"code": "invalid_or_missing_header", "line": 1})
            self.header_text = "".join(tracked.parts)
        self.width = len(self.header)
        self.headers = [normalize_header(name) for name in self.header]
        if source.relative_path == "IUACAAS/Resources.csv" and self.width == 12:
            self.adapter_id = "agc-iuacaas-resources-empty-tail"
            if self.header != HEADERS["Resources"] + [""] * 5:
                self.file_issues.append({"code": "adapter_header_signature", "line": 1})
            else:
                self._check_empty_tail()
            self.headers = self.headers[:7]
        if len(set(self.headers)) != len(self.headers):
            self.file_issues.append({"code": "header_collision", "line": 1})
        expected = {normalize_header(name) for name in HEADERS[self.kind]}
        if set(self.headers) != expected:
            self.file_issues.append({"code": "schema_columns", "line": 1,
                                     "missing": sorted(expected - set(self.headers)),
                                     "extra": sorted(set(self.headers) - expected)})

    def _check_empty_tail(self):
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            next(reader)
            try:
                for row in reader:
                    if len(row) != 12 or any(token.strip() for token in row[7:]):
                        self.file_issues.append({"code": "adapter_nonempty_or_wrong_width_tail", "line": reader.line_num})
                        break
            except csv.Error:
                self.file_issues.append({"code": "adapter_csv_error", "line": reader.line_num})

    def metadata(self):
        return {"adapter_id": self.adapter_id, "adapter_version": self.adapter_version,
                "header_raw": self.header, "header_raw_text": self.header_text,
                "header_normalized": self.headers, "utf8_bom": self.bom,
                "file_issues": self.file_issues}

    def records(self):
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            tracked = TrackedLines(handle)
            reader = csv.reader(tracked, strict=True)
            try:
                next(reader)
            except (StopIteration, csv.Error):
                return
            while True:
                tracked.parts.clear()
                first_line = reader.line_num + 1
                try:
                    tokens = next(reader)
                except StopIteration:
                    return
                except csv.Error:
                    yield self._record([], "".join(tracked.parts), first_line, reader.line_num,
                                       [{"code": "csv_lexical_error"}])
                    return  # An unterminated tail cannot be safely resynchronized.
                yield self._record(tokens, "".join(tracked.parts), first_line, reader.line_num, [])

    def _record(self, tokens, raw_text, first_line, last_line, issues):
        issues = list(self.file_issues) + issues
        raw_hash = digest(raw_text.encode("utf-8"))
        source = self.source
        record = {
            "snapshot_id": source.snapshot_id, "snapshot_sha": source.snapshot_sha,
            "source_file": source.relative_path, "file_checksum": source.file_checksum,
            "line_number": first_line, "line_end": last_line, "run_id": source.run_id,
            "source_record_id": digest(f"{source.snapshot_sha}\0{source.relative_path}\0{first_line}".encode()),
            "raw_reference": f"{source.relative_path}:{first_line}", "raw_hash": raw_hash,
            "raw_tokens": tokens, "raw_text": raw_text,
            "adapter_id": self.adapter_id, "adapter_version": self.adapter_version,
            "parser_version": PARSER_VERSION, "time_policy_version": TIME_POLICY,
            "values": {}, "null_reasons": {}, "issues": issues,
            "logical_key": None, "source_timestamp": None, "slot_start": None,
            "slot_residual_seconds": None, "is_close_boundary": False,
        }
        if len(tokens) != self.width:
            issues.append({"code": "row_width", "expected": self.width, "actual": len(tokens)})
        if issues:
            return record  # Never map values across an unverified width/header.
        for index, name in enumerate(self.headers):
            token = tokens[index].strip()
            if token.lower() in NULLS:
                record["values"][name] = None
                record["null_reasons"][name] = NULLS[token.lower()]
            else:
                try:
                    value = Decimal(token)
                    if not value.is_finite():
                        raise InvalidOperation
                    record["values"][name] = value
                except InvalidOperation:
                    record["values"][name] = None
                    issues.append({"code": "invalid_numeric", "column": name, "column_index": index})
        serial = record["values"]["time"]
        if serial is None:
            issues.append({"code": "invalid_or_missing_time"})
            return record
        try:
            observed = EPOCH + timedelta(microseconds=int(serial * 86400000000))
            record["source_timestamp"] = observed
            if self.kind == "GreenhouseClimate":
                slot = (serial * 288).to_integral_value(rounding=ROUND_HALF_EVEN)
                residual = abs(serial * 288 - slot) * 300
                record["slot_residual_seconds"] = residual
                if residual > Decimal("0.5"):
                    issues.append({"code": "off_grid_time"})
                else:
                    record["slot_start"] = EPOCH + timedelta(minutes=int(slot) * 5)
                    record["logical_key"] = record["slot_start"].isoformat()
                record["is_close_boundary"] = record["slot_start"] == EPOCH + timedelta(days=int(END))
            elif serial != serial.to_integral_value():
                issues.append({"code": "non_midnight_daily_time"})
            else:
                record["logical_key"] = observed.date().isoformat()
            if self.kind == "Production" and not START <= serial < END:
                issues.append({"code": "out_of_cycle_unresolved", "policy_version": CYCLE_POLICY})
        except (ArithmeticError, ValueError):
            issues.append({"code": "time_out_of_range"})
        return record
