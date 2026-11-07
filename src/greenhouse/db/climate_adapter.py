"""Sealed climate mart adapter for BR-01; SELECTs only, no core fallback.

Pins the publication, verifies the climate bundle receipt (009), reads the
frozen slot rows with their source revision identity, exposes immutable
schedule revisions through the repository, and reuses the core lineage path
(pinned membership + raw hashes). Raw CSV parsing never leaks here; the
per-measure quality triplets stay frozen exactly as materialized.
"""
from datetime import datetime

from greenhouse.contracts import CycleWindow
from greenhouse.db.climate_mart import BUNDLE_VERSION
from greenhouse.db.migrate import sql
from greenhouse.db.query_adapter import PostgresCoreQueryAdapter
from greenhouse.queries.slot_rows import ClimateSlotRow

class PostgresClimateQueryAdapter(PostgresCoreQueryAdapter):
    """Default application adapter: sealed climate mart only; no core fallback."""

    @property
    def database_key(self):
        """Non-secret endpoint identity for the evaluation cache: no
        password, no DSN — different servers must never alias one entry."""
        info = self.connection.info
        return tuple(str(value) for value in (info.host, info.port, info.dbname, info.user))

    def publication(self, publication_id):
        publication = super().publication(publication_id)
        self._require_climate_mart(publication_id)
        return publication

    def _require_climate_mart(self, publication_id):
        receipt = self._rows('query_climate_receipt', (publication_id,))
        if len(receipt) != 1 or receipt[0]['bundle_version'] != BUNDLE_VERSION:
            raise ValueError('Publication lacks the climate mart bundle; run a new pipeline workflow')

    def cycle_window(self, context):
        """Climate mart grain: same contract anchor as the water cycle."""
        rows = self._rows('query_climate_cycle', (context.publication_id, context.scope_id,
                                                 context.cycle_id,
                                                 context.compartment_ids[0]))
        if len(rows) != 1:
            raise ValueError('Cycle/compartment is absent from the pinned climate mart')
        row = rows[0]
        return CycleWindow(row['cycle_id'], row['compartment_id'],
                           row['start_source_time'].date(), row['end_source_time_exclusive'].date())

    def schedule(self, rule_version):
        """Explicit schedule selection through the repository contract."""
        from greenhouse.db.rule_schedule import read_rule_schedule
        revision = read_rule_schedule(self.connection, rule_version)
        if revision is None:
            raise ValueError(f'Unknown schedule revision {rule_version}')
        return revision

    def read_climate_slots(self, context, publication, *, window_start=None, window_end=None):
        """Frozen slot rows of the pinned publication bounded to a half-open
        source-time window; default bounds are the WHOLE cycle (episode
        building needs the full-cycle grain). Callers keep the window as
        small as the view needs — the service never materializes the whole
        window twice for the same rerun."""
        context.matches(publication)
        cycle = self.cycle_window(context)
        start_bound = (window_start or datetime.combine(cycle.start, datetime.min.time()))
        end_bound = (window_end or datetime.combine(cycle.end, datetime.min.time()))
        rows = self._rows('query_climate_slots', (
            context.publication_id, context.scope_id, context.cycle_id,
            context.compartment_ids[0], start_bound, end_bound))
        result = []
        for row in rows:
            result.append(ClimateSlotRow(
                slot_start=row['slot_start'], revision_id=row['revision_id'],
                logical_key=row['logical_record_key'],
                quality_policy_version=row['quality_policy_version'],
                correction_id=row['correction_id'], correction_version=row['correction_version'],
                tair=row['tair'], co2air=row['co2air'], t_heat_sp=row['t_heat_sp'],
                t_vent_sp=row['t_vent_sp'], co2_sp=row['co2_sp'],
                t_heat_vip=row['t_heat_vip'], t_ventlee_vip=row['t_ventlee_vip'],
                co2_vip=row['co2_vip'], measure_quality=row['measure_quality']))
        if len({row.slot_start for row in result}) != len(result):
            raise ValueError('Duplicate climate slot grain in the pinned mart')
        return tuple(result)
