"""Backend choice for replay catalog, evaluation and raw-envelope reads."""
from greenhouse.app.runtime import is_clickhouse


def current_replays(connection):
    if is_clickhouse(connection):
        from greenhouse.clickhouse.replay_catalog import current_replays as read
    else:
        from greenhouse.db.replay_catalog import current_replays as read
    return read(connection)


def source_rules(connection,*args):
    if is_clickhouse(connection):
        from greenhouse.clickhouse.replay_catalog import source_rules as read
    else:
        from greenhouse.db.replay_catalog import source_rules as read
    return read(connection,*args)


def latest_quality(connection,metadata,event,*,publication_id):
    if is_clickhouse(connection):
        from greenhouse.clickhouse.replay_catalog import latest_quality as read
        return read(connection,publication_id,event)
    from greenhouse.db.replay_catalog import latest_quality as read
    return read(connection,metadata,event)


def read_publication(connection,publication_id):
    if is_clickhouse(connection):
        from greenhouse.clickhouse.replay_publication import read_publication as read
    else:
        from greenhouse.sensor.replay_publication import read_publication as read
    return read(connection,publication_id)


def evaluate_publication(connection,*args,**kwargs):
    if is_clickhouse(connection):
        from greenhouse.clickhouse.replay_evaluation import evaluate_publication as evaluate
    else:
        from greenhouse.sensor.replay_evaluation import evaluate_publication as evaluate
    return evaluate(connection,*args,**kwargs)


def map_historical_rule(connection,*args):
    if is_clickhouse(connection):
        from greenhouse.clickhouse.replay_evaluation import map_historical_rule as read
    else:
        from greenhouse.sensor.replay_evaluation import map_historical_rule as read
    return read(connection,*args)
