SELECT cycle_id,component,sum(absolute_kwh) AS kwh FROM mart.fact_energy_day
WHERE publication_id=%(pub)s AND accepted
GROUP BY cycle_id,component ORDER BY cycle_id,component
