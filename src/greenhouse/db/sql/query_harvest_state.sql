SELECT s.snapshot_complete,s.source_resolved,s.expected_files,s.files_present,s.files_loaded,s.files_failed,
 s.records_read,s.records_accepted,s.records_quarantine,s.records_duplicate,s.records_conflict,
 s.unresolved_records,s.conflicted_records,s.accepted_records,s.paired_records,
 s.prod_a_usable_records,s.prod_b_usable_records,s.event_a_dates,s.event_b_dates,s.reasons,
 s.details,s.unresolved
FROM mart.harvest_cycle_state s JOIN meta.data_scope ds USING(data_scope_id)
WHERE s.publication_id=%s AND ds.snapshot_id=%s AND s.cycle_id=%s AND s.compartment_id=%s
