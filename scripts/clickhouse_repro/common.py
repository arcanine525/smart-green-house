"""Fence rehearsal writes to an explicitly identified disposable deployment."""
import json
import os
import re
from pathlib import Path

from greenhouse.clickhouse.client import Client, ClickHouseSettings

STATE = Path('/state')
COORDINATOR = STATE / 'coordinator'


def connect(*, action=False):
    settings = ClickHouseSettings.from_env()
    identity = json.loads((STATE / 'identity.json').read_text())
    project = os.environ.get('GREENHOUSE_REHEARSAL_PROJECT', '')
    base = identity['project']
    if not re.fullmatch(r'gh-ch-repro-[a-z0-9-]+', base):
        raise ValueError('Disposable rehearsal identity required')
    if project not in (base, base + '-restored', base + '-rebuilt'):
        raise ValueError('Deployment does not match the rehearsal identity')
    if (settings.host, settings.database, settings.user) != (
        'clickhouse', 'greenhouse_repro', 'greenhouse_repro'
    ) or identity['database'] != settings.database:
        raise ValueError('Private rehearsal endpoint/database/user required')
    client = Client(settings)
    return client.begin_action(shared_proofs=True) if action else client


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def historical_id():
    return json.loads((STATE / 'historical.json').read_text())['publication_id']
