"""Readers verify exactly the immutable bundle set of the selected publication."""
import json
from datetime import datetime

from greenhouse.clickhouse.publication import Publications
from greenhouse.contracts import PublicationRef, Versions


def envelope(client,scope,publication_id=None):
    cache=getattr(client,'_action_cache',None)
    if cache is not None and publication_id is not None:
        saved=cache.get(('historical-envelope',publication_id))
        if saved is not None and saved[0]==scope:return saved[1]
    if cache is not None and getattr(client,'_shared_proofs',False):
        from greenhouse.clickhouse.proof_cache import verified
        return verified(client,scope,publication_id,lambda selected:_envelope(client,scope,selected))
    return _envelope(client,scope,publication_id)


def _envelope(client,scope,publication_id=None):
    cache=getattr(client,'_action_cache',None)
    if cache is not None and publication_id is not None:
        saved=cache.get(('historical-envelope',publication_id))
        if saved is not None and saved[0]==scope:return saved[1]
    probe=Publications(client,None)
    rows=[r for r in probe._states(scope) if r['stage']=='COMPLETE'
          and (publication_id is None or r['publication_id']==publication_id)]
    if not rows:
        raise ValueError('No complete publication')
    selected=max(rows,key=lambda r:int(r['generation']))
    required=json.loads(selected['payload'])['required_bundles']
    validators={}
    for name in required:
        if name=='water':
            from greenhouse.clickhouse.water import WaterMart
            validators[name]=WaterMart(client,None).verify
        elif name=='harvest':
            from greenhouse.clickhouse.harvest import HarvestMart
            validators[name]=HarvestMart(client,None).verify
        elif name=='energy':
            from greenhouse.clickhouse.energy import EnergyMart
            validators[name]=EnergyMart(client,None).verify
        elif name in ('climate','drain'):
            from greenhouse.clickhouse.threshold import SlotMart
            validators[name]=SlotMart(client,None,name).verify
        else:
            raise ValueError('Unsupported publication bundle: '+name)
    verified=Publications(client,None,bundles=validators).pin(scope,publication_id=selected['publication_id'])
    if cache is not None:cache[('historical-envelope',verified['publication_id'])]=(scope,verified)
    return verified


def publication_envelope(client,publication_id):
    cache=getattr(client,'_action_cache',None)
    if cache is not None and ('historical-envelope',publication_id) in cache:return cache[('historical-envelope',publication_id)][1]
    scopes=client.query('SELECT DISTINCT scope_id FROM publication_state WHERE publication_id={id:String} AND stage=\'COMPLETE\'',{'id':publication_id})
    if len(scopes)!=1:
        raise ValueError('Unknown complete publication')
    return envelope(client,scopes[0]['scope_id'],publication_id)


def reference(client,publication_id,verified=None):
    verified=verified or publication_envelope(client,publication_id)
    snapshots=[p for p in verified['configuration']['pins'] if p['kind']=='snapshot']
    if len(snapshots)!=1:
        raise ValueError('Publication lacks unambiguous snapshot pin')
    snapshot=json.loads(snapshots[0]['payload'])['snapshot_id']
    policies=[]
    selected={(r['configuration_ref']['kind'],r['configuration_ref']['version'])
              for r in verified['receipts'].values() if 'configuration_ref' in r}
    for pin in verified['configuration']['pins']:
        if (pin['kind'],pin['version_id']) in selected:
            config=json.loads(pin['payload'])
            policies.append((config['quality_policy_version'],config['parser_version']))
    if not policies or len(set(policies))!=1:
        raise ValueError('Publication has missing or inconsistent semantic configuration')
    code='clickhouse-water-v1' if verified['required_bundles']==['water'] else 'clickhouse-warehouse-v1'
    return PublicationRef(publication_id,snapshots[0]['scope_id'],snapshot,
        Versions(verified['load_id'],policies[0][0],policies[0][1],code,verified['configuration']['checksum']),
        datetime.fromisoformat(verified['committed_at']),'complete')
