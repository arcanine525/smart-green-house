"""Immutable engineer profile boundaries over the real published drain fixture."""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest

from greenhouse.app import wiring
from greenhouse.db.drain_profiles import save_drain_profile
from greenhouse.db.rule_schedule import read_rule_schedule
from test_drain_mart import contracts_database, drain_database, publish_drain_fixture, make_service, drain_context


def test_engineer_profile_versions_replay_and_reject_drift(drain_database, tmp_path):
    with psycopg.connect(**drain_database, autocommit=True) as conn:
        fixture = publish_drain_fixture(conn, tmp_path, {'Only': {0: {'EC_drain_PC': '6'}}})
        service = make_service(conn, fixture)
        publication = service.adapter.publication(fixture['publication'])
        cycle = wiring.catalog_service(conn).cycles(publication)[0]
        args = dict(name='engineer', ec_lower='2', ec_upper='5', ph_lower='5.5', ph_upper='6.5',
                    provenance='Unverified engineer hypothesis')
        first = save_drain_profile(conn, publication, cycle, version=1, **args)
        original = service.get_evaluation(drain_context(fixture, 'Only'), rule_version=first.rule_version)
        second = save_drain_profile(conn, publication, cycle, version=2, **(args | {'ec_upper': '10'}))
        changed = service.get_evaluation(drain_context(fixture, 'Only'), rule_version=second.rule_version)
        assert changed.result.metric_results[0].out_minutes < original.result.metric_results[0].out_minutes
        assert service.get_evaluation(drain_context(fixture, 'Only'), rule_version=first.rule_version) == original
        assert save_drain_profile(conn, publication, cycle, version=1, **args) == first
        with pytest.raises(ValueError, match='drift'):
            save_drain_profile(conn, publication, cycle, version=1, **(args | {'ec_upper': '10'}))
        assert read_rule_schedule(conn, first.rule_version) == first
        assert conn.execute('SELECT count(*) FROM meta.publication').fetchone()[0] == 1
        assert conn.execute('SELECT publication_id FROM meta.current_publication').fetchone()[0] == publication.publication_id


def test_engineer_profile_validation_keeps_typed_contract(drain_database, tmp_path):
    with psycopg.connect(**drain_database, autocommit=True) as conn:
        fixture = publish_drain_fixture(conn, tmp_path, {'Only': {}})
        publication = make_service(conn, fixture).adapter.publication(fixture['publication'])
        cycle = wiring.catalog_service(conn).cycles(publication)[0]
        args = dict(name='engineer', version=1, ec_lower='2', ec_upper='5',
                    ph_lower='5.5', ph_upper='6.5', provenance='Unverified hypothesis')
        for invalid in ({'ec_lower': '-1'}, {'ec_upper': 'NaN'}, {'ph_upper': 'Infinity'},
                        {'ec_lower': '6'}, {'ph_lower': '7'}, {'provenance': '  '},
                        {'version': 0}, {'name': 'not a stable name'}):
            with pytest.raises(ValueError):
                save_drain_profile(conn, publication, cycle, **(args | invalid))
        with pytest.raises(ValueError, match='cửa sổ'):
            save_drain_profile(conn, publication, replace(cycle, end=cycle.end + timedelta(days=1)), **args)
        assert conn.execute('SELECT count(*) FROM config.rule_schedule_revision').fetchone()[0] == 0
