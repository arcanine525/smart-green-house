SELECT r.revision_id,r.revision_number,r.correction_id,r.data_scope_id,s.source_record_id,
 s.snapshot_id,s.first_run_id,f.relative_path,f.checksum AS file_checksum,
 d.archive_checksum AS archive_sha256,s.line_number,s.line_end,s.raw_reference,s.raw_hash
FROM core.record_revision r JOIN meta.source_record s USING(source_record_id)
JOIN meta.source_file f USING(source_file_id) JOIN meta.dataset_snapshot d ON d.snapshot_id=s.snapshot_id
WHERE r.revision_id=%s
