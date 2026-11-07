"""Shared literal energy/production pipeline fixture, usable in both images."""
import csv
from datetime import date,timedelta
from decimal import Decimal
import hashlib,json
from types import SimpleNamespace
from uuid import uuid4
import psycopg
from psycopg.types.json import Jsonb
import pytest
from greenhouse.contracts import QueryContext
from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
from greenhouse.db.pipeline import run_pipeline,encode,membership,save_artifact
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries.energy import EnergyService

D0=date(2020,1,1)
EPOCH=date(1899,12,30)
CODES=('Heat_cons','ElecHigh','ElecLow')


def fixture_stage(connection,workflow,target,*,fault=None):
    with connection.transaction():
        info=membership(connection,workflow)
    target.mkdir()
    (target/'fixture.json').write_text(encode(info))
    return save_artifact(target, info | {'rows_in':info['member_count'],'rows_out':info['member_count']})


@pytest.fixture
def energy_scope(contracts_database, tmp_path):
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        def create(rows=(('3.6', '1', '0'), ('7.2', '0', '1')), *, areas=None,
                   basis_status='verified', assumption_enabled=True, production_area='50',
                   production_kind='synthetic_production_area', harvest_rows=None,
                   suspect_zero=(), missing_basis=()):
            tag = uuid4().hex
            site, comp, cycle, snapshot, scope, policy = (
                prefix + ':' + tag for prefix in ('site', 'comp', 'cycle', 'snapshot', 'scope', 'quality'))
            end = D0 + timedelta(days=len(rows))
            conn.execute("INSERT INTO core.site VALUES (%s,'Energy fixture','source-naive','fixture')", (site,))
            conn.execute("INSERT INTO core.compartment VALUES (%s,%s,'Energy fixture','fixture')", (comp, site))
            conn.execute("INSERT INTO core.team VALUES (%s,'Energy fixture',false,'fixture')", (site,))
            conn.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",
                         (cycle, comp, site, D0, end))
            conn.execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://energy','fixture')", (snapshot, tag*2))
            conn.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')", (scope, snapshot))
            conn.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,'Synthetic',%s,%s,%s,'fixture')",
                         (snapshot, comp, site, cycle))
            definition = {'nonnegative': list(CODES) + ['Irr', 'Drain', 'ProdA', 'ProdB'],
                          'suspect_zero': list(suspect_zero), 'off_mode_zero': []}
            conn.execute("INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,%s,%s,true,'synthetic energy policy')",
                         (policy, D0, end, Jsonb(definition), hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()))
            if basis_status == 'assumed_unverified':
                conn.execute("INSERT INTO config.assumption_application VALUES (%s,'AGC_SHARED_BASIS_V1',1,%s)",
                             (scope, assumption_enabled))
            areas = areas or (('62.5' if basis_status == 'assumed_unverified' else '100'),)*len(rows)
            basis_ids = []
            for measure in CODES + ('ProdA', 'ProdB'):
                energy = measure in CODES
                if measure in missing_basis or (energy and basis_status == 'unknown'):
                    continue
                for i in range(len(rows)):
                    status = basis_status if energy else 'verified'
                    area = areas[i] if energy else production_area
                    kind = ('growing_area_assumed' if status == 'assumed_unverified' else 'synthetic_energy_area') if energy else production_kind
                    assumption = ('AGC_SHARED_BASIS_V1', 1) if status == 'assumed_unverified' else (None, None)
                    identity = f'{tag}:{measure}:{i}'
                    conn.execute("INSERT INTO config.area_basis_version VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'literal fixture evidence')",
                        (identity, scope, comp, measure, i+1, Decimal(area), kind, status,
                         D0+timedelta(days=i), D0+timedelta(days=i+1), *assumption))
                    conn.execute("INSERT INTO config.basis_application VALUES (%s,%s,%s,'shared_analysis',%s,true)",
                                 (scope, comp, measure, identity))
                    basis_ids.append(identity)
            raw = tmp_path / tag / 'raw'
            folder = raw / 'Synthetic'
            folder.mkdir(parents=True)
            resources = folder / 'Resources.csv'
            with resources.open('w', newline='') as stream:
                writer = csv.writer(stream, lineterminator='\r\n')
                writer.writerow(HEADERS['Resources'])
                for i, values in enumerate(rows):
                    if values is None:
                        continue
                    fields = dict(zip(CODES, values)) | {'Irr': '1', 'Drain': '0', 'CO2_cons': '0'}
                    writer.writerow([str((D0-EPOCH).days+i)] + [fields.get(h.strip(), '') for h in HEADERS['Resources'][1:]])
            production = folder / 'Production.csv'
            harvest_rows = harvest_rows if harvest_rows is not None else ((len(rows)-1, '2', '1'),)
            with production.open('w', newline='') as stream:
                writer = csv.writer(stream, lineterminator='\r\n')
                writer.writerow(HEADERS['Production'])
                for i, a, b in harvest_rows:
                    fields = {'ProdA': a, 'ProdB': b}
                    writer.writerow([str((D0-EPOCH).days+i)] + [fields.get(h.strip(), '') for h in HEADERS['Production'][1:]])
            files = [{'path': 'Synthetic/'+path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'csv': {'data_rows': sum(v is not None for v in rows) if path == resources else len(harvest_rows)}}
                     for path in (resources, production)]
            (raw / 'manifest.json').write_text(json.dumps({'snapshot_id': snapshot, 'archive': {'sha256': tag*2}, 'files': files}))
            output = tmp_path / tag / 'output'
            result = run_pipeline(conn, raw, output, scope, policy, stage_function=fixture_stage)
            adapter = PostgresEnergyQueryAdapter(conn, raw_snapshots={snapshot: raw})
            service = EnergyService(adapter)
            publication = service.pin_publication(scope)
            context = QueryContext(scope, publication.publication_id, snapshot, cycle, (comp,), D0, end)
            return SimpleNamespace(conn=conn, tag=tag, comp=comp, cycle=cycle, snapshot=snapshot, scope=scope,
                policy=policy, raw=raw, output=output, result=result, adapter=adapter, service=service,
                publication=publication, context=context, basis_ids=basis_ids)
        yield create
