"""Read-only quality inspection for one sealed historical publication."""
from datetime import timedelta
from psycopg.rows import dict_row


def measures(connection, publication_id, cycle_id, start, end):
    """Count quality decisions, not expected observations or coverage."""
    with connection.cursor(row_factory=dict_row) as cursor:
        return cursor.execute('''
            SELECT r.record_type, q.measure_code, q.status, q.applicability_status,
                   q.reason_code, count(*) AS decisions
            FROM meta.publication p
            JOIN meta.publication_member pm ON pm.publication_id = p.publication_id
            JOIN core.record_revision r ON r.revision_id = pm.revision_id
            JOIN core.measure_quality q ON q.revision_id = r.revision_id
                AND q.policy_version_id = p.quality_policy_version
            WHERE p.publication_id = %s AND r.cycle_id = %s
              AND r.observed_at >= %s AND r.observed_at < %s
            GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5
        ''', (publication_id, cycle_id, start, end + timedelta(days=1))).fetchall()


def excluded(connection, publication_id, cycle_id, start, end):
    """Rejected/uncertain measure decisions including source lineage."""
    with connection.cursor(row_factory=dict_row) as cursor:
        return cursor.execute('''
            SELECT r.observed_at, r.record_type, q.measure_code, q.status,
                   q.applicability_status, q.reason_code, r.revision_id,
                   f.relative_path, s.line_number, s.line_end
            FROM meta.publication p
            JOIN meta.publication_member pm ON pm.publication_id = p.publication_id
            JOIN core.record_revision r ON r.revision_id = pm.revision_id
            JOIN core.measure_quality q ON q.revision_id = r.revision_id
                AND q.policy_version_id = p.quality_policy_version
            JOIN meta.source_record s ON s.source_record_id = r.source_record_id
            JOIN meta.source_file f ON f.source_file_id = s.source_file_id
            WHERE p.publication_id = %s AND r.cycle_id = %s
              AND r.observed_at >= %s AND r.observed_at < %s
              AND (q.status <> 'accepted' OR q.applicability_status <> 'applicable')
            ORDER BY r.observed_at, r.revision_id, q.measure_code
        ''', (publication_id, cycle_id, start, end + timedelta(days=1))).fetchall()


def quarantined(connection, publication_id, cycle_id, start, end):
    """Load-run dispositions for the selected cycle; undated rows remain visible."""
    with connection.cursor(row_factory=dict_row) as cursor:
        return cursor.execute('''
            SELECT s.source_timestamp, q.disposition, q.reason_code, q.policy_version,
                   f.relative_path, s.line_number, s.line_end, q.source_record_id
            FROM meta.publication p
            JOIN meta.quarantine_record q ON q.run_id = p.run_id
            JOIN meta.source_record s ON s.source_record_id = q.source_record_id
            JOIN meta.source_file f ON f.source_file_id = s.source_file_id
            JOIN meta.source_folder_mapping m ON m.snapshot_id = f.snapshot_id
                AND m.source_folder = f.source_folder
            WHERE p.publication_id = %s AND m.cycle_id = %s
              AND (s.source_timestamp IS NULL OR
                   (s.source_timestamp >= %s AND s.source_timestamp < %s))
            ORDER BY s.source_timestamp NULLS LAST, q.source_record_id
        ''', (publication_id, cycle_id, start, end + timedelta(days=1))).fetchall()
