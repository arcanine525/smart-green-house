SELECT r.cycle_id,sum(h.prod_a_kg_m2*b.area_m2) AS a_kg
FROM meta.publication p JOIN meta.publication_member pm USING(publication_id,data_scope_id)
JOIN core.record_revision r USING(revision_id)
JOIN core.harvest h USING(revision_id)
JOIN core.growing_cycle c ON c.cycle_id=r.cycle_id
JOIN core.measure_quality q ON q.revision_id=r.revision_id AND q.measure_code='ProdA'
 AND q.policy_version_id=p.quality_policy_version
JOIN meta.publication_basis pb ON pb.publication_id=p.publication_id AND pb.compartment_id=c.compartment_id
 AND pb.measure_code='ProdA' AND pb.analysis_context='shared_analysis'
JOIN config.area_basis_version b ON b.basis_version_id=pb.basis_version_id
 AND h.harvest_date>=b.valid_from AND h.harvest_date<b.valid_to_exclusive
WHERE p.publication_id=%(pub)s AND pm.record_type='harvest'
 AND h.harvest_date>=c.start_source_time AND h.harvest_date<c.end_source_time_exclusive
 AND q.status='accepted' AND q.applicability_status='applicable'
GROUP BY r.cycle_id ORDER BY r.cycle_id
