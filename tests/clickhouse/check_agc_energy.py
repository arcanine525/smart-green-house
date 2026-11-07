"""Full Resources/Production BR04/06 raw/PG parity; climate remains bounded."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from energy_oracle import evaluate
# Only the SELECT-only reference helper's output projection is shared.
from read_postgres_energy import values
from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.energy import EnergyAdapter
from greenhouse.clickhouse.harvest import HarvestAdapter
from greenhouse.clickhouse.cost import CostAdapter
from greenhouse.clickhouse.water_run import AGC_SNAPSHOT
from greenhouse.contracts import QueryContext,ScenarioCommand,ScenarioChange,EnergyComponent
from greenhouse.queries.energy import EnergyService
from greenhouse.queries.intensity import EnergyIntensityService
from greenhouse.queries.cost import EnergyCostService


def equal_values(left,right):
    if left.keys()!=right.keys():return False
    for key,value in left.items():
        other=right[key]
        if key=='harvest_complete':
            if value!=other:return False
        elif value=='None' or other=='None':
            if value!=other:return False
        elif Decimal(value)!=Decimal(other):return False
    return True


def main():
    client=Client(ClickHouseSettings.from_env());raw=Path('/data/raw/agc-2nd-2019')/AGC_SNAPSHOT.removeprefix('agc-2nd-2019-')
    scope=AGC_SNAPSHOT+':historical';reference=json.loads(Path('/evidence/energy-postgres-reference.json').read_text())
    assert reference['read_only'] and reference['status']=='passed'
    adapter=EnergyAdapter(client,raw_snapshots={AGC_SNAPSHOT:raw});energy=EnergyService(adapter);pub=energy.pin_publication(scope)
    intensity=EnergyIntensityService(energy,HarvestAdapter(client));cost=EnergyCostService(energy,CostAdapter(client,Path('/candidate/coordinator')))
    checks=[]
    for cycle in adapter.catalog_cycles(pub.publication_id):
        tariff=cost.get_baseline_tariff(scope,cycle.compartment_id) or cost.get_baseline_tariff(scope)
        assert tariff is not None
        for label,start,end in (('two-day',date(2020,3,2),date(2020,3,4)),('full-cycle',cycle.start,cycle.end)):
            q=QueryContext(scope,pub.publication_id,pub.snapshot_id,cycle.cycle_id,(cycle.compartment_id,),start,end)
            changes=tuple(ScenarioChange(c,'EUR/kWh',change_percent=Decimal(20)) for c in (EnergyComponent.ELEC_HIGH,EnergyComponent.ELEC_LOW))
            command=ScenarioCommand('agc-price-demo:'+pub.publication_id+':'+cycle.compartment_id+':'+label,tariff.tariff_version,'Electricity +20%',pub,changes,analysis_context=q)
            scenario=cost.execute_scenario_command(command);assert cost.execute_scenario_command(command)==scenario
            summary=energy.get_summary(q);ratio=intensity.get_intensity(q);baseline=cost.get_cost(q,tariff_version=tariff.tariff_version)
            comparison=cost.compare_scenarios(q,scenario_version=scenario.scenario_version,baseline_tariff_version=tariff.tariff_version)
            actual=values(summary,ratio,baseline,comparison);oracle=evaluate(raw,cycle.compartment_name,start,end)
            pg=next(r['values'] for r in reference['results'] if (r['compartment'],r['window'])==(cycle.compartment_name,label))
            assert equal_values(actual,oracle),(cycle.compartment_name,label,'raw',actual,oracle)
            assert equal_values(actual,pg),(cycle.compartment_name,label,'PostgreSQL',actual,pg)
            from greenhouse.app import cost_exports
            exported=cost_exports.summary_csv(comparison,scenario)
            assert scenario.scenario_version.encode() in exported and pub.publication_id.encode() in exported
            checks.append({'compartment':cycle.compartment_name,'window':label,'values':actual,'raw_match':True,'postgres_match':True,
                'scenario_version':scenario.scenario_version,'scenario_retry_reopen':cost.get_scenario(scenario.scenario_version)==scenario,'csv_pinned':True})
    print(json.dumps({'status':'passed','publication':pub.publication_id,'postgres_publication':reference['publication'],'checks':checks,
        'limits':['Full Resources/Production slice; climate/drain still two-day selection, full six-BR backfill remains T38','+20% scenario changes source tariffs only, not modeled energy consumption or an invoice','Browser video set still T51']},indent=2))


if __name__=='__main__':main()
