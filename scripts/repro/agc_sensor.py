"""Restore a real AGC replay: MQTT queue, SQLite WAL, Spark checkpoint, publication.

Only run in the uniquely named rehearsal projects. The historical DB is read
by verification tools; this harness writes exclusively to sensor replay scope.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

import psycopg
from pyspark.sql import SparkSession
from greenhouse.db.loader import AGC_SHA
from greenhouse.settings import Settings
from greenhouse.sensor.collector import MqttCollector, DurableSpool
from greenhouse.sensor.outbox import DurableOutbox
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.simulator import SensorSimulator
from greenhouse.sensor.stream_landing import start_landing
from greenhouse.sensor.replay_publication import read_publication

ROOT = Path('/state')


def wait_until(check, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.1)
    raise AssertionError('Timed out waiting for the expected replay state')


def start_collector(metadata):
    instance = MqttCollector(spool_dir=ROOT/'spool', ready_dir=ROOT/'ready',
        mqtt_host='broker', client_id=metadata['collector_id'], clean_session=False,
        batch_size=5, batch_timeout=.2)
    errors = []
    def run():
        try:
            instance.start(poll_interval=.05)
        except Exception as error:
            errors.append(str(error))
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    wait_until(lambda: instance._client and instance._client.is_connected())
    # Allow the broker to acknowledge the persistent subscription.
    time.sleep(.5)
    assert not errors, errors
    return instance, worker, errors


def stop_collector(collector, worker, errors):
    collector.stop()
    worker.join(timeout=10)
    assert not worker.is_alive() and not errors, errors


def stream(metadata):
    options = {k: metadata[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')}
    query = start_landing(Settings.from_env(), ROOT/'ready', ROOT/'checkpoint',
        metadata['stream_id'], 'v1', available_now=True, publication_options=options)
    try:
        assert query.awaitTermination(180)
        progress = [(x['batchId'], x['numInputRows']) for x in query.recentProgress if x['numInputRows']]
    finally:
        if query.isActive:
            query.stop()
        active = SparkSession.getActiveSession()
        if active:
            active.stop()
    return progress


def seed():
    source = Settings.from_env().raw_dir/'agc-2nd-2019'/AGC_SHA/'Automatoes/GreenhouseClimate.csv'
    manifest = json.loads((source.parent.parent/'manifest.json').read_text())
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    assert next(f['sha256'] for f in manifest['files'] if f['path']=='Automatoes/GreenhouseClimate.csv') == checksum
    # Keep committed SQLite state in WAL to verify that the backup includes it.
    outbox = DurableOutbox(ROOT/'outbox')
    reader_out = sqlite3.connect(outbox.db_path)
    reader_out.execute('BEGIN')
    reader_out.execute('SELECT count(*) FROM telemetry_messages').fetchone()
    replay = 'agc-restore-'+uuid4().hex[:12]
    simulator = SensorSimulator(source, replay, ROOT/'outbox', compartment_id='Automatoes',
        cursor_interval=5, limit=30)
    simulation = simulator.run()
    assert simulation.expected_unique_events == 30
    spool = DurableSpool(ROOT/'spool')
    reader_spool = sqlite3.connect(spool.db_path)
    reader_spool.execute('BEGIN')
    reader_spool.execute('SELECT count(*) FROM spool_messages').fetchone()
    metadata = dict(replay_id=replay, scope_id=simulator.data_scope_id, cycle_id=simulator.cycle_id,
        compartment_id='Automatoes', quality_policy_version='AGC_QUALITY_V1', stream_id=replay,
        collector_id='collector-'+replay, publisher_id='publisher-'+replay,
        source_sha256=checksum, unique_events=30)
    collector, worker, errors = start_collector(metadata)
    publisher = OutboxPublisher(outbox_dir=ROOT/'outbox', mqtt_host='broker',
        client_id=metadata['publisher_id'], max_inflight=50)
    publisher.start()
    first = publisher.fetch_unconfirmed_telemetry(limit=10)
    assert len(first) == 10
    for item in first:
        assert publisher.publish_single_item(item)
    wait_until(lambda: all(publisher.is_row_confirmed(i.outbox_table,i.outbox_row_id) for i in first))
    stop_collector(collector,worker,errors)
    metadata['seed_progress'] = stream(metadata)
    assert metadata['seed_progress']
    pending = publisher.fetch_unconfirmed_telemetry(limit=100)+publisher.fetch_unconfirmed_controls(limit=100)
    assert len(pending) > 20
    for item in pending:
        assert publisher.publish_single_item(item)
    publisher.stop()
    metadata['queued_rows'] = [(i.outbox_table,i.outbox_row_id) for i in pending]
    with sqlite3.connect(outbox.db_path) as db:
        metadata['original_envelopes'] = [json.loads(row[0]) for row in db.execute('SELECT payload_bytes FROM telemetry_messages ORDER BY id')]
    for db in (outbox.db_path,spool.db_path):
        for suffix in ('-wal','-shm'):
            assert Path(str(db)+suffix).stat().st_size>0
    (ROOT/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    print(json.dumps({'seeded':True,'real_agc':True,'events':30,'queued_rows':len(pending),'spark_progress':metadata['seed_progress']}),flush=True)
    # All MQTT and Spark workers stopped. Preserve WAL/SHM like an interrupted process.
    os._exit(0)


def restore():
    metadata = json.loads((ROOT/'metadata.json').read_text())
    source = Settings.from_env().raw_dir/'agc-2nd-2019'/AGC_SHA/'Automatoes/GreenhouseClimate.csv'
    assert hashlib.sha256(source.read_bytes()).hexdigest()==metadata['source_sha256']
    for name,table in (('outbox','telemetry_messages'),('spool','spool_messages')):
        path = ROOT/name/(name+'.db')
        assert Path(str(path)+'-wal').is_file() and Path(str(path)+'-shm').is_file()
        with sqlite3.connect(f'file:{path}?immutable=1', uri=True) as base:
            assert base.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        with sqlite3.connect(path) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            count = db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            assert count>=10
    collector, worker, errors = start_collector(metadata)
    queued = {tuple(row) for row in metadata['queued_rows']}
    def received_queue():
        with collector.spool._connection() as db:
            return queued <= set(db.execute('SELECT outbox_table,outbox_row_id FROM spool_messages').fetchall())
    # No publisher exists yet: these rows can only come from the restored broker queue.
    wait_until(received_queue)
    publisher = OutboxPublisher(outbox_dir=ROOT/'outbox', mqtt_host='broker',
        client_id=metadata['publisher_id'], max_inflight=50)
    publisher.start()
    publisher.publish_all_pending(poll_interval=.05)
    publisher.stop()
    stop_collector(collector,worker,errors)
    progress = stream(metadata)
    assert progress and min(p[0] for p in progress)>max(p[0] for p in metadata['seed_progress'])
    with psycopg.connect(**Settings.from_env().connection_kwargs()) as conn:
        pub = conn.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s',(metadata['replay_id'],)).fetchone()[0]
        state,events = read_publication(conn,pub)
        assert state[-1]['complete'] and state[-1]['checksum_verified'],state[-1]
        assert len(events)==30 and len({e['event_id'] for e in events})==30
        assert {e['event_id']:e for e in events} == {e['event_id']:e for e in metadata['original_envelopes']}
        deliveries = conn.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0]
        batches = conn.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0]
    result = dict(real_agc=True,scope='30 Automatoes source rows through MQTT and Spark',
        source_sha256=metadata['source_sha256'],replay_id=metadata['replay_id'],publication_id=pub,
        unique_events=len(events),deliveries=deliveries,batches=batches,complete=True,checksum_verified=True,
        original_envelopes_exact=True,queued_rows_received_before_retry=len(queued),
        wal_integrity=['outbox','spool'],base_rows_without_wal=0,seed_progress=metadata['seed_progress'],restored_progress=progress)
    (ROOT/'restore-result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)


if __name__ == '__main__':
    import sys
    {'seed':seed,'restore':restore}[sys.argv[1]]()
