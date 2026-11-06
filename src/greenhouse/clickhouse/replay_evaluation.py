"""Replay BR01/03 through the same typed slots and duration engine, no PG reads."""
from dataclasses import replace
from datetime import datetime,time,timedelta
import json

from greenhouse.clickhouse.quality import measure_verdict
from greenhouse.clickhouse.replay_publication import _read_verified,read_publication
from greenhouse.clickhouse.replay_config import read_config
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schedules import read_rule_schedule
from greenhouse.contracts import CycleWindow,IntradayContext,RuleBasis
from greenhouse.queries.climate import build_slots as climate_slots,CLIMATE_METRICS,CLIMATE_CONTEXT
from greenhouse.queries.drain import build_slots as drain_slots,DRAIN_METRICS,DRAIN_CONTEXT,validate_drain_profile
from greenhouse.queries.rule_duration import evaluate_rule_schedule
from greenhouse.sensor.replay_models import ReplayEvaluation
from greenhouse.sensor.slot_inputs import CODES,prepare_events,assemble_rows


def build_replay_rows(events,config):
    start=datetime.fromisoformat(config['policy_start']);end=datetime.fromisoformat(config['policy_end'])
    prepared=prepare_events(events,**{k:config[k] for k in ('replay_id','scope_id','cycle_id','compartment_id')},
        policy_start=start,policy_end=end,cycle_end=datetime.fromisoformat(config['cycle_end']))
    maps=[]
    for item in prepared:
        stub={'values':item['values'],'issues':item['issues'],'null_reasons':item['nulls']};quality={}
        for code in CODES:
            _,status,app,reason=measure_verdict(config['quality_definition'],stub,code)
            quality[code]={'status':status,'applicability':app,'reason':reason}
        maps.append(quality)
    return assemble_rows(prepared,maps,config['quality_policy_version'])


def evaluate_publication(client,publication_id,*,climate_rule=None,drain_rule=None,climate_basis=RuleBasis.REQUESTED):
    seal,config,meta,events=_read_verified(client,publication_id)
    state=meta['state']
    if state['observed_through'] is None:raise ValueError('Replay cursor has not been observed')
    if climate_basis not in (RuleBasis.REQUESTED,RuleBasis.REALIZED):raise ValueError('Climate basis must be requested or realized')
    from greenhouse.sensor.event_time import normalize_cursor
    observed=normalize_cursor(state['observed_through'])
    cycle_start=datetime.fromisoformat(config['cycle_start']);cycle_end=datetime.fromisoformat(config['cycle_end'])
    end=min(cycle_end,observed+timedelta(minutes=5))
    if end<=cycle_start or observed>cycle_end:raise ValueError('Replay cursor outside cycle window')
    cycle=CycleWindow(config['cycle_id'],config['compartment_id'],cycle_start.date(),cycle_end.date())
    climates,drains=build_replay_rows(events,config)
    climate_context=IntradayContext(config['scope_id'],cycle,(config['compartment_id'],),cycle_start,end,CLIMATE_CONTEXT,climate_basis)
    drain_context=IntradayContext(config['scope_id'],cycle,(config['compartment_id'],),cycle_start,end,DRAIN_CONTEXT,RuleBasis.MEASURED)
    validate_drain_profile(drain_rule)
    climate=evaluate_rule_schedule(climate_context,(climate_rule,) if climate_rule else (),climate_slots(climates,climate_basis),CLIMATE_METRICS)
    drain=evaluate_rule_schedule(drain_context,(drain_rule,) if drain_rule else (),drain_slots(drains),DRAIN_METRICS)
    return ReplayEvaluation(publication_id,config['replay_id'],config['scope_id'],config['quality_policy_version'],observed,end,
        not state['complete'],climate_rule.rule_version if climate_rule else None,drain_rule.rule_version if drain_rule else None,climate,drain)


def map_historical_rule(client,publication_id,rule_version):
    _,config,_,events=_read_verified(client,publication_id)
    if not events:raise ValueError('Cannot map a rule without source provenance')
    revision=read_rule_schedule(client,rule_version)
    if revision is None:raise ValueError('Unknown source rule version')
    sources=[json.loads(p['payload']) for p in Repository(client,None).configurations()['rows'] if p['kind']=='snapshot' and p['scope_id']==revision.scope_id]
    if len(sources)!=1 or sources[0]['snapshot_id']!=config['snapshot_id'] or revision.compartment_id!=config['compartment_id']:
        raise ValueError('Source rule snapshot or compartment does not match replay')
    return replace(revision,scope_id=config['scope_id'],evidence_reference=revision.evidence_reference+f' [replay mapping from {revision.scope_id}]')
