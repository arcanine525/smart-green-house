"""Mandatory drain bundle hook on the coordinator's connection/transaction.

Identity mirrors the climate/water/harvest bundles (db.climate_mart,
db.irrigation_mart, db.harvest_mart): one set-based SQL statement, receipt
version checked by the 010 seal trigger. Runs after the climate hook so
every bundle exists before the seal validates them. The bundle freezes ALL
drain slots of the publication (EC/pH values in the exact
'drain_process_computer' context, the frozen per-measure quality for the
policy's drain measures, source revision identity) — runtime BR-03
evaluation reads only this, never the current raw/core.
"""
from greenhouse.db.migrate import sql

BUNDLE_VERSION = 'drain-mart-v1'


def materialize_drain(connection, publication_id, workflow_id):
    slot_count, cycle_count, seconds = connection.execute(
        sql('materialize_drain'),
        (publication_id,) * 4 + (BUNDLE_VERSION,)).fetchone()
    return {'slots': slot_count, 'cycles': cycle_count, 'seconds': str(seconds)}
