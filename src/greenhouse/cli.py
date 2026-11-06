"""Single container entrypoint; future tasks must implement their own commands."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from greenhouse.settings import Settings

PENDING = {}


def setup_environment(settings):
    """Check mounts without changing import/raw, test output writes and DB access.

    No migrations or dataset inspection are performed by this scaffold.
    """
    for label, path in (("import", settings.import_dir), ("raw", settings.raw_dir)):
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise ValueError(f"Mount {label} không đọc được hoặc không phải thư mục")
    output = settings.output_dir
    if not output.is_dir():
        raise ValueError("Mount output không phải thư mục")
    # A real write check is more reliable than access() on Docker bind mounts.
    with tempfile.TemporaryFile(dir=output) as handle:
        handle.write(b"greenhouse-setup-check")
        handle.flush()
    import psycopg
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        if connection.execute("SELECT 1").fetchone() != (1,):
            raise ValueError("PostgreSQL chưa sẵn sàng")
    return "Setup OK: mount import/raw đọc được, output ghi được, PostgreSQL reachable; chưa chạy migration/ingest."


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m greenhouse")
    parser.add_argument("command", choices=["setup", "test", "preview", "snapshot", "inventory", "verify-fixtures", "ingest", "migrate", "load-core", "explain-core", "run-pipeline", "transform", "publish", "correct-core", "lineage", "simulate", "collect", "stream", "register-agc-tariff", *PENDING])
    arguments, rest = parser.parse_known_args(argv)
    if arguments.command == "register-agc-tariff":
        from greenhouse.db.reference_tariff import main as tariff_main
        return tariff_main(rest)
    if arguments.command in PENDING:
        print(f"{arguments.command}: chưa hiện thực — thuộc task sau: {PENDING[arguments.command]}", file=sys.stderr)
        return 2
    if arguments.command == "stream":
        from greenhouse.sensor.stream_cli import stream_command
        return stream_command(rest)
    if arguments.command == "simulate":
        from greenhouse.sensor.cli import simulate_command
        return simulate_command(rest)
    if arguments.command == "collect":
        from greenhouse.sensor.cli import collect_command
        return collect_command(rest)
    if arguments.command == "transform":
        return pipeline_command("run-pipeline", rest)
    if arguments.command in ("run-pipeline", "publish", "correct-core", "lineage"):
        return pipeline_command(arguments.command, rest)
    if arguments.command in ("migrate", "load-core", "explain-core"):
        return core_command(arguments.command, rest)
    if arguments.command == "ingest":
        return ingestion_command(rest)
    if arguments.command == "test":
        return subprocess.call([sys.executable, "-m", "pytest", *rest])
    if arguments.command in ("snapshot", "inventory", "verify-fixtures"):
        return dataset_command(arguments.command, rest)
    if arguments.command == "preview":
        script = Path("/workspace/prototypes/dashboard/build_preview.py")
        if not script.is_file():
            print("preview cần image UI; dùng service preview-build", file=sys.stderr)
            return 2
        return subprocess.call([sys.executable, str(script), *rest])
    if rest:
        parser.error("setup không nhận thêm tham số")
    try:
        print(setup_environment(Settings.from_env()))
        return 0
    except (ValueError, OSError) as error:
        # Do not include exception messages that could echo credentials/DSNs.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f"Setup thất bại: {message}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Setup thất bại: {type(error).__name__} (kiểm tra DB/config)", file=sys.stderr)
        return 1


def dataset_command(command, argv):
    import json
    from greenhouse.ingestion.fixtures import verify_fixtures
    from greenhouse.ingestion.inventory import load_verified_manifest, render_inventory
    from greenhouse.ingestion.snapshot import AGC, create_snapshot, download_archive
    parser = argparse.ArgumentParser(prog=f"python -m greenhouse {command}")
    parser.add_argument("--raw-dir", type=Path, default=Path(os.environ.get("GREENHOUSE_RAW_DIR", "/data/raw")))
    parser.add_argument("--fixtures", type=Path, default=Path("/workspace/tests/fixtures/agc_synthetic"))
    if command == "snapshot":
        parser.add_argument("--archive", type=Path, default=Path("/data/import") / AGC.filename)
        parser.add_argument("--download", action="store_true")
    if command == "inventory":
        parser.add_argument("--snapshot", type=Path)
        parser.add_argument("--output", type=Path)
    options = parser.parse_args(argv)
    try:
        if command in ("snapshot", "verify-fixtures"):
            fixtures = verify_fixtures(options.fixtures)
        if command == "snapshot":
            archive = download_archive(options.raw_dir) if options.download else options.archive
            path, manifest, created = create_snapshot(archive, options.raw_dir, source_method="download" if options.download else "local")
            print(json.dumps({"status": "created" if created else "already_exists_verified", "snapshot_path": str(path),
                              "snapshot_id": manifest["snapshot_id"], "file_count": manifest["file_count"],
                              "csv_count": manifest["csv_count"], "pdf_count": manifest["pdf_count"],
                              "total_csv_rows": manifest["total_csv_rows"], "fixtures": fixtures}, ensure_ascii=False))
        elif command == "inventory":
            path = options.snapshot or options.raw_dir / AGC.dataset / AGC.sha256
            manifest = load_verified_manifest(path)
            if manifest["archive"] != {"sha256": AGC.sha256, "size_bytes": AGC.size_bytes}:
                raise ValueError("Inventory source does not match locked AGC archive")
            text = render_inventory(manifest)
            if options.output:
                options.output.write_text(text)
            else:
                print(text, end="")
        else:
            print(json.dumps(fixtures))
        return 0
    except Exception as error:
        print(f"{command} failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


def ingestion_command(argv):
    import json
    import zipfile
    from greenhouse.ingestion.run import aggregate_summary, ingest_snapshot
    from greenhouse.ingestion.snapshot import AGC, archive_members, validate_snapshot, verify_archive
    parser = argparse.ArgumentParser(prog="python -m greenhouse ingest")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--archive", type=Path, default=Path("/data/import") / AGC.filename)
    parser.add_argument("--output-root", type=Path, default=Path(os.environ.get("GREENHOUSE_OUTPUT_DIR", "/data/output")))
    parser.add_argument("--run-id")
    options = parser.parse_args(argv)
    snapshot = options.snapshot or Path(os.environ.get("GREENHOUSE_RAW_DIR", "/data/raw")) / AGC.dataset / AGC.sha256
    try:
        # Read-only verification against ZIP bytes; no publisher lock/raw writes.
        verify_archive(options.archive)
        with zipfile.ZipFile(options.archive) as archive:
            manifest = validate_snapshot(snapshot, archive, archive_members(archive))
        folder, result = ingest_snapshot(snapshot, manifest, options.output_root, options.run_id)
        print(json.dumps(aggregate_summary(folder, result), ensure_ascii=False))
        return 1 if result["status"] == "failed" else 0
    except Exception as error:
        print(f"ingest failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


def core_command(command, argv):
    import json
    import psycopg
    from greenhouse.db.loader import AGC_SCOPE, AGC_SHA, explain, load_core
    from greenhouse.db.migrate import migrate
    parser = argparse.ArgumentParser(prog=f"python -m greenhouse {command}")
    if command != "migrate":
        parser.add_argument("--scope-id", default=AGC_SCOPE)
    if command == "load-core":
        parser.add_argument("--run", required=True, type=Path)
        parser.add_argument("--snapshot", type=Path, default=Path(os.environ.get("GREENHOUSE_RAW_DIR", "/data/raw")) / "agc-2nd-2019" / AGC_SHA)
        parser.add_argument("--quality-policy", default="AGC_QUALITY_V1")
    if command == "explain-core":
        parser.add_argument("--compartment", required=True)
        parser.add_argument("--date", required=True)
        parser.add_argument("--measure", required=True)
        parser.add_argument("--limit", type=int, default=10)
    options = parser.parse_args(argv)
    try:
        with psycopg.connect(**Settings.from_env().connection_kwargs()) as connection:
            if command == "migrate":
                result = migrate(connection)
            elif command == "load-core":
                result = load_core(connection, options.run, options.snapshot, options.scope_id, options.quality_policy)
            else:
                result = explain(connection, options.scope_id, options.compartment, options.date, options.measure, options.limit)
        # DB commands never echo connection strings/credentials on failure.
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 1 if result.get("status") == "blocked" else 0
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f"{command} failed: {message}", file=sys.stderr)
        return 1


def pipeline_command(command, argv):
    import json
    import psycopg
    from greenhouse.db.loader import AGC_SCOPE, AGC_SHA
    from greenhouse.db.pipeline import run_pipeline
    from greenhouse.db.corrections import correct
    from greenhouse.db.lineage import lineage
    parser = argparse.ArgumentParser(prog=f"python -m greenhouse {command}")
    parser.add_argument("--scope-id", default=AGC_SCOPE)
    parser.add_argument("--snapshot", type=Path, default=Path(os.environ.get("GREENHOUSE_RAW_DIR", "/data/raw")) / "agc-2nd-2019" / AGC_SHA)
    if command == "run-pipeline":
        parser.add_argument("--output-root", type=Path, default=Path(os.environ.get("GREENHOUSE_OUTPUT_DIR", "/data/output")))
        parser.add_argument("--archive", type=Path, default=Path("/data/import/autonomous-greenhouse-challengeagc-2nd-2019.zip"))
        parser.add_argument("--workflow-id")
        parser.add_argument("--resume", metavar="WORKFLOW_ID")
        parser.add_argument("--ingestion-run", type=Path, help="Adopt a verified existing ingestion run")
        parser.add_argument("--quality-policy", default="AGC_QUALITY_V1")
        parser.add_argument("--fault", help="Controlled validation only: before/after-step or publish-members/hook/pointer")
    elif command == "publish":
        parser.add_argument("--workflow-id", required=True)
    elif command == "lineage":
        parser.add_argument("--revision-id", required=True)
    else:
        parser.add_argument("--source-record-id", required=True)
        parser.add_argument("--record-type", required=True, choices=["climate", "drain", "resource", "harvest"])
        parser.add_argument("--old-value", required=True, type=Path, help="Full measure map JSON; exact decimal strings/null")
        parser.add_argument("--new-value", required=True, type=Path)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--evidence", required=True)
        parser.add_argument("--correction-id", required=True, help="Caller-generated idempotency key")
    options = parser.parse_args(argv)
    try:
        with psycopg.connect(**Settings.from_env().connection_kwargs(), autocommit=True) as conn:
            if command == "run-pipeline":
                if options.resume and options.workflow_id:
                    raise ValueError("Use --resume or --workflow-id")
                # Same locked ZIP verification as ingest; a self-consistent but
                # modified raw manifest is not proof of archive identity.
                if options.scope_id == AGC_SCOPE:
                    import zipfile
                    from greenhouse.ingestion.snapshot import verify_archive, validate_snapshot, archive_members
                    verify_archive(options.archive)
                    with zipfile.ZipFile(options.archive) as archive:
                        validate_snapshot(options.snapshot, archive, archive_members(archive))
                result = run_pipeline(conn, options.snapshot, options.output_root, options.scope_id, options.quality_policy,
                                      workflow=options.resume or options.workflow_id, resume=bool(options.resume),
                                      ingestion_run=options.ingestion_run, fault=options.fault)
            elif command == "publish":
                from greenhouse.db.pipeline import read_workflow
                run = read_workflow(conn, options.workflow_id)
                spec = run['input_spec']
                # Reuse the state/log recovery path, including failed publish.
                result = run_pipeline(conn, Path(spec['snapshot']), Path(spec['output']),
                                      run['data_scope_id'], run['versions']['quality_policy'],
                                      workflow=options.workflow_id, resume=True)
            elif command == "lineage":
                result = lineage(conn, options.revision_id, options.snapshot)
            else:
                result = correct(conn, options.scope_id, options.source_record_id, options.record_type,
                                 json.loads(options.old_value.read_text()), json.loads(options.new_value.read_text()),
                                 options.actor, options.evidence, correction_id=options.correction_id)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        print(f"{command} failed: {message}", file=sys.stderr)
        return 1
