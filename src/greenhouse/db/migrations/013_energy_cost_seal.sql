-- BR-06 Energy cost and tariff scenarios: immutability sealing (T20 additive migration).
-- Enforces WHOLE-version rate sealing for config.tariff_profile and config.cost_scenario.
-- Header carries an explicit immutable registration_xid (xid8) stamped by a BEFORE INSERT trigger.
-- Child rate BEFORE INSERT guards compare stored stamp against pg_current_xact_id()
-- and reject any later transaction appending child rates to an existing profile or scenario.

ALTER TABLE config.tariff_profile
    ADD COLUMN registration_xid xid8 NOT NULL DEFAULT pg_current_xact_id();

ALTER TABLE config.cost_scenario
    ADD COLUMN registration_xid xid8 NOT NULL DEFAULT pg_current_xact_id();

-- Tariff profile stamp and seal guards
CREATE FUNCTION config.stamp_tariff_profile_registration() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.registration_xid := pg_current_xact_id();
    RETURN NEW;
END $$;

CREATE TRIGGER stamp_tariff_profile_registration BEFORE INSERT ON config.tariff_profile
    FOR EACH ROW EXECUTE FUNCTION config.stamp_tariff_profile_registration();

CREATE FUNCTION config.check_tariff_profile_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    registration xid8;
BEGIN
    SELECT p.registration_xid INTO registration
      FROM config.tariff_profile p WHERE p.tariff_version = NEW.tariff_version;
    IF registration IS NOT NULL AND registration <> pg_current_xact_id() THEN
        RAISE EXCEPTION
            'tariff profile % is sealed: registered rates cannot change membership',
            NEW.tariff_version
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER seal_tariff_rate BEFORE INSERT ON config.tariff_rate
    FOR EACH ROW EXECUTE FUNCTION config.check_tariff_profile_sealed();

-- Cost scenario stamp and seal guards
CREATE FUNCTION config.stamp_cost_scenario_registration() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.registration_xid := pg_current_xact_id();
    RETURN NEW;
END $$;

CREATE TRIGGER stamp_cost_scenario_registration BEFORE INSERT ON config.cost_scenario
    FOR EACH ROW EXECUTE FUNCTION config.stamp_cost_scenario_registration();

CREATE FUNCTION config.check_cost_scenario_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    registration xid8;
BEGIN
    SELECT s.registration_xid INTO registration
      FROM config.cost_scenario s WHERE s.scenario_version = NEW.scenario_version;
    IF registration IS NOT NULL AND registration <> pg_current_xact_id() THEN
        RAISE EXCEPTION
            'cost scenario % is sealed: registered rates cannot change membership',
            NEW.scenario_version
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER seal_cost_scenario_rate BEFORE INSERT ON config.cost_scenario_rate
    FOR EACH ROW EXECUTE FUNCTION config.check_cost_scenario_sealed();
