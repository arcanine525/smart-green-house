"""Export migration metadata from one explicitly read-only PostgreSQL snapshot.

No telemetry/dump or credentials in this artifact. All immutable configuration
versions and source scenario/publication references are retained verbatim;
selected basis DTOs additionally drive the candidate's frozen configuration.
"""
import argparse
from datetime import date,datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from greenhouse.settings import Settings
from greenhouse.contracts import EffectiveAreaBasis,BasisStatus,ASSUMPTION_LABEL,to_dict
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
from greenhouse.db.cost_adapter import PostgresCostAdapter
from greenhouse.db.rule_schedule import read_rule_schedule
from greenhouse.clickhouse.water_run import water_configuration
from greenhouse.clickhouse.harvest_run import harvest_configuration
from greenhouse.clickhouse.threshold_run import threshold_configuration
from greenhouse.clickhouse.energy_run import energy_configuration

TABLES=('config.quality_policy_version','config.basis_assumption_version','config.assumption_application',
    'config.area_basis_version','config.basis_application','config.irrigation_rule_profile','config.harvest_rule_profile',
    'config.rule_schedule_revision','config.rule_schedule_segment','config.tariff_profile','config.tariff_rate',
    'config.cost_scenario','config.cost_scenario_rate','meta.correction','meta.publication','meta.publication_basis',
    'meta.publication_seal','meta.dataset_snapshot','meta.source_folder_mapping')


def encode(value):
    if isinstance(value,(Decimal,date,datetime)):return str(value) if isinstance(value,Decimal) else value.isoformat()
    raise ValueError('Unsupported metadata value: '+type(value).__name__)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    settings=Settings.from_env()
    options=settings.connection_kwargs()|{'options':'-c default_transaction_read_only=on'}
    with psycopg.connect(**options,row_factory=dict_row) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        assert connection.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'
        # Repositories returning tuple rows use their own cursor factories.
        connection.row_factory=psycopg.rows.tuple_row
        adapter=PostgresEnergyQueryAdapter(connection);pub=adapter.publication(args.publication_id)
        catalog=PostgresCatalogAdapter(connection);cycles=catalog.catalog_cycles(pub.publication_id)
        def rows(sql,params=()):
            with connection.cursor(row_factory=dict_row) as cursor:return cursor.execute(sql,params).fetchall()
        tables={name:sorted(rows('SELECT * FROM '+name),key=lambda r:json.dumps(r,sort_keys=True,default=encode)) for name in TABLES}
        basis_rows=rows('''SELECT b.*,a.statement AS assumption_statement,a.evidence_reference AS assumption_evidence
            FROM config.area_basis_version b LEFT JOIN config.basis_assumption_version a
            ON a.assumption_id=b.assumption_id AND a.version=b.assumption_version WHERE b.data_scope_id=%s
            ORDER BY b.basis_version_id''',(pub.scope_id,))
        bases=[EffectiveAreaBasis(r['basis_version_id'],r['version'],r['data_scope_id'],r['compartment_id'],r['measure_code'],r['area_m2'],
            r['basis_kind'],BasisStatus(r['verification_status']),r['valid_from'],r['valid_to_exclusive'],r['evidence_reference'],
            r['assumption_id'],r['assumption_version'],r['assumption_statement'],r['assumption_evidence'],
            ASSUMPTION_LABEL if r['verification_status']=='assumed_unverified' else 'verified') for r in basis_rows]
        pins={r['basis_version_id'] for r in tables['meta.publication_basis'] if r['publication_id']==pub.publication_id and r['analysis_context']=='shared_analysis'}
        selected=[b for b in bases if b.basis_version_id in pins]
        def measure_metadata(b):
            return dict(basis_status=b.status.value,basis_version_id=b.basis_version_id,area_m2=str(b.area_m2),basis_kind=b.basis_kind,
                assumption_id=b.assumption_id,assumption_version=b.assumption_version,basis_label=b.label)
        comps=[c.compartment_id for c in cycles]
        config=dict(water=water_configuration(pub.snapshot_id,comps),harvest=harvest_configuration(pub.snapshot_id,comps),
            threshold=threshold_configuration(pub.snapshot_id,comps),energy=energy_configuration(pub.scope_id,pub.snapshot_id,comps))
        for kind,codes in (('water',('Irr','Drain')),('harvest',('ProdA','ProdB'))):
            config[kind]['bases']=[dict(compartment_id=b.compartment_id,measure_code=b.measure_code,start=b.valid_from.isoformat(),end=b.valid_to_exclusive.isoformat(),
                metadata=measure_metadata(b)) for b in selected if b.measure_code in codes]
        config['threshold']['storage_version']='threshold-stream-v2'
        config['energy']['bases']=[to_dict(b) for b in selected if b.measure_code in ('Heat_cons','ElecHigh','ElecLow','ProdA','ProdB')]
        rules=[to_dict(read_rule_schedule(connection,r['rule_version'])) for r in tables['config.rule_schedule_revision'] if r['data_scope_id']==pub.scope_id]
        irrigation=[];harvest=[]
        for c in cycles:
            irrigation.extend(to_dict(r) for r in catalog.catalog_rules(pub.scope_id,c.compartment_id))
            harvest.extend(to_dict(r) for r in catalog.catalog_harvest_rules(pub.scope_id,c.compartment_id))
        cost=PostgresCostAdapter(connection)
        tariffs=[to_dict(cost.get_tariff(r['tariff_version'])) for r in tables['config.tariff_profile'] if r['data_scope_id']==pub.scope_id]
        scenarios=[to_dict(s) for s in cost.list_scenarios(pub.scope_id)]
        result=dict(format=1,backend='postgres',read_only=True,isolation='repeatable read',source_database=settings.pg_database,
            source_publication=to_dict(pub),cycles=[to_dict(c) for c in cycles],tables=tables,bases=[to_dict(b) for b in bases],
            selected_basis_ids=sorted(pins),configurations=config,rules=rules,irrigation_rules=irrigation,harvest_rules=harvest,
            tariffs=tariffs,scenarios=scenarios)
        payload=json.dumps(result,default=encode,sort_keys=True,ensure_ascii=False,indent=2)+'\n'
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(payload)
        print(json.dumps({'status':'exported','read_only':True,'source_publication':pub.publication_id,'sha256':hashlib.sha256(payload.encode()).hexdigest(),
            'table_counts':{name:len(values) for name,values in tables.items()},'scenario_versions':len(scenarios),'basis_versions':len(bases)}))


if __name__=='__main__':main()
