"""Read-only provenance verification from any revision or frozen unresolved
source row to physical raw bytes. Never mutates raw or source rows."""
from pathlib import Path
from psycopg.rows import dict_row
from greenhouse.db.loader import digest_file, safe_child
from greenhouse.db.migrate import sql
from greenhouse.ingestion.parser import Source, SourceParser


def _parser_for(raw_snapshot, relative_path, snapshot_id, archive_sha256, file_checksum, run_id):
    """Verified parser for a raw file: safe child path + checksum identity.

    Identity checks fail loudly (checksum drift among them) with distinct
    messages so callers can expose honest unverified states — and no raw
    value — instead of a guess."""
    raw = Path(raw_snapshot).resolve()
    path = safe_child(raw, relative_path)
    if digest_file(path) != file_checksum:
        raise ValueError('Raw file checksum mismatch')
    return SourceParser(path, Source(snapshot_id, archive_sha256, relative_path,
                                     file_checksum, run_id))


def _verify_record(parser, line_number, source_record_id, raw_hash, line_end=None):
    for record in parser.records():
        if record['line_number'] != line_number:
            continue
        if record['source_record_id'] != source_record_id or record['raw_hash'] != raw_hash or (
                line_end is not None and record['line_end'] != line_end):
            raise ValueError('Raw record hash/identity mismatch')
        return record
    raise ValueError('Raw physical record not found')


def lineage(conn, revision_id, raw_snapshot):
    with conn.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(sql('lineage'), (revision_id,)).fetchone()
    if not row:
        raise ValueError('Unknown revision')
    parser = _parser_for(raw_snapshot, row['relative_path'], row['snapshot_id'],
                         row['archive_sha256'], row['file_checksum'], row['first_run_id'])
    _verify_record(parser, row['line_number'], row['source_record_id'], row['raw_hash'],
                   row['line_end'])
    return dict(row) | {'raw_hash_verified': True}


def verify_source_row(raw_snapshot, relative_path, snapshot_id, archive_sha256, file_checksum,
                      run_id, line_number, source_record_id, raw_hash, line_end=None):
    """Verify ONE frozen unresolved source row; returns (headers, record)."""
    parser = _parser_for(raw_snapshot, relative_path, snapshot_id, archive_sha256,
                         file_checksum, run_id)
    headers = tuple(parser.headers)
    return headers, _verify_record(parser, line_number, source_record_id, raw_hash, line_end)
