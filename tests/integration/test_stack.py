"""Real broker/database/JVM tests. All records here are synthetic, not AGC."""

from dataclasses import astuple
from datetime import datetime
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from pyspark.sql import SparkSession
from pyspark.sql.types import DoubleType, StringType, StructField, StructType, TimestampNTZType
import pytest

from greenhouse.diagnostics.stack_probe import ClimateSample, mqtt_round_trip, parse_synthetic_climate


@pytest.fixture(scope="module")
def database():
    # libpq reads PG* env vars; Compose waits for pg_isready beforehand.
    with psycopg.connect(connect_timeout=10, autocommit=True) as connection:
        connection.execute("CREATE SCHEMA IF NOT EXISTS spike_staging")
        for table in ("mqtt_climate", "spark_climate"):
            connection.execute(
                sql.SQL("""
                    CREATE TABLE IF NOT EXISTS spike_staging.{} (
                        event_id text PRIMARY KEY,
                        data_kind text NOT NULL CHECK (data_kind = 'synthetic'),
                        source_observed_at timestamp without time zone NOT NULL,
                        tair_c double precision NOT NULL,
                        co2_ppm double precision,
                        ec_drain_ds_m double precision NOT NULL,
                        ph_drain double precision NOT NULL
                    )
                """).format(sql.Identifier(table))
            )
        yield connection


def synthetic_payload(**changes):
    value = {
        "event_id": f"synthetic-mqtt-{uuid4().hex}",
        "data_kind": "synthetic",
        "source_observed_at": "2000-01-01T00:00:00",
        "tair_c": 20.5,
        "co2_ppm": 400,
        "ec_drain_ds_m": 3.25,
        "ph_drain": 6.2,
    }
    value.update(changes)
    return json.dumps(value).encode()


def test_mqtt_typed_psycopg_round_trip(database):
    payload = synthetic_payload()
    received = mqtt_round_trip(payload)
    assert received == payload
    sample = parse_synthetic_climate(received)
    assert isinstance(sample, ClimateSample)
    assert sample.source_observed_at == datetime(2000, 1, 1)
    assert sample.tair_c == 20.5
    assert sample.co2_ppm == 400.0
    database.execute(
        "INSERT INTO spike_staging.mqtt_climate VALUES (%s,%s,%s,%s,%s,%s,%s)",
        astuple(sample),
    )
    row = database.execute(
        "SELECT * FROM spike_staging.mqtt_climate WHERE event_id = %s", (sample.event_id,)
    ).fetchone()
    assert row == (sample.event_id, "synthetic", datetime(2000, 1, 1), 20.5, 400.0, 3.25, 6.2)
    database.execute("DELETE FROM spike_staging.mqtt_climate WHERE event_id = %s", (sample.event_id,))
    print("MQTT QoS1 -> typed ClimateSample -> psycopg INSERT/SELECT: PASS")


@pytest.mark.parametrize(
    "changes",
    [
        {"data_kind": "historical"},
        {"tair_c": float("nan")},
        {"tair_c": True},
        {"source_observed_at": "2000-01-01T00:00:00+00:00"},
    ],
)
def test_synthetic_parser_rejects_invalid_input(changes):
    with pytest.raises(ValueError):
        parse_synthetic_climate(synthetic_payload(**changes))


def test_synthetic_parser_preserves_zero_and_missing():
    sample = parse_synthetic_climate(synthetic_payload(tair_c=0, co2_ppm=None))
    assert sample.tair_c == 0.0
    assert sample.co2_ppm is None
    assert sample.source_observed_at.tzinfo is None


def test_spark_csv_jdbc_round_trip(database):
    database.execute("TRUNCATE spike_staging.spark_climate")
    jar = Path(os.environ["JDBC_JAR"])
    assert jar.is_file(), "JDBC jar must already be packaged in the image"
    schema = StructType([
        StructField("event_id", StringType(), False),
        StructField("data_kind", StringType(), False),
        StructField("source_observed_at", TimestampNTZType(), False),
        StructField("tair_c", DoubleType(), False),
        StructField("co2_ppm", DoubleType(), True),
        StructField("ec_drain_ds_m", DoubleType(), False),
        StructField("ph_drain", DoubleType(), False),
    ])
    spark = (
        SparkSession.builder.master("local[2]")
        .appName("synthetic-greenhouse-stack-spike")
        .config("spark.jars", str(jar))
        .config("spark.ui.enabled", "false")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.sql.shuffle.partitions", "2")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    try:
        frame = (
            spark.read.schema(schema)
            .option("header", "true")
            .option("mode", "FAILFAST")
            .option("timestampNTZFormat", "yyyy-MM-dd'T'HH:mm:ss")
            .csv(str(Path(__file__).parents[1] / "fixtures" / "synthetic_climate.csv"))
        )
        properties = {
            "driver": "org.postgresql.Driver",
            "user": os.environ["PGUSER"],
            "password": os.environ["PGPASSWORD"],
        }
        url = f"jdbc:postgresql://{os.environ['PGHOST']}:{os.environ['PGPORT']}/{os.environ['PGDATABASE']}"
        frame.write.jdbc(url, "spike_staging.spark_climate", mode="append", properties=properties)
        rows = database.execute(
            "SELECT * FROM spike_staging.spark_climate ORDER BY event_id"
        ).fetchall()
        # Independent literal oracle includes zero, NULL and source-time values.
        assert rows == [
            ("synthetic-csv-001", "synthetic", datetime(2000, 1, 1), 20.5, 400.0, 3.25, 6.2),
            ("synthetic-csv-002", "synthetic", datetime(2000, 1, 1, 0, 5), 0.0, 0.0, 2.5, 6.0),
            ("synthetic-csv-003", "synthetic", datetime(2000, 1, 1, 0, 10), 25.0, None, 4.0, 6.5),
        ]
        assert spark.read.jdbc(url, "spike_staging.spark_climate", properties=properties).count() == 3
        assert spark.sparkContext.master == "local[2]"
        print(f"Spark {spark.version} local[2] CSV -> JDBC -> PostgreSQL + psycopg/JDBC query: PASS (3 rows)")
    finally:
        spark.stop()
