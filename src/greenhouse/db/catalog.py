"""Read-only catalog SELECTs for application filters; no writes, no pointer reads.

The catalog never consults meta.current_publication for a chosen publication:
callers pin by publication ID (see query_adapter), so catalog rows and later
reads cannot race the pointer. Connection is owned by the caller.
"""
from psycopg.rows import dict_row

from greenhouse.contracts import CatalogCycle, CatalogPublication, IrrigationRuleProfile
from greenhouse.db.migrate import sql


class PostgresCatalogAdapter:
    def __init__(self, connection):
        self.connection = connection

    def _rows(self, name, parameters):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(sql(name), parameters).fetchall()

    def catalog_publications(self):
        """Current sealed publication per data scope, with its mart receipt."""
        return tuple(CatalogPublication(
            row['scope_id'], row['snapshot_id'], row['publication_id'], row['committed_at'],
            row['finality_status'], row['code_version'], row['bundle_version'],
            row['day_count'], row['cycle_count'])
            for row in self._rows('query_catalog_scopes', ()))

    def catalog_cycles(self, publication_id):
        """Cycle/compartment windows materialized for one pinned publication."""
        return tuple(CatalogCycle(row['cycle_id'], row['compartment_id'], row['compartment_name'],
                                  row['start_source_time'].date(), row['end_source_time_exclusive'].date())
                     for row in self._rows('query_catalog_cycles', (publication_id,)))

    def catalog_rules(self, scope_id, compartment_id):
        """Explicit immutable rule profiles valid for this scope/compartment."""
        return tuple(IrrigationRuleProfile(**row)
                     for row in self._rows('query_catalog_rules', (scope_id, compartment_id)))
