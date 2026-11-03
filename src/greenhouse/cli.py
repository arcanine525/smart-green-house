"""Single container entrypoint; future tasks must implement their own commands."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from greenhouse.settings import Settings

PENDING = {
    "ingest": "T06 (parser; dùng snapshot để chuẩn bị archive ở T05)",
    "transform": "T07–T09 (core, publication và marts)",
    "simulate": "T32 (sensor simulator)",
    "collect": "T33 (durable MQTT collector)",
    "stream": "T34 (Structured Streaming)",
}


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
    parser.add_argument("command", choices=["setup", "test", "preview", "snapshot", "inventory", "verify-fixtures", *PENDING])
    arguments, rest = parser.parse_known_args(argv)
    if arguments.command in PENDING:
        print(f"{arguments.command}: chưa hiện thực — thuộc task sau: {PENDING[arguments.command]}", file=sys.stderr)
        return 2
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
