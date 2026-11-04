WITH daily AS (
 SELECT c.compartment_id,r.cycle_id,v.measure_code,r.observed_at::date AS day,
 count(*) AS observed_count,count(*) FILTER(WHERE v.value IS NULL) AS null_count,
 count(*) FILTER(WHERE q.status='missing') AS missing_count,
 count(*) FILTER(WHERE q.status='invalid') AS invalid_count,
 count(*) FILTER(WHERE q.status='suspect') AS suspect_count,
 count(*) FILTER(WHERE q.status='accepted') AS accepted_count,
 count(*) FILTER(WHERE q.applicability_status='unknown') AS applicability_unknown,
 min(v.value)::text AS min_observed,max(v.value)::text AS max_observed
 FROM meta.workflow_candidate wc JOIN core.record_revision r USING(revision_id)
 JOIN core.growing_cycle c USING(cycle_id) JOIN core.measure_value v USING(revision_id)
 JOIN core.measure_quality q USING(revision_id,measure_code)
 WHERE wc.workflow_id=%s AND q.policy_version_id=%s
 AND r.observed_at>=c.start_source_time AND r.observed_at<c.end_source_time_exclusive
 GROUP BY c.compartment_id,r.cycle_id,v.measure_code,r.observed_at::date
), cycle_types AS (
 SELECT DISTINCT r.cycle_id,r.record_type FROM meta.workflow_candidate wc
 JOIN core.record_revision r USING(revision_id) WHERE wc.workflow_id=%s
), calendar AS (
 SELECT c.cycle_id,c.compartment_id,m.measure_code,m.record_type,
 generate_series(c.start_source_time,c.end_source_time_exclusive-interval '1 day',interval '1 day')::date AS day
 FROM cycle_types r JOIN core.growing_cycle c USING(cycle_id)
 JOIN config.measure_definition m ON m.record_type=r.record_type
)
SELECT cal.*,CASE WHEN cal.record_type IN ('climate','drain') THEN 288
 WHEN cal.record_type='resource' THEN 1 ELSE NULL END AS expected_count,
 coalesce(d.observed_count,0) AS observed_count,coalesce(d.null_count,0) AS null_count,
 coalesce(d.missing_count,0) AS missing_count,coalesce(d.invalid_count,0) AS invalid_count,
 coalesce(d.suspect_count,0) AS suspect_count,coalesce(d.accepted_count,0) AS accepted_count,
 coalesce(d.applicability_unknown,0) AS applicability_unknown,d.min_observed,d.max_observed
 FROM calendar cal LEFT JOIN daily d USING(cycle_id,compartment_id,measure_code,day)
 ORDER BY cal.compartment_id,cal.measure_code,cal.day
