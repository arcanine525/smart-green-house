SELECT ds.data_scope_id AS scope_id,ds.snapshot_id,p.publication_id,p.committed_at,
 p.finality_status,w.versions->>'code' AS code_version,
 r.bundle_version,r.day_count,r.cycle_count,
 h.bundle_version AS harvest_bundle_version,
 CASE WHEN h.publication_id IS NULL THEN ARRAY['missing_expected_harvest_bundle']::text[]
 ELSE ARRAY[]::text[] END AS harvest_reason_codes,
 c.bundle_version AS climate_bundle_version,
 CASE WHEN c.publication_id IS NULL THEN ARRAY['missing_expected_climate_bundle']::text[]
 ELSE ARRAY[]::text[] END AS climate_reason_codes,
 d.bundle_version AS drain_bundle_version
FROM meta.current_publication cp
JOIN meta.publication p ON p.publication_id=cp.publication_id AND p.data_scope_id=cp.data_scope_id
JOIN meta.data_scope ds ON ds.data_scope_id=cp.data_scope_id
JOIN meta.publication_seal s ON s.publication_id=p.publication_id
JOIN meta.workflow_run w ON w.workflow_id=s.workflow_id
LEFT JOIN mart.irrigation_publication r ON r.publication_id=p.publication_id
LEFT JOIN mart.harvest_publication h ON h.publication_id=p.publication_id
LEFT JOIN mart.climate_publication c ON c.publication_id=p.publication_id
LEFT JOIN mart.drain_publication d ON d.publication_id=p.publication_id
ORDER BY p.committed_at DESC,ds.data_scope_id
