-- Shared immutable rule schedules for BR-01 (climate) and BR-03 (drain
-- nutrition): additive T14 store, separate from the water/harvest rule
-- profiles (005/007 keep byte-identical; this file only ADDS).
-- It is corrected within the unpublished T14 task commit to enforce a
-- WHOLE-revision immutable schedule boundary (R1):
--
-- Adapted from main decisions:
-- * one immutable revision may carry segments for several metrics;
-- * inside one revision the same (metric, measurement_context, basis) key
--   must not overlap in time: enforced HERE with an exclusion constraint so
--   repository bypasses cannot create per-key overlap; exact adjacency
--   stays legal;
-- * revision identity is sealed as a WHOLE: the header carries an explicit
--   immutable `registration_xid` (xid8) stamped by a BEFORE INSERT trigger
--   with `pg_current_xact_id()` (caller values are overridden). A segment
--   INSERT guard compares the stored stamp against the CURRENT
--   pg_current_xact_id() and refuses any later transaction. Inside nested
--   savepoints this stays allowed because PostgreSQL 17 documents
--   pg_current_xact_id() as the TOP-LEVEL id even under subtransactions
--   (functions-info.html), so header stamp and segment checks compare the
--   same identifier across nesting; later transactions cannot append.
--   Registration atomicity itself belongs to the repository's single
--   transaction/savepoint, so a failed append leaves no partial revision;
-- * alternate explicitly-selected revisions may cover the same interval
--   for replay, but an evaluation selects AT MOST ONE revision per
--   requested (metric, context, basis) — enforced in the pure engine;
-- * segment content is mode-strict: temperature/co2 carry delta only, and
--   ec/ph carry fixed lower/upper only (never mixed), with per-basis
--   restrictions (climate rules a requested/realized basis; nutrition
--   rules the measured basis);
-- * applied 005/006/007 remain immutable and untouched.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE config.rule_schedule_revision (
 rule_version text PRIMARY KEY,
 rule_id text NOT NULL,
 version integer NOT NULL CHECK(version>0),
 data_scope_id text NOT NULL REFERENCES meta.data_scope,
 compartment_id text NOT NULL REFERENCES core.compartment,
 evidence_reference text NOT NULL CHECK(length(trim(evidence_reference))>0),
 is_demo boolean NOT NULL,
 label text NOT NULL CHECK(length(trim(label))>0),
 registration_xid xid8 NOT NULL,
 UNIQUE(rule_id, version, data_scope_id, compartment_id),
 CHECK(NOT is_demo OR label='demo_rule')
);

CREATE TABLE config.rule_schedule_segment (
 rule_version text NOT NULL REFERENCES config.rule_schedule_revision(rule_version),
 metric text NOT NULL CHECK(metric IN ('temperature','co2','ec','ph')),
 measurement_context text NOT NULL CHECK(length(trim(measurement_context))>0),
 basis text NOT NULL CHECK(basis IN ('requested','realized','measured')),
 source_start timestamp NOT NULL,
 source_end timestamp NOT NULL,
 delta numeric NULL,
 lower numeric NULL,
 upper numeric NULL,
 CHECK(source_start < source_end),
 CHECK(delta IS NULL OR (delta NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric))),
 CHECK(lower IS NULL OR (lower >= 0 AND lower NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric))),
 CHECK(upper IS NULL OR (upper >= 0 AND upper NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric))),
 CHECK (
   (metric IN ('temperature','co2') AND delta IS NOT NULL AND lower IS NULL AND upper IS NULL
    AND basis IN ('requested','realized') AND delta >= 0)
   OR
   (metric IN ('ec','ph') AND delta IS NULL AND lower IS NOT NULL AND upper IS NOT NULL
    AND basis = 'measured' AND lower >= 0 AND lower <= upper)
 ),
 EXCLUDE USING gist (
   rule_version WITH =,
   metric WITH =,
   measurement_context WITH =,
   basis WITH =,
   tsrange(source_start, source_end, '[)') WITH &&
 )
);

CREATE TRIGGER immutable_rule_schedule_revision BEFORE UPDATE OR DELETE ON config.rule_schedule_revision
 FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
CREATE TRIGGER immutable_rule_schedule_segment BEFORE UPDATE OR DELETE ON config.rule_schedule_segment
 FOR EACH ROW EXECUTE FUNCTION config.immutable_version();

-- Registration identity stamp: the xid that owns the whole registration is
-- recorded ON THE HEADER by a trigger, so any INSERT path gets the same
-- top-level id and callers cannot forge or omit it. NULL/NOT NULL checks
-- run after BEFORE triggers, so the stamp always lands.
CREATE FUNCTION config.stamp_rule_schedule_registration() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.registration_xid := pg_current_xact_id();
 RETURN NEW;
END $$;
CREATE TRIGGER stamp_rule_schedule_registration BEFORE INSERT ON config.rule_schedule_revision
 FOR EACH ROW EXECUTE FUNCTION config.stamp_rule_schedule_registration();

-- Whole-revision seal: a segment may only join the registration that owns
-- the header (same top-level xid, any savepoint nesting). Any LATER
-- transaction — append, overlapping or not — is refused without privileged
-- trigger disabling. Exclusion constraint and immutability triggers above
-- stay fully enforced the whole time.
CREATE FUNCTION config.check_rule_schedule_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
 registration xid8;
BEGIN
 SELECT r.registration_xid INTO registration
  FROM config.rule_schedule_revision r WHERE r.rule_version = NEW.rule_version;
 IF registration IS NOT NULL AND registration <> pg_current_xact_id() THEN
  RAISE EXCEPTION
   'rule schedule revision % is sealed: registered segments cannot change membership',
   NEW.rule_version
   USING ERRCODE = 'P0001';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER seal_rule_schedule_segment BEFORE INSERT ON config.rule_schedule_segment
 FOR EACH ROW EXECUTE FUNCTION config.check_rule_schedule_sealed();
