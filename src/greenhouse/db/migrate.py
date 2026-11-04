"""Checksummed SQL migrations, applied transactionally under one advisory lock."""
import hashlib
from importlib.resources import files
from pathlib import Path


def sql(name):
    return files("greenhouse.db").joinpath("sql", name + ".sql").read_text()


def migrate(connection, directory=None):
    root = Path(directory) if directory else files("greenhouse.db").joinpath("migrations")
    applied, skipped = [], []
    with connection.transaction():
        connection.execute(sql("bootstrap"))
        for path in sorted(root.iterdir(), key=lambda p: p.name):
            if not path.name.endswith(".sql"):
                continue
            payload = path.read_bytes()
            checksum = hashlib.sha256(payload).hexdigest()
            previous = connection.execute("SELECT checksum FROM meta.schema_migration WHERE version=%s", (path.name,)).fetchone()
            if previous:
                if previous[0] != checksum:
                    raise ValueError("Applied migration checksum changed: " + path.name)
                skipped.append(path.name)
                continue
            connection.execute(payload.decode())
            connection.execute("INSERT INTO meta.schema_migration(version,checksum) VALUES (%s,%s)", (path.name, checksum))
            applied.append(path.name)
    return {"applied": applied, "skipped": skipped}
