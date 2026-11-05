"""Read only sealed energy rows; never consult current basis/core quantities."""
from greenhouse.contracts import (
    ASSUMPTION_LABEL, Applicability, BasisStatus, CorrectionRef, CycleWindow, EffectiveAreaBasis,
    EnergyComponent, EnergyQuantity, QualityStatus,
)
from greenhouse.db.energy_mart import BUNDLE_VERSION, QUANTITY_VERSION
from greenhouse.db.query_adapter import PostgresCoreQueryAdapter


class PostgresEnergyQueryAdapter(PostgresCoreQueryAdapter):
    def publication(self, publication_id):
        publication = super().publication(publication_id)
        receipt = self._rows('query_energy_receipt', (publication_id,))
        if len(receipt) != 1 or (receipt[0]['bundle_version'], receipt[0]['quantity_version']) != (BUNDLE_VERSION, QUANTITY_VERSION):
            raise ValueError('Publication lacks the expected energy mart bundle; run a new workflow')
        return publication

    def cycle_window(self, context):
        rows = self._rows('query_energy_cycle', (context.publication_id, context.scope_id,
            context.cycle_id, context.compartment_ids[0]))
        if len(rows) != 1:
            raise ValueError('Cycle/compartment absent from pinned energy publication')
        row = rows[0]
        return CycleWindow(row['cycle_id'], row['compartment_id'],
            row['start_source_time'].date(), row['end_source_time_exclusive'].date())

    def read_energy_quantities(self, context, publication):
        context.matches(publication)
        rows = self._rows('query_energy_quantities', (context.publication_id, context.scope_id,
            context.cycle_id, context.compartment_ids[0], context.start, context.end))
        result = []
        for row in rows:
            basis = None
            if row['basis_version_id'] is not None:
                basis = EffectiveAreaBasis(row['basis_version_id'], row['basis_version'], row['data_scope_id'],
                    row['compartment_id'], row['measure_code'], row['area_m2'], row['basis_kind'],
                    BasisStatus(row['basis_status']), row['basis_valid_from'], row['basis_valid_to_exclusive'],
                    row['basis_evidence'], row['assumption_id'], row['assumption_version'],
                    row['assumption_statement'], row['assumption_evidence'],
                    ASSUMPTION_LABEL if row['basis_status'] == 'assumed_unverified' else 'verified')
            correction = (CorrectionRef(row['correction_id'], row['correction_version'])
                          if row['correction_id'] is not None else None)
            result.append(EnergyQuantity(publication, row['cycle_id'], row['compartment_id'], row['resource_date'],
                EnergyComponent(row['component']), row['source_value'], row['source_unit'],
                QualityStatus(row['quality']), Applicability(row['applicability']), row['reason_code'],
                row['revision_id'], row['logical_record_key'], correction, basis,
                row['normalized_kwh_m2'], row['absolute_source_quantity'], row['absolute_kwh']))
        return tuple(result)
