"""Readers verify exactly the immutable bundle set of the selected publication."""
import json
from datetime import datetime

from greenhouse.clickhouse.publication import Publications
from greenhouse.contracts import PublicationRef, Versions


def envelope(client,scope,publication_id=None):
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
        elif name in ('climate','drain'):
            from greenhouse.clickhouse.threshold import SlotMart
            validators[name]=SlotMart(client,None,name).verify
        else:
            raise ValueError('Unsupported publication bundle: '+name)
    return Publications(client,None,bundles=validators).pin(scope,publication_id=selected['publication_id'])


def publication_envelope(client,publication_id):
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
