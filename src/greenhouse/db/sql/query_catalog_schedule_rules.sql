SELECT r.rule_version,r.rule_id,r.version,r.is_demo,r.label,r.evidence_reference,
 r.evidence_reference IS NOT NULL AND r.evidence_reference<>'' AS documented,
 coalesce((SELECT jsonb_agg(DISTINCT jsonb_build_object(
   'metric',s.metric,'measurement_context',s.measurement_context,'basis',s.basis))
   FROM config.rule_schedule_segment s WHERE s.rule_version=r.rule_version),'[]'::jsonb) AS keys,
 (SELECT count(*) FROM config.rule_schedule_segment s WHERE s.rule_version=r.rule_version)::int AS segment_count
FROM config.rule_schedule_revision r
WHERE r.data_scope_id=%s AND r.compartment_id=%s
ORDER BY r.is_demo DESC,r.rule_id,r.version DESC
