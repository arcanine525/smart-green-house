-- Drain publication bundle for BR-03 (T16): additive ONLY. Applies after the
-- T15-approved 009 without touching it. The mandatory drain bundle freezes
-- EVERY drain slot of a publication: values (EC_drain, pH_drain in
-- dS/m / pH from the process-computer columns of GreenhouseClimate.csv),
-- per-measure quality/applicability/reason for the policy's drain measures
-- (EC_drain_PC, pH_drain_PC; EC negative -> invalid and pH 0 -> suspect come
-- from the SAME loader policy rows, never re-derived), and the source
-- revision/correction identity. measurement_context keeps the exact core
-- identity 'drain_process_computer' — no rename to a generic drain label.
-- Runtime rule evaluation reads ONLY this bundle for the pinned publication
-- — no mutable current core fallback. Existing water/harvest/climate bundles
-- and historical publications stay untouched.
--
-- Slot identity includes the cycle: several compartments share the same
-- timestamps, so (publication_id, slot_start) is NOT unique by itself.

CREATE TABLE mart.dim_drain_cycle (
 publication_id text NOT NULL, data_scope_id text NOT NULL,
 cycle_id text NOT NULL REFERENCES core.growing_cycle,
 compartment_id text NOT NULL REFERENCES core.compartment,
 compartment_name text NOT NULL, source_folder text NOT NULL,
 start_source_time timestamp NOT NULL, end_source_time_exclusive timestamp NOT NULL,
 PRIMARY KEY(publication_id,cycle_id), UNIQUE(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id)
);
CREATE TRIGGER sealed_drain_cycles BEFORE INSERT OR UPDATE OR DELETE ON mart.dim_drain_cycle
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

CREATE TABLE mart.fact_drain_slot (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL,
 compartment_id text NOT NULL, revision_id text NOT NULL,
 record_type text NOT NULL CHECK(record_type='drain'),
 logical_record_key text NOT NULL,
 quality_policy_version text NOT NULL REFERENCES config.quality_policy_version,
 correction_id text REFERENCES meta.correction, correction_version integer,
 slot_start timestamp NOT NULL,
 ec_drain numeric NULL, ph_drain numeric NULL,
 measure_quality jsonb NOT NULL CHECK(measure_quality::text <> '{}'),
 PRIMARY KEY(publication_id,revision_id),
 UNIQUE(publication_id,cycle_id,slot_start),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 FOREIGN KEY(publication_id,cycle_id,compartment_id) REFERENCES mart.dim_drain_cycle(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,record_type,logical_record_key) REFERENCES meta.publication_member(publication_id,record_type,logical_record_key),
 FOREIGN KEY(publication_id,revision_id) REFERENCES meta.publication_member(publication_id,revision_id),
 FOREIGN KEY(revision_id,data_scope_id,record_type,logical_record_key)
  REFERENCES core.record_revision(revision_id,data_scope_id,record_type,logical_record_key),
 CHECK((correction_id IS NULL AND correction_version IS NULL) OR (correction_id IS NOT NULL AND correction_version>0))
);
CREATE TRIGGER sealed_drain_slots BEFORE INSERT OR UPDATE OR DELETE ON mart.fact_drain_slot
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();
CREATE INDEX drain_slot_lookup
 ON mart.fact_drain_slot(publication_id, cycle_id, compartment_id, slot_start);

CREATE TABLE mart.drain_publication (
 publication_id text PRIMARY KEY REFERENCES meta.publication,
 bundle_version text NOT NULL, slot_count bigint NOT NULL CHECK(slot_count>=0),
 cycle_count integer NOT NULL CHECK(cycle_count>=0),
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL
);
CREATE TRIGGER sealed_drain_receipts BEFORE INSERT OR UPDATE OR DELETE ON mart.drain_publication
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

-- Current pipeline code (v5-drain) requires ALL FOUR bundles in the same
-- seal transaction; the 009 trigger keeps guarding v4 seals exactly as
-- approved, and older seals keep representing legacy shapes.
CREATE FUNCTION mart.require_drain_bundle() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE code text;
BEGIN
 SELECT w.versions->>'code' INTO code FROM meta.workflow_run w WHERE w.workflow_id=NEW.workflow_id;
 IF code='pipeline-v5-drain' THEN
  IF NOT EXISTS(SELECT 1 FROM mart.irrigation_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'water mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM mart.harvest_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'harvest mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM mart.climate_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'climate mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM mart.drain_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'drain mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER required_drain_bundle BEFORE INSERT ON meta.publication_seal
 FOR EACH ROW EXECUTE FUNCTION mart.require_drain_bundle();
