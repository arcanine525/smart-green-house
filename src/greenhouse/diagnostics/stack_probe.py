"""Only the adapters needed to exercise MQTT -> typed record -> PostgreSQL."""

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from queue import Queue
from threading import Event
from uuid import uuid4

import paho.mqtt.client as mqtt


@dataclass(frozen=True)
class ClimateSample:
    event_id: str
    data_kind: str
    source_observed_at: datetime
    tair_c: float
    co2_ppm: float | None
    ec_drain_ds_m: float
    ph_drain: float


def parse_synthetic_climate(payload: bytes) -> ClimateSample:
    """Validate a small labelled fixture, preserving source wall-clock time."""
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("data_kind") != "synthetic":
        raise ValueError("Spike accepts only labelled synthetic records")
    if not isinstance(value.get("event_id"), str) or not value["event_id"]:
        raise ValueError("event_id must be a nonempty string")
    observed_at = datetime.fromisoformat(value["source_observed_at"])
    if observed_at.tzinfo is not None:
        raise ValueError("Synthetic fixture uses source time without timezone")

    def number(name: str, *, nullable: bool = False) -> float | None:
        raw = value[name]
        if raw is None and nullable:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{name} must be numeric")
        if not math.isfinite(raw):
            raise ValueError(f"{name} must be finite")
        return float(raw)

    return ClimateSample(
        event_id=value["event_id"],
        data_kind=value["data_kind"],
        source_observed_at=observed_at,
        tair_c=number("tair_c"),
        co2_ppm=number("co2_ppm", nullable=True),
        ec_drain_ds_m=number("ec_drain_ds_m"),
        ph_drain=number("ph_drain"),
    )


def mqtt_round_trip(payload: bytes) -> bytes:
    """Wait for SUBACK before publishing QoS 1, then return received bytes.

    This checks connectivity, not durable collector ACK or exactly-once delivery.
    Each call owns its clients and topic, so healthchecks cannot satisfy it.
    """
    topic = f"spike/synthetic/{uuid4().hex}"
    received = Queue()
    subscribed = Event()
    publisher_ready = Event()
    failures = Queue()

    def subscribe(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            failures.put(f"MQTT subscribe connection failed: {reason_code}")
            return
        result, _ = client.subscribe(topic, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            failures.put(f"MQTT subscribe failed: {result}")

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        if any(code.is_failure for code in reason_codes):
            failures.put(f"MQTT SUBACK failed: {reason_codes}")
        else:
            subscribed.set()

    def on_publish_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            failures.put(f"MQTT publish connection failed: {reason_code}")
        else:
            publisher_ready.set()

    subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    subscriber.on_connect = subscribe
    subscriber.on_subscribe = on_subscribe
    subscriber.on_message = lambda client, userdata, message: received.put(message.payload)
    publisher.on_connect = on_publish_connect
    host = os.environ.get("MQTT_HOST", "broker")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    try:
        subscriber.connect(host, port, keepalive=30)
        subscriber.loop_start()
        if not subscribed.wait(timeout=10):
            raise TimeoutError(f"No successful SUBACK: {list(failures.queue)}")
        publisher.connect(host, port, keepalive=30)
        publisher.loop_start()
        if not publisher_ready.wait(timeout=10):
            raise TimeoutError(f"No successful MQTT connection: {list(failures.queue)}")
        published = publisher.publish(topic, payload, qos=1, retain=False)
        published.wait_for_publish(timeout=10)
        if not published.is_published():
            raise TimeoutError("No publisher PUBACK")
        return received.get(timeout=10)
    finally:
        for client in (publisher, subscriber):
            client.disconnect()
            client.loop_stop()
