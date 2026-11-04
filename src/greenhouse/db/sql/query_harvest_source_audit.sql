SELECT sr.source_record_id,sr.snapshot_id,sr.first_run_id,f.relative_path,f.checksum AS file_checksum,
 d.archive_checksum AS archive_sha256,sr.line_number,sr.line_end,sr.raw_hash,sr.source_time_serial
FROM meta.source_record sr
JOIN meta.source_file f ON f.source_file_id=sr.source_file_id
JOIN meta.dataset_snapshot d ON d.snapshot_id=sr.snapshot_id
WHERE sr.source_record_id=%s AND sr.snapshot_id=%s
