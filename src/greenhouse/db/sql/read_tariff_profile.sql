SELECT p.*,
       r.rate_id,
       r.tariff_version AS rate_tariff_version,
       r.component,
       r.currency AS rate_currency,
       r.price_unit,
       r.rate,
       r.valid_from,
       r.valid_to_exclusive,
       r.source_reference AS rate_source_reference
FROM config.tariff_profile p
LEFT JOIN config.tariff_rate r ON r.tariff_version = p.tariff_version
WHERE p.tariff_version = %s
ORDER BY r.component, r.valid_from;
