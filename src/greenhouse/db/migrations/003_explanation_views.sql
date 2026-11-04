CREATE VIEW core.measure_value AS
 SELECT c.revision_id,v.measure_code,v.value FROM core.climate_observation c
 CROSS JOIN LATERAL(VALUES ('Tair',c.tair),('CO2air',c.co2air),('t_heat_sp',c.t_heat_sp),
 ('t_vent_sp',c.t_vent_sp),('co2_sp',c.co2_sp),('t_heat_vip',c.t_heat_vip),
 ('t_ventlee_vip',c.t_ventlee_vip),('co2_vip',c.co2_vip)) v(measure_code,value)
 UNION ALL SELECT d.revision_id,v.measure_code,v.value FROM core.drain_observation d
 CROSS JOIN LATERAL(VALUES ('EC_drain_PC',d.ec_drain),('pH_drain_PC',d.ph_drain)) v(measure_code,value)
 UNION ALL SELECT r.revision_id,v.measure_code,v.value FROM core.resource_day r
 CROSS JOIN LATERAL(VALUES ('Heat_cons',r.heat_mj_m2),('ElecHigh',r.elec_high_kwh_m2),
 ('ElecLow',r.elec_low_kwh_m2),('CO2_cons',r.co2_kg_m2),('Irr',r.irrigation_l_m2),('Drain',r.drain_l_m2)) v(measure_code,value)
 UNION ALL SELECT h.revision_id,v.measure_code,v.value FROM core.harvest h
 CROSS JOIN LATERAL(VALUES ('ProdA',h.prod_a_kg_m2),('ProdB',h.prod_b_kg_m2)) v(measure_code,value);

CREATE VIEW config.selected_basis AS
 SELECT b.*,a.analysis_context,
 (a.enabled AND (b.verification_status='verified' OR coalesce(s.enabled,false))) AS applied,
 CASE WHEN b.verification_status='assumed_unverified' THEN 'giả định chưa xác minh' ELSE 'verified' END AS basis_label
 FROM config.basis_application a JOIN config.area_basis_version b USING(basis_version_id,data_scope_id,compartment_id,measure_code)
 LEFT JOIN config.assumption_application s ON s.data_scope_id=b.data_scope_id
 AND s.assumption_id=b.assumption_id AND s.assumption_version=b.assumption_version;

-- Base candidate value/lineage; no mutable basis selection in this view.
CREATE VIEW core.explain_value AS
 SELECT r.revision_id,r.data_scope_id,r.cycle_id,c.compartment_id,r.run_id,r.record_type,r.logical_record_key,
 r.observed_at,r.is_close_boundary,
 (r.observed_at>=c.start_source_time AND r.observed_at<c.end_source_time_exclusive) AS in_cycle,
 extract(isoyear FROM r.observed_at)::integer AS iso_year,extract(week FROM r.observed_at)::integer AS iso_week,
 v.measure_code,v.value,m.unit,q.status AS quality_status,q.reason_code,q.applicability_status,q.policy_version_id,
 p.is_demo AS quality_is_demo,
 CASE WHEN q.status='accepted' AND q.applicability_status='applicable'
 AND r.observed_at<c.end_source_time_exclusive THEN v.value END AS usable_value,
 s.source_time_serial,s.raw_time_token,s.source_timestamp,s.time_precision,s.time_policy_version,
 s.raw_reference,s.raw_hash,f.relative_path,f.checksum AS file_checksum,s.line_number,s.line_end,s.snapshot_id
 FROM core.record_revision r JOIN core.growing_cycle c USING(cycle_id)
 JOIN core.measure_value v USING(revision_id) JOIN config.measure_definition m USING(measure_code)
 JOIN core.measure_quality q USING(revision_id,measure_code)
 JOIN config.quality_policy_version p ON p.policy_version_id=q.policy_version_id
 JOIN meta.source_record s USING(source_record_id) JOIN meta.source_file f USING(source_file_id);
-- Diagnostic only: current explicit basis selection, not an app publication.
CREATE VIEW core.explain_measure AS SELECT e.*,
 b.basis_version_id,b.area_m2,b.verification_status AS basis_status,b.assumption_id AS basis_assumption_id,
 b.assumption_version AS basis_assumption_version,
 CASE WHEN b.verification_status='assumed_unverified' THEN 'giả định chưa xác minh'
 WHEN b.verification_status='verified' THEN 'verified' END AS basis_label,b.evidence_reference AS basis_evidence,
 CASE WHEN e.measure_code IN ('Irr','Drain','Heat_cons','ElecHigh','ElecLow','ProdA','ProdB') AND b.basis_version_id IS NULL
 THEN 'incompatible_or_unknown_basis' END AS basis_reason
 FROM core.explain_value e LEFT JOIN config.selected_basis b
 ON b.data_scope_id=e.data_scope_id AND b.compartment_id=e.compartment_id
 AND b.measure_code=e.measure_code AND b.analysis_context='shared_analysis' AND b.applied
 AND e.observed_at::date>=b.valid_from AND e.observed_at::date<b.valid_to_exclusive;

-- App path: pointer, membership, quality policy and pinned basis from ONE publication.
CREATE VIEW core.published_measure AS SELECT e.*,cp.publication_id,
 b.basis_version_id,b.area_m2,b.verification_status AS basis_status,b.assumption_id AS basis_assumption_id,
 b.assumption_version AS basis_assumption_version,
 CASE WHEN b.verification_status='assumed_unverified' THEN 'giả định chưa xác minh'
 WHEN b.verification_status='verified' THEN 'verified' END AS basis_label,b.evidence_reference AS basis_evidence,
 CASE WHEN e.measure_code IN ('Irr','Drain','Heat_cons','ElecHigh','ElecLow','ProdA','ProdB') AND b.basis_version_id IS NULL
 THEN 'incompatible_or_unknown_basis' END AS basis_reason
 FROM meta.current_publication cp
 JOIN meta.publication p ON p.publication_id=cp.publication_id AND p.data_scope_id=cp.data_scope_id
 JOIN meta.publication_member pm ON pm.publication_id=p.publication_id AND pm.data_scope_id=p.data_scope_id
 JOIN core.explain_value e ON e.revision_id=pm.revision_id AND e.data_scope_id=pm.data_scope_id
 AND e.policy_version_id=p.quality_policy_version
 LEFT JOIN meta.publication_basis pb ON pb.publication_id=p.publication_id AND pb.data_scope_id=p.data_scope_id
 AND pb.compartment_id=e.compartment_id AND pb.measure_code=e.measure_code AND pb.analysis_context='shared_analysis'
 LEFT JOIN config.area_basis_version b ON b.basis_version_id=pb.basis_version_id
 AND e.observed_at::date>=b.valid_from AND e.observed_at::date<b.valid_to_exclusive;

CREATE VIEW meta.harvest_ingestion_state AS
 SELECT pr.run_id,pr.data_scope_id,m.compartment_id,fi.ingestion_status,fi.core_status,
 fi.accepted,fi.quarantine,fi.conflict,
 (fi.core_status='loaded' AND fi.quarantine=0 AND fi.conflict=0) AS source_rows_resolved,
 'T12 must additionally check per-measure quality and published snapshot completeness'::text AS limitation
 FROM meta.file_ingestion fi JOIN meta.pipeline_run pr USING(run_id)
 JOIN meta.source_file f USING(source_file_id)
 JOIN meta.source_folder_mapping m ON m.snapshot_id=f.snapshot_id AND m.source_folder=f.source_folder
 WHERE f.relative_path LIKE '%%/Production.csv';
