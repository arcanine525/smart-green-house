"""External fault harness: orchestrator restarts only dedicated test containers.

Run with GREENHOUSE_RECOVERY_CONTROL set to a shared writable directory.
The host orchestrator reacts to marker files; no Docker socket enters the job.
"""
import json
import os
from pathlib import Path
import time

import psycopg
import pytest
from pyspark.sql import SparkSession

from greenhouse.sensor.replay_publication import read_publication
from greenhouse.sensor.stream_landing import start_landing
from integration.test_sensor_live_replay import local_database
from unit.test_sensor_replay_state import example_messages, rows

CONTROL = os.environ.get('GREENHOUSE_RECOVERY_CONTROL')
pytestmark = pytest.mark.skipif(not CONTROL, reason='requires isolated external restart orchestrator')


def signal(name):
    (Path(CONTROL) / name).write_text('ready\n')


def await_signal(name, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (Path(CONTROL) / name).exists():
            return
        time.sleep(0.1)
    raise TimeoutError(f'external restart signal not received: {name}')


def test_postgresql_restart_replays_uncommitted_spark_batch(local_database, tmp_path):
    connection, target = local_database
    records = rows(tmp_path / 'fixture', example_messages())
    ready = tmp_path / 'input'
    ready.mkdir()
    (ready / 'batch-0001.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records[:2]))
    class Target:
        def connection_kwargs(self):
            return target | {'connect_timeout': 2}
    query = None
    try:
        query = start_landing(Target(), ready, tmp_path / 'checkpoint', 'db-restart', 'v1', available_now=True)
        assert query.awaitTermination(120)
        assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 2
        signal('db_ready_to_stop')
        await_signal('db_stopped')
        (ready / 'batch-0002.jsonl').write_text(json.dumps(records[2]) + '\n')
        query = start_landing(Target(), ready, tmp_path / 'checkpoint', 'db-restart', 'v1', available_now=True)
        with pytest.raises(Exception):
            query.awaitTermination(60)
        signal('db_failure_observed')
        await_signal('db_restarted')
        with psycopg.connect(**target, autocommit=True) as recovered:
            query = start_landing(Target(), ready, tmp_path / 'checkpoint', 'db-restart', 'v1', available_now=True)
            assert query.awaitTermination(120)
            assert recovered.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 3
            assert recovered.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0] == 2
            assert recovered.execute('SELECT count(*) FROM sensor_stream.event_variant').fetchone()[0] == 3
        (Path(CONTROL) / 'db_recovery_passed').write_text(json.dumps({
            'deliveries_before_restart': 2, 'deliveries_after_restart': 3,
            'processed_batches_after_restart': 2, 'variants_after_restart': 3,
            'failed_batch_replayed_from_checkpoint': True}) + '\n')
    finally:
        if query is not None and query.isActive:
            query.stop()
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()


def test_broker_collector_and_simulator_restart(local_database, tmp_path):
    import threading
    from uuid import uuid4
    from greenhouse.sensor.collector import MqttCollector
    from greenhouse.sensor.publisher import OutboxPublisher
    from greenhouse.sensor.simulator import SensorSimulator
    from unit.test_sensor_simulator import create_synthetic_climate_csv

    connection, target = local_database
    replay = 'restart-' + uuid4().hex[:12]
    source = create_synthetic_climate_csv(tmp_path / 'climate.csv', num_rows=6)
    simulator = SensorSimulator(source, replay, tmp_path / 'outbox', cursor_interval=2, pause_after=3)
    first = simulator.run()
    assert first.status == 'paused' and first.expected_unique_events == 3
    resumed = SensorSimulator(source, replay, tmp_path / 'outbox', cursor_interval=2)
    result = resumed.run(resume=True)
    assert result.status == 'completed' and result.expected_unique_events == 6
    ready = tmp_path / 'ready'
    client_id = 'restart-collector-' + replay
    errors = []
    collector = None
    publisher = None
    worker = None
    query = None
    def start_collector():
        instance = MqttCollector(spool_dir=tmp_path / 'spool', ready_dir=ready,
            mqtt_host=os.environ['MQTT_HOST'], client_id=client_id, clean_session=False, batch_size=4, batch_timeout=0.1)
        def run():
            try:
                instance.start(poll_interval=0.05)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (instance._client and instance._client.is_connected()):
            time.sleep(0.05)
        assert instance._client and instance._client.is_connected() and not errors
        time.sleep(0.5)
        return instance, thread
    try:
        collector, worker = start_collector()
        publisher = OutboxPublisher(outbox_dir=tmp_path / 'outbox', mqtt_host=os.environ['MQTT_HOST'],
                                    client_id='restart-publisher-' + replay, max_inflight=20)
        publisher.start()
        first_three = publisher.fetch_unconfirmed_telemetry(limit=3)
        assert len(first_three) == 3
        for item in first_three:
            assert publisher.publish_single_item(item)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(publisher.is_row_confirmed(i.outbox_table, i.outbox_row_id) for i in first_three):
            time.sleep(0.05)
        assert all(publisher.is_row_confirmed(i.outbox_table, i.outbox_row_id) for i in first_three)
        collector.stop()
        worker.join(timeout=5)
        assert not worker.is_alive()
        # Send pending rows while the persistent collector session is offline.
        pending = publisher.fetch_unconfirmed_telemetry(limit=20) + publisher.fetch_unconfirmed_controls(limit=20)
        assert pending
        for item in pending:
            assert publisher.publish_single_item(item)
        publisher.stop()
        signal('broker_ready_to_restart')
        await_signal('broker_restarted')
        collector, worker = start_collector()
        # Verify messages queued while the collector was offline survived the
        # broker restart BEFORE the publisher is allowed to retry anything.
        queued = {(item.outbox_table, item.outbox_row_id) for item in pending}
        deadline = time.monotonic() + 15
        received = set()
        while time.monotonic() < deadline:
            with collector.spool._connection() as spool:
                received = set(spool.execute('SELECT outbox_table,outbox_row_id FROM spool_messages').fetchall())
            if queued <= received:
                break
            time.sleep(0.1)
        assert queued <= received, f'broker lost queued rows: {queued - received}'
        # A fresh publisher retries unconfirmed outbox rows after broker restart;
        # queued/redelivered messages may also arrive and must not be lost.
        publisher = OutboxPublisher(outbox_dir=tmp_path / 'outbox', mqtt_host=os.environ['MQTT_HOST'],
                                    client_id='restart-publisher-' + replay, max_inflight=20)
        publisher.start()
        publisher.publish_all_pending(poll_interval=0.05)
        publisher.stop()
        collector.stop()
        worker.join(timeout=5)
        assert not worker.is_alive() and not errors
        class Target:
            def connection_kwargs(self):
                return target
        options = dict(replay_id=replay, scope_id=resumed.data_scope_id, cycle_id=resumed.cycle_id,
                       compartment_id=resumed.compartment_id, quality_policy_version='AGC_QUALITY_V1')
        query = start_landing(Target(), ready, tmp_path / 'checkpoint', 'broker-restart-' + replay, 'v1',
                              available_now=True, publication_options=options)
        assert query.awaitTermination(120)
        publication = connection.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s', (replay,)).fetchone()[0]
        meta, events = read_publication(connection, publication)
        assert meta[-1]['complete'] and meta[-1]['checksum_verified']
        assert len(events) == 6 and len({event['event_id'] for event in events}) == 6
        (Path(CONTROL) / 'broker_recovery_passed').write_text(json.dumps({
            'replay_id': replay, 'unique_events': len(events), 'deliveries': collector.spool.count_spooled_records()[0],
            'checksum_verified': meta[-1]['checksum_verified'], 'simulator_pause_resume': True,
            'collector_restart': True, 'broker_restart': True,
            'queued_rows_verified_before_publisher_retry': len(queued)}) + '\n')
    finally:
        if publisher is not None:
            publisher.stop()
        if collector is not None:
            collector.stop()
        if worker is not None:
            worker.join(timeout=5)
        if query is not None and query.isActive:
            query.stop()
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()
