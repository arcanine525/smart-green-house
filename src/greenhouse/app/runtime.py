"""Backend-owned connection/settings composition; no implicit database fallback."""
from dataclasses import dataclass
import os
from pathlib import Path

from greenhouse.clickhouse.client import Client, ClickHouseError, ClickHouseSettings
from greenhouse.settings import Settings


@dataclass(frozen=True)
class ClickHousePaths:
    raw_dir: Path


def backend():
    selected=os.environ.get('GREENHOUSE_BACKEND','postgres')
    if selected not in ('postgres','clickhouse'):
        raise ValueError('GREENHOUSE_BACKEND must be postgres or clickhouse')
    return selected


def settings():
    if backend()=='postgres':
        return Settings.from_env()
    path=Path(os.environ.get('GREENHOUSE_RAW_DIR','/data/raw'))
    if not path.is_absolute():
        raise ValueError('Raw path in container must be absolute')
    return ClickHousePaths(path)


def connect(configuration):
    if backend()=='clickhouse':
        client=Client(ClickHouseSettings.from_env())
        client.ping()
        return client
    import psycopg
    return psycopg.connect(**configuration.connection_kwargs())


def is_clickhouse(connection):
    return isinstance(connection,Client)
