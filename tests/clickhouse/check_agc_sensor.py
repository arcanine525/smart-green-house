"""Actual locked AGC file → MQTT → durable collector → Spark → candidate replay.

The simulator's explicit 30-event selection retains the original full file
checksum. This proves a bounded replay, not all 47,809 file rows or live devices.
"""
import argparse
from datetime import datetime,time,timedelta
import json
import os
from pathlib import Path
import sys
import threading
import time as walltime
from uuid import uuid4

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.replay_config import from_historical
from greenhouse.clickhouse.replay_publication import read_publication
from greenhouse.clickhouse.replay_evaluation import evaluate_publication,map_historical_rule,build_replay_rows
from greenhouse.clickhouse.threshold import ClimateAdapter,DrainAdapter,CLIMATE,DRAIN
from greenhouse.clickhouse.schedules import read_rule_schedule
from greenhouse.clickhouse.stream_store import StreamSettings
from greenhouse.clickhouse.water_run import AGC_SNAPSHOT,AGC_SHA
from greenhouse.contracts import QueryContext,CycleWindow,IntradayContext,RuleBasis,to_dict
from greenhouse.queries.climate import build_slots as climate_slots
from greenhouse.queries.drain import build_slots as drain_slots
from greenhouse.queries.rule_duration import evaluate_rule_schedule
from greenhouse.sensor.simulator import SensorSimulator
from greenhouse.sensor.collector import MqttCollector
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.stream_landing import start_landing

sys.path.insert(0,str(Path(__file__).resolve().parent))
from threshold_oracle import source_rows,evaluate as raw_evaluate


def main():
    from pyspark.sql import SparkSession
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--replay-id',default='agc-native-proof-30')
    for name in ('raw','state','coordinator','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();args.state.mkdir(parents=True,exist_ok=True)
    if any(os.environ.get(k) for k in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD')):raise ValueError('This proof must not use PostgreSQL settings')
    replay=args.replay_id;stream=replay+'-stream';version=replay+'-config-v1'
    checkpoint_file=args.state/'outbox/replay_state.json'
    if checkpoint_file.exists() and json.loads(checkpoint_file.read_text()).get('status')=='completed':
        raise ValueError('Proof selection already completed; use a new --replay-id and --state for another fresh MQTT run')
    print(json.dumps({'stage':'historical_publication_verification_started','publication_id':args.publication_id}),flush=True)
    client=Client(ClickHouseSettings.from_env()).begin_action();adapter=ClimateAdapter(client)
    historical=adapter.publication(args.publication_id)
    if historical.snapshot_id!=AGC_SNAPSHOT:raise ValueError('Locked AGC publication required')
    cycle=next(c for c in adapter.catalog_cycles(historical.publication_id) if c.compartment_id=='Automatoes')
    simulator=SensorSimulator(args.raw/'Automatoes/GreenhouseClimate.csv',replay,args.state/'outbox',compartment_id='Automatoes',
        cycle_id=cycle.cycle_id,snapshot_id=AGC_SNAPSHOT,snapshot_sha=AGC_SHA,relative_path='Automatoes/GreenhouseClimate.csv',limit=30,cursor_interval=5)
    resumed=(args.state/'outbox/replay_state.json').exists()
    simulation=simulator.run(resume=resumed)
    if simulation.expected_unique_events!=30 or simulation.status!='completed':raise ValueError('Bounded AGC selection must complete exactly 30 events')
    config,_=from_historical(client,args.coordinator,historical.publication_id,replay_id=replay,scope_id=simulator.data_scope_id,
        compartment_id=cycle.compartment_id,config_version=version)
    options={k:config[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')}
    start=datetime.combine(cycle.start,time.min);end=start+timedelta(minutes=150)
    q=QueryContext(historical.scope_id,historical.publication_id,AGC_SNAPSHOT,cycle.cycle_id,('Automatoes',),cycle.start,cycle.start+timedelta(days=1))
    crows=adapter.read_climate_slots(q,historical,window_start=start,window_end=end)
    drows=DrainAdapter(client).read_drain_slots(q,historical,window_start=start,window_end=end)
    rules={kind:read_rule_schedule(client,'threshold-agc-demo:'+kind+':Automatoes:v1') for kind in ('climate','drain')}
    if any(r is None for r in rules.values()):raise ValueError('Explicit historical validation rules required')
    native_cycle=CycleWindow(cycle.cycle_id,cycle.compartment_id,cycle.start,cycle.end)
    contexts={kind:IntradayContext(historical.scope_id,native_cycle,('Automatoes',),start,end,
        'climate' if kind=='climate' else 'drain_process_computer',RuleBasis.REQUESTED if kind=='climate' else RuleBasis.MEASURED) for kind in rules}
    batch={'climate':evaluate_rule_schedule(contexts['climate'],(rules['climate'],),climate_slots(crows,RuleBasis.REQUESTED),('temperature','co2')),
        'drain':evaluate_rule_schedule(contexts['drain'],(rules['drain'],),drain_slots(drows),('ec','ph'))}
    raw=source_rows(args.raw/'Automatoes/GreenhouseClimate.csv',start,end)
    client.close() # Do not retain the historical proof's large spool during Spark startup.
    ready=args.state/'ready';ready.mkdir(exist_ok=True)
    collector=MqttCollector(spool_dir=args.state/'spool',ready_dir=ready,mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),
        client_id='agc-collector-'+uuid4().hex[:12],clean_session=False,batch_size=5,batch_timeout=.2)
    publisher=OutboxPublisher(outbox_dir=args.state/'outbox',mqtt_host=os.environ.get('MQTT_HOST','ch-broker'),client_id='agc-publisher-'+uuid4().hex[:12],max_inflight=10)
    errors=[]
    def collect():
        try:collector.start(poll_interval=.05)
        except Exception as error:errors.append(error)
    def send():
        try:publisher.publish_all_pending(poll_interval=.05)
        except Exception as error:errors.append(error)
    receiver=threading.Thread(target=collect,daemon=True);sender=threading.Thread(target=send,daemon=True);query=None
    try:
        print(json.dumps({'stage':'agc_sensor_source_ready','selection_limit':30,'source_snapshot':AGC_SNAPSHOT,'resume':resumed}),flush=True)
        query=start_landing(StreamSettings(client.settings,args.coordinator,version),ready,args.state/'checkpoint',stream,replay+'-generation-v1',
            publication_options=options,max_files_per_batch=20)
        receiver.start();deadline=walltime.monotonic()+15
        while walltime.monotonic()<deadline and not (collector._client and collector._client.is_connected()):walltime.sleep(.05)
        if not (collector._client and collector._client.is_connected()):raise ValueError('Collector did not connect')
        publisher.start();sender.start();deadline=walltime.monotonic()+180
        publication=None
        while walltime.monotonic()<deadline:
            if errors:raise RuntimeError(type(errors[0]).__name__)
            pointers=client.query('SELECT DISTINCT publication_id,generation FROM replay_pointer WHERE scope_id={scope:String} ORDER BY generation DESC',{'scope':simulator.data_scope_id})
            if pointers:
                candidate=pointers[0]['publication_id'];metadata,events=read_publication(client,candidate)
                if metadata[-1]['complete'] and len(events)==30:publication=candidate;break
            walltime.sleep(.2)
        if publication is None:raise ValueError('AGC MQTT/Spark replay did not reconcile by deadline')
        replay_result=evaluate_publication(client,publication,climate_rule=map_historical_rule(client,publication,rules['climate'].rule_version),
            drain_rule=map_historical_rule(client,publication,rules['drain'].rule_version))
        details=[]
        for kind in ('climate','drain'):
            actual=getattr(replay_result,kind);expected=batch[kind]
            if actual.metric_results!=expected.metric_results or actual.episodes!=expected.episodes or actual.joint_results!=expected.joint_results:
                raise ValueError('Actual AGC batch/stream metrics, episodes or joint coverage differ')
            raw_metrics,raw_episodes=raw_evaluate(raw,start,end,kind,'requested' if kind=='climate' else 'measured',include_boundary_episodes=True)
            fields=('metric','expected_minutes','valid_minutes','unknown_minutes','out_minutes','suspect_minutes','out_low_minutes','out_high_minutes')
            metrics=[{field:str(getattr(r,field)) for field in fields} for r in actual.metric_results]
            episodes=[[e.metric,e.original_start.isoformat(),e.original_end.isoformat(),str(e.original_minutes)] for e in actual.episodes]
            if metrics!=raw_metrics or episodes!=raw_episodes:raise ValueError('Actual AGC sensor differs from independent raw oracle')
            details.append({'kind':kind,'metrics':metrics,'episodes':episodes,'joint':to_dict(actual.joint_results)})
        replay_c,replay_d=build_replay_rows(events,config)
        for historical_rows,replay_rows,mapping in ((crows,replay_c,CLIMATE),(drows,replay_d,DRAIN)):
            if len(historical_rows)!=len(replay_rows):raise ValueError('AGC replay quality row count differs')
            for before,after in zip(historical_rows,replay_rows,strict=True):
                # Replay rows carry all ten measure verdicts for both views;
                # each historical mart carries its own eight/two. Compare all
                # ten exactly across the two views, including applicability and
                # reason, rather than comparing maps with different domains.
                if set(after.measure_quality)!=set(CLIMATE)|set(DRAIN):raise ValueError('Replay quality measure set differs')
                quality={code:after.measure_quality[code] for code in mapping}
                if before.slot_start!=after.slot_start or before.measure_quality!=quality:raise ValueError('AGC replay per-measure quality differs')
                if before.quality_policy_version!=after.quality_policy_version:raise ValueError('AGC replay policy differs')
                if any(getattr(before,field)!=getattr(after,field) for field in mapping.values()):raise ValueError('AGC replay measure value differs')
        result={'status':'passed','selection_limit':30,'source_events':30,'full_file_replay':False,'source_snapshot':AGC_SNAPSHOT,
            'source_file_checksum':simulator.file_checksum,'historical_publication':historical.publication_id,'replay_publication':publication,
            'complete':metadata[-1]['complete'],'checksum_verified':metadata[-1]['checksum_verified'],'missing_sequences':metadata[-1]['missing_sequences'],
            'quarantined':metadata[-1]['quarantined'],'expected_minutes':'150',
            'batch_stream_raw_metrics_episodes_quality_match':True,'ready_files':len(list(ready.glob('batch-*.jsonl'))),'details':details}
        args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n');print(json.dumps({'status':'passed','source_events':30,'replay_publication':publication}),flush=True)
    finally:
        publisher.stop();collector.stop()
        if receiver.ident:receiver.join(timeout=5)
        if sender.ident:sender.join(timeout=5)
        if query:query.stop()
        spark=SparkSession.getActiveSession()
        if spark:spark.stop()


if __name__=='__main__':main()
