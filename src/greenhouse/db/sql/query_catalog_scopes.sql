SELECT ds.data_scope_id AS scope_id,ds.snapshot_id,p.publication_id,p.committed_at,
 p.finality_status,w.versions->>'code' AS code_version,
 r.bundle_version,r.day_count,r.cycle_count
FROM meta.current_publication cp
JOIN meta.publication p ON p.publication_id=cp.publication_id AND p.data_scope_id=cp.data_scope_id
JOIN meta.data_scope ds ON ds.data_scope_id=cp.data_scope_id
JOIN meta.publication_seal s ON s.publication_id=p.publication_id
JOIN meta.workflow_run w ON w.workflow_id=s.workflow_id
LEFT JOIN mart.irrigation_publication r ON r.publication_id=p.publication_id
ORDER BY p.committed_at DESC,ds.data_scope_id
