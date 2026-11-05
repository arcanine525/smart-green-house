SELECT scenario_version
FROM config.cost_scenario
WHERE scenario_id = %s
ORDER BY version DESC
LIMIT 1;
