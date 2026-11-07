"""Verify raw lineage at every source subtype using the same backend factories."""
import argparse
from contextlib import closing
from datetime import datetime,time,timedelta
import json
from pathlib import Path

from greenhouse.contracts import QueryContext
from greenhouse.app import wiring
from read_business_reference import logical


def project(connection,publication_id,raw):
    pub=wiring.energy_service(connection).adapter.publication(publication_id)
    mapping={pub.snapshot_id:raw}
    services={kind:factory(connection,mapping) for kind,factory in (
        ('water',wiring.irrigation_service),('harvest',wiring.harvest_service),('energy',wiring.energy_service),
        ('climate',wiring.climate_service),('drain',wiring.drain_service))}
    results=[]
    for cycle in wiring.catalog_service(connection).cycles(pub):
        q=QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cycle.cycle_id,(cycle.compartment_id,),cycle.start,cycle.end)
        start=datetime.combine(cycle.start,time.min);end=start+timedelta(minutes=5)
        rows=dict(water=services['water'].get_irrigation_days(q)[0],harvest=services['harvest'].get_harvest_records(q)[0],
            energy=services['energy'].get_quantities(q)[0],
            climate=services['climate'].adapter.read_climate_slots(q,pub,window_start=start,window_end=end)[0],
            drain=services['drain'].adapter.read_drain_slots(q,pub,window_start=start,window_end=end)[0])
        for kind,row in rows.items():
            lineage=services[kind].get_lineage(pub,revision_id=row.revision_id)
            if not lineage.raw_hash_verified:raise ValueError('Raw hash not verified')
            projection=logical(lineage)
            # Units form a measure-code map. PG's locale collation and Python's
            # identifier order differ; preserve every exact pair, sort by code.
            projection['units'].sort(key=lambda unit:unit['measure_code'])
            results.append(dict(compartment=cycle.compartment_id,kind=kind,lineage=projection))
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=['postgres','clickhouse'],required=True)
    parser.add_argument('--publication-id',required=True);parser.add_argument('--raw',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--reference',type=Path)
    args=parser.parse_args()
    if args.backend=='postgres':
        import psycopg
        from greenhouse.settings import Settings
        connection=psycopg.connect(**(Settings.from_env().connection_kwargs()|{'options':'-c default_transaction_read_only=on'}))
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        assert connection.execute('SHOW transaction_read_only').fetchone()[0]=='on'
    else:
        from greenhouse.clickhouse.client import Client,ClickHouseSettings
        connection=Client(ClickHouseSettings.from_env()).begin_action()
    with closing(connection):results=project(connection,args.publication_id,args.raw)
    result=dict(status='passed',backend=args.backend,read_only=True,publication_id=args.publication_id,results=results)
    if args.reference and results!=json.loads(args.reference.read_text())['results']:
        args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
        raise ValueError('Raw lineage differs after explicit physical ID mapping')
    result['reference_match']=bool(args.reference)
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'status':'passed','lineages':len(results),'backend':args.backend}))


if __name__=='__main__':main()
