SELECT * FROM core.explain_measure
WHERE data_scope_id=%s AND compartment_id=%s AND observed_at::date=%s AND measure_code=%s
ORDER BY observed_at,revision_id LIMIT %s
