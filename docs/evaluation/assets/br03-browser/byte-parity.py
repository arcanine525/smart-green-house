"""Recompute the pinned final browser downloads and compare every byte."""
from datetime import datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import psycopg
from greenhouse.app import wiring, drain_actions, drain_exports
from greenhouse.contracts import DrainEvaluation, from_json
from greenhouse.queries import page_episodes
from greenhouse.settings import Settings

folder = Path('/evidence')
with psycopg.connect(**Settings.from_env().connection_kwargs(),
                     options='-c default_transaction_read_only=on') as conn:
    catalog = wiring.catalog_service(conn)
    service = wiring.drain_service(conn)
    # Saved exports pin a sealed publication, which may no longer be current.
    # Resolve it directly; current-only UI discovery must not gate replay.
    publication = service.adapter.publication(os.environ['EXPECTED_PUBLICATION'])
    cycle = next(cycle for cycle in catalog.adapter.catalog_cycles(publication.publication_id)
                 if cycle.compartment_name == 'Reference')
    rule = f'drain-demo-b03:{cycle.compartment_id}:v1'
    context = wiring.build_context(publication, cycle, cycle.start, cycle.end - timedelta(days=1), rule)
    evaluation = service.get_evaluation(context, rule_version=rule)
    shown, total = page_episodes(evaluation.result, metric_order=('ec','ph'), limit=500)
    ec, _ = page_episodes(evaluation.result, metric='ec', metric_order=('ec','ph'), limit=40)
    episode = ec[0]
    assert episode.original_minutes == 80400
    trace, _, _ = drain_actions.trace_page(service, context, rule, episode.original_start, episode.original_end)
    source = service.slot_facts(context, window_start=datetime(2019,12,30,13,55),
        window_end=datetime.combine(context.end,time.min), max_slots=12)
    water = drain_actions.daily_water_context(wiring.irrigation_service(conn), evaluation,
        episode.original_start,episode.original_end)
    # Literal raw-derived answer for the global Reference EC case; not just
    # a second call to the same serializer or a sum over the returned rows.
    assert len(water.action.days) == 57
    assert water.action.summary.irrigation_observed_l_m2 == Decimal('212.4')
    assert water.action.summary.drain_observed_l_m2 == Decimal('85.6')
    stem = f'drain-Reference-{context.start.isoformat()}-{context.end.isoformat()}'
    expected = {
        'summary.csv': drain_exports.summary_csv(evaluation),
        'joint.csv': drain_exports.joint_csv(evaluation),
        'episodes.csv': drain_exports.episodes_csv(evaluation,shown,total=total,metric=None,min_minutes=Decimal('0')),
        'result.json': drain_exports.result_json(evaluation),
        'water.csv': drain_exports.water_csv(evaluation,water),
        'source.csv': drain_exports.source_csv(evaluation,source),
        'detail.csv': drain_exports.detail_csv(evaluation,trace),
    }
    checks=[]
    for suffix,data in expected.items():
        name=stem+'-'+suffix
        actual=(folder/name).read_bytes()
        checks.append({'file':name,'bytes':len(actual),
            'sha256':hashlib.sha256(actual).hexdigest(),'match':actual==data})
    assert from_json(DrainEvaluation,(folder/(stem+'-result.json')).read_bytes().decode()) == evaluation
    rows=drain_exports.source_rows(source)
    assert rows[0]['ph']==0 and rows[0]['ph_status']=='suspect'
    assert rows[0]['ec']==Decimal('0.1') and rows[0]['ec_status']=='accepted'
    result={'publication':publication.publication_id,'rule':rule,'checks':checks,
        'all_match':all(check['match'] for check in checks),'typed_json_roundtrip':True,
        'trace_slots':trace.window_slots,'source_rows':len(source.rows),
        'water_days':len(water.action.days),
        'irrigation_observed_l_m2':str(water.action.summary.irrigation_observed_l_m2),
        'drain_observed_l_m2':str(water.action.summary.drain_observed_l_m2)}
    (folder/'byte-parity.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    assert result['all_match']
