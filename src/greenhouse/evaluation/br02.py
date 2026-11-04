"""BR-02 acceptance evidence runner backing docs/evaluation/br_02.md.

Modes (python -m greenhouse.evaluation.br02 --mode <name>):
  fixture      — all ten ACs on a disposable guarded test_ database through
                 the real pipeline. Only the Spark stage is replaced by a
                 prepared artifact (parser, loader, mart, service and
                 adapter are the real code); the doc must state this.
  agc          — independent reconciliation of locked raw CSV values to the
                 published mart/service for an existing read-only publication
                 (all compartments plus the known week window). Read-only.
  performance  — BR-01 convention timings: first request kept separate, then
                 5 warmups + 30 measured runs per query, p95 against the
                 proposed <=2s target, container limits recorded. Read-only.

Existing databases are never dropped or modified; the fixture database is
created as test_br02_<hex> and dropped afterwards (kept only with explicit
GREENHOUSE_KEEP_TEST_DB=1). Output is a single JSON document on stdout or
--output; raw paths stay out of the report (ids only, no payload dumps).
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import platform
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from greenhouse.app import exports, wiring
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.pipeline import run_pipeline
from greenhouse.db.query_adapter import PostgresQueryAdapter
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries import IrrigationService
from greenhouse.queries.catalog import CatalogService
from greenhouse.settings import Settings

WEEK = (date(2020, 3, 2), date(2020, 3, 8))
EXCEL_EPOCH = date(1899, 12, 30)
P95_TARGET_SECONDS = Decimal('2')


def independent_percent(numerator, denominator):
    """Expected ratio computed in THIS script, not by the query service."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * 100 / Decimal(denominator)


def exact(value):
    return None if value is None else str(value)


# ---------------------------------------------------------------- fixture --
@contextmanager
def fixture_database(admin_kwargs):
    name = 'test_br02_' + uuid4().hex[:12]
    keep = os.environ.get('GREENHOUSE_KEEP_TEST_DB') == '1'
    from greenhouse.db.migrate import migrate
    with psycopg.connect(**admin_kwargs, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(name)))
    target = admin_kwargs | {'dbname': name}
    try:
        with psycopg.connect(**target) as connection:
            migrate(connection)
        yield target
    finally:
        if keep:
            print('fixture database retained:', name, file=sys.stderr)
        else:
            with psycopg.connect(**admin_kwargs, autocommit=True) as admin:
                admin.execute(psycopg.sql.SQL('DROP DATABASE {} WITH (FORCE)')
                              .format(psycopg.sql.Identifier(name)))


def artifact_stage(connection, workflow, target, *, fault=None):
    """Prepared artifact replacing ONLY the Spark stage (coordinator is real)."""
    from greenhouse.db.pipeline import membership, save_artifact
    with connection.transaction():
        info = membership(connection, workflow)
    target.mkdir()
    (target / 'fixture.json').write_text(json.dumps(info, sort_keys=True, separators=(',', ':')))
    return save_artifact(target, info | {'rows_in': info['member_count'],
                                         'rows_out': info['member_count']})


def publish_fixture(connection, scratch, rows, *, basis='verified',
                    start=date(2020, 1, 1), end=date(2020, 1, 5), rule=False):
    """One synthetic scope published through the real pipeline (no Spark)."""
    from greenhouse.contracts import IrrigationRuleProfile
    from greenhouse.db.irrigation_rules import register_irrigation_rule
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
    definition = {'nonnegative': ['Irr', 'Drain'], 'suspect_zero': [], 'off_mode_zero': []}
    connection.execute(
        "INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,%s,%s,true,'synthetic quality fixture')",
        (policy, start, end, Jsonb(definition), hashlib.sha256(
            json.dumps(definition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
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
            connection.execute("INSERT INTO config.basis_application VALUES (%s,%s,%s,'shared_analysis',%s,true)",
                               (scope, comp, code, tag + code))
    raw = scratch / tag / 'raw'
    (raw / 'Synthetic').mkdir(parents=True)
    path = raw / 'Synthetic/Resources.csv'
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream, lineterminator='\r\n')
        writer.writerow(HEADERS['Resources'])
        for index, (irr, drain) in enumerate(rows):
            serial = str((start - EXCEL_EPOCH).days + index)
            writer.writerow([serial] + [{'Irr': irr, 'Drain': drain}.get(header.strip(), '1')
                                        for header in HEADERS['Resources'][1:]])
    source = {'snapshot_id': snapshot, 'archive': {'sha256': tag * 2},
              'files': [{'path': 'Synthetic/Resources.csv',
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'csv': {'data_rows': len(rows)}}]}
    (raw / 'manifest.json').write_text(json.dumps(source))
    first = run_pipeline(connection, raw, scratch / tag / 'output', scope, policy,
                         stage_function=artifact_stage)
    rule_version = None
    if rule:
        profile = IrrigationRuleProfile('rule-version:' + tag, 'rule:' + tag, 1, scope, comp,
                                        Decimal('20'), Decimal('40'), Decimal('0'), Decimal('100'),
                                        'synthetic review thresholds; not agronomic guidance', True,
                                        'demo_rule')
        register_irrigation_rule(connection, profile)
        rule_version = profile.rule_version
    return {'scope': scope, 'comp': comp, 'cycle': cycle, 'snapshot': snapshot, 'policy': policy,
            'raw': raw, 'publication': first['publication_id'], 'rule_version': rule_version}


def context_for(e, start, end, rule_version=None):
    from greenhouse.contracts import QueryContext
    return QueryContext(e['scope'], e['publication'], e['snapshot'], e['cycle'], (e['comp'],),
                        start, end, rule_version)


def observation(service, context):
    summary = service.get_irrigation_summary(context)
    days = service.get_irrigation_days(context)
    return {
        'irrigation_observed': exact(summary.irrigation_observed_l_m2),
        'drain_observed': exact(summary.drain_observed_l_m2),
        'paired_irrigation': exact(summary.paired_irrigation_l_m2),
        'paired_drain': exact(summary.paired_drain_l_m2),
        'ratio_pct': exact(summary.drain_fraction_pct),
        'irrigation_coverage_pct': exact(summary.irrigation_coverage_pct),
        'drain_coverage_pct': exact(summary.drain_coverage_pct),
        'paired_coverage_pct': exact(summary.paired_coverage_pct),
        'expected_days': summary.expected_days,
        'status': summary.status.value,
        'basis_status': summary.basis_status.value,
        'basis_label': summary.basis_label,
        'rule_status': summary.rule_status.value,
        'rule_version': summary.rule_version,
        'outside_rule_days': summary.outside_rule_days,
        'unknown_days': summary.unknown_days,
        'suspect_days': summary.suspect_days,
        'drain_exceeds_irrigation_days': summary.drain_exceeds_irrigation_days,
        'reason_codes': list(summary.reason_codes),
        'day_ratios': [exact(day.drain_fraction_pct) for day in days],
        'day_rule_status': [day.rule_status.value for day in days],
    }


def run_fixture_acceptance(admin_kwargs):
    """Ten ACs against literal, independently hand-computed expectations."""
    results = {'mode': 'fixture', 'spark_stage': 'replaced_with_prepared_artifact',
               'real_components': ['parser', 'staging', 'loader', 'publication', 'water mart',
                                   'query adapter', 'service oracle', 'exports'], 'cases': []}

    def record(case_id, title, expected, observed):
        results['cases'].append({
            'ac': case_id, 'title': title, 'expected': expected, 'observed': observed,
            'pass': all(observed.get(key) == value for key, value in expected.items())
            and observed.get('pass', True),
        })

    with fixture_database(admin_kwargs) as target:
        with psycopg.connect(**target, autocommit=True) as connection:
            service = IrrigationService(PostgresQueryAdapter(connection))
            scratch = Path('/tmp/br02-fixture-' + uuid4().hex)

            # AC-01 ratio of sums (35%, not the 30% daily average).
            e = publish_fixture(connection, scratch, (('10', '2'), ('30', '12')))
            observed = observation(service, context_for(e, date(2020, 1, 1), date(2020, 1, 3)))
            record('AC-01', 'Hai ngày 10/2, 30/12 → tổng 40/14, tỷ lệ kỳ 35% (không phải trung bình ngày 30%)',
                   {'irrigation_observed': '40', 'drain_observed': '14', 'ratio_pct': '35',
                    'paired_irrigation': '40', 'paired_drain': '14',
                    'irrigation_coverage_pct': '100', 'drain_coverage_pct': '100',
                    'paired_coverage_pct': '100', 'status': 'complete'}, observed)

            # AC-02 zero quantities keep ratio NULL with a zero-denominator reason.
            e = publish_fixture(connection, scratch, (('0', '0'), ('0', '0')))
            observed = observation(service, context_for(e, date(2020, 1, 1), date(2020, 1, 3)))
            record('AC-02', 'Irr=Drain=0 → tổng 0, coverage 100%, tỷ lệ NULL (mẫu số 0)',
                   {'irrigation_observed': '0', 'drain_observed': '0', 'ratio_pct': None,
                    'irrigation_coverage_pct': '100', 'paired_coverage_pct': '100',
                    'reason_codes': ['no_irrigation_quantity']}, observed)

            # AC-03 partial pairing keeps observed totals and 50% paired coverage.
            e = publish_fixture(connection, scratch, (('10', '2'), ('30', None)))
            observed = observation(service, context_for(e, date(2020, 1, 1), date(2020, 1, 3)))
            record('AC-03', 'Ngày 2 thiếu Drain → Irr 40/Drain 2, paired 10/2, tỷ lệ 20%, paired coverage 50%',
                   {'irrigation_observed': '40', 'drain_observed': '2', 'paired_irrigation': '10',
                    'paired_drain': '2', 'ratio_pct': '20', 'paired_coverage_pct': '50',
                    'status': 'partial'}, observed)

            # AC-04 >100% is retained with the review flag, never clamped.
            e = publish_fixture(connection, scratch, (('5', '6'), ('5', '6')))
            observed = observation(service, context_for(e, date(2020, 1, 1), date(2020, 1, 3)))
            record('AC-04', 'Drain>Irr (5/6) → tỷ lệ 120% giữ nguyên kèm cờ rà soát',
                   {'ratio_pct': '120', 'drain_exceeds_irrigation_days': 2}, observed)

            # AC-05 nothing usable stays NULL with zero coverage.
            e = publish_fixture(connection, scratch, ((None, None), (None, None)))
            observed = observation(service, context_for(e, date(2020, 1, 1), date(2020, 1, 3)))
            record('AC-05', 'Không có số dùng được → tổng NULL, coverage 0%, không hiển thị 0',
                   {'irrigation_observed': None, 'drain_observed': None, 'ratio_pct': None,
                    'irrigation_coverage_pct': '0', 'status': 'unknown'}, observed)

            # AC-06 demo rule: equality bounds in-range, 50% day outside, version kept.
            e = publish_fixture(connection, scratch, (('10', '2'), ('10', '5')), rule=True)
            observed = observation(service, context_for(e, date(2020, 1, 1), date(2020, 1, 3),
                                                        rule_version=e['rule_version']))
            record('AC-06', 'Rule demo [20,40]%: ngày 20% trong dải (biên = trong), ngày 50% ngoài, giữ rule version',
                   {'day_ratios': ['20', '50'], 'day_rule_status': ['within_range', 'outside_range'],
                    'rule_status': 'configured', 'outside_rule_days': 1,
                    'rule_version': e['rule_version']}, observed)

            # AC-07 no rule selected → not_configured without any verdict.
            observed = observation(service, context_for(e, date(2020, 1, 1), date(2020, 1, 3)))
            record('AC-07', 'Chưa chọn rule → not_configured, không có kết luận đạt/không đạt',
                   {'rule_status': 'not_configured', 'rule_version': None, 'outside_rule_days': None},
                   observed)

            # AC-08 rerun of the same snapshot reuses the publication without doubling.
            e = publish_fixture(connection, scratch, (('10', '2'), ('30', '12')))
            second = run_pipeline(connection, e['raw'], scratch / (e['scope'] + '-rerun'), e['scope'],
                                  e['policy'], stage_function=artifact_stage)
            day_count = connection.execute(
                'SELECT day_count FROM mart.irrigation_publication WHERE publication_id=%s',
                (e['publication'],)).fetchone()[0]
            publication_count = connection.execute(
                'SELECT count(*) FROM meta.publication WHERE data_scope_id=%s', (e['scope'],)).fetchone()[0]
            record('AC-08', 'Chạy lại cùng snapshot → cùng publication (không tạo bản mới), không nhân dữ liệu '
                            '(duplicate/conflict chi tiết ở test unit được dẫn nguồn)',
                   {'pass': True},
                   {'pass': second['status'] == 'completed' and second['publication_id'] == e['publication']
                    and int(day_count) == 2 and publication_count == 1,
                    'second_status': second['status'],
                    'publication_id_stable': second['publication_id'] == e['publication'],
                    'scope_publication_count': publication_count,
                    'mart_day_count': int(day_count)})

            # AC-09 unknown basis keeps totals and NULLs the ratio.
            e = publish_fixture(connection, scratch, (('10', '2'), ('30', '12')), basis='unknown')
            observed = observation(service, context_for(e, date(2020, 1, 1), date(2020, 1, 3)))
            record('AC-09', 'Basis chưa xác minh → giữ tổng riêng, tỷ lệ NULL với reason basis',
                   {'irrigation_observed': '40', 'drain_observed': '14', 'ratio_pct': None,
                    'reason_codes': ['incompatible_or_unknown_basis', 'no_paired_days',
                                     'partial_coverage']}, observed)

            # AC-10 export agrees with the displayed action: values, filter, metadata.
            # UI days 2020-01-02..03 inclusive go through the app's single
            # inclusive→half-open conversion before the context is built.
            e = publish_fixture(connection, scratch, (('10', '2'), ('30', '12'), ('5', '1')))
            start_inclusive, end_exclusive = wiring.context_window(date(2020, 1, 2), date(2020, 1, 3))
            context = context_for(e, start_inclusive, end_exclusive)
            action = wiring.water_action(service, service.adapter.publication(e['publication']), context)
            text = exports.day_csv(action).decode('utf-8-sig')
            lines = text.splitlines()
            rows = list(csv.DictReader(io.StringIO('\n'.join(l for l in lines if not l.startswith('# ')))))
            record('AC-10', 'Xuất CSV đúng bộ lọc/DTO/metadata (chỉ ngày trong cửa sổ, giá trị exact, metadata đầy đủ)',
                   {'pass': True},
                   {'pass': [row['resource_date'] for row in rows] == ['2020-01-02', '2020-01-03']
                    and rows[0]['irrigation_l_m2'] == '30' and rows[1]['drain_l_m2'] == '1'
                    and any(f'scope={e["scope"]}' in line for line in lines)
                    and any('effective_window=[2020-01-02,2020-01-04)' in line for line in lines),
                    'exported_dates': [row['resource_date'] for row in rows],
                    'first_row_irrigation': rows[0]['irrigation_l_m2'] if rows else None,
                    'second_row_drain': rows[1]['drain_l_m2'] if len(rows) > 1 else None,
                    'scope_header_found': any(f'scope={e["scope"]}' in line for line in lines),
                    'window_header_found': any('effective_window=[2020-01-02,2020-01-04)' in line
                                               for line in lines)})
    return results


# -------------------------------------------------------------------- AGC --
def read_raw_resources(path, window):
    """Independent raw reader: csv + Decimal + documented Excel epoch."""
    irrigation, drain, days = Decimal(0), Decimal(0), 0
    first = last = None
    with open(path, newline='', encoding='utf-8-sig') as stream:
        for row in csv.DictReader(stream):
            when = EXCEL_EPOCH + timedelta(days=int(Decimal(row['%Time '])))
            if not window[0] <= when < window[1]:
                continue
            irrigation += Decimal(row['Irr'])
            drain += Decimal(row['Drain'])
            days += 1
            first = first or when
            last = when
    return irrigation, drain, days, first, last


def run_agc_reconciliation(kwargs, raw_root):
    """Locked raw values -> published mart/service, all compartments + week."""
    results = {'mode': 'agc', 'read_only': True, 'compartments': [], 'windows': {}}
    with psycopg.connect(**kwargs) as connection:
        catalog = CatalogService(PostgresCatalogAdapter(connection))
        probe = IrrigationService(PostgresQueryAdapter(connection))
        usable = catalog.usable()
        if len(usable) != 1:
            raise ValueError('Expected exactly one usable publication on this database')
        publication = wiring.resolve_publication(probe, usable[0].publication_id)
        mapping = wiring.raw_snapshot_mapping(raw_root)
        snapshot_dir = mapping.get(publication.snapshot_id)
        if snapshot_dir is None:
            raise ValueError('Raw snapshot for the pinned publication is not mounted')
        service = IrrigationService(PostgresQueryAdapter(
            connection, raw_snapshots={publication.snapshot_id: snapshot_dir}))
        archive = connection.execute('SELECT archive_checksum FROM meta.dataset_snapshot WHERE snapshot_id=%s',
                                     (publication.snapshot_id,)).fetchone()[0]
        workflow_span = connection.execute(
            'SELECT created_at,finished_at FROM meta.workflow_run WHERE publication_id=%s '
            'AND status=\'completed\' ORDER BY created_at DESC LIMIT 1', (publication.publication_id,)).fetchone()
        bundle_span = connection.execute(
            'SELECT started_at,finished_at FROM mart.irrigation_publication WHERE publication_id=%s',
            (publication.publication_id,)).fetchone()
        results['publication'] = {
            'publication_id': publication.publication_id, 'scope_id': publication.scope_id,
            'snapshot_id': publication.snapshot_id, 'archive_checksum': archive,
            'committed_at': publication.committed_at.isoformat(),
            'finality_status': publication.finality_status,
            'pipeline_run_id': publication.versions.pipeline_run_id,
            'quality_policy_version': publication.versions.quality_policy_version,
            'parser_version': publication.versions.parser_version,
            'code_version': publication.versions.code_version,
            'config_bundle_hash': publication.versions.config_bundle_hash,
            'metric_version': publication.versions.metric_version,
            'bundle_version': usable[0].bundle_version,
            'pipeline_wall_seconds': (workflow_span[1] - workflow_span[0]).total_seconds()
            if workflow_span else None,
            'mart_hook_seconds': (bundle_span[1] - bundle_span[0]).total_seconds() if bundle_span else None,
            'basis': 'AGC_SHARED_BASIS_V1 v1 — assumed_unverified (giả định chưa xác minh, user cho phép có nhãn)',
        }
        for cycle in catalog.cycles(publication):
            folder = connection.execute(
                'SELECT source_folder FROM meta.source_folder_mapping WHERE snapshot_id=%s AND compartment_id=%s',
                (publication.snapshot_id, cycle.compartment_id)).fetchone()[0]
            raw_irr, raw_drain, raw_days, first, last = read_raw_resources(
                snapshot_dir / folder / 'Resources.csv', (cycle.start, cycle.end))
            context = wiring.build_context(publication, cycle, cycle.start, cycle.end - timedelta(days=1))
            summary = service.get_irrigation_summary(context)
            expected_ratio = independent_percent(raw_drain, raw_irr)
            results['compartments'].append({
                'compartment': cycle.compartment_id, 'cycle': cycle.cycle_id,
                'source_folder': folder, 'raw_days': raw_days,
                'raw_first': first.isoformat(), 'raw_last': last.isoformat(),
                'raw_irrigation': exact(raw_irr), 'raw_drain': exact(raw_drain),
                'service_irrigation': exact(summary.irrigation_observed_l_m2),
                'service_drain': exact(summary.drain_observed_l_m2),
                'service_ratio_pct': exact(summary.drain_fraction_pct),
                'expected_ratio_pct': exact(expected_ratio),
                'paired_coverage_pct': exact(summary.paired_coverage_pct),
                'status': summary.status.value,
                'totals_match': raw_irr == summary.irrigation_observed_l_m2
                and raw_drain == summary.drain_observed_l_m2,
                'ratio_match': expected_ratio == summary.drain_fraction_pct,
            })
        aicu = next(cycle for cycle in catalog.cycles(publication) if cycle.compartment_id == 'AICU')
        raw_irr, raw_drain, raw_days, _, _ = read_raw_resources(
            snapshot_dir / 'AICU' / 'Resources.csv', (WEEK[0], WEEK[1] + timedelta(days=1)))
        week_context = wiring.build_context(publication, aicu, WEEK[0], WEEK[1])
        summary = service.get_irrigation_summary(week_context)
        week_days = service.get_irrigation_days(week_context)
        first_day = week_days[0]
        lineage = wiring.cycle_lineage(service, publication, first_day.revision_id)
        results['windows']['aicu_week'] = {
            'window': f'[{WEEK[0].isoformat()},{(WEEK[1] + timedelta(days=1)).isoformat()})',
            'raw_days': raw_days, 'raw_irrigation': exact(raw_irr), 'raw_drain': exact(raw_drain),
            'service_irrigation': exact(summary.irrigation_observed_l_m2),
            'service_drain': exact(summary.drain_observed_l_m2),
            'service_ratio_pct': exact(summary.drain_fraction_pct),
            'expected_ratio_pct': exact(independent_percent(raw_drain, raw_irr)),
            'status': summary.status.value,
            'totals_match': raw_irr == summary.irrigation_observed_l_m2
            and raw_drain == summary.drain_observed_l_m2,
            'ratio_match': independent_percent(raw_drain, raw_irr) == summary.drain_fraction_pct,
            'lineage_first_day': {
                'resource_date': first_day.resource_date.isoformat(),
                'revision_id': lineage.revision_id, 'revision_number': lineage.revision_number,
                'relative_path': lineage.relative_path,
                'line_number': lineage.line_number, 'line_end': lineage.line_end,
                'source_time_serial': exact(lineage.source_time_serial),
                'raw_hash_verified': lineage.raw_hash_verified,
            },
        }
    results['all_totals_match'] = all(row['totals_match'] for row in results['compartments']) \
        and results['windows']['aicu_week']['totals_match']
    results['all_ratios_match'] = all(row['ratio_match'] for row in results['compartments']) \
        and results['windows']['aicu_week']['ratio_match']
    return results


# ------------------------------------------------------------ performance --
def percentile(sorted_values, fraction):
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return sorted_values[min(index, len(sorted_values) - 1)]


def container_limits():
    limits = {}
    for key, path in (('memory_max', '/sys/fs/cgroup/memory.max'),
                      ('memory_peak', '/sys/fs/cgroup/memory.peak'),
                      ('cpu_max', '/sys/fs/cgroup/cpu.max')):
        try:
            limits[key] = Path(path).read_text().strip()
        except OSError:
            limits[key] = 'unavailable'
    return limits


def timed(call):
    start = time.perf_counter()
    call()
    return time.perf_counter() - start


def run_performance(kwargs, runs=30, warmups=5):
    results = {'mode': 'performance', 'target_p95_seconds': exact(P95_TARGET_SECONDS),
               'runs': runs, 'warmups': warmups, 'workloads': {}, 'environment': {
                   'python': platform.python_version(), 'platform': platform.platform(),
                   'limits': container_limits()}}
    with psycopg.connect(**kwargs) as connection:
        catalog = CatalogService(PostgresCatalogAdapter(connection))
        service = IrrigationService(PostgresQueryAdapter(connection))
        publication = wiring.resolve_publication(service, catalog.usable()[0].publication_id)
        aicu = next(cycle for cycle in catalog.cycles(publication) if cycle.compartment_id == 'AICU')
        results['workload'] = {
            'publication_id': publication.publication_id, 'snapshot_id': publication.snapshot_id,
            'compartment': 'AICU', 'cycle': aicu.cycle_id,
            'week_window': f'[{WEEK[0].isoformat()},{(WEEK[1] + timedelta(days=1)).isoformat()})',
            'cycle_window': f'[{aicu.start.isoformat()},{aicu.end.isoformat()})',
            'connection_state': 'one persistent connection; per call the service re-resolves the '
                                'pinned publication by ID (app action path, no pointer read)',
        }
        week_context = wiring.build_context(publication, aicu, WEEK[0], WEEK[1])
        cycle_context = wiring.build_context(publication, aicu, aicu.start, aicu.end - timedelta(days=1))
        workloads = {
            'summary_week_7d': lambda: service.get_irrigation_summary(week_context),
            'days_week_7d': lambda: service.get_irrigation_days(week_context),
            'weekly_full_cycle_166d': lambda: service.get_irrigation_weekly(cycle_context),
        }
        for name, call in workloads.items():
            first_request = timed(call)
            for _ in range(warmups):
                call()
            measured = sorted(timed(call) for _ in range(runs))
            p95 = Decimal(repr(percentile(measured, 0.95))).quantize(Decimal('0.000001'))
            results['workloads'][name] = {
                'first_request_seconds': round(first_request, 6),
                'first_request_note': 'first timed call of this workload on the shared connection; '
                                      'earlier workloads/warmups may already have warmed caches — '
                                      'not a verified cold-cache run',
                'p50_seconds': round(statistics.median(measured), 6),
                'p95_seconds': float(p95),
                'min_seconds': round(measured[0], 6),
                'max_seconds': round(measured[-1], 6),
                'target_met': p95 <= P95_TARGET_SECONDS,
            }
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m greenhouse.evaluation.br02')
    parser.add_argument('--mode', choices=['fixture', 'agc', 'performance', 'all'], required=True)
    parser.add_argument('--raw-root', default=os.environ.get('GREENHOUSE_RAW_DIR', '/data/raw'))
    parser.add_argument('--runs', type=int, default=30)
    parser.add_argument('--output', type=Path, help='write the JSON report here instead of stdout')
    options = parser.parse_args(argv)
    kwargs = Settings.from_env().connection_kwargs()
    report = {'tool': 'greenhouse.evaluation.br02',
              'generated_at': datetime.now(timezone.utc).isoformat(), 'database': kwargs['dbname']}
    if options.mode in ('fixture', 'all'):
        report['fixture'] = run_fixture_acceptance(kwargs)
    if options.mode in ('agc', 'all'):
        report['agc'] = run_agc_reconciliation(kwargs, options.raw_root)
    if options.mode in ('performance', 'all'):
        report['performance'] = run_performance(kwargs, runs=options.runs)
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if options.output:
        options.output.write_text(payload + '\n', encoding='utf-8')
        print(f'report written: {options.output}')
    else:
        print(payload)
    if any(not case['pass'] for case in report.get('fixture', {}).get('cases', [])):
        return 1
    if report.get('agc') and not (report['agc']['all_totals_match'] and report['agc']['all_ratios_match']):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
