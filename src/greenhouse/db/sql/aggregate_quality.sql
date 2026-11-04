SELECT q.measure_code,q.status,q.applicability_status,q.reason_code,count(*) AS records
FROM core.measure_quality q JOIN core.record_revision r USING(revision_id)
WHERE r.run_id=%s GROUP BY q.measure_code,q.status,q.applicability_status,q.reason_code
ORDER BY q.measure_code,q.status,q.applicability_status,q.reason_code
