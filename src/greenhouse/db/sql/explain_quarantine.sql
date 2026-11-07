SELECT q.disposition,q.reason_code,q.policy_version,s.raw_reference,s.source_time_serial,
 s.source_timestamp,f.relative_path,fi.ingestion_status,fi.core_status
FROM meta.quarantine_record q JOIN meta.source_record s USING(source_record_id)
JOIN meta.source_file f USING(source_file_id)
JOIN meta.pipeline_run pr ON pr.run_id=q.run_id
JOIN meta.source_folder_mapping m ON m.snapshot_id=f.snapshot_id AND m.source_folder=f.source_folder
JOIN meta.file_ingestion fi ON fi.run_id=q.run_id AND fi.source_file_id=f.source_file_id
WHERE pr.data_scope_id=%s AND m.compartment_id=%s ORDER BY s.raw_reference LIMIT %s
