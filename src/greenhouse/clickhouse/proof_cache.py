"""Bounded process-local proofs, guarded by physical parts and fresh config checks.

Only explicit read actions opt in. Reopening the process requires verification;
INSERT, mutation, merge, schema change and table recreation invalidate a proof.
The source spool is shared by reference and released after its last reader.
"""
from collections import OrderedDict
from copy import deepcopy
from threading import RLock

from greenhouse.clickhouse.encoding import digest
from greenhouse.clickhouse.repository import Repository

_entries=OrderedDict()
_guard=RLock()
_MAX_ENTRIES=2


class WarehouseChanged(ValueError):
    pass


def clear():
    with _guard:_entries.clear()


def _capture(client):
    tables=client.query('''SELECT name,toString(uuid) AS uuid,create_table_query FROM system.tables
        WHERE database=currentDatabase() AND engine='MergeTree'
        AND NOT startsWith(name,'replay_') AND NOT startsWith(name,'stream_') ORDER BY name''')
    names=[r['name'] for r in tables];parameters={'tables':str(names)}
    pending=client.query('''SELECT count() AS n FROM system.mutations WHERE database=currentDatabase()
        AND table IN {tables:Array(String)} AND NOT is_done''',parameters)
    if int(pending[0]['n']):raise ValueError('Historical verification unavailable while a table mutation is pending')
    parts=client.query('''SELECT table,name,toString(uuid) AS uuid,rows,bytes_on_disk,data_version,hash_of_all_files
        FROM system.parts WHERE database=currentDatabase() AND active
        AND table IN {tables:Array(String)} ORDER BY table,name''',
        {'tables':str([name for name in names if name!='configuration_envelope'])})
    if any(len(r['hash_of_all_files'])!=32 for r in parts):
        raise ValueError('Physical part checksum unavailable for shared verification')
    server=client.query('SELECT toString(serverUUID()) AS identity')[0]['identity']
    s=client.settings;namespace=(s.host,s.port,s.database,s.user,server)
    return namespace,digest({'tables':tables,'parts':parts})


def _check_configuration(client,envelope):
    # New immutable rule/scenario versions do not change historical source
    # parts. Validate the ENTIRE registry freshly, then every selected pin.
    rows=Repository(client,None).configurations()['rows']
    index={(r['kind'],r['scope_id'],r['version_id']):r for r in rows}
    selected=envelope['configuration']['pins']
    if digest(selected)!=envelope['configuration']['checksum']:
        raise ValueError('Cached publication configuration checksum mismatch')
    for pin in selected:
        if index.get((pin['kind'],pin['scope_id'],pin['version_id']))!=pin:
            raise ValueError('Publication configuration pin missing or changed')


def _verified(client,scope,publication_id,loader):
    if publication_id is None:
        rows=client.query('''SELECT DISTINCT publication_id,generation FROM publication_state
            WHERE scope_id={scope:String} AND stage='COMPLETE' ORDER BY generation DESC''',{'scope':scope})
        if not rows:raise ValueError('No complete publication')
        generation=int(rows[0]['generation'])
        latest=[r for r in rows if int(r['generation'])==generation]
        if len(latest)!=1:raise ValueError('Conflicting complete generation')
        publication_id=latest[0]['publication_id']
    # Single flight: multiple Streamlit sessions must not run concurrent cold
    # full-source verification against the same bounded local server.
    with _guard:
        namespace,stamp=_capture(client);key=(namespace,scope,publication_id)
        entry=_entries.get(key)
        if entry is not None and entry['stamp']==stamp:
            _check_configuration(client,entry['envelope'])
            if _capture(client)!=(namespace,stamp):
                raise WarehouseChanged('Warehouse changed during verification; retry when writers are stable')
            envelope=deepcopy(entry['envelope']);core=entry['core']
            _entries.move_to_end(key)
            client._shared_proof_hits+=1
        else:
            envelope=loader(publication_id)
            if _capture(client)!=(namespace,stamp):
                raise WarehouseChanged('Warehouse changed during verification; retry when writers are stable')
            core=client._action_cache.get(('load-membership',scope,envelope['load_id']))
            saved=deepcopy(core) if isinstance(core,list) else core
            _entries[key]={'stamp':stamp,'envelope':deepcopy(envelope),'core':saved}
            _entries.move_to_end(key)
            while len(_entries)>_MAX_ENTRIES:_entries.popitem(last=False)
            client._shared_proof_misses+=1
        client._action_cache[('historical-envelope',publication_id)]=(scope,envelope)
        if core is not None:
            client._action_cache[('load-membership',scope,envelope['load_id'])]=deepcopy(core) if isinstance(core,list) else core
        client._action_cache[('historical-stamp',publication_id)]=(namespace,stamp)
        return envelope


def verified(client,scope,publication_id,loader):
    for attempt in range(2):
        try:return _verified(client,scope,publication_id,loader)
        except WarehouseChanged:
            # A legitimate physical retry/merge can finish during a read. Start
            # one independent verification from the new physical fingerprint.
            client._action_cache={}
            if attempt:raise
        except BaseException:
            # Never expose a partial proof to another service in this action.
            client._action_cache={}
            raise
