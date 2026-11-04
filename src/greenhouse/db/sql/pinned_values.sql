SELECT r.revision_id,r.record_type,r.logical_record_key,v.measure_code,v.value,m.unit,
 q.status,q.applicability_status,q.reason_code,q.policy_version_id
FROM meta.publication_member pm JOIN core.record_revision r USING(revision_id)
JOIN core.measure_value v USING(revision_id) JOIN config.measure_definition m USING(measure_code)
JOIN core.measure_quality q USING(revision_id,measure_code)
JOIN meta.publication p ON p.publication_id=pm.publication_id AND p.quality_policy_version=q.policy_version_id
WHERE pm.publication_id=%s AND v.measure_code=%s ORDER BY r.logical_record_key LIMIT %s
