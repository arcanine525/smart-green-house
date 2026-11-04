INSERT INTO core.climate_observation(revision_id,observed_at,tair,co2air,t_heat_sp,t_vent_sp,co2_sp,t_heat_vip,t_ventlee_vip,co2_vip)
 SELECT r.revision_id,r.observed_at,(i.vals->>'Tair')::numeric,(i.vals->>'CO2air')::numeric,
 (i.vals->>'t_heat_sp')::numeric,(i.vals->>'t_vent_sp')::numeric,(i.vals->>'co2_sp')::numeric,
 (i.vals->>'t_heat_vip')::numeric,(i.vals->>'t_ventlee_vip')::numeric,(i.vals->>'co2_vip')::numeric
 FROM core_batch_revision r JOIN core_input i USING(source_record_id)
 WHERE r.run_id=(SELECT run_id FROM core_input LIMIT 1) AND r.record_type='climate';
INSERT INTO core.drain_observation(revision_id,observed_at,ec_drain,ph_drain)
 SELECT r.revision_id,r.observed_at,(i.vals->>'EC_drain_PC')::numeric,(i.vals->>'pH_drain_PC')::numeric
 FROM core_batch_revision r JOIN core_input i USING(source_record_id)
 WHERE r.run_id=(SELECT run_id FROM core_input LIMIT 1) AND r.record_type='drain';
INSERT INTO core.resource_day(revision_id,observed_at,heat_mj_m2,elec_high_kwh_m2,elec_low_kwh_m2,co2_kg_m2,irrigation_l_m2,drain_l_m2)
 SELECT r.revision_id,r.observed_at,(i.vals->>'Heat_cons')::numeric,(i.vals->>'ElecHigh')::numeric,
 (i.vals->>'ElecLow')::numeric,(i.vals->>'CO2_cons')::numeric,(i.vals->>'Irr')::numeric,(i.vals->>'Drain')::numeric
 FROM core_batch_revision r JOIN core_input i USING(source_record_id)
 WHERE r.run_id=(SELECT run_id FROM core_input LIMIT 1) AND r.record_type='resource';
INSERT INTO core.harvest(revision_id,observed_at,prod_a_kg_m2,prod_b_kg_m2)
 SELECT r.revision_id,r.observed_at,(i.vals->>'ProdA')::numeric,(i.vals->>'ProdB')::numeric
 FROM core_batch_revision r JOIN core_input i USING(source_record_id)
 WHERE r.run_id=(SELECT run_id FROM core_input LIMIT 1) AND r.record_type='harvest';


INSERT INTO core.measure_quality
 SELECT r.revision_id,r.record_type,v.measure_code,p.policy_version_id,
 CASE WHEN EXISTS(SELECT 1 FROM jsonb_array_elements(i.issues) e WHERE e->>'code'='invalid_numeric' AND e->>'column'=v.measure_code) THEN 'invalid'
      WHEN v.value IS NULL THEN 'missing'
      WHEN p.definition->'nonnegative' ? v.measure_code AND v.value<0 THEN 'invalid'
      WHEN p.definition->'suspect_zero' ? v.measure_code AND v.value=0 THEN 'suspect'
      ELSE 'accepted' END,
 CASE WHEN v.value IS NULL OR (p.definition->'off_mode_zero' ? v.measure_code AND v.value=0)
 THEN 'unknown' ELSE 'applicable' END,
 CASE WHEN EXISTS(SELECT 1 FROM jsonb_array_elements(i.issues) e WHERE e->>'code'='invalid_numeric' AND e->>'column'=v.measure_code) THEN 'invalid_numeric'
      WHEN v.value IS NULL THEN coalesce(i.null_reasons->>v.measure_code,'missing_source_value')
      WHEN p.definition->'nonnegative' ? v.measure_code AND v.value<0 THEN 'negative_quantity'
      WHEN p.definition->'suspect_zero' ? v.measure_code AND v.value=0 THEN 'demo_zero_ph_suspect'
      WHEN p.definition->'off_mode_zero' ? v.measure_code AND v.value=0 THEN 'off_mode_unverified'
      ELSE NULL END
 FROM core_input i JOIN core_batch_revision r USING(source_record_id)
 JOIN meta.pipeline_run pr ON pr.run_id=r.run_id
 JOIN config.quality_policy_version p ON p.policy_version_id=pr.quality_policy_version
 JOIN config.measure_definition m ON m.record_type=r.record_type
 CROSS JOIN LATERAL (SELECT m.measure_code,(i.vals->>m.measure_code)::numeric AS value) v
 WHERE r.run_id=(SELECT run_id FROM core_input LIMIT 1);

INSERT INTO meta.dq_issue
 SELECT md5(q.revision_id||':'||q.measure_code||':'||q.policy_version_id),r.run_id,r.source_record_id,
 r.revision_id,q.measure_code,q.reason_code,q.policy_version_id,p.evidence_reference
 FROM core_batch_revision r JOIN core.measure_quality q USING(revision_id)
 JOIN core_input i ON i.source_record_id=r.source_record_id
 JOIN config.quality_policy_version p USING(policy_version_id)
 WHERE r.run_id=(SELECT run_id FROM core_input LIMIT 1) AND q.reason_code IS NOT NULL;
