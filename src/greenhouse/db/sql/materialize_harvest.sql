-- One set-based statement: harvest dimensions/facts/completeness state and
-- receipt for one pinned publication inside the coordinator publish
-- transaction. Mirrors materialize_irrigation: dims first, facts with pinned
-- membership/quality/basis, per-cycle state, receipt last; any failure rolls
-- back with the publication. Record grain preserved (several records per date
-- stay distinct); basis mirrors the water mart (shared_analysis pins).
-- Completeness: harvest_snapshot_complete = source_resolved (expected file
-- present+loaded with consumable outputs, no unresolved quarantine/conflict
-- rows) AND accepted_records = paired_records, so a record missing B keeps A
-- totals but marks the snapshot incomplete (BR-05 §3.7/§3.8). Unresolved
-- truth is keyed on the SOURCE record identity + snapshot (not the ingestion
-- run): audit issues dedupe by (source, issue code/column) across runs and
-- stay unresolved unless THIS publication's pinned revision/quality for that
-- source actually fixes the issue — only invalid-numeric harvest measures
-- (ProdA/ProdB) are correctable in core; out-of-cycle, conflicts and every
-- other issue class keep blocking, and unpublished later corrections are
-- never consulted. Old publications keep their own frozen state. Record
-- counters count actual record identity, so a missing/empty expected file
-- contributes zero records, never a phantom row. Quarantined values stay
-- with raw (no invented amounts here).
WITH started AS MATERIALIZED (
 SELECT clock_timestamp() AS at
), cycles AS MATERIALIZED (
 SELECT DISTINCT p.publication_id,p.data_scope_id,ds.snapshot_id,c.cycle_id,c.compartment_id,
 comp.name AS compartment_name,m.source_folder,c.start_source_time,c.end_source_time_exclusive
 FROM started CROSS JOIN meta.publication p JOIN meta.data_scope ds USING(data_scope_id)
 JOIN meta.source_folder_mapping m ON m.snapshot_id=ds.snapshot_id
 JOIN core.growing_cycle c ON c.cycle_id=m.cycle_id
 JOIN core.compartment comp ON comp.compartment_id=c.compartment_id WHERE p.publication_id=%s
), cycle_insert AS (
 INSERT INTO mart.dim_harvest_cycle
 SELECT %s AS publication_id,data_scope_id,cycle_id,compartment_id,compartment_name,
 start_source_time,end_source_time_exclusive FROM cycles RETURNING cycle_id
), source AS MATERIALIZED (
 SELECT p.publication_id,p.data_scope_id,r.cycle_id,c.compartment_id,r.source_record_id,h.harvest_date,
 r.revision_id,r.record_type,r.logical_record_key,p.quality_policy_version,r.correction_id,
 cor.version AS correction_version,
 h.prod_a_kg_m2,h.prod_b_kg_m2,ma.unit AS prod_a_unit,mb.unit AS prod_b_unit,
 qa.status AS prod_a_quality,qa.applicability_status AS prod_a_applicability,qa.reason_code AS prod_a_reason,
 qb.status AS prod_b_quality,qb.applicability_status AS prod_b_applicability,qb.reason_code AS prod_b_reason,
 bi.basis_version_id AS prod_a_basis_id,bi.area_m2 AS prod_a_area_m2,bi.basis_kind AS prod_a_basis_kind,
 bi.verification_status AS prod_a_basis_status,bi.assumption_id AS prod_a_assumption_id,bi.assumption_version AS prod_a_assumption_version,
 bj.basis_version_id AS prod_b_basis_id,bj.area_m2 AS prod_b_area_m2,bj.basis_kind AS prod_b_basis_kind,
 bj.verification_status AS prod_b_basis_status,bj.assumption_id AS prod_b_assumption_id,bj.assumption_version AS prod_b_assumption_version
 FROM started CROSS JOIN meta.publication p JOIN meta.publication_member pm USING(publication_id,data_scope_id)
 JOIN core.record_revision r ON r.revision_id=pm.revision_id
 JOIN core.harvest h ON h.revision_id=r.revision_id
 JOIN core.growing_cycle c ON c.cycle_id=r.cycle_id
 JOIN config.measure_definition ma ON ma.measure_code='ProdA' AND ma.record_type='harvest'
 JOIN config.measure_definition mb ON mb.measure_code='ProdB' AND mb.record_type='harvest'
 JOIN core.measure_quality qa ON qa.revision_id=r.revision_id AND qa.measure_code='ProdA'
 AND qa.policy_version_id=p.quality_policy_version
 JOIN core.measure_quality qb ON qb.revision_id=r.revision_id AND qb.measure_code='ProdB'
 AND qb.policy_version_id=p.quality_policy_version
 LEFT JOIN meta.correction cor ON cor.correction_id=r.correction_id
 LEFT JOIN (meta.publication_basis pi JOIN config.area_basis_version bi USING(basis_version_id))
 ON pi.publication_id=p.publication_id AND pi.data_scope_id=p.data_scope_id
 AND pi.compartment_id=c.compartment_id AND pi.measure_code='ProdA' AND pi.analysis_context='shared_analysis'
 AND h.harvest_date>=bi.valid_from AND h.harvest_date<bi.valid_to_exclusive
 LEFT JOIN (meta.publication_basis pj JOIN config.area_basis_version bj USING(basis_version_id))
 ON pj.publication_id=p.publication_id AND pj.data_scope_id=p.data_scope_id
 AND pj.compartment_id=c.compartment_id AND pj.measure_code='ProdB' AND pj.analysis_context='shared_analysis'
 AND h.harvest_date>=bj.valid_from AND h.harvest_date<bj.valid_to_exclusive
 WHERE p.publication_id=%s AND pm.record_type='harvest'
), flags AS (
 SELECT s.*,
 (prod_a_quality='accepted' AND prod_a_applicability='applicable' AND prod_a_kg_m2 IS NOT NULL) AS prod_a_accepted,
 (prod_b_quality='accepted' AND prod_b_applicability='applicable' AND prod_b_kg_m2 IS NOT NULL) AS prod_b_accepted
 FROM source s
), pairs AS (
 SELECT f.*,(prod_a_accepted AND prod_b_accepted) AS paired FROM flags f
), fact_insert AS (
 INSERT INTO mart.fact_harvest_record
 SELECT publication_id,data_scope_id,cycle_id,compartment_id,revision_id,record_type,logical_record_key,harvest_date,
 quality_policy_version,correction_id,correction_version,prod_a_kg_m2,prod_b_kg_m2,
 CASE WHEN prod_a_unit='kg/m²/harvest' AND prod_b_unit='kg/m²/harvest' THEN prod_a_unit ELSE 'unsupported_unit' END,
 prod_a_quality,prod_a_applicability,prod_a_reason,prod_b_quality,prod_b_applicability,prod_b_reason,
 prod_a_basis_id,prod_a_area_m2,prod_a_basis_kind,prod_a_basis_status,prod_a_assumption_id,prod_a_assumption_version,
 prod_b_basis_id,prod_b_area_m2,prod_b_basis_kind,prod_b_basis_status,prod_b_assumption_id,prod_b_assumption_version,
 prod_a_accepted,prod_b_accepted,paired FROM pairs RETURNING 1
), measures AS MATERIALIZED (
 SELECT cy.cycle_id,
  count(f.revision_id)::bigint AS accepted_records,
  count(f.revision_id) FILTER (WHERE f.paired)::bigint AS paired_records,
  count(f.revision_id) FILTER (WHERE f.prod_a_accepted)::bigint AS prod_a_usable_records,
  count(f.revision_id) FILTER (WHERE f.prod_b_accepted)::bigint AS prod_b_usable_records,
  count(DISTINCT f.harvest_date) FILTER (WHERE f.prod_a_accepted)::bigint AS event_a_dates,
  count(DISTINCT f.harvest_date) FILTER (WHERE f.prod_b_accepted)::bigint AS event_b_dates
 FROM cycles cy LEFT JOIN pairs f ON f.cycle_id=cy.cycle_id
  AND f.compartment_id=cy.compartment_id GROUP BY cy.cycle_id
), unresolved AS MATERIALIZED (
 -- One row per (source record, issue code, issue column): the audit may hold
 -- several runs' copies of the SAME raw issue, and DISTINCT reasons of one
 -- source stay distinct; latest run wins for file/line identity. An issue
 -- counts as RESOLVED only when this publication pins a revision for the
 -- source whose exposed harvest quality made the affected measure accepted
 -- (invalid ProdB/ProdA), with values present. Quarantined issues only:
 -- conflicts (disposition conflict) and out-of-cycle/time issues never
 -- clear through measure quality.
 WITH aud AS (
  SELECT DISTINCT ON (sr.source_record_id,i.code,i.col) sr.source_record_id,q.disposition,
   i.code AS issue_code,i.col AS issue_column,f.relative_path,sr.line_number,
   sr.source_time_serial,m2.cycle_id
  FROM meta.quarantine_record q
  JOIN meta.source_record sr USING(source_record_id)
  JOIN meta.source_file f ON f.source_file_id=sr.source_file_id AND f.snapshot_id=sr.snapshot_id
  AND f.relative_path LIKE '%%/Production.csv'
  JOIN meta.source_folder_mapping m2 ON m2.snapshot_id=f.snapshot_id AND m2.source_folder=f.source_folder
  CROSS JOIN LATERAL jsonb_array_elements(q.details) e(i)
  CROSS JOIN LATERAL (SELECT e.i->>'code' AS code,e.i->>'column' AS col) i
  WHERE q.disposition IN ('quarantine','conflict')
   AND sr.snapshot_id=(SELECT snapshot_id FROM cycles LIMIT 1)
   AND m2.cycle_id IN (SELECT cycle_id FROM cycles)
  ORDER BY sr.source_record_id,i.code,i.col,q.run_id DESC
 )
 SELECT a.cycle_id,
  count(DISTINCT a.source_record_id) FILTER (WHERE NOT r.resolved AND a.disposition='quarantine')::bigint AS quarantine_rows,
  count(DISTINCT a.source_record_id) FILTER (WHERE NOT r.resolved AND a.disposition='conflict')::bigint AS conflict_rows,
  coalesce(jsonb_agg(jsonb_build_object(
   'source_record_id',a.source_record_id,'disposition',a.disposition,
   'reason_code',a.issue_code,'issue_column',a.issue_column,
   'relative_path',a.relative_path,'line_number',a.line_number,
   'source_time_serial',a.source_time_serial::text)
   ORDER BY a.relative_path,a.line_number,a.issue_code,a.issue_column
  ) FILTER (WHERE NOT r.resolved),'[]'::jsonb) AS items
 FROM aud a
 LEFT JOIN pairs p ON p.source_record_id=a.source_record_id AND p.cycle_id=a.cycle_id
 CROSS JOIN LATERAL (
  SELECT a.disposition='quarantine'
   AND a.issue_code='invalid_numeric' AND a.issue_column IN ('ProdA','ProdB')
   AND p.revision_id IS NOT NULL
   AND CASE a.issue_column WHEN 'ProdA' THEN p.prod_a_accepted ELSE p.prod_b_accepted END
 ) r(resolved)
 GROUP BY a.cycle_id
), files AS MATERIALIZED (
 SELECT cy.cycle_id,
  (SELECT count(*) FROM meta.source_file sf WHERE sf.snapshot_id=cy.snapshot_id
   AND sf.relative_path=cy.source_folder||'/Production.csv')::bigint AS files_present,
  (SELECT fi.ingestion_status FROM meta.source_file sf2
   LEFT JOIN meta.file_ingestion fi ON fi.source_file_id=sf2.source_file_id
   AND fi.run_id=cy.run_id
   WHERE sf2.snapshot_id=cy.snapshot_id AND sf2.relative_path=cy.source_folder||'/Production.csv') AS ingestion_status,
  (SELECT coalesce(fi.outputs_consumable,true) FROM meta.source_file sf2
   LEFT JOIN meta.file_ingestion fi ON fi.source_file_id=sf2.source_file_id
   AND fi.run_id=cy.run_id
   WHERE sf2.snapshot_id=cy.snapshot_id AND sf2.relative_path=cy.source_folder||'/Production.csv') AS outputs_consumable,
  (SELECT coalesce(max(fi.records_read),0) FROM meta.source_file sf2
   LEFT JOIN meta.file_ingestion fi ON fi.source_file_id=sf2.source_file_id
   AND fi.run_id=cy.run_id
   WHERE sf2.snapshot_id=cy.snapshot_id AND sf2.relative_path=cy.source_folder||'/Production.csv')::bigint AS records_read,
  (SELECT coalesce(max(fi.accepted),0) FROM meta.source_file sf2
   LEFT JOIN meta.file_ingestion fi ON fi.source_file_id=sf2.source_file_id
   AND fi.run_id=cy.run_id
   WHERE sf2.snapshot_id=cy.snapshot_id AND sf2.relative_path=cy.source_folder||'/Production.csv')::bigint AS records_accepted,
  (SELECT coalesce(max(fi.quarantine),0) FROM meta.source_file sf2
   LEFT JOIN meta.file_ingestion fi ON fi.source_file_id=sf2.source_file_id
   AND fi.run_id=cy.run_id
   WHERE sf2.snapshot_id=cy.snapshot_id AND sf2.relative_path=cy.source_folder||'/Production.csv')::bigint AS records_quarantine,
  (SELECT coalesce(max(fi.duplicate),0) FROM meta.source_file sf2
   LEFT JOIN meta.file_ingestion fi ON fi.source_file_id=sf2.source_file_id
   AND fi.run_id=cy.run_id
   WHERE sf2.snapshot_id=cy.snapshot_id AND sf2.relative_path=cy.source_folder||'/Production.csv')::bigint AS records_duplicate,
  (SELECT coalesce(max(fi.conflict),0) FROM meta.source_file sf2
   LEFT JOIN meta.file_ingestion fi ON fi.source_file_id=sf2.source_file_id
   AND fi.run_id=cy.run_id
   WHERE sf2.snapshot_id=cy.snapshot_id AND sf2.relative_path=cy.source_folder||'/Production.csv')::bigint AS records_conflict,
  (cy.source_folder||'/Production.csv') AS expected_path
 FROM (SELECT cy.*,p.run_id FROM cycles cy JOIN meta.publication p USING(publication_id,data_scope_id)
  WHERE p.publication_id=%s) cy
), state AS (
 SELECT %s AS publication_id,cy.data_scope_id,cy.cycle_id,cy.compartment_id,cy.compartment_name,
 cy.start_source_time,cy.end_source_time_exclusive,
 coalesce(m.accepted_records,0) AS accepted_records,coalesce(m.paired_records,0) AS paired_records,
 coalesce(m.prod_a_usable_records,0) AS prod_a_usable_records,coalesce(m.prod_b_usable_records,0) AS prod_b_usable_records,
 coalesce(m.event_a_dates,0) AS event_a_dates,coalesce(m.event_b_dates,0) AS event_b_dates,
 coalesce(u.quarantine_rows,0) AS unresolved_records,coalesce(u.conflict_rows,0) AS conflicted_records,
 coalesce(u.items,'[]'::jsonb) AS unresolved,
 (f.files_present>0 AND coalesce(f.ingestion_status IN ('loaded','loaded_with_quarantine'),false)
  AND coalesce(f.outputs_consumable,true)) AS file_loaded_ok,
 coalesce(f.files_present,0) AS files_present,coalesce(f.ingestion_status,'') AS ingestion_status,
 f.records_read,f.records_accepted,f.records_quarantine,f.records_duplicate,f.records_conflict,
 f.expected_path
 FROM cycles cy
 LEFT JOIN measures m USING(cycle_id) LEFT JOIN unresolved u USING(cycle_id) LEFT JOIN files f USING(cycle_id)
), state_insert AS (
 INSERT INTO mart.harvest_cycle_state
 SELECT s.publication_id,s.data_scope_id,s.cycle_id,s.compartment_id,s.compartment_name,
 s.start_source_time,s.end_source_time_exclusive,
 (s.file_loaded_ok AND unresolved_records=0 AND conflicted_records=0
  AND s.accepted_records=s.paired_records) AS snapshot_complete,
 (s.file_loaded_ok AND unresolved_records=0 AND conflicted_records=0) AS source_resolved,
 1 AS expected_files,s.files_present,
 CASE WHEN s.file_loaded_ok THEN 1 ELSE 0 END AS files_loaded,
 CASE WHEN s.file_loaded_ok THEN 0 ELSE 1 END AS files_failed,
 s.records_read,s.records_accepted,s.records_quarantine,s.records_duplicate,s.records_conflict,
 s.unresolved_records,s.conflicted_records,s.accepted_records,s.paired_records,
 s.prod_a_usable_records,s.prod_b_usable_records,
 s.event_a_dates,s.event_b_dates,s.unresolved,
 ARRAY(SELECT DISTINCT reason FROM unnest(ARRAY[
  CASE WHEN s.files_present=0 THEN 'missing_expected_source_file' END,
  CASE WHEN s.files_present>0 AND NOT s.file_loaded_ok THEN 'ingestion_file_not_loaded' END,
  CASE WHEN s.unresolved_records>0 THEN 'unresolved_quarantine_records' END,
  CASE WHEN s.conflicted_records>0 THEN 'unresolved_conflict_records' END,
  CASE WHEN s.accepted_records<>s.paired_records THEN 'records_without_paired_measures' END]) reason
  WHERE reason IS NOT NULL ORDER BY reason) AS reasons,
 jsonb_build_object('expected_relative_path',s.expected_path,'ingestion_status',
  NULLIF(s.ingestion_status,''),'records_read',s.records_read,'accepted',s.records_accepted,
  'quarantine',s.records_quarantine,'duplicate',s.records_duplicate,'conflict',s.records_conflict,
  'prod_a_usable_records',s.prod_a_usable_records,'prod_b_usable_records',s.prod_b_usable_records,
  'event_a_dates',s.event_a_dates,'event_b_dates',s.event_b_dates) AS details
 FROM state s RETURNING cycle_id
)
INSERT INTO mart.harvest_publication
SELECT %s,%s,(SELECT coalesce(sum(accepted_records),0) FROM measures),
 (SELECT count(*) FROM cycle_insert),(SELECT at FROM started),clock_timestamp()
FROM started CROSS JOIN (SELECT count(*) AS dependency FROM state_insert) d
RETURNING record_count,cycle_count,extract(epoch FROM finished_at-started_at) AS seconds
