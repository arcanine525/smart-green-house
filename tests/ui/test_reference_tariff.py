"""Exercise registration against a real, isolated published energy fixture."""
from dataclasses import replace
from decimal import Decimal
import json

import psycopg
import pytest

from greenhouse.db import reference_tariff
from greenhouse.db.cost_adapter import PostgresCostAdapter
from greenhouse.cli import main
from test_climate_ui import run_app, metric
from ui.energy_fixture import energy_scope


def test_registration_requires_publication_and_is_idempotent(energy_scope, contracts_database, monkeypatch):
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        with pytest.raises(ValueError, match='energy pipeline'):
            reference_tariff.register_reference_tariff(conn, apply=True)
        fixture = energy_scope(rows=(('100', '10', '5'),))
        monkeypatch.setattr(reference_tariff, 'AGC_SCOPE', fixture.scope)
        monkeypatch.setattr(reference_tariff, 'AGC_SNAPSHOT', fixture.snapshot)
        monkeypatch.setattr(reference_tariff, 'AGC_SHA', fixture.tag * 2)
        adapter = PostgresCostAdapter(conn)
        assert reference_tariff.register_reference_tariff(conn)['status'] == 'ready'
        assert adapter.get_baseline_tariff(fixture.scope) is None
        first = reference_tariff.register_reference_tariff(conn, apply=True)
        second = reference_tariff.register_reference_tariff(conn, apply=True)
        assert first['status'] == 'registered'
        assert second == {'status': 'already_registered', 'tariff_version': first['tariff_version']}
        tariff = adapter.get_baseline_tariff(fixture.scope, fixture.comp)
        assert {rate.rate for rate in tariff.rates} == {Decimal('0.0083'), Decimal('0.08'), Decimal('0.04')}
        monkeypatch.setattr(reference_tariff, 'AGC_SHA', '0' * 64)
        with pytest.raises(ValueError, match='snapshot'):
            reference_tariff.register_reference_tariff(conn, apply=True)


def test_registration_preserves_existing_baseline(energy_scope, contracts_database, monkeypatch):
    fixture = energy_scope(rows=(('100', '10', '5'),))
    monkeypatch.setattr(reference_tariff, 'AGC_SCOPE', fixture.scope)
    monkeypatch.setattr(reference_tariff, 'AGC_SNAPSHOT', fixture.snapshot)
    monkeypatch.setattr(reference_tariff, 'AGC_SHA', fixture.tag * 2)
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        adapter = PostgresCostAdapter(conn)
        canonical = adapter.register_historical_tariff(fixture.scope)
        # The canonical tariff is immutable; insert a newer, distinct baseline.
        newer = replace(canonical, tariff_version='other:' + fixture.tag,
                        tariff_id='OTHER', version=2,
                        rates=tuple(replace(r, rate_id='other:' + r.rate_id,
                                            tariff_version='other:' + fixture.tag)
                                    for r in canonical.rates))
        adapter.register_tariff(newer)
        with pytest.raises(ValueError, match='different scope-wide baseline'):
            reference_tariff.register_reference_tariff(conn, apply=True)
        assert adapter.get_baseline_tariff(fixture.scope) == newer


def test_cli_setup_makes_cost_dashboard_available(energy_scope, contracts_database,
                                                   monkeypatch, capsys, run_app):
    fixture = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified')
    monkeypatch.setattr(reference_tariff, 'AGC_SCOPE', fixture.scope)
    monkeypatch.setattr(reference_tariff, 'AGC_SNAPSHOT', fixture.snapshot)
    monkeypatch.setattr(reference_tariff, 'AGC_SHA', fixture.tag * 2)
    monkeypatch.setenv('PGDATABASE', contracts_database['dbname'])
    assert main(['register-agc-tariff']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ready'
    assert main(['register-agc-tariff', '--apply']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'registered'
    assert main(['register-agc-tariff', '--apply']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'already_registered'
    app = run_app(contracts_database)
    app.radio(key='screen_choice').set_value('💶 Chi phí & Kịch bản (BR-06)').run()
    assert not app.exception
    assert metric(app, 'Tổng chi phí (EUR)') == '114.38 EUR'
