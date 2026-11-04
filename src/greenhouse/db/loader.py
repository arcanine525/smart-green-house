"""Load one verified T06 run into candidate core; no publication or recovery."""

from contextlib import ExitStack
from datetime import date
import gzip
import hashlib
import heapq
import json
from itertools import chain, islice
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from greenhouse.ingestion.parser import PARSER_VERSION, Source, SourceParser
from greenhouse.ingestion.run import json_value
from greenhouse.db.migrate import sql

AGC_SHA = "c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"
AGC_SNAPSHOT = "agc-2nd-2019-" + AGC_SHA
AGC_SCOPE = AGC_SNAPSHOT + ":historical"
CORE_KINDS = {"Resources", "Production", "GreenhouseClimate"}
BATCH_SIZE = 1000


def digest_file(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def safe_child(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in str(path):
        raise ValueError("Unsafe manifest path")
    target = root / path
    if target.is_symlink() or root.resolve() not in target.resolve().parents:
        raise ValueError("Unsafe manifest link")
    return target


def staging_records(folder, raw_snapshot, manifest, file):
    """Read only declared outputs, merge original order and recheck against raw.

    Reusing the versioned source parser prevents accepting a changed typed value,
    timestamp, raw hash or lineage in a mutable JSONL staging file. No globbing.
    """
    relative = file["source_file"]
    path = safe_child(raw_snapshot, relative)
    if digest_file(path) != file["file_checksum"]:
        raise ValueError("Raw file checksum mismatch: " + relative)
    source = Source(manifest["snapshot_id"], manifest["snapshot_sha"], relative,
                    file["file_checksum"], manifest["run_id"])
    parser = SourceParser(path, source)
    for key, value in parser.metadata().items():
        if file.get(key) != value:
            raise ValueError("Adapter/header metadata mismatch: " + relative)
    with ExitStack() as stack:
        iterators = []
        for category in ("accepted", "quarantine", "duplicate"):
            stream = stack.enter_context(gzip.open(safe_child(folder, file["outputs"][category]), "rt", encoding="utf-8"))
            def read_rows(handle, expected_category):
                for line in handle:
                    row = json.loads(line)
                    if row.get("disposition") not in (("quarantine", "conflict") if expected_category == "quarantine" else (expected_category,)):
                        raise ValueError("Staging disposition mismatch")
                    yield row
            iterators.append(read_rows(stream, category))
        merged = iter(heapq.merge(*iterators, key=lambda row: row["line_number"]))
        counts = dict.fromkeys(("accepted", "quarantine", "duplicate", "conflict"), 0)
        read = 0
        for expected in parser.records():
            try:
                row = next(merged)
            except StopIteration:
                raise ValueError("Missing staging record: " + relative) from None
            canonical = json.loads(json.dumps(expected, default=json_value))
            for key in canonical.keys() - {"issues"}:
                if row.get(key) != canonical[key]:
                    raise ValueError(f"Staging/raw mismatch {relative}:{expected['line_number']} field={key}")
            expected_issues = canonical["issues"]
            if row["disposition"] == "conflict":
                extras = [i for i in row.get("issues", []) if i not in expected_issues]
                if len(extras) != 1 or extras[0].get("code") != "conflicting_key":
                    raise ValueError("Invalid staging conflict issue")
            elif row.get("issues") != expected_issues:
                raise ValueError("Staging issues differ from raw parser")
            if row["disposition"] == "accepted" and row.get("issues"):
                raise ValueError("Accepted record carries parse issues")
            counts[row["disposition"]] += 1
            read += 1
            yield row
        if next(merged, None) is not None or read != file["read"] or any(counts[k] != file[k] for k in counts):
            raise ValueError("Staging count mismatch: " + relative)


def load_core(connection, folder, raw_snapshot, scope_id=AGC_SCOPE, policy_id="AGC_QUALITY_V1"):
    folder, raw_snapshot = Path(folder).resolve(), Path(raw_snapshot).resolve()
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest["status"] not in ("completed", "completed_with_issues", "failed"):
        raise ValueError("Unfinished ingestion run is not consumable")
    if manifest["parser_version"] != PARSER_VERSION:
        raise ValueError("Unsupported parser version")
    raw_manifest = json.loads((raw_snapshot / "manifest.json").read_text())
    if (raw_manifest["snapshot_id"] != manifest["snapshot_id"]
            or raw_manifest["archive"]["sha256"] != manifest["snapshot_sha"]):
        raise ValueError("Raw snapshot manifest identity mismatch")
    expected_files = {f["path"]: f for f in raw_manifest["files"]}
    if len(expected_files) != len(raw_manifest["files"]) or set(expected_files) != {f["source_file"] for f in manifest["files"]}:
        raise ValueError("Ingestion file set differs from raw snapshot manifest")
    for file in manifest["files"]:
        original = expected_files[file["source_file"]]
        if (file["file_checksum"] != original["sha256"]
                or file.get("expected_rows") != original.get("csv", {}).get("data_rows")):
            raise ValueError("Ingestion source metadata differs from raw snapshot")
    if manifest["status"] != "failed" and manifest.get("counts") != {
            key: sum(f[key] for f in manifest["files"]) for key in ("read", "accepted", "quarantine", "duplicate", "conflict")}:
        raise ValueError("Unbalanced ingestion run counts")
    run_id = manifest["run_id"]
    blocked = manifest["status"] == "failed"
    with connection.transaction():
        scope = connection.execute(sql("load_scope"), (scope_id,)).fetchone()
        if scope != (manifest["snapshot_id"], manifest["snapshot_sha"]):
            raise ValueError("Run snapshot does not match declared scope")
        if connection.execute("SELECT 1 FROM meta.pipeline_run WHERE run_id=%s", (run_id,)).fetchone():
            raise ValueError("Run already loaded; no reload in this loader")
        connection.execute("INSERT INTO meta.pipeline_run(run_id,data_scope_id,snapshot_id,status,parser_version,quality_policy_version,source_manifest) VALUES (%s,%s,%s,'loading',%s,%s,%s)",
                           (run_id, scope_id, manifest["snapshot_id"], PARSER_VERSION, policy_id, Jsonb(manifest)))
        files, seen = [], set()
        prepared = False
        load_stats = {"source_records": 0, "sql_batches": 0, "max_batch_records": 0}
        for file in manifest["files"]:
            relative = file["source_file"]
            safe_child(raw_snapshot, relative)
            if relative in seen:
                raise ValueError("Repeated source file")
            seen.add(relative)
            if file["read"] != sum(file[k] for k in ("accepted", "quarantine", "duplicate", "conflict")):
                raise ValueError("Unbalanced ingestion file counts")
            file_id = hashlib.sha256((manifest["snapshot_sha"] + ":" + relative).encode()).hexdigest()
            existing = connection.execute("SELECT checksum,expected_rows FROM meta.source_file WHERE source_file_id=%s", (file_id,)).fetchone()
            expected = (file["file_checksum"], file.get("expected_rows"))
            if existing and existing != expected:
                raise ValueError("Conflicting source file metadata")
            if not existing:
                connection.execute("INSERT INTO meta.source_file VALUES (%s,%s,%s,%s,%s,%s)",
                                   (file_id, manifest["snapshot_id"], relative, *expected, Path(relative).parent.as_posix()))
            allowed = file["status"] in ("loaded", "loaded_with_quarantine") and file.get("outputs_consumable", True)
            core_status = ("failed" if file["status"] == "failed" or not file.get("outputs_consumable", True)
                           else "blocked" if blocked and allowed else "deferred" if Path(relative).stem not in CORE_KINDS
                           else file["status"])
            connection.execute("INSERT INTO meta.file_ingestion VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (run_id, file_id, file["status"], core_status, allowed and not blocked,
                                file["read"], file["accepted"], file["quarantine"], file["duplicate"], file["conflict"], Jsonb(file)))
            files.append({"file": relative, "ingestion_status": file["status"], "core_status": core_status})
            if not allowed or blocked or Path(relative).stem not in CORE_KINDS:
                continue
            mapping = connection.execute("SELECT cycle_id FROM meta.source_folder_mapping WHERE snapshot_id=%s AND source_folder=%s",
                                         (manifest["snapshot_id"], Path(relative).parent.as_posix())).fetchone()
            if not mapping:
                raise ValueError("Missing explicit source-folder mapping: " + relative)
            rows = iter(staging_records(folder, raw_snapshot, manifest, file))
            while (first := next(rows, None)) is not None:
                if not prepared:
                    connection.execute(sql("prepare_load"))
                    prepared = True
                batch_count = 0
                with connection.cursor().copy(sql("copy_input")) as copy:
                    for row in chain((first,), islice(rows, BATCH_SIZE - 1)):
                        values = row["values"]
                        copy.write_row((row["source_record_id"], file_id, manifest["snapshot_id"], run_id, scope_id,
                                        mapping[0], Path(relative).stem, row["line_number"], row["line_end"], row["raw_reference"], row["raw_hash"],
                                        values.get("time"), row["raw_tokens"][0] if row["raw_tokens"] else None,
                                        row["source_timestamp"], row["slot_residual_seconds"], row["adapter_id"], row["adapter_version"],
                                        row["time_policy_version"], row["slot_start"] or row["source_timestamp"], row["is_close_boundary"],
                                        row["logical_key"], row["disposition"], json.dumps(values), json.dumps(row["null_reasons"]), json.dumps(row["issues"])))
                        batch_count += 1
                connection.execute(sql("load_content"))
                load_stats["source_records"] += batch_count
                load_stats["sql_batches"] += 1
                load_stats["max_batch_records"] = max(load_stats["max_batch_records"], batch_count)
        if prepared:
            connection.execute(sql("finish_load"))
        status = "blocked" if blocked else "core_loaded"
        connection.execute("UPDATE meta.pipeline_run SET status=%s WHERE run_id=%s", (status, run_id))
    return {"run_id": run_id, "status": status, "scope_id": scope_id, "files": files, "load_stats": load_stats, **aggregate(connection, run_id)}


def aggregate(connection, run_id):
    with connection.cursor(row_factory=dict_row) as cursor:
        records = cursor.execute(sql("aggregate"), (run_id,)).fetchall()
        quality = cursor.execute(sql("aggregate_quality"), (run_id,)).fetchall()
        counts = cursor.execute("SELECT (SELECT count(*) FROM meta.quarantine_record WHERE run_id=%s) AS quarantine_records, (SELECT count(*) FROM meta.dq_issue WHERE run_id=%s) AS dq_issues", (run_id, run_id)).fetchone()
    return {"record_counts": records, "quality_counts": quality, **counts}


def explain(connection, scope, compartment, day, measure, limit=10):
    if not 1 <= limit <= 100:
        raise ValueError("Explain limit must be 1–100")
    day = date.fromisoformat(day)
    with connection.cursor(row_factory=dict_row) as cursor:
        values = cursor.execute(sql("explain"), (scope, compartment, day, measure, limit)).fetchall()
        quarantine = cursor.execute(sql("explain_quarantine"), (scope, compartment, limit)).fetchall()
    return {"kind": "unpublished_core_diagnostic", "values": values, "cycle_quarantine": quarantine}
