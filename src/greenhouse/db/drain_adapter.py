"""Sealed drain mart adapter for BR-03; SELECTs only, no core fallback.

Pins the publication, verifies the drain bundle receipt (010), reads the
frozen slot rows with their source revision identity, shares the immutable
schedule repository and the core lineage path with the other adapters. Raw
CSV parsing never leaks here; the per-measure quality triplets stay frozen
exactly as materialized. measurement_context is the exact
'drain_process_computer' identity — never renamed.
"""
from datetime import datetime

from greenhouse.contracts import CycleWindow
from greenhouse.db.drain_mart import BUNDLE_VERSION
from greenhouse.db.migrate import sql
from greenhouse.db.query_adapter import PostgresCoreQueryAdapter
from greenhouse.queries.slot_rows import DrainSlotRow

class PostgresDrainQueryAdapter(PostgresCoreQueryAdapter):
    """Default application adapter: sealed drain mart only; no core fallback."""

    @property
    def database_key(self):
        """Non-secret endpoint identity for the evaluation cache: no
        password, no DSN — different servers must never alias one entry."""
        info = self.connection.info
        return tuple(str(value) for value in (info.host, info.port, info.dbname, info.user))

    def publication(self, publication_id):
        publication = super().publication(publication_id)
        self._require_drain_mart(publication_id)
        return publication

    def _require_drain_mart(self, publication_id):
        receipt = self._rows('query_drain_receipt', (publication_id,))
        if len(receipt) != 1 or receipt[0]['bundle_version'] != BUNDLE_VERSION:
            raise ValueError('Publication lacks the drain mart bundle; '
                             'run a new pipeline workflow')

    def cycle_window(self, context):
        rows = self._rows('query_drain_cycle', (context.publication_id, context.scope_id,
                                                context.cycle_id,
                                                context.compartment_ids[0]))
        if len(rows) != 1:
            raise ValueError('Cycle/compartment is absent from the pinned drain mart')
        row = rows[0]
        return CycleWindow(row['cycle_id'], row['compartment_id'],
                           row['start_source_time'].date(),
                           row['end_source_time_exclusive'].date())

    def schedule(self, rule_version):
        """Explicit schedule selection through the repository contract
        (shared with the other adapters; drift content refused at
        registration)."""
        from greenhouse.db.rule_schedule import read_rule_schedule
        revision = read_rule_schedule(self.connection, rule_version)
        if revision is None:
            raise ValueError(f'Unknown schedule revision {rule_version}')
        return revision

    def read_drain_slots(self, context, publication, *, window_start=None,
                         window_end=None):
        """Frozen drain slot rows of the pinned publication bounded to a
        half-open source-time window; default bounds are the WHOLE cycle.
        The caller keeps the window bounded BEFORE fetch — a whole-cycle
        read is never the way to inspect one long episode."""
        context.matches(publication)
        cycle = self.cycle_window(context)
        start_bound = window_start or datetime.combine(cycle.start, datetime.min.time())
        end_bound = window_end or datetime.combine(cycle.end, datetime.min.time())
        rows = self._rows('query_drain_slots', (
            context.publication_id, context.scope_id, context.cycle_id,
            context.compartment_ids[0], start_bound, end_bound))
        result = [DrainSlotRow(
            slot_start=row['slot_start'], revision_id=row['revision_id'],
            logical_key=row['logical_record_key'],
            quality_policy_version=row['quality_policy_version'],
            correction_id=row['correction_id'],
            correction_version=row['correction_version'],
            ec_drain=row['ec_drain'], ph_drain=row['ph_drain'],
            measure_quality=row['measure_quality'])
            for row in rows]
        if len({row.slot_start for row in result}) != len(result):
            raise ValueError('Duplicate drain slot grain in the pinned mart')
        return tuple(result)
