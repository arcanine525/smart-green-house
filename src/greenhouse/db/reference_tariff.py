"""Explicitly register the documented historical AGC energy tariff."""
import argparse
import json
import sys

from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.cost_adapter import PostgresCostAdapter
from greenhouse.db.loader import AGC_SCOPE, AGC_SHA, AGC_SNAPSHOT


def register_reference_tariff(connection, *, apply=False):
    """Verify the locked historical scope and current energy publication first."""
    identity = connection.execute(
        "SELECT s.snapshot_id, d.archive_checksum, s.mode FROM meta.data_scope s "
        "JOIN meta.dataset_snapshot d ON d.snapshot_id=s.snapshot_id "
        "WHERE s.data_scope_id=%s", (AGC_SCOPE,)
    ).fetchone()
    if identity != (AGC_SNAPSHOT, AGC_SHA, 'historical'):
        raise ValueError('Locked AGC historical scope and snapshot are required')
    publications = PostgresCatalogAdapter(connection).catalog_publications()
    if not any(p.scope_id == AGC_SCOPE and p.snapshot_id == AGC_SNAPSHOT and p.energy_usable
               for p in publications):
        raise ValueError('Run and publish the AGC energy pipeline first')
    adapter = PostgresCostAdapter(connection)
    existing = adapter.get_baseline_tariff(AGC_SCOPE)
    if existing is not None:
        if not adapter._is_canonical_historical(existing):
            raise ValueError('A different scope-wide baseline already exists; it was not changed')
        return {'status': 'already_registered', 'tariff_version': existing.tariff_version}
    if not apply:
        return {'status': 'ready', 'scope_id': AGC_SCOPE, 'action': 'repeat with --apply'}
    profile = adapter.register_historical_tariff(AGC_SCOPE)
    return {'status': 'registered', 'tariff_version': profile.tariff_version}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m greenhouse register-agc-tariff')
    parser.add_argument('--apply', action='store_true', help='register the scope-wide baseline')
    options = parser.parse_args(argv)
    try:
        import psycopg
        from greenhouse.settings import Settings
        with psycopg.connect(**Settings.from_env().connection_kwargs()) as connection:
            result = register_reference_tariff(connection, apply=options.apply)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f'register-agc-tariff failed: {message}', file=sys.stderr)
        return 1
