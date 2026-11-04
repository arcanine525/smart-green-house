WITH started AS MATERIALIZED (SELECT clock_timestamp() AS at),
cycles AS MATERIALIZED (
 SELECT DISTINCT p.publication_id,p.data_scope_id,c.cycle_id,c.compartment_id,comp.name AS compartment_name,
 c.start_source_time,c.end_source_time_exclusive
 FROM started CROSS JOIN meta.publication p JOIN meta.data_scope ds USING(data_scope_id)
 JOIN meta.source_folder_mapping m ON m.snapshot_id=ds.snapshot_id
 JOIN core.growing_cycle c ON c.cycle_id=m.cycle_id
 JOIN core.compartment comp ON comp.compartment_id=c.compartment_id WHERE p.publication_id=%s
), cycle_insert AS (
 INSERT INTO mart.dim_irrigation_cycle SELECT * FROM cycles RETURNING cycle_id
), date_insert AS (
 INSERT INTO mart.dim_date(resource_date)
 SELECT DISTINCT generate_series(c.start_source_time,c.end_source_time_exclusive-interval '1 day',interval '1 day')::date
 FROM cycles c ON CONFLICT(resource_date) DO NOTHING RETURNING resource_date
), source AS MATERIALIZED (
 SELECT p.publication_id,p.data_scope_id,r.cycle_id,c.compartment_id,d.resource_date,r.revision_id,
 r.record_type,r.logical_record_key,p.quality_policy_version,r.correction_id,cor.version AS correction_version,
 d.irrigation_l_m2,d.drain_l_m2,mi.unit AS irrigation_unit,md.unit AS drain_unit,
 qi.status AS irrigation_quality,qi.applicability_status AS irrigation_applicability,qi.reason_code AS irrigation_reason,
 qd.status AS drain_quality,qd.applicability_status AS drain_applicability,qd.reason_code AS drain_reason,
 bi.basis_version_id AS irrigation_basis_id,bi.area_m2 AS irrigation_area_m2,bi.basis_kind AS irrigation_basis_kind,
 bi.verification_status AS irrigation_basis_status,bi.assumption_id AS irrigation_assumption_id,bi.assumption_version AS irrigation_assumption_version,
 bd.basis_version_id AS drain_basis_id,bd.area_m2 AS drain_area_m2,bd.basis_kind AS drain_basis_kind,
 bd.verification_status AS drain_basis_status,bd.assumption_id AS drain_assumption_id,bd.assumption_version AS drain_assumption_version
 FROM started CROSS JOIN meta.publication p JOIN meta.publication_member pm USING(publication_id,data_scope_id)
 JOIN core.record_revision r ON r.revision_id=pm.revision_id
 JOIN core.resource_day d ON d.revision_id=r.revision_id
 JOIN core.growing_cycle c ON c.cycle_id=r.cycle_id
 JOIN config.measure_definition mi ON mi.measure_code='Irr' AND mi.record_type='resource'
 JOIN config.measure_definition md ON md.measure_code='Drain' AND md.record_type='resource'
 JOIN core.measure_quality qi ON qi.revision_id=r.revision_id AND qi.measure_code='Irr' AND qi.policy_version_id=p.quality_policy_version
 JOIN core.measure_quality qd ON qd.revision_id=r.revision_id AND qd.measure_code='Drain' AND qd.policy_version_id=p.quality_policy_version
 LEFT JOIN meta.correction cor ON cor.correction_id=r.correction_id
 LEFT JOIN meta.publication_basis pi ON pi.publication_id=p.publication_id AND pi.data_scope_id=p.data_scope_id
 AND pi.compartment_id=c.compartment_id AND pi.measure_code='Irr' AND pi.analysis_context='shared_analysis'
 LEFT JOIN config.area_basis_version bi ON bi.basis_version_id=pi.basis_version_id
 AND d.resource_date>=bi.valid_from AND d.resource_date<bi.valid_to_exclusive
 LEFT JOIN meta.publication_basis pd ON pd.publication_id=p.publication_id AND pd.data_scope_id=p.data_scope_id
 AND pd.compartment_id=c.compartment_id AND pd.measure_code='Drain' AND pd.analysis_context='shared_analysis'
 LEFT JOIN config.area_basis_version bd ON bd.basis_version_id=pd.basis_version_id
 AND d.resource_date>=bd.valid_from AND d.resource_date<bd.valid_to_exclusive
 WHERE p.publication_id=%s AND pm.record_type='resource'
), flags AS (
 SELECT s.*,
 (irrigation_quality='accepted' AND irrigation_applicability='applicable' AND irrigation_l_m2 IS NOT NULL) AS irrigation_accepted,
 (drain_quality='accepted' AND drain_applicability='applicable' AND drain_l_m2 IS NOT NULL) AS drain_accepted,
 coalesce(irrigation_basis_status IN ('verified','assumed_unverified') AND drain_basis_status=irrigation_basis_status
 AND irrigation_area_m2=drain_area_m2 AND irrigation_basis_kind=drain_basis_kind
 AND (irrigation_basis_status='verified' OR (irrigation_assumption_id=drain_assumption_id
 AND irrigation_assumption_version=drain_assumption_version)),false) AS compatible
 FROM source s
), pairs AS (
 SELECT f.*,(irrigation_accepted AND drain_accepted AND compatible) AS paired FROM flags f
), fact_insert AS (
 INSERT INTO mart.fact_irrigation_day
 SELECT publication_id,data_scope_id,cycle_id,compartment_id,resource_date,revision_id,record_type,logical_record_key,
 quality_policy_version,correction_id,correction_version,irrigation_l_m2,drain_l_m2,
 CASE WHEN irrigation_unit='L/m²/day' AND drain_unit='L/m²/day' THEN irrigation_unit ELSE 'unsupported_unit' END,
 irrigation_quality,irrigation_applicability,irrigation_reason,drain_quality,drain_applicability,drain_reason,
 irrigation_basis_id,irrigation_area_m2,irrigation_basis_kind,irrigation_basis_status,irrigation_assumption_id,irrigation_assumption_version,
 drain_basis_id,drain_area_m2,drain_basis_kind,drain_basis_status,drain_assumption_id,drain_assumption_version,
 irrigation_accepted,drain_accepted,paired,
 CASE WHEN paired AND irrigation_l_m2>0 THEN mart.water_percent(drain_l_m2,irrigation_l_m2) END,
 CASE WHEN paired THEN drain_l_m2>irrigation_l_m2 END,
 ARRAY(SELECT DISTINCT reason FROM unnest(ARRAY[irrigation_reason,drain_reason,
 CASE WHEN NOT compatible THEN 'incompatible_or_unknown_basis' END,
 CASE WHEN paired AND irrigation_l_m2=0 THEN 'no_irrigation_quantity' END,
 CASE WHEN paired AND drain_l_m2>irrigation_l_m2 THEN 'drain_exceeds_irrigation' END]) reason WHERE reason IS NOT NULL ORDER BY reason)
 FROM pairs RETURNING publication_id
)
INSERT INTO mart.irrigation_publication
SELECT %s,%s,(SELECT count(*) FROM fact_insert),(SELECT count(*) FROM cycle_insert),started.at,clock_timestamp()
FROM started CROSS JOIN (SELECT count(*) FROM date_insert) dependency
RETURNING day_count,cycle_count,extract(epoch FROM finished_at-started_at) AS seconds
