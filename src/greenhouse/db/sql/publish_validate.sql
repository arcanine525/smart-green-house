WITH candidates AS MATERIALIZED (
 SELECT r.revision_id,r.record_type,r.data_scope_id FROM meta.workflow_candidate w JOIN core.record_revision r USING(revision_id) WHERE w.workflow_id=%s
), qualities AS (
 SELECT q.revision_id,count(*) AS measures FROM candidates c JOIN core.measure_quality q USING(revision_id)
 WHERE q.policy_version_id=%s GROUP BY q.revision_id
), subtypes AS (
 SELECT revision_id,'climate' AS record_type FROM core.climate_observation
 UNION ALL SELECT revision_id,'drain' FROM core.drain_observation
 UNION ALL SELECT revision_id,'resource' FROM core.resource_day
 UNION ALL SELECT revision_id,'harvest' FROM core.harvest
), expected AS (
 SELECT record_type,count(*) AS measures FROM config.measure_definition GROUP BY record_type
)
SELECT count(*) FROM candidates c LEFT JOIN qualities q USING(revision_id)
LEFT JOIN subtypes t ON t.revision_id=c.revision_id AND t.record_type=c.record_type
LEFT JOIN expected e ON e.record_type=c.record_type
WHERE c.data_scope_id<>%s OR t.revision_id IS NULL OR q.measures IS DISTINCT FROM e.measures
