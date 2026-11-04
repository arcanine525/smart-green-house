"""PostgreSQL implementation of the structural query contract; SELECTs only.

Own the supplied connection in the caller. Pin once, then use that publication
ID throughout chart/summary/lineage/export. No query here repins implicitly.
"""
from datetime import time
from pathlib import Path

from psycopg.rows import dict_row

from greenhouse.contracts import (
    ASSUMPTION_LABEL, RESOURCE_WATER_UNIT, WATER_UNIT, Applicability, BasisStatus,
    CycleWindow, IrrigationDay, LineageResult, MeasureUnit, MeasureValue,
    PublicationRef, QualityStatus, Versions,
)
from greenhouse.db.lineage import lineage
from greenhouse.db.migrate import sql
from greenhouse.db.pipeline import pin_publication


class PostgresQueryAdapter:
    def __init__(self, connection, *, raw_snapshots=None):
        self.connection = connection
        self.raw_snapshots = {k: Path(v) for k, v in (raw_snapshots or {}).items()}

    def _rows(self, name, parameters):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(sql(name), parameters).fetchall()

    def pin_publication(self, scope_id):
        return self.publication(pin_publication(self.connection, scope_id))

    def publication(self, publication_id):
        rows = self._rows('query_publication', (publication_id,))
        if len(rows) != 1:
            raise ValueError('Unknown or unsealed publication without workflow provenance')
        row = rows[0]
        return PublicationRef(row['publication_id'], row['scope_id'], row['snapshot_id'],
                              Versions(row['run_id'], row['quality_policy_version'], row['parser_version'],
                                       row['code_version'], row['config_bundle_hash']),
                              row['committed_at'], row['finality_status'])

    def cycle_window(self, context):
        rows = self._rows('query_cycle', (context.scope_id, context.snapshot_id, context.cycle_id, context.compartment_ids[0]))
        if len(rows) != 1:
            raise ValueError('Cycle/compartment is not mapped to this snapshot and scope')
        row = rows[0]
        if any(row[key].time() != time.min for key in ('start_source_time', 'end_source_time_exclusive')):
            raise ValueError('BR-02 requires a cycle with whole source dates')
        return CycleWindow(row['cycle_id'], row['compartment_id'], row['start_source_time'].date(),
                           row['end_source_time_exclusive'].date())

    def read_irrigation_days(self, context, publication):
        context.matches(publication)
        rows = self._rows('query_irrigation_days', (context.publication_id, context.scope_id,
                          context.snapshot_id, context.cycle_id, context.compartment_ids[0], context.start, context.end))
        days = {}
        for row in rows:
            if row['source_unit'] != RESOURCE_WATER_UNIT:
                raise ValueError('Unexpected resource water unit: expected L/m²/day')
            status = BasisStatus(row['basis_status']) if row['basis_status'] else BasisStatus.UNKNOWN
            measure = MeasureValue(row['value'], WATER_UNIT, QualityStatus(row['quality']),
                                   Applicability(row['applicability_status']), row['reason_code'], row['source_unit'],
                                   status, row['basis_version_id'], row['area_m2'], row['basis_kind'],
                                   row['assumption_id'], row['assumption_version'],
                                   ASSUMPTION_LABEL if status == BasisStatus.ASSUMED else 'verified' if status == BasisStatus.VERIFIED else None)
            metadata = (row['revision_id'], row['logical_record_key'], row['cycle_id'], row['compartment_id'],
                        row['correction_id'], row['correction_version'])
            day = days.setdefault(row['resource_date'], (metadata, {}))
            if day[0] != metadata or row['measure_code'] in day[1]:
                raise ValueError('Duplicate day/measure grain or join fanout')
            day[1][row['measure_code']] = measure
        result = []
        for when, (metadata, measures) in sorted(days.items()):
            if set(measures) != {'Irr', 'Drain'}:
                raise ValueError('Published day is missing measure quality/unit metadata')
            revision, key, cycle, compartment, correction, version = metadata
            result.append(IrrigationDay(publication, cycle, compartment, when, measures['Irr'], measures['Drain'],
                                        revision, key, correction, version))
        return tuple(result)

    def read_lineage(self, publication, *, revision_id=None, record_key=None):
        if (revision_id is None) == (record_key is None):
            raise ValueError('Select exactly one revision_id or logical record key')
        rows = self._rows('query_lineage_member', (publication.publication_id, publication.scope_id,
                          publication.snapshot_id, revision_id, revision_id, record_key, record_key))
        if len(rows) != 1:
            raise ValueError('Revision/key absent or ambiguous in the pinned publication; select a revision ID')
        member = rows[0]
        raw = self.raw_snapshots.get(publication.snapshot_id)
        if raw is None:
            raise ValueError('Raw snapshot mount not configured')
        source = lineage(self.connection, member['revision_id'], raw)
        units = self._rows('query_lineage_units', (member['record_type'],))
        return LineageResult(
            publication=publication, revision_id=member['revision_id'], record_type=member['record_type'],
            units=tuple(MeasureUnit(r['measure_code'], r['unit']) for r in units),
            logical_record_key=member['logical_record_key'], cycle_id=member['cycle_id'], compartment_id=member['compartment_id'],
            revision_number=source['revision_number'], source_record_id=source['source_record_id'], first_run_id=source['first_run_id'],
            relative_path=source['relative_path'], file_checksum=source['file_checksum'], archive_sha256=source['archive_sha256'],
            line_number=source['line_number'], line_end=source['line_end'], raw_reference=source['raw_reference'], raw_hash=source['raw_hash'],
            raw_hash_verified=source['raw_hash_verified'], source_time_serial=member['source_time_serial'],
            source_timestamp=member['source_timestamp'], correction_id=source['correction_id'], correction_version=member['correction_version'],
        )
