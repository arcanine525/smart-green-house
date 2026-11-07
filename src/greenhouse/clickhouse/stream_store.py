"""Journaled collector micro-batches. Only verified receipts expose deliveries."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from greenhouse.clickhouse.client import ClickHouseSettings
from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.encoding import canonical_json,digest
from greenhouse.clickhouse.journal import execute_write
from greenhouse.clickhouse.repository import Repository
from greenhouse.sensor.stream_records import decode_ready_record

DDL=(
    '''CREATE TABLE IF NOT EXISTS stream_delivery (
        stream_id String, generation String, batch_id UInt64, ordinal UInt64,
        scope_id String, config_version String, record String, disposition LowCardinality(String),
        reason Nullable(String), replay_id Nullable(String), identity Nullable(String), payload_hash Nullable(String)
    ) ENGINE=MergeTree ORDER BY (stream_id,generation,batch_id,ordinal)''',
    '''CREATE TABLE IF NOT EXISTS stream_batch (
        stream_id String, generation String, batch_id UInt64, scope_id String,
        config_version String, config_hash FixedString(64), content_sha256 FixedString(64), rows UInt64
    ) ENGINE=MergeTree ORDER BY (stream_id,generation,batch_id)''',
)


@dataclass(frozen=True)
class StreamSettings:
    database:ClickHouseSettings
    coordinator:Path
    config_version:str


def migrate(client,coordinator):
    with writer_lock(coordinator,client):
        for sql in DDL:execute_write(client,coordinator,sql)


def config_pin(client,scope,version):
    pins=[r for r in Repository(client,None).configurations()['rows'] if (r['kind'],r['scope_id'],r['version_id'])==('replay',scope,version)]
    if len(pins)!=1:raise ValueError('Unknown replay configuration')
    return pins[0]


def encoded_records(rows):
    # Transport audit can include malformed finite JSON numbers. It is not a
    # Decimal KPI payload. Match the existing Spark/PG batch digest byte-for-byte.
    return sorted(json.dumps(r,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False) for r in rows)


def content_hash(encoded):
    return hashlib.sha256(('\n'.join(encoded)+'\n').encode()).hexdigest()


def _key(stream,generation,batch):
    return {'stream':stream,'gen':generation,'batch':batch}


def verify_batch(client,receipt):
    key=_key(receipt['stream_id'],receipt['generation'],int(receipt['batch_id']))
    rows=client.query('''SELECT DISTINCT * FROM stream_delivery WHERE stream_id={stream:String}
        AND generation={gen:String} AND batch_id={batch:UInt64} ORDER BY ordinal''',key)
    if len(rows)!=int(receipt['rows']) or [int(r['ordinal']) for r in rows]!=list(range(len(rows))):
        raise ValueError('Stream batch delivery grain/count differs from receipt')
    encoded=[]
    for row in rows:
        if (row['scope_id'],row['config_version'])!=(receipt['scope_id'],receipt['config_version']):raise ValueError('Mixed stream configuration')
        result=decode_ready_record(json.loads(row['record']))
        if (row['disposition'],row['reason'],row['replay_id'],row['identity'],row['payload_hash'])!=(result.disposition,result.reason,result.replay_id,result.identity,result.payload_hash):
            raise ValueError('Stream delivery verdict differs from original raw bytes')
        encoded.append(row['record'])
    if encoded!=sorted(encoded) or content_hash(encoded)!=receipt['content_sha256']:
        raise ValueError('Stream batch checksum differs from receipt')
    pin=config_pin(client,receipt['scope_id'],receipt['config_version'])
    if pin['content_hash']!=receipt['config_hash']:raise ValueError('Stream batch configuration pin changed')
    return tuple(json.loads(r) for r in encoded)


def receipts(client,*,scope=None,stream=None,generation=None):
    params={};conditions=[]
    for name,value in (('scope_id',scope),('stream_id',stream),('generation',generation)):
        if value is not None:params[name]=value;conditions.append(name+'={'+name+':String}')
    rows=client.query('SELECT DISTINCT * FROM stream_batch'+(' WHERE '+' AND '.join(conditions) if conditions else '')+' ORDER BY stream_id,generation,batch_id',params)
    seen=set()
    for r in rows:
        key=(r['stream_id'],r['generation'],r['batch_id'])
        if key in seen:raise ValueError('Conflicting stream batch receipts')
        seen.add(key)
    return rows


def _commit_locked(client,coordinator,stream,generation,batch,rows,scope,version):
    if not all(isinstance(v,str) and v.strip() for v in (stream,generation,scope,version)) or type(batch) is not int or batch<0:
        raise ValueError('Explicit stream, generation, scope, configuration and nonnegative batch ID required')
    pin=config_pin(client,scope,version);repo=Repository(client,coordinator)
    encoded=encoded_records(tuple(rows));checksum=content_hash(encoded)
    expected=dict(stream_id=stream,generation=generation,batch_id=batch,scope_id=scope,config_version=version,
        config_hash=pin['content_hash'],content_sha256=checksum,rows=len(encoded))
    bindings=client.query('SELECT DISTINCT scope_id,config_version FROM stream_delivery WHERE stream_id={stream:String} AND generation={gen:String}',{'stream':stream,'gen':generation})
    if any((r['scope_id'],r['config_version'])!=(scope,version) for r in bindings):
        raise ValueError('Checkpoint generation has an unsealed delivery from a different replay configuration')
    previous=receipts(client,stream=stream,generation=generation)
    for r in previous:
        if (r['scope_id'],r['config_version'],r['config_hash'])!=(scope,version,pin['content_hash']):
            raise ValueError('Checkpoint generation reused with a different replay configuration')
        if int(r['batch_id'])==batch:
            if any(str(r[k])!=str(v) for k,v in expected.items()):raise ValueError('Batch ID reused with different content')
            verify_batch(client,r)
            return False
    deliveries=[]
    for ordinal,line in enumerate(encoded):
        result=decode_ready_record(json.loads(line))
        deliveries.append(dict(stream_id=stream,generation=generation,batch_id=batch,ordinal=ordinal,scope_id=scope,config_version=version,
            record=line,disposition=result.disposition,reason=result.reason,replay_id=result.replay_id,identity=result.identity,payload_hash=result.payload_hash))
    # A known failed attempt may have left an unsealed prefix. Refuse drift before
    # writing; identical prefixes may be safely replayed with physical DISTINCT.
    existing=client.query('SELECT DISTINCT * FROM stream_delivery WHERE stream_id={stream:String} AND generation={gen:String} AND batch_id={batch:UInt64}',_key(stream,generation,batch))
    allowed={canonical_json(r) for r in deliveries}
    normalize=lambda r:dict(r,batch_id=int(r['batch_id']),ordinal=int(r['ordinal']))
    if any(canonical_json(normalize(r)) not in allowed for r in existing):raise ValueError('Unsealed batch retry changed content')
    for offset in range(0,len(deliveries),512):repo._insert('stream_delivery',deliveries[offset:offset+512])
    verify_batch(client,expected)
    repo._insert('stream_batch',[expected])
    return True


def commit_batch(client,coordinator,stream_id,generation,batch_id,rows,*,scope_id,config_version):
    with writer_lock(coordinator,client):
        return _commit_locked(client,coordinator,stream_id,generation,batch_id,rows,scope_id,config_version)


def landed_records(client,scope,version):
    result=[]
    for receipt in receipts(client,scope=scope):
        if receipt['config_version']==version:
            result.extend(verify_batch(client,receipt))
    return tuple(result)
