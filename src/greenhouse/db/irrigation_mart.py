"""Mandatory water bundle hook on the coordinator's existing connection/tx."""
from greenhouse.db.migrate import sql

BUNDLE_VERSION = 'irrigation-mart-v1'


def materialize_irrigation(connection, publication_id, workflow_id):
    # Exactly one set-based statement; publication, dimensions, facts, receipt and
    # caller hooks all roll back with the publication transaction on failure.
    day_count, cycle_count, seconds = connection.execute(sql('materialize_irrigation'),
                                                        (publication_id, publication_id, publication_id, BUNDLE_VERSION)).fetchone()
    return {'days': day_count, 'cycles': cycle_count, 'seconds': str(seconds)}
