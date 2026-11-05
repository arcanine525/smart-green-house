INSERT INTO config.tariff_rate (
    rate_id, tariff_version, component, currency, price_unit,
    rate, valid_from, valid_to_exclusive, source_reference
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
