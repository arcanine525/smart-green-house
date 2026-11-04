"""BR-05 mart, completeness and query contracts against real PostgreSQL.

Publishes Production fixture rows through the REAL pipeline; only the Spark
stage is replaced by a small prepared artifact (same rule as the water
suites). Expectations are hand-computed literals plus independently read raw
CSV values; core rows give an independent oracle path. No record collapsing,
no invented events, no time-coverage metric for a sparse event grain.
"""
import csv
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
import pytest

from greenhouse.contracts import (
    HARVEST_SOURCE_UNIT, BasisStatus, HarvestCompleteness, HarvestRecord, HarvestSummary,
    QualityStatus, QueryContext, ResultStatus, from_json, to_json,
)
from greenhouse.db.corrections import correct
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.db.pipeline import run_pipeline
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries.harvest import HarvestService, share
from test_query_adapter import contracts_database, fixture_stage

D1 = 43864             # 2020-02-03 (inside default cycle 2020-02-01..09)
D2 = 43865             # 2020-02-04
D3 = 43866             # 2020-02-04
OUT_OF_CYCLE = 43510   # the Reference-like 2019-02-14 serial


def independent_percent(numerator, denominator):
    """Independent expected share computed in this test module."""
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * 100 / Decimal(denominator)


@pytest.fixture(scope='module')
def published_harvest(contracts_database, tmp_path_factory):
    """Publish scope variants into one module database; each call gets its own."""
    root = tmp_path_factory.mktemp('harvest')
    with psycopg.connect(**contracts_database, autocommit=True) as connection:
        def create(rows, *, cycle_start=date(2020, 2, 1), cycle_end=date(2020, 2, 9), two_cycles=False):
            tag = uuid4().hex
            site, comp, cycle, snapshot, scope, policy = (
                name + ':' + tag for name in ('site', 'comp', 'cycle', 'snapshot', 'scope', 'quality'))
            connection.execute("INSERT INTO core.site VALUES (%s,'Fixture','source-naive','fixture')", (site,))
            connection.execute("INSERT INTO core.compartment VALUES (%s,%s,'Fixture','fixture')", (comp, site))
            connection.execute("INSERT INTO core.team VALUES (%s,'Fixture',false,'fixture')", (site,))
            connection.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",
                               (cycle, comp, site, cycle_start, cycle_end))
            connection.execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://fixture','fixture')",
                               (snapshot, tag * 2))
            connection.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')",
                               (scope, snapshot))
            connection.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,'Synthetic',%s,%s,%s,'fixture')",
                               (snapshot, comp, site, cycle))
            comp_b, cycle_b = None, None
            if two_cycles:
                comp_b, cycle_b = comp + ':b', cycle + ':b'
                connection.execute("INSERT INTO core.compartment VALUES (%s,%s,'Fixture B','fixture')",
                                   (comp_b, site))
                connection.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",
                                   (cycle_b, comp_b, site, cycle_start, cycle_end))
                connection.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,'Second',%s,%s,%s,'fixture')",
                                   (snapshot, comp_b, site, cycle_b))
            definition = {'nonnegative': ['ProdA', 'ProdB'], 'suspect_zero': [], 'off_mode_zero': []}
            connection.execute(
                "INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,%s,%s,true,'synthetic quality fixture')",
                (policy, cycle_start, cycle_end, Jsonb(definition),
                 hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
            # Constant synthetic verified basis, separate from AGC seeds.
            for code in ('ProdA', 'ProdB'):
                connection.execute(
                    "INSERT INTO config.area_basis_version VALUES "
                    "(%s,%s,%s,%s,1,100,'synthetic_harvest_area','verified',%s,%s,NULL,NULL,'explicit synthetic basis')",
                    (tag + code, scope, comp, code, cycle_start, cycle_end))
                connection.execute(
                    "INSERT INTO config.basis_application VALUES (%s,%s,%s,'shared_analysis',%s,true)",
                    (scope, comp, code, tag + code))
            raw = root / tag / 'raw'
            (raw / 'Synthetic').mkdir(parents=True)
            path = raw / 'Synthetic/Production.csv'
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream, lineterminator='\r\n')
                writer.writerow(HEADERS['Production'])
                for serial, prod_a, prod_b in rows:
                    values = {'ProdA': prod_a, 'ProdB': prod_b}
                    writer.writerow([serial] + [values.get(header.strip(), '0')
                                                for header in HEADERS['Production'][1:]])
            source = {'snapshot_id': snapshot, 'archive': {'sha256': tag * 2},
                      'files': [{'path': 'Synthetic/Production.csv',
                                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'csv': {'data_rows': len(rows)}}]}
            (raw / 'manifest.json').write_text(json.dumps(source))
            run_pipeline(connection, raw, root / tag / 'output', scope, policy,
                         stage_function=fixture_stage)
            adapter = PostgresHarvestQueryAdapter(connection)
            service = HarvestService(adapter)
            publication = service.pin_publication(scope)
            context = QueryContext(scope, publication.publication_id, snapshot, cycle, (comp,),
                                   cycle_start, cycle_end)
            return dict(connection=connection, scope=scope, comp=comp, cycle=cycle, snapshot=snapshot,
                        raw=raw, adapter=adapter, service=service, publication=publication,
                        context=context, cycle_start=cycle_start, cycle_end=cycle_end,
                        comp_b=comp_b, cycle_b=cycle_b, policy=policy, fixture=root / tag)
        yield create


def independent_rows(scope):
    """Independent raw reader: values straight from the fixture CSV file."""
    with open(scope['raw'] / 'Synthetic/Production.csv', newline='', encoding='utf-8-sig') as stream:
        return [(int(Decimal(row['%time'])), Decimal(row['ProdA']), Decimal(row['ProdB']))
                for row in csv.DictReader(stream)]


def test_ac01_ratio_of_sums_weights_completeness_and_reuse(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0')])
    summary = e['service'].get_harvest_summary(e['context'])
    assert summary.prod_a_observed == 3 and summary.prod_b_observed == Decimal('0.2')
    assert (summary.paired_prod_a, summary.paired_prod_b) == (Decimal('3'), Decimal('0.2'))
    assert summary.paired_total == Decimal('3.2')
    assert summary.class_a_share_pct == Decimal('93.75') and summary.class_b_share_pct == Decimal('6.25')
    # Weighted share, never the mean of per-record percentages (91.6666… is wrong).
    mean_of_records = (independent_percent(Decimal('0.2'), Decimal('1.2'))
                       + independent_percent(Decimal('0'), Decimal('2'))) / 2
    assert mean_of_records != summary.class_a_share_pct
    assert summary.accepted_records == 2 and summary.paired_records == 2
    assert summary.completeness.snapshot_complete and summary.completeness.source_resolved
    assert summary.completeness.accepted_records == 2 and summary.completeness.paired_records == 2
    assert summary.status == ResultStatus.COMPLETE
    assert summary.cumulative_a == Decimal('3') and summary.cumulative_basis_label == 'verified'
    assert from_json(HarvestSummary, to_json(summary)) == summary
    weekly = e['service'].get_harvest_weekly(e['context'])
    assert sum((week.prod_a_observed or 0) for week in weekly) == summary.prod_a_observed
    for week in weekly:
        year, number, _ = week.window_start.isocalendar()
        assert (week.iso_year, week.iso_week) == (year, number)
    second = run_pipeline(e['connection'], e['raw'], e['fixture'] / 'rerun', e['scope'], e['policy'],
                          stage_function=fixture_stage)
    assert second['publication_id'] == e['publication'].publication_id  # no-change rerun reuses
    assert e['service'].get_harvest_summary(e['context']) == summary
    records = e['service'].get_harvest_records(e['context'])
    assert len(records) == 2 and all(record.paired for record in records)
    assert all(record.prod_a.source_unit == HARVEST_SOURCE_UNIT for record in records)
    assert independent_rows(e) == [(D1, 1, Decimal('0.2')), (D2, 2, 0)]


def test_ac04_missing_b_is_partial_and_snapshot_incomplete(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', ''), (D3, '5', '')])
    summary = e['service'].get_harvest_summary(e['context'])
    assert summary.prod_a_observed == Decimal('8') and summary.prod_b_observed == Decimal('0.2')
    assert (summary.paired_prod_a, summary.paired_prod_b) == (Decimal('1'), Decimal('0.2'))
    assert summary.paired_total == Decimal('1.2')
    assert summary.accepted_records == 3 and summary.paired_records == 1
    assert abs(summary.class_a_share_pct - independent_percent(1, Decimal('1.2'))) < Decimal('1e-25')
    assert abs(summary.class_b_share_pct - independent_percent(Decimal('0.2'), Decimal('1.2'))) < Decimal('1e-25')
    assert not summary.completeness.snapshot_complete
    assert summary.completeness.source_resolved  # files resolve; pairing is the gap
    assert 'records_without_paired_measures' in summary.completeness.reasons
    assert summary.status == ResultStatus.PARTIAL
    assert 'incomplete_snapshot' in summary.reason_codes
    unpaired = [record for record in e['service'].get_harvest_records(e['context'])
                if not record.paired]
    assert len(unpaired) == 2 and all(not record.prod_b.accepted for record in unpaired)
    assert all(record.prod_a.accepted for record in unpaired)
    days = e['service'].get_harvest_days(e['context'])
    empty = [day for day in days if not day.record_present]
    assert empty and all(day.prod_a is None and day.prod_b is None for day in empty)
    assert all('unverified_no_harvest_record' in day.reason_codes for day in empty)
    present = e['service'].get_harvest_days(replace(e['context'], start=date(2020, 2, 3), end=date(2020, 2, 4)))[0]
    assert present.record_present and present.prod_a == 1 and present.prod_b == Decimal('0.2')


def test_ac03_out_of_cycle_quarantine_excluded_traceable_completeness_false(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0'), (OUT_OF_CYCLE, '0.32', '0.1')])
    summary = e['service'].get_harvest_summary(e['context'])
    assert summary.prod_a_observed == 3 and summary.prod_b_observed == Decimal('0.2')  # excluded
    assert summary.class_a_share_pct == Decimal('93.75')
    assert not summary.completeness.snapshot_complete
    assert summary.completeness.unresolved_records == 1 and summary.completeness.conflicted_records == 0
    assert 'unresolved_quarantine_records' in summary.completeness.reasons
    assert summary.status == ResultStatus.PARTIAL and 'unranked_incomplete_compartment' in summary.reason_codes
    assert len(e['service'].get_harvest_records(e['context'])) == 2
    raw_values = [(serial, a, b) for serial, a, b in independent_rows(e) if serial == OUT_OF_CYCLE]
    assert raw_values == [(OUT_OF_CYCLE, Decimal('0.32'), Decimal('0.1'))]


def test_conflict_rows_block_completeness_without_totals(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D1, '2', '0')])  # same date, different content
    summary = e['service'].get_harvest_summary(e['context'])
    assert summary.prod_a_observed is None and summary.class_a_share_pct is None
    assert summary.completeness.conflicted_records == 2
    assert 'unresolved_conflict_records' in summary.completeness.reasons
    assert not summary.completeness.snapshot_complete
    assert summary.status == ResultStatus.UNKNOWN and summary.accepted_records == 0
    assert summary.cumulative_a is None  # nothing but unresolved rows in this cycle


def test_ac05_zero_mass_and_known_zero_calendar(published_harvest):
    e = published_harvest([(D1, '0', '0')])
    summary = e['service'].get_harvest_summary(e['context'])
    assert summary.prod_a_observed == 0 and summary.prod_b_observed == 0
    assert summary.class_a_share_pct is None and 'no_paired_mass' in summary.reason_codes
    assert summary.completeness.snapshot_complete and summary.status == ResultStatus.COMPLETE
    days = e['service'].get_harvest_days(e['context'])
    zero_day = [day for day in days if day.harvest_date == date(2020, 2, 3)][0]
    assert zero_day.record_present and zero_day.prod_a == 0 and zero_day.class_a_share_pct is None
    filled = [day for day in days if not day.record_present and day.harvest_date == date(2020, 2, 5)][0]
    assert filled.prod_a == 0 and 'known_zero_harvest_day' in filled.reason_codes
    narrow = e['service'].get_harvest_summary(replace(e['context'], start=date(2020, 2, 6), end=date(2020, 2, 9)))
    assert narrow.prod_a_observed == 0 and narrow.accepted_records == 0
    assert narrow.status == ResultStatus.COMPLETE and 'known_zero_event_window' in narrow.reason_codes
    assert narrow.cumulative_a == 0 and narrow.cumulative_basis_label == 'verified'


def test_same_date_accepted_records_survive_mart_and_queries(published_harvest):
    """Mart/query preservation on an ACCEPTED-core synthetic envelope.

    The locked T06 parser applies a daily conflict policy to Production, so
    distinct same-date rows cannot both be accepted through ingestion today;
    this test builds the accepted-core envelope directly and materializes the
    harvest mart on it, proving the mart keeps record grain without collapsing
    keys (a future dedup version inherits this proof). Values stay exact.
    """
    e = published_harvest([(D1, '1', '0.2')])
    connection, cycle, scope = e['connection'], e['cycle'], e['scope']
    tag = uuid4().hex
    run_id, workflow, new_pub = 'manual-run:' + tag, 'manual-workflow:' + tag, uuid4().hex
    source_a, source_b = 'manual-src-a:' + tag, 'manual-src-b:' + tag
    connection.execute("INSERT INTO meta.pipeline_run VALUES (%s,%s,%s,'core_loaded','agc-parser-v1',%s,'{}'::jsonb)",
                       (run_id, scope, e['snapshot'], e['policy']))
    provenance = connection.execute(
        "SELECT sr.source_file_id,sr.adapter_id,sr.adapter_version,sr.time_policy_version,sr.source_timestamp "
        "FROM meta.source_record sr JOIN meta.source_file f USING(source_file_id) "
        "WHERE sr.snapshot_id=%s AND f.relative_path='Synthetic/Production.csv' LIMIT 1",
        (e['snapshot'],)).fetchone()
    for source_id, line in ((source_a, 5), (source_b, 6)):
        connection.execute(
            "INSERT INTO meta.source_record VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual-token',%s,NULL,%s,%s,%s,"
            "'staging_truncated_us')",
            (source_id, provenance[0], e['snapshot'], run_id, line, line,
             'Synthetic/Production.csv:' + str(line), 'b' * 64, Decimal(D2),
             provenance[4], provenance[1], provenance[2], provenance[3]))
    for source_id, prod_a, prod_b in ((source_a, '1', '0.2'), (source_b, '2', '2.2')):
        revision = 'manual-rev:' + source_id
        connection.execute(
            "INSERT INTO core.record_revision VALUES (%s,%s,%s,%s,%s,%s,'harvest',%s,1,'x',"
            "'2020-02-04 00:00:00'::timestamp,false,NULL)",
            (revision, scope, e['snapshot'], cycle, run_id, source_id, cycle + ':' + source_id))
        connection.execute("INSERT INTO core.harvest VALUES (%s,'harvest','2020-02-04 00:00:00'::timestamp,"
                           "DEFAULT,%s,%s)", (revision, Decimal(prod_a), Decimal(prod_b)))
        for code in ('ProdA', 'ProdB'):
            connection.execute("INSERT INTO core.measure_quality VALUES (%s,'harvest',%s,%s,'accepted',"
                               "'applicable',NULL)", (revision, code, e['policy']))
    connection.execute("INSERT INTO meta.workflow_run VALUES (%s,%s,%s,'{}'::jsonb,"
                       "'{\"code\": \"pipeline-v3-harvest\"}'::jsonb,'completed',%s,NULL,now(),now())",
                       (workflow, scope, e['snapshot'], run_id))
    # A parsed-file receipt for THIS run (the loader normally writes it): both
    # accepted rows land here, matching the hand-built accepted-core envelope.
    fixture_file = connection.execute("SELECT source_file_id FROM meta.source_file WHERE snapshot_id=%s "
                                      "AND relative_path='Synthetic/Production.csv'", (e['snapshot'],)).fetchone()[0]
    connection.execute("INSERT INTO meta.file_ingestion VALUES (%s,%s,'loaded','loaded',true,2,2,0,0,0,"
                       "'{\"synthetic\":true}'::jsonb)", (run_id, fixture_file))
    connection.execute("INSERT INTO meta.publication VALUES (%s,%s,%s,%s,'manual-hash',NULL,now(),'provisional')",
                       (new_pub, scope, run_id, e['policy']))
    connection.execute("INSERT INTO meta.publication_member "
                       "SELECT %s,%s,'harvest',r.logical_record_key,r.revision_id FROM core.record_revision r "
                       "WHERE r.revision_id IN (%s,%s)", (new_pub, scope, 'manual-rev:' + source_a,
                                                          'manual-rev:' + source_b))
    connection.execute("INSERT INTO meta.publication_basis SELECT %s,data_scope_id,compartment_id,measure_code,"
                       "analysis_context,basis_version_id FROM meta.publication_basis WHERE publication_id=%s",
                       (new_pub, e['publication'].publication_id))
    from greenhouse.db.irrigation_mart import materialize_irrigation
    from greenhouse.db.harvest_mart import materialize_harvest, BUNDLE_VERSION
    irrigation = materialize_irrigation(connection, new_pub, workflow)
    harvest = materialize_harvest(connection, new_pub, workflow)
    connection.execute("INSERT INTO meta.publication_seal VALUES (%s,%s,'manual',2,'fixture')", (new_pub, workflow))
    assert harvest['records'] == 2 and int(harvest['cycles']) == int(irrigation['cycles']) > 0
    assert connection.execute('SELECT bundle_version FROM mart.harvest_publication WHERE publication_id=%s',
                              (new_pub,)).fetchone()[0] == BUNDLE_VERSION
    service = HarvestService(PostgresHarvestQueryAdapter(connection))
    context = QueryContext(scope, new_pub, e['snapshot'], cycle, (e['comp'],),
                           date(2020, 2, 1), date(2020, 2, 9))
    records = service.get_harvest_records(context)
    assert all(record.harvest_date == date(2020, 2, 4) for record in records)
    assert {record.logical_record_key for record in records} == {cycle + ':' + source_a, cycle + ':' + source_b}
    day = service.get_harvest_days(context)[3]  # 2020-02-04 inside the calendar fill
    assert day.record_count == 2 and day.record_present and day.paired_records == 2
    assert day.prod_a == Decimal('3') and day.prod_b == Decimal('2.4')
    summary = service.get_harvest_summary(context)
    assert summary.accepted_records == 2 and summary.paired_records == 2
    assert summary.prod_a_observed == Decimal('3') and summary.prod_b_observed == Decimal('2.4')
    assert abs(summary.class_a_share_pct - independent_percent(Decimal('3'), Decimal('5.4'))) < Decimal('1e-25')
    assert summary.completeness.snapshot_complete  # both accepted records fully paired
    assert summary.cumulative_a == Decimal('3')


def test_correction_moves_totals_in_new_publication_only(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0')])
    before = e['service'].get_harvest_summary(e['context'])
    revision = next(r for r in e['service'].get_harvest_records(e['context'])
                    if r.prod_a.value == 1 and r.prod_b.value == Decimal('0.2'))
    source = e['connection'].execute(
        'SELECT source_record_id FROM core.record_revision WHERE revision_id=%s',
        (revision.revision_id,)).fetchone()[0]
    old = {'ProdA': '1', 'ProdB': '0.2'}
    correct(e['connection'], e['scope'], source, 'harvest', old, old | {'ProdA': '20'},
            'fixture actor', 'documented correction fixture')
    again = run_pipeline(e['connection'], e['raw'], e['fixture'] / 'post-correction', e['scope'],
                         e['policy'], stage_function=fixture_stage)
    assert again['publication_id'] != e['publication'].publication_id
    new_publication = e['service'].pin_publication(e['scope'])
    new_summary = e['service'].get_harvest_summary(
        replace(e['context'], publication_id=new_publication.publication_id))
    assert new_summary.prod_a_observed == Decimal('22')
    assert abs(new_summary.class_a_share_pct - independent_percent(Decimal('22'), Decimal('22.2'))) < Decimal('1e-25')
    assert new_summary.completeness.snapshot_complete  # correction resolves fully in the new scope
    assert new_summary.cumulative_a == Decimal('22')
    assert e['service'].get_harvest_summary(e['context']) == before  # old result stays reproducible


def test_core_mart_parity_independently(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0.3')])
    core = e['connection'].execute(
        "SELECT sum(h.prod_a_kg_m2), sum(h.prod_b_kg_m2) FROM core.harvest h "
        "JOIN meta.publication_member pm USING(revision_id) "
        "JOIN core.measure_quality qa ON qa.revision_id=h.revision_id AND qa.measure_code='ProdA' "
        "JOIN core.measure_quality qb ON qb.revision_id=h.revision_id AND qb.measure_code='ProdB' "
        "WHERE pm.publication_id=%s AND qa.status='accepted' AND qb.status='accepted' "
        "AND qa.applicability_status='applicable' AND qb.applicability_status='applicable'",
        (e['publication'].publication_id,)).fetchone()
    summary = e['service'].get_harvest_summary(e['context'])
    assert core == (Decimal('3'), Decimal('0.5'))
    assert summary.paired_prod_a == core[0] and summary.paired_prod_b == core[1]


def test_serialization_and_record_contract(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '')])
    record = e['service'].get_harvest_records(e['context'])[0]
    assert from_json(HarvestRecord, to_json(record)) == record
    unpaired = [r for r in e['service'].get_harvest_records(e['context']) if not r.paired]
    assert len(unpaired) == 1 and unpaired[0].prod_b.quality == QualityStatus.MISSING
    assert unpaired[0].prod_b.value is None and unpaired[0].prod_a.accepted
    assert record.prod_a.unit == 'kg/m²' and record.prod_b.unit == 'kg/m²'
    state = e['service'].adapter.harvest_state(e['context'])
    assert from_json(HarvestCompleteness, to_json(state)) == state
    weekly = e['service'].get_harvest_weekly(e['context'])
    assert from_json(HarvestSummary, to_json(weekly[0])) == weekly[0]


def test_workflow_rollback_keeps_prior_publication_and_immutable_mart(published_harvest):
    e = published_harvest([(D1, '1', '0.2')])
    before = e['service'].get_harvest_summary(e['context'])
    receipts = e['connection'].execute('SELECT count(*) FROM mart.harvest_publication').fetchone()[0]
    # An additional enabled basis version changes the workflow config identity,
    # so the next attempt cannot take the no-change reuse shortcut.
    tag = uuid4().hex
    e['connection'].execute(
        "INSERT INTO config.area_basis_version VALUES "
        "(%s,%s,%s,'ProdA',1,100,'synthetic_harvest_area','verified',%s,%s,NULL,NULL,'another explicit basis')",
        (tag, e['scope'], e['comp'], e['cycle_start'], e['cycle_end']))
    e['connection'].execute(
        "INSERT INTO config.basis_application VALUES (%s,%s,'ProdA','sensitivity',%s,true)",
        (e['scope'], e['comp'], tag))
    with pytest.raises(RuntimeError):
        run_pipeline(e['connection'], e['raw'], e['fixture'] / 'failed', e['scope'], e['policy'],
                     stage_function=fixture_stage, fault='publish-hook')
    assert e['service'].pin_publication(e['scope']).publication_id == e['publication'].publication_id
    assert e['connection'].execute('SELECT count(*) FROM mart.harvest_publication').fetchone()[0] == receipts
    assert e['service'].get_harvest_summary(e['context']) == before
    with pytest.raises(psycopg.errors.CheckViolation):  # RAISE EXCEPTION → 23514
        e['connection'].execute('UPDATE mart.fact_harvest_record SET prod_a_kg_m2=9 '
                                'WHERE publication_id=%s', (e['publication'].publication_id,))


def test_bundle_identity_frozen_in_config_and_adapter_rejects_v1(published_harvest):
    """Decision: bundle identities belong in publication reuse identity."""
    from greenhouse.db.pipeline import config as pipeline_config, CODE_VERSION
    from greenhouse.db.harvest_mart import BUNDLE_VERSION
    from greenhouse.db.irrigation_mart import BUNDLE_VERSION as WATER_BUNDLE

    e = published_harvest([(D1, '1', '0.2')])
    from greenhouse.db.climate_mart import BUNDLE_VERSION as CLIMATE_BUNDLE
    versions = pipeline_config(e['connection'], e['scope'], e['policy'])
    assert versions['code'] == CODE_VERSION
    assert versions['marts'] == [WATER_BUNDLE, BUNDLE_VERSION, CLIMATE_BUNDLE]
    assert BUNDLE_VERSION == 'harvest-mart-v3'
    # Stale v1 receipt is rejected actionably; nothing relabels or recomputes.
    tag = uuid4().hex
    scope = 'stale-receipt:' + tag
    e['connection'].execute("INSERT INTO core.site VALUES (%s,'Stale','source-naive','fixture')", (tag,))
    e['connection'].execute("INSERT INTO core.compartment VALUES (%s,%s,'Stale','fixture')", (tag, tag))
    e['connection'].execute("INSERT INTO core.team VALUES (%s,'Stale',false,'fixture')", (tag,))
    e['connection'].execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",
                            ('stale-cycle', tag, tag, date(2020, 2, 1), date(2020, 2, 9)))
    e['connection'].execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://stale','fixture')",
                            ('stale-snapshot', 'c' * 64))
    e['connection'].execute("INSERT INTO meta.data_scope VALUES (%s,'stale-snapshot','historical',NULL,'fixture')",
                            (scope,))
    e['connection'].execute("INSERT INTO meta.pipeline_run VALUES (%s,%s,'stale-snapshot','core_loaded',"
                            "'fixture-parser',%s,'{}'::jsonb)", ('stale-run', scope, e['policy']))
    e['connection'].execute("INSERT INTO meta.publication VALUES (%s,%s,'stale-run',%s,'hash',NULL,now(),'provisional')",
                            ('stale-pub', scope, e['policy']))
    e['connection'].execute("INSERT INTO meta.workflow_run VALUES (%s,%s,'stale-snapshot','{}'::jsonb,"
                            "'{\"code\": \"pipeline-v3-harvest\"}'::jsonb,'completed','stale-run','stale-pub',"
                            "now(),now())", ('stale-workflow', scope))
    e['connection'].execute("INSERT INTO mart.irrigation_publication VALUES "
                            "('stale-pub','irrigation-mart-v1',0,0,now(),now())")
    e['connection'].execute("INSERT INTO mart.harvest_publication VALUES "
                            "('stale-pub','harvest-mart-v1',0,0,now(),now())")
    e['connection'].execute("INSERT INTO meta.publication_seal VALUES ('stale-pub','stale-workflow','fixture',0,'fixture')")
    e['connection'].execute("INSERT INTO meta.current_publication VALUES (%s,'stale-pub')", (scope,))
    adapter = PostgresHarvestQueryAdapter(e['connection'])
    with pytest.raises(ValueError, match='harvest mart bundle'):
        adapter.publication('stale-pub')


def test_out_of_cycle_quarantine_survives_fresh_ingestion_and_unrelated_correction(
        published_harvest):
    """Out-of-cycle row keeps blocking completeness in fresh runs; a correction
    on an UNRELATED source must neither clear the quarantine nor update it."""
    e = published_harvest([(D1, '1', '0.2'), (OUT_OF_CYCLE, '0.32', '0.1')])
    baseline = e['service'].get_harvest_summary(e['context'])
    assert baseline.completeness.unresolved_records == 1
    assert not baseline.completeness.snapshot_complete
    unrelated = next(r for r in e['service'].get_harvest_records(e['context'])
                     if r.prod_a.value == 1)
    source = e['connection'].execute(
        'SELECT source_record_id FROM core.record_revision WHERE revision_id=%s',
        (unrelated.revision_id,)).fetchone()[0]
    old = {'ProdA': '1', 'ProdB': '0.2'}
    correct(e['connection'], e['scope'], source, 'harvest', old, old | {'ProdA': '20'},
            'fixture actor', 'unrelated correction')
    # Fresh ingestion run reproduces its own quarantine audit row for the same
    # source id; provenance keyed by SOURCE identity dedupes it back to one.
    again = run_pipeline(e['connection'], e['raw'], e['fixture'] / 'fresh-run', e['scope'],
                         e['policy'], stage_function=fixture_stage)
    assert again['publication_id'] != e['publication'].publication_id
    new_publication = e['service'].pin_publication(e['scope'])
    summary = e['service'].get_harvest_summary(
        replace(e['context'], publication_id=new_publication.publication_id))
    assert summary.completeness.unresolved_records == 1  # still exactly one block
    assert not summary.completeness.snapshot_complete
    assert 'unresolved_quarantine_records' in summary.completeness.reasons
    assert summary.prod_a_observed == Decimal('20') and summary.prod_b_observed == Decimal('0.2')
    assert e['service'].get_harvest_summary(e['context']).completeness == baseline.completeness


def test_legacy_publication_without_harvest_bundle_fails_actionably(published_harvest):
    e = published_harvest([(D1, '1', '0.2')])
    tag = uuid4().hex
    scope = 'legacy:' + tag
    e['connection'].execute("INSERT INTO core.site VALUES (%s,'Legacy','source-naive','fixture')", (tag,))
    e['connection'].execute("INSERT INTO core.compartment VALUES (%s,%s,'Legacy','fixture')", (tag, tag))
    e['connection'].execute("INSERT INTO core.team VALUES (%s,'Legacy',false,'fixture')", (tag,))
    e['connection'].execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",
                            ('legacy-cycle', tag, tag, date(2020, 2, 1), date(2020, 2, 9)))
    e['connection'].execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://legacy','fixture')",
                            ('legacy-snapshot', 'a' * 64))
    e['connection'].execute("INSERT INTO meta.data_scope VALUES (%s,'legacy-snapshot','historical',NULL,'fixture')",
                            (scope,))
    e['connection'].execute("INSERT INTO meta.pipeline_run VALUES (%s,%s,'legacy-snapshot','core_loaded',"
                            "'fixture-parser',%s,'{}'::jsonb)", ('legacy-run', scope, e['policy']))
    e['connection'].execute("INSERT INTO meta.publication VALUES (%s,%s,'legacy-run',%s,'hash',NULL,now(),'provisional')",
                            ('legacy-pub', scope, e['policy']))
    e['connection'].execute("INSERT INTO meta.workflow_run VALUES (%s,%s,'legacy-snapshot','{}'::jsonb,"
                            "'{\"code\": \"pipeline-v1-legacy\"}'::jsonb,'completed','legacy-run','legacy-pub',"
                            "now(),now())", ('legacy-workflow', scope))
    e['connection'].execute("INSERT INTO meta.publication_seal VALUES ('legacy-pub','legacy-workflow','fixture',0,'fixture')")
    e['connection'].execute("INSERT INTO meta.current_publication VALUES (%s,'legacy-pub')", (scope,))
    adapter = PostgresHarvestQueryAdapter(e['connection'])
    with pytest.raises(ValueError, match='harvest mart bundle'):
        adapter.publication('legacy-pub')
    with pytest.raises(ValueError, match='harvest mart bundle'):
        adapter.pin_publication(scope)


def test_queries_reject_windows_outside_the_cycle(published_harvest):
    e = published_harvest([(D1, '1', '0.2')])
    outside = replace(e['context'], start=date(2021, 1, 1), end=date(2021, 1, 9))
    service = e['service']
    for call in (service.get_harvest_summary, service.get_harvest_records, service.get_harvest_days):
        with pytest.raises(ValueError, match='overlap'):
            call(outside)


def test_exact_totals_are_context_free_and_roundtrip(published_harvest):
    """Summation and DTO invariants never use the ambient 28-digit context."""
    e = published_harvest([(D1, '1.1234567890123456789012345678901', '0.2')])
    summary = e['service'].get_harvest_summary(e['context'])
    assert summary.paired_prod_a == Decimal('1.1234567890123456789012345678901')
    assert summary.paired_total == Decimal('1.3234567890123456789012345678901')
    assert summary.prod_a_observed == summary.paired_prod_a
    assert summary.cumulative_a == summary.paired_prod_a
    assert from_json(HarvestSummary, to_json(summary)) == summary


def test_cumulative_origin_zoom_and_clipped_requests(published_harvest):
    """Chart cumulatives start per record at the cycle origin; zoom vs a
    request outside the cycle carry distinct clip reasons."""
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0')])
    whole = e['service'].get_harvest_summary(e['context'])
    assert (whole.cumulative_a, whole.cumulative_total) == (Decimal('3'), Decimal('3.2'))
    assert whole.cumulative_origin == date(2020, 2, 1) and whole.cumulative_basis_label == 'verified'
    summary = e['service'].get_harvest_summary(replace(e['context'], start=date(2020, 2, 5),
                                                       end=date(2020, 2, 6)))
    assert summary.accepted_records == 0  # interior zoom, zero new events
    assert (summary.cumulative_a, summary.cumulative_total) == (Decimal('3'), Decimal('3.2'))
    assert summary.cumulative_origin == date(2020, 2, 1)
    assert 'cumulative_from_cycle_start' in summary.reason_codes
    assert 'window_clipped_to_cycle' not in summary.reason_codes
    # A request beyond the cycle edge is actual clipping (single boundary is
    # enough), on top of the from-cycle-start cumulative note.
    calendar = e['service'].get_harvest_summary(replace(e['context'], start=date(2020, 2, 8),
                                                        end=date(2020, 2, 12)))
    assert 'window_clipped_to_cycle' in calendar.reason_codes
    assert 'cumulative_from_cycle_start' in calendar.reason_codes
    assert (calendar.cumulative_a, calendar.cumulative_total) == (Decimal('3'), Decimal('3.2'))
    # The whole-cycle accessor deliberately replaces the request with the
    # full-cycle context and never claims expansion as clipping.
    canonical = e['service'].get_harvest_cycle(replace(e['context'], start=date(2020, 2, 4),
                                                        end=date(2020, 2, 20)))
    assert 'window_clipped_to_cycle' not in canonical.reason_codes
    assert 'cumulative_from_cycle_start' not in canonical.reason_codes
    assert canonical.cumulative_a == Decimal('3')
    weekly = e['service'].get_harvest_weekly(replace(e['context'], start=date(2020, 2, 4),
                                                     end=date(2020, 2, 5)))
    assert weekly[0].cumulative_a == Decimal('3') and weekly[0].cumulative_total == Decimal('3.2')
    assert weekly[0].cumulative_origin == date(2020, 2, 1)


def test_incomplete_empty_week_status_matches_summary(published_harvest):
    """Week rows classify actual events, not the synthetic calendar list."""
    e = published_harvest([(D1, '1', '')])
    window = replace(e['context'], start=date(2020, 2, 7), end=date(2020, 2, 9))
    summary = e['service'].get_harvest_summary(window)
    weekly = e['service'].get_harvest_weekly(window)
    assert summary.status == ResultStatus.UNKNOWN and weekly[0].status == ResultStatus.UNKNOWN
    assert weekly[0].cumulative_a == 1  # accepted A before the window rides along
    assert weekly[0].cumulative_total is None  # no paired contributor yet
    assert weekly[0].cumulative_origin == date(2020, 2, 1)
    assert summary.cumulative_total is None  # missing B: paired set unknown here


def test_verified_noevent_prefix_reports_known_zero_without_future_basis(published_harvest):
    """A complete preharvest window is known zero; cumulative basis is drawn
    from contributing records only, never borrowed from later rows."""
    e = published_harvest([(D1, '1', '0.2')])
    summary = e['service'].get_harvest_summary(replace(e['context'], start=date(2020, 2, 1),
                                                       end=date(2020, 2, 2)))
    assert summary.prod_a_observed == 0 and summary.status == ResultStatus.COMPLETE
    assert summary.cumulative_a == 0 and summary.cumulative_total == 0
    assert summary.cumulative_basis_label is None
    assert 'known_zero_event_window' in summary.reason_codes
    assert summary.class_a_share_pct is None and summary.class_b_share_pct is None
    days = e['service'].get_harvest_days(summary.context)
    first = days[0]
    assert first.cumulative_a == 0 and first.cumulative_origin == date(2020, 2, 1)
    # The contributing zero-mass record keeps its own verified label later on.
    late = e['service'].get_harvest_days(e['context'])[4]  # 2020-02-05, filled day
    assert late.cumulative_a == 1 and late.cumulative_total == Decimal('1.2')


def test_parsed_empty_file_is_a_verified_zero_snapshot(published_harvest):
    e = published_harvest([])
    state = e['service'].adapter.harvest_state(e['context'])
    assert state.snapshot_complete and state.source_resolved
    assert state.files_present == 1 and state.files_loaded == 1
    assert state.accepted_records == 0 and state.paired_records == 0
    assert state.expected_relative_path == 'Synthetic/Production.csv'
    summary = e['service'].get_harvest_summary(e['context'])
    assert summary.prod_a_observed == 0 and summary.status == ResultStatus.COMPLETE
    assert (summary.cumulative_a, summary.cumulative_total) == (0, 0)
    assert summary.cumulative_origin == e['cycle_start']
    days = e['service'].get_harvest_days(e['context'])
    assert all(day.record_present is False and day.prod_a == 0 and
               day.cumulative_a == 0 for day in days)


def test_missing_expected_file_never_fabricates_a_record(published_harvest):
    e = published_harvest([(D1, '1', '0.2')], two_cycles=True)
    state = e['service'].adapter.harvest_state(replace(
        e['context'], cycle_id=e['cycle_b'], compartment_ids=(e['comp_b'],)))
    assert state.expected_files == 1 and state.files_present == 0 and state.files_loaded == 0
    assert state.accepted_records == 0 and state.paired_records == 0
    assert 'missing_expected_source_file' in state.reasons
    assert 'records_without_paired_measures' not in state.reasons
    summary = e['service'].get_harvest_summary(replace(
        e['context'], cycle_id=e['cycle_b'], compartment_ids=(e['comp_b'],)))
    assert summary.accepted_records == 0 and summary.status == ResultStatus.UNKNOWN
    assert summary.prod_a_observed is None and summary.class_a_share_pct is None
    assert summary.cumulative_a is None and summary.cumulative_total is None
    assert summary.cumulative_origin == e['cycle_start']
    zoomed = e['service'].get_harvest_summary(replace(
        summary.context, start=date(2020, 2, 3), end=date(2020, 2, 5)))
    assert zoomed.accepted_records == 0
    assert 'cumulative_from_cycle_start' in zoomed.reason_codes
    assert 'window_clipped_to_cycle' not in zoomed.reason_codes
    receipt = e['connection'].execute('SELECT record_count,cycle_count FROM mart.harvest_publication '
                                      'WHERE publication_id=%s',
                                      (e['publication'].publication_id,)).fetchone()
    assert receipt == (1, 2)  # actually materialized records only


def test_invalid_numeric_quarantine_resolves_in_new_publication(published_harvest):
    """Correction fixes the quarantined measure in the NEW publication only;
    provenance stays typed and old publications stay reproducible."""
    e = published_harvest([(D1, '1', 'bad-numeric')])
    before = e['service'].get_harvest_summary(e['context'])
    assert before.completeness.snapshot_complete is False
    assert before.completeness.unresolved_records == 1
    assert 'unresolved_quarantine_records' in before.completeness.reasons
    assert before.prod_a_observed == 1 and before.prod_b_observed is None
    assert before.class_a_share_pct is None
    src = e['connection'].execute(
        'SELECT source_record_id FROM core.record_revision WHERE revision_id=%s',
        (e['service'].get_harvest_records(e['context'])[0].revision_id,)).fetchone()[0]
    state_before = e['service'].adapter.harvest_state(e['context'])
    assert len(state_before.unresolved_items) == 1
    item = state_before.unresolved_items[0]
    assert item.source_record_id == src and item.disposition == 'quarantine'
    assert item.reason_code == 'invalid_numeric' and item.issue_column == 'ProdB'
    assert item.relative_path == 'Synthetic/Production.csv' and item.line_number == 2
    assert item.source_time_serial == Decimal(D1)
    assert from_json(HarvestCompleteness, to_json(state_before)) == state_before
    correction = correct(e['connection'], e['scope'], src, 'harvest',
                         {'ProdA': '1', 'ProdB': None}, {'ProdA': '1', 'ProdB': '0.2'},
                         'fixture actor', 'confirmed numeric correction')
    resolved = run_pipeline(e['connection'], e['raw'], e['fixture'] / 'resolves', e['scope'],
                            e['policy'], stage_function=fixture_stage)
    assert resolved['publication_id'] != e['publication'].publication_id
    context = replace(e['context'], publication_id=resolved['publication_id'])
    after = e['service'].get_harvest_summary(context)
    assert after.prod_a_observed == 1 and after.prod_b_observed == Decimal('0.2')
    assert after.completeness.snapshot_complete and after.completeness.source_resolved
    assert after.completeness.unresolved_records == 0
    fixed = e['service'].get_harvest_records(context)[0]
    assert fixed.correction_id == correction['correction_id']
    assert fixed.correction_version == 1
    assert e['service'].get_harvest_summary(e['context']) == before  # frozen result
    assert e['service'].get_harvest_records(e['context'])[0].correction_id is None


def test_partial_correction_keeps_the_other_invalid_measure_unresolved(published_harvest):
    e = published_harvest([(D1, 'bad-a', 'bad-b')])
    before = e['service'].get_harvest_summary(e['context'])
    assert before.completeness.snapshot_complete is False
    src = e['connection'].execute(
        'SELECT source_record_id FROM core.record_revision WHERE revision_id=%s',
        (e['service'].get_harvest_records(e['context'])[0].revision_id,)).fetchone()[0]
    correction = correct(e['connection'], e['scope'], src, 'harvest',
                         {'ProdA': None, 'ProdB': None}, {'ProdA': '1', 'ProdB': None},
                         'fixture actor', 'only ProdA resolved')
    new = run_pipeline(e['connection'], e['raw'], e['fixture'] / 'partial', e['scope'],
                       e['policy'], stage_function=fixture_stage)
    context = replace(e['context'], publication_id=new['publication_id'])
    after = e['service'].get_harvest_summary(context)
    assert after.prod_a_observed == 1 and after.prod_b_observed is None
    assert after.completeness.snapshot_complete is False
    assert after.completeness.unresolved_records == 1
    resolved = e['service'].get_harvest_records(context)[0]
    assert resolved.correction_id == correction['correction_id']
    assert resolved.correction_version == 1
    assert e['service'].get_harvest_summary(e['context']) == before


def test_missing_b_correction_flips_snapshot_incomplete_to_complete(published_harvest):
    e = published_harvest([(D1, '1', '')])
    before = e['service'].get_harvest_summary(e['context'])
    assert not before.completeness.snapshot_complete and before.completeness.unresolved_records == 0
    assert before.cumulative_a == 1 and before.cumulative_total is None
    src = e['connection'].execute(
        'SELECT source_record_id FROM core.record_revision WHERE revision_id=%s',
        (e['service'].get_harvest_records(e['context'])[0].revision_id,)).fetchone()[0]
    correct(e['connection'], e['scope'], src, 'harvest',
            {'ProdA': '1', 'ProdB': None}, {'ProdA': '1', 'ProdB': '0.2'},
            'fixture actor', 'fill documented missing value')
    new = run_pipeline(e['connection'], e['raw'], e['fixture'] / 'fill-b', e['scope'],
                       e['policy'], stage_function=fixture_stage)
    context = replace(e['context'], publication_id=new['publication_id'])
    after = e['service'].get_harvest_summary(context)
    assert after.completeness.snapshot_complete and after.completeness.paired_records == 1
    assert after.completeness.accepted_records == 1
    assert abs(after.class_a_share_pct - independent_percent(1, Decimal('1.2'))) < Decimal('1e-25')
    assert after.cumulative_a == 1 and after.cumulative_total == Decimal('1.2')


def test_unresolved_issues_keep_distinct_reasons_and_stay_outside_membership(
        published_harvest):
    """Out-of-cycle + invalid columns of ONE source audit as distinct
    unresolved issues; none of them fabricates mart data."""
    e = published_harvest([(OUT_OF_CYCLE, 'bad-a', 'bad-b')])
    state = e['service'].adapter.harvest_state(e['context'])
    assert state.snapshot_complete is False and state.unresolved_records == 1
    assert state.conflicted_records == 0
    assert {(item.reason_code, item.issue_column) for item in state.unresolved_items} == {
        ('invalid_numeric', 'ProdA'), ('invalid_numeric', 'ProdB'),
        ('out_of_cycle_unresolved', None)}
    assert e['service'].get_harvest_records(e['context']) == ()  # no accepted grain
    summary = e['service'].get_harvest_summary(e['context'])
    assert summary.prod_a_observed is None and summary.accepted_records == 0
    assert summary.cumulative_a is None and summary.cumulative_total is None


def test_record_grain_keeps_chronological_order_and_correction_identity(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0')])
    records = e['service'].get_harvest_records(e['context'])
    assert [record.harvest_date for record in records] == [date(2020, 2, 3), date(2020, 2, 4)]
    assert from_json(HarvestRecord, to_json(records[1])) == records[1]
    assert all(record.correction_id is None for record in records)
