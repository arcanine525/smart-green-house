ANALYZE core_input;
INSERT INTO meta.source_record
SELECT source_record_id,source_file_id,snapshot_id,run_id,line_number,line_end,raw_reference,raw_hash,
 source_time_serial,raw_time_token,source_timestamp,slot_residual_seconds,adapter_id,adapter_version,
 time_policy_version,'staging_truncated_us' FROM core_input;

INSERT INTO meta.quarantine_record
SELECT md5(run_id||':'||source_record_id),source_record_id,run_id,disposition,
 coalesce(issues->0->>'code','duplicate_source_key'),
 coalesce(issues->0->>'policy_version','agc-parser-v1'),issues,NULL
 FROM core_input WHERE disposition<>'accepted';
INSERT INTO meta.dq_issue
SELECT md5(i.run_id||':'||i.source_record_id||':'||e.ordinality::text),i.run_id,i.source_record_id,NULL,NULL,
 e.issue->>'code',coalesce(e.issue->>'policy_version','agc-parser-v1'),i.raw_reference
 FROM core_input i CROSS JOIN LATERAL jsonb_array_elements(i.issues) WITH ORDINALITY e(issue,ordinality)
 WHERE i.disposition<>'accepted';

INSERT INTO core_batch_revision
SELECT md5(i.data_scope_id||':'||i.source_record_id||':'||t.record_type||':1') AS revision_id,
 i.data_scope_id,i.snapshot_id,i.cycle_id,i.run_id,i.source_record_id,t.record_type,
 i.cycle_id||':'||CASE WHEN t.record_type='harvest' THEN i.source_record_id ELSE i.logical_key END AS logical_record_key,
 1 AS revision_number,i.raw_hash AS content_hash,i.observed_at,i.is_close_boundary,NULL::text AS correction_id
 FROM core_input i CROSS JOIN LATERAL (
 SELECT 'climate'::text WHERE i.kind='GreenhouseClimate'
 UNION ALL SELECT 'drain' WHERE i.kind='GreenhouseClimate'
 UNION ALL SELECT 'resource' WHERE i.kind='Resources'
 UNION ALL SELECT 'harvest' WHERE i.kind='Production'
 ) t(record_type) WHERE i.disposition='accepted' OR (i.disposition='quarantine' AND i.logical_key IS NOT NULL
 AND jsonb_array_length(i.issues)>0 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(i.issues) e WHERE e->>'code'<>'invalid_numeric'));

ANALYZE core_batch_revision;
INSERT INTO core.record_revision SELECT * FROM core_batch_revision;

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
 q.verdict->>'status',q.verdict->>'applicability',q.verdict->>'reason'
 FROM core_input i JOIN core_batch_revision r USING(source_record_id)
 JOIN meta.pipeline_run pr ON pr.run_id=r.run_id
 JOIN config.quality_policy_version p ON p.policy_version_id=pr.quality_policy_version
 JOIN config.measure_definition m ON m.record_type=r.record_type
 CROSS JOIN LATERAL (SELECT m.measure_code,(i.vals->>m.measure_code)::numeric AS value) v
 CROSS JOIN LATERAL (SELECT sensor_stream.measure_verdict(p.definition,i.issues,i.null_reasons,v.measure_code,v.value) AS verdict) q
 WHERE r.run_id=(SELECT run_id FROM core_input LIMIT 1);

INSERT INTO meta.dq_issue
 SELECT md5(q.revision_id||':'||q.measure_code||':'||q.policy_version_id),r.run_id,r.source_record_id,
 r.revision_id,q.measure_code,q.reason_code,q.policy_version_id,p.evidence_reference
 FROM core_batch_revision r JOIN core.measure_quality q USING(revision_id)
 JOIN core_input i ON i.source_record_id=r.source_record_id
 JOIN config.quality_policy_version p USING(policy_version_id)
 WHERE r.run_id=(SELECT run_id FROM core_input LIMIT 1) AND q.reason_code IS NOT NULL;
TRUNCATE core_batch_revision, core_input;
