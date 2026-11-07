-- One set-based statement: drain dimensions/slots/receipt for one pinned
-- publication inside the coordinator publish transaction. Mirrors
-- materialize_climate exactly (dims first, fact slots with pinned membership
-- and the frozen per-measure quality of the policy's DRAIN measures, receipt
-- last; any failure rolls back with the publication). Slot grain: every drain
-- record_revision of the pinned membership becomes ONE mart slot (its
-- observed_at is the [t,t+5) slot start; the exact end-of-cycle boundary
-- sample stays frozen in the mart and adds nothing downstream). Values freeze
-- ec_drain/ph_drain in the exact 'drain_process_computer' context; the
-- per-measure quality freeze jsonb holds the triplets of EVERY drain measure
-- defined by the pinned quality policy (status/applicability/reason).
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
 INSERT INTO mart.dim_drain_cycle
 SELECT %s AS publication_id,data_scope_id,cycle_id,compartment_id,compartment_name,
 source_folder,start_source_time,end_source_time_exclusive FROM cycles RETURNING cycle_id
), slots AS MATERIALIZED (
 SELECT p.publication_id,p.data_scope_id,r.cycle_id,c.compartment_id,r.revision_id,
 r.record_type,r.logical_record_key,p.quality_policy_version,r.correction_id,
 cor.version AS correction_version,drain.observed_at AS slot_start,
 drain.ec_drain,drain.ph_drain,
 (SELECT jsonb_object_agg(q.measure_code, jsonb_build_object(
   'status',q.status,'applicability',q.applicability_status,'reason',q.reason_code)
  ORDER BY q.measure_code)
  FROM core.measure_quality q
  JOIN config.measure_definition md ON md.measure_code=q.measure_code AND md.record_type='drain'
  WHERE q.revision_id=r.revision_id AND q.policy_version_id=p.quality_policy_version) AS measure_quality
 FROM started CROSS JOIN meta.publication p JOIN meta.publication_member pm USING(publication_id,data_scope_id)
 JOIN core.record_revision r ON r.revision_id=pm.revision_id
 JOIN core.drain_observation drain ON drain.revision_id=r.revision_id
 JOIN core.growing_cycle c ON c.cycle_id=r.cycle_id
 LEFT JOIN meta.correction cor ON cor.correction_id=r.correction_id
 WHERE p.publication_id=%s AND pm.record_type='drain'
), fact_insert AS (
 INSERT INTO mart.fact_drain_slot
 SELECT publication_id,data_scope_id,cycle_id,compartment_id,revision_id,record_type,
 logical_record_key,quality_policy_version,correction_id,correction_version,slot_start,
 ec_drain,ph_drain,measure_quality
 FROM slots RETURNING 1
), measures AS MATERIALIZED (
 -- Count from the in-flight `slots` CTE: later CTEs read the statement's
 -- snapshot, which cannot see this same statement's fact inserts, so any
 -- count taken from the target table here would be zero (same main T15
 -- preflight). `slots` is exactly the inserted row set.
 SELECT cy.cycle_id, count(f.revision_id)::bigint AS slot_count
 FROM cycles cy LEFT JOIN slots f
 ON f.cycle_id=cy.cycle_id AND f.compartment_id=cy.compartment_id
 GROUP BY cy.cycle_id
)
INSERT INTO mart.drain_publication
SELECT %s,%s,(SELECT coalesce(sum(slot_count),0) FROM measures),
 (SELECT count(*) FROM cycle_insert),(SELECT at FROM started),clock_timestamp()
FROM started CROSS JOIN (SELECT count(*) AS dependency FROM fact_insert) d
RETURNING slot_count,cycle_count,extract(epoch FROM finished_at-started_at) AS seconds
