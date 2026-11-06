"""Create and restore a small real sensor state; used only by isolated harness."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
from uuid import uuid4

import psycopg
from pyspark.sql import SparkSession

from greenhouse.db.migrate import migrate
from greenhouse.sensor.collector import BatchManager, DurableSpool
from greenhouse.sensor.outbox import DurableOutbox
from greenhouse.sensor.replay_publication import read_publication
from greenhouse.sensor.simulator import SensorSimulator
from greenhouse.sensor.stream_landing import start_landing
from greenhouse.settings import Settings
from unit.test_sensor_simulator import create_synthetic_climate_csv

ROOT = Path('/state')


def connection():
    return psycopg.connect(**Settings.from_env().connection_kwargs(), autocommit=True)


def stream(metadata):
    options = {key: metadata[key] for key in
               ('replay_id', 'scope_id', 'cycle_id', 'compartment_id', 'quality_policy_version')}
    query = start_landing(Settings.from_env(), ROOT / 'ready', ROOT / 'checkpoint',
                          metadata['stream_id'], 'v1', available_now=True, publication_options=options)
    assert query.awaitTermination(120)
    progress = [(item['batchId'], item['numInputRows']) for item in query.recentProgress
                if item['numInputRows'] > 0]
    active = SparkSession.getActiveSession()
    if active:
        active.stop()
    return progress


def seed():
    with connection() as conn:
        migrate(conn)
    raw_dir = ROOT / 'raw'
    (raw_dir / 'AICU').mkdir(parents=True)
    source = create_synthetic_climate_csv(raw_dir / 'AICU' / 'GreenhouseClimate.csv', num_rows=2)
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot = 'synthetic-backup-' + checksum[:16]
    (raw_dir / 'manifest.json').write_text(json.dumps({'snapshot_id': snapshot,
        'archive': {'sha256': checksum}, 'files': [{'path': 'AICU/GreenhouseClimate.csv',
                                                     'sha256': checksum}]}))
    outbox = DurableOutbox(ROOT / 'outbox')
    # Hold read snapshots so committed writes remain in WAL. Exiting via
    # os._exit simulates an interrupted process without SQLite clean shutdown.
    outbox_reader = sqlite3.connect(outbox.db_path)
    outbox_reader.execute('BEGIN')
    outbox_reader.execute('SELECT count(*) FROM telemetry_messages').fetchone()
    replay_id = 'backup-' + uuid4().hex[:12]
    simulator = SensorSimulator(source, replay_id, ROOT / 'outbox', cursor_interval=1)
    result = simulator.run()
    assert result.expected_unique_events == 2
    spool = DurableSpool(ROOT / 'spool')
    spool_reader = sqlite3.connect(spool.db_path)
    spool_reader.execute('BEGIN')
    spool_reader.execute('SELECT count(*) FROM spool_messages').fetchone()
    with sqlite3.connect(outbox.db_path) as db:
        for table, key in (('telemetry_messages', 'id'), ('control_messages', 'control_sequence')):
            for row_id, topic, payload in db.execute(f'SELECT {key},topic,payload_bytes FROM {table} ORDER BY {key}'):
                spool.record_delivery(topic, payload, '2026-01-01T00:00:00+00:00', outbox_table=table,
                                      outbox_row_id=row_id, replay_id=replay_id,
                                      compartment_id=simulator.compartment_id)
    packed_dir = ROOT / 'packed'
    packed = BatchManager(spool, packed_dir).pack_batch()
    lines = (packed_dir / packed.file_name).read_text().splitlines()
    assert len(lines) >= 4
    ready = ROOT / 'ready'
    ready.mkdir()
    (ready / 'batch-0001.jsonl').write_text(lines[0] + '\n')
    (ROOT / 'pending.json').write_text(json.dumps(lines[1:]))
    metadata = {'replay_id': replay_id, 'scope_id': simulator.data_scope_id,
                'cycle_id': simulator.cycle_id, 'compartment_id': simulator.compartment_id,
                'quality_policy_version': 'AGC_QUALITY_V1', 'stream_id': replay_id,
                'total_deliveries': len(lines), 'source_sha256': checksum}
    assert stream(metadata) == [(0, 1)]
    with connection() as conn:
        assert conn.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0] == 1
    for path in (ROOT / 'outbox' / 'outbox.db-wal', ROOT / 'outbox' / 'outbox.db-shm',
                 ROOT / 'spool' / 'spool.db-wal', ROOT / 'spool' / 'spool.db-shm'):
        assert path.is_file() and path.stat().st_size > 0, str(path)
    (ROOT / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print('Seeded actual outbox, spool, WAL/SHM, first Spark batch and checkpoint', flush=True)
    os._exit(0)


def restore():
    metadata = json.loads((ROOT / 'metadata.json').read_text())
    raw = ROOT / 'raw' / 'AICU' / 'GreenhouseClimate.csv'
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == metadata['source_sha256']
    manifest = json.loads((ROOT / 'raw' / 'manifest.json').read_text())
    assert manifest['files'][0]['sha256'] == metadata['source_sha256']
    # The main SQLite files alone must lack the committed rows, proving that
    # restoration depends on the archived WAL rather than just the base file.
    with tempfile.TemporaryDirectory() as directory:
        for name, path, table in (
            ('outbox', ROOT / 'outbox' / 'outbox.db', 'telemetry_messages'),
            ('spool', ROOT / 'spool' / 'spool.db', 'spool_messages'),
        ):
            assert Path(str(path) + '-wal').stat().st_size > 0
            assert Path(str(path) + '-shm').stat().st_size > 0
            isolated = Path(directory) / f'{name}.db'
            shutil.copyfile(path, isolated)
            with sqlite3.connect(f'file:{isolated}?immutable=1', uri=True) as base:
                assert base.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    with sqlite3.connect(ROOT / 'outbox' / 'outbox.db') as outbox:
        assert outbox.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert outbox.execute('SELECT count(*) FROM telemetry_messages').fetchone()[0] == 2
    with sqlite3.connect(ROOT / 'spool' / 'spool.db') as spool:
        assert spool.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert spool.execute('SELECT count(*) FROM spool_messages').fetchone()[0] == metadata['total_deliveries']
    with connection() as conn:
        assert conn.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0] == 1
    lines = json.loads((ROOT / 'pending.json').read_text())
    (ROOT / 'ready' / 'batch-0002.jsonl').write_text('\n'.join(lines) + '\n')
    progress = stream(metadata)
    assert progress == [(1, len(lines))], progress
    with connection() as conn:
        deliveries = conn.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0]
        batches = conn.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0]
        publication = conn.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s',
                                   (metadata['replay_id'],)).fetchone()[0]
        published, events = read_publication(conn, publication)
        assert deliveries == metadata['total_deliveries'] and batches == 2
        assert len(events) == 2 and len({event['event_id'] for event in events}) == 2
        assert published[-1]['complete'] and published[-1]['checksum_verified']
    result = {'synthetic': True, 'replay_id': metadata['replay_id'], 'source_sha256': metadata['source_sha256'],
              'deliveries': deliveries, 'batches': batches, 'unique_events': len(events),
              'complete': True, 'checksum_verified': True,
              'sqlite_wal_recovered': ['outbox', 'spool'], 'base_rows_without_wal': 0,
              'spark_checkpoint_resumed': True, 'restored_spark_progress': progress}
    (ROOT / 'restore-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('seed', 'restore'))
    args = parser.parse_args()
    seed() if args.phase == 'seed' else restore()
