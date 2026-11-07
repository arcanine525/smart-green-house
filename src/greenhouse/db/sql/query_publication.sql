SELECT p.publication_id,p.data_scope_id AS scope_id,ds.snapshot_id,p.run_id,
 p.quality_policy_version,pr.parser_version,w.versions->>'code' AS code_version,
 p.config_bundle_hash,p.committed_at,p.finality_status
FROM meta.publication p JOIN meta.data_scope ds USING(data_scope_id)
JOIN meta.pipeline_run pr ON pr.run_id=p.run_id AND pr.data_scope_id=p.data_scope_id
JOIN meta.publication_seal s USING(publication_id)
JOIN meta.workflow_run w ON w.workflow_id=s.workflow_id
WHERE p.publication_id=%s
