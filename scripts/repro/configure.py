"""Explicit demo config for an isolated, newly built AGC database."""
from datetime import date
from decimal import Decimal
import json
import psycopg
from greenhouse.app import wiring
from greenhouse.contracts import EnergyComponent, QueryContext, ScenarioChange, ScenarioCommand
from greenhouse.db.demo_rules import seed, seed_climate, seed_drain
from greenhouse.db.loader import AGC_SHA
from greenhouse.settings import Settings

with psycopg.connect(**Settings.from_env().connection_kwargs()) as conn:
    for register in (seed,seed_climate,seed_drain):
        register(conn,apply=True)
    publication_id = conn.execute('SELECT publication_id FROM meta.current_publication').fetchone()[0]
    raw = Settings.from_env().raw_dir/'agc-2nd-2019'/AGC_SHA
    svc = wiring.cost_service(conn, {'agc-2nd-2019-'+AGC_SHA:raw})
    pub = svc.energy.adapter.publication(publication_id)
    cycle = next(c for c in wiring.catalog_service(conn).cycles(pub) if c.compartment_id=='Automatoes')
    context = QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cycle.cycle_id,
        ('Automatoes',),date(2020,3,2),date(2020,3,4))
    baseline = svc.get_cost(context)
    scenario = svc.execute_scenario_command(ScenarioCommand(
        request_id='offline-restore-electricity-plus20', baseline_version_id=baseline.tariff_version,
        name='Offline restore electricity +20%', publication=pub, analysis_context=context,
        changes=tuple(ScenarioChange(c,'EUR/kWh',change_percent=Decimal('20'))
            for c in (EnergyComponent.ELEC_HIGH,EnergyComponent.ELEC_LOW))))
    conn.commit()
    result = svc.compare_scenarios(context,scenario_version=scenario.scenario_version)
    assert result.baseline.official_total_cost_eur==Decimal('17.60006771')
    assert result.scenario.official_total_cost_eur==Decimal('20.840734377')
    print(json.dumps({'publication':publication_id,'scenario_version':scenario.scenario_version,
        'baseline_eur':str(result.baseline.official_total_cost_eur),
        'scenario_eur':str(result.scenario.official_total_cost_eur),'demo_rules':'harvest v2, climate v1, drain v1'}))
