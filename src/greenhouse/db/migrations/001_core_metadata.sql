CREATE SCHEMA core;
CREATE SCHEMA config;

CREATE TABLE core.site (
 site_id text PRIMARY KEY, name text NOT NULL, source_time_basis text NOT NULL,
 evidence_reference text NOT NULL CHECK (length(evidence_reference)>0)
);
CREATE TABLE core.compartment (
 compartment_id text PRIMARY KEY, site_id text NOT NULL REFERENCES core.site,
 name text NOT NULL, evidence_reference text NOT NULL
);
CREATE TABLE core.team (
 team_id text PRIMARY KEY, name text NOT NULL, is_reference boolean NOT NULL,
 evidence_reference text NOT NULL
);
CREATE TABLE core.crop (
 crop_id text PRIMARY KEY, name text NOT NULL, cultivar text,
 evidence_reference text NOT NULL
);
CREATE TABLE core.growing_cycle (
 cycle_id text PRIMARY KEY, compartment_id text NOT NULL REFERENCES core.compartment,
 team_id text NOT NULL REFERENCES core.team, crop_id text NOT NULL REFERENCES core.crop,
 start_source_time timestamp NOT NULL, end_source_time_exclusive timestamp NOT NULL,
 evidence_reference text NOT NULL,
 CHECK (start_source_time < end_source_time_exclusive), UNIQUE(cycle_id,compartment_id,team_id)
);
CREATE TABLE meta.dataset_snapshot (
 snapshot_id text PRIMARY KEY, archive_checksum text NOT NULL CHECK(archive_checksum ~ '^[a-f0-9]{64}$'),
 source_url text NOT NULL, evidence_reference text NOT NULL
);
CREATE TABLE meta.data_scope (
 data_scope_id text PRIMARY KEY, snapshot_id text NOT NULL REFERENCES meta.dataset_snapshot,
 mode text NOT NULL CHECK(mode IN ('historical','replay','fault')),
 replay_id text, evidence_reference text NOT NULL,
 CHECK((mode='historical' AND replay_id IS NULL) OR (mode<>'historical' AND replay_id IS NOT NULL)),
 UNIQUE(data_scope_id,snapshot_id)
);
CREATE TABLE meta.source_folder_mapping (
 snapshot_id text NOT NULL REFERENCES meta.dataset_snapshot, source_folder text NOT NULL,
 compartment_id text NOT NULL, team_id text NOT NULL, cycle_id text NOT NULL,
 evidence_reference text NOT NULL, PRIMARY KEY(snapshot_id,source_folder),
 FOREIGN KEY(cycle_id,compartment_id,team_id) REFERENCES core.growing_cycle(cycle_id,compartment_id,team_id),
 UNIQUE(snapshot_id,source_folder,cycle_id)
);
CREATE TABLE config.measure_definition (
 measure_code text PRIMARY KEY, record_type text NOT NULL CHECK(record_type IN ('climate','drain','resource','harvest')),
 unit text NOT NULL, evidence_reference text NOT NULL, UNIQUE(measure_code,record_type)
);
CREATE TABLE config.quality_policy_version (
 policy_version_id text PRIMARY KEY, version integer NOT NULL CHECK(version>0),
 valid_from timestamp NOT NULL, valid_to_exclusive timestamp NOT NULL,
 definition jsonb NOT NULL, definition_hash text NOT NULL,
 is_demo boolean NOT NULL, evidence_reference text NOT NULL,
 CHECK(valid_from<valid_to_exclusive)
);
CREATE TABLE config.basis_assumption_version (
 assumption_id text NOT NULL, version integer NOT NULL CHECK(version>0),
 status text NOT NULL CHECK(status='assumed_unverified'), statement text NOT NULL,
 evidence_reference text NOT NULL, PRIMARY KEY(assumption_id,version)
);
CREATE TABLE config.assumption_application (
 data_scope_id text NOT NULL REFERENCES meta.data_scope,
 assumption_id text NOT NULL, assumption_version integer NOT NULL,
 enabled boolean NOT NULL, PRIMARY KEY(data_scope_id,assumption_id,assumption_version),
 FOREIGN KEY(assumption_id,assumption_version) REFERENCES config.basis_assumption_version
);
CREATE TABLE config.area_basis_version (
 basis_version_id text PRIMARY KEY, data_scope_id text NOT NULL REFERENCES meta.data_scope,
 compartment_id text NOT NULL REFERENCES core.compartment, measure_code text NOT NULL REFERENCES config.measure_definition,
 version integer NOT NULL CHECK(version>0), area_m2 numeric NOT NULL CHECK(area_m2>0 AND area_m2<>'NaN'::numeric),
 basis_kind text NOT NULL, verification_status text NOT NULL CHECK(verification_status IN ('verified','assumed_unverified')),
 valid_from date NOT NULL, valid_to_exclusive date NOT NULL,
 assumption_id text, assumption_version integer, evidence_reference text NOT NULL CHECK(length(evidence_reference)>0),
 FOREIGN KEY(assumption_id,assumption_version) REFERENCES config.basis_assumption_version,
 CHECK(valid_from<valid_to_exclusive),
 CHECK((verification_status='verified' AND assumption_id IS NULL AND assumption_version IS NULL)
    OR (verification_status='assumed_unverified' AND assumption_id IS NOT NULL AND assumption_version IS NOT NULL)),
 UNIQUE(basis_version_id,data_scope_id,compartment_id,measure_code)
);
-- The explicit selection pointer gives at most one selected version per context;
-- old basis versions can overlap in time, but are never chosen by "latest".
CREATE TABLE config.basis_application (
 data_scope_id text NOT NULL, compartment_id text NOT NULL, measure_code text NOT NULL,
 analysis_context text NOT NULL CHECK(analysis_context IN ('source_verified','shared_analysis','sensitivity')),
 basis_version_id text NOT NULL, enabled boolean NOT NULL,
 PRIMARY KEY(data_scope_id,compartment_id,measure_code,analysis_context),
 FOREIGN KEY(basis_version_id,data_scope_id,compartment_id,measure_code)
 REFERENCES config.area_basis_version(basis_version_id,data_scope_id,compartment_id,measure_code)
);
CREATE TABLE meta.pipeline_run (
 run_id text PRIMARY KEY, data_scope_id text NOT NULL, snapshot_id text NOT NULL,
 status text NOT NULL CHECK(status IN ('loading','core_loaded','blocked')),
 parser_version text NOT NULL, quality_policy_version text NOT NULL REFERENCES config.quality_policy_version,
 source_manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(data_scope_id,snapshot_id) REFERENCES meta.data_scope(data_scope_id,snapshot_id),
 UNIQUE(run_id,data_scope_id)
);
CREATE TABLE meta.source_file (
 source_file_id text PRIMARY KEY, snapshot_id text NOT NULL REFERENCES meta.dataset_snapshot,
 relative_path text NOT NULL, checksum text NOT NULL CHECK(checksum ~ '^[a-f0-9]{64}$'),
 expected_rows integer CHECK(expected_rows>=0), source_folder text NOT NULL,
 UNIQUE(snapshot_id,relative_path), UNIQUE(source_file_id,snapshot_id)
);
CREATE TABLE meta.file_ingestion (
 run_id text NOT NULL REFERENCES meta.pipeline_run, source_file_id text NOT NULL REFERENCES meta.source_file,
 ingestion_status text NOT NULL CHECK(ingestion_status IN ('loaded','loaded_with_quarantine','failed','deferred')),
 core_status text NOT NULL CHECK(core_status IN ('loaded','loaded_with_quarantine','failed','deferred','blocked')),
 outputs_consumable boolean NOT NULL, records_read integer NOT NULL CHECK(records_read>=0),
 accepted integer NOT NULL CHECK(accepted>=0), quarantine integer NOT NULL CHECK(quarantine>=0),
 duplicate integer NOT NULL CHECK(duplicate>=0), conflict integer NOT NULL CHECK(conflict>=0),
 details jsonb NOT NULL, PRIMARY KEY(run_id,source_file_id),
 CHECK(records_read=accepted+quarantine+duplicate+conflict)
);
CREATE TABLE meta.source_record (
 source_record_id text PRIMARY KEY, source_file_id text NOT NULL, snapshot_id text NOT NULL,
 first_run_id text NOT NULL REFERENCES meta.pipeline_run,
 line_number integer NOT NULL CHECK(line_number>=2), line_end integer NOT NULL,
 raw_reference text NOT NULL, raw_hash text NOT NULL CHECK(raw_hash ~ '^[a-f0-9]{64}$'),
 source_time_serial numeric, raw_time_token text, source_timestamp timestamp,
 slot_residual_seconds numeric, adapter_id text NOT NULL, adapter_version integer NOT NULL,
 time_policy_version text NOT NULL, time_precision text NOT NULL CHECK(time_precision='staging_truncated_us'),
 FOREIGN KEY(source_file_id,snapshot_id) REFERENCES meta.source_file(source_file_id,snapshot_id),
 CHECK(line_end>=line_number), UNIQUE(source_file_id,line_number), UNIQUE(source_record_id,snapshot_id)
);
CREATE TABLE meta.correction (
 correction_id text PRIMARY KEY, source_record_id text NOT NULL REFERENCES meta.source_record,
 version integer NOT NULL CHECK(version>0), old_value jsonb NOT NULL, new_value jsonb NOT NULL,
 evidence_reference text NOT NULL CHECK(length(trim(evidence_reference))>0),
 recorded_by text NOT NULL CHECK(length(trim(recorded_by))>0), created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(source_record_id,version), UNIQUE(correction_id,source_record_id)
);
CREATE TABLE core.record_revision (
 revision_id text PRIMARY KEY, data_scope_id text NOT NULL, snapshot_id text NOT NULL,
 cycle_id text NOT NULL REFERENCES core.growing_cycle, run_id text NOT NULL,
 source_record_id text NOT NULL, record_type text NOT NULL CHECK(record_type IN ('climate','drain','resource','harvest')),
 logical_record_key text NOT NULL, revision_number integer NOT NULL CHECK(revision_number>0),
 content_hash text NOT NULL, observed_at timestamp NOT NULL, is_close_boundary boolean NOT NULL,
 correction_id text,
 FOREIGN KEY(run_id,data_scope_id) REFERENCES meta.pipeline_run(run_id,data_scope_id),
 FOREIGN KEY(data_scope_id,snapshot_id) REFERENCES meta.data_scope(data_scope_id,snapshot_id),
 FOREIGN KEY(source_record_id,snapshot_id) REFERENCES meta.source_record(source_record_id,snapshot_id),
 FOREIGN KEY(correction_id,source_record_id) REFERENCES meta.correction(correction_id,source_record_id),
 UNIQUE(data_scope_id,record_type,logical_record_key,revision_number),
 UNIQUE(data_scope_id,record_type,source_record_id,revision_number),
 UNIQUE(revision_id,record_type,observed_at), UNIQUE(revision_id,record_type),
 UNIQUE(revision_id,data_scope_id,record_type,logical_record_key)
);
CREATE FUNCTION core.check_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c core.growing_cycle; f meta.source_file; sn meta.source_record;
BEGIN
 SELECT * INTO c FROM core.growing_cycle WHERE cycle_id=NEW.cycle_id;
 SELECT * INTO sn FROM meta.source_record WHERE source_record_id=NEW.source_record_id;
 SELECT * INTO f FROM meta.source_file WHERE source_file_id=sn.source_file_id;
 IF NOT EXISTS(SELECT 1 FROM meta.source_folder_mapping m WHERE m.snapshot_id=NEW.snapshot_id
  AND m.source_folder=f.source_folder AND m.cycle_id=NEW.cycle_id) THEN
  RAISE EXCEPTION 'source/cycle mapping mismatch' USING ERRCODE='23514';
 END IF;
 IF NEW.is_close_boundary <> (NEW.observed_at=c.end_source_time_exclusive AND NEW.record_type IN ('climate','drain'))
 OR NEW.observed_at<c.start_source_time OR NEW.observed_at>c.end_source_time_exclusive
 OR (NEW.observed_at=c.end_source_time_exclusive AND NOT NEW.is_close_boundary) THEN
  RAISE EXCEPTION 'outside cycle or invalid closing boundary' USING ERRCODE='23514';
 END IF;
 IF (NEW.record_type IN ('climate','drain') AND mod(extract(epoch FROM NEW.observed_at-timestamp '1899-12-30'),300)<>0)
 OR (NEW.record_type IN ('resource','harvest') AND NEW.observed_at<>date_trunc('day',NEW.observed_at)) THEN
  RAISE EXCEPTION 'invalid record time grain' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER check_revision BEFORE INSERT OR UPDATE ON core.record_revision FOR EACH ROW EXECUTE FUNCTION core.check_revision();

CREATE TABLE core.climate_observation (
 revision_id text PRIMARY KEY, record_type text NOT NULL DEFAULT 'climate' CHECK(record_type='climate'),
 observed_at timestamp NOT NULL, tair numeric, co2air numeric, t_heat_sp numeric, t_vent_sp numeric,
 co2_sp numeric, t_heat_vip numeric, t_ventlee_vip numeric, co2_vip numeric,
 iso_year integer GENERATED ALWAYS AS (extract(isoyear FROM observed_at)::integer) STORED,
 iso_week integer GENERATED ALWAYS AS (extract(week FROM observed_at)::integer) STORED,
 FOREIGN KEY(revision_id,record_type,observed_at) REFERENCES core.record_revision(revision_id,record_type,observed_at)
);
CREATE TABLE core.drain_observation (
 revision_id text PRIMARY KEY, record_type text NOT NULL DEFAULT 'drain' CHECK(record_type='drain'),
 observed_at timestamp NOT NULL, measurement_context text NOT NULL DEFAULT 'drain_process_computer' CHECK(measurement_context='drain_process_computer'),
 ec_drain numeric, ph_drain numeric,
 iso_year integer GENERATED ALWAYS AS (extract(isoyear FROM observed_at)::integer) STORED,
 iso_week integer GENERATED ALWAYS AS (extract(week FROM observed_at)::integer) STORED,
 FOREIGN KEY(revision_id,record_type,observed_at) REFERENCES core.record_revision(revision_id,record_type,observed_at)
);
CREATE TABLE core.resource_day (
 revision_id text PRIMARY KEY, record_type text NOT NULL DEFAULT 'resource' CHECK(record_type='resource'),
 observed_at timestamp NOT NULL, resource_date date GENERATED ALWAYS AS (observed_at::date) STORED,
 heat_mj_m2 numeric, elec_high_kwh_m2 numeric, elec_low_kwh_m2 numeric, co2_kg_m2 numeric,
 irrigation_l_m2 numeric, drain_l_m2 numeric,
 iso_year integer GENERATED ALWAYS AS (extract(isoyear FROM observed_at)::integer) STORED,
 iso_week integer GENERATED ALWAYS AS (extract(week FROM observed_at)::integer) STORED,
 FOREIGN KEY(revision_id,record_type,observed_at) REFERENCES core.record_revision(revision_id,record_type,observed_at)
);
CREATE TABLE core.harvest (
 revision_id text PRIMARY KEY, record_type text NOT NULL DEFAULT 'harvest' CHECK(record_type='harvest'),
 observed_at timestamp NOT NULL, harvest_date date GENERATED ALWAYS AS (observed_at::date) STORED,
 prod_a_kg_m2 numeric, prod_b_kg_m2 numeric,
 iso_year integer GENERATED ALWAYS AS (extract(isoyear FROM observed_at)::integer) STORED,
 iso_week integer GENERATED ALWAYS AS (extract(week FROM observed_at)::integer) STORED,
 FOREIGN KEY(revision_id,record_type,observed_at) REFERENCES core.record_revision(revision_id,record_type,observed_at)
);
CREATE TABLE core.measure_quality (
 revision_id text NOT NULL, record_type text NOT NULL, measure_code text NOT NULL,
 policy_version_id text NOT NULL REFERENCES config.quality_policy_version,
 status text NOT NULL CHECK(status IN ('accepted','missing','invalid','suspect')),
 applicability_status text NOT NULL CHECK(applicability_status IN ('applicable','unknown')),
 reason_code text, PRIMARY KEY(revision_id,measure_code,policy_version_id),
 FOREIGN KEY(revision_id,record_type) REFERENCES core.record_revision(revision_id,record_type),
 FOREIGN KEY(measure_code,record_type) REFERENCES config.measure_definition(measure_code,record_type),
 CHECK((status='accepted' AND applicability_status='applicable' AND reason_code IS NULL)
    OR ((status<>'accepted' OR applicability_status='unknown') AND reason_code IS NOT NULL))
);
CREATE FUNCTION core.check_quality_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM core.record_revision r JOIN config.quality_policy_version p
 ON p.policy_version_id=NEW.policy_version_id WHERE r.revision_id=NEW.revision_id
 AND r.observed_at>=p.valid_from AND (r.observed_at<p.valid_to_exclusive
 OR (r.is_close_boundary AND r.observed_at=p.valid_to_exclusive))) THEN
 RAISE EXCEPTION 'quality policy outside effective interval' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER quality_effective BEFORE INSERT OR UPDATE ON core.measure_quality FOR EACH ROW EXECUTE FUNCTION core.check_quality_policy();
CREATE TABLE meta.dq_issue (
 issue_id text PRIMARY KEY, run_id text NOT NULL REFERENCES meta.pipeline_run,
 source_record_id text NOT NULL REFERENCES meta.source_record, revision_id text REFERENCES core.record_revision,
 measure_code text REFERENCES config.measure_definition, reason_code text NOT NULL,
 policy_version text NOT NULL, evidence_reference text NOT NULL
);
CREATE TABLE meta.quarantine_record (
 quarantine_id text PRIMARY KEY, source_record_id text NOT NULL REFERENCES meta.source_record,
 run_id text NOT NULL REFERENCES meta.pipeline_run, disposition text NOT NULL CHECK(disposition IN ('quarantine','conflict','duplicate')),
 reason_code text NOT NULL, policy_version text NOT NULL, details jsonb NOT NULL,
 correction_id text,
 FOREIGN KEY(correction_id,source_record_id) REFERENCES meta.correction(correction_id,source_record_id),
 UNIQUE(run_id,source_record_id)
);
CREATE TABLE meta.sensor_event (
 data_scope_id text NOT NULL REFERENCES meta.data_scope, event_id text NOT NULL,
 source_record_id text REFERENCES meta.source_record, payload_hash text NOT NULL,
 source_observed_at timestamp NOT NULL, published_at timestamptz NOT NULL,
 first_received_at timestamptz NOT NULL, PRIMARY KEY(data_scope_id,event_id)
);
CREATE TABLE meta.publication (
 publication_id text PRIMARY KEY, data_scope_id text NOT NULL REFERENCES meta.data_scope,
 run_id text NOT NULL, quality_policy_version text NOT NULL REFERENCES config.quality_policy_version,
 config_bundle_hash text NOT NULL, observed_through timestamp, committed_at timestamptz NOT NULL,
 finality_status text NOT NULL CHECK(finality_status IN ('provisional','complete')),
 FOREIGN KEY(run_id,data_scope_id) REFERENCES meta.pipeline_run(run_id,data_scope_id),
 UNIQUE(publication_id,data_scope_id)
);
CREATE TABLE meta.publication_member (
 publication_id text NOT NULL, data_scope_id text NOT NULL, record_type text NOT NULL,
 logical_record_key text NOT NULL, revision_id text NOT NULL,
 PRIMARY KEY(publication_id,record_type,logical_record_key),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 FOREIGN KEY(revision_id,data_scope_id,record_type,logical_record_key)
 REFERENCES core.record_revision(revision_id,data_scope_id,record_type,logical_record_key)
);
CREATE TABLE meta.current_publication (
 data_scope_id text PRIMARY KEY, publication_id text NOT NULL,
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id)
);
CREATE TABLE meta.publication_basis (
 publication_id text NOT NULL, data_scope_id text NOT NULL, compartment_id text NOT NULL,
 measure_code text NOT NULL, analysis_context text NOT NULL CHECK(analysis_context IN ('source_verified','shared_analysis','sensitivity')),
 basis_version_id text NOT NULL,
 PRIMARY KEY(publication_id,compartment_id,measure_code,analysis_context),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 FOREIGN KEY(basis_version_id,data_scope_id,compartment_id,measure_code)
 REFERENCES config.area_basis_version(basis_version_id,data_scope_id,compartment_id,measure_code)
);
CREATE FUNCTION config.immutable_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'version is immutable: append a new version' USING ERRCODE='23514'; END $$;
CREATE TRIGGER immutable_basis BEFORE UPDATE OR DELETE ON config.area_basis_version FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_assumption BEFORE UPDATE OR DELETE ON config.basis_assumption_version FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_quality BEFORE UPDATE OR DELETE ON config.quality_policy_version FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_measure BEFORE UPDATE OR DELETE ON config.measure_definition FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_correction BEFORE UPDATE OR DELETE ON meta.correction FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE INDEX revision_run ON core.record_revision(run_id);
CREATE INDEX revision_cycle_time ON core.record_revision(cycle_id,observed_at);
CREATE INDEX source_record_file ON meta.source_record(source_file_id);
CREATE INDEX dq_source ON meta.dq_issue(source_record_id);
CREATE FUNCTION core.finite_or_null(n numeric) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
 SELECT n IS NULL OR n NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)
$$;
ALTER TABLE config.area_basis_version ADD CHECK(core.finite_or_null(area_m2));
ALTER TABLE meta.source_record ADD CHECK(core.finite_or_null(source_time_serial) AND core.finite_or_null(slot_residual_seconds));
ALTER TABLE core.climate_observation ADD CHECK(core.finite_or_null(tair) AND core.finite_or_null(co2air)
 AND core.finite_or_null(t_heat_sp) AND core.finite_or_null(t_vent_sp) AND core.finite_or_null(co2_sp)
 AND core.finite_or_null(t_heat_vip) AND core.finite_or_null(t_ventlee_vip) AND core.finite_or_null(co2_vip));
ALTER TABLE core.drain_observation ADD CHECK(core.finite_or_null(ec_drain) AND core.finite_or_null(ph_drain));
ALTER TABLE core.resource_day ADD CHECK(core.finite_or_null(heat_mj_m2) AND core.finite_or_null(elec_high_kwh_m2)
 AND core.finite_or_null(elec_low_kwh_m2) AND core.finite_or_null(co2_kg_m2)
 AND core.finite_or_null(irrigation_l_m2) AND core.finite_or_null(drain_l_m2));
ALTER TABLE core.harvest ADD CHECK(core.finite_or_null(prod_a_kg_m2) AND core.finite_or_null(prod_b_kg_m2));

CREATE TRIGGER immutable_revision BEFORE UPDATE OR DELETE ON core.record_revision FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_source BEFORE UPDATE OR DELETE ON meta.source_record FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_publication BEFORE UPDATE OR DELETE ON meta.publication FOR EACH ROW EXECUTE FUNCTION config.immutable_version();

CREATE UNIQUE INDEX revision_time_grain ON core.record_revision(data_scope_id,cycle_id,record_type,observed_at,revision_number)
 WHERE record_type IN ('climate','drain','resource');
CREATE FUNCTION config.check_basis_scope() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM meta.data_scope ds JOIN meta.source_folder_mapping m USING(snapshot_id)
 JOIN core.growing_cycle c ON c.cycle_id=m.cycle_id
 WHERE ds.data_scope_id=NEW.data_scope_id AND m.compartment_id=NEW.compartment_id
 AND NEW.valid_from>=c.start_source_time::date AND NEW.valid_to_exclusive<=c.end_source_time_exclusive::date) THEN
 RAISE EXCEPTION 'basis outside mapped scope/cycle' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER basis_scope BEFORE INSERT ON config.area_basis_version FOR EACH ROW EXECUTE FUNCTION config.check_basis_scope();
