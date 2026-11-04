SELECT bundle_version, slot_count, cycle_count
FROM mart.drain_publication
WHERE publication_id=%s
