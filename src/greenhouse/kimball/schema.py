"""Kimball dimensional schema DDL for PostgreSQL (T24).

Dedicated isolated schema 'kimball' holding conformed dimensions and
dimensional facts directly populated from staging/parser streams.
Zero physical core tables, zero core foreign keys, zero dblink/FDW.
"""

SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS kimball;

-- Exact 50-significant IEEE rounding identical to mart.decimal_exponent and mart.ratio_sig
CREATE OR REPLACE FUNCTION kimball.decimal_exponent(v numeric) RETURNS integer LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE s text; fraction text;
BEGIN
 IF v=0 THEN RETURN 0; END IF;
 s:=abs(v)::text;
 IF abs(v)>=1 THEN RETURN length(split_part(s,'.',1))-1; END IF;
 fraction:=split_part(s,'.',2);
 RETURN -(length(fraction)-length(ltrim(fraction,'0'))+1);
END $$;

CREATE OR REPLACE FUNCTION kimball.ratio_sig(n numeric, d numeric, p integer) RETURNS numeric LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE exponent integer; quantum numeric; denominator numeric; whole numeric; remainder numeric;
BEGIN
 IF d<=0 OR n<0 OR p<1 THEN RAISE EXCEPTION 'invalid nonnegative ratio'; END IF;
 IF n=0 THEN RETURN 0; END IF;
 exponent:=kimball.decimal_exponent(n)-kimball.decimal_exponent(d);
 IF n<d*('1e'||exponent)::numeric THEN exponent:=exponent-1; END IF;
 quantum:=('1e'||(exponent-p+1))::numeric;
 denominator:=d*quantum;
 whole:=div(n,denominator); remainder:=n-whole*denominator;
 IF remainder*2>denominator OR (remainder*2=denominator AND mod(whole,2)=1) THEN whole:=whole+1; END IF;
 RETURN whole*quantum;
END $$;

CREATE OR REPLACE FUNCTION kimball.water_percent(n numeric, d numeric) RETURNS numeric LANGUAGE sql IMMUTABLE STRICT AS $$
 SELECT kimball.ratio_sig(kimball.ratio_sig(n*100, 1, 50), d, 50)
$$;

-- 1. Conformed Dimensions

CREATE TABLE IF NOT EXISTS kimball.dim_site (
    site_id text PRIMARY KEY,
    name text NOT NULL,
    source_time_basis text NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.dim_compartment (
    compartment_id text PRIMARY KEY,
    site_id text NOT NULL,
    name text NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.dim_growing_cycle (
    cycle_id text PRIMARY KEY,
    compartment_id text NOT NULL REFERENCES kimball.dim_compartment(compartment_id),
    start_source_time timestamp NOT NULL,
    end_source_time_exclusive timestamp NOT NULL,
    source_folder text NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.dim_date (
    resource_date date PRIMARY KEY,
    iso_year integer NOT NULL,
    iso_week integer NOT NULL,
    day_of_week integer NOT NULL,
    day_of_year integer NOT NULL,
    month integer NOT NULL,
    year integer NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.dim_measure_definition (
    measure_code text PRIMARY KEY,
    record_type text NOT NULL,
    unit text NOT NULL,
    source_reference text NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.dim_area_basis (
    basis_version_id text PRIMARY KEY,
    data_scope_id text NOT NULL,
    compartment_id text NOT NULL REFERENCES kimball.dim_compartment(compartment_id),
    measure_code text NOT NULL,
    version integer NOT NULL,
    area_m2 numeric NOT NULL CHECK(area_m2 > 0),
    basis_kind text NOT NULL,
    verification_status text NOT NULL CHECK(verification_status IN ('verified', 'assumed_unverified')),
    valid_from date NOT NULL,
    valid_to_exclusive date NOT NULL,
    assumption_id text,
    assumption_version integer,
    assumption_statement text,
    evidence_reference text NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.dim_rule_schedule (
    rule_version text PRIMARY KEY,
    rule_id text NOT NULL,
    version integer NOT NULL CHECK(version > 0),
    data_scope_id text NOT NULL,
    compartment_id text REFERENCES kimball.dim_compartment(compartment_id),
    evidence_reference text NOT NULL,
    is_demo boolean NOT NULL,
    label text NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.dim_rule_segment (
    rule_version text NOT NULL REFERENCES kimball.dim_rule_schedule(rule_version) ON DELETE CASCADE,
    metric text NOT NULL CHECK(metric IN ('temperature', 'co2', 'ec', 'ph')),
    measurement_context text NOT NULL,
    basis text NOT NULL CHECK(basis IN ('requested', 'realized', 'measured')),
    source_start timestamp NOT NULL,
    source_end timestamp NOT NULL,
    delta numeric NULL,
    lower numeric NULL,
    upper numeric NULL,
    CHECK(source_start < source_end),
    CHECK (
      (metric IN ('temperature','co2') AND delta IS NOT NULL AND lower IS NULL AND upper IS NULL
       AND basis IN ('requested','realized') AND delta >= 0)
      OR
      (metric IN ('ec','ph') AND delta IS NULL AND lower IS NOT NULL AND upper IS NOT NULL
       AND basis = 'measured' AND lower >= 0 AND lower <= upper)
    ),
    PRIMARY KEY (rule_version, metric, measurement_context, basis, source_start, source_end)
);

CREATE TABLE IF NOT EXISTS kimball.dim_harvest_rule (
    rule_version text PRIMARY KEY,
    rule_id text NOT NULL,
    version integer NOT NULL CHECK(version > 0),
    data_scope_id text NOT NULL,
    compartment_id text NOT NULL REFERENCES kimball.dim_compartment(compartment_id),
    max_class_b_share_pct numeric NOT NULL,
    evidence_reference text NOT NULL,
    is_demo boolean NOT NULL,
    label text NOT NULL,
    unit text NOT NULL DEFAULT 'kg/m²',
    ratio_unit text NOT NULL DEFAULT '%'
);

CREATE TABLE IF NOT EXISTS kimball.dim_tariff_profile (
    tariff_version text PRIMARY KEY,
    tariff_id text NOT NULL,
    version integer NOT NULL,
    data_scope_id text NOT NULL,
    currency text NOT NULL,
    source_reference text NOT NULL,
    label text NOT NULL,
    is_baseline boolean NOT NULL,
    compartment_id text,
    command_payload jsonb
);

CREATE TABLE IF NOT EXISTS kimball.dim_tariff_rate (
    rate_id text PRIMARY KEY,
    tariff_version text NOT NULL REFERENCES kimball.dim_tariff_profile(tariff_version) ON DELETE CASCADE,
    component text NOT NULL,
    currency text NOT NULL,
    price_unit text NOT NULL,
    rate numeric NOT NULL,
    valid_from date NOT NULL,
    valid_to_exclusive date NOT NULL,
    source_reference text NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.dim_scenario (
    scenario_version text PRIMARY KEY,
    scenario_id text NOT NULL,
    version integer NOT NULL CHECK(version > 0),
    request_id text NOT NULL UNIQUE,
    baseline_tariff_version text NOT NULL REFERENCES kimball.dim_tariff_profile(tariff_version),
    publication_id text NOT NULL,
    name text NOT NULL,
    author text NOT NULL DEFAULT 'user',
    changes jsonb NOT NULL,
    command_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS kimball.dim_scenario_rate (
    scenario_rate_id text PRIMARY KEY,
    scenario_version text NOT NULL REFERENCES kimball.dim_scenario(scenario_version) ON DELETE CASCADE,
    component text NOT NULL,
    currency text NOT NULL,
    price_unit text NOT NULL,
    rate numeric NOT NULL,
    valid_from date NOT NULL,
    valid_to_exclusive date NOT NULL,
    source_reference text NOT NULL
);

-- Publication Metadata and Receipts in kimball namespace
CREATE TABLE IF NOT EXISTS kimball.publication (
    publication_id text PRIMARY KEY,
    data_scope_id text NOT NULL,
    snapshot_id text NOT NULL,
    run_id text NOT NULL,
    quality_policy_version text NOT NULL,
    parser_version text NOT NULL,
    code_version text NOT NULL,
    config_bundle_hash text NOT NULL,
    committed_at timestamptz NOT NULL,
    finality_status text NOT NULL CHECK(finality_status IN ('provisional', 'complete')),
    member_count bigint NOT NULL DEFAULT 0,
    membership_checksum text NOT NULL
);

CREATE TABLE IF NOT EXISTS kimball.publication_receipt (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    bundle_name text NOT NULL,
    bundle_version text NOT NULL,
    quantity_version text,
    item_count bigint NOT NULL,
    details jsonb NOT NULL,
    PRIMARY KEY(publication_id, bundle_name)
);

CREATE TABLE IF NOT EXISTS kimball.source_file (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    snapshot_id text NOT NULL,
    archive_sha256 text NOT NULL,
    relative_path text NOT NULL,
    file_checksum text NOT NULL,
    size_bytes bigint,
    expected_rows integer,
    verified_at timestamptz NOT NULL,
    PRIMARY KEY(publication_id, relative_path)
);

CREATE TABLE IF NOT EXISTS kimball.quarantine_record (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    cycle_id text NOT NULL,
    compartment_id text NOT NULL,
    source_record_id text NOT NULL,
    disposition text NOT NULL,
    reason_code text NOT NULL,
    issue_column text,
    relative_path text NOT NULL,
    line_number integer NOT NULL,
    line_end integer,
    raw_reference text NOT NULL,
    raw_hash text NOT NULL,
    source_time_serial numeric,
    source_timestamp timestamp,
    PRIMARY KEY(publication_id, source_record_id, reason_code)
);

CREATE TABLE IF NOT EXISTS kimball.harvest_cycle_state (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    cycle_id text NOT NULL,
    compartment_id text NOT NULL,
    snapshot_complete boolean NOT NULL,
    source_resolved boolean NOT NULL,
    expected_files integer NOT NULL,
    files_present integer NOT NULL,
    files_loaded integer NOT NULL,
    files_failed integer NOT NULL,
    records_read integer NOT NULL,
    records_accepted integer NOT NULL,
    records_quarantine integer NOT NULL,
    records_duplicate integer NOT NULL,
    records_conflict integer NOT NULL,
    unresolved_records integer NOT NULL,
    conflicted_records integer NOT NULL,
    accepted_records integer NOT NULL,
    paired_records integer NOT NULL,
    prod_a_usable_records integer NOT NULL,
    prod_b_usable_records integer NOT NULL,
    event_a_dates integer NOT NULL,
    event_b_dates integer NOT NULL,
    reasons text[],
    expected_relative_path text,
    unresolved jsonb,
    PRIMARY KEY(publication_id, cycle_id, compartment_id)
);

-- 2. Stored Dimensional Facts (Directly Populated from Staging/Parser)

CREATE TABLE IF NOT EXISTS kimball.fact_climate_slot (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    data_scope_id text NOT NULL,
    cycle_id text NOT NULL REFERENCES kimball.dim_growing_cycle(cycle_id),
    compartment_id text NOT NULL REFERENCES kimball.dim_compartment(compartment_id),
    slot_start timestamp NOT NULL,
    tair numeric,
    co2air numeric,
    t_heat_sp numeric,
    t_vent_sp numeric,
    co2_sp numeric,
    t_heat_vip numeric,
    t_ventlee_vip numeric,
    co2_vip numeric,
    quality_policy_version text NOT NULL,
    measure_quality jsonb NOT NULL,
    -- Full Source Provenance
    source_record_id text NOT NULL,
    source_file text NOT NULL,
    line_number integer NOT NULL,
    line_end integer,
    raw_reference text NOT NULL,
    raw_hash text NOT NULL,
    source_time_serial numeric,
    source_timestamp timestamp NOT NULL,
    revision_id text NOT NULL,
    logical_record_key text NOT NULL,
    PRIMARY KEY(publication_id, cycle_id, slot_start)
);

CREATE TABLE IF NOT EXISTS kimball.fact_drain_slot (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    data_scope_id text NOT NULL,
    cycle_id text NOT NULL REFERENCES kimball.dim_growing_cycle(cycle_id),
    compartment_id text NOT NULL REFERENCES kimball.dim_compartment(compartment_id),
    slot_start timestamp NOT NULL,
    ec_drain numeric,
    ph_drain numeric,
    quality_policy_version text NOT NULL,
    measure_quality jsonb NOT NULL,
    -- Full Source Provenance
    source_record_id text NOT NULL,
    source_file text NOT NULL,
    line_number integer NOT NULL,
    line_end integer,
    raw_reference text NOT NULL,
    raw_hash text NOT NULL,
    source_time_serial numeric,
    source_timestamp timestamp NOT NULL,
    revision_id text NOT NULL,
    logical_record_key text NOT NULL,
    PRIMARY KEY(publication_id, cycle_id, slot_start)
);

CREATE TABLE IF NOT EXISTS kimball.fact_irrigation_day (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    data_scope_id text NOT NULL,
    cycle_id text NOT NULL REFERENCES kimball.dim_growing_cycle(cycle_id),
    compartment_id text NOT NULL REFERENCES kimball.dim_compartment(compartment_id),
    resource_date date NOT NULL,
    irrigation_l_m2 numeric,
    drain_l_m2 numeric,
    source_unit text NOT NULL DEFAULT 'L/m²/day',
    irrigation_quality text NOT NULL,
    irrigation_applicability text NOT NULL,
    irrigation_reason text,
    drain_quality text NOT NULL,
    drain_applicability text NOT NULL,
    drain_reason text,
    basis_version_id text REFERENCES kimball.dim_area_basis(basis_version_id),
    area_m2 numeric NOT NULL,
    basis_kind text NOT NULL,
    basis_status text NOT NULL,
    assumption_id text,
    assumption_version integer,
    paired boolean NOT NULL,
    drain_fraction_pct numeric,
    drain_exceeds_irrigation boolean NOT NULL,
    reason_codes text[],
    -- Full Source Provenance
    source_record_id text NOT NULL,
    source_file text NOT NULL,
    line_number integer NOT NULL,
    line_end integer,
    raw_reference text NOT NULL,
    raw_hash text NOT NULL,
    source_time_serial numeric,
    source_timestamp timestamp NOT NULL,
    revision_id text NOT NULL,
    logical_record_key text NOT NULL,
    PRIMARY KEY(publication_id, cycle_id, resource_date)
);

CREATE TABLE IF NOT EXISTS kimball.fact_energy_day (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    data_scope_id text NOT NULL,
    cycle_id text NOT NULL REFERENCES kimball.dim_growing_cycle(cycle_id),
    compartment_id text NOT NULL REFERENCES kimball.dim_compartment(compartment_id),
    resource_date date NOT NULL,
    component text NOT NULL,
    measure_code text NOT NULL,
    source_unit text NOT NULL,
    source_value numeric,
    quality text NOT NULL,
    applicability text NOT NULL,
    reason_code text,
    accepted boolean NOT NULL,
    normalized_kwh_m2 numeric,
    basis_version_id text REFERENCES kimball.dim_area_basis(basis_version_id),
    basis_version integer,
    area_m2 numeric NOT NULL,
    basis_kind text NOT NULL,
    basis_status text NOT NULL,
    basis_valid_from date NOT NULL,
    basis_valid_to_exclusive date NOT NULL,
    basis_evidence text NOT NULL,
    assumption_id text,
    assumption_version integer,
    assumption_statement text,
    assumption_evidence text,
    absolute_source_quantity numeric,
    absolute_kwh numeric,
    -- Full Source Provenance
    source_record_id text NOT NULL,
    source_file text NOT NULL,
    line_number integer NOT NULL,
    line_end integer,
    raw_reference text NOT NULL,
    raw_hash text NOT NULL,
    source_time_serial numeric,
    source_timestamp timestamp NOT NULL,
    revision_id text NOT NULL,
    logical_record_key text NOT NULL,
    PRIMARY KEY(publication_id, cycle_id, resource_date, component)
);

CREATE TABLE IF NOT EXISTS kimball.fact_harvest_record (
    publication_id text NOT NULL REFERENCES kimball.publication(publication_id),
    data_scope_id text NOT NULL,
    cycle_id text NOT NULL REFERENCES kimball.dim_growing_cycle(cycle_id),
    compartment_id text NOT NULL REFERENCES kimball.dim_compartment(compartment_id),
    harvest_date date NOT NULL,
    source_record_id text NOT NULL,
    prod_a_kg_m2 numeric,
    prod_b_kg_m2 numeric,
    prod_a_unit text NOT NULL DEFAULT 'kg/m²',
    prod_b_unit text NOT NULL DEFAULT 'kg/m²',
    prod_a_quality text NOT NULL,
    prod_a_applicability text NOT NULL,
    prod_a_reason text,
    prod_b_quality text NOT NULL,
    prod_b_applicability text NOT NULL,
    prod_b_reason text,
    prod_a_basis_id text REFERENCES kimball.dim_area_basis(basis_version_id),
    prod_a_area_m2 numeric NOT NULL,
    prod_a_basis_status text NOT NULL,
    prod_b_basis_id text REFERENCES kimball.dim_area_basis(basis_version_id),
    prod_b_area_m2 numeric NOT NULL,
    prod_b_basis_status text NOT NULL,
    -- Full Source Provenance
    source_file text NOT NULL,
    line_number integer NOT NULL,
    line_end integer,
    raw_reference text NOT NULL,
    raw_hash text NOT NULL,
    source_time_serial numeric,
    source_timestamp timestamp NOT NULL,
    revision_id text NOT NULL,
    logical_record_key text NOT NULL,
    PRIMARY KEY(publication_id, cycle_id, source_record_id)
);
"""
