"""Real local MQTT + Spark replay with independent batch KPI parity and timing."""
import csv
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from greenhouse.contracts import CycleWindow, IntradayContext, QueryContext, RuleBasis, RuleScheduleRevision, RuleScheduleSegment
from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.db.migrate import migrate
from greenhouse.db.pipeline import encode, membership, run_pipeline, save_artifact
from greenhouse.queries.climate import build_slots as climate_slots
from greenhouse.queries.drain import build_slots as drain_slots
from greenhouse.queries.rule_duration import evaluate_rule_schedule
from greenhouse.sensor.collector import MqttCollector
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.replay_evaluation import evaluate_publication
from greenhouse.sensor.replay_publication import read_publication
from greenhouse.sensor.replay_slots import build_replay_rows
from greenhouse.sensor.simulator import SensorSimulator
from greenhouse.sensor.stream_landing import start_landing
from greenhouse.settings import Settings
from unit.test_core import synthetic_scope
from unit.test_sensor_simulator import create_synthetic_climate_csv


@pytest.fixture
def local_database():
    base = Settings.from_env().connection_kwargs()
    name = 'test_live_replay_' + uuid4().hex[:12]
    with psycopg.connect(**base, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        with psycopg.connect(**(base | {'dbname': name}), autocommit=True) as conn:
            migrate(conn)
            yield conn, base | {'dbname': name}
    finally:
        with psycopg.connect(**base, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def fixture_stage(connection, workflow, target, *, fault=None):
    with connection.transaction():
        info = membership(connection, workflow)
    target.mkdir()
    (target / 'candidate.json').write_text(encode(info))
    return save_artifact(target, info | {'rows_in': info['member_count'], 'rows_out': info['member_count']})


@pytest.mark.skipif(os.environ.get('GREENHOUSE_SENSOR_ACCEPTANCE') != '1',
                    reason='requires an explicitly isolated MQTT acceptance broker')
def test_real_broker_replay_matches_batch_and_records_latency(local_database, tmp_path):
    connection, target = local_database
    synthetic_scope(connection)
    raw = tmp_path / 'raw'
    folder = raw / 'Synthetic'
    folder.mkdir(parents=True)
    source = create_synthetic_climate_csv(folder / 'GreenhouseClimate.csv', num_rows=30)
    # Provide a meaningful temperature range, keeping the rest of the simulator fixture intact.
    with source.open(newline='') as f:
        data = list(csv.DictReader(f))
        fieldnames = list(data[0])
    for row in data:
        row['t_heat_sp'], row['t_vent_sp'] = '18', '22'
    with source.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(data)
    file_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {'snapshot_id': 'synthetic-core', 'archive': {'sha256': 'a' * 64},
                'files': [{'path': 'Synthetic/GreenhouseClimate.csv', 'sha256': file_hash, 'csv': {'data_rows': 30}}]}
    (raw / 'manifest.json').write_text(json.dumps(manifest))
    batch = run_pipeline(connection, raw, tmp_path / 'batch-output', 'synthetic:scope', 'SYNTHETIC_QUALITY_V1',
                         workflow='sensor-baseline-' + uuid4().hex[:8], stage_function=fixture_stage)
    historical_publication = batch['publication_id']
    replay_id = 'live-' + uuid4().hex[:12]
    simulator = SensorSimulator(source, replay_id, tmp_path / 'outbox', compartment_id='Synthetic',
        cycle_id='synthetic-cycle', snapshot_id='synthetic-core', snapshot_sha='a' * 64,
        relative_path='Synthetic/GreenhouseClimate.csv', cursor_interval=5)
    simulation = simulator.run()
    assert simulation.expected_unique_events == 30
    with sqlite3.connect(tmp_path / 'outbox' / 'outbox.db') as outbox:
        event_ids = {row[0] for row in outbox.execute('SELECT event_id FROM telemetry_messages')}
    assert len(event_ids) == 30

    ready = tmp_path / 'ready'
    ready.mkdir()
    collector = MqttCollector(spool_dir=tmp_path / 'spool', ready_dir=ready,
        mqtt_host=os.environ['MQTT_HOST'], client_id='coll-' + replay_id,
        clean_session=False, batch_size=5, batch_timeout=0.2)
    errors = []
    def collect():
        try:
            collector.start(poll_interval=0.05)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=collect, daemon=True)
    publisher = OutboxPublisher(outbox_dir=tmp_path / 'outbox', mqtt_host=os.environ['MQTT_HOST'],
        client_id='pub-' + replay_id, max_inflight=10)
    class TargetSettings:
        def connection_kwargs(self):
            return target
    query = None
    from pyspark.sql import SparkSession
    publish_times, visible_times, publish_utc, visible_utc, peak_unbatched = {}, {}, {}, {}, 0
    def cpu_usage_usec():
        path = Path('/sys/fs/cgroup/cpu.stat')
        if not path.exists():
            return None
        return int(dict(line.split() for line in path.read_text().splitlines())['usage_usec'])
    cpu_start = cpu_usage_usec()
    operational_start = time.monotonic()
    try:
        options = dict(replay_id=replay_id, scope_id=simulator.data_scope_id, cycle_id=simulator.cycle_id,
                       compartment_id=simulator.compartment_id, quality_policy_version='SYNTHETIC_QUALITY_V1')
        query = start_landing(TargetSettings(), ready, tmp_path / 'checkpoint', 'stream-' + replay_id,
                              'v1', publication_options=options, max_files_per_batch=20)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (collector._client and collector._client.is_connected()):
            time.sleep(0.05)
        assert collector._client and collector._client.is_connected() and not errors
        # Collector.start sets _running before it waits for broker SUBACK.
        time.sleep(0.5)
        publisher.start()
        original_publish = publisher.publish_single_item
        def timed_publish(item):
            if item.outbox_table == 'telemetry_messages':
                publish_times.setdefault(item.event_id, time.monotonic())
                publish_utc.setdefault(item.event_id, datetime.now(timezone.utc).isoformat())
            return original_publish(item)
        publisher.publish_single_item = timed_publish
        publisher_result = []
        def send():
            try:
                publisher_result.append(publisher.publish_all_pending(poll_interval=0.05))
            except Exception as error:
                errors.append(error)
        send_thread = threading.Thread(target=send, daemon=True)
        send_thread.start()
        deadline = time.monotonic() + 120
        current_id = None
        while time.monotonic() < deadline:
            if errors:
                raise AssertionError(f'collector/publisher error: {errors!r}')
            if not query.isActive:
                raise AssertionError(f'Spark query stopped: {query.exception()}')
            row = connection.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s', (replay_id,)).fetchone()
            if row:
                current_id = row[0]
                current = connection.execute('SELECT identity FROM sensor_stream.publication_event WHERE publication_id=%s', (current_id,)).fetchall()
                now = time.monotonic()
                for (event_id,) in current:
                    if event_id in publish_times:
                        visible_times.setdefault(event_id, now)
                        visible_utc.setdefault(event_id, datetime.now(timezone.utc).isoformat())
                state = read_publication(connection, current_id)[0][-1]
                if state['complete'] and len(visible_times) == len(event_ids) and not send_thread.is_alive():
                    break
            peak_unbatched = max(peak_unbatched, collector.spool.count_spooled_records()[2])
            time.sleep(0.1)
        else:
            pytest.fail(f'replay did not reconcile before deadline: visible={len(visible_times)} sent={len(publish_times)}')
        assert not errors and publisher_result and publisher_result[0] >= 30
        assert set(visible_times) == event_ids == set(publish_times)
        operational_elapsed = time.monotonic() - operational_start
        cpu_end = cpu_usage_usec()
        latencies_ms = sorted((visible_times[key] - publish_times[key]) * 1000 for key in event_ids)
        assert all(x >= 0 for x in latencies_ms)
        state, replay_events = read_publication(connection, current_id)[0][-1], read_publication(connection, current_id)[1]
        assert state['complete'] and state['checksum_verified'] and len(replay_events) == 30

        start, end = datetime(2019, 12, 16), datetime(2019, 12, 16, 2, 30)
        climate_rule = RuleScheduleRevision('fixture-climate-v1', 'fixture-climate', 1, 'synthetic:scope',
            'Synthetic', 'synthetic oracle', True, 'demo_rule', (
                RuleScheduleSegment('temperature', 'climate', RuleBasis.REQUESTED, start, datetime(2020,5,30), delta=Decimal('1')),))
        drain_rule = RuleScheduleRevision('fixture-drain-v1', 'fixture-drain', 1, 'synthetic:scope',
            'Synthetic', 'synthetic oracle', True, 'demo_rule', (
                RuleScheduleSegment('ec', 'drain_process_computer', RuleBasis.MEASURED, start, datetime(2020,5,30), lower=Decimal('2'), upper=Decimal('5')),
                RuleScheduleSegment('ph', 'drain_process_computer', RuleBasis.MEASURED, start, datetime(2020,5,30), lower=Decimal('5.5'), upper=Decimal('6.5'))))
        context = QueryContext('synthetic:scope', historical_publication, 'synthetic-core', 'synthetic-cycle',
                               ('Synthetic',), date(2019,12,16), date(2019,12,17))
        ca, da = PostgresClimateQueryAdapter(connection), PostgresDrainQueryAdapter(connection)
        historical_ref = ca.publication(historical_publication)
        climate_rows = ca.read_climate_slots(context, historical_ref)
        drain_rows = da.read_drain_slots(context, da.publication(historical_publication))
        cycle = CycleWindow('synthetic-cycle', 'Synthetic', date(2019,12,16), date(2020,5,30))
        climate_context = IntradayContext('synthetic:scope', cycle, ('Synthetic',), start, end, 'climate', RuleBasis.REQUESTED)
        drain_context = IntradayContext('synthetic:scope', cycle, ('Synthetic',), start, end, 'drain_process_computer', RuleBasis.MEASURED)
        historical_climate = evaluate_rule_schedule(climate_context, (climate_rule,), climate_slots(climate_rows, RuleBasis.REQUESTED), ('temperature', 'co2'))
        historical_drain = evaluate_rule_schedule(drain_context, (drain_rule,), drain_slots(drain_rows), ('ec', 'ph'))
        replay = evaluate_publication(connection, current_id, climate_rule=replace(climate_rule, scope_id=simulator.data_scope_id),
                                      drain_rule=replace(drain_rule, scope_id=simulator.data_scope_id))
        replay_climate_rows, replay_drain_rows = build_replay_rows(connection, replay_events,
            replay_id=replay_id, scope_id=simulator.data_scope_id, cycle_id=simulator.cycle_id,
            compartment_id=simulator.compartment_id, quality_policy_version='SYNTHETIC_QUALITY_V1')
        for historical, streamed in zip(climate_rows, replay_climate_rows, strict=True):
            assert historical.slot_start == streamed.slot_start
            for field in ('tair','co2air','t_heat_sp','t_vent_sp','co2_sp','t_heat_vip','t_ventlee_vip','co2_vip'):
                assert getattr(historical, field) == getattr(streamed, field)
            for code, verdict in historical.measure_quality.items():
                assert streamed.measure_quality[code] == verdict
        for historical, streamed in zip(drain_rows, replay_drain_rows, strict=True):
            assert historical.slot_start == streamed.slot_start
            assert (historical.ec_drain, historical.ph_drain) == (streamed.ec_drain, streamed.ph_drain)
            for code, verdict in historical.measure_quality.items():
                assert streamed.measure_quality[code] == verdict
        assert replay.climate.metric_results == historical_climate.metric_results
        assert replay.drain.metric_results == historical_drain.metric_results
        assert replay.climate.joint_results == historical_climate.joint_results
        assert replay.drain.joint_results == historical_drain.joint_results
        assert replay.climate.episodes == historical_climate.episodes
        assert replay.drain.episodes == historical_drain.episodes
        assert historical_climate.metric_results[0].expected_minutes == Decimal(150)
        origin = min(publish_times.values())
        received = {}
        received_rows = connection.execute("""SELECT record->>'event_id',record->>'received_at'
            FROM sensor_stream.delivery WHERE record->>'replay_id'=%s AND record->>'message_type'='telemetry'""", (replay_id,)).fetchall()
        for identity, stamp in received_rows:
            if identity and stamp:
                received.setdefault(identity, []).append(stamp)
        assert set(received) == event_ids
        traces = [{'event_id': key, 'publisher_call_utc': publish_utc[key],
                   'collector_received_utc': min(received[key]), 'queryable_observed_utc': visible_utc[key],
                   'publish_offset_ms': (publish_times[key] - origin) * 1000,
                   'queryable_offset_ms': (visible_times[key] - origin) * 1000,
                   'latency_ms': (visible_times[key] - publish_times[key]) * 1000}
                  for key in sorted(event_ids)]
        spooled, batched, unbatched = collector.spool.count_spooled_records()
        def cgroup_value(name):
            path = Path('/sys/fs/cgroup') / name
            return path.read_text().strip() if path.exists() else None
        report = {'scenario': 'synthetic-30-events-no-fault-real-mqtt', 'replay_id': replay_id,
            'source_sha256': file_hash, 'events_expected': 30, 'events_unique': len(event_ids),
            'queryable_samples': len(latencies_ms), 'latencies_ms_sorted': latencies_ms, 'event_traces': traces,
            'processing_cgroup_memory_peak_bytes': cgroup_value('memory.peak'),
            'processing_cgroup_memory_limit_bytes': cgroup_value('memory.max'), 'processing_cgroup_cpu_max': cgroup_value('cpu.max'),
            'stream_operational_wall_seconds': operational_elapsed,
            'stream_operational_cpu_seconds': (cpu_end - cpu_start) / 1000000 if cpu_end is not None and cpu_start is not None else None,
            'resource_scope': 'combined Spark, collector, publisher and test polling in one processing cgroup; memory peak also includes prior fixture batch',
            'p50_ms': (latencies_ms[14] + latencies_ms[15]) / 2, 'p95_ms_nearest_rank': latencies_ms[28],
            'throughput_events_per_second': 30 / (max(visible_times.values()) - min(publish_times.values())),
            'peak_unbatched_observed': peak_unbatched, 'spooled_deliveries': spooled,
            'batched_deliveries': batched, 'remaining_unbatched': unbatched,
            'ready_file_count': len(list(ready.glob('batch-*.jsonl'))), 'state': state,
            'batch_stream_br01_br03_values_quality_metrics_joint_and_episodes_equal': True,
            'timing_method': 'monotonic before MQTT publish call to first polling observation in current publication'}
        evidence_path = os.environ.get('GREENHOUSE_SENSOR_EVIDENCE')
        if evidence_path:
            Path(evidence_path).write_text(json.dumps(report, indent=2) + '\n')
        print('SENSOR_EVIDENCE=' + json.dumps(report))
    finally:
        publisher.stop()
        collector.stop()
        thread.join(timeout=5)
        if query is not None:
            query.stop()
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()


def test_stream_process_restart_uses_same_checkpoint(local_database, tmp_path):
    """Start two independent Python/Spark processes against one checkpoint."""
    import subprocess
    import sys
    from greenhouse.sensor.collector import BatchManager, DurableSpool

    connection, target = local_database
    source = create_synthetic_climate_csv(tmp_path / 'climate.csv', num_rows=1)
    replay = 'spark-restart-' + uuid4().hex[:12]
    simulator = SensorSimulator(source, replay, tmp_path / 'outbox', cursor_interval=1)
    assert simulator.run().expected_unique_events == 1
    spool = DurableSpool(tmp_path / 'spool')
    with sqlite3.connect(tmp_path / 'outbox' / 'outbox.db') as db:
        for table, key in (('telemetry_messages', 'id'), ('control_messages', 'control_sequence')):
            for row_id, topic, payload in db.execute(f'SELECT {key},topic,payload_bytes FROM {table} ORDER BY {key}'):
                spool.record_delivery(topic, payload, '2026-11-06T10:01:00+00:00', outbox_table=table,
                    outbox_row_id=row_id, replay_id=replay, compartment_id=simulator.compartment_id)
    collector_ready = tmp_path / 'collector-ready'
    packed = BatchManager(spool, collector_ready).pack_batch()
    lines = (collector_ready / packed.file_name).read_text().splitlines()
    assert len(lines) >= 3
    ready = tmp_path / 'stream-ready'
    ready.mkdir()
    (ready / 'batch-0001.jsonl').write_text(lines[0] + '\n')
    command = [sys.executable, '-m', 'greenhouse', 'stream', '--stream-id', replay, '--generation', 'v1',
        '--replay-id', replay, '--scope-id', simulator.data_scope_id, '--cycle-id', simulator.cycle_id,
        '--compartment-id', simulator.compartment_id, '--quality-policy-version', 'AGC_QUALITY_V1',
        '--ready-dir', str(ready), '--checkpoint-dir', str(tmp_path / 'checkpoint'), '--available-now']
    env = os.environ | {'PGHOST': target['host'], 'PGPORT': str(target['port']),
                        'PGDATABASE': target['dbname'], 'PGUSER': target['user'], 'PGPASSWORD': target['password']}
    first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr[-3000:]
    assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == 1
    (ready / 'batch-0002.jsonl').write_text('\n'.join(lines[1:]) + '\n')
    second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stderr[-3000:]
    assert connection.execute('SELECT count(*) FROM sensor_stream.delivery').fetchone()[0] == len(lines)
    assert connection.execute('SELECT count(*) FROM sensor_stream.processed_batch').fetchone()[0] == 2
    publication = connection.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s', (replay,)).fetchone()[0]
    meta, events = read_publication(connection, publication)
    assert meta[-1]['complete'] and len(events) == 1
    evidence_path = os.environ.get('GREENHOUSE_SENSOR_EVIDENCE')
    if evidence_path:
        (Path(evidence_path).parent / 'spark-process-restart.json').write_text(json.dumps({
            'replay_id': replay, 'independent_processes': 2, 'checkpoint_reused': True,
            'deliveries': len(lines), 'processed_batches': 2, 'unique_events': len(events),
            'complete': meta[-1]['complete']}, indent=2) + '\n')


@pytest.mark.skipif(not os.environ.get('GREENHOUSE_REFERENCE_DATABASE') or not os.environ.get('GREENHOUSE_REFERENCE_PUBLICATION'),
                    reason='requires explicitly selected read-only historical publication')
def test_real_agc_replay_window_matches_historical_publication(local_database, tmp_path):
    """Read 30 real AGC rows; compare replay to a pinned historical mart."""
    from greenhouse.db.rule_schedule import read_rule_schedule
    from greenhouse.sensor.collector import BatchManager, DurableSpool

    connection, target = local_database
    snapshot_id = 'agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68'
    source = Path('/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68/Automatoes/GreenhouseClimate.csv')
    if not source.is_file():
        pytest.skip('locked AGC snapshot is not mounted')
    replay_id = 'agc-parity-' + uuid4().hex[:12]
    sim = SensorSimulator(source, replay_id, tmp_path / 'outbox', compartment_id='Automatoes',
                          relative_path='Automatoes/GreenhouseClimate.csv', cursor_interval=5, limit=30)
    assert sim.snapshot_id == snapshot_id
    assert sim.run().expected_unique_events == 30
    spool = DurableSpool(tmp_path / 'spool')
    with sqlite3.connect(tmp_path / 'outbox' / 'outbox.db') as db:
        for table, key in (('telemetry_messages', 'id'), ('control_messages', 'control_sequence')):
            for row_id, topic, payload in db.execute(f'SELECT {key},topic,payload_bytes FROM {table} ORDER BY {key}'):
                spool.record_delivery(topic, payload, '2026-11-06T10:01:00+00:00', outbox_table=table,
                    outbox_row_id=row_id, replay_id=replay_id, compartment_id='Automatoes')
    ready = tmp_path / 'ready'
    BatchManager(spool, ready).pack_batch(max_records=100)
    class Target:
        def connection_kwargs(self):
            return target
    options = dict(replay_id=replay_id, scope_id=sim.data_scope_id, cycle_id=sim.cycle_id,
                   compartment_id='Automatoes', quality_policy_version='AGC_QUALITY_V1')
    from pyspark.sql import SparkSession
    query = None
    try:
        query = start_landing(Target(), ready, tmp_path / 'checkpoint', replay_id, 'v1',
                              available_now=True, publication_options=options)
        assert query.awaitTermination(120)
        replay_publication = connection.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s', (replay_id,)).fetchone()[0]
        replay_meta, events = read_publication(connection, replay_publication)
        assert replay_meta[-1]['complete'] and len(events) == 30
        reference_args = Settings.from_env().connection_kwargs() | {
            'dbname': os.environ['GREENHOUSE_REFERENCE_DATABASE'],
            'options': '-c default_transaction_read_only=on'}
        historical_id = os.environ['GREENHOUSE_REFERENCE_PUBLICATION']
        with psycopg.connect(**reference_args, autocommit=True) as historical:
            ca, da = PostgresClimateQueryAdapter(historical), PostgresDrainQueryAdapter(historical)
            ref = ca.publication(historical_id)
            assert ref.snapshot_id == snapshot_id
            context = QueryContext(ref.scope_id, historical_id, snapshot_id, 'agc-cycle-Automatoes',
                                   ('Automatoes',), date(2019,12,16), date(2019,12,17))
            start = datetime(2019,12,16)
            end = start + timedelta(minutes=150)
            cr = ca.read_climate_slots(context, ref, window_start=start, window_end=end)
            dr = da.read_drain_slots(context, da.publication(historical_id), window_start=start, window_end=end)
            assert len(cr) == len(dr) == 30
            rc, rd = build_replay_rows(connection, events, replay_id=replay_id, scope_id=sim.data_scope_id,
                cycle_id=sim.cycle_id, compartment_id='Automatoes', quality_policy_version='AGC_QUALITY_V1')
            event_by_id = {e['event_id']: e for e in events}
            source_rows = historical.execute('''SELECT r.revision_id,s.source_record_id,s.line_number,s.raw_hash
                FROM core.record_revision r JOIN meta.source_record s USING(source_record_id)
                WHERE r.revision_id=ANY(%s)''', ([row.revision_id for row in cr],)).fetchall()
            lineage = {revision: (source_id, line_number, raw_hash)
                       for revision, source_id, line_number, raw_hash in source_rows}
            assert len(lineage) == 30
            for a, b in zip(cr, rc, strict=True):
                assert a.slot_start == b.slot_start
                event = event_by_id[b.revision_id]
                assert lineage[a.revision_id] == (event['source_record_id'],
                    event['source_provenance']['line_number'], event['source_provenance']['raw_hash'])
                for field in ('tair','co2air','t_heat_sp','t_vent_sp','co2_sp','t_heat_vip','t_ventlee_vip','co2_vip'):
                    assert getattr(a, field) == getattr(b, field)
                for code, quality in a.measure_quality.items():
                    assert b.measure_quality[code] == quality
            for a, b in zip(dr, rd, strict=True):
                assert a.slot_start == b.slot_start
                assert (a.ec_drain, a.ph_drain) == (b.ec_drain, b.ph_drain)
                for code, quality in a.measure_quality.items():
                    assert b.measure_quality[code] == quality
            climate_rule = read_rule_schedule(historical, 'climate-demo-b01:Automatoes:v1')
            drain_rule = read_rule_schedule(historical, 'drain-demo-b03:Automatoes:v1')
            assert climate_rule and drain_rule
            cycle = CycleWindow('agc-cycle-Automatoes', 'Automatoes', date(2019,12,16), date(2020,5,30))
            cctx = IntradayContext(ref.scope_id, cycle, ('Automatoes',), start, end, 'climate', RuleBasis.REQUESTED)
            dctx = IntradayContext(ref.scope_id, cycle, ('Automatoes',), start, end, 'drain_process_computer', RuleBasis.MEASURED)
            hc = evaluate_rule_schedule(cctx, (climate_rule,), climate_slots(cr, RuleBasis.REQUESTED), ('temperature', 'co2'))
            hd = evaluate_rule_schedule(dctx, (drain_rule,), drain_slots(dr), ('ec', 'ph'))
            replay = evaluate_publication(connection, replay_publication,
                climate_rule=replace(climate_rule, scope_id=sim.data_scope_id),
                drain_rule=replace(drain_rule, scope_id=sim.data_scope_id))
            assert replay.climate.metric_results == hc.metric_results
            assert replay.drain.metric_results == hd.metric_results
            assert replay.climate.joint_results == hc.joint_results
            assert replay.drain.joint_results == hd.joint_results
            assert replay.climate.episodes == hc.episodes
            assert replay.drain.episodes == hd.episodes
            evidence = {'dataset': 'locked AGC 2nd edition', 'snapshot_id': snapshot_id,
                'compartment': 'Automatoes', 'source_file': 'Automatoes/GreenhouseClimate.csv',
                'source_sha256': sim.file_checksum, 'window_start': start.isoformat(), 'window_end': end.isoformat(),
                'events': len(events), 'event_trace': [{'event_id': e['event_id'],
                    'source_record_id': e['source_record_id'], 'line_number': e['source_provenance']['line_number'],
                    'raw_hash': e['source_provenance']['raw_hash'], 'source_observed_at': e['source_observed_at']}
                    for e in events], 'historical_publication': historical_id,
                'historical_reference_database': os.environ['GREENHOUSE_REFERENCE_DATABASE'],
                'replay_publication': replay_publication, 'quality_policy': 'AGC_QUALITY_V1',
                'climate_rule': climate_rule.rule_version, 'drain_rule': drain_rule.rule_version,
                'parity': 'passed: source lineage, per-slot values and quality, metric/joint durations, episodes',
                'transport': 'outbox to collector spool to Spark; real MQTT measured separately in synthetic run'}
            path = os.environ.get('GREENHOUSE_REAL_PARITY_EVIDENCE')
            if path:
                Path(path).write_text(json.dumps(evidence, indent=2) + '\n')
    finally:
        if query is not None and query.isActive:
            query.stop()
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()
