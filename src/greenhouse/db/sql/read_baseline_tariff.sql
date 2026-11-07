WITH winning_profile AS (
    SELECT p.tariff_version
    FROM config.tariff_profile p
    WHERE p.data_scope_id = %s
      AND p.is_baseline = true
      AND (p.compartment_id = %s OR p.compartment_id IS NULL)
    ORDER BY (p.compartment_id IS NOT NULL) DESC, p.version DESC, p.tariff_id DESC, p.tariff_version DESC
    LIMIT 1
)
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
FROM winning_profile w
JOIN config.tariff_profile p ON p.tariff_version = w.tariff_version
LEFT JOIN config.tariff_rate r ON r.tariff_version = p.tariff_version
ORDER BY r.component, r.valid_from;
