"""Real AGC MQTT/Spark publish-to-verified-queryable timing on private benchmark DBs.

Thirty events form one burst, not thirty independent replay trials. Spark starts
before sending; setup/proof time and poll interval are reported separately.
"""
import argparse
from datetime import datetime,timezone
import hashlib,json,math,os,sqlite3,statistics,threading,time
from pathlib import Path
from uuid import uuid4

from greenhouse.sensor.collector import MqttCollector
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.simulator import SensorSimulator,STANDARD_AGC_SHA
from greenhouse.sensor.stream_landing import start_landing
from greenhouse.app import wiring
from read_business_reference import fingerprint


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend',choices=('postgres','clickhouse'),required=True)
    for name in ('state','raw','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--publication-id',required=True);p.add_argument('--coordinator',type=Path)
    p.add_argument('--reference',type=Path)
    a=p.parse_args();a.state.mkdir(parents=True,exist_ok=False)
    setup=time.monotonic();replay='bench-sensor-'+uuid4().hex[:12]
    if a.backend=='postgres':
        import psycopg
        from greenhouse.settings import Settings
        settings=Settings.from_env()
        if (settings.pg_host,settings.pg_database,settings.pg_user)!=('postgres','greenhouse_bench_pg','greenhouse_bench'):
            raise ValueError('Private benchmark endpoint required')
        connection=psycopg.connect(**settings.connection_kwargs(),autocommit=True)
    else:
        from greenhouse.clickhouse.client import Client,ClickHouseSettings
        from greenhouse.clickhouse.stream_store import StreamSettings
        from greenhouse.clickhouse.replay_config import from_historical
        s=ClickHouseSettings.from_env()
        if (s.host,s.database,s.user)!=('clickhouse','greenhouse_bench_ch','greenhouse_bench') or a.coordinator is None:
            raise ValueError('Private benchmark endpoint and coordinator required')
        connection=Client(s).begin_action(shared_proofs=True)
    pub=wiring.energy_service(connection).adapter.publication(a.publication_id)
    cycle=next(c for c in wiring.catalog_service(connection).cycles(pub) if c.compartment_id=='Automatoes')
    source=a.raw/'Automatoes/GreenhouseClimate.csv'
    simulator=SensorSimulator(source,replay,a.state/'outbox',compartment_id='Automatoes',cycle_id=cycle.cycle_id,
        snapshot_id=pub.snapshot_id,snapshot_sha=STANDARD_AGC_SHA,relative_path='Automatoes/GreenhouseClimate.csv',limit=30,cursor_interval=5)
    simulation=simulator.run()
    if simulation.expected_unique_events!=30 or simulation.status!='completed':raise ValueError('Expected exactly thirty AGC events')
    with sqlite3.connect(a.state/'outbox/outbox.db') as db:
        original={r[0]:json.loads(r[1]) for r in db.execute('SELECT event_id,payload_bytes FROM telemetry_messages')}
    if len(original)!=30:raise ValueError('Expected thirty unique source event identities')
    options=dict(replay_id=replay,scope_id=simulator.data_scope_id,cycle_id=cycle.cycle_id,
        compartment_id='Automatoes',quality_policy_version='AGC_QUALITY_V1')
    if a.backend=='clickhouse':
        config,unused=from_historical(connection,a.coordinator,pub.publication_id,replay_id=replay,
            scope_id=simulator.data_scope_id,compartment_id='Automatoes',config_version=replay+'-v1')
        settings=StreamSettings(connection.settings,a.coordinator,replay+'-v1')
    ready=a.state/'ready';ready.mkdir()
    collector=MqttCollector(spool_dir=a.state/'spool',ready_dir=ready,mqtt_host='broker',
        client_id='collector-'+replay,clean_session=False,batch_size=5,batch_timeout=.2)
    publisher=OutboxPublisher(outbox_dir=a.state/'outbox',mqtt_host='broker',client_id='publisher-'+replay,max_inflight=10)
    errors=[];sent={};visible={};utc_sent={};utc_visible={};query=None
    def receive():
        try:collector.start(poll_interval=.05)
        except Exception as e:errors.append(e)
    def send():
        try:publisher.publish_all_pending(poll_interval=.05)
        except Exception as e:errors.append(e)
    original_publish=publisher.publish_single_item
    def timed_publish(item):
        if item.outbox_table=='telemetry_messages':
            sent.setdefault(item.event_id,time.monotonic());utc_sent.setdefault(item.event_id,datetime.now(timezone.utc).isoformat())
        return original_publish(item)
    publisher.publish_single_item=timed_publish
    receiver=threading.Thread(target=receive,daemon=True);sender=threading.Thread(target=send,daemon=True)
    try:
        query=start_landing(settings,ready,a.state/'checkpoint',replay+'-stream','v1',publication_options=options,max_files_per_batch=20)
        receiver.start();deadline=time.monotonic()+15
        while time.monotonic()<deadline and not (collector._client and collector._client.is_connected()):time.sleep(.05)
        if not (collector._client and collector._client.is_connected()):raise ValueError('Collector not connected')
        time.sleep(.5);publisher.start();setup_seconds=time.monotonic()-setup;sender.start();deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            if errors:raise RuntimeError(type(errors[0]).__name__)
            if not query.isActive:raise RuntimeError(str(query.exception()))
            if a.backend=='postgres':
                from greenhouse.sensor.replay_publication import read_publication
                pointer=connection.execute('SELECT publication_id FROM sensor_stream.current_publication WHERE replay_id=%s',(replay,)).fetchone()
                publication=pointer[0] if pointer else None
                if publication:metadata,events=read_publication(connection,publication)
            else:
                from greenhouse.clickhouse.replay_publication import read_publication
                pointer=connection.query('SELECT DISTINCT publication_id,generation FROM replay_pointer WHERE scope_id={scope:String} ORDER BY generation DESC',{'scope':simulator.data_scope_id})
                publication=pointer[0]['publication_id'] if pointer else None
                if publication:metadata,events=read_publication(connection,publication)
            if publication:
                now=time.monotonic();stamp=datetime.now(timezone.utc).isoformat()
                for event in events:
                    identity=event['event_id']
                    if original.get(identity)!=event:raise ValueError('Sealed event differs from actual source outbox bytes')
                    if identity not in sent:raise ValueError('Queryable event has no observed publisher call')
                    visible.setdefault(identity,now);utc_visible.setdefault(identity,stamp)
                state=metadata[-1]
                if state['complete'] and len(visible)==30 and not sender.is_alive():break
            time.sleep(.1)
        else:raise ValueError('Timed out waiting for verified complete replay')
        if set(visible)!=set(sent) or not state['checksum_verified'] or state['missing_sequences'] or state['quarantined']:
            raise ValueError('Sensor reconciliation differs')
        # Cross-engine correctness ignores replay-specific provenance identifiers,
        # comparing actual value/quality DTOs from the native slot constructors.
        if a.backend=='postgres':
            from greenhouse.sensor.replay_slots import build_replay_rows
            rows=build_replay_rows(connection,events,**options)
        else:
            from greenhouse.clickhouse.replay_evaluation import build_replay_rows
            rows=build_replay_rows(events,config)
        checksum=fingerprint(rows)
        if a.reference and json.loads(a.reference.read_text())['business_fingerprint']!=checksum:raise ValueError('Cross-engine sensor slot/value/quality differs')
        samples=[{'event_id':k,'publisher_call_utc':utc_sent[k],'queryable_observed_utc':utc_visible[k],
            'latency_ms':(visible[k]-sent[k])*1000} for k in sorted(sent)]
        values=sorted(r['latency_ms'] for r in samples)
        report=dict(status='measured',backend=a.backend,replay_id=replay,publication_id=publication,
            source_snapshot=pub.snapshot_id,source_file_checksum=hashlib.file_digest(source.open('rb'),'sha256').hexdigest(),
            selection_events=30,full_file_replay=False,independent_replay_trials=1,sample_unit='event in one burst; correlated',
            setup_including_spark_and_historical_proof_seconds=setup_seconds,poll_interval_seconds=.1,
            source_bytes_match=True,complete=state['complete'],checksum_verified=state['checksum_verified'],
            business_fingerprint=checksum,cross_engine_reference_matched=a.reference is not None,
            p50_ms=statistics.median(values),p95_ms=values[math.ceil(.95*len(values))-1],samples=samples)
        a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:report[k] for k in ('status','backend','p50_ms','p95_ms','publication_id')}),flush=True)
    finally:
        publisher.stop();collector.stop()
        if sender.ident:sender.join(timeout=5)
        if receiver.ident:receiver.join(timeout=5)
        if query:query.stop()
        from pyspark.sql import SparkSession
        spark=SparkSession.getActiveSession()
        if spark:spark.stop()
        connection.close()


if __name__=='__main__':main()
