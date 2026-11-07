"""Per-run filesystem staging and ingestion manifest; never publishes core data."""

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import re
import uuid

from greenhouse.ingestion.parser import HEADERS, PARSER_VERSION, Source, SourceParser

COUNTS = ("read", "accepted", "quarantine", "duplicate", "conflict")


def json_value(value):
    # Exact Decimal strings and ISO source-naive timestamps, never binary floats.
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def checksum(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_manifest(folder, manifest):
    temporary = folder / "manifest.pending.json"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(folder / "manifest.json")


def ingest_file(path, source, folder, expected_rows=None):
    """Two passes with a compact key index: withhold EVERY conflicting member.

    Duplicate means same logical key and identical parsed CSV token vector (not
    physical newline bytes). Different serial lexemes colliding on a slot are
    conflicts too. A parse-invalid row with a usable key participates in conflict
    detection; no arbitrary first winner is left in accepted staging.
    """
    parser = SourceParser(path, source)
    index = {}
    for record in parser.records():
        key = record["logical_key"]
        if key is not None:
            content = hashlib.sha256(json.dumps(record["raw_tokens"], ensure_ascii=False).encode()).hexdigest()
            if key not in index:
                index[key] = [content, record["line_number"], False]
            elif index[key][0] != content:
                index[key][2] = True
    result = {"source_file": source.relative_path, "file_checksum": source.file_checksum,
              **parser.metadata(), **dict.fromkeys(COUNTS, 0), "expected_rows": expected_rows,
              "null_counts": {}, "issue_counts": {}, "outputs": {}}
    streams = {}
    try:
        for category in ("accepted", "quarantine", "duplicate"):
            relative = Path(category) / (source.relative_path + ".jsonl.gz")
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            streams[category] = gzip.open(target, "xt", encoding="utf-8", compresslevel=1)
            result["outputs"][category] = relative.as_posix()
        null_counts, issue_counts = Counter(), Counter()
        for record in parser.records():
            result["read"] += 1
            key = record["logical_key"]
            if key is not None and index[key][2]:
                category = "conflict"
                record["issues"].append({"code": "conflicting_key", "first_line": index[key][1]})
            elif record["issues"]:
                category = "quarantine"
            elif index[key][1] != record["line_number"]:
                category = "duplicate"
                record["duplicate_of_line"] = index[key][1]
            else:
                category = "accepted"
            result[category] += 1
            record["disposition"] = category
            issue_counts.update(issue["code"] for issue in record["issues"])
            null_counts.update(record["null_reasons"].keys())
            stream = streams["quarantine" if category == "conflict" else category]
            stream.write(json.dumps(record, default=json_value, ensure_ascii=False, separators=(",", ":")) + "\n")
        result["null_counts"], result["issue_counts"] = dict(null_counts), dict(issue_counts)
    finally:
        for stream in streams.values():
            stream.close()
    fatal = bool(parser.file_issues) or "csv_lexical_error" in result["issue_counts"]
    if expected_rows is not None and result["read"] != expected_rows:
        result["file_issues"].append({"code": "record_count_mismatch", "expected": expected_rows, "actual": result["read"]})
        fatal = True
    if checksum(path) != source.file_checksum:
        result["file_issues"].append({"code": "source_changed_during_parse"})
        fatal = True
    result["status"] = ("failed" if fatal else "loaded_with_quarantine" if result["quarantine"] or result["conflict"] else "loaded")
    result["complete_for_ingestion"] = result["status"] == "loaded"
    assert result["read"] == sum(result[key] for key in COUNTS[1:])
    return result


def ingest_snapshot(snapshot, source_manifest, output_root, run_id=None):
    """Consume a caller-verified source manifest; allocate a new isolated run.

    CLI verifies the locked ZIP and every snapshot file first. File checksums are
    also checked here before/after parsing, including deferred source payloads.
    A run cannot be reused/overwritten; recovery/idempotent core loading is T08.
    """
    snapshot, output_root = Path(snapshot).resolve(), Path(output_root).resolve()
    if snapshot == output_root or snapshot in output_root.parents or output_root in snapshot.parents:
        raise ValueError("Snapshot and output must be disjoint")
    run_id = run_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise ValueError("Invalid run_id")
    folder = output_root / "ingestion" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {"manifest_version": 1, "run_id": run_id, "status": "running",
                "parser_version": PARSER_VERSION, "snapshot_id": source_manifest["snapshot_id"],
                "snapshot_sha": source_manifest["archive"]["sha256"],
                "started_at_utc": datetime.now(timezone.utc).isoformat(), "files": []}
    write_manifest(folder, manifest)
    seen = set()
    try:
        for entry in sorted(source_manifest["files"], key=lambda item: item["path"]):
            relative = Path(entry["path"])
            if (relative.is_absolute() or ".." in relative.parts or "\\" in str(relative)
                    or relative.as_posix() in seen):
                raise ValueError("Unsafe or repeated source path")
            seen.add(relative.as_posix())
            path = snapshot / relative
            if (not path.is_file() or any(p.is_symlink() for p in [path, *path.parents])
                    or checksum(path) != entry["sha256"]):
                raise ValueError("Source integrity mismatch: " + relative.as_posix())
            expected_rows = entry.get("csv", {}).get("data_rows")
            if relative.stem not in HEADERS or relative.suffix != ".csv":
                result = {"source_file": relative.as_posix(), "file_checksum": entry["sha256"],
                          "status": "deferred", "complete_for_ingestion": False,
                          "reason": "not_required_for_current_six_br_staging", "expected_rows": expected_rows,
                          **dict.fromkeys(COUNTS, 0)}
            else:
                source = Source(manifest["snapshot_id"], manifest["snapshot_sha"], relative.as_posix(), entry["sha256"], run_id)
                try:
                    result = ingest_file(path, source, folder, expected_rows)
                except UnicodeError:
                    # No decoded record stream can be trusted. Preserve raw and
                    # the failed file entry, never report a completed ingestion.
                    result = {"source_file": relative.as_posix(), "file_checksum": entry["sha256"],
                              "status": "failed", "complete_for_ingestion": False,
                              "expected_rows": expected_rows, "file_issues": [{"code": "invalid_utf8"}],
                              "outputs_consumable": False, **dict.fromkeys(COUNTS, 0)}
            manifest["files"].append(result)
            write_manifest(folder, manifest)
        manifest["counts"] = {key: sum(f[key] for f in manifest["files"]) for key in COUNTS}
        manifest["file_status_counts"] = dict(Counter(f["status"] for f in manifest["files"]))
        manifest["deferred_rows"] = sum(f["expected_rows"] or 0 for f in manifest["files"] if f["status"] == "deferred")
        manifest["status"] = ("failed" if any(f["status"] == "failed" for f in manifest["files"])
                              else "completed_with_issues" if manifest["counts"]["quarantine"] or manifest["counts"]["conflict"]
                              else "completed")
    except Exception as error:
        manifest["status"] = "failed"
        manifest["error_type"] = type(error).__name__
        raise
    finally:
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_manifest(folder, manifest)
    return folder, manifest


def aggregate_summary(folder, manifest):
    """Only aggregates and output manifest location; never echoes raw CSV rows."""
    return {"run_id": manifest["run_id"], "status": manifest["status"],
            "manifest_path": str(folder / "manifest.json"), "counts": manifest["counts"],
            "file_status_counts": manifest["file_status_counts"], "deferred_rows": manifest["deferred_rows"],
            "files": [{key: file[key] for key in ("source_file", "status", *COUNTS)} for file in manifest["files"]]}
