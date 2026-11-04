SELECT cy.cycle_id,cy.compartment_id,cy.start_source_time,cy.end_source_time_exclusive
FROM mart.dim_climate_cycle cy
WHERE cy.publication_id=%s AND cy.data_scope_id=%s AND cy.cycle_id=%s AND cy.compartment_id=%s
