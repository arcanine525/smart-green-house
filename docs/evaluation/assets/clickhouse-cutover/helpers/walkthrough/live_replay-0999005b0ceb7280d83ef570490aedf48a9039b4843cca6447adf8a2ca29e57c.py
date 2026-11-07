"""Real MQTT/Spark partial-to-complete replay for the browser walkthrough.

Prepare ten actual AGC events plus the expected-30 control manifest. During the
recorded UI session, complete sends the remaining twenty through the same
durable outbox/collector/checkpoint. It never updates DOM or warehouse rows by
hand to simulate progress.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

sys.path.insert(0,'/repro')
sys.path.insert(0,'/migration')
from common import COORDINATOR, STATE, connect, historical_id, write_json
import agc_sensor as transport
from greenhouse.app import wiring
from greenhouse.clickhouse.replay_config import from_historical
from greenhouse.clickhouse.replay_publication import read_publication
from greenhouse.clickhouse.replay_evaluation import build_replay_rows
from greenhouse.sensor.simulator import SensorSimulator, STANDARD_AGC_SHA
from greenhouse.sensor.publisher import OutboxPublisher
from read_business_reference import fingerprint

run_name=os.environ.get('GREENHOUSE_WALKTHROUGH_REPLAY_RUN','live-replay')
if not re.fullmatch(r'live-replay(?:-[a-z0-9]+)?',run_name):raise ValueError('Explicit replay run name required')
ROOT=STATE/'walkthrough'/run_name
transport.ROOT=ROOT


def current(metadata):
    with closing(connect()) as client:
        pointer=client.query('SELECT DISTINCT publication_id,generation FROM replay_pointer WHERE scope_id={scope:String} ORDER BY generation DESC',{'scope':metadata['scope_id']})
        assert pointer
        pub=pointer[0]['publication_id'];state,events=read_publication(client,pub)
        return pub,state[-1],events


def publish(metadata, *, partial):
    collector,worker,errors=transport.start_collector(metadata)
    publisher=OutboxPublisher(outbox_dir=ROOT/'outbox',mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),
        client_id=metadata['publisher_id'],max_inflight=50)
    publisher.start()
    try:
        if partial:
            items=publisher.fetch_unconfirmed_telemetry(limit=10)+publisher.fetch_unconfirmed_controls(limit=100)
            assert len(items)>10
            for item in items:assert publisher.publish_single_item(item)
            transport.wait_until(lambda:all(publisher.is_row_confirmed(i.outbox_table,i.outbox_row_id) for i in items))
        else:
            publisher.publish_all_pending(poll_interval=.05)
    finally:
        publisher.stop();transport.stop_collector(collector,worker,errors)
    return transport.stream(metadata)


def prepare():
    if ROOT.exists():raise ValueError('Live replay preparation already exists; preserve it')
    ROOT.mkdir(parents=True)
    with closing(connect(action=True)) as client:
        pub=wiring.energy_service(client).adapter.publication(historical_id())
        cycle=next(c for c in wiring.catalog_service(client).cycles(pub) if c.compartment_id=='Automatoes')
        source=Path(os.environ['GREENHOUSE_RAW_DIR'])/'agc-2nd-2019'/STANDARD_AGC_SHA/'Automatoes/GreenhouseClimate.csv'
        replay='walkthrough-live-'+uuid4().hex[:12]
        simulator=SensorSimulator(source,replay,ROOT/'outbox',compartment_id='Automatoes',
            cycle_id=cycle.cycle_id,snapshot_id=pub.snapshot_id,limit=30,cursor_interval=5)
        result=simulator.run();assert result.expected_unique_events==30
        version=replay+'-v1'
        config,pin=from_historical(client,COORDINATOR,pub.publication_id,replay_id=replay,
            scope_id=simulator.data_scope_id,compartment_id='Automatoes',config_version=version)
    metadata=dict(replay_id=replay,scope_id=simulator.data_scope_id,cycle_id=cycle.cycle_id,
        compartment_id='Automatoes',quality_policy_version='AGC_QUALITY_V1',stream_id=replay,
        config_version=version,config=config,config_hash=pin['content_hash'],
        collector_id='collector-'+replay,publisher_id='publisher-'+replay,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    write_json(ROOT/'metadata.json',metadata)
    progress=publish(metadata,partial=True)
    publication,state,events=current(metadata)
    assert len(events)==10 and state['accepted_events']==10 and not state['complete']
    proof=dict(status='passed',phase='partial',backend='clickhouse',replay_id=replay,
        publication_id=publication,scope_id=metadata['scope_id'],accepted_events=10,complete=False,
        source_sha256=metadata['source_sha256'],spark_progress=progress,
        transport='Actual AGC -> SQLite outbox -> MQTT -> collector -> ready files -> Spark -> native publication')
    write_json(ROOT/'partial-result.json',proof);print(json.dumps(proof),flush=True)


def complete():
    metadata=json.loads((ROOT/'metadata.json').read_text())
    if (ROOT/'complete-result.json').exists():raise ValueError('Already completed; do not fake another live transition')
    progress=publish(metadata,partial=False)
    publication,state,events=current(metadata)
    with sqlite3.connect(ROOT/'outbox/outbox.db') as db:
        original={r[0]:json.loads(r[1]) for r in db.execute('SELECT event_id,payload_bytes FROM telemetry_messages')}
    assert len(events)==30 and {e['event_id']:e for e in events}==original
    assert state['complete'] and state['checksum_verified'] and not state['missing_sequences']
    value=fingerprint(build_replay_rows(events,metadata['config']))
    assert value=='7783fdfa2f63909171709dea99b43bb5e6956af13fde3dca15ce4d1030d1f675'
    proof=dict(status='passed',phase='complete',backend='clickhouse',replay_id=metadata['replay_id'],
        publication_id=publication,scope_id=metadata['scope_id'],accepted_events=30,
        original_envelopes_exact=True,complete=True,checksum_verified=True,business_fingerprint=value,
        source_sha256=metadata['source_sha256'],spark_progress=progress)
    write_json(ROOT/'complete-result.json',proof);print(json.dumps(proof),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=('prepare','complete'))
    {'prepare':prepare,'complete':complete}[parser.parse_args().mode]()
