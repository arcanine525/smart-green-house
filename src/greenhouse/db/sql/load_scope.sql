SELECT ds.snapshot_id,s.archive_checksum FROM meta.data_scope ds JOIN meta.dataset_snapshot s USING(snapshot_id)
WHERE ds.data_scope_id=%s
