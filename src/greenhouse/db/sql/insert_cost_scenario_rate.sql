INSERT INTO config.cost_scenario_rate (
    scenario_rate_id, scenario_version, component, currency, price_unit,
    rate, valid_from, valid_to_exclusive, source_reference
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
