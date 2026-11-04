-- Additive harvest mart + snapshot completeness for pinned publications.
-- Mirrors migration 005 shapes: immutable sealed children, per-record grain,
-- per-measure quality/basis provenance, and a receipt identified by bundle.
-- Never edit applied migrations; v2 behavior of migration 005 stays intact.

CREATE TABLE mart.dim_harvest_cycle (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL REFERENCES core.growing_cycle,
 compartment_id text NOT NULL REFERENCES core.compartment, compartment_name text NOT NULL,
 start_source_time timestamp NOT NULL, end_source_time_exclusive timestamp NOT NULL,
 PRIMARY KEY(publication_id,cycle_id), UNIQUE(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 CHECK(start_source_time<end_source_time_exclusive AND start_source_time::time='00:00'
  AND end_source_time_exclusive::time='00:00')
);
CREATE TRIGGER sealed_harvest_cycles BEFORE INSERT OR UPDATE OR DELETE ON mart.dim_harvest_cycle
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

CREATE TABLE mart.fact_harvest_record (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL,
 compartment_id text NOT NULL, revision_id text NOT NULL,
 record_type text NOT NULL DEFAULT 'harvest' CHECK(record_type='harvest'),
 logical_record_key text NOT NULL, harvest_date date NOT NULL REFERENCES mart.dim_date,
 quality_policy_version text NOT NULL REFERENCES config.quality_policy_version,
 correction_id text REFERENCES meta.correction, correction_version integer,
 prod_a_kg_m2 numeric, prod_b_kg_m2 numeric, source_unit text NOT NULL CHECK(source_unit='kg/m²/harvest'),
 prod_a_quality text NOT NULL CHECK(prod_a_quality IN ('accepted','missing','invalid','suspect')),
 prod_a_applicability text NOT NULL CHECK(prod_a_applicability IN ('applicable','unknown')), prod_a_reason text,
 prod_b_quality text NOT NULL CHECK(prod_b_quality IN ('accepted','missing','invalid','suspect')),
 prod_b_applicability text NOT NULL CHECK(prod_b_applicability IN ('applicable','unknown')), prod_b_reason text,
 prod_a_basis_id text REFERENCES config.area_basis_version, prod_a_area_m2 numeric, prod_a_basis_kind text,
 prod_a_basis_status text, prod_a_assumption_id text, prod_a_assumption_version integer,
 prod_b_basis_id text REFERENCES config.area_basis_version, prod_b_area_m2 numeric, prod_b_basis_kind text,
 prod_b_basis_status text, prod_b_assumption_id text, prod_b_assumption_version integer,
 prod_a_accepted boolean NOT NULL, prod_b_accepted boolean NOT NULL, paired boolean NOT NULL,
 PRIMARY KEY(publication_id,logical_record_key),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 FOREIGN KEY(publication_id,cycle_id,compartment_id) REFERENCES mart.dim_harvest_cycle(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,record_type,logical_record_key) REFERENCES meta.publication_member(publication_id,record_type,logical_record_key),
 FOREIGN KEY(publication_id,revision_id) REFERENCES meta.publication_member(publication_id,revision_id),
 FOREIGN KEY(revision_id,data_scope_id,record_type,logical_record_key)
  REFERENCES core.record_revision(revision_id,data_scope_id,record_type,logical_record_key),
 CHECK((correction_id IS NULL AND correction_version IS NULL) OR (correction_id IS NOT NULL AND correction_version>0)),
 CHECK(NOT paired OR (prod_a_accepted AND prod_b_accepted))
);
CREATE TRIGGER sealed_harvest_records BEFORE INSERT OR UPDATE OR DELETE ON mart.fact_harvest_record
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

-- Completeness per cycle: canonical harvest_snapshot_complete is TRUE only
-- when source resolution holds (expected file present/parsed, no unresolved
-- quarantine/conflict rows) AND every accepted record carries BOTH required
-- measures accepted. source_resolved keeps the structural half separately but
-- never stands in for snapshot_complete. Unresolved item identity/line/reason
-- is frozen for drilldown; values stay with raw (no invented amounts here),
-- record-event coverage is never a time-coverage claim (BR-05 §3.7–3.8).
CREATE TABLE mart.harvest_cycle_state (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL,
 compartment_id text NOT NULL REFERENCES core.compartment, compartment_name text NOT NULL,
 start_source_time timestamp NOT NULL, end_source_time_exclusive timestamp NOT NULL,
 snapshot_complete boolean NOT NULL, source_resolved boolean NOT NULL,
 expected_files bigint NOT NULL CHECK(expected_files>=0), files_present bigint NOT NULL CHECK(files_present>=0),
 files_loaded bigint NOT NULL CHECK(files_loaded>=0), files_failed bigint NOT NULL CHECK(files_failed>=0),
 records_read bigint CHECK(records_read>=0), records_accepted bigint CHECK(records_accepted>=0),
 records_quarantine bigint CHECK(records_quarantine>=0), records_duplicate bigint CHECK(records_duplicate>=0),
 records_conflict bigint CHECK(records_conflict>=0),
 unresolved_records bigint NOT NULL CHECK(unresolved_records>=0),
 conflicted_records bigint NOT NULL CHECK(conflicted_records>=0),
 accepted_records bigint NOT NULL CHECK(accepted_records>=0),
 paired_records bigint NOT NULL CHECK(paired_records>=0),
 prod_a_usable_records bigint NOT NULL CHECK(prod_a_usable_records>=0),
 prod_b_usable_records bigint NOT NULL CHECK(prod_b_usable_records>=0),
 event_a_dates bigint NOT NULL CHECK(event_a_dates>=0),
 event_b_dates bigint NOT NULL CHECK(event_b_dates>=0),
 unresolved jsonb NOT NULL, reasons text[] NOT NULL, details jsonb NOT NULL,
 PRIMARY KEY(publication_id,cycle_id),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 FOREIGN KEY(publication_id,cycle_id,compartment_id) REFERENCES mart.dim_harvest_cycle(publication_id,cycle_id,compartment_id)
);
CREATE TRIGGER sealed_harvest_states BEFORE INSERT OR UPDATE OR DELETE ON mart.harvest_cycle_state
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

CREATE TABLE mart.harvest_publication (
 publication_id text PRIMARY KEY REFERENCES meta.publication,
 bundle_version text NOT NULL, record_count bigint NOT NULL CHECK(record_count>=0),
 cycle_count bigint NOT NULL CHECK(cycle_count>=0),
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL
);
CREATE TRIGGER sealed_harvest_receipts BEFORE INSERT OR UPDATE OR DELETE ON mart.harvest_publication
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

-- Version-aware seal requirements (complements the v2 rule in migration 005,
-- which stays untouched): current pipeline code requires BOTH bundles in the
-- same seal transaction; older code seals keep representing legacy shapes.
CREATE FUNCTION mart.require_bundles() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE code text;
BEGIN
 SELECT w.versions->>'code' INTO code FROM meta.workflow_run w WHERE w.workflow_id=NEW.workflow_id;
 IF code='pipeline-v3-harvest' THEN
  IF NOT EXISTS(SELECT 1 FROM mart.irrigation_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'water mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM mart.harvest_publication WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'harvest mart bundle required before publication seal' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER required_mart_bundles BEFORE INSERT ON meta.publication_seal
 FOR EACH ROW EXECUTE FUNCTION mart.require_bundles();

CREATE FUNCTION mart.require_harvest_bundle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM meta.workflow_run w WHERE w.workflow_id=NEW.workflow_id
 AND w.versions->>'code'='pipeline-v3-harvest')
 AND NOT EXISTS(SELECT 1 FROM mart.harvest_publication WHERE publication_id=NEW.publication_id) THEN
  RAISE EXCEPTION 'harvest bundle required before publication seal' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER required_harvest_bundle BEFORE INSERT ON meta.publication_seal
 FOR EACH ROW EXECUTE FUNCTION mart.require_harvest_bundle();
