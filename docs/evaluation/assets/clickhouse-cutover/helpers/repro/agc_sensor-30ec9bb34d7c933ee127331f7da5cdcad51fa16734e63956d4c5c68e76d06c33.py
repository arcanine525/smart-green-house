"""Restore a real AGC replay: MQTT queue, SQLite WAL, Spark checkpoint, publication.

Only run in the uniquely named rehearsal projects. The historical DB is read
by verification tools; this harness writes exclusively to sensor replay scope.
"""
import hashlib
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import threading
import time
from uuid import uuid4

from common import COORDINATOR, STATE, connect, historical_id, write_json
from greenhouse.clickhouse.stream_store import StreamSettings
from greenhouse.clickhouse.replay_config import from_historical
from greenhouse.clickhouse.replay_publication import read_publication
from greenhouse.clickhouse.replay_evaluation import build_replay_rows
from greenhouse.app import wiring
from pyspark.sql import SparkSession
from greenhouse.sensor.simulator import STANDARD_AGC_SHA as AGC_SHA
from greenhouse.sensor.collector import MqttCollector, DurableSpool
from greenhouse.sensor.outbox import DurableOutbox
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.simulator import SensorSimulator
from greenhouse.sensor.stream_landing import start_landing


ROOT = STATE/'sensor'
BROKER = os.environ.get('MQTT_HOST', 'ch-broker')


def wait_until(check, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.1)
    raise AssertionError('Timed out waiting for the expected replay state')


def start_collector(metadata):
    instance = MqttCollector(spool_dir=ROOT/'spool', ready_dir=ROOT/'ready',
        mqtt_host=BROKER, client_id=metadata['collector_id'], clean_session=False,
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


def stream(metadata, *, rebuild=False):
    client=connect()
    settings=StreamSettings(client.settings, COORDINATOR, metadata['config_version'])
    client.close()
    options = {k: metadata[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')}
    query = start_landing(settings, ROOT/'ready', ROOT/('checkpoint-rebuilt' if rebuild else 'checkpoint'),
        metadata['stream_id'], 'raw-rebuild-v1' if rebuild else 'v1', available_now=True, publication_options=options)
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
    ROOT.mkdir(parents=True,exist_ok=True)
    client=connect(action=True)
    pub=wiring.energy_service(client).adapter.publication(historical_id())
    cycle=next(c for c in wiring.catalog_service(client).cycles(pub) if c.compartment_id=='Automatoes')
    source = Path(os.environ['GREENHOUSE_RAW_DIR'])/'agc-2nd-2019'/AGC_SHA/'Automatoes/GreenhouseClimate.csv'
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
        cursor_interval=5, limit=30, cycle_id=cycle.cycle_id, snapshot_id=pub.snapshot_id)
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
    metadata['config_version']=replay+'-v1'
    config,pin=from_historical(client,COORDINATOR,pub.publication_id,replay_id=replay,
        scope_id=simulator.data_scope_id,compartment_id='Automatoes',config_version=metadata['config_version'])
    client.close()
    metadata['config']=config
    metadata['config_hash']=pin['content_hash']
    collector, worker, errors = start_collector(metadata)
    publisher = OutboxPublisher(outbox_dir=ROOT/'outbox', mqtt_host=BROKER,
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
    # Export genuine native user state while keeping the WAL readers open.
    from argparse import Namespace
    from native_state import export
    export(Namespace(proof=STATE/'parity/parity.json',raw=source.parent.parent,archive=STATE/'native-state.json'))
    # All MQTT and Spark workers stopped. Preserve WAL/SHM like an interrupted process.
    os._exit(0)


def restore():
    metadata = json.loads((ROOT/'metadata.json').read_text())
    source = Path(os.environ['GREENHOUSE_RAW_DIR'])/'agc-2nd-2019'/AGC_SHA/'Automatoes/GreenhouseClimate.csv'
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
    publisher = OutboxPublisher(outbox_dir=ROOT/'outbox', mqtt_host=BROKER,
        client_id=metadata['publisher_id'], max_inflight=50)
    publisher.start()
    publisher.publish_all_pending(poll_interval=.05)
    publisher.stop()
    stop_collector(collector,worker,errors)
    progress = stream(metadata)
    assert progress and min(p[0] for p in progress)>max(p[0] for p in metadata['seed_progress'])
    state,events,pub,deliveries,batches,fingerprint = verify(metadata)
    result = dict(real_agc=True,scope='30 Automatoes source rows through MQTT and Spark',
        source_sha256=metadata['source_sha256'],replay_id=metadata['replay_id'],publication_id=pub,
        unique_events=len(events),deliveries=deliveries,batches=batches,complete=True,checksum_verified=True,
        business_fingerprint=fingerprint,original_envelopes_exact=True,queued_rows_received_before_retry=len(queued),
        wal_integrity=['outbox','spool'],base_rows_without_wal=0,seed_progress=metadata['seed_progress'],restored_progress=progress)
    (ROOT/'restore-result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)


def verify(metadata):
    import sys
    sys.path.insert(0,'/migration')
    from read_business_reference import fingerprint
    with closing(connect()) as client:
        pointer=client.query('SELECT DISTINCT publication_id,generation FROM replay_pointer WHERE scope_id={scope:String} ORDER BY generation DESC',{'scope':metadata['scope_id']})
        assert pointer, 'Replay pointer missing'
        pub=pointer[0]['publication_id']
        state,events=read_publication(client,pub)
        assert state[-1]['complete'] and state[-1]['checksum_verified'],state[-1]
        assert not state[-1]['missing_sequences'] and not state[-1]['quarantined']
        assert len(events)==30 and len({e['event_id'] for e in events})==30
        assert {e['event_id']:e for e in events}=={e['event_id']:e for e in metadata['original_envelopes']}
        deliveries=int(client.query('SELECT count() AS n FROM stream_delivery')[0]['n'])
        batches=int(client.query('SELECT count() AS n FROM stream_batch')[0]['n'])
        rows=build_replay_rows(events,metadata['config'])
        business_hash=fingerprint(rows)
        assert business_hash=='7783fdfa2f63909171709dea99b43bb5e6956af13fde3dca15ce4d1030d1f675'
        return state,events,pub,deliveries,batches,business_hash


def rebuild():
    metadata=json.loads((ROOT/'metadata.json').read_text())
    with closing(connect(action=True)) as client:
        config,pin=from_historical(client,COORDINATOR,historical_id(),replay_id=metadata['replay_id'],
            scope_id=metadata['scope_id'],compartment_id='Automatoes',config_version=metadata['config_version'])
    original_config=metadata['config']
    assert {k:v for k,v in original_config.items() if k not in ('source_publication_id','source_config_hash')}=={
        k:v for k,v in config.items() if k not in ('source_publication_id','source_config_hash')}
    metadata['config']=config
    assert not (ROOT/'checkpoint-rebuilt').exists(), 'Fresh DB requires a NEW checkpoint'
    progress=stream(metadata,rebuild=True)
    state,events,pub,deliveries,batches,fingerprint=verify(metadata)
    result=dict(status='passed',backend='clickhouse',mode='raw-ready-files-new-checkpoint',
        replay_id=metadata['replay_id'],publication_id=pub,unique_events=len(events),
        original_envelopes_exact=True,business_fingerprint=fingerprint,
        complete=state[-1]['complete'],checksum_verified=state[-1]['checksum_verified'],
        progress=progress,old_checkpoint_used=False,deliveries=deliveries,batches=batches,
        original_config=original_config,rebuilt_config=config)
    write_json(ROOT/'rebuild-result.json',result)
    print(json.dumps({k:result[k] for k in ('status','mode','unique_events','business_fingerprint')}),flush=True)


if __name__ == '__main__':
    import sys
    {'seed':seed,'restore':restore,'rebuild':rebuild}[sys.argv[1]]()
