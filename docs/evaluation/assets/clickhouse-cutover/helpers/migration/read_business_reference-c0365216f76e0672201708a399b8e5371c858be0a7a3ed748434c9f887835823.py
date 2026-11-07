"""Backend-neutral full-cycle contract projection, with explicit physical ID mapping."""
import argparse
from contextlib import closing
from dataclasses import fields,is_dataclass
from datetime import date,datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path

from greenhouse.clickhouse.encoding import decimal_text
from greenhouse.contracts import QueryContext

PHYSICAL_FIELDS={'publication','versions','revision_id','first_run_id','ingest_run_id','run_id'}


def logical(value):
    if isinstance(value,Decimal):return decimal_text(value)
    if isinstance(value,(date,datetime)):return value.isoformat()
    if isinstance(value,Enum):return value.value
    if is_dataclass(value):
        return {f.name:('MIGRATED_PUBLICATION' if f.name=='publication_id' else logical(getattr(value,f.name)))
            for f in fields(value) if f.name not in PHYSICAL_FIELDS}
    if isinstance(value,dict):return {k:logical(v) for k,v in value.items() if k not in PHYSICAL_FIELDS}
    if isinstance(value,(tuple,list)):return [logical(v) for v in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(logical(value),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def project(connection,publication_id,raw):
    from greenhouse.app import wiring,quality_reads
    catalog=wiring.catalog_service(connection)
    water=wiring.irrigation_service(connection)
    harvest=wiring.harvest_service(connection)
    energy=wiring.energy_service(connection)
    intensity=wiring.intensity_service(connection)
    cost=wiring.cost_service(connection)
    publication=energy.adapter.publication(publication_id)
    output=[]
    for cycle in catalog.cycles(publication):
        q=QueryContext(publication.scope_id,publication.publication_id,publication.snapshot_id,cycle.cycle_id,
            (cycle.compartment_id,),cycle.start,cycle.end)
        wsummary=water.get_irrigation_summary(q);wdays=water.get_irrigation_days(q)
        hsummary=harvest.get_harvest_summary(q);hrecords=harvest.get_harvest_records(q);hdays=harvest.get_harvest_days(q)
        esummary=energy.get_summary(q);quantities=energy.get_quantities(q);ratio=intensity.get_intensity(q)
        tariff=cost.get_baseline_tariff(publication.scope_id,cycle.compartment_id) or cost.get_baseline_tariff(publication.scope_id)
        if tariff is None:raise ValueError('Explicit frozen source tariff is missing')
        baseline=cost.get_cost(q,tariff_version=tariff.tariff_version)
        end=cycle.end.fromordinal(cycle.end.toordinal()-1)
        decisions=quality_reads.measures(connection,publication_id,cycle.cycle_id,cycle.start,end)
        decisions=sorted(decisions,key=lambda r:json.dumps(r,sort_keys=True,default=str))
        quarantine=quality_reads.quarantined(connection,publication_id,cycle.cycle_id,cycle.start,end)
        result=dict(compartment=cycle.compartment_id,water_summary=logical(wsummary),water_days=fingerprint(wdays),
            water_weeks=fingerprint(water.get_irrigation_weekly(q)),harvest_summary=logical(hsummary),
            harvest_records=fingerprint(hrecords),harvest_days=fingerprint(hdays),harvest_weeks=fingerprint(harvest.get_harvest_weekly(q)),
            energy_summary=logical(esummary),energy_quantities=fingerprint(quantities),intensity=logical(ratio),
            baseline_cost=logical(baseline),weekly_cost=fingerprint(cost.get_weekly_cost(baseline)),
            quality_decisions=logical(decisions),quarantine=logical(quarantine))
        output.append(result)
        print(json.dumps({'stage':'business_reference','compartment':cycle.compartment_id}),flush=True)
    return {'status':'passed','publication_id':publication_id,'allowed_physical_fields':sorted(PHYSICAL_FIELDS|{'publication_id'}),'results':output}


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
    with closing(connection):result=project(connection,args.publication_id,args.raw)
    result['backend']=args.backend;result['read_only']=True
    if args.reference:
        reference=json.loads(args.reference.read_text())
        if result['results']!=reference['results']:
            args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
            raise ValueError('Business contract projection differs; inspect both outputs before accepting migration')
        result['reference_match']=True
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'status':'passed','backend':args.backend,'compartments':len(result['results'])}))


if __name__=='__main__':main()
