SELECT cycle_id,sum(prod_a_kg_m2*prod_a_area_m2) AS a_kg FROM mart.fact_harvest_record
WHERE publication_id=%(pub)s AND prod_a_quality='accepted' AND prod_a_applicability='applicable'
GROUP BY cycle_id ORDER BY cycle_id
