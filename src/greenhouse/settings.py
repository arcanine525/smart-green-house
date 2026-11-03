"""Environment configuration shared by container commands."""

from dataclasses import dataclass, field
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    import_dir: Path
    raw_dir: Path
    output_dir: Path
    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str = field(repr=False)

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        required = ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise ValueError("Thiếu cấu hình: " + ", ".join(missing))
        try:
            port = int(env.get("PGPORT", "5432"))
        except ValueError:
            raise ValueError("PGPORT phải là số nguyên 1–65535") from None
        if not 1 <= port <= 65535:
            raise ValueError("PGPORT phải là số nguyên 1–65535")
        paths = [Path(env.get(key, default)) for key, default in (
            ("GREENHOUSE_IMPORT_DIR", "/data/import"),
            ("GREENHOUSE_RAW_DIR", "/data/raw"),
            ("GREENHOUSE_OUTPUT_DIR", "/data/output"),
        )]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("Đường dẫn trong container phải là tuyệt đối")
        if len(set(path.resolve() for path in paths)) != len(paths):
            raise ValueError("Import, raw và output phải là các thư mục riêng")
        return cls(*paths, env["PGHOST"], port, env["PGDATABASE"], env["PGUSER"], env["PGPASSWORD"])

    def connection_kwargs(self):
        return dict(host=self.pg_host, port=self.pg_port, dbname=self.pg_database,
                    user=self.pg_user, password=self.pg_password, connect_timeout=5)
