-- BR-06 Historical tariff scope and compartment coexistence (T20 additive migration).
-- Allows scope-level baseline (compartment_id IS NULL) and compartment-specific baselines
-- to coexist for the same data_scope_id, tariff_id, and version, while preventing duplicates.

ALTER TABLE config.tariff_profile
    DROP CONSTRAINT IF EXISTS tariff_profile_tariff_id_version_data_scope_id_key;

ALTER TABLE config.tariff_profile
    ADD CONSTRAINT tariff_profile_tariff_id_version_scope_comp_key
    UNIQUE NULLS NOT DISTINCT (tariff_id, version, data_scope_id, compartment_id);
