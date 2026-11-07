"""Honest ClickHouse state export and raw rebuild, without a live PostgreSQL DB.

The original envelopes remain in a checksummed native archive. Source-independent
configuration identities are preserved; physical publication/context references
are explicitly mapped after a fresh six-BR parity proof. Corrected source rows
are rejected: this rehearsal rebuilds the locked uncorrected raw snapshot.
"""
import argparse
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys

from common import COORDINATOR, STATE, connect, historical_id, write_json
from greenhouse.clickhouse.cli import migrate_all
from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.cost import CostAdapter, validate_rates
from greenhouse.clickhouse.encoding import digest
from greenhouse.clickhouse.energy_run import run
from greenhouse.clickhouse.registry import publication_envelope, reference
from greenhouse.clickhouse.repository import Repository
from greenhouse.contracts import CostScenario, PublicationRef, ScenarioCommand, from_dict, to_dict
from greenhouse.queries.cost import derive_scenario_rates

sys.path.insert(0, '/migration')
from read_business_reference import project as project_business

SUPPORTED = {'snapshot', 'area_basis', 'rule_schedule', 'harvest_rule', 'irrigation_rule',
    'tariff', 'water', 'harvest', 'threshold', 'energy', 'energy_workflow',
    'migration_source', 'migration_publication_map', 'cost_scenario', 'replay'}
DEFERRED = {'cost_scenario', 'replay'}


def selected_configs(envelope):
    selected = {}
    pins = envelope['configuration']['pins']
    for bundle, kind in (('water', 'water'), ('harvest', 'harvest'),
                         ('climate', 'threshold'), ('energy', 'energy')):
        ref = envelope['receipts'][bundle]['configuration_ref']
        matches = [p for p in pins if (p['kind'], p['version_id'], p['content_hash']) ==
            (kind, ref['version'], ref['hash'])]
        if len(matches) != 1:
            raise ValueError('Missing or ambiguous pipeline configuration pin')
        selected[kind] = dict(version=ref['version'], payload=json.loads(matches[0]['payload']))
    if envelope['receipts']['drain']['configuration_ref'] != envelope['receipts']['climate']['configuration_ref']:
        raise ValueError('Rehearsal requires the shared threshold configuration')
    return selected


def parity(path, target):
    proof = json.loads(path.read_text())
    if proof.get('status') != 'passed' or proof.get('all_six_brs') is not True or proof.get('clickhouse_publication') != target:
        raise ValueError('A passing full six-BR proof for this exact target is required')
    return proof


def read_archive(path):
    wrapper = json.loads(path.read_text())
    archive = wrapper['archive']
    if wrapper['sha256'] != digest(archive) or archive['format'] != 'greenhouse-native-rebuild-v1' or archive['backend'] != 'clickhouse':
        raise ValueError('Invalid native rebuild archive')
    if archive.get('corrected_source_rows') != 0:
        raise ValueError('Native raw rebuild requires an explicitly uncorrected source archive')
    registry = archive['configurations']
    if registry.get('format') != 'greenhouse-config-v1' or registry['checksum'] != digest(registry['rows']):
        raise ValueError('Invalid original configuration registry checksum')
    unknown = {r['kind'] for r in registry['rows']} - SUPPORTED
    if unknown:
        raise ValueError('Unsupported configuration kinds: ' + repr(sorted(unknown)))
    for row in registry['rows']:
        if row['schema_version'] != 1 or row['content_hash'] != digest(json.loads(row['payload'])):
            raise ValueError('Corrupt/unsupported native configuration envelope')
    return archive, wrapper['sha256']


def export(args):
    with closing(connect(action=True)) as client:
        pub = reference(client, historical_id())
        proof = parity(args.proof, pub.publication_id)
        repo = Repository(client, COORDINATOR)
        corrected = client.query("SELECT count() AS n FROM source_revision WHERE revision!=1 OR correction_id!=''")[0]['n']
        if int(corrected):
            raise ValueError('Export of corrections requires a separate correction replay archive')
        registry = repo.configurations()
        if {r['kind'] for r in registry['rows']} - SUPPORTED:
            raise ValueError('Unsupported configuration kind; do not silently drop user state')
        scenarios = CostAdapter(client, COORDINATOR).list_scenarios(pub.scope_id)
        archive = dict(format='greenhouse-native-rebuild-v1', backend='clickhouse',
            source_publication=to_dict(pub), source_envelope=publication_envelope(client, pub.publication_id),
            configurations=registry, scenarios=[to_dict(s) for s in scenarios],
            business=project_business(client, pub.publication_id, args.raw)['results'],
            parity_proof=proof, corrected_source_rows=0,
            mapping_policy='Keep original envelopes in archive; map publication/context and replay source references explicitly')
        write_json(args.archive, {'archive': archive, 'sha256': digest(archive)})
        print(json.dumps({'stage': 'native_exported', 'backend': 'clickhouse',
            'sha256': digest(archive), 'scenario_versions': len(scenarios),
            'configuration_versions': len(registry['rows'])}), flush=True)


def rebuild(args):
    archive, checksum = read_archive(args.archive)
    source = from_dict(PublicationRef, archive['source_publication'])
    configs = selected_configs(archive['source_envelope'])
    with closing(connect()) as client:
        migrate_all(client, COORDINATOR)
        if int(client.query('SELECT count() AS n FROM source_revision')[0]['n']):
            raise ValueError('Raw rebuild requires an empty warehouse, not checkpoint resume')
        repo = Repository(client, COORDINATOR)
        rows = [r for r in archive['configurations']['rows'] if r['kind'] not in DEFERRED]
        repo.restore_configurations(dict(format='greenhouse-config-v1', rows=rows, checksum=digest(rows)))
        # Archive source-bound state without making it an operational old pointer.
        repo.register_configuration('native_rebuild_archive', source.scope_id, checksum,
            'native-rebuild-archive:' + checksum, archive)
        print(json.dumps({'stage':'native_rebuild_inputs_verified','backend':'clickhouse',
            'archive_sha256':checksum,'configuration_versions_restored':len(rows),'source_publication':source.publication_id}),flush=True)
        result = run(client, COORDINATOR, args.raw, STATE / 'output', source.scope_id,
            args.workflow, *(configs[k]['payload'] for k in ('water', 'harvest', 'threshold', 'energy')),
            **{k + '_version': configs[k]['version'] for k in configs})
        write_json(STATE / 'historical.json', result)
        write_json(STATE / 'native-rebuild-input.json', dict(archive_sha256=checksum,
            restored_independent_versions=len(rows), source_publication=source.publication_id,
            target_publication=result['publication_id'], fresh_raw_load=True))
        print(json.dumps({'stage': 'native_raw_rebuilt', 'publication_id': result['publication_id'],
            'load_id': result['load_id']}), flush=True)


def import_scenarios(args):
    archive, checksum = read_archive(args.archive)
    source = from_dict(PublicationRef, archive['source_publication'])
    proof = parity(args.proof, historical_id())
    if proof['postgres_publication'] != archive['parity_proof']['postgres_publication']:
        raise ValueError('Rebuilt target was compared to another baseline')
    with closing(connect(action=True)) as client:
        repo = Repository(client, COORDINATOR)
        cost = CostAdapter(client, COORDINATOR)
        with writer_lock(COORDINATOR, client):
            target = cost.publication(historical_id())
            if (source.scope_id, source.snapshot_id) != (target.scope_id, target.snapshot_id):
                raise ValueError('Source and rebuilt scope/snapshot differ')
            actual = project_business(client, target.publication_id, args.raw)['results']
            if actual != archive['business']:
                raise ValueError('Native source/target business projection differs')
            expected, families, requests = {}, {}, set()
            for original in sorted((from_dict(CostScenario, v) for v in archive['scenarios']), key=lambda s: (s.scenario_id, s.version)):
                if original.publication != source or original.request_id in requests or original.scenario_version in expected:
                    raise ValueError('Foreign or duplicated native scenario')
                requests.add(original.request_id)
                context = original.analysis_context
                if context is not None:
                    context.matches(source)
                    context = replace(context, publication_id=target.publication_id)
                    cost.cycle_window(context)
                candidate = replace(original, publication=target, analysis_context=context)
                baseline = cost.get_tariff(candidate.baseline_tariff_version)
                if baseline is None or baseline.data_scope_id != target.scope_id:
                    raise ValueError('Scenario baseline is missing or foreign')
                validate_rates(candidate.scenario_version, 'EUR', candidate.rates)
                if candidate.rates != derive_scenario_rates(baseline, candidate.scenario_version, candidate.name, candidate.changes):
                    raise ValueError('Native scenario derived rates differ')
                expected[candidate.scenario_version] = candidate
                families.setdefault(candidate.scenario_id, []).append(candidate.version)
            for versions in families.values():
                if versions != list(range(1, max(versions) + 1)):
                    raise ValueError('Native scenario chain has gaps')
            for saved in cost.list_scenarios(target.scope_id):
                if saved.scenario_id in families and expected.get(saved.scenario_version) != saved:
                    raise ValueError('Existing target scenario is divergent or has extra versions')
            inserted = 0
            for candidate in expected.values():
                command = ScenarioCommand(candidate.request_id, candidate.baseline_tariff_version,
                    candidate.name, target, candidate.changes, candidate.scenario_id, candidate.analysis_context)
                inserted += repo._register_configuration_locked('cost_scenario', target.scope_id,
                    candidate.scenario_version, 'scenario-command:' + candidate.request_id,
                    {'scenario': to_dict(candidate), 'command': to_dict(command)})
            for version, candidate in expected.items():
                if cost.get_scenario(version) != candidate:
                    raise ValueError('Native scenario mapped readback differs')
            mapping = dict(backend='clickhouse', archive_sha256=checksum,
                source_publication=to_dict(source), target_publication=to_dict(target),
                parity_proof_hash=digest(proof), native_business_exact=True,
                scenario_versions=sorted(expected), preserved_fields='All except publication and analysis-context publication ID')
            repo._register_configuration_locked('native_rebuild_map', target.scope_id, checksum,
                'native-rebuild-map:' + checksum, mapping)
            result = dict(status='passed', **mapping, inserted=inserted, reused=len(expected) - inserted)
            write_json(STATE / 'native-scenario-import.json', result)
            print(json.dumps({'stage': 'native_scenarios_imported', 'versions': len(expected), 'inserted': inserted}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('export', 'rebuild', 'import-scenarios'))
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--raw', type=Path, required=True)
    parser.add_argument('--proof', type=Path)
    parser.add_argument('--workflow')
    args = parser.parse_args()
    if args.mode != 'rebuild' and args.proof is None:
        parser.error('--proof is required for export/import')
    if args.mode == 'rebuild' and not args.workflow:
        parser.error('--workflow is required for raw rebuild')
    {'export': export, 'rebuild': rebuild, 'import-scenarios': import_scenarios}[args.mode](args)


if __name__ == '__main__':
    main()
