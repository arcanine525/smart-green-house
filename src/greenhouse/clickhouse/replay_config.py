"""Explicit immutable replay scope/configuration; never reuse historical scope IDs."""
from datetime import datetime,time
import json
from pathlib import PurePosixPath
import re

from greenhouse.clickhouse.quality import AGC_QUALITY_DEFINITION
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.stream_store import config_pin


def validated_config(config,scope):
    required={'replay_id','scope_id','cycle_id','compartment_id','cycle_start','cycle_end','quality_policy_version',
              'quality_definition','policy_start','policy_end','snapshot_id','snapshot_sha','source_file','file_checksum'}
    if not required<=config.keys() or config['scope_id']!=scope or not all(isinstance(config[k],str) and config[k].strip() for k in required-{'quality_definition'}):
        raise ValueError('Replay configuration needs explicit scope/source/cycle/policy')
    if config['quality_policy_version']!='AGC_QUALITY_V1' or config['quality_definition']!=AGC_QUALITY_DEFINITION:
        raise ValueError('Unsupported replay quality policy or definition drift')
    for name in ('cycle','policy'):
        lo,hi=(datetime.fromisoformat(config[name+'_'+bound]) for bound in ('start','end'))
        if lo.tzinfo or hi.tzinfo or lo>=hi or lo.time()!=time.min or hi.time()!=time.min or [lo.isoformat(),hi.isoformat()]!=[config[name+'_start'],config[name+'_end']]:
            raise ValueError('Replay cycle/policy must be whole source-naive dates')
    if not config['policy_start']<=config['cycle_start']<config['cycle_end']<=config['policy_end']:
        raise ValueError('Replay cycle is outside its quality policy')
    for name in ('snapshot_sha','file_checksum'):
        if not re.fullmatch('[0-9a-f]{64}',config[name]):raise ValueError('Replay source checksum required')
    path=PurePosixPath(config['source_file'])
    if path.is_absolute() or '..' in path.parts or '\\' in str(path):raise ValueError('Replay source must be a safe relative path')
    return config


def read_config(client,scope,version):
    pin=config_pin(client,scope,version)
    return validated_config(json.loads(pin['payload']),scope),pin


def register_config(client,coordinator,scope,version,payload):
    validated_config(payload,scope)
    Repository(client,coordinator).register_configuration('replay',scope,version,'replay-config:'+scope+':'+version,payload)
    return read_config(client,scope,version)


def from_historical(client,coordinator,source_publication,*,replay_id,scope_id,compartment_id,config_version):
    from greenhouse.clickhouse.threshold import ClimateAdapter
    adapter=ClimateAdapter(client);pub=adapter.publication(source_publication)
    if scope_id==pub.scope_id:raise ValueError('Replay must have a separate scope from historical data')
    cycles=[c for c in adapter.catalog_cycles(source_publication) if c.compartment_id==compartment_id]
    if len(cycles)!=1:raise ValueError('Replay source cycle is absent or ambiguous')
    cycle=cycles[0];envelope=adapter._envelope(source_publication)
    sources=[json.loads(p['payload']) for p in envelope['configuration']['pins'] if p['kind']=='snapshot']
    if len(sources)!=1:raise ValueError('Replay source snapshot pin missing')
    source=sources[0];relative=compartment_id+'/GreenhouseClimate.csv'
    files=[f for f in source['files'] if f['path']==relative]
    if len(files)!=1:raise ValueError('Replay climate source file pin missing')
    payload=dict(replay_id=replay_id,scope_id=scope_id,cycle_id=cycle.cycle_id,compartment_id=compartment_id,
        cycle_start=datetime.combine(cycle.start,time.min).isoformat(),cycle_end=datetime.combine(cycle.end,time.min).isoformat(),
        policy_start=datetime.combine(cycle.start,time.min).isoformat(),policy_end=datetime.combine(cycle.end,time.min).isoformat(),
        quality_policy_version=pub.versions.quality_policy_version,quality_definition=AGC_QUALITY_DEFINITION,
        snapshot_id=pub.snapshot_id,snapshot_sha=source['archive']['sha256'],source_file=relative,file_checksum=files[0]['sha256'],
        source_publication_id=source_publication,source_config_hash=pub.versions.config_bundle_hash)
    return register_config(client,coordinator,scope_id,config_version,payload)


def main():
    import argparse
    from pathlib import Path
    from greenhouse.clickhouse.client import Client,ClickHouseSettings
    parser=argparse.ArgumentParser(description='Register an explicit replay configuration from a verified historical publication')
    for flag in ('source-publication','replay-id','scope-id','compartment-id','config-version'):
        parser.add_argument('--'+flag,required=True)
    parser.add_argument('--coordinator',type=Path,required=True)
    args=parser.parse_args()
    config,pin=from_historical(Client(ClickHouseSettings.from_env()),args.coordinator,args.source_publication,
        replay_id=args.replay_id,scope_id=args.scope_id,compartment_id=args.compartment_id,config_version=args.config_version)
    print(json.dumps({'registered':True,'scope_id':config['scope_id'],'config_version':pin['version_id'],'content_hash':pin['content_hash']}))


if __name__=='__main__':main()
