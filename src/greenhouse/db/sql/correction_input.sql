INSERT INTO core_input
SELECT s.source_record_id,s.source_file_id,s.snapshot_id,r.run_id,r.data_scope_id,r.cycle_id,
 CASE r.record_type WHEN 'climate' THEN 'GreenhouseClimate' WHEN 'drain' THEN 'GreenhouseClimate'
 WHEN 'resource' THEN 'Resources' ELSE 'Production' END,
 s.line_number,s.line_end,s.raw_reference,s.raw_hash,s.source_time_serial,s.raw_time_token,s.source_timestamp,
 s.slot_residual_seconds,s.adapter_id,s.adapter_version,s.time_policy_version,r.observed_at,r.is_close_boundary,
 r.logical_record_key,'accepted',%s,%s,'[]'::jsonb
FROM core.record_revision r JOIN meta.source_record s USING(source_record_id) WHERE r.revision_id=%s
