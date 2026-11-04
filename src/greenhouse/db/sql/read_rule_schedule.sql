SELECT r.rule_version, r.rule_id, r.version, r.data_scope_id, r.compartment_id,
       r.evidence_reference, r.is_demo, r.label,
       s.metric, s.measurement_context, s.basis, s.source_start, s.source_end,
       s.delta, s.lower, s.upper
FROM config.rule_schedule_revision r
LEFT JOIN config.rule_schedule_segment s USING (rule_version)
WHERE r.rule_version = %s
ORDER BY s.basis, s.measurement_context, s.metric, s.source_start, s.source_end, s.delta, s.lower, s.upper
