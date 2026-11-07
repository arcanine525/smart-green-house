-- Stream state is isolated from historical core/publications.
CREATE SCHEMA sensor_stream;
CREATE TABLE sensor_stream.processed_batch (
    stream_id text NOT NULL,
    checkpoint_generation text NOT NULL,
    batch_id bigint NOT NULL CHECK (batch_id >= 0),
    content_sha256 text NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_id, checkpoint_generation, batch_id)
);
CREATE TABLE sensor_stream.delivery (
    stream_id text NOT NULL,
    checkpoint_generation text NOT NULL,
    batch_id bigint NOT NULL,
    ordinal bigint NOT NULL,
    record jsonb NOT NULL,
    disposition text NOT NULL,
    reason text,
    replay_id text,
    identity text,
    payload_hash text,
    PRIMARY KEY (stream_id, checkpoint_generation, batch_id, ordinal),
    FOREIGN KEY (stream_id, checkpoint_generation, batch_id)
      REFERENCES sensor_stream.processed_batch(stream_id, checkpoint_generation, batch_id)
);
CREATE TABLE sensor_stream.event_variant (
    replay_id text NOT NULL,
    identity text NOT NULL,
    payload_hash text NOT NULL,
    envelope jsonb NOT NULL,
    PRIMARY KEY (replay_id, identity, payload_hash)
);
CREATE INDEX sensor_event_identity ON sensor_stream.event_variant (replay_id, identity);
-- Unresolved identity conflicts are omitted rather than choosing a winner.
CREATE VIEW sensor_stream.uncontested_event AS
SELECT v.* FROM sensor_stream.event_variant v
JOIN (SELECT replay_id, identity FROM sensor_stream.event_variant
      GROUP BY replay_id, identity HAVING count(*) = 1) ok
  ON ok.replay_id = v.replay_id AND ok.identity = v.identity;

-- One quality rule implementation for historical loads, corrections and replay.
CREATE FUNCTION sensor_stream.measure_verdict(
    definition jsonb, issues jsonb, null_reasons jsonb, code text, value numeric
) RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
 SELECT jsonb_build_object(
   'status', CASE
      WHEN EXISTS(SELECT 1 FROM jsonb_array_elements(issues) e
                  WHERE e->>'code'='invalid_numeric' AND e->>'column'=code) THEN 'invalid'
      WHEN value IS NULL THEN 'missing'
      WHEN definition->'nonnegative' ? code AND value<0 THEN 'invalid'
      WHEN definition->'suspect_zero' ? code AND value=0 THEN 'suspect'
      ELSE 'accepted' END,
   'applicability', CASE WHEN value IS NULL OR (definition->'off_mode_zero' ? code AND value=0)
      THEN 'unknown' ELSE 'applicable' END,
   'reason', CASE
      WHEN EXISTS(SELECT 1 FROM jsonb_array_elements(issues) e
                  WHERE e->>'code'='invalid_numeric' AND e->>'column'=code) THEN 'invalid_numeric'
      WHEN value IS NULL THEN coalesce(null_reasons->>code,'missing_source_value')
      WHEN definition->'nonnegative' ? code AND value<0 THEN 'negative_quantity'
      WHEN definition->'suspect_zero' ? code AND value=0 THEN 'demo_zero_ph_suspect'
      WHEN definition->'off_mode_zero' ? code AND value=0 THEN 'off_mode_unverified'
      ELSE NULL END)
$$;

CREATE TABLE sensor_stream.publication (
    publication_id text PRIMARY KEY,
    replay_id text NOT NULL,
    data_scope_id text NOT NULL,
    cycle_id text NOT NULL,
    compartment_id text NOT NULL,
    quality_policy_version text NOT NULL,
    cursor_sequence bigint NOT NULL CHECK (cursor_sequence >= 0),
    observed_through timestamp,
    received_through timestamptz,
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (publication_id, replay_id)
);
CREATE TABLE sensor_stream.publication_event (
    publication_id text NOT NULL,
    replay_id text NOT NULL,
    identity text NOT NULL,
    payload_hash text NOT NULL,
    PRIMARY KEY (publication_id, identity),
    FOREIGN KEY (publication_id, replay_id) REFERENCES sensor_stream.publication(publication_id, replay_id),
    FOREIGN KEY (replay_id, identity, payload_hash)
      REFERENCES sensor_stream.event_variant(replay_id, identity, payload_hash)
);
CREATE TABLE sensor_stream.publication_seal (
    publication_id text PRIMARY KEY,
    replay_id text NOT NULL,
    event_count bigint NOT NULL CHECK (event_count >= 0),
    membership_checksum text NOT NULL,
    UNIQUE (publication_id, replay_id),
    FOREIGN KEY (publication_id, replay_id) REFERENCES sensor_stream.publication(publication_id, replay_id)
);
CREATE TABLE sensor_stream.current_publication (
    replay_id text PRIMARY KEY,
    publication_id text NOT NULL,
    FOREIGN KEY (publication_id, replay_id) REFERENCES sensor_stream.publication_seal(publication_id, replay_id)
);
CREATE FUNCTION sensor_stream.reject_publication_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'sensor publications and membership are immutable';
END $$;
CREATE TRIGGER sensor_publication_immutable BEFORE UPDATE OR DELETE ON sensor_stream.publication
FOR EACH ROW EXECUTE FUNCTION sensor_stream.reject_publication_mutation();
CREATE TRIGGER sensor_publication_event_immutable BEFORE UPDATE OR DELETE ON sensor_stream.publication_event
FOR EACH ROW EXECUTE FUNCTION sensor_stream.reject_publication_mutation();
CREATE TRIGGER sensor_publication_seal_immutable BEFORE UPDATE OR DELETE ON sensor_stream.publication_seal
FOR EACH ROW EXECUTE FUNCTION sensor_stream.reject_publication_mutation();
CREATE FUNCTION sensor_stream.reject_sealed_event_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM sensor_stream.publication WHERE publication_id=NEW.publication_id FOR UPDATE;
    IF EXISTS (SELECT 1 FROM sensor_stream.publication_seal WHERE publication_id=NEW.publication_id) THEN
        RAISE EXCEPTION 'cannot add membership to sealed sensor publication';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sensor_publication_event_seal BEFORE INSERT ON sensor_stream.publication_event
FOR EACH ROW EXECUTE FUNCTION sensor_stream.reject_sealed_event_insert();
CREATE TRIGGER sensor_event_variant_immutable BEFORE UPDATE OR DELETE ON sensor_stream.event_variant
FOR EACH ROW EXECUTE FUNCTION sensor_stream.reject_publication_mutation();
CREATE FUNCTION sensor_stream.validate_publication_seal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_count bigint;
BEGIN
    PERFORM 1 FROM sensor_stream.publication WHERE publication_id=NEW.publication_id FOR UPDATE;
    SELECT count(*) INTO actual_count FROM sensor_stream.publication_event WHERE publication_id=NEW.publication_id;
    IF actual_count <> NEW.event_count THEN
        RAISE EXCEPTION 'sensor publication event count mismatch';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sensor_publication_seal_validate BEFORE INSERT ON sensor_stream.publication_seal
FOR EACH ROW EXECUTE FUNCTION sensor_stream.validate_publication_seal();
