"""Reopen a full candidate, rerun its workflow, then retry scenario migration.

Run while candidate writers are stopped. Both stages use fresh uncached clients;
the JSON result asserts exact receipts, identities and unchanged physical counts.
"""
import argparse
import json
from pathlib import Path
import time

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.energy_run import run
from greenhouse.clickhouse.migration import import_scenarios


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('metadata','proof','raw','coordinator','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--workflow-id',required=True)
    args=parser.parse_args();metadata=json.loads(args.metadata.read_text());proof=json.loads(args.proof.read_text())
    if proof.get('status')!='passed' or proof.get('all_six_brs') is not True:
        raise ValueError('Passing full parity proof required')
    settings=ClickHouseSettings.from_env();client=Client(settings)
    target=proof['clickhouse_publication'];scope=metadata['source_publication']['scope_id']
    states=client.query('SELECT DISTINCT payload FROM publication_state WHERE scope_id={scope:String} AND publication_id={pub:String} AND stage=\'COMPLETE\'',
        {'scope':scope,'pub':target})
    if len(states)!=1:raise ValueError('One complete baseline publication required')
    original=json.loads(states[0]['payload'])
    def inventory():
        tables=client.query('SELECT name FROM system.tables WHERE database=currentDatabase() AND engine=\'MergeTree\' ORDER BY name')
        result={}
        for row in tables:
            name=row['name']
            if not name.replace('_','').isalnum():raise ValueError('Unexpected table name')
            result[name]=int(client.query('SELECT count() AS n FROM '+name)[0]['n'])
        return result
    before=inventory();config=metadata['configurations'];started=time.monotonic()
    print(json.dumps({'stage':'full_complete_rerun_started','publication_id':target}),flush=True)
    rerun=run(client,args.coordinator,args.raw,args.output.parent/'pipeline-output',scope,args.workflow_id,
        *(config[key] for key in ('water','harvest','threshold','energy')),
        water_version='pg-water-v1',harvest_version='pg-harvest-v1',threshold_version='pg-threshold-stream-v2',energy_version='pg-energy-v1')
    if (rerun['publication_id'],rerun['load_id'],rerun['receipts'])!=(target,original['load_id'],original['receipts']):
        raise ValueError('Complete workflow retry changed identity or receipt')
    if not rerun.get('resumed'):raise ValueError('Expected existing workflow retry')
    rerun_seconds=time.monotonic()-started;client.close()
    print(json.dumps({'stage':'full_complete_rerun_passed','seconds':rerun_seconds}),flush=True)
    started=time.monotonic()
    result=import_scenarios(Client(settings),args.coordinator,metadata,proof)
    if result['inserted']!=0 or result['reused']!=len(metadata['scenarios']):
        raise ValueError('Scenario retry inserted versions or failed to reuse source versions')
    after=inventory()
    if before!=after:raise ValueError('Retry changed physical warehouse table counts')
    report={'status':'passed','publication_id':target,'load_id':original['load_id'],
        'complete_workflow_resumed':True,'receipts_unchanged':True,'physical_counts_unchanged':True,
        'physical_table_counts':after,'scenario_retry':result,
        'complete_rerun_seconds':rerun_seconds,'scenario_retry_seconds':time.monotonic()-started,
        'client_action_cache':False,'source_scope':scope}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)


if __name__=='__main__':main()
