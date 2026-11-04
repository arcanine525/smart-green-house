"""Read-only provenance verification from any revision to physical raw bytes."""
from pathlib import Path
from psycopg.rows import dict_row
from greenhouse.db.loader import digest_file, safe_child
from greenhouse.db.migrate import sql
from greenhouse.ingestion.parser import Source, SourceParser


def lineage(conn, revision_id, raw_snapshot):
    with conn.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(sql('lineage'), (revision_id,)).fetchone()
    if not row:
        raise ValueError('Unknown revision')
    raw = Path(raw_snapshot).resolve()
    path = safe_child(raw, row['relative_path'])
    if digest_file(path) != row['file_checksum']:
        raise ValueError('Raw file checksum mismatch')
    source = Source(row['snapshot_id'], row['archive_sha256'], row['relative_path'], row['file_checksum'], row['first_run_id'])
    for record in SourceParser(path, source).records():
        if record['line_number'] == row['line_number']:
            if any(record[k] != row[k] for k in ('source_record_id','line_end','raw_hash')):
                raise ValueError('Raw record hash/identity mismatch')
            return dict(row) | {'raw_hash_verified': True}
    raise ValueError('Raw physical record not found')
