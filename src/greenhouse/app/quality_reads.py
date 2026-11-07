"""Backend-owned read factories; no formula or SQL in overview rendering."""
from greenhouse.app.runtime import is_clickhouse


def adapter(connection):
    if is_clickhouse(connection):
        from greenhouse.clickhouse import quality_overview
    else:
        from greenhouse.db import quality_overview
    return quality_overview


def measures(connection,*args):return adapter(connection).measures(connection,*args)
def excluded(connection,*args):return adapter(connection).excluded(connection,*args)
def quarantined(connection,*args):return adapter(connection).quarantined(connection,*args)
