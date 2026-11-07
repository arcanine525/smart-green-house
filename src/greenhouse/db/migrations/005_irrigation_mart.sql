CREATE SCHEMA mart;

-- Half-even significant rounding uses exact integer/remainder arithmetic. This
-- reproduces Python Decimal(prec=50), including its multiplication before divide.
CREATE FUNCTION mart.decimal_exponent(v numeric) RETURNS integer LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE s text; fraction text;
BEGIN
 IF v=0 THEN RETURN 0; END IF;
 s:=abs(v)::text;
 IF abs(v)>=1 THEN RETURN length(split_part(s,'.',1))-1; END IF;
 fraction:=split_part(s,'.',2);
 RETURN -(length(fraction)-length(ltrim(fraction,'0'))+1);
END $$;
CREATE FUNCTION mart.ratio_sig(n numeric,d numeric,p integer) RETURNS numeric LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE exponent integer; quantum numeric; denominator numeric; whole numeric; remainder numeric;
BEGIN
 IF d<=0 OR n<0 OR p<1 THEN RAISE EXCEPTION 'invalid nonnegative ratio'; END IF;
 IF n=0 THEN RETURN 0; END IF;
 exponent:=mart.decimal_exponent(n)-mart.decimal_exponent(d);
 IF n<d*('1e'||exponent)::numeric THEN exponent:=exponent-1; END IF;
 quantum:=('1e'||(exponent-p+1))::numeric;
 denominator:=d*quantum;
 whole:=div(n,denominator); remainder:=n-whole*denominator;
 IF remainder*2>denominator OR (remainder*2=denominator AND mod(whole,2)=1) THEN whole:=whole+1; END IF;
 RETURN whole*quantum;
END $$;
CREATE FUNCTION mart.water_percent(n numeric,d numeric) RETURNS numeric LANGUAGE sql IMMUTABLE STRICT AS $$
 SELECT mart.ratio_sig(mart.ratio_sig(n*100,1,50),d,50)
$$;

CREATE TABLE config.irrigation_rule_profile (
 rule_version text PRIMARY KEY, rule_id text NOT NULL, version integer NOT NULL CHECK(version>0),
 data_scope_id text NOT NULL REFERENCES meta.data_scope,
 compartment_id text NOT NULL REFERENCES core.compartment,
 min_drain_pct numeric NOT NULL, max_drain_pct numeric NOT NULL,
 min_irrigation_l_m2 numeric NOT NULL, max_irrigation_l_m2 numeric NOT NULL,
 unit text NOT NULL CHECK(unit='L/m²'), ratio_unit text NOT NULL CHECK(ratio_unit='%'),
 evidence_reference text NOT NULL CHECK(length(trim(evidence_reference))>0),
 is_demo boolean NOT NULL, label text NOT NULL CHECK(length(trim(label))>0),
 CHECK(NOT is_demo OR label='demo_rule'),
 CHECK(min_drain_pct>=0 AND min_drain_pct<=max_drain_pct
 AND min_irrigation_l_m2>=0 AND min_irrigation_l_m2<=max_irrigation_l_m2),
 CHECK(min_drain_pct NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)
 AND max_drain_pct NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)
 AND min_irrigation_l_m2 NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)
 AND max_irrigation_l_m2 NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
 UNIQUE(rule_id,version,data_scope_id,compartment_id)
);
CREATE TRIGGER immutable_irrigation_rule BEFORE UPDATE OR DELETE ON config.irrigation_rule_profile
 FOR EACH ROW EXECUTE FUNCTION config.immutable_version();

CREATE TABLE mart.dim_date (
 resource_date date PRIMARY KEY,
 iso_year integer GENERATED ALWAYS AS (extract(isoyear FROM resource_date)::integer) STORED,
 iso_week integer GENERATED ALWAYS AS (extract(week FROM resource_date)::integer) STORED
);
CREATE TRIGGER immutable_date BEFORE UPDATE OR DELETE ON mart.dim_date
 FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TABLE mart.dim_irrigation_cycle (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL REFERENCES core.growing_cycle,
 compartment_id text NOT NULL REFERENCES core.compartment, compartment_name text NOT NULL,
 start_source_time timestamp NOT NULL, end_source_time_exclusive timestamp NOT NULL,
 PRIMARY KEY(publication_id,cycle_id), UNIQUE(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 CHECK(start_source_time<end_source_time_exclusive AND start_source_time::time='00:00'
 AND end_source_time_exclusive::time='00:00')
);
-- A fact must name the selected revision, not merely another revision of the
-- same logical key. The existing PK implies uniqueness; this index exposes it to FK.
CREATE UNIQUE INDEX publication_member_selected_revision ON meta.publication_member(publication_id,revision_id);
CREATE TABLE mart.fact_irrigation_day (
 publication_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL,
 compartment_id text NOT NULL, resource_date date NOT NULL REFERENCES mart.dim_date,
 revision_id text NOT NULL, record_type text NOT NULL DEFAULT 'resource' CHECK(record_type='resource'),
 logical_record_key text NOT NULL, quality_policy_version text NOT NULL REFERENCES config.quality_policy_version,
 correction_id text REFERENCES meta.correction, correction_version integer,
 irrigation_l_m2 numeric, drain_l_m2 numeric, source_unit text NOT NULL CHECK(source_unit='L/m²/day'),
 irrigation_quality text NOT NULL CHECK(irrigation_quality IN ('accepted','missing','invalid','suspect')),
 irrigation_applicability text NOT NULL CHECK(irrigation_applicability IN ('applicable','unknown')),
 irrigation_reason text, drain_quality text NOT NULL CHECK(drain_quality IN ('accepted','missing','invalid','suspect')),
 drain_applicability text NOT NULL CHECK(drain_applicability IN ('applicable','unknown')), drain_reason text,
 irrigation_basis_id text REFERENCES config.area_basis_version, irrigation_area_m2 numeric,
 irrigation_basis_kind text, irrigation_basis_status text, irrigation_assumption_id text, irrigation_assumption_version integer,
 drain_basis_id text REFERENCES config.area_basis_version, drain_area_m2 numeric,
 drain_basis_kind text, drain_basis_status text, drain_assumption_id text, drain_assumption_version integer,
 irrigation_accepted boolean NOT NULL, drain_accepted boolean NOT NULL, paired boolean NOT NULL,
 drain_fraction_pct numeric, drain_exceeds_irrigation boolean, reason_codes text[] NOT NULL,
 PRIMARY KEY(publication_id,cycle_id,resource_date),
 FOREIGN KEY(publication_id,data_scope_id) REFERENCES meta.publication(publication_id,data_scope_id),
 FOREIGN KEY(publication_id,cycle_id,compartment_id) REFERENCES mart.dim_irrigation_cycle(publication_id,cycle_id,compartment_id),
 FOREIGN KEY(publication_id,record_type,logical_record_key) REFERENCES meta.publication_member(publication_id,record_type,logical_record_key),
 FOREIGN KEY(publication_id,revision_id) REFERENCES meta.publication_member(publication_id,revision_id),
 FOREIGN KEY(revision_id,data_scope_id,record_type,logical_record_key)
 REFERENCES core.record_revision(revision_id,data_scope_id,record_type,logical_record_key),
 CHECK((correction_id IS NULL AND correction_version IS NULL) OR (correction_id IS NOT NULL AND correction_version>0)),
 CHECK(NOT paired OR (irrigation_accepted AND drain_accepted)),
 CHECK(paired OR (drain_fraction_pct IS NULL AND drain_exceeds_irrigation IS NULL))
);
CREATE TABLE mart.irrigation_publication (
 publication_id text PRIMARY KEY REFERENCES meta.publication,
 bundle_version text NOT NULL, day_count bigint NOT NULL CHECK(day_count>=0),
 cycle_count bigint NOT NULL CHECK(cycle_count>=0),
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL
);
CREATE TRIGGER sealed_irrigation_cycles BEFORE INSERT OR UPDATE OR DELETE ON mart.dim_irrigation_cycle
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();
CREATE TRIGGER sealed_irrigation_days BEFORE INSERT OR UPDATE OR DELETE ON mart.fact_irrigation_day
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();
CREATE TRIGGER sealed_irrigation_receipt BEFORE INSERT OR UPDATE OR DELETE ON mart.irrigation_publication
 FOR EACH ROW EXECUTE FUNCTION meta.guard_publication_children();

CREATE FUNCTION config.guard_irrigation_rule_scope() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM meta.data_scope ds JOIN meta.source_folder_mapping m USING(snapshot_id)
 WHERE ds.data_scope_id=NEW.data_scope_id AND m.compartment_id=NEW.compartment_id) THEN
 RAISE EXCEPTION 'rule compartment is outside data scope' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER scoped_irrigation_rule BEFORE INSERT ON config.irrigation_rule_profile
 FOR EACH ROW EXECUTE FUNCTION config.guard_irrigation_rule_scope();

CREATE FUNCTION mart.require_irrigation_bundle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM meta.workflow_run w WHERE w.workflow_id=NEW.workflow_id
 AND w.versions->>'code'='pipeline-v2-irrigation')
 AND NOT EXISTS(SELECT 1 FROM mart.irrigation_publication WHERE publication_id=NEW.publication_id) THEN
 RAISE EXCEPTION 'irrigation bundle required before publication seal' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER required_irrigation_bundle BEFORE INSERT ON meta.publication_seal
 FOR EACH ROW EXECUTE FUNCTION mart.require_irrigation_bundle();
