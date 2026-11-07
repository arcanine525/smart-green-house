"""Durable pending-query fence for a supported writer holding the shared flock.

Never infer failure from a missing response or an empty process list. A terminal
query-log entry is necessary to clear an ambiguous operation automatically.
"""
import json
import os
from pathlib import Path
from uuid import uuid4

from greenhouse.clickhouse.client import ClickHouseError
import hashlib


class PendingWrite(ClickHouseError):
    pass


def _sync_directory(root):
    descriptor=os.open(root,os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _save(root, value):
    temporary=root/('pending-'+uuid4().hex+'.tmp')
    with temporary.open('x') as stream:
        json.dump(value,stream,sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(root/'pending-write.json')
    _sync_directory(root)


def _clear(root):
    (root/'pending-write.json').unlink()
    _sync_directory(root)


def reconcile(client, root):
    root=Path(root)
    marker=root/'pending-write.json'
    if not marker.exists():
        return
    operation=json.loads(marker.read_text())
    query=operation['query_id']
    # Flush only for ambiguous recovery, not for each ordinary write.
    client.execute('SYSTEM FLUSH LOGS')
    active=client.query('SELECT query_id FROM system.processes WHERE query_id={id:String}',{'id':query})
    if active:
        raise PendingWrite('Previous write is still active; keep the pending fence and retry reconciliation later')
    rows=client.query('''SELECT toString(type) AS type FROM system.query_log
        WHERE query_id={id:String} AND type IN ('QueryFinish','ExceptionBeforeStart','ExceptionWhileProcessing')''',{'id':query})
    if not rows:
        raise PendingWrite('Previous write has no terminal query evidence; pending fence retained for explicit recovery')
    if operation.get('cutover_query'):
        from greenhouse.clickhouse.cutover_journal import terminal
        statuses={row['type'] for row in rows}
        if len(statuses)!=1:raise PendingWrite('Conflicting terminal query evidence; keep the write fence')
        terminal(root,operation['cutover_query'],next(iter(statuses)),'server query_log reconciliation')
    # Terminal failure may have partial inserts across blocks: semantic manifests
    # still need validation. Clearing the fence does not mark the batch complete.
    _clear(root)


def execute_write(client, root, sql, *, query_id=None):
    """Caller holds the shared writer lock; synchronous request, no auto retry."""
    root=Path(root)
    reconcile(client,root)
    # Unique wire identity per request; never mistake an earlier retry's log
    # entry for terminal evidence of a later request with the same logical key.
    query_id=(query_id or 'warehouse')+'-'+uuid4().hex
    from greenhouse.clickhouse.cutover_journal import before_write,terminal
    cutover_query=before_write(client,root,query_id,sql)
    _save(root,{'format':1,'query_id':query_id,'sql_hash':hashlib.sha256(sql.encode()).hexdigest(),
        'cutover_query':cutover_query})
    result=client.execute(sql,query_id=query_id)
    if cutover_query:terminal(root,cutover_query,'QueryFinish','synchronous response before acknowledgment')
    _clear(root)
    return result
