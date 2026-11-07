"""Real-MQTT replay fault cases against an isolated broker and PostgreSQL DB."""
import base64
import csv
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from greenhouse.sensor.collector import MqttCollector
from greenhouse.sensor.faults import FaultScheduler
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.replay_publication import read_publication
from greenhouse.sensor.simulator import SensorSimulator
from greenhouse.sensor.stream_landing import start_landing
from integration.test_sensor_live_replay import local_database
from unit.test_sensor_simulator import create_synthetic_climate_csv

pytestmark = pytest.mark.skipif(os.environ.get('GREENHOUSE_SENSOR_ACCEPTANCE') != '1',
                                reason='requires an explicitly isolated MQTT acceptance broker')


@pytest.mark.parametrize('fault', ['duplicate', 'conflict', 'delay', 'drop', 'malformed', 'invalid-schema'])
def test_fault_case_over_real_mqtt(local_database, tmp_path, fault):
    connection, target = local_database
    replay_id = 'fault-' + uuid4().hex[:12]
    source = create_synthetic_climate_csv(tmp_path / 'climate.csv', num_rows=6)
    with source.open(newline='') as handle:
        data = list(csv.DictReader(handle))
        names = list(data[0])
    for row in data:
        row['t_heat_sp'], row['t_vent_sp'] = '18', '20'
    with source.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=names, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(data)
    scheduler = FaultScheduler(seed=3, duplicate_prob=1 if fault == 'duplicate' else 0,
        conflict_prob=1 if fault == 'conflict' else 0, delay_prob=1 if fault == 'delay' else 0,
        drop_prob=1 if fault == 'drop' else 0)
    sim = SensorSimulator(source, replay_id, tmp_path / 'outbox', cursor_interval=2, fault_scheduler=scheduler)
    assert sim.run().expected_unique_events == 6
    with sqlite3.connect(tmp_path / 'outbox' / 'outbox.db') as db:
        audit = db.execute('SELECT event_id,sequence,fault_type FROM fault_audit ORDER BY id').fetchall()
        telemetry_order = [row[0] for row in db.execute('SELECT sequence FROM telemetry_messages ORDER BY id')]
        if fault in ('malformed', 'invalid-schema'):
            event_id, original = db.execute('SELECT event_id,payload_bytes FROM telemetry_messages ORDER BY id LIMIT 1').fetchone()
            if fault == 'malformed':
                replacement = b'\xff{not-json'
            else:
                invalid = json.loads(original)
                invalid['units']['Tair'] = 'wrong-unit'
                replacement = json.dumps(invalid, sort_keys=True).encode()
            db.execute('UPDATE telemetry_messages SET payload_bytes=?,payload_hash=? WHERE id=1',
                       (replacement, hashlib.sha256(replacement).hexdigest()))
            db.commit()
    ready = tmp_path / 'ready'
    collector = MqttCollector(spool_dir=tmp_path / 'spool', ready_dir=ready,
        mqtt_host=os.environ['MQTT_HOST'], client_id='coll-' + replay_id, batch_size=4, batch_timeout=0.1)
    errors = []
    def collect():
        try:
            collector.start(poll_interval=0.05)
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=collect, daemon=True)
    publisher = OutboxPublisher(outbox_dir=tmp_path / 'outbox', mqtt_host=os.environ['MQTT_HOST'],
                                client_id='pub-' + replay_id, max_inflight=10)
    query = None
    from pyspark.sql import SparkSession
    try:
        worker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (collector._client and collector._client.is_connected()):
            time.sleep(0.05)
        assert collector._client and collector._client.is_connected() and not errors
        time.sleep(0.5)
        publisher.start()
        assert publisher.publish_all_pending(poll_interval=0.05) >= 2  # cursor and manifest even with total drop
        publisher.stop()
        collector.stop()
        worker.join(timeout=5)
        assert not worker.is_alive() and not errors
        class Target:
            def connection_kwargs(self):
                return target
        options = dict(replay_id=replay_id, scope_id=sim.data_scope_id, cycle_id=sim.cycle_id,
                       compartment_id=sim.compartment_id, quality_policy_version='AGC_QUALITY_V1')
        query = start_landing(Target(), ready, tmp_path / 'checkpoint', 'fault-' + replay_id, 'v1',
                              available_now=True, publication_options=options)
        assert query.awaitTermination(120)
        publication = connection.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s', (replay_id,)).fetchone()[0]
        metadata, events = read_publication(connection, publication)
        state = metadata[-1]
        assert state['completion_requested']
        if fault in ('duplicate', 'delay'):
            assert state['complete'] and len(events) == 6 and state['checksum_verified']
            assert len(audit) == 6
            if fault == 'delay':
                assert telemetry_order != sorted(telemetry_order), 'fixture must exercise out-of-order delivery'
        elif fault == 'conflict':
            assert not state['complete'] and len(events) == 0
            assert set(state['conflicts']) == {row[0] for row in audit}
            assert state['missing_sequences'] == [1, 2, 3, 4, 5, 6]
        elif fault == 'drop':
            assert not state['complete'] and len(events) == 0
            assert set(state['missing_sequences']) == {row[1] for row in audit}
        else:
            assert not state['complete'] and state['quarantined'] == 1
            assert state['missing_sequences'] == [1] and len(events) == 5
            quarantined = connection.execute("SELECT record FROM sensor_stream.delivery WHERE replay_id IS NULL AND record->>'replay_id'=%s", (replay_id,)).fetchall()
            assert len(quarantined) == 1
            assert base64.b64decode(quarantined[0][0]['raw_payload_base64']) == replacement
        # Independent source fixture oracle: 6 five-minute slots; temperatures
        # 20.0..20.5 with heat=18, vent=20 and delta=0.1 yield 20 out-minutes.
        # EC 3.2 and pH 6.2 stay inside their declared synthetic bounds.
        from greenhouse.contracts import RuleBasis, RuleScheduleRevision, RuleScheduleSegment
        from greenhouse.sensor.replay_evaluation import evaluate_publication
        start, end = datetime(2019,12,16), datetime(2020,5,30)
        climate = RuleScheduleRevision('fault-climate-v1', 'fault-climate', 1, sim.data_scope_id,
            sim.compartment_id, 'synthetic fault oracle', True, 'demo_rule',
            (RuleScheduleSegment('temperature', 'climate', RuleBasis.REQUESTED, start, end, delta=Decimal('0.1')),))
        drain = RuleScheduleRevision('fault-drain-v1', 'fault-drain', 1, sim.data_scope_id,
            sim.compartment_id, 'synthetic fault oracle', True, 'demo_rule', (
            RuleScheduleSegment('ec', 'drain_process_computer', RuleBasis.MEASURED, start, end,
                                lower=Decimal('2'), upper=Decimal('5')),
            RuleScheduleSegment('ph', 'drain_process_computer', RuleBasis.MEASURED, start, end,
                                lower=Decimal('5.5'), upper=Decimal('6.5'))))
        evaluation = evaluate_publication(connection, publication, climate_rule=climate, drain_rule=drain)
        temperature = next(x for x in evaluation.climate.metric_results if x.metric == 'temperature')
        ec = next(x for x in evaluation.drain.metric_results if x.metric == 'ec')
        assert temperature.expected_minutes == ec.expected_minutes == Decimal(30)
        if fault in ('conflict', 'drop'):
            assert temperature.valid_minutes == ec.valid_minutes == 0
            assert temperature.out_hours is None and ec.out_hours is None
        else:
            expected_valid = Decimal(25 if fault in ('malformed', 'invalid-schema') else 30)
            assert temperature.valid_minutes == ec.valid_minutes == expected_valid
            assert temperature.out_minutes == Decimal(20) and ec.out_minutes == 0
        evidence_path = os.environ.get('GREENHOUSE_SENSOR_EVIDENCE')
        if evidence_path:
            delivered = connection.execute("""SELECT record FROM sensor_stream.delivery
                WHERE record->>'replay_id'=%s ORDER BY batch_id,ordinal""", (replay_id,)).fetchall()
            trace = [{key: row[0].get(key) for key in ('outbox_table', 'outbox_row_id', 'event_id',
                'message_type', 'is_valid', 'quarantine_reason', 'payload_hash', 'raw_payload_base64')}
                for row in delivered]
            evidence = {'scenario': fault, 'replay_id': replay_id, 'publication_id': publication,
                'input_rows': 6, 'audit': [{'event_id': a, 'sequence': b, 'fault_type': c} for a,b,c in audit],
                'state': state, 'delivered_count': len(trace), 'deliveries': trace,
                'expected_valid_minutes': str(temperature.valid_minutes),
                'observed_temperature_out_minutes': str(temperature.out_minutes),
                'observed_ec_out_minutes': str(ec.out_minutes), 'fixture_oracle_passed': True}
            (Path(evidence_path).parent / f'fault-{fault}.json').write_text(json.dumps(evidence, indent=2) + '\n')
    finally:
        publisher.stop()
        collector.stop()
        worker.join(timeout=5)
        if query is not None:
            query.stop()
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()


def test_late_event_changes_episode_only_in_new_publication(local_database, tmp_path):
    """Publish event 2 after a cursor at event 3 through real MQTT and Spark."""
    from greenhouse.contracts import RuleBasis, RuleScheduleRevision, RuleScheduleSegment
    from greenhouse.sensor.replay_evaluation import evaluate_publication
    connection, target = local_database
    replay_id = 'late-' + uuid4().hex[:12]
    source = create_synthetic_climate_csv(tmp_path / 'climate.csv', num_rows=3)
    with source.open(newline='') as handle:
        data = list(csv.DictReader(handle))
        names = list(data[0])
    for row in data:
        row.update({'Tair': '25', 't_heat_sp': '18', 't_vent_sp': '20'})
    with source.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=names, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(data)
    sim = SensorSimulator(source, replay_id, tmp_path / 'outbox', cursor_interval=1)
    assert sim.run().expected_unique_events == 3
    ready = tmp_path / 'ready'
    ready.mkdir()
    collector = MqttCollector(spool_dir=tmp_path / 'spool', ready_dir=ready,
        mqtt_host=os.environ['MQTT_HOST'], client_id='late-coll-' + replay_id, batch_size=2, batch_timeout=0.1)
    errors = []
    def collect():
        try:
            collector.start(poll_interval=0.05)
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=collect, daemon=True)
    publisher = OutboxPublisher(outbox_dir=tmp_path / 'outbox', mqtt_host=os.environ['MQTT_HOST'],
                                client_id='late-pub-' + replay_id, max_inflight=10)
    query = None
    from pyspark.sql import SparkSession
    class Target:
        def connection_kwargs(self):
            return target
    try:
        options = dict(replay_id=replay_id, scope_id=sim.data_scope_id, cycle_id=sim.cycle_id,
                       compartment_id=sim.compartment_id, quality_policy_version='AGC_QUALITY_V1')
        query = start_landing(Target(), ready, tmp_path / 'checkpoint', 'late-' + replay_id, 'v1',
                              publication_options=options)
        worker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (collector._client and collector._client.is_connected()):
            time.sleep(0.05)
        assert collector._client and collector._client.is_connected() and not errors
        time.sleep(0.5)
        publisher.start()
        telemetry = publisher.fetch_unconfirmed_telemetry(limit=10)
        controls = publisher.fetch_unconfirmed_controls(limit=10)
        assert len(telemetry) == 3 and len(controls) >= 5
        for item in (telemetry[0], telemetry[2], *controls[:3]):
            assert publisher.publish_single_item(item)
        deadline = time.monotonic() + 60
        partial_id = None
        while time.monotonic() < deadline:
            row = connection.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s', (replay_id,)).fetchone()
            if row:
                state, events = read_publication(connection, row[0])
                state = state[-1]
                if state['cursor_sequence'] == 3 and state['missing_sequences'] == [2] and len(events) == 2:
                    partial_id = row[0]
                    break
            if errors or not query.isActive:
                pytest.fail('collector or Spark stopped before provisional publication')
            time.sleep(0.1)
        assert partial_id is not None
        rule = RuleScheduleRevision('late-rule-v1', 'late-rule', 1, sim.data_scope_id, sim.compartment_id,
            'synthetic late-event oracle', True, 'demo_rule',
            (RuleScheduleSegment('temperature', 'climate', RuleBasis.REQUESTED,
                datetime(2019,12,16), datetime(2020,5,30), delta=Decimal('1')),))
        before = evaluate_publication(connection, partial_id, climate_rule=rule)
        assert before.provisional and len(before.climate.episodes) == 2
        assert publisher.publish_single_item(telemetry[1])
        publisher.publish_all_pending(poll_interval=0.05)
        deadline = time.monotonic() + 60
        final_id = None
        while time.monotonic() < deadline:
            row = connection.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s', (replay_id,)).fetchone()
            if row and row[0] != partial_id:
                meta, events = read_publication(connection, row[0])
                if meta[-1]['complete'] and len(events) == 3:
                    final_id = row[0]
                    break
            if errors or not query.isActive:
                pytest.fail('collector or Spark stopped before final publication')
            time.sleep(0.1)
        assert final_id is not None
        after = evaluate_publication(connection, final_id, climate_rule=rule)
        assert not after.provisional and len(after.climate.episodes) == 1
        assert after.climate.episodes[0].original_minutes == Decimal('15')
        assert evaluate_publication(connection, partial_id, climate_rule=rule) == before
        evidence_path = os.environ.get('GREENHOUSE_SENSOR_EVIDENCE')
        if evidence_path:
            evidence = {'scenario': 'late-after-publication', 'replay_id': replay_id,
                'partial_publication': partial_id, 'final_publication': final_id,
                'partial_missing_sequence': [2], 'partial_episodes': len(before.climate.episodes),
                'final_episodes': len(after.climate.episodes),
                'final_episode_minutes': str(after.climate.episodes[0].original_minutes),
                'old_publication_unchanged': True}
            (Path(evidence_path).parent / 'fault-late.json').write_text(json.dumps(evidence, indent=2) + '\n')
    finally:
        publisher.stop()
        collector.stop()
        worker.join(timeout=5)
        if query is not None:
            query.stop()
        spark = SparkSession.getActiveSession()
        if spark:
            spark.stop()
