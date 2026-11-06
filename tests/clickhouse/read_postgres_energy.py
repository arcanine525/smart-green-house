"""Read-only AGC BR04/06 numeric reference, including ephemeral +20% comparison."""
from datetime import date
from decimal import Decimal
import json
import psycopg
from greenhouse.settings import Settings
from greenhouse.contracts import QueryContext,CostScenario,ScenarioChange,EnergyComponent
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.db.cost_adapter import PostgresCostAdapter
from greenhouse.queries.energy import EnergyService
from greenhouse.queries.intensity import EnergyIntensityService
from greenhouse.queries.cost import EnergyCostService,derive_scenario_rates


def values(energy,intensity,baseline,comparison):
    return {'official_energy_kwh':str(energy.official_total_kwh),'observed_energy_kwh':str(energy.observed_total_kwh),
        'energy_coverage':str(energy.complete_coverage_pct),'official_a_kg':str(intensity.official_a_kg),
        'intensity_kwh_kg_a':str(intensity.intensity_kwh_kg_a),'harvest_complete':intensity.harvest_state.snapshot_complete if hasattr(intensity,'harvest_state') else intensity.official_a_kg is not None,
        'baseline_eur':str(baseline.official_total_cost_eur),'baseline_eur_m2':str(baseline.official_total_cost_per_m2),
        'scenario_eur':str(comparison.scenario.official_total_cost_eur),'scenario_eur_m2':str(comparison.scenario.official_total_cost_per_m2),
        'delta_eur':str(comparison.delta_total_cost_eur)}


def main():
    result=[]
    with psycopg.connect(**(Settings.from_env().connection_kwargs()|{'options':'-c default_transaction_read_only=on'})) as connection:
        assert connection.execute('SHOW transaction_read_only').fetchone()[0]=='on'
        catalog=PostgresCatalogAdapter(connection);selected=[r for r in catalog.catalog_publications() if r.snapshot_id.startswith('agc-2nd-2019-') and r.energy_usable]
        assert len(selected)==1;selected=selected[0]
        energy=EnergyService(PostgresEnergyQueryAdapter(connection));pub=energy.adapter.publication(selected.publication_id)
        intensity=EnergyIntensityService(energy,PostgresHarvestQueryAdapter(connection));base_adapter=PostgresCostAdapter(connection)
        for cycle in catalog.catalog_cycles(pub.publication_id):
            tariff=base_adapter.get_baseline_tariff(pub.scope_id,cycle.compartment_id) or base_adapter.get_baseline_tariff(pub.scope_id)
            assert tariff is not None
            for label,start,end in (('two-day',date(2020,3,2),date(2020,3,4)),('full-cycle',cycle.start,cycle.end)):
                q=QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cycle.cycle_id,(cycle.compartment_id,),start,end)
                changes=tuple(ScenarioChange(c,'EUR/kWh',change_percent=Decimal(20)) for c in (EnergyComponent.ELEC_HIGH,EnergyComponent.ELEC_LOW))
                version='validation-only:'+cycle.compartment_id+':'+label
                scenario=CostScenario(version,version,1,version,tariff.tariff_version,pub,'Electricity +20%',derive_scenario_rates(tariff,version,'Electricity +20%',changes),changes,analysis_context=q)
                class Adapter(PostgresCostAdapter):
                    def get_scenario(self,requested):
                        return scenario if requested==version else super().get_scenario(requested)
                cost=EnergyCostService(energy,Adapter(connection))
                summary=energy.get_summary(q);ratio=intensity.get_intensity(q);baseline=cost.get_cost(q,tariff_version=tariff.tariff_version)
                comparison=cost.compare_scenarios(q,scenario_version=version,baseline_tariff_version=tariff.tariff_version)
                result.append({'compartment':cycle.compartment_name,'window':label,'values':values(summary,ratio,baseline,comparison)})
        print(json.dumps({'status':'passed','engine':'postgres','read_only':True,'publication':pub.publication_id,'scenario':'ephemeral read-only price +20%, no persisted writes','results':result},indent=2))


if __name__=='__main__':main()
