"""Pin a verified native AGC walkthrough and derive independent raw answers.

Run only in the disposable T50 rehearsal copy, never a production/source DB.
The immutable historical publication stays unchanged; the water rule is an
explicit demo-only query configuration, not an agronomic recommendation.
"""
import argparse
from contextlib import closing
import csv
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, '/repro')
from common import COORDINATOR, STATE, connect, historical_id, write_json
from greenhouse.clickhouse.registry import reference, publication_envelope
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.cost import CostAdapter
from greenhouse.contracts import IrrigationRuleProfile, to_dict
from greenhouse.sensor.simulator import STANDARD_AGC_SHA

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--publication-id', required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
raw = Path(os.environ['GREENHOUSE_RAW_DIR']) / 'agc-2nd-2019' / STANDARD_AGC_SHA
manifest = json.loads((raw/'manifest.json').read_text())


def source_rows(relative):
    path = raw/relative
    expected = next(row['sha256'] for row in manifest['files'] if row['path'] == relative)
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert actual == expected
    with path.open(newline='', encoding='utf-8-sig') as stream:
        rows = [{k.strip(): v for k, v in row.items()} for row in csv.DictReader(stream)]
    for row in rows:
        row['source_day'] = date(1899, 12, 30) + timedelta(days=int(Decimal(next(value for key,value in row.items() if key.lower()=='%time'))))
    return actual, rows


resource_hash, all_resources = source_rows('Automatoes/Resources.csv')
resources = [r for r in all_resources if date(2020,3,2) <= r['source_day'] <= date(2020,3,3)]
assert len(resources) == 2
area = Decimal('62.5')
irrigation = sum((Decimal(r['Irr']) for r in resources), Decimal(0))
drain = sum((Decimal(r['Drain']) for r in resources), Decimal(0))
energy = sum(((Decimal(r['Heat_cons'])/Decimal('3.6') + Decimal(r['ElecHigh']) + Decimal(r['ElecLow']))*area for r in resources), Decimal(0))


def cost(high, low):
    return sum(((Decimal(r['Heat_cons'])*Decimal('0.0083') + Decimal(r['ElecHigh'])*Decimal(high) +
        Decimal(r['ElecLow'])*Decimal(low))*area for r in resources), Decimal(0))


production_hash, production = source_rows('Digilog/Production.csv')
harvest = [r for r in production if r['source_day'] == date(2020,2,19)]
assert len(harvest) == 1
grade_a, grade_b = Decimal(harvest[0]['ProdA']), Decimal(harvest[0]['ProdB'])
assert (irrigation, drain, grade_a, grade_b) == tuple(map(Decimal, ('9.36','3.89','0.05','0.10')))
with closing(connect(action=True)) as client:
    assert args.publication_id == historical_id()
    publication = reference(client, args.publication_id)
    assert publication.versions.code_version == 'clickhouse-warehouse-v1'
    envelope = publication_envelope(client, publication.publication_id)
    assert set(envelope['required_bundles']) == {'water','harvest','climate','drain','energy'}
    profile = IrrigationRuleProfile('walkthrough-water:Automatoes:v1','walkthrough-water:Automatoes',1,
        publication.scope_id,'Automatoes',Decimal('20'),Decimal('40'),Decimal('0'),Decimal('20'),
        'Synthetic walkthrough thresholds only; not agronomic guidance',True,'demo_rule')
    Repository(client,COORDINATOR).register_configuration('irrigation_rule',publication.scope_id,
        profile.rule_version,'walkthrough-water:'+publication.publication_id,to_dict(profile))
    result = dict(format='greenhouse-walkthrough-v2',purpose='isolated_walkthrough',backend='clickhouse',
        project=os.environ['GREENHOUSE_REHEARSAL_PROJECT'], database=client.settings.database,
        publication=publication.publication_id, publication_reference=to_dict(publication),
        snapshot=publication.snapshot_id, archive_sha256=STANDARD_AGC_SHA,
        required_bundles=envelope['required_bundles'], configuration_hash=publication.versions.config_bundle_hash,
        added_demo_rule=to_dict(profile), scenario_versions_before=len(CostAdapter(client,COORDINATOR).list_scenarios(publication.scope_id)),
        raw_oracle=dict(resource_sha256=resource_hash,production_sha256=production_hash,
            area_assumption_m2=str(area),irrigation_l_m2=str(irrigation),drain_l_m2=str(drain),
            drain_paired_percent=str(100*drain/irrigation),energy_kwh=str(energy),
            baseline_eur=str(cost('0.08','0.04')),electric_plus20_eur=str(cost('0.096','0.048')),
            harvest_a_kg_m2=str(grade_a),harvest_b_kg_m2=str(grade_b),harvest_share_b_percent=str(100*grade_b/(grade_a+grade_b))),
        historical_parity_proof_sha256=hashlib.sha256((STATE/'parity/parity.json').read_bytes()).hexdigest(),
        pg_credentials_present=any(os.environ.get(k) for k in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD')))
    assert not result['pg_credentials_present']
    write_json(args.output,result)
    print(json.dumps(dict(status='passed',backend='clickhouse',publication=publication.publication_id,
        raw_oracle=result['raw_oracle'])),flush=True)
