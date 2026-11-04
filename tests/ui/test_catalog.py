"""Catalog contract: UI filter options derive from published data, not IDs."""
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from greenhouse.contracts import CatalogCycle, CatalogPublication, IrrigationRuleProfile
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.irrigation_rules import register_irrigation_rule
from greenhouse.queries.catalog import CatalogService


def test_catalog_lists_current_publication_with_mart_receipt(published_scope):
    e = published_scope()
    rows = [row for row in e.catalog.publications() if row.scope_id == e.scope]
    assert len(rows) == 1
    row = rows[0]
    assert row.publication_id == e.publication.publication_id
    assert row.snapshot_id == e.snapshot
    assert row.usable is True
    assert row.bundle_version == 'irrigation-mart-v1'
    assert row.day_count == 2 and row.cycle_count == 1


def test_catalog_marks_pre_mart_publication_unusable(legacy_scope):
    rows = [row for row in legacy_scope.catalog.publications() if row.scope_id == legacy_scope.scope]
    assert len(rows) == 1 and rows[0].usable is False
    assert rows[0].bundle_version is None and rows[0].day_count is None


def test_catalog_cycles_belong_to_pinned_publication(published_scope):
    e = published_scope()
    cycles = e.catalog.cycles(e.publication)
    assert len(cycles) == 1
    cycle = cycles[0]
    assert (cycle.cycle_id, cycle.compartment_id, cycle.compartment_name) == (e.cycle, e.comp, 'Fixture')
    assert (cycle.start, cycle.end) == (e.start, e.end)
    assert e.catalog.cycle(e.publication, e.cycle) == cycle
    with pytest.raises(ValueError):
        e.catalog.cycle(e.publication, 'unknown-cycle')


def test_catalog_rules_are_scoped_to_scope_and_compartment(published_scope):
    e = published_scope()
    tag = uuid4().hex
    profile = IrrigationRuleProfile('rule-version:' + tag, 'rule:' + tag, 1, e.scope, e.comp,
                                    Decimal('20'), Decimal('40'), Decimal('0'), Decimal('100'),
                                    'synthetic review thresholds; not agronomic guidance', True, 'demo_rule')
    register_irrigation_rule(e.connection, profile)
    found = e.catalog.rules(e.publication, e.comp)
    assert [rule.rule_version for rule in found] == [profile.rule_version]
    assert e.catalog.rules(e.publication, 'other-compartment') == ()


def test_catalog_publication_dto_validates_receipt_fields():
    identity = dict(scope_id='scope', snapshot_id='snapshot', publication_id='pub',
                    finality_status='provisional', code_version='pipeline-v2-irrigation')
    committed = datetime.now(timezone.utc)
    assert CatalogPublication(**identity, committed_at=committed).usable is False
    assert CatalogPublication(**identity, committed_at=committed, bundle_version='irrigation-mart-v1',
                              day_count=1, cycle_count=1).usable is True
    with pytest.raises(ValueError):
        CatalogPublication(**identity, committed_at=committed, bundle_version='irrigation-mart-v1',
                           day_count=None, cycle_count=1)
    with pytest.raises(ValueError):
        CatalogPublication(**{**identity, 'finality_status': 'weird'}, committed_at=committed)


def test_catalog_cycle_dto_and_usable_helper(published_scope, legacy_scope):
    e = published_scope()
    with pytest.raises(ValueError):
        CatalogCycle('cycle', 'comp', '', date(2020, 1, 1), date(2020, 1, 5))
    rows = e.catalog.publications()
    usable = e.catalog.usable(rows)
    assert all(row.usable for row in usable)
    assert any(row.scope_id == e.scope for row in usable)
