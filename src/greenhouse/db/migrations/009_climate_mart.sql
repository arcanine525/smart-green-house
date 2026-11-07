-- Climate publication bundle for BR-01 (T15): additive ONLY. Applies after
-- the T14-approved 008 without touching it. The mandatory climate bundle
-- freezes EVERY climate slot of a publication: values (actuals + requested
-- and realized setpoints), per-measure quality/applicability/reason for the
-- policy's climate measures (jsonb, frozen at materialization: Tair,
-- CO2Air and the six SP/VIP measures on the AGC policy), and the source
-- revision/correction identity for lineage. Runtime rule evaluation reads
-- ONLY this bundle for the pinned publication — no mutable current
-- raw/core fallback (main T15 decision). Existing water/harvest bundles and
-- historical publications stay untouched.
--
-- One clock of truth: bundle_version on the receipt; the seal requirement
-- trigger demands this bundle (plus water and harvest) for the current
-- pipeline code, mirroring the 005/006 guards. Slot identity includes the
-- cycle: several compartments share the same timestamps, so
-- (publication_id, slot_start) is NOT unique by itself.

CREATE TABLE mart.dim_climate_cycle (
 publication_id text NOT NULL, data_scope_id text NOT NULL,
 cycle_id text NOT NULL REFERENCES core.growing_cycle,
 compartment_id text NOT NULL REFERENCES core.compartment,
 compartment_name text NOT NULL, source_folder text NOT NULL,
 start_source_time timestamp NOT NULL, end_source_time_exclusive timestamp NOT NULL,
 PRIMARY KEY(publication_id,cycle_id), UNIQUE(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id)
);
CREATE TRIGGER sealed_climate_cycles BEFORE INSERT OR UPDATE OR DELETE ON mart.dim_climate_cycle
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

CREATE TABLE mart.fact_climate_slot (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL,
 compartment_id text NOT NULL, revision_id text NOT NULL,
 record_type text NOT NULL CHECK(record_type='climate'),
 logical_record_key text NOT NULL,
 quality_policy_version text NOT NULL REFERENCES config.quality_policy_version,
 correction_id text REFERENCES meta.correction, correction_version integer,
 slot_start timestamp NOT NULL,
 tair numeric NULL, co2air numeric NULL,
 t_heat_sp numeric NULL, t_vent_sp numeric NULL, co2_sp numeric NULL,
 t_heat_vip numeric NULL, t_ventlee_vip numeric NULL, co2_vip numeric NULL,
 measure_quality jsonb NOT NULL CHECK(measure_quality::text <> '{}'),
 PRIMARY KEY(publication_id,revision_id),
 UNIQUE(publication_id,cycle_id,slot_start),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 FOREIGN KEY(publication_id,cycle_id,compartment_id) REFERENCES mart.dim_climate_cycle(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,record_type,logical_record_key) REFERENCES meta.publication_member(publication_id,record_type,logical_record_key),
 FOREIGN KEY(publication_id,revision_id) REFERENCES meta.publication_member(publication_id,revision_id),
 FOREIGN KEY(revision_id,data_scope_id,record_type,logical_record_key)
  REFERENCES core.record_revision(revision_id,data_scope_id,record_type,logical_record_key),
 CHECK((correction_id IS NULL AND correction_version IS NULL) OR (correction_id IS NOT NULL AND correction_version>0))
);
CREATE TRIGGER sealed_climate_slots BEFORE INSERT OR UPDATE OR DELETE ON mart.fact_climate_slot
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();
CREATE INDEX climate_slot_lookup
 ON mart.fact_climate_slot(publication_id, cycle_id, compartment_id, slot_start);

CREATE TABLE mart.climate_publication (
 publication_id text PRIMARY KEY REFERENCES meta.publication,
 bundle_version text NOT NULL, slot_count bigint NOT NULL CHECK(slot_count>=0),
 cycle_count integer NOT NULL CHECK(cycle_count>=0),
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL
);
CREATE TRIGGER sealed_climate_receipts BEFORE INSERT OR UPDATE OR DELETE ON mart.climate_publication
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

-- Current pipeline code requires the climate bundle (and keeps the two
-- legacy bundle requirements) in the same seal transaction. Older seals
-- keep representing legacy shapes.
CREATE FUNCTION mart.require_climate_bundle() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE code text;
BEGIN
 SELECT w.versions->>'code' INTO code FROM meta.workflow_run w WHERE w.workflow_id=NEW.workflow_id;
 IF code='pipeline-v4-climate' THEN
  IF NOT EXISTS(SELECT 1 FROM mart.irrigation_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'water mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM mart.harvest_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'harvest mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM mart.climate_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'climate mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER required_climate_bundle BEFORE INSERT ON meta.publication_seal
 FOR EACH ROW EXECUTE FUNCTION mart.require_climate_bundle();
