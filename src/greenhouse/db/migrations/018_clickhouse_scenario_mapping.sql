-- A restored older PG rehearsal and a native fork may have independently used
-- the same request/family IDs. Retain both immutable states and map explicitly.
CREATE TABLE meta.clickhouse_scenario_mapping (
 source_namespace text NOT NULL,
 source_scenario_version text NOT NULL,
 original_request_id text NOT NULL,
 target_scenario_version text NOT NULL REFERENCES config.cost_scenario(scenario_version),
 target_request_id text NOT NULL,
 source_payload_sha256 text NOT NULL CHECK(source_payload_sha256 ~ '^[a-f0-9]{64}$'),
 mapping jsonb NOT NULL,
 PRIMARY KEY(source_namespace,source_scenario_version),
 UNIQUE(source_namespace,original_request_id)
);
CREATE TRIGGER immutable_clickhouse_scenario_mapping BEFORE UPDATE OR DELETE ON meta.clickhouse_scenario_mapping
 FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
