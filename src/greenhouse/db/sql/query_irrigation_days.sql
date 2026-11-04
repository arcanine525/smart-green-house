SELECT r.revision_id,r.logical_record_key,r.cycle_id,c.compartment_id,d.resource_date,
 r.correction_id,cor.version AS correction_version,v.measure_code,v.value,m.unit AS source_unit,
 q.status AS quality,q.applicability_status,q.reason_code,
 b.basis_version_id,b.area_m2,b.basis_kind,b.verification_status AS basis_status,
 b.assumption_id,b.assumption_version
FROM meta.publication_member pm
JOIN meta.publication p ON p.publication_id=pm.publication_id AND p.data_scope_id=pm.data_scope_id
JOIN core.record_revision r ON r.revision_id=pm.revision_id AND r.data_scope_id=pm.data_scope_id
JOIN core.resource_day d ON d.revision_id=r.revision_id
JOIN core.growing_cycle c ON c.cycle_id=r.cycle_id
CROSS JOIN LATERAL (VALUES ('Irr',d.irrigation_l_m2),('Drain',d.drain_l_m2)) v(measure_code,value)
JOIN config.measure_definition m ON m.measure_code=v.measure_code AND m.record_type='resource'
JOIN core.measure_quality q ON q.revision_id=r.revision_id AND q.measure_code=v.measure_code AND q.policy_version_id=p.quality_policy_version
LEFT JOIN meta.correction cor ON cor.correction_id=r.correction_id
LEFT JOIN meta.publication_basis pb ON pb.publication_id=p.publication_id AND pb.data_scope_id=p.data_scope_id
 AND pb.compartment_id=c.compartment_id AND pb.measure_code=v.measure_code AND pb.analysis_context='shared_analysis'
LEFT JOIN config.area_basis_version b ON b.basis_version_id=pb.basis_version_id
 AND d.resource_date>=b.valid_from AND d.resource_date<b.valid_to_exclusive
WHERE pm.publication_id=%s AND pm.data_scope_id=%s AND r.snapshot_id=%s
 AND pm.record_type='resource' AND r.cycle_id=%s AND c.compartment_id=%s
 AND d.resource_date>=%s AND d.resource_date<%s
ORDER BY d.resource_date,v.measure_code
