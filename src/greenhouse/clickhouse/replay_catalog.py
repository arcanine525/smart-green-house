"""Replay catalog reads one verified immutable ID per current scope/replay."""
from datetime import datetime
import json

from greenhouse.clickhouse.replay_publication import _read_verified,read_publication
from greenhouse.clickhouse.replay_evaluation import build_replay_rows
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schedules import catalog_rules
from greenhouse.contracts import RuleBasis


def current_replays(client):
    if not int(client.query('EXISTS TABLE replay_pointer')[0]['result']):return ()
    pointers=client.query('SELECT DISTINCT * FROM replay_pointer ORDER BY scope_id,replay_id,generation')
    by_key={}
    for p in pointers:
        key=(p['scope_id'],p['replay_id']);old=by_key.get(key)
        if old and int(old['generation'])==int(p['generation']) and old['publication_id']!=p['publication_id']:
            raise ValueError('Conflicting replay current generation')
        if old is None or int(old['generation'])<int(p['generation']):by_key[key]=p
    result=[]
    for pointer in by_key.values():
        pub=pointer['publication_id'];seal,config,meta,_=_read_verified(client,pub);metadata,_=read_publication(client,pub)
        if (meta['scope_id'],meta['replay_id'])!=(pointer['scope_id'],pointer['replay_id']):raise ValueError('Replay pointer scope differs from seal')
        received=metadata[7];observed=metadata[6];state=metadata[8]
        result.append((metadata[0],pub,metadata[1],metadata[2],metadata[3],observed,received,state,datetime.fromisoformat(seal['created_at'])))
    return tuple(sorted(result,key=lambda r:(r[8],r[0]),reverse=True))


def source_rules(client,snapshot_id,compartment_id,context,basis):
    if context not in ('climate','drain_process_computer'):raise ValueError('Unknown replay rule context')
    metrics=('temperature','co2') if context=='climate' else ('ec','ph')
    bases=(RuleBasis.REQUESTED,RuleBasis.REALIZED) if context=='climate' else (RuleBasis.MEASURED,)
    scopes={r['scope_id'] for r in Repository(client,None).configurations()['rows'] if r['kind']=='snapshot' and json.loads(r['payload'])['snapshot_id']==snapshot_id}
    options=[]
    for scope in sorted(scopes):
        for rule in catalog_rules(client,scope,compartment_id,measurement_context=context,metrics=metrics,bases=bases):
            if any(k[2].value==basis for k in rule.keys):options.append((rule.rule_version,rule.label,rule.is_demo,rule.evidence_reference))
    return tuple(sorted(options))


def latest_quality(client,publication_id,event):
    _,config,_,events=_read_verified(client,publication_id)
    if not any(e==event for e in events):raise ValueError('Quality event absent from pinned replay membership')
    climate,_=build_replay_rows((event,),config)
    return tuple({'measure':code,**verdict} for code,verdict in sorted(climate[0].measure_quality.items()))
