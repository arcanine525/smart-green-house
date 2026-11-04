"""Explicit opt-in demo rule registration for BR-05 evidence environments.

Additive ONLY: the applied migration state stays immutable; the legacy (v1)
demo rows keep their rule_version strings and remain resolvable. This seed
targets a FIXED explicit version (v2) of each compartment family
`harvest-demo-b10:<compartment>` of the one current harvest-usable
publication, with a stable accurate provenance constant citing the synthetic
10% boundary example documented in docs/br/br_05_harvest-quality-tracking.md
§4.2/AC-08 (never any unsupported dataset claim).

Run deliberately on a validation database. The app never seeds, workflows
never call this, and no migration auto-seeds:

  # dry run (default): prints the plan only
  docker compose -p smart-greenhouse run --rm job \
      python -m greenhouse.db.demo_rules
  # add --apply to write the new versions

Semantics at the fixed demo identity (rule_id family, version, scope,
compartment, deterministic rule_version):
  * absent → register (a fresh database without legacy rows also gets v2);
  * present with identical full identity + content → REUSE, no write;
  * present with diverging content at that identity → DRIFT, loud refusal
    (no auto version bump, no upsert, no rewrite);
  * the rule_version is claimed by another scope/compartment → foreign
    identity collision, loud refusal.
No threshold is ever applied automatically: choosing a profile stays an
explicit query-time decision of the screen/API.
"""
import argparse
import sys
from datetime import datetime
from decimal import Decimal

import psycopg

from greenhouse.contracts import (
    RuleBasis, RuleScheduleRevision, RuleScheduleSegment, HarvestRuleProfile, from_json, to_json,
)
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.db.harvest_rules import register_harvest_rule
from greenhouse.db.rule_schedule import register_rule_schedule
from greenhouse.queries.catalog import CatalogService
from greenhouse.settings import Settings

DEMO_VERSION = 2
DEMO_THRESHOLD = Decimal('10')
DEMO_UNIT = 'kg/m²'
DEMO_RATIO_UNIT = '%'
DEMO_LABEL = 'demo_rule'
FAMILY_PREFIX = 'harvest-demo-b10'
DEMO_EVIDENCE = (
    'Synthetic demo per docs/br/br_05_harvest-quality-tracking.md §4.2/AC-08: '
    '10% class-B share boundary example; explicitly NOT an AGC standard threshold. '
    'Supersedes the earlier unsupported demo wording (v1).')


def _plan_entry(connection, publication, cycle):
    """One compartment's reuse/register/refuse plan at the fixed identity.

    Comparison covers the COMPLETE stored identity (rule_id, version, scope,
    compartment) plus all content columns — a rule_version reuse never
    inherits a foreign or drifted identity."""
    family = f'{FAMILY_PREFIX}:{cycle.compartment_id}'
    rule_version = f'{family}:v{DEMO_VERSION}'
    scope_id, compartment_id = publication.scope_id, cycle.compartment_id
    stored = connection.execute(
        'SELECT rule_id, version, data_scope_id, compartment_id, max_class_b_share_pct, '
        'unit, ratio_unit, evidence_reference, is_demo, label '
        'FROM config.harvest_rule_profile WHERE rule_version=%s', (rule_version,)).fetchone()
    if stored is not None:
        identity = stored[:4]
        if identity != (family, DEMO_VERSION, scope_id, compartment_id):
            raise ValueError(
                f'demo rule identity at {rule_version} is claimed by different stored identity '
                f'{identity}, expected {(family, DEMO_VERSION, scope_id, compartment_id)}; '
                'refusing (no foreign scope/compartment reuse, no drift inheritance)')
        content = stored[4:]
        if content == (DEMO_THRESHOLD, DEMO_UNIT, DEMO_RATIO_UNIT, DEMO_EVIDENCE,
                       True, DEMO_LABEL):
            return {'compartment': compartment_id, 'result': 'reuse',
                    'rule_version': rule_version, 'version': DEMO_VERSION}
        raise ValueError(
            f'demo rule drift at the fixed identity {rule_version}: existing content '
            f'{content} differs from the documented provenance '
            f'{(DEMO_THRESHOLD, DEMO_UNIT, DEMO_RATIO_UNIT, DEMO_EVIDENCE, True, DEMO_LABEL)}; '
            'refusing (no auto version bump, no rewrite)')
    profile = HarvestRuleProfile(
        rule_version=rule_version, rule_id=family, version=DEMO_VERSION,
        scope_id=scope_id, compartment_id=compartment_id,
        max_class_b_share_pct=DEMO_THRESHOLD, unit=DEMO_UNIT,
        ratio_unit=DEMO_RATIO_UNIT, evidence_reference=DEMO_EVIDENCE,
        is_demo=True, label=DEMO_LABEL)
    return {'compartment': compartment_id, 'result': 'register',
            'rule_version': rule_version, 'version': DEMO_VERSION,
            'profile': profile}


def seed(connection, apply=False, scope_id=None):
    """Plan (dry run) or register the complete demo profile set at v2.

    The CLI stays fail-safe (no scope argument: exactly one harvest-usable
    publication is required). Explicit tests pass `scope_id` to pin the
    target scope when the harness database holds several scopes."""
    catalog = CatalogService(PostgresCatalogAdapter(connection))
    usable = catalog.harvest_usable()
    if scope_id is None:
        if len(usable) != 1:
            raise ValueError('Expected exactly one harvest-usable publication; '
                             'refusing to guess a scope')
    else:
        usable = [row for row in usable if row.scope_id == scope_id]
        if len(usable) != 1:
            raise ValueError(f'Expected exactly one harvest-usable publication for '
                             f'scope {scope_id}, found {len(usable)}')
    adapter = PostgresHarvestQueryAdapter(connection)
    publication = adapter.publication(usable[0].publication_id)
    if usable[0].harvest_bundle_version != 'harvest-mart-v3':
        raise ValueError('Expected the harvest-mart-v3 receipt publication')
    planned = [_plan_entry(connection, publication, cycle)
               for cycle in catalog.cycles(publication)]
    result = {'mode': 'dry-run' if not apply else 'applied',
              'publication_id': publication.publication_id,
              'scope': publication.scope_id,
              'entries': [{key: (None if key == 'profile' else value)
                           for key, value in entry.items()} for entry in planned]}
    if not apply:
        return result
    for entry in planned:
        if entry['result'] == 'register':
            register_harvest_rule(connection, entry['profile'])
    connection.commit()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m greenhouse.db.demo_rules',
        description='Opt-in demo rule registration for the current harvest publication '
                    '(fixed v2 identity, idempotent, provenance constant; '
                    'dry run by default)')
    parser.add_argument('--apply', action='store_true',
                        help='write the new versions; the default is a dry run')
    parser.add_argument('--kind', choices=('harvest', 'climate'), default='harvest',
                        help='demo family: harvest share rule (default) or climate '
                             'BR-01 schedule (delta 0.5 °C / 50 ppm, requested+realized)')
    options = parser.parse_args(argv)
    kwargs = Settings.from_env().connection_kwargs()
    with psycopg.connect(**kwargs) as connection:
        result = (seed(connection, apply=options.apply) if options.kind == 'harvest'
                  else seed_climate(connection, apply=options.apply))
        print(result)
    return 0




# ---------------------------------------------------------------- BR-01 demo

CLIMATE_FAMILY_PREFIX = 'climate-demo-b01'
CLIMATE_DELTA_TEMP = Decimal('0.5')
CLIMATE_DELTA_CO2 = Decimal('50')
CLIMATE_BASIS = (RuleBasis.REQUESTED, RuleBasis.REALIZED)
CLIMATE_EVIDENCE = (
    'Synthetic demo per docs/br/br_01_climate-condition-monitoring.md §4.1/§7: '
    'delta_temp 0.5 °C and delta_co2 50 ppm applied to BOTH the requested and '
    'realized bases; explicitly NOT an agronomic-standard AGC threshold and not '
    'derived from the setpoint dictionary. Registration boundary follows the '
    'approved T14 schedule semantics (one segment mode, at most one revision '
    'per requested metric key; historical revisions stay replayable).')


def _climate_plan_entry(connection, publication, cycle):
    """Fixed demo identity for one compartment: v1 schedule carrying
    temperature+CO2 segments on BOTH climate bases (requested/realized).
    Reuse exactly for identical content; refuse drift and foreign claims."""
    family = f'{CLIMATE_FAMILY_PREFIX}:{cycle.compartment_id}'
    rule_version = f'{family}:v1'
    scope_id, compartment_id = publication.scope_id, cycle.compartment_id
    from greenhouse.db.rule_schedule import read_rule_schedule
    stored_revision = read_rule_schedule(connection, rule_version)
    expected_segments = tuple(
        RuleScheduleSegment(metric=metric, measurement_context='climate', basis=basis,
                            start=datetime.combine(cycle.start, datetime.min.time()),
                            end=datetime.combine(cycle.end, datetime.min.time()),
                            delta=CLIMATE_DELTA_TEMP if metric == 'temperature' else CLIMATE_DELTA_CO2)
        for basis in CLIMATE_BASIS for metric in ('temperature', 'co2'))
    expected = RuleScheduleRevision(
        rule_version=rule_version, rule_id=family, version=1,
        scope_id=scope_id, compartment_id=compartment_id,
        evidence_reference=CLIMATE_EVIDENCE, is_demo=True, label='demo_rule',
        segments=tuple(sorted(expected_segments, key=lambda s: (
            s.start, s.metric, s.measurement_context, s.basis.value, s.end))))
    if stored_revision is not None:
        if stored_revision != expected:
            raise ValueError(
                f'climate demo drift at the fixed identity {rule_version}: the stored revision '
                'differs from the documented provenance; refusing (no auto version bump, '
                'no rewrite)')
        return {'compartment': compartment_id, 'result': 'reuse',
                'rule_version': rule_version, 'version': 1}
    return {'compartment': compartment_id, 'result': 'register',
            'rule_version': rule_version, 'version': 1, 'revision': expected}


def seed_climate(connection, apply=False, scope_id=None):
    """Plan (dry run) or register the BR-01 demo schedule on the current
    climate-usable publication: v1 per compartment, both climate bases,
    delta_temp 0.5 °C / delta_co2 50 ppm, labelled demo_rule. No threshold
    is ever applied automatically: choosing the revision stays an explicit
    query-time decision."""
    catalog = CatalogService(PostgresCatalogAdapter(connection))
    usable = catalog.climate_usable()
    if scope_id is None:
        if len(usable) != 1:
            raise ValueError('Expected exactly one climate-usable publication; '
                             'refusing to guess a scope')
    else:
        usable = [row for row in usable if row.scope_id == scope_id]
        if len(usable) != 1:
            raise ValueError(f'Expected exactly one climate-usable publication for '
                             f'scope {scope_id}, found {len(usable)}')
    from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
    publication = PostgresClimateQueryAdapter(connection).publication(usable[0].publication_id)
    planned = [_climate_plan_entry(connection, publication, cycle)
               for cycle in catalog.cycles(publication)]
    result = {'mode': 'dry-run' if not apply else 'applied',
              'publication_id': publication.publication_id, 'scope': publication.scope_id,
              'entries': [{key: (None if key in ('profile', 'revision') else value)
                           for key, value in entry.items()} for entry in planned]}
    if not apply:
        return result
    for entry in planned:
        if entry['result'] == 'register':
            register_rule_schedule(connection, entry['revision'])
    connection.commit()
    return result

if __name__ == '__main__':
    sys.exit(main())
