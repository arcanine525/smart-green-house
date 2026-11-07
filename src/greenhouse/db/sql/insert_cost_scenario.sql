INSERT INTO config.cost_scenario (
    scenario_version, scenario_id, version, request_id, baseline_tariff_version,
    publication_id, name, author, changes, command_payload
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
