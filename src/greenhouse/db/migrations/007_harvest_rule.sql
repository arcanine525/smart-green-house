-- Additive harvest rule configuration for BR-05: explicit, versioned,
-- immutable class-B share threshold per scope/compartment. No default rule,
-- no silent threshold; mirrors the water rule shape of migration 005
-- (which stays byte-identical). Applied later than 006, never edits it.

CREATE TABLE config.harvest_rule_profile (
 rule_version text PRIMARY KEY, rule_id text NOT NULL, version integer NOT NULL CHECK(version>0),
 data_scope_id text NOT NULL REFERENCES meta.data_scope,
 compartment_id text NOT NULL REFERENCES core.compartment,
 max_class_b_share_pct numeric NOT NULL,
 unit text NOT NULL CHECK(unit='kg/m²'), ratio_unit text NOT NULL CHECK(ratio_unit='%'),
 evidence_reference text NOT NULL CHECK(length(trim(evidence_reference))>0),
 is_demo boolean NOT NULL, label text NOT NULL CHECK(length(trim(label))>0),
 CHECK(NOT is_demo OR label='demo_rule'),
 CHECK(max_class_b_share_pct>=0 AND max_class_b_share_pct<=100),
 CHECK(max_class_b_share_pct NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
 UNIQUE(rule_id,version,data_scope_id,compartment_id)
);
CREATE TRIGGER immutable_harvest_rule BEFORE UPDATE OR DELETE ON config.harvest_rule_profile
 FOR EACH ROW EXECUTE FUNCTION config.immutable_version();
