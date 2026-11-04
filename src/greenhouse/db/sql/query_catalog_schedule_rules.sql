SELECT r.rule_version,r.rule_id,r.version,r.is_demo,r.label,r.evidence_reference,
 r.evidence_reference IS NOT NULL AND r.evidence_reference<>'' AS documented,
 coalesce((SELECT jsonb_agg(DISTINCT jsonb_build_object(
   'metric',s.metric,'measurement_context',s.measurement_context,'basis',s.basis))
   FROM config.rule_schedule_segment s WHERE s.rule_version=r.rule_version),'[]'::jsonb) AS keys,
 (SELECT count(*) FROM config.rule_schedule_segment s WHERE s.rule_version=r.rule_version)::int AS segment_count
FROM config.rule_schedule_revision r
WHERE r.data_scope_id=%s AND r.compartment_id=%s
 -- OPTIONS MUST BE COMPATIBLE WITH THE DECLARED SERVICE GROUP: every
 -- segment of the revision matches the declared context/metric/basis
 -- group. A revision that only partially matches (mixed EC-drain +
 -- temperature-climate) would be selectable here yet be refused by the
 -- declared service boundary, so it is never offered.
 AND EXISTS (SELECT 1 FROM config.rule_schedule_segment gs
             WHERE gs.rule_version=r.rule_version
              AND gs.measurement_context=%s
              AND gs.metric=ANY(%s) AND gs.basis::text=ANY(%s))
 AND NOT EXISTS (SELECT 1 FROM config.rule_schedule_segment fs
             WHERE fs.rule_version=r.rule_version
              AND NOT (fs.measurement_context=%s
                  AND fs.metric=ANY(%s) AND fs.basis::text=ANY(%s)))
ORDER BY r.is_demo DESC,r.rule_id,r.version DESC
