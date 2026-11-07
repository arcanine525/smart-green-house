-- Read only pinned membership, own quality and effective pinned area.
SELECT r.cycle_id,v.component,sum(CASE WHEN v.component='heat'
 THEN mart.ratio_sig(v.value*b.area_m2,3.6,50) ELSE v.value*b.area_m2 END) AS kwh
FROM meta.publication p JOIN meta.publication_member pm USING(publication_id,data_scope_id)
JOIN core.record_revision r USING(revision_id)
JOIN core.resource_day d USING(revision_id)
JOIN core.growing_cycle c ON c.cycle_id=r.cycle_id
CROSS JOIN LATERAL (VALUES ('heat','Heat_cons',d.heat_mj_m2),
 ('elec_high','ElecHigh',d.elec_high_kwh_m2),('elec_low','ElecLow',d.elec_low_kwh_m2)) v(component,measure,value)
JOIN core.measure_quality q ON q.revision_id=r.revision_id AND q.measure_code=v.measure
 AND q.policy_version_id=p.quality_policy_version
JOIN meta.publication_basis pb ON pb.publication_id=p.publication_id AND pb.compartment_id=c.compartment_id
 AND pb.measure_code=v.measure AND pb.analysis_context='shared_analysis'
JOIN config.area_basis_version b ON b.basis_version_id=pb.basis_version_id
 AND d.resource_date>=b.valid_from AND d.resource_date<b.valid_to_exclusive
WHERE p.publication_id=%(pub)s AND pm.record_type='resource'
 AND d.resource_date>=c.start_source_time AND d.resource_date<c.end_source_time_exclusive
 AND q.status='accepted' AND q.applicability_status='applicable'
GROUP BY r.cycle_id,v.component ORDER BY r.cycle_id,v.component
