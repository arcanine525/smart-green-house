SELECT s.*,
       r.scenario_rate_id,
       r.scenario_version AS rate_scenario_version,
       r.component,
       r.currency AS rate_currency,
       r.price_unit,
       r.rate,
       r.valid_from,
       r.valid_to_exclusive,
       r.source_reference AS rate_source_reference
FROM config.cost_scenario s
LEFT JOIN config.cost_scenario_rate r ON r.scenario_version = s.scenario_version
WHERE s.request_id = %s
ORDER BY r.component, r.valid_from;
