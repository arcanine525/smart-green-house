"""Backend-owned connection/settings composition; no implicit database fallback."""
from dataclasses import dataclass
import os
from pathlib import Path

from greenhouse.clickhouse.client import Client, ClickHouseError, ClickHouseSettings
from greenhouse.settings import Settings


@dataclass(frozen=True)
class ClickHousePaths:
    raw_dir: Path
    database: ClickHouseSettings


def backend():
    selected=os.environ.get('GREENHOUSE_BACKEND','clickhouse')
    if selected not in ('postgres','clickhouse'):
        raise ValueError('GREENHOUSE_BACKEND must be postgres or clickhouse')
    return selected


def settings():
    if backend()=='postgres':
        return Settings.from_env()
    path=Path(os.environ.get('GREENHOUSE_RAW_DIR','/data/raw'))
    if not path.is_absolute():
        raise ValueError('Raw path in container must be absolute')
    return ClickHousePaths(path,ClickHouseSettings.from_env())


def connect(configuration):
    if isinstance(configuration,ClickHousePaths):
        client=Client(configuration.database)
        client.ping()
        return client.begin_action(shared_proofs=True)
    import psycopg
    return psycopg.connect(**configuration.connection_kwargs())


def is_clickhouse(connection):
    return isinstance(connection,Client)


def identity(connection):
    if is_clickhouse(connection):
        s=connection.settings
        return ('clickhouse',s.host,str(s.port),s.database,s.user)
    info=connection.info
    return ('postgres',*(str(v) for v in (info.host,info.port,info.dbname,info.user)))
