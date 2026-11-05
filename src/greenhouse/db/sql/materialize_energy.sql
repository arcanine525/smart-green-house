WITH started AS MATERIALIZED (SELECT clock_timestamp() AS at),
cycles AS MATERIALIZED (
 SELECT DISTINCT p.publication_id,p.data_scope_id,c.cycle_id,c.compartment_id,
  comp.name AS compartment_name,c.start_source_time,c.end_source_time_exclusive
 FROM started CROSS JOIN meta.publication p JOIN meta.data_scope ds USING(data_scope_id)
 JOIN meta.source_folder_mapping m ON m.snapshot_id=ds.snapshot_id
 JOIN core.growing_cycle c ON c.cycle_id=m.cycle_id
 JOIN core.compartment comp ON comp.compartment_id=c.compartment_id WHERE p.publication_id=%s
), cycle_insert AS (
 INSERT INTO mart.dim_energy_cycle SELECT * FROM cycles RETURNING cycle_id
), date_insert AS (
 INSERT INTO mart.dim_date(resource_date)
 SELECT DISTINCT generate_series(start_source_time,end_source_time_exclusive-interval '1 day',interval '1 day')::date
 FROM cycles ON CONFLICT(resource_date) DO NOTHING RETURNING resource_date
), source AS MATERIALIZED (
 SELECT p.publication_id,p.data_scope_id,r.cycle_id,c.compartment_id,d.resource_date,
  v.component,v.measure_code,md.unit AS source_unit,r.revision_id,r.record_type,r.logical_record_key,
  p.quality_policy_version,r.correction_id,cor.version AS correction_version,v.source_value,
  q.status AS quality,q.applicability_status AS applicability,q.reason_code,
  (q.status='accepted' AND q.applicability_status='applicable' AND v.source_value IS NOT NULL) AS accepted,
  b.basis_version_id,b.version AS basis_version,b.area_m2,b.basis_kind,b.verification_status AS basis_status,
  b.valid_from AS basis_valid_from,b.valid_to_exclusive AS basis_valid_to_exclusive,
  b.evidence_reference AS basis_evidence,b.assumption_id,b.assumption_version,
  b.statement AS assumption_statement,b.assumption_evidence
 FROM meta.publication p JOIN meta.publication_member pm USING(publication_id,data_scope_id)
 JOIN core.record_revision r ON r.revision_id=pm.revision_id
 JOIN core.resource_day d ON d.revision_id=r.revision_id
 JOIN core.growing_cycle c ON c.cycle_id=r.cycle_id
 CROSS JOIN LATERAL (VALUES
  ('heat','Heat_cons',d.heat_mj_m2),('elec_high','ElecHigh',d.elec_high_kwh_m2),
  ('elec_low','ElecLow',d.elec_low_kwh_m2)) v(component,measure_code,source_value)
 JOIN config.measure_definition md ON md.measure_code=v.measure_code AND md.record_type='resource'
 JOIN core.measure_quality q ON q.revision_id=r.revision_id AND q.measure_code=v.measure_code
  AND q.policy_version_id=p.quality_policy_version
 LEFT JOIN meta.correction cor ON cor.correction_id=r.correction_id
 LEFT JOIN LATERAL (
  SELECT ab.*,a.statement,a.evidence_reference AS assumption_evidence
  FROM meta.publication_basis pb JOIN config.area_basis_version ab USING(basis_version_id)
  LEFT JOIN config.basis_assumption_version a ON a.assumption_id=ab.assumption_id AND a.version=ab.assumption_version
  WHERE pb.publication_id=p.publication_id AND pb.data_scope_id=p.data_scope_id
   AND pb.compartment_id=c.compartment_id AND pb.measure_code=v.measure_code
   AND pb.analysis_context='shared_analysis'
   AND d.resource_date>=ab.valid_from AND d.resource_date<ab.valid_to_exclusive
 ) b ON true
 WHERE p.publication_id=%s AND pm.record_type='resource'
  AND d.resource_date>=c.start_source_time::date AND d.resource_date<c.end_source_time_exclusive::date
), fact_insert AS (
 INSERT INTO mart.fact_energy_day
 SELECT publication_id,data_scope_id,cycle_id,compartment_id,resource_date,component,measure_code,source_unit,
  revision_id,record_type,logical_record_key,quality_policy_version,correction_id,correction_version,
  source_value,quality,applicability,reason_code,accepted,
  CASE WHEN accepted THEN CASE WHEN component='heat' THEN mart.ratio_sig(source_value,3.6,50) ELSE source_value END END,
  basis_version_id,basis_version,area_m2,basis_kind,basis_status,basis_valid_from,basis_valid_to_exclusive,
  basis_evidence,assumption_id,assumption_version,assumption_statement,assumption_evidence,
  CASE WHEN accepted THEN source_value*area_m2 END,
  CASE WHEN accepted THEN CASE WHEN component='heat' THEN mart.ratio_sig(source_value*area_m2,3.6,50)
   ELSE source_value*area_m2 END END
 FROM source RETURNING 1
)
INSERT INTO mart.energy_publication
SELECT %s,%s,%s,(SELECT count(*) FROM fact_insert),(SELECT count(*) FROM cycle_insert),started.at,clock_timestamp()
FROM started CROSS JOIN (SELECT count(*) FROM date_insert) dependency
RETURNING component_rows,cycle_count,extract(epoch FROM finished_at-started_at)
