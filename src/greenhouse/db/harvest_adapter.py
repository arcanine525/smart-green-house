"""PostgreSQL implementation of the BR-05 harvest query contract; SELECTs only.

Same pinning rules as the water adapter: pin the chosen publication once per
action and reuse its IDs; a publication without the sealed harvest bundle
fails actionably, with no fallback to core or the current pointer. The caller
owns the connection.
"""
from datetime import date, time
from decimal import Decimal

from psycopg.rows import dict_row

from greenhouse.contracts import (
    ASSUMPTION_LABEL, HARVEST_SOURCE_UNIT, HARVEST_UNIT, Applicability, BasisStatus, CycleWindow,
    HarvestCompleteness, HarvestRecord, HarvestUnresolvedIssue, MeasureValue, PublicationRef,
    QualityStatus, Versions,
)
from greenhouse.db.harvest_mart import BUNDLE_VERSION
from greenhouse.db.migrate import sql
from greenhouse.db.pipeline import pin_publication


class PostgresHarvestQueryAdapter:
    def __init__(self, connection):
        self.connection = connection

    def _rows(self, name, parameters):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(sql(name), parameters).fetchall()

    def pin_publication(self, scope_id):
        return self.publication(pin_publication(self.connection, scope_id))

    def publication(self, publication_id):
        rows = self._rows('query_publication', (publication_id,))
        if len(rows) != 1:
            raise ValueError('Unknown or unsealed publication without workflow provenance')
        receipt = self._rows('query_harvest_receipt', (publication_id,))
        if len(receipt) != 1 or receipt[0]['bundle_version'] != BUNDLE_VERSION:
            raise ValueError('Publication lacks the harvest mart bundle; run a new pipeline workflow')
        row = rows[0]
        return PublicationRef(row['publication_id'], row['scope_id'], row['snapshot_id'],
                              Versions(row['run_id'], row['quality_policy_version'], row['parser_version'],
                                       row['code_version'], row['config_bundle_hash']),
                              row['committed_at'], row['finality_status'])

    def cycle_window(self, context):
        rows = self._rows('query_harvest_cycle', (context.publication_id, context.scope_id,
                                                  context.snapshot_id, context.cycle_id,
                                                  context.compartment_ids[0]))
        if len(rows) != 1:
            raise ValueError('Cycle/compartment is absent from the pinned harvest mart')
        row = rows[0]
        if any(row[key].time() != time.min for key in ('start_source_time', 'end_source_time_exclusive')):
            raise ValueError('BR-05 requires a cycle with whole source dates')
        return CycleWindow(row['cycle_id'], row['compartment_id'], row['start_source_time'].date(),
                           row['end_source_time_exclusive'].date())

    def read_harvest_records(self, context, publication):
        context.matches(publication)
        rows = self._rows('query_harvest_records', (context.publication_id, context.scope_id,
                                                    context.snapshot_id, context.cycle_id,
                                                    context.compartment_ids[0], context.start, context.end))
        records = {}
        for row in rows:
            if row['source_unit'] != HARVEST_SOURCE_UNIT:
                raise ValueError('Unexpected harvest unit: expected kg/m²/harvest')
            status = BasisStatus(row['basis_status']) if row['basis_status'] else BasisStatus.UNKNOWN
            measure = MeasureValue(row['value'], HARVEST_UNIT, QualityStatus(row['quality']),
                                   Applicability(row['applicability_status']), row['reason_code'],
                                   HARVEST_SOURCE_UNIT, status, row['basis_version_id'], row['area_m2'],
                                   row['basis_kind'], row['assumption_id'], row['assumption_version'],
                                   ASSUMPTION_LABEL if status == BasisStatus.ASSUMED else
                                   'verified' if status == BasisStatus.VERIFIED else None)
            slot = records.setdefault(row['logical_record_key'],
                                      {'revision_id': row['revision_id'], 'when': row['harvest_date'],
                                       'correction_id': row['correction_id'],
                                       'correction_version': row['correction_version'],
                                       'measures': {}})
            if slot['measures'] and row['measure_code'] in slot['measures']:
                raise ValueError('Duplicate harvest record/measure grain or join fanout')
            if row['correction_id'] != slot['correction_id'] or (
                    row['correction_version'] != slot['correction_version']):
                raise ValueError('Record measure rows disagree on frozen correction identity')
            slot['measures'][row['measure_code']] = measure
        result = []
        for key, slot in sorted(records.items(), key=lambda item: (item[1]['when'], item[0])):
            if set(slot['measures']) != {'ProdA', 'ProdB'}:
                raise ValueError('Published harvest record is missing measure quality/unit metadata')
            paired = slot['measures']['ProdA'].accepted and slot['measures']['ProdB'].accepted
            record = HarvestRecord(
                publication=publication, cycle_id=context.cycle_id,
                compartment_id=context.compartment_ids[0], harvest_date=slot['when'],
                prod_a=slot['measures']['ProdA'], prod_b=slot['measures']['ProdB'],
                revision_id=slot['revision_id'], logical_record_key=key, paired=paired,
                correction_id=slot['correction_id'], correction_version=slot['correction_version'])
            result.append(record)
        return tuple(result)

    def harvest_state(self, context):
        rows = self._rows('query_harvest_state', (context.publication_id, context.snapshot_id,
                                                  context.cycle_id, context.compartment_ids[0]))
        if len(rows) != 1:
            raise ValueError('Harvest completeness state is absent from the pinned publication')
        row = rows[0]
        details = row['details'] or {}
        items = tuple(HarvestUnresolvedIssue(
            source_record_id=item['source_record_id'], disposition=item['disposition'],
            reason_code=item['reason_code'], issue_column=item.get('issue_column'),
            relative_path=item.get('relative_path'), line_number=item.get('line_number'),
            source_time_serial=None if item.get('source_time_serial') is None
            else Decimal(str(item['source_time_serial'])))
            for item in (row['unresolved'] or []))
        return HarvestCompleteness(
            snapshot_complete=row['snapshot_complete'], source_resolved=row['source_resolved'],
            expected_files=row['expected_files'], files_present=row['files_present'],
            files_loaded=row['files_loaded'], files_failed=row['files_failed'],
            records_read=row['records_read'], records_accepted=row['records_accepted'],
            records_quarantine=row['records_quarantine'], records_duplicate=row['records_duplicate'],
            records_conflict=row['records_conflict'], unresolved_records=row['unresolved_records'],
            conflicted_records=row['conflicted_records'], accepted_records=row['accepted_records'],
            paired_records=row['paired_records'], prod_a_usable_records=row['prod_a_usable_records'],
            prod_b_usable_records=row['prod_b_usable_records'], event_a_dates=row['event_a_dates'],
            event_b_dates=row['event_b_dates'], reasons=tuple(row['reasons']),
            expected_relative_path=details.get('expected_relative_path'),
            unresolved_items=items)
