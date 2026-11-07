"""Verify every archived user/config version after physical restore or rebuild."""
import argparse
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path

from common import COORDINATOR, STATE, connect, historical_id, write_json
from greenhouse.clickhouse.cost import CostAdapter
from greenhouse.clickhouse.registry import reference
from greenhouse.clickhouse.repository import Repository
from greenhouse.contracts import CostScenario, PublicationRef, ScenarioCommand, from_dict, to_dict
from native_state import read_archive

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--mode', choices=('physical', 'rebuilt'), required=True)
parser.add_argument('--archive', type=Path, required=True)
args = parser.parse_args()
archive, checksum = read_archive(args.archive)
source = from_dict(PublicationRef, archive['source_publication'])
with closing(connect(action=True)) as client:
    target = reference(client, historical_id())
    if args.mode == 'physical':
        assert target == source, 'Physical restore changed a publication field'
    else:
        assert target.publication_id != source.publication_id, 'Raw rebuild must publish a new physical generation'
        assert (target.scope_id, target.snapshot_id) == (source.scope_id, source.snapshot_id)
    repo = Repository(client, COORDINATOR)
    rows = repo.configurations()['rows']
    actual = {(r['kind'], r['scope_id'], r['version_id']): r for r in rows}
    preserved = []
    mapped = []
    for row in archive['configurations']['rows']:
        key = (row['kind'], row['scope_id'], row['version_id'])
        current = actual[key]
        if args.mode == 'physical' or row['kind'] not in ('cost_scenario', 'replay'):
            assert current == row, ('Original envelope differs', key)
            preserved.append(key)
        elif row['kind'] == 'replay':
            before, after = json.loads(row['payload']), json.loads(current['payload'])
            expected = dict(before, source_publication_id=target.publication_id,
                source_config_hash=target.versions.config_bundle_hash)
            assert after == expected, 'Replay configuration changed beyond its explicit source mapping'
            mapped.append(key)
        else:
            original = from_dict(CostScenario, json.loads(row['payload'])['scenario'])
            context = original.analysis_context
            if context:
                context = replace(context, publication_id=target.publication_id)
            expected = replace(original, publication=target, analysis_context=context)
            command = ScenarioCommand(expected.request_id, expected.baseline_tariff_version,
                expected.name, target, expected.changes, expected.scenario_id, context)
            assert json.loads(current['payload']) == {'scenario': to_dict(expected), 'command': to_dict(command)}
            mapped.append(key)
    expected_scenarios = []
    for value in archive['scenarios']:
        original = from_dict(CostScenario, value)
        context = original.analysis_context
        if context:
            context = replace(context, publication_id=target.publication_id)
        expected_scenarios.append(replace(original, publication=target, analysis_context=context))
    scenarios = CostAdapter(client, COORDINATOR).list_scenarios(target.scope_id)
    assert tuple(expected_scenarios) == scenarios, 'Missing, extra, or changed scenario version'
    result = dict(status='passed', backend='clickhouse', mode=args.mode, archive_sha256=checksum,
        source_publication=source.publication_id, target_publication=target.publication_id,
        exact_configuration_versions=len(preserved), explicitly_mapped_versions=len(mapped),
        scenario_versions=len(scenarios), all_scenario_fields_preserved_except_explicit_physical_mapping=True,
        preserved_identities=preserved, mapped_identities=mapped)
    write_json(STATE / ('configuration-' + args.mode + '-proof.json'), result)
    print(json.dumps({k: result[k] for k in ('status', 'mode', 'exact_configuration_versions',
        'explicitly_mapped_versions', 'scenario_versions')}), flush=True)
