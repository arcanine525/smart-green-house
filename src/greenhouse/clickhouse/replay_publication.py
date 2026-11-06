"""Immutable replay members + verified seal; current pointer is append-only."""
from datetime import datetime,timezone
from dataclasses import replace
import json

from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.encoding import canonical_json,digest,source_clock_us
from greenhouse.clickhouse.journal import execute_write
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.replay_config import read_config
from greenhouse.clickhouse.stream_store import receipts,verify_batch,_commit_locked
from greenhouse.sensor.event_time import event_slot,normalize_cursor
from greenhouse.sensor.publication_plan import plan_publication
from greenhouse.sensor.replay_state import reconcile
from greenhouse.sensor.schema import SCHEMA_VERSION
from greenhouse.sensor.stream_records import decode_ready_record

DDL=(
    '''CREATE TABLE IF NOT EXISTS replay_member (
        publication_id FixedString(64), replay_id String, identity String,
        payload_hash FixedString(64), source_record_id String, slot_clock_us Int64, event String
    ) ENGINE=MergeTree ORDER BY (publication_id,identity,payload_hash)''',
    '''CREATE TABLE IF NOT EXISTS replay_seal (
        publication_id FixedString(64), replay_id String, scope_id String, config_version String,
        config_hash FixedString(64), cursor_sequence UInt64, observed_clock_us Nullable(Int64),
        created_at String, payload String, seal_hash FixedString(64)
    ) ENGINE=MergeTree ORDER BY (scope_id,replay_id,publication_id)''',
    '''CREATE TABLE IF NOT EXISTS replay_pointer (
        replay_id String, scope_id String, generation UInt64, publication_id FixedString(64)
    ) ENGINE=MergeTree ORDER BY (scope_id,replay_id,generation,publication_id)''',
)


def migrate(client,coordinator):
    with writer_lock(coordinator,client):
        for sql in DDL:execute_write(client,coordinator,sql)


def normalize_receipt(row):
    return dict(row,batch_id=int(row['batch_id']),rows=int(row['rows']))


def selection(records,config):
    state=reconcile(config['replay_id'],records);variants={}
    orphan_quarantine=sum(1 for row in records if (not isinstance(row,dict) or row.get('replay_id')!=config['replay_id'])
        and decode_ready_record(row).disposition!='accepted')
    if orphan_quarantine:
        # The stream/config scope is authoritative even for a malformed ready
        # line without a decodable replay ID. Preserve it as quarantine, no value.
        state=replace(state,quarantined=state.quarantined+orphan_quarantine,complete=False)
    for row in records:
        result=decode_ready_record(row)
        if result.disposition=='accepted' and result.replay_id==config['replay_id']:
            variants.setdefault(result.identity,{})[result.payload_hash]=result.payload
    eligible=[];source_conflicts=[]
    for identity,values in sorted(variants.items()):
        if len(values)!=1:continue
        hashed,event=next(iter(values.items()))
        if event['schema_version']!=SCHEMA_VERSION:continue
        source=event['source_provenance']
        if any(source[k]!=config[k] for k in ('snapshot_id','snapshot_sha','source_file','file_checksum')):
            source_conflicts.append(identity)
        else:eligible.append((identity,hashed,event))
    _,meta,members=plan_publication(state,eligible,
        tuple(datetime.fromisoformat(config[k]) for k in ('cycle_start','cycle_end')),
        tuple(datetime.fromisoformat(config[k]) for k in ('policy_start','policy_end')),
        **{k:config[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')})
    if source_conflicts:
        meta['state']['scope_conflicts']=sorted(set(meta['state']['scope_conflicts'])|set(source_conflicts))
        meta['state']['complete']=False
    by_identity={(i,h):e for i,h,e in eligible}
    selected=[(identity,hashed,by_identity[identity,hashed]) for identity,hashed in members]
    return json.loads(canonical_json(meta)),selected


def member_rows(publication,replay,selected):
    return [dict(publication_id=publication,replay_id=replay,identity=identity,payload_hash=hashed,
        source_record_id=event['source_record_id'],slot_clock_us=source_clock_us(event_slot(event)),event=canonical_json(event))
        for identity,hashed,event in selected]


def _read_verified(client,publication):
    rows=client.query('SELECT DISTINCT * FROM replay_seal WHERE publication_id={pub:String}',{'pub':publication})
    if len(rows)!=1:raise ValueError('Unknown/unsealed or conflicting replay publication')
    seal=rows[0];body=json.loads(seal['payload'])
    if digest(body)!=seal['seal_hash']:raise ValueError('Replay seal checksum mismatch')
    config,pin=read_config(client,seal['scope_id'],seal['config_version'])
    if pin['content_hash']!=seal['config_hash'] or body['configuration_hash']!=seal['config_hash']:
        raise ValueError('Replay configuration pin mismatch')
    source_records=[]
    for wanted in body['batches']:
        found=[normalize_receipt(r) for r in receipts(client,stream=wanted['stream_id'],generation=wanted['generation']) if int(r['batch_id'])==wanted['batch_id']]
        if found!=[wanted] or wanted['scope_id']!=seal['scope_id'] or wanted['config_hash']!=seal['config_hash']:
            raise ValueError('Replay source batch receipt missing/changed')
        source_records.extend(verify_batch(client,found[0]))
    meta,selected=selection(source_records,config)
    expected_id=digest({'metadata':meta,'configuration_hash':pin['content_hash'],'configuration_version':pin['version_id']})
    observed=normalize_cursor(meta['state']['observed_through']) if meta['state']['observed_through'] else None
    if (expected_id!=publication or body['metadata']!=meta or int(seal['cursor_sequence'])!=meta['state']['cursor_sequence']
            or (int(seal['observed_clock_us']) if seal['observed_clock_us'] is not None else None)!=source_clock_us(observed)
            or seal['replay_id']!=config['replay_id']):
        raise ValueError('Replay semantic metadata differs from pinned source batches')
    members=client.query('SELECT DISTINCT * FROM replay_member WHERE publication_id={pub:String} ORDER BY identity,payload_hash',{'pub':publication})
    members=[dict(r,slot_clock_us=int(r['slot_clock_us'])) for r in members]
    expected=member_rows(publication,config['replay_id'],selected)
    if members!=expected or digest(expected)!=body['members_hash']:
        raise ValueError('Replay membership differs from verified seal')
    return seal,config,meta,tuple(event for _,_,event in sorted(selected,key=lambda r:(event_slot(r[2]),r[0])))


def read_publication(client,publication_id):
    seal,config,meta,events=_read_verified(client,publication_id);state=meta['state']
    observed=normalize_cursor(state['observed_through']) if state['observed_through'] else None
    received=datetime.fromisoformat(state['received_through']) if state['received_through'] else None
    return (meta['replay_id'],meta['scope_id'],meta['cycle_id'],meta['compartment_id'],meta['quality_policy_version'],
        state['cursor_sequence'],observed,received,state),events


def _advance_locked(client,repo,replay,scope,publication):
    pointers=client.query('SELECT DISTINCT generation,publication_id FROM replay_pointer WHERE replay_id={replay:String} AND scope_id={scope:String}',{'replay':replay,'scope':scope})
    maximum=max((int(p['generation']) for p in pointers),default=0)
    latest=[p for p in pointers if int(p['generation'])==maximum]
    if len(latest)>1:raise ValueError('Conflicting replay pointer generation')
    if latest and latest[0]['publication_id']==publication:return
    repo._insert('replay_pointer',[dict(replay_id=replay,scope_id=scope,generation=maximum+1,publication_id=publication)])


def _publish_locked(client,coordinator,*,config_version,replay_id,scope_id,cycle_id,compartment_id,quality_policy_version):
    config,pin=read_config(client,scope_id,config_version)
    options=locals()
    if any(config[k]!=options[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')):
        raise ValueError('Replay publication options differ from pinned configuration')
    batches=[normalize_receipt(r) for r in receipts(client,scope=scope_id) if r['config_version']==config_version]
    records=[]
    for receipt in batches:records.extend(verify_batch(client,receipt))
    meta,selected=selection(records,config)
    publication=digest({'metadata':meta,'configuration_hash':pin['content_hash'],'configuration_version':pin['version_id']});repo=Repository(client,coordinator)
    existing=client.query('SELECT publication_id FROM replay_seal WHERE publication_id={pub:String} LIMIT 1',{'pub':publication})
    if not existing:
        members=member_rows(publication,replay_id,selected)
        for offset in range(0,len(members),512):repo._insert('replay_member',members[offset:offset+512])
        body={'metadata':meta,'configuration_hash':pin['content_hash'],'batches':batches,'members_hash':digest(members)}
        observed=normalize_cursor(meta['state']['observed_through']) if meta['state']['observed_through'] else None
        repo._insert('replay_seal',[dict(publication_id=publication,replay_id=replay_id,scope_id=scope_id,config_version=config_version,
            config_hash=pin['content_hash'],cursor_sequence=meta['state']['cursor_sequence'],observed_clock_us=source_clock_us(observed),
            created_at=datetime.now(timezone.utc).isoformat(),payload=canonical_json(body),seal_hash=digest(body))])
    _read_verified(client,publication)
    _advance_locked(client,repo,replay_id,scope_id,publication)
    return publication


def publish_replay(client,coordinator,**options):
    with writer_lock(coordinator,client):return _publish_locked(client,coordinator,**options)


def commit_and_publish(client,coordinator,stream,generation,batch,rows,*,config_version,publication_options):
    with writer_lock(coordinator,client):
        created=_commit_locked(client,coordinator,stream,generation,batch,rows,publication_options['scope_id'],config_version)
        publication=_publish_locked(client,coordinator,config_version=config_version,**publication_options)
        return created,publication
