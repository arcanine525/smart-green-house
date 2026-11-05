INSERT INTO config.tariff_profile (
    tariff_version, tariff_id, version, data_scope_id, compartment_id,
    currency, source_reference, label, is_baseline
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
