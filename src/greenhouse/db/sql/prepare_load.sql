CREATE TEMP TABLE core_input (
 source_record_id text PRIMARY KEY, source_file_id text NOT NULL, snapshot_id text NOT NULL,
 run_id text NOT NULL, data_scope_id text NOT NULL, cycle_id text NOT NULL,
 kind text NOT NULL, line_number integer NOT NULL, line_end integer NOT NULL,
 raw_reference text NOT NULL, raw_hash text NOT NULL, source_time_serial numeric, raw_time_token text,
 source_timestamp timestamp, slot_residual_seconds numeric, adapter_id text NOT NULL, adapter_version integer NOT NULL,
 time_policy_version text NOT NULL, observed_at timestamp, is_close_boundary boolean NOT NULL,
 logical_key text, disposition text NOT NULL, vals jsonb NOT NULL, null_reasons jsonb NOT NULL, issues jsonb NOT NULL
) ON COMMIT DROP;

CREATE TEMP TABLE core_batch_revision (LIKE core.record_revision INCLUDING DEFAULTS) ON COMMIT DROP;
ALTER TABLE core_batch_revision ADD PRIMARY KEY(revision_id);
