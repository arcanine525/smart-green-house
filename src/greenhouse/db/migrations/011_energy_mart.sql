-- Explicit effective basis timelines; legacy single-version selections remain
-- valid. Versions themselves stay immutable. No latest/version fallback.
ALTER TABLE config.basis_application DROP CONSTRAINT basis_application_pkey;
ALTER TABLE config.basis_application ADD PRIMARY KEY
 (data_scope_id,compartment_id,measure_code,analysis_context,basis_version_id);
ALTER TABLE meta.publication_basis DROP CONSTRAINT publication_basis_pkey;
ALTER TABLE meta.publication_basis ADD PRIMARY KEY
 (publication_id,compartment_id,measure_code,analysis_context,basis_version_id);

CREATE FUNCTION config.guard_basis_intervals() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE selected config.area_basis_version;
BEGIN
 IF NOT NEW.enabled THEN RETURN NEW; END IF;
 -- Serialize competing selections of the same scope/measure/context. The scope
 -- row lock also makes concurrent statements observe earlier committed choices.
 PERFORM 1 FROM meta.data_scope WHERE data_scope_id=NEW.data_scope_id FOR UPDATE;
 SELECT * INTO STRICT selected FROM config.area_basis_version WHERE basis_version_id=NEW.basis_version_id;
 IF EXISTS(
  SELECT 1 FROM config.basis_application a JOIN config.area_basis_version b USING(basis_version_id)
  WHERE a.data_scope_id=NEW.data_scope_id AND a.compartment_id=NEW.compartment_id
   AND a.measure_code=NEW.measure_code AND a.analysis_context=NEW.analysis_context AND a.enabled
   AND (TG_OP='INSERT' OR a.basis_version_id<>OLD.basis_version_id)
   AND daterange(b.valid_from,b.valid_to_exclusive,'[)') &&
       daterange(selected.valid_from,selected.valid_to_exclusive,'[)')
 ) THEN RAISE EXCEPTION 'overlapping enabled effective basis intervals' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER effective_basis_intervals BEFORE INSERT OR UPDATE ON config.basis_application
 FOR EACH ROW EXECUTE FUNCTION config.guard_basis_intervals();

-- Resolve the effective pinned interval inside the LEFT JOIN, so another
-- interval never contributes a second NULL-basis row for the same measure.
CREATE OR REPLACE VIEW core.published_measure AS SELECT e.*,cp.publication_id,
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
 LEFT JOIN (meta.publication_basis pb JOIN config.area_basis_version b USING(basis_version_id))
 ON pb.publication_id=p.publication_id AND pb.data_scope_id=p.data_scope_id
 AND pb.compartment_id=e.compartment_id AND pb.measure_code=e.measure_code AND pb.analysis_context='shared_analysis'
 AND e.observed_at::date>=b.valid_from AND e.observed_at::date<b.valid_to_exclusive;

CREATE TABLE mart.dim_energy_cycle (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL REFERENCES core.growing_cycle,
 compartment_id text NOT NULL REFERENCES core.compartment, compartment_name text NOT NULL,
 start_source_time timestamp NOT NULL, end_source_time_exclusive timestamp NOT NULL,
 PRIMARY KEY(publication_id,cycle_id), UNIQUE(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 CHECK(start_source_time<end_source_time_exclusive AND start_source_time::time='00:00'
  AND end_source_time_exclusive::time='00:00')
);
CREATE TRIGGER sealed_energy_cycles BEFORE INSERT OR UPDATE OR DELETE ON mart.dim_energy_cycle
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

-- One row per selected ResourceDay x component, even missing/invalid values.
-- Decimal heat normalization is 50 significant digits, half-even. Raw MJ is
-- never reconstructed from normalized/display kWh when pricing later.
CREATE TABLE mart.fact_energy_day (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL,
 compartment_id text NOT NULL, resource_date date NOT NULL REFERENCES mart.dim_date,
 component text NOT NULL CHECK(component IN ('heat','elec_high','elec_low')),
 measure_code text NOT NULL, source_unit text NOT NULL,
 revision_id text NOT NULL, record_type text NOT NULL DEFAULT 'resource' CHECK(record_type='resource'),
 logical_record_key text NOT NULL, quality_policy_version text NOT NULL REFERENCES config.quality_policy_version,
 correction_id text REFERENCES meta.correction, correction_version integer,
 source_value numeric, quality text NOT NULL CHECK(quality IN ('accepted','missing','invalid','suspect')),
 applicability text NOT NULL CHECK(applicability IN ('applicable','unknown')), reason_code text,
 accepted boolean NOT NULL, normalized_kwh_m2 numeric,
 basis_version_id text REFERENCES config.area_basis_version, basis_version integer,
 area_m2 numeric, basis_kind text, basis_status text, basis_valid_from date, basis_valid_to_exclusive date,
 basis_evidence text, assumption_id text, assumption_version integer,
 assumption_statement text, assumption_evidence text,
 absolute_source_quantity numeric, absolute_kwh numeric,
 PRIMARY KEY(publication_id,cycle_id,resource_date,component),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 FOREIGN KEY(publication_id,cycle_id,compartment_id) REFERENCES mart.dim_energy_cycle(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,record_type,logical_record_key) REFERENCES meta.publication_member(publication_id,record_type,logical_record_key),
 FOREIGN KEY(publication_id,revision_id) REFERENCES meta.publication_member(publication_id,revision_id),
 FOREIGN KEY(revision_id,data_scope_id,record_type,logical_record_key)
  REFERENCES core.record_revision(revision_id,data_scope_id,record_type,logical_record_key),
 CHECK((component='heat' AND measure_code='Heat_cons' AND source_unit='MJ/m²/day')
  OR (component='elec_high' AND measure_code='ElecHigh' AND source_unit='kWh/m²/day')
  OR (component='elec_low' AND measure_code='ElecLow' AND source_unit='kWh/m²/day')),
 CHECK((correction_id IS NULL AND correction_version IS NULL) OR (correction_id IS NOT NULL AND correction_version>0)),
 CHECK(accepted=(quality='accepted' AND applicability='applicable' AND source_value IS NOT NULL)),
 CHECK(NOT accepted OR (source_value>=0 AND core.finite_or_null(source_value))),
 CHECK((accepted AND normalized_kwh_m2 IS NOT NULL) OR (NOT accepted AND normalized_kwh_m2 IS NULL)),
 CHECK((basis_version_id IS NULL AND area_m2 IS NULL AND basis_status IS NULL)
  OR (basis_version_id IS NOT NULL AND area_m2>0 AND core.finite_or_null(area_m2)
   AND basis_version>0 AND basis_status IN ('verified','assumed_unverified')
   AND resource_date>=basis_valid_from AND resource_date<basis_valid_to_exclusive)),
 CHECK((accepted AND basis_version_id IS NOT NULL AND absolute_source_quantity IS NOT NULL AND absolute_kwh IS NOT NULL)
  OR ((NOT accepted OR basis_version_id IS NULL) AND absolute_source_quantity IS NULL AND absolute_kwh IS NULL))
);
CREATE TRIGGER sealed_energy_days BEFORE INSERT OR UPDATE OR DELETE ON mart.fact_energy_day
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

CREATE TABLE mart.energy_publication (
 publication_id text PRIMARY KEY REFERENCES meta.publication, bundle_version text NOT NULL,
 quantity_version text NOT NULL, component_rows bigint NOT NULL CHECK(component_rows>=0),
 cycle_count integer NOT NULL CHECK(cycle_count>=0),
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL
);
CREATE TRIGGER sealed_energy_receipts BEFORE INSERT OR UPDATE OR DELETE ON mart.energy_publication
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

CREATE FUNCTION mart.require_energy_bundle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM meta.workflow_run w WHERE w.workflow_id=NEW.workflow_id
   AND w.versions->>'code'='pipeline-v6-energy') THEN
  IF NOT EXISTS(SELECT 1 FROM mart.irrigation_publication WHERE publication_id=NEW.publication_id)
   OR NOT EXISTS(SELECT 1 FROM mart.harvest_publication WHERE publication_id=NEW.publication_id)
   OR NOT EXISTS(SELECT 1 FROM mart.climate_publication WHERE publication_id=NEW.publication_id)
   OR NOT EXISTS(SELECT 1 FROM mart.drain_publication WHERE publication_id=NEW.publication_id)
   OR NOT EXISTS(SELECT 1 FROM mart.energy_publication WHERE publication_id=NEW.publication_id
     AND bundle_version='energy-mart-v1' AND quantity_version='energy-quantity-v1') THEN
   RAISE EXCEPTION 'all five mart bundles required before energy publication seal' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER required_energy_bundle BEFORE INSERT ON meta.publication_seal
 FOR EACH ROW EXECUTE FUNCTION mart.require_energy_bundle();
