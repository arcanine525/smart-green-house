"""Structured Streaming file source for collector delivery landing.

Backend selection is explicit. Optional replay publications reconcile verified
deliveries and cursor/manifest evidence; a manifest alone never proves complete.
"""
import json
import os
from pathlib import Path

import psycopg
from pyspark.sql import SparkSession
from pyspark.sql.functions import input_file_name

from greenhouse.sensor.stream_store import commit_batch
from greenhouse.sensor.replay_publication import publish_replay


def start_landing(settings, ready_dir, checkpoint_dir, stream_id, generation,
                  *, available_now=False, max_rows_per_batch=50000, max_files_per_batch=20, publication_options=None):
    if publication_options is not None:
        required = {'replay_id', 'scope_id', 'cycle_id', 'compartment_id', 'quality_policy_version'}
        if set(publication_options) != required or not all(publication_options.values()):
            raise ValueError('publication options must explicitly identify replay, scope, cycle, compartment and policy')
    if not stream_id or not generation:
        raise ValueError('explicit stream ID and checkpoint generation are required')
    ready, checkpoint = Path(ready_dir).resolve(), Path(checkpoint_dir).resolve()
    if ready == checkpoint or ready in checkpoint.parents or checkpoint in ready.parents:
        raise ValueError('ready and checkpoint directories must be separate')
    if max_rows_per_batch <= 0 or max_files_per_batch <= 0:
        raise ValueError('batch limits must be positive')
    # A Spark checkpoint is a single stream incarnation. Refuse to attach an
    # existing checkpoint to a different generation or source path.
    checkpoint.mkdir(parents=True, exist_ok=True)
    marker = checkpoint / '.greenhouse-stream-identity.json'
    from greenhouse.clickhouse.stream_store import StreamSettings
    clickhouse=isinstance(settings,StreamSettings)
    if clickhouse:
        if publication_options is None:
            raise ValueError('ClickHouse streaming requires explicit replay publication scope')
        from greenhouse.clickhouse.client import Client
        from greenhouse.clickhouse.stream_store import migrate as migrate_stream
        from greenhouse.clickhouse.replay_publication import migrate as migrate_replay
        from greenhouse.clickhouse.replay_config import read_config
        client=Client(settings.database)
        migrate_stream(client,settings.coordinator);migrate_replay(client,settings.coordinator)
        config,pin=read_config(client,publication_options['scope_id'],settings.config_version)
        if any(config[k]!=v for k,v in publication_options.items()):
            raise ValueError('Stream replay options differ from frozen configuration')
        endpoint=dict(host=settings.database.host,port=settings.database.port,dbname=settings.database.database,user=settings.database.user)
    else:
        endpoint=settings.connection_kwargs()
    identity_data={'stream_id':stream_id,'generation':generation,'ready_dir':str(ready),
        'database':{k:str(endpoint.get(k,'')) for k in ('host','port','dbname','user')}}
    if clickhouse:
        identity_data.update(backend='clickhouse',configuration_scope=publication_options['scope_id'],
            configuration_version=settings.config_version,configuration_hash=pin['content_hash'])
    identity=json.dumps(identity_data,sort_keys=True)
    if not marker.exists() and any(checkpoint.iterdir()):
        raise ValueError('existing checkpoint has no stream identity marker')
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if marker.read_text() != identity:
            raise ValueError('checkpoint belongs to a different stream, generation or source')
    else:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(identity)
            handle.flush()
            os.fsync(handle.fileno())
    spark = SparkSession.builder.appName('greenhouse-sensor-landing').getOrCreate()
    source = (spark.readStream.format('text')
              .option('pathGlobFilter', 'batch-*.jsonl')
              .option('maxFilesPerTrigger', max_files_per_batch)
              .load(str(ready))
              .select('value', input_file_name().alias('source_file')))

    def write_batch(frame, batch_id):
        rows = []
        for row in frame.toLocalIterator():
            if len(rows) >= max_rows_per_batch:
                raise ValueError('batch exceeds max_rows_per_batch; checkpoint was not advanced')
            try:
                value = json.loads(row.value)
            except (json.JSONDecodeError, TypeError):
                value = {'malformed_ready_line': row.value, 'source_file': row.source_file}
            rows.append(value)
        if clickhouse:
            from greenhouse.clickhouse.client import Client
            from greenhouse.clickhouse.replay_publication import commit_and_publish
            commit_and_publish(Client(settings.database),settings.coordinator,stream_id,generation,batch_id,rows,
                config_version=settings.config_version,publication_options=publication_options)
        else:
            with psycopg.connect(**settings.connection_kwargs()) as connection:
                with connection.transaction():
                    commit_batch(connection, stream_id, generation, batch_id, rows)
                    if publication_options is not None:
                        publish_replay(connection, **publication_options)

    writer = (source.writeStream.foreachBatch(write_batch)
              .option('checkpointLocation', str(checkpoint)))
    if available_now:
        writer = writer.trigger(availableNow=True)
    return writer.start()
