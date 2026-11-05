-- Immutable version rows authorized by THIS publication's frozen pins.
-- No current application/assumption enabled flag or latest metadata fallback.
SELECT b.*,a.statement AS assumption_statement,a.evidence_reference AS assumption_evidence
FROM meta.publication_basis p JOIN config.area_basis_version b USING(basis_version_id)
LEFT JOIN config.basis_assumption_version a ON a.assumption_id=b.assumption_id AND a.version=b.assumption_version
WHERE p.publication_id=%s AND p.data_scope_id=%s AND p.compartment_id=%s
 AND p.measure_code='ProdA' AND p.analysis_context='shared_analysis'
ORDER BY b.valid_from,b.basis_version_id
