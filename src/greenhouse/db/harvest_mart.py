"""Mandatory harvest bundle hook on the coordinator's connection/transaction.

Identity mirrors the water bundle (db.irrigation_mart): one set-based SQL
statement, receipt version checked by the seals. Runs after the water hook so
both bundles exist before either seal trigger validates them.
"""
from greenhouse.db.migrate import sql

BUNDLE_VERSION = 'harvest-mart-v3'


def materialize_harvest(connection, publication_id, workflow_id):
    record_count, cycle_count, seconds = connection.execute(
        sql('materialize_harvest'),
        (publication_id,) * 6 + (BUNDLE_VERSION,)).fetchone()
    return {'records': record_count, 'cycles': cycle_count, 'seconds': str(seconds)}
