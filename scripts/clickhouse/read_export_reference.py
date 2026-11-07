"""Compare production CSV business cells; physical provenance is mapped separately."""
import argparse
from contextlib import closing
import csv
from decimal import Decimal
import hashlib
from io import StringIO
import json
from pathlib import Path
import re

from greenhouse.app import wiring,exports,energy_exports,cost_exports
from greenhouse.contracts import QueryContext,CostScenario,ScenarioChange,EnergyComponent
from greenhouse.queries.cost import EnergyCostService,derive_scenario_rates
from read_business_reference import PHYSICAL_FIELDS

CSV_PHYSICAL=PHYSICAL_FIELDS|{'publication_id','pipeline_run_id','code_version','config_bundle_hash','committed_at'}


def csv_projection(data):
    text=data.decode('utf-8-sig') if isinstance(data,bytes) else data
    lines=[line for line in text.splitlines() if not line.startswith('#')]
    rows=list(csv.DictReader(StringIO('\n'.join(lines))))
    numeric=re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$')
    from greenhouse.clickhouse.encoding import decimal_text
    normalized=[{k:decimal_text(v) if numeric.fullmatch(v or '') else v for k,v in row.items() if k not in CSV_PHYSICAL} for row in rows]
    payload=json.dumps(normalized,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    return {'rows':len(rows),'sha256':hashlib.sha256(payload).hexdigest(),'columns':[k for k in (rows[0] if rows else ()) if k not in CSV_PHYSICAL]}


def project(connection,publication_id):
    energy=wiring.energy_service(connection);pub=energy.adapter.publication(publication_id)
    water=wiring.irrigation_service(connection);harvest=wiring.harvest_service(connection)
    intensity=wiring.intensity_service(connection);cost=wiring.cost_service(connection);checks=[]
    for cycle in wiring.catalog_service(connection).cycles(pub):
        q=QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cycle.cycle_id,(cycle.compartment_id,),cycle.start,cycle.end)
        wa=wiring.water_action(water,pub,q);ha=wiring.harvest_action(harvest,pub,q);ratio=intensity.get_intensity(q)
        tariff=cost.get_baseline_tariff(pub.scope_id,cycle.compartment_id) or cost.get_baseline_tariff(pub.scope_id)
        changes=tuple(ScenarioChange(c,'EUR/kWh',change_percent=Decimal(20)) for c in (EnergyComponent.ELEC_HIGH,EnergyComponent.ELEC_LOW))
        version='migration-csv-probe:'+cycle.compartment_id
        scenario=CostScenario(version,version,1,version,tariff.tariff_version,pub,'CSV validation electricity +20%',
            derive_scenario_rates(tariff,version,'CSV validation electricity +20%',changes),changes,analysis_context=q)
        class ReadOnlyScenario:
            def __getattr__(self,name):return getattr(cost.adapter,name)
            def get_scenario(self,requested):return scenario if requested==version else cost.adapter.get_scenario(requested)
        comparison=EnergyCostService(energy,ReadOnlyScenario()).compare_scenarios(q,scenario_version=version,baseline_tariff_version=tariff.tariff_version)
        outputs={'water_days':exports.day_csv(wa),'water_weeks':exports.weekly_csv(wa),
            'harvest_records':exports.harvest_record_csv(ha),'harvest_days':exports.harvest_day_csv(ha),'harvest_weeks':exports.harvest_week_csv(ha),
            'energy_intensity':energy_exports.summary_csv(ratio),'energy_quantities':energy_exports.quantities_csv(ratio,energy.get_quantities(q)),
            'cost_summary':cost_exports.summary_csv(comparison),'cost_days':cost_exports.days_csv(comparison),
            'cost_weeks':cost_exports.weekly_csv(comparison)}
        checks.append({'compartment':cycle.compartment_id,'exports':{key:csv_projection(data) for key,data in outputs.items()}})
    return checks


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=['postgres','clickhouse'],required=True)
    parser.add_argument('--publication-id',required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reference',type=Path)
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
    with closing(connection):checks=project(connection,args.publication_id)
    result={'status':'passed','backend':args.backend,'read_only':True,'publication_id':args.publication_id,'results':checks,
        'provenance':'Actual production CSV exporters; compare business cells after exact Decimal normalization. Engine publication/revision/version fields mapped separately by metadata and raw lineage reports.',
        'scenario':'Ephemeral +20% electricity price; no persisted scenario writes'}
    if args.reference and checks!=json.loads(args.reference.read_text())['results']:
        args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
        raise ValueError('Production CSV business cells differ')
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'status':'passed','csv_exports':sum(len(c['exports']) for c in checks),'backend':args.backend}))


if __name__=='__main__':main()
