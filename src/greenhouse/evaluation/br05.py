"""BR-05 acceptance evidence runner backing docs/evaluation/br_05.md.

Modes (python -m greenhouse.evaluation.br05 --mode <name>):
  fixture      — AC-01..AC-10 on a disposable guarded test_ database through
                 the real pipeline. Only the Spark stage is replaced by a
                 prepared artifact (parser, staging, loader, publication,
                 harvest+water marts, query adapter, service oracle and
                 exports are real code); the doc states this.
  agc          — independent reconciliation of the locked raw Production.csv
                 values to the published harvest mart/service on the real
                 AGC validation database (all compartments plus the
                 quarantined out-of-cycle Reference row). Read-only.
  performance  — BR-01 convention timings: first timed call kept separate,
                 then 5 warmups + 30 measured runs per query, p95 against the
                 <=2s target, container limits recorded. Read-only.

Existing databases are never dropped or modified; the fixture database is
created as test_br05_<hex> and dropped afterwards (kept only with explicit
GREENHOUSE_KEEP_TEST_DB=1). Output is one JSON document on stdout or
--output; no credential dumps, ids only — quarantined numbers surface only
through the authorized audit path (AC-03), never recomputed into totals.
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
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from greenhouse.app import exports, wiring
from greenhouse.contracts import (HarvestCompleteness, HarvestRuleProfile, HarvestSummary,
                                  QueryContext, from_json)
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.db.harvest_rules import register_harvest_rule
from greenhouse.db.pipeline import run_pipeline
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries.harvest import HarvestService
from greenhouse.queries.catalog import CatalogService
from greenhouse.settings import Settings

EXCEL_EPOCH = date(1899, 12, 30)
P95_TARGET_SECONDS = Decimal('2')


def independent_percent(numerator, denominator):
    """Expected share computed in THIS script, not by the query service."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * 100 / Decimal(denominator)


def exact(value):
    return None if value is None else str(value)


@contextmanager
def fixture_database(admin_kwargs):
    name = 'test_br05_' + uuid4().hex[:12]
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


def publish_fixture(connection, scratch, rows, *, start=date(2020, 2, 1),
                    end=date(2020, 2, 9), rule_pct=None, broken=False):
    """One synthetic Production scope published through the real pipeline.

    rows: (offset_from_start_serial, prodA, prodB) with '' for a missing
    measure. broken=True appends an unterminated fragment after the data rows
    so the parser fails the file and ingestion closes (no publication).
    """
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
    definition = {'nonnegative': ['ProdA', 'ProdB'], 'suspect_zero': [], 'off_mode_zero': []}
    connection.execute(
        "INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,%s,%s,true,'synthetic quality fixture')",
        (policy, start, end, Jsonb(definition), hashlib.sha256(
            json.dumps(definition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
    for code in ('ProdA', 'ProdB'):
        connection.execute(
            "INSERT INTO config.area_basis_version VALUES "
            "(%s,%s,%s,%s,1,100,'synthetic_harvest_area','verified',%s,%s,NULL,NULL,"
            "'explicit synthetic basis')",
            (tag + code, scope, comp, code, start, end))
        connection.execute(
            "INSERT INTO config.basis_application VALUES (%s,%s,%s,'shared_analysis',%s,true)",
            (scope, comp, code, tag + code))
    raw = scratch / tag / 'raw'
    (raw / 'Synthetic').mkdir(parents=True)
    path = raw / 'Synthetic/Production.csv'
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream, lineterminator='\r\n')
        writer.writerow(HEADERS['Production'])
        for offset, prod_a, prod_b in rows:
            values = {'ProdA': prod_a, 'ProdB': prod_b}
            serial = (start - EXCEL_EPOCH).days + offset
            writer.writerow([serial] + [values.get(header.strip(), '0')
                                        for header in HEADERS['Production'][1:]])
        if broken:
            stream.write('"unterminated-fragment\nnothing,here\n')
    source = {'snapshot_id': snapshot, 'archive': {'sha256': tag * 2},
              'files': [{'path': 'Synthetic/Production.csv',
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'csv': {'data_rows': len(rows)}}]}
    (raw / 'manifest.json').write_text(json.dumps(source))
    rule_version = None
    if rule_pct is not None:
        rule_tag = uuid4().hex
        profile = HarvestRuleProfile('harvest-rule-version:' + rule_tag,
                                    'harvest-rule:' + rule_tag, 1, scope, comp,
                                     Decimal(rule_pct),
                                     'synthetic review thresholds; not agronomic guidance', True,
                                     'demo_rule')
        register_harvest_rule(connection, profile)
        rule_version = profile.rule_version
    result = None
    failure = None
    try:
        result = run_pipeline(connection, raw, scratch / tag / 'output', scope, policy,
                              stage_function=artifact_stage)
    except Exception as error:  # fail-closed run: record the type for AC-07
        failure = f'{type(error).__name__}: {str(error)[:160]}'
    return {'tag': tag, 'scope': scope, 'comp': comp, 'cycle': cycle, 'snapshot': snapshot,
            'policy': policy, 'raw': raw, 'rule_version': rule_version,
            'result': result, 'failure': failure}


def service_for(e, connection):
    return HarvestService(PostgresHarvestQueryAdapter(
        connection, raw_snapshots={e['snapshot']: e['raw']}))


def synthetic_failed_file_state():
    """A clearly LABELLED synthetic HarvestCompleteness: one failed file and
    an incomplete snapshot. This is an explicit test seam (never a real AGC
    ingestion failure); the model output is an explicit unknown state, no
    fabricated zero, and no sealed/durable state is touched."""
    return HarvestCompleteness(
        snapshot_complete=False, source_resolved=False, expected_files=1,
        files_present=0, files_loaded=0, files_failed=1,
        records_read=None, records_accepted=None, records_quarantine=None,
        records_duplicate=None, records_conflict=None,
        unresolved_records=0, conflicted_records=0,
        accepted_records=0, paired_records=0, prod_a_usable_records=0,
        prod_b_usable_records=0, event_a_dates=0, event_b_dates=0,
        reasons=('synthetic_failed_file_fixture',), expected_relative_path=None,
        unresolved_items=())


class SyntheticIncompleteAdapter:
    """Typed wrapping adapter: everything real, EXCEPT completeness comes
    from `synthetic_failed_file_state()` so the no-event unknown behavior is
    exercised through the REAL HarvestService (and the same seam backs the
    UI AppTest) without modifying any database state."""
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def harvest_state(self, context):
        return synthetic_failed_file_state()


def context_of(e, publication, start, end, rule_version=None):
    return QueryContext(e['scope'], publication.publication_id, publication.snapshot_id,
                        e['cycle'], (e['comp'],), start, end, rule_version)


def observation(service, context, publication):
    summary = service.get_harvest_summary(context)
    records = service.get_harvest_records(context)
    days = service.get_harvest_days(context)
    weekly = service.get_harvest_weekly(context)
    state = summary.completeness
    present_shares = [value for value in (item.class_b_share_pct for item in records)
                      if value is not None]
    mean_of_record_shares = (sum(present_shares, Decimal(0)) / len(present_shares)
                             if present_shares else None)
    return {
        'a_observed': exact(summary.prod_a_observed),
        'b_observed': exact(summary.prod_b_observed),
        'paired_a': exact(summary.paired_prod_a),
        'paired_b': exact(summary.paired_prod_b),
        'paired_total': exact(summary.paired_total),
        'a_share_pct': exact(summary.class_a_share_pct),
        'b_share_pct': exact(summary.class_b_share_pct),
        'mean_of_record_shares_pct': exact(mean_of_record_shares),
        'accepted_records': summary.accepted_records,
        'paired_records': summary.paired_records,
        'cumulative_a': exact(summary.cumulative_a),
        'cumulative_total': exact(summary.cumulative_total),
        'cumulative_origin': summary.cumulative_origin.isoformat() if summary.cumulative_origin else None,
        'cumulative_basis_label': summary.cumulative_basis_label,
        'status': summary.status.value,
        'rule_status': summary.rule_status.value,
        'rule_version': summary.rule_version,
        'outside_rule_records': summary.outside_rule_records,
        'rule_unknown_records': summary.rule_unknown_records,
        'snapshot_complete': state.snapshot_complete,
        'unresolved': state.unresolved_records,
        'conflicted': state.conflicted_records,
        'state_accepted': state.accepted_records,
        'state_paired': state.paired_records,
        'record_shares': [exact(item.class_b_share_pct) for item in records],
        'record_rule_status': [item.rule_status.value for item in records],
        'day_b_share': [exact(day.class_b_share_pct) for day in days],
        'day_present': [day.record_present for day in days],
        'day_a': [exact(day.prod_a) for day in days],
        'day_reasons': [list(day.reason_codes) for day in days],
        'weekly_b_share': [exact(item.class_b_share_pct) for item in weekly],
        'weekly_cumulative_a': [exact(item.cumulative_a) for item in weekly],
        'weekly_cumulative_total': [exact(item.cumulative_total) for item in weekly],
        'unresolved_reasons': [item.reason_code for item in state.unresolved_items],
        'reason_codes': list(summary.reason_codes),
    }


def run_fixture_acceptance(admin_kwargs):
    """AC-01..AC-10 against literal, independently hand-computed expectations."""
    results = {'mode': 'fixture', 'spark_stage': 'replaced_with_prepared_artifact',
               'real_components': ['parser', 'staging', 'loader', 'publication', 'harvest mart',
                                   'water mart', 'query adapter', 'service oracle',
                                   'rule profile', 'exports'], 'cases': []}

    def record(case_id, title, expected, observed):
        results['cases'].append({
            'ac': case_id, 'title': title, 'expected': expected, 'observed': observed,
            'pass': all(observed.get(key) == value for key, value in expected.items())
            and observed.get('pass', True),
        })

    with fixture_database(admin_kwargs) as target_kwargs, \
            psycopg.connect(**target_kwargs, autocommit=True) as connection:
        scratch = Path('/tmp/br05-fixture-' + uuid4().hex)

        # AC-01/AC-02 weighted ratio of masses over the SAME paired records.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2'), (1, '2', '0')])
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        observed = observation(service, context_of(e, publication, date(2020, 2, 1),
                                                   date(2020, 2, 9)), publication)
        record('AC-01', 'Fixture: A=3, B=0.2, tổng 3.2 kg/m²; share A=93.75%, B=6.25% '
                        '(weighted on the SAME paired records)',
               {'a_observed': '3', 'b_observed': '0.2', 'paired_total': '3.2',
                'a_share_pct': '93.75', 'b_share_pct': '6.25', 'accepted_records': 2,
                'paired_records': 2, 'status': 'complete', 'snapshot_complete': True,
                'cumulative_a': '3', 'cumulative_total': '3.2'}, observed)
        mean = observed['mean_of_record_shares_pct']  # record shares 16.66…% & 0% → mean 8.33…%
        record('AC-02', 'Khác trung bình tỷ lệ từng lần thu: mean of record shares 8.33…% ≠ 6.25%',
               {'pass': True},
               {'pass': mean not in (None, '6.25', '6.25') and
                abs(Decimal(mean) - Decimal('6.25')) > Decimal('1'),
                'mean_of_record_shares_pct': mean, 'b_share_pct': observed['b_share_pct']})

        # AC-03 quarantined out-of-cycle row: totals unchanged, completeness blocked, and
        # the quarantined number only via the authorized audit (raw read-only).
        e = publish_fixture(connection, scratch,
                            [(0, '1', '0.2'), (1, '2', '0'), (700, '0.32', '0.1')])
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        context = context_of(e, publication, date(2020, 2, 1), date(2020, 2, 9))
        item = service.adapter.harvest_state(context).unresolved_items[0]
        audit = service.get_unresolved_source(context, item)
        proda_index = audit.headers.index('ProdA')
        observed = observation(service, context, publication)
        observed.update({'pass': True, 'audit_verified': audit.record_verified,
                         'audit_raw_value': audit.tokens[proda_index],
                         'audit_serial': exact(audit.source_time_serial),
                         'audit_line': audit.line_number,
                         'audit_reason': audit.reason_code})
        record('AC-03', 'Record ngoài vụ (offset 700 → A 0.32 quarantine): tổng accepted không đổi '
                        '(A=3/B=0.2), completeness=false, không xếp hạng; audit raw đọc đúng dòng '
                        'và KHÔNG vào tổng',
               {'pass': True},
               {'pass': observed['a_observed'] == '3' and observed['b_observed'] == '0.2'
                and observed['snapshot_complete'] is False and observed['unresolved'] == 1
                and observed['status'] == 'partial' and observed['audit_verified']
                and observed['audit_raw_value'] == '0.32'
                and observed['audit_reason'] == 'out_of_cycle_unresolved',
                'observed': observed})

        # AC-04 missing B: observed per-measure totals, paired subset, subset warning.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2'), (1, '5', ''), (2, '2', '')])
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        observed = observation(service, context_of(e, publication, date(2020, 2, 1),
                                                   date(2020, 2, 9)), publication)
        record('AC-04', 'Record thiếu B: A observed=8; B observed=0.2; paired 1/0.2; '
                        'share B 16.66…% trên tập con; snapshot incomplete',
               {'a_observed': '8', 'b_observed': '0.2', 'paired_a': '1', 'paired_b': '0.2',
                'accepted_records': 3, 'paired_records': 1, 'snapshot_complete': False,
                'status': 'partial'}, observed)
        # AC-04b: the exact two-record case from the spec — (A 1/B 0.2) and
        # (A 2/B missing). Shares stay weighted on the paired subset.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2'), (1, '2', '')])
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        observed = observation(service, context_of(e, publication, date(2020, 2, 1),
                                                   date(2020, 2, 9)), publication)
        record('AC-04b', 'Case hai record đúng spec: (1, 0.2) + (2, NULL) → A observed=3, '
                        'B observed=0.2, paired 1/0.2, share A 83.33…% trên đúng tập paired '
                        '(không phải trung bình share từng record)',
               {'a_observed': '3', 'b_observed': '0.2', 'paired_a': '1', 'paired_b': '0.2',
                'paired_total': '1.2',
                'a_share_pct': '83.333333333333333333333333333333333333333333333333',
                'accepted_records': 2, 'paired_records': 1, 'snapshot_complete': False,
                'status': 'partial'}, observed)

        # AC-05 zero-mass record vs missing data: totals 0, shares NULL, still complete.
        e = publish_fixture(connection, scratch, [(0, '0', '0')])
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        observed = observation(service, context_of(e, publication, date(2020, 2, 1),
                                                   date(2020, 2, 9)), publication)
        record('AC-05', 'Record A=B=0: tổng 0 (khác dữ liệu thiếu), share NULL (mẫu số 0), '
                        'snapshot đầy đủ → complete',
               {'a_observed': '0', 'b_observed': '0', 'a_share_pct': None, 'b_share_pct': None,
                'accepted_records': 1, 'paired_records': 1, 'status': 'complete',
                'snapshot_complete': True}, observed)

        # AC-06 verified no-event day inside a complete snapshot: 0 with no record;
        # inside an incomplete snapshot: NULL / unknown, never a fabricated zero.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2')])
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        verified = observation(service, context_of(e, publication, date(2020, 2, 5),
                                                   date(2020, 2, 9)), publication)
        e_incomplete = publish_fixture(connection, scratch, [(0, '1', '0.2'), (1, '2', '')])
        service_incomplete = service_for(e_incomplete, connection)
        publication_incomplete = service_incomplete.pin_publication(e_incomplete['scope'])
        incomplete = observation(service_incomplete,
                                 context_of(e_incomplete, publication_incomplete,
                                            date(2020, 2, 5), date(2020, 2, 9)),
                                 publication_incomplete)
        record('AC-06', 'Ngày/cửa sổ không event trong snapshot ĐẦY ĐỦ → known 0 với '
                        'known_zero_event_window; trong snapshot CHƯA HOÀN CHỈNH → '
                        'unknown/NULL, không điền 0 như đã biết',
               {'pass': True},
               {'pass': verified['a_observed'] == '0' and verified['status'] == 'complete'
                and 'known_zero_event_window' in verified['reason_codes']
                and incomplete['a_observed'] is None and incomplete['status'] == 'unknown'
                and 'known_zero_event_window' not in incomplete['reason_codes'],
                'verified_window_a': verified['a_observed'],
                'verified_window_status': verified['status'],
                'verified_window_reasons': verified['reason_codes'],
                'incomplete_window_a': incomplete['a_observed'],
                'incomplete_window_status': incomplete['status'],
                'incomplete_window_reasons': incomplete['reason_codes']})

        # AC-06b day grain: a no-event day BETWEEN two event days is stated
        # explicitly — record_present=false, zero known mass, and the
        # cumulative continues on the later event day (never counted as coverage).
        e_mid = publish_fixture(connection, scratch, [(0, '1', '0.2'), (2, '2', '0')])
        service_mid = service_for(e_mid, connection)
        publication_mid = service_mid.pin_publication(e_mid['scope'])
        mid_rows = service_mid.get_harvest_days(context_of(e_mid, publication_mid,
                                                           date(2020, 2, 1), date(2020, 2, 9)))
        mid = next(row for row in mid_rows if row.harvest_date == date(2020, 2, 2))
        later = next(row for row in mid_rows if row.harvest_date == date(2020, 2, 3))
        record('AC-06b', 'Ngày không event NẰM GIỮA hai ngày có event (2020-02-02 giữa '
                        '2020-02-01 và 2020-02-03): record_present=false, record_count=0, '
                        'day A=B=0 tường minh trong snapshot đầy đủ, lũy kế chạy tiếp đúng ở '
                        'ngày 2020-02-03; ngày này KHÔNG được tính là coverage',
               {'pass': True},
               {'pass': mid.record_present is False and mid.record_count == 0
                and exact(mid.prod_a) == '0' and exact(mid.prod_b) == '0'
                and mid.class_b_share_pct is None
                and 'known_zero_harvest_day' in mid.reason_codes
                and later.record_present
                and exact(later.prod_a) == '2' and exact(later.cumulative_a) == '3'
                and exact(later.cumulative_total) == '3.2'
                and len(mid_rows) == 8,  # day rows over the full cycle window
                'mid_record_present': mid.record_present,
                'mid_record_count': mid.record_count,
                'mid_a': exact(mid.prod_a), 'mid_b': exact(mid.prod_b),
                'mid_b_share': exact(mid.class_b_share_pct),
                'mid_reasons': mid.reason_codes,
                'next_day_a': exact(later.prod_a),
                'next_cumulative_a': exact(later.cumulative_a),
                'next_cumulative_total': exact(later.cumulative_total),
                'day_rows': len(mid_rows)})

        # AC-07 — honest failure handling, three separate checks.
        # (i) file parse failure at ingestion: the file is marked failed, the
        #     pipeline closes at the consumption boundary with the exact
        #     reason recorded, and NO publication is created for that scope.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2')], broken=True)
        broken_count = connection.execute(
            'SELECT count(*) FROM meta.publication WHERE data_scope_id=%s',
            (e['scope'],)).fetchone()[0]
        record('AC-07', 'File fail khi ingest (parse fail): file bị đánh failed → pipeline '
                        'fail-closed tại đường consume với lý do cụ thể ("Ingestion is '
                        'incomplete or parser version differs"); scope này có ĐÚNG 0 '
                        'publication', {'pass': True},
               {'pass': e['result'] is None and e['failure'] is not None
                and 'Ingestion is incomplete or parser version differs' in e['failure']
                and broken_count == 0,
                'failure': e['failure'],
                'publication_count': broken_count})

        # (ii) controlled failing workflow on the SAME data scope: coordinator fault
        # injection (supported API, no raw/database mutation). The known-good
        # publication must retain its pointer, results and count; no new publication;
        # the failed workflow state is recorded explicitly.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2'), (1, '2', '0')])
        service = service_for(e, connection)
        good_publication = service.pin_publication(e['scope'])
        good_summary = service.get_harvest_summary(context_of(e, good_publication,
                                                              date(2020, 2, 1), date(2020, 2, 9)))
        good_count = connection.execute(
            'SELECT count(*) FROM meta.publication WHERE data_scope_id=%s',
            (e['scope'],)).fetchone()[0]
        workflow = uuid4().hex
        failure = None
        try:
            # fault='before-spark': a guaranteed coordinator step that runs
            # BEFORE any same-content reuse could short-circuit the run.
            run_pipeline(connection, e['raw'], scratch / ('fail-' + workflow), e['scope'],
                         e['policy'], workflow=workflow,
                         stage_function=artifact_stage, fault='before-spark')
        except RuntimeError as error:  # controlled injected failure, expected and recorded
            failure = {'type': type(error).__name__, 'injected_stage': 'before-spark',
                       'message': str(error)[:160]}
        pointer_after = service.pin_publication(e['scope'])
        count_after = connection.execute(
            'SELECT count(*) FROM meta.publication WHERE data_scope_id=%s',
            (e['scope'],)).fetchone()[0]
        summary_after = service.get_harvest_summary(context_of(e, pointer_after,
                                                               date(2020, 2, 1), date(2020, 2, 9)))
        failed_rows = connection.execute(
            "SELECT step, status, error FROM meta.workflow_step WHERE workflow_id=%s "
            "AND status='failed' ORDER BY step", (workflow,)).fetchall()
        failed_steps = [{'step': step, 'status': status, 'error': error}
                        for step, status, error in failed_rows]
        record('AC-07a', 'Fail-flow cùng scope có sẵn (coordinator fault before-spark): '
                        'pointer vẫn trỏ ĐÚNG publication tốt cũ, số publication không đổi, '
                        'kết quả summary giữ nguyên, trạng thái failed được ghi rõ; '
                        'hành vi không đổi regardless của việc spark stage lỗi',
               {'pass': True},
               {'pass': failure is not None and failure['type'] == 'RuntimeError'
                and 'Injected failure' in failure['message']
                and pointer_after.publication_id == good_publication.publication_id
                and count_after == good_count == 1
                and any(row['step'] == 'spark' and row['error'].get('type') == 'RuntimeError'
                        and row['error'].get('injected') == 'before-spark'
                        for row in failed_steps)
                and exact(summary_after.prod_a_observed) == exact(good_summary.prod_a_observed)
                and exact(summary_after.cumulative_a) == exact(good_summary.cumulative_a),
                'failure_injection': failure,
                'failed_step_rows': failed_steps,
                'pointer_still': pointer_after.publication_id,
                'publication_count_after': count_after,
                'summary_a_after': exact(summary_after.prod_a_observed),
                'summary_cumulative_a_after': exact(summary_after.cumulative_a)})

        # (iii) labelled synthetic seam for a failed-file completeness: a typed
        # wrapping adapter injects HarvestCompleteness(files_failed=1,
        # snapshot_complete=False) so the no-event UNKNOWN/NULL semantics are
        # exercised through the REAL HarvestService (the same seam backs the UI
        # AppTest). No sealed/durable state of any database is touched.
        service_synth = HarvestService(SyntheticIncompleteAdapter(service.adapter))
        unknown_zoom = service_synth.get_harvest_summary(
            context_of(e, pointer_after, date(2020, 2, 4), date(2020, 2, 5)))
        unknown_days = service_synth.get_harvest_days(
            context_of(e, pointer_after, date(2020, 2, 4), date(2020, 2, 5)))
        unknown_day = unknown_days[0]
        record('AC-07b', 'Completeness cho file failed (seam tổng hợp bằng adapter bọc: '
                        'files_failed=1, snapshot_complete=False, GHI NHÃN synthetic): ngày '
                        'không event qua HarvestService là UNKNOWN/NULL — không điền 0, '
                        'không tính coverage',
               {'pass': True},
               {'pass': unknown_zoom.prod_a_observed is None
                and unknown_zoom.status.value == 'unknown'
                and unknown_day.prod_a is None
                and unknown_day.record_present is False
                and 'unverified_no_harvest_record' in unknown_day.reason_codes
                and 'incomplete_snapshot' in unknown_zoom.reason_codes
                and 'synthetic_failed_file_fixture' in unknown_zoom.reason_codes,
                'zoom_status': unknown_zoom.status.value,
                'zoom_reason_codes': unknown_zoom.reason_codes,
                'day_a': exact(unknown_day.prod_a),
                'day_record_present': unknown_day.record_present,
                'day_reason_codes': unknown_day.reason_codes,
                'synthetic_label': 'injected HarvestCompleteness(files_failed=1, '
                                   'snapshot_complete=False) via typed wrapper; not a real '
                                   'ingestion failure'})

        # AC-08 demo rule 10% per record; not_configured without a selection.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2'), (1, '2', '0')], rule_pct='10')
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        with_rule = observation(service, context_of(e, publication, date(2020, 2, 1),
                                                    date(2020, 2, 9),
                                                    rule_version=e['rule_version']),
                                publication)
        without_rule = observation(service, context_of(e, publication, date(2020, 2, 1),
                                                       date(2020, 2, 9)), publication)
        record('AC-08', 'Rule demo 10%: record 1 share 16.66…% = outside; record 2 0% = within '
                        '(bằng ngưỡng là trong dải); verdict của CỬA SỔ là share của chính nó '
                        '6.25% → WITHIN, bộ đếm record tách riêng (outside=1)',
               {'rule_status': 'within_range', 'outside_rule_records': 1,
                'record_rule_status': ['outside_range', 'within_range'],
                'rule_version': e['rule_version']}, with_rule)
        record('AC-08b', 'Không chọn rule: not_configured, không có kết luận theo ngưỡng nào',
               {'rule_status': 'not_configured', 'rule_version': None,
                'outside_rule_records': None,
                'record_rule_status': ['not_configured', 'not_configured']}, without_rule)

        # AC-09 the EXACT second-record zoom from the spec: the window
        # [2020-02-02, 2020-02-03) contains ONLY the second record.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2'), (1, '2', '0')])
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        second = observation(service, context_of(e, publication, date(2020, 2, 2),
                                                 date(2020, 2, 3)), publication)
        record('AC-09', 'Zoom ĐÚNG lần thu thứ 2 ([2020-02-02, 2020-02-03)): observed A=2/B=0, '
                        'lũy kế từ đầu vụ A=3/total=3.2, origin đầu vụ giữ nguyên, '
                        'không reset âm thầm',
               {'a_observed': '2', 'b_observed': '0', 'accepted_records': 1,
                'paired_records': 1, 'cumulative_a': '3', 'cumulative_total': '3.2',
                'cumulative_origin': date(2020, 2, 1).isoformat()}, second)
        # Kept as an extra case: an empty later day inside a complete snapshot.
        zoom = observation(service, context_of(e, publication, date(2020, 2, 4),
                                               date(2020, 2, 5)), publication)
        record('AC-09b', 'Zoom trống sau cùng record (case thêm, cứ giữ): accepted=0, '
                        'lũy kế A=3/total=3.2 (contributor dừng ở hết ngày trước cửa sổ), '
                        'origin không đổi, complete',
               {'cumulative_a': '3', 'cumulative_total': '3.2',
                'cumulative_origin': date(2020, 2, 1).isoformat(),
                'accepted_records': 0, 'status': 'complete'}, zoom)
        whole = service.get_harvest_cycle(context_of(e, publication, date(2020, 2, 1),
                                                     date(2020, 2, 9)))
        record('AC-09c', 'Toàn-vụ accessor dùng cùng service (canonical BR-04 input): '
                         'lũy kế A=3 bằng tổng observed của BR-05',
               {'pass': True},
               {'pass': exact(whole.prod_a_observed) == '3' and exact(whole.cumulative_a) == '3'
                and int(whole.accepted_records) == 2,
                'whole_cycle_a_observed': exact(whole.prod_a_observed),
                'whole_cycle_cumulative_a': exact(whole.cumulative_a)})

        # AC-10 rerun snapshot: publication identity and counts unchanged; export parity.
        e = publish_fixture(connection, scratch, [(0, '1', '0.2'), (1, '2', '0')])
        service = service_for(e, connection)
        publication = service.pin_publication(e['scope'])
        rerun = run_pipeline(connection, e['raw'], scratch / e['tag'] / 'out', e['scope'],
                             e['policy'], stage_function=artifact_stage)
        publications = connection.execute(
            'SELECT count(*) FROM meta.publication WHERE data_scope_id=%s',
            (e['scope'],)).fetchone()[0]
        receipt = connection.execute(
            'SELECT record_count FROM mart.harvest_publication WHERE publication_id=%s',
            (publication.publication_id,)).fetchone()[0]
        action = wiring.harvest_action(service, publication,
                                       context_of(e, publication, date(2020, 2, 1),
                                                  date(2020, 2, 9)))
        payload = exports.harvest_record_csv(action).decode('utf-8-sig')
        weekly_payload = exports.harvest_week_csv(action).decode('utf-8-sig')
        summary_payload = exports.harvest_summary_json(action).decode('utf-8')
        lines = payload.splitlines()
        rows = list(csv.DictReader(io.StringIO('\n'.join(
            line for line in lines if not line.startswith('# ')))))
        record('AC-10', 'Rerun cùng snapshot → cùng publication, record_count không nhân; '
                        'grain metadata tường minh trong export; CSV record xuất đúng DTO '
                        'đã hiển thị (exact, NULL trống, 0 giữ 0)',
               {'pass': True},
               {'pass': rerun['publication_id'] == publication.publication_id and publications == 1
                and int(receipt) == 2 and rows[0]['prod_a_kg_m2'] == '1'
                and rows[0]['prod_b_kg_m2'] == '0.2' and rows[1]['prod_a_kg_m2'] == '2'
                and rows[1]['prod_b_kg_m2'] == '0'
                and any(f'scope={e["scope"]}' in line for line in lines)
                and any('evaluation_grain=record' in line for line in lines)
                and 'evaluation_grain=week' in weekly_payload
                and '"harvest_export_version": 1' in summary_payload
                and json.loads(summary_payload)['summary']['contract_version'] == 2
                and from_json(HarvestSummary, json.dumps(
                    json.loads(summary_payload)['summary'])).accepted_records == 2,
                'publication_id_stable': rerun['publication_id'] == publication.publication_id,
                'scope_publication_count': publications,
                'mart_record_count': int(receipt),
                'export_rows': len(rows),
                'export_first_a': rows[0]['prod_a_kg_m2'] if rows else None,
                'export_first_b': rows[0]['prod_b_kg_m2'] if rows else None,
                'record_grain_metadata': any('evaluation_grain=record' in line for line in lines),
                'weekly_grain_metadata': 'evaluation_grain=week' in weekly_payload,
                'summary_json_kind': ('window_summary'
                                      if '"export_kind": "window_summary"' in summary_payload
                                      else 'missing'),
                'summary_decode_records': from_json(HarvestSummary, json.dumps(
                    json.loads(summary_payload)['summary'])).accepted_records})
    return results


# -------------------------------------------------------------------- AGC --
def read_raw_production(path, window):
    """Independent raw reader: csv + Decimal + documented Excel epoch. Returns
    in-window totals, the excluded (out-of-window) non-zero rows with serials,
    and per-record in-window rows keyed by physical line for the record-level
    class-B investigation (share computed HERE, independent of the service)."""
    accepted_a = accepted_b = Decimal(0)
    excluded = []
    records = []
    first = last = None
    with open(path, newline='', encoding='utf-8-sig') as stream:
        for physical_line, row in enumerate(stream, start=1):
            if not row.strip():
                continue
            cells = next(csv.reader(io.StringIO(row)))
            if not cells or cells[0].strip().startswith('%'):
                continue
            serial = int(Decimal(cells[0]))
            when = EXCEL_EPOCH + timedelta(days=serial)
            a, b = Decimal(cells[1]), Decimal(cells[2])
            if window[0] <= when < window[1]:
                accepted_a += a
                accepted_b += b
                records.append((physical_line, serial, exact(a), exact(b),
                                exact(independent_percent(b, a + b))))
                first = first or when.isoformat()
                last = when.isoformat()
            elif a != 0 or b != 0:
                excluded.append((serial, when.isoformat(), exact(a), exact(b)))
    top = None
    for line, serial, a_text, b_text, share_text in records:
        if share_text is None:
            continue
        share = Decimal(share_text)
        if top is None or share > top['share']:
            top = {'line': line, 'serial': serial, 'a': a_text, 'b': b_text,
                   'share_pct': share_text, 'share': share}
    if top is not None:
        top.pop('share')
    return {'a': accepted_a, 'b': accepted_b, 'first': first, 'last': last,
            'excluded': excluded, 'records': records, 'max_record_b_share': top}


def run_agc_reconciliation(kwargs, raw_root):
    """Locked raw values -> published harvest mart/service, all compartments."""
    results = {'mode': 'agc', 'read_only': True, 'compartments': []}
    with psycopg.connect(**kwargs) as connection:
        catalog = CatalogService(PostgresCatalogAdapter(connection))
        usable = catalog.harvest_usable()
        if len(usable) != 1:
            raise ValueError('Expected exactly one harvest-usable publication here')
        service_probe = HarvestService(PostgresHarvestQueryAdapter(connection))
        publication = service_probe.adapter.publication(usable[0].publication_id)
        if usable[0].harvest_bundle_version != 'harvest-mart-v3':
            raise ValueError('Expected the harvest-mart-v3 receipt published')
        mapping = wiring.raw_snapshot_mapping(raw_root)
        snapshot_dir = mapping.get(publication.snapshot_id)
        if snapshot_dir is None:
            raise ValueError('Raw snapshot for the pinned publication is not mounted')
        service = HarvestService(PostgresHarvestQueryAdapter(
            connection, raw_snapshots={publication.snapshot_id: snapshot_dir}))
        archive = connection.execute(
            'SELECT archive_checksum FROM meta.dataset_snapshot WHERE snapshot_id=%s',
            (publication.snapshot_id,)).fetchone()[0]
        versions = publication.versions
        results['publication'] = {
            'publication_id': publication.publication_id, 'scope_id': publication.scope_id,
            'snapshot_id': publication.snapshot_id, 'archive_checksum': archive,
            'committed_at': publication.committed_at.isoformat(),
            'finality_status': publication.finality_status,
            'pipeline_run_id': versions.pipeline_run_id,
            'quality_policy_version': versions.quality_policy_version,
            'parser_version': versions.parser_version, 'code_version': versions.code_version,
            'config_bundle_hash': versions.config_bundle_hash,
            'metric_version': 'br05-summary-v1',
            'bundle_version': usable[0].harvest_bundle_version,
            'pinned_bundles': ['irrigation-mart-v1', 'harvest-mart-v3'],
        }
        for cycle in catalog.cycles(publication):
            folder = connection.execute(
                'SELECT source_folder FROM meta.source_folder_mapping '
                'WHERE snapshot_id=%s AND compartment_id=%s',
                (publication.snapshot_id, cycle.compartment_id)).fetchone()[0]
            raw = read_raw_production(snapshot_dir / folder / 'Production.csv',
                                      (cycle.start, cycle.end))
            context = wiring.build_context(publication, cycle, cycle.start,
                                           cycle.end - timedelta(days=1))
            summary = service.get_harvest_summary(context)
            expected_a_share = independent_percent(raw['a'], raw['a'] + raw['b'])
            expected_b_share = independent_percent(raw['b'], raw['a'] + raw['b'])
            results['compartments'].append({
                'compartment': cycle.compartment_id, 'cycle': cycle.cycle_id,
                'source_folder': folder,
                'raw_a': exact(raw['a']), 'raw_b': exact(raw['b']),
                'raw_first': raw['first'], 'raw_last': raw['last'],
                'service_a': exact(summary.prod_a_observed),
                'service_b': exact(summary.prod_b_observed),
                'service_a_share_pct': exact(summary.class_a_share_pct),
                'service_b_share_pct': exact(summary.class_b_share_pct),
                'expected_a_share_pct': exact(expected_a_share),
                'expected_b_share_pct': exact(expected_b_share),
                'cumulative_a': exact(summary.cumulative_a),
                'cumulative_total': exact(summary.cumulative_total),
                'snapshot_complete': summary.completeness.snapshot_complete,
                'unresolved': summary.completeness.unresolved_records,
                'conflicts': summary.completeness.conflicted_records,
                'accepted_records': summary.accepted_records,
                'paired_records': summary.paired_records,
                'status': summary.status.value,
                'totals_match': raw['a'] == summary.prod_a_observed
                and raw['b'] == summary.prod_b_observed,
                'shares_match': expected_a_share == summary.class_a_share_pct
                and expected_b_share == summary.class_b_share_pct,
                'cumulative_matches': summary.cumulative_a == summary.prod_a_observed,
                'excluded_raw_rows': raw['excluded'],
                'max_record_b_share': raw['max_record_b_share'],
                'record_count_raw': len(raw['records']),
            })
        reference = next(row for row in results['compartments']
                         if row['compartment'] == 'Reference')
        excluded = reference['excluded_raw_rows'][0]
        results['reference_special'] = {
            'excluded_serial': excluded[0], 'excluded_date': excluded[1],
            'excluded_raw_values': [excluded[2], excluded[3]],
            'excluded_never_in_totals': reference['totals_match'],
            'snapshot_complete': reference['snapshot_complete'],
            'unresolved': reference['unresolved'], 'status': reference['status'],
        }
        # Independent record-level high-B investigation: the single record with the largest class-B share per
        # compartment, computed from raw here.
        highs = {row['compartment']: row['max_record_b_share']
                 for row in results['compartments']}
        results['record_level_investigation'] = {
            'rule_reference': 'explicit demo 10% threshold (harvest-demo-b10:*), NOT a standard',
            'digilog_high_b_event': highs.get('Digilog'),
            'aicu_max_record_b_share': highs.get('AICU'),
            'digilog_above_demo10': Decimal(highs['Digilog']['share_pct']) > Decimal('10')
            if highs.get('Digilog') else None,
            'aicu_all_records_below_demo10': Decimal(highs['AICU']['share_pct']) <= Decimal('10')
            if highs.get('AICU') else None,
        }
    results['all_totals_match'] = all(row['totals_match'] for row in results['compartments'])
    results['all_shares_match'] = all(row['shares_match'] for row in results['compartments'])
    results['all_cumulative_match'] = all(row['cumulative_matches']
                                          for row in results['compartments'])
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
        service = HarvestService(PostgresHarvestQueryAdapter(connection))
        publication = service.adapter.publication(catalog.harvest_usable()[0].publication_id)
        aicu = next(cycle for cycle in catalog.cycles(publication)
                    if cycle.compartment_id == 'AICU')
        results['workload'] = {
            'publication_id': publication.publication_id, 'snapshot_id': publication.snapshot_id,
            'compartment': 'AICU', 'cycle': aicu.cycle_id,
            'cycle_window': f'[{aicu.start.isoformat()},{aicu.end.isoformat()})',
            'connection_state': 'one persistent connection; per call the service re-resolves '
                                'the pinned publication by ID (app action path, no pointer read)',
        }
        cycle_context = wiring.build_context(publication, aicu, aicu.start,
                                             aicu.end - timedelta(days=1))
        workloads = {
            'summary_full_cycle_166d': lambda: service.get_harvest_summary(cycle_context),
            'records_full_cycle': lambda: service.get_harvest_records(cycle_context),
            'days_full_cycle': lambda: service.get_harvest_days(cycle_context),
            'weekly_full_cycle': lambda: service.get_harvest_weekly(cycle_context),
        }
        for name, call in workloads.items():
            first_request = timed(call)
            warmup_seconds = sorted(round(timed(call), 6) for _ in range(warmups))
            samples = sorted(round(timed(call), 6) for _ in range(runs))
            # Serialization precision FIRST, quantiles second: every reported
            # number is derived from exactly the serialized sample_seconds list.
            p50 = Decimal(repr(statistics.median(samples))).quantize(Decimal('0.000001'))
            p95 = Decimal(repr(percentile(samples, 0.95))).quantize(Decimal('0.000001'))
            results['workloads'][name] = {
                'warmups': warmups, 'runs': runs,
                'first_request_seconds': round(first_request, 6),
                'first_request_note': 'first timed call of this workload on the shared '
                                      'connection; earlier workloads/warmups may already have '
                                      'warmed caches — not a verified cold-cache run',
                'warmup_seconds': warmup_seconds,
                'sample_seconds': samples,
                'quantile_basis_note': 'p50 = median of sample_seconds (average of the middle '
                                       'two values because n=30 is even), quantized to 6 '
                                       'decimals for serialization; p95 = nearest-rank '
                                       'ceil(0.95*n)-1 read from the SAME sample_seconds list; '
                                       'min/max read the list directly',
                'p50_seconds': float(p50),
                'p95_seconds': float(p95), 'min_seconds': samples[0],
                'max_seconds': samples[-1],
                'target_met': p95 <= P95_TARGET_SECONDS,
            }
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m greenhouse.evaluation.br05')
    parser.add_argument('--mode', choices=['fixture', 'agc', 'performance', 'all'], required=True)
    parser.add_argument('--raw-root', default=os.environ.get('GREENHOUSE_RAW_DIR', '/data/raw'))
    parser.add_argument('--runs', type=int, default=30)
    parser.add_argument('--output', type=Path, help='write the JSON report here instead of stdout')
    options = parser.parse_args(argv)
    kwargs = Settings.from_env().connection_kwargs()
    report = {'tool': 'greenhouse.evaluation.br05',
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
    if report.get('agc') and not (report['agc']['all_totals_match'] and
                                  report['agc']['all_shares_match'] and
                                  report['agc']['all_cumulative_match']):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
