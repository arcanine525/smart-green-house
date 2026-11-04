"""Bounded local Spark staging. No Spark/JDBC writes to public tables."""
from collections import defaultdict
from decimal import Decimal
from uuid import uuid4

from psycopg.rows import dict_row

from greenhouse.db.loader import digest_file
from greenhouse.db.migrate import sql
from greenhouse.db.pipeline import encode, fail, membership, read_workflow, save_artifact


def stage_candidates(conn, workflow, target, *, fault=None):
    from pyspark.sql import SparkSession
    from pyspark.sql.types import StructType, StructField, StringType, BooleanType
    pending = target.with_name('.pending-' + target.name)
    pending.mkdir()
    run = read_workflow(conn, workflow)
    with conn.transaction():
        counts = membership(conn, workflow)
        with conn.cursor(name='spark_input_' + uuid4().hex, row_factory=dict_row) as cursor:
            cursor.itersize = 1000
            cursor.execute(sql('spark_input'), (workflow,))
            with (pending / 'input.jsonl').open('w') as stream:
                for row in cursor:
                    stream.write(encode(row) + '\n')
    # Input timestamps stay exact source-naive TEXT. No JVM timezone cast or
    # decimal precision reduction. Numeric profiles are computed with PG numeric.
    schema = StructType([StructField(name, StringType(), False) for name in
                         ('revision_id', 'source_record_id', 'record_type', 'cycle_id', 'compartment_id', 'observed_at', 'content_hash')]
                        + [StructField('is_close_boundary', BooleanType(), False)])
    spark = None
    try:
        spark = (SparkSession.builder.master('local[1]').appName('greenhouse-' + workflow)
                 .config('spark.driver.memory', '768m').config('spark.sql.shuffle.partitions', '4')
                 .config('spark.default.parallelism', '1').config('spark.sql.adaptive.enabled', 'false')
                 .config('spark.ui.enabled', 'false').config('spark.driver.maxResultSize', '64m')
                 .config('spark.sql.session.timeZone', 'UTC').getOrCreate())
        spark.sparkContext.setLogLevel('WARN')
        frame = spark.read.schema(schema).json(str(pending / 'input.jsonl'))
        frame.write.mode('errorifexists').option('maxRecordsPerFile', 50000).parquet(str(pending / 'candidate.parquet'))
        if spark.read.parquet(str(pending / 'candidate.parquet')).count() != counts['member_count']:
            raise ValueError('Spark output count mismatch')
        fail('spark-written', fault)
    finally:
        if spark is not None:
            spark.stop()
    summary = defaultdict(lambda: {'observed_count': 0, 'null_count': 0, 'missing_count': 0, 'invalid_count': 0, 'suspect_count': 0, 'accepted_count': 0, 'applicability_unknown': 0, 'days_observed': 0, 'days_expected': 0, 'min_observed': None, 'max_observed': None})
    with conn.transaction(), conn.cursor(name='profile_' + uuid4().hex, row_factory=dict_row) as cursor:
        cursor.itersize = 1000
        cursor.execute(sql('pipeline_profile'), (workflow, run['versions']['quality_policy'], workflow))
        with (pending / 'profile-days.jsonl').open('w') as stream:
            for row in cursor:
                stream.write(encode(row) + '\n')
                item = summary[(row['compartment_id'], row['measure_code'])]
                for field in ('observed_count', 'null_count', 'missing_count', 'invalid_count', 'suspect_count', 'accepted_count', 'applicability_unknown'):
                    item[field] += row[field]
                item['days_observed'] += bool(row['observed_count'])
                item['days_expected'] += 1
                for field, choose in (('min_observed', min), ('max_observed', max)):
                    if row[field] is not None:
                        value = Decimal(row[field])
                        item[field] = value if item[field] is None else choose(item[field], value)
    (pending / 'profile-summary.json').write_text(encode({'kind': 'initial_profile_not_kpi', 'limits': 'Source-naive half-open cycle; closing rows excluded. Min/max include suspect/invalid values; quality separate. Event-grain harvest has no expected daily count. No area conversion, interpolation or agronomic judgement.', 'measures': [{'compartment_id': k[0], 'measure_code': k[1], **v} for k, v in sorted(summary.items())]}) + '\n')
    save_artifact(pending, counts | {'rows_in': counts['member_count'], 'rows_out': counts['member_count'], 'spark': {'master': 'local[1]', 'driver_memory': '768m', 'shuffle_partitions': 4}, 'profile': 'profile-summary.json'})
    fail('spark-before-rename', fault)
    # On one output filesystem. No manifest at the final path before this rename.
    pending.rename(target)
    return {'folder': str(target), 'checksum': digest_file(target / 'manifest.json')}
