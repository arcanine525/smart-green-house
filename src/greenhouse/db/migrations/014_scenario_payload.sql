-- BR-06 Energy cost scenario payload persistence (T20 additive migration).
-- Persists exact ScenarioChange tuple and full command payload for deterministic replay.

ALTER TABLE config.cost_scenario
    ADD COLUMN changes jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE config.cost_scenario
    ADD COLUMN command_payload jsonb NULL;
