SELECT * FROM mart.fact_energy_day
WHERE publication_id=%s AND data_scope_id=%s AND cycle_id=%s AND compartment_id=%s
 AND resource_date>=%s AND resource_date<%s
ORDER BY resource_date, CASE component WHEN 'heat' THEN 0 WHEN 'elec_high' THEN 1 ELSE 2 END
