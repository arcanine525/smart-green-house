CREATE TABLE meta.workflow_run (
 workflow_id text PRIMARY KEY, data_scope_id text NOT NULL REFERENCES meta.data_scope,
 snapshot_id text NOT NULL, input_spec jsonb NOT NULL, versions jsonb NOT NULL,
 status text NOT NULL CHECK(status IN ('running','failed','completed')),
 ingestion_run_id text REFERENCES meta.pipeline_run, publication_id text REFERENCES meta.publication,
 created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 FOREIGN KEY(data_scope_id,snapshot_id) REFERENCES meta.data_scope(data_scope_id,snapshot_id)
);
CREATE TABLE meta.workflow_step (
 workflow_id text NOT NULL REFERENCES meta.workflow_run, step text NOT NULL CHECK(step IN ('ingest','load-core','spark','publish')),
 status text NOT NULL CHECK(status IN ('running','failed','completed')),
 attempt integer NOT NULL CHECK(attempt>0), started_at timestamptz NOT NULL, finished_at timestamptz,
 rows_in bigint NOT NULL DEFAULT 0, rows_out bigint NOT NULL DEFAULT 0,
 versions jsonb NOT NULL, artifact jsonb, artifact_checksum text, error jsonb,
 PRIMARY KEY(workflow_id,step)
);
CREATE TABLE meta.workflow_candidate (
 workflow_id text NOT NULL REFERENCES meta.workflow_run, revision_id text NOT NULL REFERENCES core.record_revision,
 PRIMARY KEY(workflow_id,revision_id)
);
CREATE TABLE meta.workflow_basis (
 workflow_id text NOT NULL REFERENCES meta.workflow_run, basis_version_id text NOT NULL REFERENCES config.area_basis_version,
 analysis_context text NOT NULL, PRIMARY KEY(workflow_id,basis_version_id,analysis_context)
);
CREATE TABLE meta.core_baseline_receipt (
 data_scope_id text PRIMARY KEY REFERENCES meta.data_scope, parser_version text NOT NULL,
 quality_policy_version text NOT NULL REFERENCES config.quality_policy_version,
 fingerprint text NOT NULL, measure_rows bigint NOT NULL, evidence_reference text NOT NULL
);
CREATE TABLE meta.publication_seal (
 publication_id text PRIMARY KEY REFERENCES meta.publication, workflow_id text UNIQUE REFERENCES meta.workflow_run,
 membership_checksum text NOT NULL, member_count bigint NOT NULL,
 artifact_checksum text NOT NULL, sealed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER immutable_baseline_receipt BEFORE UPDATE OR DELETE ON meta.core_baseline_receipt FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_publication_seal BEFORE UPDATE OR DELETE ON meta.publication_seal FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_climate_values BEFORE UPDATE OR DELETE ON core.climate_observation FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_drain_values BEFORE UPDATE OR DELETE ON core.drain_observation FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_resource_values BEFORE UPDATE OR DELETE ON core.resource_day FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_harvest_values BEFORE UPDATE OR DELETE ON core.harvest FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_quality_values BEFORE UPDATE OR DELETE ON core.measure_quality FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE FUNCTION meta.guard_publication_children() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (TG_OP<>'INSERT' AND EXISTS(SELECT 1 FROM meta.publication_seal WHERE publication_id=OLD.publication_id))
 OR (TG_OP<>'DELETE' AND EXISTS(SELECT 1 FROM meta.publication_seal WHERE publication_id=NEW.publication_id)) THEN
 RAISE EXCEPTION 'sealed publication is immutable' USING ERRCODE='23514'; END IF;
 RETURN coalesce(NEW,OLD);
END $$;
CREATE TRIGGER sealed_members BEFORE INSERT OR UPDATE OR DELETE ON meta.publication_member FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();
CREATE TRIGGER sealed_basis BEFORE INSERT OR UPDATE OR DELETE ON meta.publication_basis FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();
CREATE FUNCTION meta.guard_publication_pointer() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM meta.publication WHERE publication_id=NEW.publication_id AND data_scope_id=NEW.data_scope_id)
 AND NOT EXISTS(SELECT 1 FROM meta.publication_seal WHERE publication_id=NEW.publication_id) THEN
 RAISE EXCEPTION 'publication has not been sealed' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER sealed_pointer BEFORE INSERT OR UPDATE ON meta.current_publication FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_pointer();
CREATE INDEX workflow_scope ON meta.workflow_run(data_scope_id);
