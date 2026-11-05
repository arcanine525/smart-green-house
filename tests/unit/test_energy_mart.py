"""Literal quantity/basis oracles through the real synthetic pipeline and DB.

Only the Spark stage uses the existing prepared artifact harness. No AGC
publication, current pointer or main-owned oracle is changed by these tests.
"""
import csv
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
import hashlib
from importlib.resources import files
import json
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
import pytest

from greenhouse.contracts import (
    Applicability, BasisStatus, EffectiveAreaBasis, EnergyComponent, EnergyQuantity,
    EnergySummary, QualityStatus, QueryContext, from_json, to_json,
)
from greenhouse.db.corrections import correct
from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.db.pipeline import config, run_pipeline
from greenhouse.db.migrate import migrate
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries.energy import EnergyService, energy_basis_gate, summarize_energy
from greenhouse.queries.harvest import HarvestService
from test_query_adapter import contracts_database, fixture_stage
from ui.energy_fixture import energy_scope

D0 = date(2020, 1, 1)
EPOCH = date(1899, 12, 30)
CODES = ('Heat_cons', 'ElecHigh', 'ElecLow')


def rerun(e, **options):
    return run_pipeline(e.conn, e.raw, e.output, e.scope, e.policy, stage_function=fixture_stage, **options)


def test_literal_units_distinct_energy_and_production_area_before_first_harvest(energy_scope):
    e = energy_scope()
    quantities = e.service.get_quantities(e.context)
    summary = e.service.get_summary(e.context)
    assert len(quantities) == 6 and len({q.quantity_id for q in quantities}) == 6
    assert [q.absolute_kwh for q in quantities] == list(map(Decimal, ('100', '100', '0', '200', '0', '100')))
    assert [c.observed_absolute_kwh for c in summary.components] == list(map(Decimal, ('300', '100', '100')))
    assert summary.official_total_kwh == 500 and summary.official_total_kwh_m2 == 5
    assert summary.complete_days == 2 and summary.complete_coverage_pct == 100
    harvest = HarvestService(PostgresHarvestQueryAdapter(e.conn)).get_harvest_records(e.context)
    assert len(harvest) == 1 and harvest[0].harvest_date == D0+timedelta(days=1)
    assert harvest[0].prod_a.value == 2 and harvest[0].prod_a.area_m2 == 50
    production = EffectiveAreaBasis('verified-production', 1, e.scope, e.comp, 'ProdA', Decimal('50'),
        'synthetic_production_area', BasisStatus.VERIFIED, D0, e.context.end, 'separate production evidence')
    gate = energy_basis_gate([q.basis for q in quantities] + [production])
    assert gate.absolute_compatible and not gate.per_m2_compatible
    # T18 provides amounts; T19 owns the intensity join/service/UI. The literal
    # fixture numerator/denominator would be500/100=5, not per-m²5/2=2.5.
    assert sum(q.absolute_kwh for q in quantities[:3]) == 200
    assert e.conn.execute('SELECT component_rows,cycle_count FROM mart.energy_publication WHERE publication_id=%s',
                          (e.publication.publication_id,)).fetchone() == (6, 1)
    assert from_json(EnergySummary, to_json(summary)) == summary
    assert from_json(EnergyQuantity, to_json(quantities[0])) == quantities[0]


def test_daily_effective_area_no_per_m2_pooling_and_no_harvest_fanout(energy_scope):
    e = energy_scope(areas=('100', '200'))
    summary = e.service.get_summary(e.context)
    assert summary.official_total_kwh == 800  # day1=200, day2=600
    assert summary.official_total_kwh_m2 is None
    assert summary.basis_gate.absolute_compatible and not summary.basis_gate.per_m2_compatible
    assert summary.components[0].observed_source_per_m2 is None
    assert [g.source_quantity_per_m2 for g in summary.components[0].basis_totals] == [Decimal('3.6'), Decimal('7.2')]
    assert [g.absolute_kwh for g in summary.components[0].basis_totals] == [100, 400]
    assert len(HarvestService(PostgresHarvestQueryAdapter(e.conn)).get_harvest_records(e.context)) == 1
    # The legacy diagnostic SELECT must also resolve one applicable pin, not
    # fan out a NULL row for every other interval on this component.
    assert e.conn.execute("SELECT count(*) FROM core.published_measure WHERE publication_id=%s AND measure_code='Heat_cons'",
                          (e.publication.publication_id,)).fetchone()[0] == 2


def test_component_missing_invalid_suspect_zero_and_complete_intersection(energy_scope):
    e = energy_scope((('3.6', '', '0'), ('7.2', '-1', '1'), ('0', '0', '0')))
    rows = e.service.get_quantities(e.context)
    summary = e.service.get_summary(e.context)
    assert rows[1].quality == QualityStatus.MISSING and rows[1].source_value is None
    assert rows[4].quality == QualityStatus.INVALID and rows[4].source_value == -1
    assert rows[4].normalized_kwh_m2 is None and rows[4].absolute_kwh is None
    assert rows[-1].accepted and rows[-1].absolute_kwh == 0
    assert [c.accepted_days for c in summary.components] == [3, 1, 3]
    assert [c.observed_absolute_kwh for c in summary.components] == [300, 0, 100]
    assert summary.observed_total_kwh == 400 and summary.complete_day_total_kwh == 0
    assert summary.complete_days == 1 and summary.incomplete_dates == (D0, D0+timedelta(days=1))
    assert summary.official_total_kwh is None and summary.official_total_kwh_m2 is None
    suspect = energy_scope((('0', '1', '0'),), suspect_zero=('Heat_cons',))
    qs = suspect.service.get_quantities(suspect.context)
    assert qs[0].quality == QualityStatus.SUSPECT and qs[0].source_value == 0
    assert qs[0].normalized_kwh_m2 is None and qs[1].accepted
    assert suspect.service.get_summary(suspect.context).complete_days == 0


def test_absent_source_day_and_all_zero_remain_distinct(energy_scope):
    e = energy_scope((('0', '0', '0'), None))
    rows = e.service.get_quantities(e.context)
    assert len(rows) == 6 and all(q.revision_id is None for q in rows[3:])
    s = e.service.get_summary(e.context)
    assert s.complete_days == 1 and s.official_total_kwh is None and s.observed_total_kwh == 0
    full_zero = energy_scope((('0', '0', '0'),))
    z = full_zero.service.get_summary(full_zero.context)
    assert z.official_total_kwh == z.official_total_kwh_m2 == 0 and z.complete_coverage_pct == 100


def test_applicability_unknown_and_one_component_basis_gap_are_separate(energy_scope):
    e = energy_scope(missing_basis=('ElecLow',))
    summary = e.service.get_summary(e.context)
    assert summary.complete_days == 2 and summary.complete_coverage_pct == 100
    assert [c.observed_absolute_kwh for c in summary.components] == [300, 100, None]
    assert summary.official_total_kwh is None and summary.basis_gate.status == BasisStatus.UNKNOWN
    rows = e.service.get_quantities(e.context)
    unknown = replace(rows[0], applicability=Applicability.UNKNOWN, reason_code='mode_unknown',
        normalized_kwh_m2=None, absolute_source_quantity=None, absolute_kwh=None)
    assert unknown.source_value == Decimal('3.6') and not unknown.accepted
    result = summarize_energy(e.context, e.publication, e.adapter.cycle_window(e.context), (unknown,)+rows[1:])
    assert result.complete_days == 1 and result.components[0].accepted_days == 1
    assert result.components[1].accepted_days == 2 and 'mode_unknown' in result.reason_codes


@pytest.mark.parametrize('status,enabled', [('unknown', True), ('assumed_unverified', False)])
def test_unknown_or_disabled_assumption_preserves_source_and_null_absolute(energy_scope, status, enabled):
    e = energy_scope(basis_status=status, assumption_enabled=enabled)
    s = e.service.get_summary(e.context)
    assert s.complete_days == 2 and s.complete_coverage_pct == 100
    assert s.components[0].observed_source_per_m2 == Decimal('10.8')
    assert s.components[0].observed_kwh_m2 == 3
    assert s.official_total_kwh is None and s.official_total_kwh_m2 is None
    assert s.basis_gate.status == BasisStatus.UNKNOWN
    assert all(q.basis is None for q in e.service.get_quantities(e.context))


def test_assumed_user_basis_mixed_verified_production_and_sensitivity(energy_scope):
    e = energy_scope(basis_status='assumed_unverified', production_area='62.5', production_kind='production_area')
    s = e.service.get_summary(e.context)
    assert s.official_total_kwh == Decimal('312.5') and s.official_total_kwh_m2 == 5
    assert s.basis_gate.status == BasisStatus.ASSUMED and s.basis_gate.label == 'giả định chưa xác minh'
    rows = e.service.get_quantities(e.context)
    basis = rows[0].basis
    assert (basis.assumption_id, basis.assumption_version) == ('AGC_SHARED_BASIS_V1', 1)
    assert basis.assumption_statement and basis.assumption_evidence and basis.evidence_reference
    production = EffectiveAreaBasis('production-verified', 1, e.scope, e.comp, 'ProdA', Decimal('62.5'),
        'production_area', BasisStatus.VERIFIED, D0, e.context.end, 'independently verified Production fixture')
    gate = energy_basis_gate([q.basis for q in rows] + [production])
    assert gate.absolute_compatible and gate.per_m2_compatible and gate.status == BasisStatus.ASSUMED
    wrong_definition = replace(production, basis_kind='different_definition')
    assert not energy_basis_gate([basis, wrong_definition]).absolute_compatible
    other_version = replace(basis, assumption_version=2)
    assert not energy_basis_gate([basis, other_version]).absolute_compatible
    with localcontext() as ctx:
        ctx.prec = 50
        sensitivity = Decimal('96')/Decimal('62.5')
        assert sensitivity == Decimal('1.536')
        assert s.official_total_kwh*sensitivity == 480  # absolute resource sensitivity
    # Source per-m² quantities remain5; no metadata/source rewrite for sensitivity.
    assert sum(q.normalized_kwh_m2 for q in rows) == 5
    alternative = replace(basis, basis_version_id='explicit-floor-sensitivity', area_m2=Decimal('96'),
        basis_kind='floor_area_sensitivity', assumption_id='AGC_FLOOR_96_SENSITIVITY_V1',
        assumption_statement='Alternative 96m² Resources /62.5m² Production; not verified',
        assumption_evidence='data-decisions §7 sensitivity')
    alternative_gate = energy_basis_gate([alternative, production])
    assert alternative_gate.absolute_compatible and not alternative_gate.per_m2_compatible
    assert alternative_gate.status == BasisStatus.ASSUMED


def test_effective_selection_overlap_rejected_and_day_gap_does_not_fallback(energy_scope):
    e = energy_scope()
    original = e.basis_ids[0]
    with pytest.raises(psycopg.errors.CheckViolation, match='overlapping'), e.conn.transaction():
        duplicate = original+':overlap'
        e.conn.execute('INSERT INTO config.area_basis_version SELECT %s,data_scope_id,compartment_id,measure_code,99,area_m2,basis_kind,verification_status,valid_from,valid_to_exclusive,assumption_id,assumption_version,evidence_reference FROM config.area_basis_version WHERE basis_version_id=%s', (duplicate, original))
        e.conn.execute("INSERT INTO config.basis_application VALUES (%s,%s,'Heat_cons','shared_analysis',%s,true)", (e.scope, e.comp, duplicate))
    before = e.service.get_summary(e.context)
    e.conn.execute('UPDATE config.basis_application SET enabled=false WHERE basis_version_id=%s', (original,))
    result = rerun(e)
    context = replace(e.context, publication_id=result['publication_id'])
    after = e.service.get_summary(context)
    assert before.official_total_kwh == 500 and after.official_total_kwh is None
    assert after.components[0].absolute_days == 1 and after.components[0].observed_absolute_kwh == 200
    assert e.service.get_summary(e.context) == before  # no latest metadata fallback


def test_fifty_digit_normalization_original_mj_and_shared_quantity_identity(energy_scope):
    e = energy_scope((('100', '10', '5'),), areas=('1',))
    q = e.service.get_quantities(e.context)[0]
    assert q.source_value == q.absolute_source_quantity == 100 and q.source_unit == 'MJ/m²/day'
    assert q.normalized_kwh_m2 == Decimal('27.777777777777777777777777777777777777777777777778')
    assert q.absolute_kwh == q.normalized_kwh_m2
    # Both later BRs consume this SAME identity, including exact original MJ.
    assert EnergyService(PostgresEnergyQueryAdapter(e.conn)).get_quantities(e.context)[0] == q
    with pytest.raises(ValueError, match='normalization'):
        replace(q, normalized_kwh_m2=Decimal('27.78'))
    with pytest.raises(ValueError, match='source unit'):
        replace(q, source_unit='kWh/m²/day')


def test_correction_history_lineage_seal_rollback_resume_and_idempotence(energy_scope):
    e = energy_scope()
    old_rows = e.service.get_quantities(e.context)
    old_summary = e.service.get_summary(e.context)
    q = old_rows[0]
    src = e.conn.execute('SELECT source_record_id FROM core.record_revision WHERE revision_id=%s', (q.revision_id,)).fetchone()[0]
    old = dict(e.conn.execute('SELECT measure_code,value::text FROM core.measure_value WHERE revision_id=%s', (q.revision_id,)))
    corrected = correct(e.conn, e.scope, src, 'resource', old, old | {'Heat_cons': '7.2'},
                        'fixture engineer', 'literal source correction evidence')
    workflow = uuid4().hex
    with pytest.raises(RuntimeError, match='Injected failure'):
        rerun(e, workflow=workflow, fault='publish-hook')
    assert e.conn.execute('SELECT publication_id FROM meta.current_publication WHERE data_scope_id=%s', (e.scope,)).fetchone()[0] == e.publication.publication_id
    assert e.conn.execute('SELECT count(*) FROM mart.energy_publication ep JOIN meta.publication p USING(publication_id) WHERE p.data_scope_id=%s', (e.scope,)).fetchone()[0] == 1
    result = rerun(e, workflow=workflow, resume=True)
    context = replace(e.context, publication_id=result['publication_id'])
    new_rows = e.service.get_quantities(context)
    assert new_rows[0].revision_id == corrected['revision_id'] and new_rows[0].correction.version == 1
    assert e.service.get_summary(context).official_total_kwh == 600
    assert e.service.get_summary(e.context) == old_summary and e.service.get_quantities(e.context) == old_rows
    assert rerun(e)['publication_id'] == result['publication_id']
    trace = e.service.get_lineage(new_rows[0].publication, revision_id=new_rows[0].revision_id)
    assert trace.raw_hash_verified and trace.line_number == 2 and trace.relative_path == 'Synthetic/Resources.csv'
    for table in ('fact_energy_day', 'dim_energy_cycle', 'energy_publication'):
        with pytest.raises(psycopg.errors.CheckViolation, match='sealed'), e.conn.transaction():
            e.conn.execute('DELETE FROM mart.'+table+' WHERE publication_id=%s', (result['publication_id'],))
    identity = config(e.conn, e.scope, e.policy)
    assert identity['marts'][-1] == 'energy-mart-v1'
    assert identity['energy_quantity'] == 'energy-quantity-v1'


def test_pin_context_grain_and_no_harvest_completeness_dependency(energy_scope):
    e = energy_scope(harvest_rows=((-365, '2', '0'),))
    harvest = HarvestService(PostgresHarvestQueryAdapter(e.conn)).get_harvest_summary(e.context)
    assert not harvest.completeness.snapshot_complete
    assert e.service.get_summary(e.context).official_total_kwh == 500
    with pytest.raises(ValueError, match='identity mismatch'):
        e.service.get_summary(replace(e.context, snapshot_id='foreign-snapshot'))
    with pytest.raises(ValueError, match='absent'):
        e.service.get_quantities(replace(e.context, compartment_ids=('foreign-compartment',)))
    with pytest.raises(ValueError, match='threshold rule'):
        e.service.get_quantities(replace(e.context, rule_version='drain-profile'))
    clipped = e.service.get_summary(replace(e.context, start=D0-timedelta(days=1)))
    assert clipped.expected_days == 2 and 'window_clipped_to_cycle' in clipped.reason_codes
    assert e.service.get_cycle(replace(e.context, start=D0+timedelta(days=1))).official_total_kwh == 500


@pytest.mark.parametrize('bundle', ['irrigation', 'harvest', 'climate', 'drain', 'energy'])
def test_v6_seal_requires_all_five_bundles_and_rolls_back(energy_scope, bundle):
    e = energy_scope()
    first = e.service.get_quantities(e.context)[0]
    source = e.conn.execute('SELECT source_record_id FROM core.record_revision WHERE revision_id=%s',
                            (first.revision_id,)).fetchone()[0]
    old = dict(e.conn.execute('SELECT measure_code,value::text FROM core.measure_value WHERE revision_id=%s',
                              (first.revision_id,)))
    correct(e.conn, e.scope, source, 'resource', old, old | {'Heat_cons': '7.2'}, 'fixture', 'seal rollback evidence')
    def remove_receipt(conn, publication, workflow):
        # An actual incomplete unsealed bundle, not a mocked trigger/function.
        conn.execute('DELETE FROM mart.'+bundle+'_publication WHERE publication_id=%s', (publication,))
    workflow = uuid4().hex
    with pytest.raises(psycopg.errors.CheckViolation, match='all five'):
        rerun(e, workflow=workflow, hook=remove_receipt)
    assert e.conn.execute('SELECT publication_id FROM meta.current_publication WHERE data_scope_id=%s',
                          (e.scope,)).fetchone()[0] == e.publication.publication_id
    assert e.conn.execute('SELECT count(*) FROM meta.publication WHERE data_scope_id=%s', (e.scope,)).fetchone()[0] == 1
    assert rerun(e, workflow=workflow, resume=True)['publication_id'] != e.publication.publication_id


def test_additive_upgrade_preserves_basis_versions_and_old_migration_checksums(contracts_database, tmp_path):
    name = 'test_energy_upgrade_'+uuid4().hex[:12]
    with psycopg.connect(**contracts_database, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(name)))
    target = contracts_database | {'dbname': name}
    try:
        root = files('greenhouse.db').joinpath('migrations')
        prior = tmp_path/'prior-migrations'
        prior.mkdir()
        for entry in root.iterdir():
            if entry.name.endswith('.sql') and entry.name < '011':
                (prior/entry.name).write_bytes(entry.read_bytes())
        with psycopg.connect(**target, autocommit=True) as conn:
            assert len(migrate(conn, prior)['applied']) == 10
            versions = conn.execute('SELECT * FROM config.area_basis_version ORDER BY basis_version_id').fetchall()
            applications = conn.execute('SELECT * FROM config.basis_application ORDER BY basis_version_id').fetchall()
            checksums = conn.execute('SELECT version,checksum FROM meta.schema_migration ORDER BY version').fetchall()
            result = migrate(conn)
            assert result['applied'] == ['011_energy_mart.sql'] and len(result['skipped']) == 10
            assert conn.execute('SELECT * FROM config.area_basis_version ORDER BY basis_version_id').fetchall() == versions
            assert conn.execute('SELECT * FROM config.basis_application ORDER BY basis_version_id').fetchall() == applications
            assert conn.execute("SELECT version,checksum FROM meta.schema_migration WHERE version<'011' ORDER BY version").fetchall() == checksums
            assert not migrate(conn)['applied']
    finally:
        with psycopg.connect(**contracts_database, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL('DROP DATABASE {} WITH (FORCE)').format(psycopg.sql.Identifier(name)))
