"""Sealed shared energy quantities; one transaction with all other bundles."""
from greenhouse.db.migrate import sql

BUNDLE_VERSION = 'energy-mart-v1'
QUANTITY_VERSION = 'energy-quantity-v1'


def materialize_energy(connection, publication_id, workflow_id):
    rows, cycles, seconds = connection.execute(
        sql('materialize_energy'), (publication_id,) * 3 +
        (BUNDLE_VERSION, QUANTITY_VERSION)).fetchone()
    return {'component_rows': rows, 'cycles': cycles, 'seconds': str(seconds)}
