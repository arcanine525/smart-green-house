"""Mandatory climate bundle hook on the coordinator's connection/transaction.

Identity mirrors the water/harvest bundles (db.irrigation_mart, db.harvest_mart):
one set-based SQL statement, receipt version checked by the 009 seal trigger.
Runs after the harvest hook so every bundle exists before the seal validates
them. The bundle freezes ALL climate slots of the publication (values, all
eight per-measure quality verdicts, source revision identity) — runtime BR-01
evaluation reads only this, neverthe current raw/core.
"""
from greenhouse.db.migrate import sql

BUNDLE_VERSION = 'climate-mart-v1'


def materialize_climate(connection, publication_id, workflow_id):
    slot_count, cycle_count, seconds = connection.execute(
        sql('materialize_climate'),
        (publication_id,) * 4 + (BUNDLE_VERSION,)).fetchone()
    return {'slots': slot_count, 'cycles': cycle_count, 'seconds': str(seconds)}
