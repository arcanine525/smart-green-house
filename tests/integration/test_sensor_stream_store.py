"""Transaction and retry tests against an isolated PostgreSQL database."""
import hashlib
import json
import os
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
import pytest

from greenhouse.sensor.collector import BatchManager, DurableSpool
from greenhouse.sensor.stream_store import commit_batch
from unit.test_sensor_collector import make_valid_telemetry_payload


@pytest.fixture
def connection():
    name = 'test_sensor_stream_' + uuid.uuid4().hex[:12]
    args = dict(host=os.environ['PGHOST'], port=os.environ.get('PGPORT', '5432'), user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'])
    with psycopg.connect(**args, dbname=os.environ['PGDATABASE'], autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            with psycopg.connect(**args, dbname=name, autocommit=True) as conn:
                conn.execute(Path('/workspace/src/greenhouse/db/migrations/016_sensor_stream.sql').read_text())
                conn.execute('CREATE SCHEMA core')
                conn.execute('CREATE TABLE core.growing_cycle(cycle_id text, compartment_id text, start_source_time timestamp, end_source_time_exclusive timestamp)')
                conn.execute("INSERT INTO core.growing_cycle VALUES ('tomatoes-2019','AICU','2019-12-16','2019-12-18'), ('agc-cycle-AICU','AICU','2019-12-16','2020-05-30')")
                yield conn
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def record(tmp_path, raw):
    spool = DurableSpool(tmp_path / 'spool')
    spool.record_delivery('v1/telemetry/replay-101/AICU', raw, '2026-11-06T10:01:00Z')
    ready = tmp_path / 'ready'
    batch = BatchManager(spool, ready).pack_batch()
    return json.loads((ready / batch.file_name).read_text())


def test_retry_conflict_and_checkpoint_reset(connection, tmp_path):
    first_raw = make_valid_telemetry_payload()
    first = record(tmp_path / 'one', first_raw)
    changed = json.loads(first_raw)
    changed['payload']['actual_measurements']['Tair'] = '99.9'
    other = record(tmp_path / 'two', json.dumps(changed, sort_keys=True).encode())
    assert commit_batch(connection, 'stream', 'generation-1', 0, [first, first])
    assert not commit_batch(connection, 'stream', 'generation-1', 0, [first, first])
    with pytest.raises(ValueError, match='different content'):
        commit_batch(connection, 'stream', 'generation-1', 0, [other])
    assert commit_batch(connection, 'stream', 'generation-2', 0, [first, other])
    assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 4
    assert connection.execute('SELECT count(*) FROM sensor_stream.event_variant').fetchone()[0] == 2
    assert connection.execute('SELECT count(DISTINCT identity) FROM sensor_stream.event_variant').fetchone()[0] == 1
    assert connection.execute('SELECT count(*) FROM sensor_stream.uncontested_event').fetchone()[0] == 0


def test_failed_batch_rolls_back_marker(connection, tmp_path, monkeypatch):
    first = record(tmp_path / 'one', make_valid_telemetry_payload())
    def fail_after_marker(_row):
        raise RuntimeError('simulated process failure')
    monkeypatch.setattr('greenhouse.sensor.stream_store.decode_ready_record', fail_after_marker)
    with pytest.raises(RuntimeError, match='simulated process failure'):
        commit_batch(connection, 'stream', 'generation', 1, [first])
    assert connection.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0] == 0
    assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 0


def test_spark_file_source_restart_is_idempotent(connection, tmp_path, monkeypatch):
    from greenhouse.sensor.stream_landing import start_landing
    from pyspark.sql import SparkSession

    raw = make_valid_telemetry_payload()
    row = record(tmp_path / 'source', raw)
    ready = tmp_path / 'input'
    ready.mkdir()
    (ready / 'batch-0001.jsonl').write_text(json.dumps(row) + '\n')
    # A manifest is not a batch and must not be read as an event.
    (ready / 'manifest.json').write_text('{}')
    class TestSettings:
        def connection_kwargs(self):
            return {'host': os.environ['PGHOST'], 'port': os.environ.get('PGPORT', '5432'),
                    'user': os.environ['PGUSER'], 'password': os.environ['PGPASSWORD'],
                    'dbname': connection.info.dbname}
    try:
        # Simulate process death after PostgreSQL commit and before Spark checkpoint.
        assert commit_batch(connection, 'spark-test', 'v1', 0, [row])
        query = start_landing(TestSettings(), ready, tmp_path / 'checkpoint', 'spark-test', 'v1', available_now=True)
        assert query.awaitTermination(120)
        assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 1
        query = start_landing(TestSettings(), ready, tmp_path / 'checkpoint', 'spark-test', 'v1', available_now=True)
        assert query.awaitTermination(120)
        assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 1
        with pytest.raises(ValueError, match='checkpoint belongs'):
            start_landing(TestSettings(), ready, tmp_path / 'checkpoint', 'spark-test', 'wrong-generation', available_now=True)
    finally:
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()


def test_state_is_derived_from_committed_rows(connection, tmp_path):
    from greenhouse.sensor.replay_state import load_replay_state
    from unit.test_sensor_replay_state import example_messages, rows

    messages = example_messages()
    deliveries = rows(tmp_path, [messages[2], messages[0], messages[1]])
    assert commit_batch(connection, 'stream', 'generation', 0, deliveries)
    state = load_replay_state(connection, 'replay-101')
    assert state.complete
    assert state.cursor_sequence == 1
    assert not load_replay_state(connection, 'different-replay').complete


def test_replay_slots_use_shared_batch_quality(connection):
    from greenhouse.sensor.replay_slots import build_replay_rows
    from greenhouse.queries.climate import build_slots as climate_slots
    from greenhouse.queries.drain import build_slots as drain_slots
    from greenhouse.contracts import RuleBasis

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute('''INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES
       ('AGC_QUALITY_V1', '{"nonnegative":["EC_drain_PC"],"off_mode_zero":["co2_sp","co2_vip"],"suspect_zero":["pH_drain_PC"]}')''')
    event = json.loads(make_valid_telemetry_payload())
    event['payload']['drain_measurements']['EC_drain_PC'] = '-1'
    event['payload']['drain_measurements']['pH_drain_PC'] = '0'
    event['payload']['controller_context']['co2_sp'] = '0'
    event['units'].update({'EC_drain_PC': 'dS/m', 'pH_drain_PC': '1', 'co2_sp': 'ppm'})
    climate, drain = build_replay_rows(connection, [event], replay_id=event['replay_id'],
        scope_id=event['data_scope_id'], cycle_id=event['cycle_id'],
        compartment_id=event['compartment_id'], quality_policy_version='AGC_QUALITY_V1')
    quality = climate[0].measure_quality
    assert quality['EC_drain_PC'] == {'status': 'invalid', 'applicability': 'applicable', 'reason': 'negative_quantity'}
    assert quality['pH_drain_PC'] == {'status': 'suspect', 'applicability': 'applicable', 'reason': 'demo_zero_ph_suspect'}
    assert quality['co2_sp'] == {'status': 'accepted', 'applicability': 'unknown', 'reason': 'off_mode_unverified'}
    assert len(climate_slots(climate, RuleBasis.REQUESTED)) == 2
    assert len(drain_slots(drain)) == 2
    with pytest.raises(ValueError, match='duplicate source slot'):
        build_replay_rows(connection, [event, event], replay_id=event['replay_id'],
            scope_id=event['data_scope_id'], cycle_id=event['cycle_id'],
            compartment_id=event['compartment_id'], quality_policy_version='AGC_QUALITY_V1')


def test_publication_seals_snapshot_and_late_conflict_advances_pointer(connection, tmp_path):
    from greenhouse.sensor.replay_publication import publish_replay, read_publication
    from unit.test_sensor_replay_state import example_messages, rows, dump

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    messages = example_messages()
    deliveries = rows(tmp_path / 'first', messages)
    event = json.loads(messages[0][2])
    args = dict(replay_id=event['replay_id'], scope_id=event['data_scope_id'], cycle_id=event['cycle_id'],
                compartment_id=event['compartment_id'], quality_policy_version='policy')
    commit_batch(connection, 'stream', 'one', 0, deliveries)
    first = publish_replay(connection, **args)
    assert publish_replay(connection, **args) == first
    metadata, events = read_publication(connection, first)
    assert metadata[-1]['complete'] and len(events) == 1
    mutated = json.loads(messages[0][2])
    mutated['payload']['actual_measurements']['Tair'] = '99'
    late = rows(tmp_path / 'late', [('telemetry_messages', 2, dump(mutated))])
    commit_batch(connection, 'stream', 'one', 1, late)
    second = publish_replay(connection, **args)
    assert first != second
    assert len(read_publication(connection, first)[1]) == 1
    assert len(read_publication(connection, second)[1]) == 0
    assert not read_publication(connection, second)[0][-1]['complete']
    assert connection.execute('SELECT publication_id FROM sensor_stream.current_publication').fetchone()[0] == second
    with pytest.raises(psycopg.errors.RaiseException, match='immutable'):
        connection.execute('UPDATE sensor_stream.event_variant SET envelope=envelope')
    with pytest.raises(psycopg.errors.RaiseException, match='sealed'):
        connection.execute('''INSERT INTO sensor_stream.publication_event
            SELECT %s,replay_id,identity,payload_hash FROM sensor_stream.event_variant LIMIT 1''', (first,))


def test_replay_evaluation_uses_cursor_and_sealed_membership(connection, tmp_path):
    from datetime import datetime
    from greenhouse.sensor.replay_evaluation import evaluate_publication
    from greenhouse.sensor.replay_publication import publish_replay
    from unit.test_sensor_replay_state import example_messages, rows, dump

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    messages = example_messages()
    event = json.loads(messages[0][2])
    args = dict(replay_id=event['replay_id'], scope_id=event['data_scope_id'], cycle_id=event['cycle_id'],
                compartment_id=event['compartment_id'], quality_policy_version='policy')
    commit_batch(connection, 'stream', 'one', 0, rows(tmp_path / 'one', messages))
    first = publish_replay(connection, **args)
    evaluated = evaluate_publication(connection, first)
    assert evaluated.climate.effective_end == datetime(2019, 12, 16, 0, 5)
    assert evaluated.drain.effective_end == datetime(2019, 12, 16, 0, 5)
    assert not evaluated.provisional
    mutated = json.loads(messages[0][2])
    mutated['payload']['actual_measurements']['Tair'] = '99'
    commit_batch(connection, 'stream', 'one', 1, rows(tmp_path / 'two', [('telemetry_messages', 2, dump(mutated))]))
    second = publish_replay(connection, **args)
    assert evaluate_publication(connection, second).provisional
    assert evaluate_publication(connection, first) == evaluated


def test_late_replay_slot_reconnects_episode_with_shared_engine(connection):
    from copy import deepcopy
    from datetime import date, datetime, timedelta
    from decimal import Decimal
    from greenhouse.contracts import CycleWindow, IntradayContext, RuleBasis, RuleScheduleRevision, RuleScheduleSegment
    from greenhouse.sensor.replay_slots import build_replay_rows
    from greenhouse.queries.climate import build_slots
    from greenhouse.queries.rule_duration import evaluate_rule_schedule

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    event = json.loads(make_valid_telemetry_payload())
    event['payload']['actual_measurements']['Tair'] = '25'
    event['payload']['controller_context'].update({'t_heat_sp': '18', 't_vent_sp': '20'})
    event['units'].update({'t_heat_sp': '°C', 't_vent_sp': '°C'})
    slots = []
    for n in range(3):
        item = deepcopy(event)
        item['event_id'] = hashlib.sha256(str(n).encode()).hexdigest()
        item['sequence'] = n + 1
        item['source_provenance']['source_time_serial'] = str(Decimal(43815) + Decimal(n) / Decimal(288))
        item['source_observed_at'] = (datetime(2019, 12, 16) + timedelta(minutes=5*n)).isoformat()
        slots.append(item)
    params = dict(replay_id=event['replay_id'], scope_id=event['data_scope_id'], cycle_id=event['cycle_id'],
                  compartment_id=event['compartment_id'], quality_policy_version='policy')
    cycle = CycleWindow(event['cycle_id'], event['compartment_id'], date(2019, 12, 16), date(2019, 12, 17))
    context = IntradayContext(event['data_scope_id'], cycle, (event['compartment_id'],),
        datetime(2019, 12, 16), datetime(2019, 12, 16, 0, 15), 'climate', RuleBasis.REQUESTED)
    rule = RuleScheduleRevision('test-rule-v1', 'test-rule', 1, event['data_scope_id'], event['compartment_id'],
        'test evidence', True, 'demo_rule', (RuleScheduleSegment('temperature', 'climate', RuleBasis.REQUESTED,
            datetime(2019, 12, 16), datetime(2019, 12, 17), delta=Decimal('1')),))
    def evaluate(events):
        climate, _ = build_replay_rows(connection, events, **params)
        return evaluate_rule_schedule(context, (rule,), build_slots(climate, RuleBasis.REQUESTED), ('temperature', 'co2'))
    before = evaluate([slots[0], slots[2]])
    after = evaluate([slots[0], slots[2], slots[1]])
    assert len(before.episodes) == 2
    assert len(after.episodes) == 1
    assert after.episodes[0].original_minutes == Decimal('15')


def test_spark_landing_commits_publication_with_batch(connection, tmp_path):
    from greenhouse.sensor.stream_landing import start_landing
    from greenhouse.sensor.replay_publication import read_publication
    from unit.test_sensor_replay_state import example_messages, rows
    from pyspark.sql import SparkSession

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    messages = example_messages()
    event = json.loads(messages[0][2])
    options = dict(replay_id=event['replay_id'], scope_id=event['data_scope_id'], cycle_id=event['cycle_id'],
                   compartment_id=event['compartment_id'], quality_policy_version='policy')
    records = rows(tmp_path / 'source', messages)
    ready = tmp_path / 'input'
    ready.mkdir()
    (ready / 'batch-0001.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records))
    class TestSettings:
        def connection_kwargs(self):
            return {'host': os.environ['PGHOST'], 'port': os.environ.get('PGPORT', '5432'),
                    'user': os.environ['PGUSER'], 'password': os.environ['PGPASSWORD'],
                    'dbname': connection.info.dbname}
    try:
        query = start_landing(TestSettings(), ready, tmp_path / 'checkpoint', 'spark-test', 'v1',
                              available_now=True, publication_options=options)
        assert query.awaitTermination(120)
        publication = connection.execute('SELECT publication_id FROM sensor_stream.current_publication').fetchone()[0]
        assert read_publication(connection, publication)[0][-1]['complete']
        assert len(read_publication(connection, publication)[1]) == 1
    finally:
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()


def test_publication_failure_rolls_back_landed_batch(connection, tmp_path):
    from greenhouse.sensor.replay_publication import publish_replay
    from unit.test_sensor_replay_state import example_messages, rows

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    messages = example_messages()
    event = json.loads(messages[0][2])
    deliveries = rows(tmp_path, messages)
    with pytest.raises(ValueError, match='unknown quality policy'):
        with connection.transaction():
            commit_batch(connection, 'stream', 'one', 0, deliveries)
            publish_replay(connection, replay_id=event['replay_id'], scope_id=event['data_scope_id'],
                cycle_id=event['cycle_id'], compartment_id=event['compartment_id'], quality_policy_version='missing')
    assert connection.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0] == 0
    assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 0


def test_simulator_collector_spark_replay_reconciles(connection, tmp_path):
    import sqlite3
    from greenhouse.sensor.simulator import SensorSimulator
    from greenhouse.sensor.stream_landing import start_landing
    from greenhouse.sensor.replay_publication import read_publication
    from unit.test_sensor_simulator import create_synthetic_climate_csv
    from pyspark.sql import SparkSession

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    csv = create_synthetic_climate_csv(tmp_path / 'climate.csv', num_rows=4)
    simulator = SensorSimulator(source_path=csv, replay_id='end-to-end', outbox_dir=tmp_path / 'outbox', cursor_interval=2)
    result = simulator.run()
    assert result.expected_unique_events == 4
    spool = DurableSpool(tmp_path / 'spool')
    with sqlite3.connect(tmp_path / 'outbox' / 'outbox.db') as db:
        # Deliver controls first to exercise out-of-order transport.
        for table, id_column in (('control_messages', 'control_sequence'), ('telemetry_messages', 'id')):
            for row_id, topic, payload in db.execute(f'SELECT {id_column},topic,payload_bytes FROM {table} ORDER BY {id_column}'):
                spool.record_delivery(topic, payload, '2026-11-06T10:01:00+00:00',
                    outbox_table=table, outbox_row_id=row_id, replay_id=simulator.replay_id,
                    compartment_id=simulator.compartment_id)
    ready = tmp_path / 'ready'
    BatchManager(spool, ready).pack_batch()
    options = dict(replay_id=simulator.replay_id, scope_id=simulator.data_scope_id, cycle_id=simulator.cycle_id,
                   compartment_id=simulator.compartment_id, quality_policy_version='policy')
    class TestSettings:
        def connection_kwargs(self):
            return {'host': os.environ['PGHOST'], 'port': os.environ.get('PGPORT', '5432'),
                    'user': os.environ['PGUSER'], 'password': os.environ['PGPASSWORD'], 'dbname': connection.info.dbname}
    try:
        query = start_landing(TestSettings(), ready, tmp_path / 'checkpoint', 'e2e', 'v1',
                              available_now=True, publication_options=options)
        assert query.awaitTermination(120)
        publication = connection.execute('SELECT publication_id FROM sensor_stream.current_publication').fetchone()[0]
        metadata, events = read_publication(connection, publication)
        assert metadata[-1]['complete'] and metadata[-1]['checksum_verified']
        assert len(events) == 4
        from datetime import datetime
        from decimal import Decimal
        from greenhouse.contracts import RuleBasis, RuleScheduleRevision, RuleScheduleSegment
        from greenhouse.sensor.replay_evaluation import evaluate_publication
        rule = RuleScheduleRevision('drain-v1', 'drain', 1, simulator.data_scope_id, simulator.compartment_id,
            'synthetic test', True, 'demo_rule', (
                RuleScheduleSegment('ec', 'drain_process_computer', RuleBasis.MEASURED,
                    datetime(2019,12,16), datetime(2020,5,30), lower=Decimal('2'), upper=Decimal('5')),
                RuleScheduleSegment('ph', 'drain_process_computer', RuleBasis.MEASURED,
                    datetime(2019,12,16), datetime(2020,5,30), lower=Decimal('5.5'), upper=Decimal('6.5'))))
        evaluation = evaluate_publication(connection, publication, drain_rule=rule)
        for metric in evaluation.drain.metric_results:
            assert metric.expected_minutes == Decimal('20')
            assert metric.valid_minutes == Decimal('20')
            assert metric.out_minutes == Decimal('0')
    finally:
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()


def test_duplicate_timestamp_is_quarantined_from_publication(connection, tmp_path):
    from greenhouse.sensor.replay_publication import publish_replay, read_publication
    from unit.test_sensor_replay_state import example_messages, rows, dump
    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    messages = example_messages()
    event = json.loads(messages[0][2])
    other = dict(event)
    other['event_id'] = 'f' * 64
    deliveries = rows(tmp_path, messages + [('telemetry_messages', 2, dump(other))])
    commit_batch(connection, 'stream', 'one', 0, deliveries)
    pub = publish_replay(connection, replay_id=event['replay_id'], scope_id=event['data_scope_id'],
                         cycle_id=event['cycle_id'], compartment_id=event['compartment_id'], quality_policy_version='policy')
    metadata, events = read_publication(connection, pub)
    assert not metadata[-1]['complete']
    assert len(metadata[-1]['slot_conflicts']) == 2
    assert events == ()


@pytest.mark.parametrize('fault, expected_conflicts, expected_missing', [
    ('conflict', 3, 3), ('drop', 0, 3),
])
def test_simulated_faults_are_retained_and_do_not_complete(connection, tmp_path, fault, expected_conflicts, expected_missing):
    import sqlite3
    from greenhouse.sensor.faults import FaultScheduler
    from greenhouse.sensor.simulator import SensorSimulator
    from greenhouse.sensor.replay_state import load_replay_state
    from unit.test_sensor_simulator import create_synthetic_climate_csv

    csv = create_synthetic_climate_csv(tmp_path / 'climate.csv', num_rows=3)
    scheduler = FaultScheduler(conflict_prob=1 if fault == 'conflict' else 0,
                               drop_prob=1 if fault == 'drop' else 0)
    sim = SensorSimulator(source_path=csv, replay_id='fault-replay', outbox_dir=tmp_path / 'outbox',
                          cursor_interval=2, fault_scheduler=scheduler)
    assert sim.run().expected_unique_events == 3
    spool = DurableSpool(tmp_path / 'spool')
    with sqlite3.connect(tmp_path / 'outbox' / 'outbox.db') as db:
        for table, key in (('control_messages', 'control_sequence'), ('telemetry_messages', 'id')):
            for row_id, topic, payload in db.execute(f'SELECT {key},topic,payload_bytes FROM {table} ORDER BY {key}'):
                spool.record_delivery(topic, payload, '2026-11-06T10:01:00+00:00',
                    outbox_table=table, outbox_row_id=row_id, replay_id=sim.replay_id,
                    compartment_id=sim.compartment_id)
    ready = tmp_path / 'ready'
    batch = BatchManager(spool, ready).pack_batch()
    records = [json.loads(line) for line in (ready / batch.file_name).read_text().splitlines()]
    commit_batch(connection, 'fault-stream', 'v1', 0, records)
    state = load_replay_state(connection, sim.replay_id)
    assert state.completion_requested and not state.complete
    assert len(state.conflicts) == expected_conflicts
    assert len(state.missing_sequences) == expected_missing


def test_replay_rule_mapping_requires_same_snapshot_and_compartment(connection, tmp_path, monkeypatch):
    from datetime import datetime
    from decimal import Decimal
    from greenhouse.contracts import RuleBasis, RuleScheduleRevision, RuleScheduleSegment
    from greenhouse.sensor.replay_evaluation import map_historical_rule
    from greenhouse.sensor.replay_publication import publish_replay
    from unit.test_sensor_replay_state import example_messages, rows

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    connection.execute('CREATE SCHEMA meta')
    connection.execute('CREATE TABLE meta.data_scope(data_scope_id text PRIMARY KEY, snapshot_id text NOT NULL)')
    messages = example_messages()
    event = json.loads(messages[0][2])
    connection.execute('INSERT INTO meta.data_scope VALUES (%s,%s)', ('historical', event['source_provenance']['snapshot_id']))
    commit_batch(connection, 'stream', 'one', 0, rows(tmp_path, messages))
    pub = publish_replay(connection, replay_id=event['replay_id'], scope_id=event['data_scope_id'],
                         cycle_id=event['cycle_id'], compartment_id=event['compartment_id'], quality_policy_version='policy')
    rule = RuleScheduleRevision('v1', 'rule', 1, 'historical', event['compartment_id'], 'source', True, 'demo_rule',
            (RuleScheduleSegment('temperature', 'climate', RuleBasis.REQUESTED, datetime(2019,12,16),
                                 datetime(2019,12,17), delta=Decimal('1')),))
    monkeypatch.setattr('greenhouse.db.rule_schedule.read_rule_schedule', lambda conn, version: rule)
    mapped = map_historical_rule(connection, pub, 'v1')
    assert mapped.scope_id == event['data_scope_id'] and mapped.rule_version == 'v1'
    connection.execute("UPDATE meta.data_scope SET snapshot_id='different'")
    with pytest.raises(ValueError, match='snapshot or compartment'):
        map_historical_rule(connection, pub, 'v1')


def test_foreign_scope_is_preserved_but_excluded_from_publication(connection, tmp_path):
    from greenhouse.sensor.replay_publication import publish_replay, read_publication
    from unit.test_sensor_replay_state import example_messages, rows, dump
    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    messages = example_messages()
    event = json.loads(messages[0][2])
    foreign = json.loads(messages[0][2])
    foreign['event_id'] = 'f' * 64
    foreign['data_scope_id'] = 'foreign-scope'
    deliveries = rows(tmp_path, messages + [('telemetry_messages', 2, dump(foreign))])
    assert commit_batch(connection, 'stream', 'one', 0, deliveries)
    publication = publish_replay(connection, replay_id=event['replay_id'], scope_id=event['data_scope_id'],
                                 cycle_id=event['cycle_id'], compartment_id=event['compartment_id'], quality_policy_version='policy')
    metadata, events = read_publication(connection, publication)
    assert not metadata[-1]['complete']
    assert metadata[-1]['scope_conflicts'] == ['f' * 64]
    assert len(events) == 1 and events[0]['data_scope_id'] == event['data_scope_id']
    assert connection.execute('SELECT count(*) FROM sensor_stream.event_variant').fetchone()[0] == 4


def test_replay_quality_policy_respects_effective_window(connection):
    from datetime import datetime
    from greenhouse.sensor.replay_slots import build_replay_rows
    connection.execute('CREATE SCHEMA config')
    connection.execute('''CREATE TABLE config.quality_policy_version(
        policy_version_id text PRIMARY KEY, definition jsonb NOT NULL,
        valid_from timestamp NOT NULL, valid_to_exclusive timestamp NOT NULL)''')
    connection.execute("INSERT INTO config.quality_policy_version VALUES ('policy', '{}', '2019-12-15', '2019-12-16')")
    event = json.loads(make_valid_telemetry_payload())
    args = dict(replay_id=event['replay_id'], scope_id=event['data_scope_id'], cycle_id=event['cycle_id'],
                compartment_id=event['compartment_id'], quality_policy_version='policy')
    with pytest.raises(ValueError, match='outside effective interval'):
        build_replay_rows(connection, [event], **args)
    climate, _ = build_replay_rows(connection, [event], cycle_end=datetime(2019,12,16), **args)
    assert len(climate) == 1


def test_publication_retains_out_of_policy_event_without_using_it(connection, tmp_path):
    from greenhouse.sensor.replay_publication import publish_replay, read_publication
    from unit.test_sensor_replay_state import example_messages, rows
    connection.execute('CREATE SCHEMA config')
    connection.execute('''CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY,
        definition jsonb NOT NULL, valid_from timestamp NOT NULL, valid_to_exclusive timestamp NOT NULL)''')
    connection.execute("INSERT INTO config.quality_policy_version VALUES ('policy', '{}','2020-01-01','2021-01-01')")
    messages = example_messages()
    event = json.loads(messages[0][2])
    commit_batch(connection, 'stream', 'one', 0, rows(tmp_path, messages))
    publication = publish_replay(connection, replay_id=event['replay_id'], scope_id=event['data_scope_id'],
        cycle_id=event['cycle_id'], compartment_id=event['compartment_id'], quality_policy_version='policy')
    meta, events = read_publication(connection, publication)
    assert not meta[-1]['complete']
    assert meta[-1]['time_conflicts'] == [event['event_id']]
    assert not events
    assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 3


def test_stream_bounds_files_per_microbatch(connection, tmp_path):
    from greenhouse.sensor.stream_landing import start_landing
    from unit.test_sensor_replay_state import example_messages, rows
    from pyspark.sql import SparkSession
    records = rows(tmp_path / 'source', example_messages())
    ready = tmp_path / 'input'
    ready.mkdir()
    for i, record in enumerate(records):
        (ready / f'batch-{i:04}.jsonl').write_text(json.dumps(record) + '\n')
    class TestSettings:
        def connection_kwargs(self):
            return {'host': os.environ['PGHOST'], 'port': os.environ.get('PGPORT', '5432'),
                    'user': os.environ['PGUSER'], 'password': os.environ['PGPASSWORD'], 'dbname': connection.info.dbname}
    try:
        query = start_landing(TestSettings(), ready, tmp_path / 'checkpoint', 'bounded', 'v1',
                              available_now=True, max_files_per_batch=1)
        assert query.awaitTermination(120)
        assert connection.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0] == 3
        assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 3
    finally:
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()


def test_stream_cli_end_to_end(connection, tmp_path):
    import subprocess
    import sys
    from unit.test_sensor_replay_state import example_messages, rows

    connection.execute('CREATE SCHEMA config')
    connection.execute("CREATE TABLE config.quality_policy_version(policy_version_id text PRIMARY KEY, definition jsonb NOT NULL, valid_from timestamp NOT NULL DEFAULT '1900-01-01', valid_to_exclusive timestamp NOT NULL DEFAULT '2100-01-01')")
    connection.execute("INSERT INTO config.quality_policy_version(policy_version_id,definition) VALUES ('policy', '{}')")
    messages = example_messages()
    event = json.loads(messages[0][2])
    deliveries = rows(tmp_path / 'source', messages)
    ready = tmp_path / 'input'
    ready.mkdir()
    (ready / 'batch-0001.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in deliveries))
    args = [sys.executable, '-m', 'greenhouse', 'stream', '--stream-id', 'cli-test',
        '--generation', 'v1', '--replay-id', event['replay_id'], '--scope-id', event['data_scope_id'],
        '--cycle-id', event['cycle_id'], '--compartment-id', event['compartment_id'],
        '--quality-policy-version', 'policy', '--ready-dir', str(ready),
        '--checkpoint-dir', str(tmp_path / 'checkpoint'), '--available-now']
    result = subprocess.run(args, env=os.environ | {'PGDATABASE': connection.info.dbname},
                            text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    publication = connection.execute('SELECT publication_id FROM sensor_stream.current_publication').fetchone()[0]
    from greenhouse.sensor.replay_publication import read_publication
    assert read_publication(connection, publication)[0][-1]['complete']
