"""Verify exact byte parity between browser downloads and Python export service."""
import hashlib
import json
import os
from pathlib import Path
import psycopg

from greenhouse.app import cost_exports, wiring
from greenhouse.contracts import QueryContext, from_json, ScenarioComparison, CostScenario
from greenhouse.settings import Settings

EVIDENCE = Path(os.environ.get('EVIDENCE_DIR', '/evidence'))
EXPECTED_PUB = os.environ.get('EXPECTED_PUBLICATION', '892b4cd58cdf4d099510974d91fa64ab')
RAW = Path('/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68')


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def check_parity():
    session_file = EVIDENCE / 'session.json'
    assert session_file.exists(), "session.json not found"
    session_data = json.loads(session_file.read_text())
    downloads = session_data['downloads']

    with psycopg.connect(**Settings.from_env().connection_kwargs()) as conn:
        cost_svc = wiring.cost_service(conn, {EXPECTED_PUB: RAW})
        pub = cost_svc.energy.adapter.publication(EXPECTED_PUB)
        catalog = wiring.catalog_service(conn)
        cycles = catalog.cycles(pub)
        aicu_cycle = next(c for c in cycles if c.compartment_name == 'AICU')

        context = QueryContext(
            pub.scope_id, pub.publication_id, pub.snapshot_id,
            aicu_cycle.cycle_id, (aicu_cycle.compartment_id,),
            aicu_cycle.start, aicu_cycle.end,
        )

        scenario_ver = None
        for fname in downloads:
            if 'scenario_req-' in fname:
                tag = fname.split('-2020-05-30-')[1].rsplit('-', 1)[0]
                scenario_ver = tag.replace('scenario_req-', 'scenario:req-').replace('_v', ':v')
                break

        assert scenario_ver is not None, "Could not extract scenario_version from download names"
        scen = cost_svc.get_scenario(scenario_ver)
        assert scen is not None, f"Scenario not found: {scenario_ver}"
        baseline_tariff = cost_svc.get_tariff(scen.baseline_tariff_version)
        comp = cost_svc.compare_scenarios(
            context,
            scenario_version=scen.scenario_version,
            baseline_tariff_version=scen.baseline_tariff_version,
        )

        # Generate expected exports
        expected_docs = {
            'summary': cost_exports.summary_csv(comp, scen),
            'components': cost_exports.components_csv(comp, scen),
            'days': cost_exports.days_csv(comp, scen),
            'weekly': cost_exports.weekly_csv(comp, scen),
            'rates': cost_exports.rates_csv(comp, baseline_tariff.rates, scen.rates, scen),
            'comp_json': cost_exports.comparison_json(comp),
            'scen_json': cost_exports.scenario_json(scen),
        }

        parity_report = {}
        all_match = True

        for fname, info in downloads.items():
            fpath = EVIDENCE / fname
            actual_bytes = fpath.read_bytes()
            actual_hash = sha256(actual_bytes)

            matched_key = None
            if 'summary.csv' in fname:
                matched_key = 'summary'
            elif 'components.csv' in fname:
                matched_key = 'components'
            elif 'days.csv' in fname:
                matched_key = 'days'
            elif 'weekly.csv' in fname:
                matched_key = 'weekly'
            elif 'rates.csv' in fname:
                matched_key = 'rates'
            elif 'comparison.json' in fname:
                matched_key = 'comp_json'
            elif 'scenario.json' in fname:
                matched_key = 'scen_json'

            if matched_key:
                exp = expected_docs[matched_key]
                exp_hash = sha256(exp)
                match = (actual_bytes == exp)
                if not match:
                    all_match = False
                parity_report[fname] = {
                    'actual_bytes': len(actual_bytes),
                    'actual_sha256': actual_hash,
                    'expected_sha256': exp_hash,
                    'match': match,
                }
            else:
                parity_report[fname] = {
                    'actual_bytes': len(actual_bytes),
                    'actual_sha256': actual_hash,
                    'note': 'unmatched download',
                }

        report = {
            'all_match': all_match,
            'files': parity_report,
        }
        (EVIDENCE / 'byte-parity.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'all_match': all_match, 'count': len(parity_report)}))
        assert all_match, "Byte parity failed"


if __name__ == '__main__':
    check_parity()
