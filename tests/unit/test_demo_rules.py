"""Demo rule seeding contract (R4): fixed explicit v2 identity, idempotent.

Explicit scope pinning keeps the module-scope harness database (which
accumulates several harvest-usable scopes) testable; the CLI stays fail-safe
without an argument.
"""
from decimal import Decimal

import pytest

from greenhouse.db.demo_rules import DEMO_EVIDENCE, seed
from test_harvest_mart import D1, contracts_database, published_harvest


def _rows(connection):
    return connection.execute(
        'SELECT rule_version, rule_id, version, data_scope_id, compartment_id, '
        'max_class_b_share_pct, evidence_reference FROM config.harvest_rule_profile '
        'ORDER BY rule_version').fetchall()


def test_demo_seed_dry_run_adds_nothing(published_harvest, contracts_database):
    e = published_harvest([(D1, '1', '0.2')])
    before = _rows(e['connection'])
    plan = seed(e['connection'], scope_id=e['scope'])
    assert plan['mode'] == 'dry-run'
    assert plan['entries'] and all(
        entry['result'] == 'register' and entry['version'] == 2
        for entry in plan['entries'])
    assert _rows(e['connection']) == before  # dry run wrote nothing


def test_demo_seed_registers_fixed_v2_then_reuses(published_harvest, contracts_database):
    e = published_harvest([(D1, '1', '0.2')])
    applied = seed(e['connection'], apply=True, scope_id=e['scope'])
    assert applied['mode'] == 'applied'
    assert all(entry['result'] == 'register' for entry in applied['entries'])
    rows = _rows(e['connection'])
    assert all(row[2] == 2 and row[6] == DEMO_EVIDENCE for row in rows)
    again = seed(e['connection'], apply=True, scope_id=e['scope'])
    assert all(entry['result'] == 'reuse' for entry in again['entries'])
    assert _rows(e['connection']) == rows  # reuse wrote nothing


def test_demo_seed_rejects_content_drift_at_fixed_identity(published_harvest,
                                                            contracts_database):
    e = published_harvest([(D1, '1', '0.2')])
    rule_version = f'harvest-demo-b10:{e["comp"]}:v2'
    # A row stored at the FIXED identity with diverging content must make the
    # seed refuse — no auto version bump, no rewrite, no release-identification.
    e['connection'].execute(
        'INSERT INTO config.harvest_rule_profile VALUES (%s,%s,2,%s,%s,%s,%s,%s,%s,%s,%s)',
        (rule_version, f'harvest-demo-b10:{e["comp"]}', e['scope'], e['comp'],
         Decimal('10'), 'kg/m²', '%', 'other wording at the same identity',
         True, 'demo_rule'))
    with pytest.raises(ValueError, match='drift'):
        seed(e['connection'], apply=True, scope_id=e['scope'])
    untouched = e['connection'].execute(
        'SELECT evidence_reference FROM config.harvest_rule_profile '
        'WHERE rule_version=%s', (rule_version,)).fetchone()
    assert untouched[0] == 'other wording at the same identity'


def test_demo_seed_rejects_foreign_identity_claim(published_harvest, contracts_database):
    first = published_harvest([(D1, '1', '0.2')])
    second = published_harvest([(D1, '1', '0.2')])
    rule_version = f'harvest-demo-b10:{first["comp"]}:v2'
    # The SAME rule_version string claimed by another data scope/compartment
    # row: pinned-scope seed must refuse, never silently reuse.
    first['connection'].execute(
        'INSERT INTO config.harvest_rule_profile VALUES (%s,%s,2,%s,%s,%s,%s,%s,%s,%s,%s)',
        (rule_version, f'harvest-demo-b10:{first["comp"]}', second['scope'],
         second['comp'], Decimal('10'), 'kg/m²', '%', 'foreign claim',
         True, 'demo_rule'))
    with pytest.raises(ValueError, match='claimed by different stored identity'):
        seed(first['connection'], apply=True, scope_id=first['scope'])
    # The foreign claim does not leak into the other compartment's OWN
    # identity: its family still plans a clean v2 registration under its scope.
    plan = seed(first['connection'], apply=False, scope_id=second['scope'])
    assert plan['entries'] and all(
        entry['result'] == 'register' and entry['rule_version'].endswith(':v2')
        and rule_version not in entry['rule_version'] for entry in plan['entries'])


def test_demo_seed_rejects_drifted_identity_columns(published_harvest, contracts_database):
    """Full-identity comparison: a stored row whose rule_id/version columns
    drift (same rule_version string) can never be treated as the demo row."""
    e = published_harvest([(D1, '1', '0.2')])
    rule_version = f'harvest-demo-b10:{e["comp"]}:v2'
    e['connection'].execute(
        'INSERT INTO config.harvest_rule_profile VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (rule_version, 'other-rule-family', 7, e['scope'], e['comp'],
         Decimal('10'), 'kg/m²', '%', DEMO_EVIDENCE, True, 'demo_rule'))
    with pytest.raises(ValueError, match='claimed by different stored identity'):
        seed(e['connection'], apply=True, scope_id=e['scope'])


def test_demo_seed_cli_is_fail_safe_on_ambiguous_scope(published_harvest, contracts_database):
    """Without an explicit scope the seed requires exactly one usable
    publication — several scopes in one harness database must refuse."""
    first = published_harvest([(D1, '1', '0.2')])
    published_harvest([(D1, '1', '0.2')])
    with pytest.raises(ValueError, match='exactly one harvest-usable publication'):
        seed(first['connection'])
