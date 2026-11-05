"""Worker's independent source-cost oracle for BR-06 energy cost & scenarios.

Computes exact raw CSV sums and prices across all 6 AGC compartments under:
1. Canonical historical baseline (Heat 0.0083 EUR/MJ, ElecHigh 0.08 EUR/kWh, ElecLow 0.04 EUR/kWh)
2. Electric +20% scenario (ElecHigh 0.096 EUR/kWh, ElecLow 0.048 EUR/kWh)
Verifies exact match against database and CostService without using any main probes.
"""
import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import psycopg

from greenhouse.app import cost_exports, wiring
from greenhouse.contracts import (
    EnergyComponent, PublicationRef, QueryContext, ScenarioChange, ScenarioCommand,
    to_json,
)
from greenhouse.db.loader import AGC_SCOPE
from greenhouse.settings import Settings

EXPECTED_PUB = os.environ.get('EXPECTED_PUBLICATION', '892b4cd58cdf4d099510974d91fa64ab')
RAW = Path('/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68')
OUT = Path(os.environ.get('EVIDENCE_DIR', '/evidence'))
TOL = Decimal('1e-40')
EPOCH = date(1899, 12, 30)
CODES = {'heat': 'Heat_cons', 'elec_high': 'ElecHigh', 'elec_low': 'ElecLow'}

HISTORICAL_RATES = {
    EnergyComponent.HEAT: Decimal('0.0083'),
    EnergyComponent.ELEC_HIGH: Decimal('0.08'),
    EnergyComponent.ELEC_LOW: Decimal('0.04'),
}


def total(values):
    with localcontext() as ctx:
        ctx.prec = 100
        return sum(values, Decimal(0))


def value(token):
    if token.strip().lower() in ('', 'nan', 'na', 'n/a'):
        return None, 'missing'
    try:
        result = Decimal(token.strip())
        if not result.is_finite():
            raise InvalidOperation
        return result, 'accepted'
    except InvalidOperation:
        return None, 'invalid'


def source(path, manifest_files):
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest_files[path.relative_to(RAW).as_posix()] == checksum
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        headers = [h.strip() for h in next(reader)]
        for line, row in enumerate(reader, 2):
            serial = Decimal(row[0])
            assert serial == int(serial)
            yield EPOCH + timedelta(days=int(serial)), line, dict(zip(headers, row))


def run_oracle():
    manifest = json.loads((RAW / 'manifest.json').read_text())
    manifest_files = {f['path']: f['sha256'] for f in manifest['files']}
    reports = []

    with psycopg.connect(**Settings.from_env().connection_kwargs()) as conn:
        raw_mapping = {manifest['snapshot_id']: RAW}
        cost_svc = wiring.cost_service(conn, raw_mapping)
        catalog = wiring.catalog_service(conn)
        pub = cost_svc.energy.adapter.publication(EXPECTED_PUB)
        cycles = catalog.cycles(pub)

        for cy in cycles:
            context = QueryContext(
                pub.scope_id, pub.publication_id, pub.snapshot_id,
                cy.cycle_id, (cy.compartment_id,), cy.start, cy.end,
            )

            # Ensure baseline tariff exists for this scope & compartment
            cost_svc.register_historical_tariff(pub.scope_id, cy.compartment_id)
            conn.commit()

            # 1. Oracle calculation directly from raw Resources.csv
            res_csv = RAW / cy.compartment_name / 'Resources.csv'
            raw_resources = {
                day: (line, row)
                for day, line, row in source(res_csv, manifest_files)
                if cy.start <= day < cy.end
            }

            oracle_comp_sums = {}
            oracle_comp_costs_m2 = {}
            for comp_name, col_name in CODES.items():
                ec = EnergyComponent(comp_name)
                comp_total = total(value(row[col_name])[0] for _, row in raw_resources.values() if value(row[col_name])[0] is not None)
                oracle_comp_sums[ec] = comp_total
                rate = HISTORICAL_RATES[ec]
                oracle_comp_costs_m2[ec] = comp_total * rate

            with localcontext() as ctx:
                ctx.prec = 100
                oracle_total_m2 = sum(oracle_comp_costs_m2.values(), Decimal('0'))
                oracle_total_eur = oracle_total_m2 * Decimal('62.5')
                oracle_sensitivity_eur = oracle_total_eur * Decimal('1.536')

            # 2. Query service baseline
            baseline_summary = cost_svc.get_cost(context)
            assert baseline_summary.official_total_cost_per_m2 == oracle_total_m2, (
                f"{cy.compartment_name}: baseline m2 mismatch {baseline_summary.official_total_cost_per_m2} != {oracle_total_m2}"
            )
            assert baseline_summary.official_total_cost_eur == oracle_total_eur
            assert baseline_summary.sensitivity_total_cost_eur == oracle_sensitivity_eur

            # 3. Create or reuse electric +20% scenario
            scen_name = f'Electric +20% {cy.compartment_name}'
            cmd = ScenarioCommand(
                request_id=f'oracle-req-{cy.compartment_name}-elec20',
                baseline_version_id=baseline_summary.tariff_version,
                name=scen_name,
                publication=pub,
                changes=(
                    ScenarioChange(EnergyComponent.ELEC_HIGH, 'EUR/kWh', change_percent=Decimal('20.0')),
                    ScenarioChange(EnergyComponent.ELEC_LOW, 'EUR/kWh', change_percent=Decimal('20.0')),
                ),
                analysis_context=context,
            )
            scenario_obj = cost_svc.execute_scenario_command(cmd)
            conn.commit()

            # Oracle calculation for scenario
            scenario_high_rate = Decimal('0.08') * Decimal('1.20')
            scenario_low_rate = Decimal('0.04') * Decimal('1.20')
            with localcontext() as ctx:
                ctx.prec = 100
                oracle_scen_costs_m2 = {
                    EnergyComponent.HEAT: oracle_comp_costs_m2[EnergyComponent.HEAT],
                    EnergyComponent.ELEC_HIGH: oracle_comp_sums[EnergyComponent.ELEC_HIGH] * scenario_high_rate,
                    EnergyComponent.ELEC_LOW: oracle_comp_sums[EnergyComponent.ELEC_LOW] * scenario_low_rate,
                }
                oracle_scen_total_m2 = sum(oracle_scen_costs_m2.values(), Decimal('0'))
                oracle_scen_total_eur = oracle_scen_total_m2 * Decimal('62.5')
                oracle_scen_delta_m2 = oracle_scen_total_m2 - oracle_total_m2
                oracle_scen_delta_eur = oracle_scen_total_eur - oracle_total_eur

            comparison = cost_svc.compare_scenarios(
                context,
                scenario_version=scenario_obj.scenario_version,
                baseline_tariff_version=baseline_summary.tariff_version,
            )
            assert comparison.scenario.official_total_cost_per_m2 == oracle_scen_total_m2
            assert comparison.delta_total_cost_per_m2 == oracle_scen_delta_m2
            assert comparison.delta_total_cost_eur == oracle_scen_delta_eur

            # Write individual DTO outputs
            OUT.joinpath(f'actual-cost-{cy.compartment_name}.json').write_text(to_json(baseline_summary) + '\n')
            OUT.joinpath(f'actual-comparison-{cy.compartment_name}.json').write_text(to_json(comparison) + '\n')

            reports.append({
                'compartment': cy.compartment_name,
                'cycle_id': cy.cycle_id,
                'resource_days': len(raw_resources),
                'source_sums': {ec.value: str(v) for ec, v in oracle_comp_sums.items()},
                'baseline_cost_per_m2': str(oracle_total_m2),
                'baseline_cost_eur': str(oracle_total_eur),
                'sensitivity_cost_eur': str(oracle_sensitivity_eur),
                'scenario_cost_per_m2': str(oracle_scen_total_m2),
                'scenario_cost_eur': str(oracle_scen_total_eur),
                'delta_cost_per_m2': str(oracle_scen_delta_m2),
                'delta_cost_eur': str(oracle_scen_delta_eur),
                'baseline_match': True,
                'scenario_match': True,
            })

    reconciliation = {
        'publication_id': EXPECTED_PUB,
        'scope_id': pub.scope_id,
        'snapshot_id': pub.snapshot_id,
        'compartments': reports,
        'canonical_rates': {ec.value: str(r) for ec, r in HISTORICAL_RATES.items()},
        'all_match': True,
    }
    OUT.joinpath('actual-cost-reconciliation.json').write_text(json.dumps(reconciliation, indent=2) + '\n')
    print(json.dumps({'status': 'SUCCESS', 'compartments': len(reports), 'all_match': True}))


if __name__ == '__main__':
    run_oracle()
