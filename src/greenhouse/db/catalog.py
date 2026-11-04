"""Read-only catalog SELECTs for application filters; no writes, no pointer reads.

The catalog never consults meta.current_publication for a chosen publication:
callers pin by publication ID (see query_adapter), so catalog rows and later
reads cannot race the pointer. Connection is owned by the caller.
"""
from dataclasses import dataclass
from datetime import date

from psycopg.rows import dict_row

from greenhouse.contracts import (
    CatalogCycle, CatalogPublication, HarvestRuleProfile, IrrigationRuleProfile, RuleBasis,
)
from greenhouse.db.climate_mart import BUNDLE_VERSION as CLIMATE_BUNDLE_VERSION
from greenhouse.db.harvest_mart import BUNDLE_VERSION as HARVEST_BUNDLE_VERSION
from greenhouse.db.migrate import sql


@dataclass(frozen=True)
class ScheduleRevisionOption:
    """Selectable immutable schedule revision for the UI (identity + keys
    only; segments load through the repository at evaluation time)."""
    rule_version: str
    rule_id: str
    version: int
    evidence_reference: str
    is_demo: bool
    label: str
    keys: tuple[tuple[str, str, RuleBasis], ...]
    segment_count: int

    def __post_init__(self):
        if self.segment_count < 1:
            raise ValueError('A schedule option represents an existing revision with segments')

    @property
    def demo_flag(self):
        return self.is_demo


class PostgresCatalogAdapter:
    def __init__(self, connection):
        self.connection = connection

    def _rows(self, name, parameters):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(sql(name), parameters).fetchall()

    def catalog_publications(self):
        """Current sealed publication per data scope, with its mart receipts.

        The water side keeps the water-receipt fact verbatim; the harvest
        side reports only the bundle THIS release reads (an older sealed
        receipt is a stale value the catalog may not pass on as usable —
        it shows explicit reasons instead)."""
        rows = []
        for row in self._rows('query_catalog_scopes', ()):
            version, reasons = row['harvest_bundle_version'], ()
            if version != HARVEST_BUNDLE_VERSION:
                reasons = ('missing_expected_harvest_bundle',)
                version = None
            climate_version, climate_reasons = row['climate_bundle_version'], ()
            if climate_version != CLIMATE_BUNDLE_VERSION:
                climate_reasons = ('missing_expected_climate_bundle',)
                climate_version = None
            rows.append(CatalogPublication(
                row['scope_id'], row['snapshot_id'], row['publication_id'], row['committed_at'],
                row['finality_status'], row['code_version'], row['bundle_version'],
                row['day_count'], row['cycle_count'], version, tuple(reasons),
                climate_version, tuple(climate_reasons)))
        return tuple(rows)

    def climate_usable(self, publications=None):
        rows = self.publications() if publications is None else tuple(publications)
        return tuple(row for row in rows if row.climate_usable)

    def catalog_cycles(self, publication_id):
        """Cycle/compartment windows materialized for one pinned publication."""
        return tuple(CatalogCycle(row['cycle_id'], row['compartment_id'], row['compartment_name'],
                                  row['start_source_time'].date(), row['end_source_time_exclusive'].date())
                     for row in self._rows('query_catalog_cycles', (publication_id,)))

    def catalog_rules(self, scope_id, compartment_id):
        """Explicit immutable irrigation rule profiles for one scope/compartment."""
        return tuple(IrrigationRuleProfile(**row)
                     for row in self._rows('query_catalog_rules', (scope_id, compartment_id)))

    def catalog_harvest_rules(self, scope_id, compartment_id):
        """Explicit immutable class-B threshold profiles for one scope/compartment."""
        return tuple(HarvestRuleProfile(**row)
                     for row in self._rows('query_harvest_rule_catalog', (scope_id, compartment_id)))

    def catalog_schedule_rules(self, scope_id, compartment_id, *, measurement_context,
                               metrics, bases):
        """Selectable immutable schedule revisions for one scope/compartment,
        FILTERED to the declared evaluation group (metric/context/basis) so
        future drain revisions can never appear in the climate UI list or
        the other way around. EVERY segment of the revision must match the
        declared group: mixed or foreign-group revisions would be refused
        by the declared service boundary, so they are never offered. Partial
        profiles (a subset of the declared metrics) stay legal. Identity/
        keys only, no fallback and no latest auto-pick."""
        declared_metrics = list(metrics)
        declared_bases = [basis.value if isinstance(basis, RuleBasis) else str(basis)
                          for basis in bases]
        group = (measurement_context, declared_metrics, declared_bases)
        return tuple(
            ScheduleRevisionOption(
                rule_version=row['rule_version'], rule_id=row['rule_id'],
                version=row['version'], evidence_reference=row['evidence_reference'],
                is_demo=row['is_demo'], label=row['label'],
                keys=tuple((entry['metric'], entry['measurement_context'],
                            RuleBasis(entry['basis'])) for entry in row['keys']),
                segment_count=row['segment_count'])
            for row in self._rows('query_catalog_schedule_rules',
                                  (scope_id, compartment_id) + group + group))
