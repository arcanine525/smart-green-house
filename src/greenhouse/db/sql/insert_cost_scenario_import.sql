-- Restore the original audit time when importing an immutable scenario.
INSERT INTO config.cost_scenario (
    scenario_version, scenario_id, version, request_id, baseline_tariff_version,
    publication_id, name, author, changes, command_payload, created_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s,clock_timestamp()))
