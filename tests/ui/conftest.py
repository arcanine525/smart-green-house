"""Water app test environment: guarded disposable database + Streamlit AppTest.

Runs inside the UI image (psycopg/pytest in requirements-ui.lock); no host
venv and no Spark — the pipeline stage is replaced by a small prepared
artifact, matching the backend contract tests. Test databases use the guarded
test_ prefix and are dropped on teardown unless GREENHOUSE_KEEP_TEST_DB=1.
"""
import csv
from datetime import date, timedelta
import hashlib
import importlib.resources
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql as pgsql
from psycopg.types.json import Jsonb
import pytest

from greenhouse.contracts import QueryContext
from greenhouse.db.catalog import PostgresCatalogAdapter


def pytest_ignore_collect(collection_path, config):
    """Processing image has no Streamlit: the whole UI suite is ignored there."""
    import importlib.util
    if importlib.util.find_spec('streamlit') is None:
        return True
    return None
from greenhouse.db.irrigation_rules import register_irrigation_rule
from greenhouse.db.migrate import migrate
from greenhouse.db.pipeline import encode, membership, run_pipeline, save_artifact
from greenhouse.db.query_adapter import PostgresQueryAdapter
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries import IrrigationService
from greenhouse.queries.catalog import CatalogService
from greenhouse.settings import Settings


@pytest.fixture(scope='session')
def admin_kwargs():
    return Settings.from_env().connection_kwargs()


def _new_database(admin_kwargs, prefix):
    """Guarded disposable database; dropped by the caller unless kept."""
    name = prefix + uuid4().hex[:12]
    keep = os.environ.get('GREENHOUSE_KEEP_TEST_DB') == '1'
    with psycopg.connect(**admin_kwargs, autocommit=True) as admin:
        admin.execute(pgsql.SQL('CREATE DATABASE {}').format(pgsql.Identifier(name)))
    target = admin_kwargs | {'dbname': name}
    with psycopg.connect(**target) as connection:
        migrate(connection)
    print('UI fixture database created:', name)
    return name, target, keep


def _drop_database(admin_kwargs, name, keep, prefix):
    if keep:
        print('UI fixture database retained (GREENHOUSE_KEEP_TEST_DB=1):', name)
        return
    assert name.startswith('test_ui_')
    with psycopg.connect(**admin_kwargs, autocommit=True) as admin:
        admin.execute(pgsql.SQL('DROP DATABASE {} WITH (FORCE)').format(pgsql.Identifier(name)))
    print('UI fixture database dropped:', name)


def seed_legacy_publication(connection, *, start=date(2020, 1, 1), end=date(2020, 1, 5)):
    """Sealed publication from a pre-mart workflow: catalog shows it unusable."""
    tag = uuid4().hex
    site, comp, cycle, snapshot, scope, policy = (
        name + ':' + tag for name in ('site', 'comp', 'cycle', 'snapshot', 'scope', 'quality'))
    connection.execute("INSERT INTO core.site VALUES (%s,'Fixture','source-naive','fixture')", (site,))
    connection.execute("INSERT INTO core.compartment VALUES (%s,%s,'Fixture','fixture')", (comp, site))
    connection.execute("INSERT INTO core.team VALUES (%s,'Fixture',false,'fixture')", (site,))
    connection.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",
                       (cycle, comp, site, start, end))
    connection.execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://fixture','fixture')",
                       (snapshot, tag * 2))
    connection.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')", (scope, snapshot))
    connection.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,'Synthetic',%s,%s,%s,'fixture')",
                       (snapshot, comp, site, cycle))
    connection.execute(
        "INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,'{}'::jsonb,%s,true,'legacy fixture policy')",
        (policy, start, end, hashlib.sha256(b'{}').hexdigest()))
    run = 'run:' + tag
    connection.execute("INSERT INTO meta.pipeline_run VALUES (%s,%s,%s,'core_loaded','fixture-parser',%s,%s)",
                       (run, scope, snapshot, policy, Jsonb({'fixture': tag})))
    publication = 'pub-legacy:' + tag
    connection.execute("INSERT INTO meta.publication VALUES (%s,%s,%s,%s,'fixture-hash',NULL,now(),'provisional')",
                       (publication, scope, run, policy))
    workflow = 'wf-legacy:' + tag
    connection.execute(
        "INSERT INTO meta.workflow_run VALUES (%s,%s,%s,'{}'::jsonb,'{\"code\": \"pipeline-v1-legacy\"}'::jsonb,"
        "'completed',%s,%s,now(),now())", (workflow, scope, snapshot, run, publication))
    connection.execute("INSERT INTO meta.publication_seal VALUES (%s,%s,'fixture',0,'fixture')",
                       (publication, workflow))
    connection.execute("INSERT INTO meta.current_publication VALUES (%s,%s)", (scope, publication))
    return SimpleNamespace(connection=connection, scope=scope, snapshot=snapshot,
                           publication_id=publication, cycle=cycle, comp=comp, start=start, end=end)


@pytest.fixture(scope='session')
def empty_database(admin_kwargs):
    """Migrated schema with nothing published: the app's true empty state."""
    name, target, keep = _new_database(admin_kwargs, 'test_ui_empty_')
    try:
        yield target
    finally:
        _drop_database(admin_kwargs, name, keep, 'test_ui_empty_')


@pytest.fixture(scope='session')
def legacy_database(admin_kwargs):
    """Fresh database holding only a pre-mart legacy publication."""
    name, target, keep = _new_database(admin_kwargs, 'test_ui_legacy_')
    try:
        with psycopg.connect(**target, autocommit=True) as connection:
            seed_legacy_publication(connection)
        yield target
    finally:
        _drop_database(admin_kwargs, name, keep, 'test_ui_legacy_')


@pytest.fixture(scope='session')
def contracts_database(admin_kwargs):
    name, target, keep = _new_database(admin_kwargs, 'test_ui_contracts_')
    try:
        yield target
    finally:
        _drop_database(admin_kwargs, name, keep, 'test_ui_contracts_')


def fixture_stage(connection, workflow, target, *, fault=None):
    """Prepared artifact for the production coordinator, not a query stub."""
    with connection.transaction():
        info = membership(connection, workflow)
    target.mkdir()
    (target / 'fixture.json').write_text(encode(info))
    return save_artifact(target, info | {'rows_in': info['member_count'], 'rows_out': info['member_count']})


@pytest.fixture
def published_scope(contracts_database, tmp_path):
    """Publish one synthetic scope with the real water mart; no UI fixtures."""
    with psycopg.connect(**contracts_database, autocommit=True) as connection:
        def create(rows=(('10', '2'), ('30', '12')), *, basis='verified', start=date(2020, 1, 1),
                   end=date(2020, 1, 5), cycles=1):
            tag = uuid4().hex
            site, comp, cycle, snapshot, scope, policy = (
                name + ':' + tag for name in ('site', 'comp', 'cycle', 'snapshot', 'scope', 'quality'))
            connection.execute("INSERT INTO core.site VALUES (%s,'Fixture','source-naive','fixture')", (site,))
            connection.execute("INSERT INTO core.compartment VALUES (%s,%s,'Fixture','fixture')", (comp, site))
            connection.execute("INSERT INTO core.team VALUES (%s,'Fixture',false,'fixture')", (site,))
            connection.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",
                               (cycle, comp, site, start, end))
            connection.execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://fixture','fixture')",
                               (snapshot, tag * 2))
            connection.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')",
                               (scope, snapshot))
            connection.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,'Synthetic',%s,%s,%s,'fixture')",
                               (snapshot, comp, site, cycle))
            # Optional second cycle/compartment in the SAME publication, with a
            # different window and no resource data (its facts simply stay absent).
            comp_b = cycle_b = None
            if cycles == 2:
                comp_b, cycle_b = comp + ':b', cycle + ':b'
                second_start, second_end = start + timedelta(days=31), start + timedelta(days=36)
                connection.execute("INSERT INTO core.compartment VALUES (%s,%s,'Fixture B','fixture')",
                                   (comp_b, site))
                connection.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",
                                   (cycle_b, comp_b, site, second_start, second_end))
                connection.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,'Second',%s,%s,%s,'fixture')",
                                   (snapshot, comp_b, site, cycle_b))
            definition = {'nonnegative': ['Irr', 'Drain'], 'suspect_zero': [], 'off_mode_zero': []}
            connection.execute(
                "INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,%s,%s,true,'synthetic quality fixture')",
                (policy, start, end, Jsonb(definition),
                 hashlib.sha256(encode(definition).encode()).hexdigest()))
            if basis == 'assumed_unverified':
                connection.execute("INSERT INTO config.assumption_application VALUES (%s,'AGC_SHARED_BASIS_V1',1,true)",
                                   (scope,))
            if basis != 'unknown':
                for code in ('Irr', 'Drain'):
                    assumption = ('AGC_SHARED_BASIS_V1', 1) if basis == 'assumed_unverified' else (None, None)
                    connection.execute(
                        "INSERT INTO config.area_basis_version VALUES "
                        "(%s,%s,%s,%s,1,100,'synthetic_water_area',%s,%s,%s,%s,%s,'explicit synthetic basis')",
                        (tag + code, scope, comp, code, basis, start, end, *assumption))
                    connection.execute(
                        "INSERT INTO config.basis_application VALUES (%s,%s,%s,'shared_analysis',%s,true)",
                        (scope, comp, code, tag + code))
            raw = tmp_path / tag / 'raw'
            (raw / 'Synthetic').mkdir(parents=True)
            path = raw / 'Synthetic/Resources.csv'
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream, lineterminator='\r\n')
                writer.writerow(HEADERS['Resources'])
                for index, (irr, drain) in enumerate(rows):
                    values = {'Irr': irr, 'Drain': drain}
                    serial = str((start - date(1899, 12, 30)).days + index)
                    writer.writerow([serial] + [values.get(header.strip(), '1')
                                                for header in HEADERS['Resources'][1:]])
            source = {'snapshot_id': snapshot, 'archive': {'sha256': tag * 2},
                      'files': [{'path': 'Synthetic/Resources.csv',
                                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'csv': {'data_rows': len(rows)}}]}
            (raw / 'manifest.json').write_text(json.dumps(source))
            output = tmp_path / tag / 'output'
            run_pipeline(connection, raw, output, scope, policy, stage_function=fixture_stage)
            adapter = PostgresQueryAdapter(connection, raw_snapshots={snapshot: raw})
            service = IrrigationService(adapter)
            publication = service.pin_publication(scope)
            context = QueryContext(scope, publication.publication_id, snapshot, cycle, (comp,),
                                   start, min(start + timedelta(days=2), end))
            catalog = CatalogService(PostgresCatalogAdapter(connection))
            return SimpleNamespace(connection=connection, scope=scope, comp=comp, cycle=cycle,
                                   comp_b=comp_b, cycle_b=cycle_b, snapshot=snapshot, raw=raw,
                                   output=output, adapter=adapter, service=service, catalog=catalog,
                                   publication=publication, context=context, start=start, end=end)
        yield create


@pytest.fixture
def legacy_scope(contracts_database):
    """Legacy scope in the shared database: visible but unusable in catalog."""
    with psycopg.connect(**contracts_database, autocommit=True) as connection:
        seeded = seed_legacy_publication(connection)
        seeded.catalog = CatalogService(PostgresCatalogAdapter(connection))
        yield seeded


@pytest.fixture
def app_path():
    """Location of the production home script inside the UI image."""
    with importlib.resources.as_file(importlib.resources.files('greenhouse.app') / 'home.py') as path:
        yield str(path)
