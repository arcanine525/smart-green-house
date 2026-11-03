"""Single container entrypoint; future tasks must implement their own commands."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from greenhouse.settings import Settings

PENDING = {
    "ingest": "T05–T06 (snapshot/import và parser)",
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
    parser.add_argument("command", choices=["setup", "test", "preview", *PENDING])
    arguments, rest = parser.parse_known_args(argv)
    if arguments.command in PENDING:
        print(f"{arguments.command}: chưa hiện thực — thuộc task sau: {PENDING[arguments.command]}", file=sys.stderr)
        return 2
    if arguments.command == "test":
        return subprocess.call([sys.executable, "-m", "pytest", *rest])
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
