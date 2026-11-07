-- Retain original native identities/unknown provenance without interpreting
-- them as operational PostgreSQL configuration or silently discarding state.
CREATE TABLE meta.clickhouse_cutover_archive (
 archive_sha256 text PRIMARY KEY CHECK(archive_sha256 ~ '^[a-f0-9]{64}$'),
 payload jsonb NOT NULL, imported_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE meta.clickhouse_configuration_archive (
 kind text NOT NULL, data_scope_id text NOT NULL, version_id text NOT NULL,
 request_id text UNIQUE NOT NULL, content_hash text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(kind,data_scope_id,version_id)
);
CREATE TRIGGER immutable_clickhouse_cutover_archive BEFORE UPDATE OR DELETE ON meta.clickhouse_cutover_archive
 FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_clickhouse_configuration_archive BEFORE UPDATE OR DELETE ON meta.clickhouse_configuration_archive
 FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
