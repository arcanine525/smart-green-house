"""Prepare explicit demo-only config and capture the publication in a copied DB."""
from decimal import Decimal
from datetime import date, timedelta
import csv
import hashlib
import json
import psycopg
from greenhouse.settings import Settings
from greenhouse.db.loader import AGC_SCOPE, AGC_SNAPSHOT, AGC_SHA
from greenhouse.db.irrigation_rules import register_irrigation_rule
from greenhouse.contracts import IrrigationRuleProfile, to_dict

settings = Settings.from_env()
assert settings.pg_database.startswith('test_walkthrough_'), 'Only a walkthrough copy may be changed'
raw = settings.raw_dir / 'agc-2nd-2019' / AGC_SHA
manifest = json.loads((raw / 'manifest.json').read_text())
resource = raw / 'Automatoes/Resources.csv'
checksum = hashlib.sha256(resource.read_bytes()).hexdigest()
assert next(f['sha256'] for f in manifest['files'] if f['path'] == 'Automatoes/Resources.csv') == checksum
rows = []
with resource.open(encoding='utf-8-sig') as source:
    for record in csv.DictReader(source):
        record = {k.strip(): v for k, v in record.items()}
        day = date(1899, 12, 30) + timedelta(days=float(record['%Time']))
        if date(2020, 3, 2) <= day <= date(2020, 3, 3):
            rows.append(record)
assert len(rows) == 2
def cost(high, low):
    return sum((Decimal(r['Heat_cons']) * Decimal('0.0083') +
                Decimal(r['ElecHigh']) * Decimal(high) + Decimal(r['ElecLow']) * Decimal(low)) * Decimal('62.5') for r in rows)
with psycopg.connect(**settings.connection_kwargs()) as connection:
    current = connection.execute('SELECT publication_id FROM meta.current_publication WHERE data_scope_id=%s', (AGC_SCOPE,)).fetchone()
    assert current == ('df3e4bcbacab45a5b86fc37d2f53c00a',), current
    profile = IrrigationRuleProfile('walkthrough-water:Automatoes:v1', 'walkthrough-water:Automatoes', 1,
        AGC_SCOPE, 'Automatoes', Decimal('20'), Decimal('40'), Decimal('0'), Decimal('20'),
        'Synthetic tutorial thresholds for T26 only; not agronomic guidance', True, 'demo_rule')
    if not connection.execute('SELECT 1 FROM config.irrigation_rule_profile WHERE rule_version=%s', (profile.rule_version,)).fetchone():
        register_irrigation_rule(connection, profile)
    scenarios = connection.execute('SELECT scenario_version,name,publication_id FROM config.cost_scenario ORDER BY scenario_version').fetchall()
    result = {'database': settings.pg_database, 'publication': current[0], 'snapshot': AGC_SNAPSHOT,
              'archive_sha256': AGC_SHA, 'added_demo_rule': to_dict(profile), 'scenarios': scenarios,
              'raw_oracle': {'resource_sha256': checksum, 'area_assumption_m2': '62.5',
                             'baseline_eur': str(cost('0.08', '0.04')),
                             'electric_plus20_eur': str(cost('0.096', '0.048'))}}
print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
