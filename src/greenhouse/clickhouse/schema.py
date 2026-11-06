"""Versioned candidate core DDL; explicit typed grain, no PostgreSQL dependency."""
SCHEMA_VERSION = 'ch-core-v1'
DDL = (
    '''CREATE TABLE IF NOT EXISTS publication_state (
        scope_id String, workflow_id String, publication_id String, generation UInt64,
        stage LowCardinality(String), content_hash FixedString(64), payload String
    ) ENGINE=MergeTree ORDER BY (scope_id,workflow_id,stage,content_hash)''',
    '''CREATE TABLE IF NOT EXISTS source_revision (
        scope_id String, snapshot_id String, record_type LowCardinality(String),
        compartment_id LowCardinality(String), logical_key String,
        source_record_id String, revision UInt32, revision_id String,
        content_hash FixedString(64), source_file String, file_checksum FixedString(64),
        line_number UInt64, line_end UInt64, raw_hash FixedString(64),
        source_serial String, source_timestamp String, source_clock_us Nullable(Int64),
        correction_id String, parser_version String, quality_policy_version String,
        is_close_boundary Bool, payload String
    ) ENGINE=MergeTree ORDER BY (scope_id,record_type,compartment_id,logical_key,revision,revision_id)''',
    '''CREATE TABLE IF NOT EXISTS source_measure (
        scope_id String, revision_id String, measure_code LowCardinality(String),
        value_exact Nullable(String), value_decimal Nullable(Decimal(76,38)),
        projection_status LowCardinality(String), null_reason String
    ) ENGINE=MergeTree ORDER BY (scope_id,revision_id,measure_code)''',
    '''CREATE TABLE IF NOT EXISTS source_disposition (
        scope_id String, snapshot_id String, source_record_id String,
        source_file String, line_number UInt64, disposition LowCardinality(String),
        content_hash FixedString(64), payload String
    ) ENGINE=MergeTree ORDER BY (scope_id,source_record_id,disposition,content_hash)''',
    '''CREATE TABLE IF NOT EXISTS configuration_envelope (
        kind LowCardinality(String), scope_id String, version_id String,
        request_id String, content_hash FixedString(64), schema_version UInt16,
        payload String
    ) ENGINE=MergeTree ORDER BY (kind,scope_id,version_id,content_hash)''',
    '''CREATE TABLE IF NOT EXISTS load_member (
        scope_id String, load_id String, revision_id String
    ) ENGINE=MergeTree ORDER BY (scope_id,load_id,revision_id)''',
    '''CREATE TABLE IF NOT EXISTS load_receipt (
        scope_id String, load_id String, schema_version String,
        input_hash FixedString(64), counts_json String,
        membership_checksum String, membership_count UInt64
    ) ENGINE=MergeTree ORDER BY (scope_id,load_id,input_hash)''',
)


def migrate(client, coordinator=None):
    """Runtime migrations share the write fence; no-root mode is maintenance only."""
    def apply(execute):
        for statement in DDL:
            execute(statement)
        execute('ALTER TABLE load_receipt ADD COLUMN IF NOT EXISTS membership_checksum String')
        execute('ALTER TABLE load_receipt ADD COLUMN IF NOT EXISTS membership_count UInt64')
        return SCHEMA_VERSION
    if coordinator is None:
        return apply(client.execute)
    from greenhouse.clickhouse.coordinator import writer_lock
    from greenhouse.clickhouse.journal import execute_write
    with writer_lock(coordinator,client):
        return apply(lambda sql:execute_write(client,coordinator,sql))
