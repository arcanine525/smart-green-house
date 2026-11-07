"""Create real user state through native APIs before backup and raw rebuild."""
from datetime import date
from decimal import Decimal
import json

from common import COORDINATOR, STATE, connect, historical_id, write_json
from greenhouse.app import wiring
from greenhouse.clickhouse.schedules import save_drain_profile, read_rule_schedule
from greenhouse.contracts import EnergyComponent, QueryContext, ScenarioChange, ScenarioCommand, to_dict

client = connect(action=True)
try:
    svc = wiring.cost_service(client)
    pub = svc.energy.adapter.publication(historical_id())
    cycle = next(c for c in wiring.catalog_service(client).cycles(pub) if c.compartment_id == 'Automatoes')
    profile = save_drain_profile(client, COORDINATOR, pub, cycle,
        name='offline-restore', version=1, ec_lower='2', ec_upper='5',
        ph_lower='5.5', ph_upper='6.5', provenance='Native offline backup/rebuild rehearsal; user-unverified limits')
    assert read_rule_schedule(client, profile.rule_version) == profile
    context = QueryContext(pub.scope_id, pub.publication_id, pub.snapshot_id, cycle.cycle_id,
        ('Automatoes',), date(2020, 3, 2), date(2020, 3, 4))
    baseline = svc.get_cost(context)
    # The public service owns the transaction and derives the complete rates.
    scenario = svc.execute_scenario_command(ScenarioCommand(
        request_id='offline-restore-electricity-plus20', baseline_version_id=baseline.tariff_version,
        name='Offline restore electricity +20%', publication=pub, analysis_context=context,
        changes=tuple(ScenarioChange(c, 'EUR/kWh', change_percent=Decimal('20'))
            for c in (EnergyComponent.ELEC_HIGH, EnergyComponent.ELEC_LOW))))
    repeated = svc.execute_scenario_command(ScenarioCommand(
        request_id=scenario.request_id, baseline_version_id=scenario.baseline_tariff_version,
        name=scenario.name, publication=pub, analysis_context=context,
        changes=scenario.changes, scenario_id=scenario.scenario_id))
    assert repeated == scenario
    result = svc.compare_scenarios(context, scenario_version=scenario.scenario_version)
    assert result.baseline.official_total_cost_eur == Decimal('17.60006771')
    assert result.scenario.official_total_cost_eur == Decimal('20.840734377')
    proof = dict(status='passed', backend='clickhouse', publication=to_dict(pub),
        user_profile=to_dict(profile), user_scenario=to_dict(scenario), retry_exact=True,
        baseline_eur=str(result.baseline.official_total_cost_eur),
        scenario_eur=str(result.scenario.official_total_cost_eur))
    write_json(STATE / 'native-user-state.json', proof)
    print(json.dumps({k: proof[k] for k in ('status', 'backend', 'baseline_eur', 'scenario_eur')}), flush=True)
finally:
    client.close()
