-- BR-06 Energy cost and tariff scenarios: additive T20 migration.
-- Historical baseline tariff, rate intervals with temporal exclusion guards,
-- and append-only cost scenarios with request idempotency.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE config.tariff_profile (
    tariff_version text PRIMARY KEY,
    tariff_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    data_scope_id text NOT NULL REFERENCES meta.data_scope,
    compartment_id text NULL REFERENCES core.compartment,
    currency text NOT NULL CHECK (currency = 'EUR'),
    source_reference text NOT NULL CHECK (length(trim(source_reference)) > 0),
    label text NOT NULL CHECK (length(trim(label)) > 0),
    is_baseline boolean NOT NULL DEFAULT false,
    UNIQUE (tariff_id, version, data_scope_id)
);

CREATE TABLE config.tariff_rate (
    rate_id text PRIMARY KEY,
    tariff_version text NOT NULL REFERENCES config.tariff_profile(tariff_version),
    component text NOT NULL CHECK (component IN ('heat', 'elec_high', 'elec_low')),
    currency text NOT NULL CHECK (currency = 'EUR'),
    price_unit text NOT NULL CHECK (price_unit IN ('EUR/MJ', 'EUR/kWh')),
    rate numeric NOT NULL CHECK (rate >= 0 AND rate NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
    valid_from date NOT NULL,
    valid_to_exclusive date NOT NULL,
    source_reference text NOT NULL CHECK (length(trim(source_reference)) > 0),
    CHECK (valid_from < valid_to_exclusive),
    EXCLUDE USING gist (tariff_version WITH =, component WITH =, daterange(valid_from, valid_to_exclusive, '[)') WITH &&)
);

CREATE TABLE config.cost_scenario (
    scenario_version text PRIMARY KEY,
    scenario_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    request_id text NOT NULL,
    baseline_tariff_version text NOT NULL REFERENCES config.tariff_profile(tariff_version),
    publication_id text NOT NULL,
    name text NOT NULL CHECK (length(trim(name)) > 0),
    author text NOT NULL DEFAULT 'user',
    created_at timestamp with time zone NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (request_id),
    UNIQUE (scenario_id, version)
);

CREATE TABLE config.cost_scenario_rate (
    scenario_rate_id text PRIMARY KEY,
    scenario_version text NOT NULL REFERENCES config.cost_scenario(scenario_version),
    component text NOT NULL CHECK (component IN ('heat', 'elec_high', 'elec_low')),
    currency text NOT NULL CHECK (currency = 'EUR'),
    price_unit text NOT NULL CHECK (price_unit IN ('EUR/MJ', 'EUR/kWh')),
    rate numeric NOT NULL CHECK (rate >= 0 AND rate NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
    valid_from date NOT NULL,
    valid_to_exclusive date NOT NULL,
    source_reference text NOT NULL CHECK (length(trim(source_reference)) > 0),
    CHECK (valid_from < valid_to_exclusive),
    EXCLUDE USING gist (scenario_version WITH =, component WITH =, daterange(valid_from, valid_to_exclusive, '[)') WITH &&)
);

CREATE TRIGGER immutable_tariff_profile BEFORE UPDATE OR DELETE ON config.tariff_profile FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_tariff_rate BEFORE UPDATE OR DELETE ON config.tariff_rate FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_cost_scenario BEFORE UPDATE OR DELETE ON config.cost_scenario FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_cost_scenario_rate BEFORE UPDATE OR DELETE ON config.cost_scenario_rate FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
