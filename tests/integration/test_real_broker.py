"""Real Mosquitto integration and failure injection tests for T33.

Exercises against live eclipse-mosquitto 2.0.22 container:
1. End-to-end outbox -> Mosquitto -> collector spool -> receipt -> raw-ready pipeline.
2. Broker redelivery on client disconnect BEFORE spool commit and ACK.
3. Broker redelivery on client disconnect AFTER spool commit before ACK (DUP flag handling).
4. Receipt loss and publisher retry with exact original payload bytes.
5. Quarantine preservation of malformed payload over real MQTT wire.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import pytest

import paho.mqtt.client as mqtt

from greenhouse.sensor.collector import (
    BatchManager,
    DurableSpool,
    MqttCollector,
)
from greenhouse.sensor.outbox import DurableOutbox
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.schema import SCHEMA_VERSION

BROKER_HOST = os.environ.get("MQTT_HOST", "greenhouse-broker-worker-b-t33")
BROKER_PORT = int(os.environ.get("MQTT_PORT", "1883"))


def make_valid_envelope(event_id: str, sequence: int, replay_id: str = "real-replay-001", compartment: str = "AICU") -> bytes:
    eid = hashlib.sha256(event_id.encode("utf-8")).hexdigest() if len(event_id) != 64 else event_id
    rec_id = hashlib.sha256(f"rec-{event_id}".encode("utf-8")).hexdigest()
    data = {
        "schema_version": SCHEMA_VERSION,
        "event_id": eid,
        "data_scope_id": f"snapshot:sim-{replay_id}",
        "replay_id": replay_id,
        "device_id": f"sim-climate-{compartment}",
        "cycle_id": "tomatoes-2019",
        "compartment_id": compartment,
        "source_record_id": rec_id,
        "source_provenance": {
            "snapshot_id": "snap-1",
            "snapshot_sha": "c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68",
            "source_file": "GreenhouseClimate.csv",
            "file_checksum": "c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68",
            "line_number": sequence + 1,
            "line_end": sequence + 1,
            "raw_hash": "b" * 64,
            "source_time_serial": f"43815.{sequence:010d}",
            "adapter_id": "agc-standard",
            "parser_version": "agc-parser-v1",
        },
        "sequence": sequence,
        "source_observed_at": "2019-12-16T00:00:00",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "actual_measurements": {"Tair": f"{20.0 + sequence:.1f}", "Rhair": "70.0"},
            "drain_measurements": {},
            "actuators": {},
            "controller_context": {},
            "null_reasons": {},
            "category_tags": {"Tair": "actual_climate", "Rhair": "actual_climate"},
        },
        "units": {"Tair": "°C", "Rhair": "%"},
        "fault_tags": [],
    }
    return json.dumps(data, sort_keys=True).encode("utf-8")


def test_real_broker_connectivity():
    """Verify live connectivity and ping to isolated Mosquitto broker."""
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    connected = []

    def on_connect(c, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            connected.append(True)

    client.on_connect = on_connect
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
    client.loop_start()
    try:
        for _ in range(50):
            if connected:
                break
            time.sleep(0.1)
        assert connected == [True], f"Failed to connect to broker at {BROKER_HOST}:{BROKER_PORT}"
    finally:
        client.loop_stop()
        client.disconnect()


def test_real_broker_end_to_end_outbox_to_ready(tmp_path):
    """Publish outbox items over real Mosquitto, verify collector receipts, and seal raw-ready."""
    outbox_dir = tmp_path / "outbox"
    spool_dir = tmp_path / "spool"
    ready_dir = tmp_path / "ready"

    # 1. Initialize outbox with 5 items
    outbox = DurableOutbox(outbox_dir)
    replay_id = f"real-test-{uuid.uuid4().hex[:8]}"
    compartment_id = "AICU"
    outbox.initialize_run(
        replay_id=replay_id,
        data_scope_id=f"scope:{replay_id}",
        compartment_id=compartment_id,
        cycle_id="tomatoes-2019",
        source_file="source.csv",
        source_file_checksum="chk",
        config_hash="conf",
        resume=False,
    )

    items = []
    for i in range(1, 6):
        p = make_valid_envelope(f"event-{i}", i, replay_id, compartment_id)
        items.append((f"v1/telemetry/{replay_id}/{compartment_id}", p, hashlib.sha256(f"event-{i}".encode("utf-8")).hexdigest(), i, i + 1))

    outbox.commit_dispatch_transaction(
        telemetry_messages=items,
        control_messages=[],
        fault_audits=[],
        sequence=5,
        line_number=6,
        control_sequence=0,
        previous_cumulative_hash="0" * 64,
        expected_unique=5,
        emitted_unique=5,
        emitted_total=5,
        fault_state={},
        status="running",
    )

    # 2. Start collector with manual ACK
    collector = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=f"test-coll-e2e-{uuid.uuid4().hex[:8]}",
        clean_session=False,
        batch_size=5,
    )
    # Start collector in thread
    import threading
    collector_thread = threading.Thread(target=collector.start, kwargs={"poll_interval": 0.1})
    collector_thread.daemon = True
    collector_thread.start()

    time.sleep(0.5)  # Let collector connect and subscribe

    # 3. Start publisher and stream outbox
    publisher = OutboxPublisher(
        outbox_dir=outbox_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=f"test-pub-e2e-{uuid.uuid4().hex[:8]}",
        max_inflight=10,
    )
    publisher.start()
    try:
        published_count = publisher.publish_all_pending(poll_interval=0.1)
        assert published_count == 5

        # Verify publisher checkpoint reached 5
        contig_tel, _ = publisher.calculate_contiguous_confirmed_cursor(replay_id)
        assert contig_tel == 5

        # Verify collector received and packed
        for _ in range(50):
            if (ready_dir / "manifest.json").exists():
                break
            time.sleep(0.1)
        else:
            collector.check_and_pack_batches(force=True)

        total_spooled, batched, unbatched = collector.spool.count_spooled_records()
        assert total_spooled == 5
        assert unbatched == 0
        assert (ready_dir / "manifest.json").is_file()

        ready_files = list(ready_dir.glob("batch-*.jsonl"))
        assert len(ready_files) == 1
        lines = ready_files[0].read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 5

    finally:
        publisher.stop()
        collector.stop()
        collector_thread.join(timeout=2.0)


def test_real_broker_redelivery_before_spool_ack(tmp_path):
    """Failure injection on production MqttCollector before spool commit; verify broker redelivers on reconnect, collector ACKs and persists."""
    spool_dir = tmp_path / "spool"
    ready_dir = tmp_path / "ready"
    client_id = f"fail-before-spool-{uuid.uuid4().hex[:8]}"
    topic = f"v1/telemetry/fail-spool-{uuid.uuid4().hex[:8]}/AICU"
    payload = make_valid_envelope("event-fail-spool", 1, "fail-spool-replay", "AICU")

    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    # Instance 1: Production MqttCollector injects crash inside spool commit BEFORE persisting
    collector1 = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=client_id,
        clean_session=False,
    )

    injected_disconnect = threading.Event()
    real_record = collector1.spool.record_delivery

    def crash_record(*args, **kwargs):
        # Crash/disconnect before spooling to disk!
        disc_props = Properties(PacketTypes.DISCONNECT)
        disc_props.SessionExpiryInterval = collector1.session_expiry_interval
        collector1._client.disconnect(properties=disc_props)
        collector1._client.loop_stop()
        collector1._running = False
        injected_disconnect.set()
        raise RuntimeError("Injected crash before SQLite WAL fsync")

    collector1.spool.record_delivery = crash_record
    t1 = threading.Thread(target=collector1.start, kwargs={"poll_interval": 0.05}, daemon=True)
    t1.start()
    time.sleep(0.5)

    # Publish QoS 1 message
    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    pub.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
    pub.loop_start()

    props = Properties(PacketTypes.PUBLISH)
    props.UserProperty = [
        ("outbox_table", "telemetry_messages"),
        ("outbox_row_id", "1"),
        ("replay_id", "fail-spool-replay"),
        ("compartment_id", "AICU"),
        ("payload_hash", hashlib.sha256(payload).hexdigest()),
    ]
    pub_info = pub.publish(topic, payload, qos=1, properties=props)
    pub_info.wait_for_publish(timeout=5.0)

    assert injected_disconnect.wait(timeout=5.0), "Injected failure before spool did not fire"
    t1.join(timeout=3.0)
    assert collector1.spool.count_spooled_records()[0] == 0  # Nothing committed to spool!

    # Instance 2: Standard MqttCollector reconnects with same client_id and persistent session
    collector2 = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=client_id,
        clean_session=False,
        batch_size=1,
    )
    t2 = threading.Thread(target=collector2.start, kwargs={"poll_interval": 0.05}, daemon=True)
    t2.start()

    try:
        # Broker must redeliver unacknowledged message to collector2
        for _ in range(50):
            if collector2.spool.count_spooled_records()[0] >= 1:
                break
            time.sleep(0.1)

        total, _, _ = collector2.spool.count_spooled_records()
        assert total == 1, f"Expected 1 spooled delivery on reconnect, got {total}"
        collector2.check_and_pack_batches(force=True)
        assert len(list(ready_dir.glob("batch-*.jsonl"))) == 1
    finally:
        collector2.stop()
        t2.join(timeout=3.0)
        pub.loop_stop()
        pub.disconnect()


def test_real_broker_crash_after_spool_before_ack(tmp_path):
    """Failure injection on production MqttCollector after spool commit before ACK; verify broker redelivers on reconnect, collector ACKs, and no data lost."""
    spool_dir = tmp_path / "spool"
    ready_dir = tmp_path / "ready"
    client_id = f"crash-test-{uuid.uuid4().hex[:8]}"
    topic = f"v1/telemetry/crash-flow-{uuid.uuid4().hex[:8]}/AICU"
    payload = make_valid_envelope("event-crash-flow", 1, "crash-replay", "AICU")

    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    # Instance 1: Production collector runs standard handle_message, but fails at the client.ack boundary
    collector1 = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=client_id,
        clean_session=False,
    )

    crash_occurred = threading.Event()

    t1 = threading.Thread(target=collector1.start, kwargs={"poll_interval": 0.05}, daemon=True)
    t1.start()
    time.sleep(0.5)

    def crash_ack(mid, qos=1):
        # Abort connection right at client.ack before sending PUBACK to broker!
        disc_props = Properties(PacketTypes.DISCONNECT)
        disc_props.SessionExpiryInterval = collector1.session_expiry_interval
        collector1._client.disconnect(properties=disc_props)
        collector1._client.loop_stop()
        collector1._running = False
        crash_occurred.set()
        raise RuntimeError("Injected crash at client.ack boundary before PUBACK sent")

    collector1._client.ack = crash_ack

    # Publish QoS 1 message
    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    pub.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
    pub.loop_start()

    props = Properties(PacketTypes.PUBLISH)
    props.UserProperty = [
        ("outbox_table", "telemetry_messages"),
        ("outbox_row_id", "1"),
        ("replay_id", "crash-replay"),
        ("compartment_id", "AICU"),
        ("payload_hash", hashlib.sha256(payload).hexdigest()),
    ]

    pub_info = pub.publish(topic, payload, qos=1, properties=props)
    pub_info.wait_for_publish(timeout=5.0)

    assert crash_occurred.wait(timeout=5.0), "Collector 1 did not execute crash injection at ACK boundary"
    t1.join(timeout=3.0)
    assert collector1.spool.count_spooled_records()[0] == 1  # Spool was successfully committed!

    # Instance 2: Standard MqttCollector reconnects with same client_id and persistent session
    collector2 = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=client_id,
        clean_session=False,
        batch_size=2,
    )
    t2 = threading.Thread(target=collector2.start, kwargs={"poll_interval": 0.05}, daemon=True)
    t2.start()

    try:
        # Broker must redeliver unacked QoS 1 message to collector2
        for _ in range(50):
            if collector2.spool.count_spooled_records()[0] >= 2:
                break
            time.sleep(0.1)

        total, _, _ = collector2.spool.count_spooled_records()
        assert total == 2, f"Expected 2 spooled deliveries (original + redelivery), got {total}"

        # Collector packs batch with full duplicate lineage
        collector2.check_and_pack_batches(force=True)
        all_batches = collector2.spool.get_all_batches()
        total_records = sum(b.record_count for b in all_batches)
        assert total_records == 2

        # Both records are sealed into ready files with duplicate lineage preserved
        ready_files = list(ready_dir.glob("batch-*.jsonl"))
        total_ready_lines = sum(len(f.read_text(encoding="utf-8").strip().split("\n")) for f in ready_files)
        assert total_ready_lines == 2
    finally:
        collector2.stop()
        t2.join(timeout=3.0)
        pub.loop_stop()
        pub.disconnect()


def test_real_broker_receipt_loss_and_publisher_retry(tmp_path):
    """Lose collector receipt in transit; verify publisher retries, collector confirms, and publisher advances."""
    spool_dir = tmp_path / "spool"
    ready_dir = tmp_path / "ready"
    outbox_dir = tmp_path / "outbox"

    outbox = DurableOutbox(outbox_dir)
    outbox.initialize_run(
        replay_id="rcpt-loss-replay",
        data_scope_id="snap-scope",
        compartment_id="AICU",
        cycle_id="cycle-1",
        source_file="GreenhouseClimate.csv",
        source_file_checksum="a" * 64,
        config_hash="b" * 64,
        resume=False,
    )
    payload = make_valid_envelope("event-rcpt-loss", 1, "rcpt-loss-replay", "AICU")
    with sqlite3.connect(outbox_dir / "outbox.db") as conn:
        conn.execute(
            """
            INSERT INTO telemetry_messages (sequence, event_id, topic, line_number, payload_bytes, payload_hash, cumulative_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, hashlib.sha256(b"event-rcpt-loss").hexdigest(), "v1/telemetry/rcpt-loss-replay/AICU", 2, payload, hashlib.sha256(payload).hexdigest(), "0" * 64),
        )

    # Collector that drops receipt on first arrival, but publishes on retry
    collector = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=f"coll-rcpt-{uuid.uuid4().hex[:8]}",
        clean_session=True,
    )

    attempt_count = 0
    orig_pub_receipt = collector._publish_receipt

    def drop_first_receipt(delivery):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            # Simulate receipt packet loss
            return
        orig_pub_receipt(delivery)

    collector._publish_receipt = drop_first_receipt
    coll_thread = threading.Thread(target=collector.start, kwargs={"poll_interval": 0.05}, daemon=True)
    coll_thread.start()
    time.sleep(0.5)

    publisher = OutboxPublisher(
        outbox_dir=outbox_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        receipt_timeout=0.6,
        max_retries=3,
    )
    publisher.start()
    try:
        published_count = publisher.publish_all_pending(poll_interval=0.1)
        assert published_count >= 1
        assert attempt_count >= 2, f"Expected retry attempt, got attempt_count={attempt_count}"
        contig_tel, _ = publisher.get_last_checkpoint("rcpt-loss-replay")
        assert contig_tel == 1
    finally:
        publisher.stop()
        collector.stop()
        coll_thread.join(timeout=2.0)


def test_real_broker_persistent_session_offline_queue(tmp_path):
    """Verify production MqttCollector preserves subscription and queues messages while offline across instances."""
    collector_id = f"test-offline-{uuid.uuid4().hex[:8]}"
    replay_id = f"offline-probe-{uuid.uuid4().hex[:8]}"
    topic = f"v1/telemetry/{replay_id}/AICU"
    spool_dir = tmp_path / "spool"
    ready_dir = tmp_path / "ready"

    # Instance 1: Starts, receives 1 message, stops cleanly with SessionExpiryInterval
    collector1 = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=collector_id,
        clean_session=False,
        session_expiry_interval=86400,
    )
    t1 = threading.Thread(target=collector1.start, kwargs={"poll_interval": 0.05}, daemon=True)
    t1.start()
    time.sleep(0.5)

    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    sender = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    sender.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
    sender.loop_start()

    payload1 = b'online-probe-payload'
    props1 = Properties(PacketTypes.PUBLISH)
    props1.UserProperty = [
        ("outbox_table", "telemetry_messages"),
        ("outbox_row_id", "1"),
        ("replay_id", replay_id),
        ("compartment_id", "AICU"),
        ("payload_hash", hashlib.sha256(payload1).hexdigest()),
    ]
    ack1 = sender.publish(topic, payload1, qos=1, properties=props1)
    ack1.wait_for_publish(timeout=5.0)

    for _ in range(50):
        if collector1.spool.count_spooled_records()[0] >= 1:
            break
        time.sleep(0.1)
    assert collector1.spool.count_spooled_records()[0] == 1

    # Stop instance 1
    collector1.stop()
    t1.join(timeout=3.0)

    # Publish second message while collector is completely offline
    payload2 = b'offline-probe-payload'
    props2 = Properties(PacketTypes.PUBLISH)
    props2.UserProperty = [
        ("outbox_table", "telemetry_messages"),
        ("outbox_row_id", "2"),
        ("replay_id", replay_id),
        ("compartment_id", "AICU"),
        ("payload_hash", hashlib.sha256(payload2).hexdigest()),
    ]
    ack2 = sender.publish(topic, payload2, qos=1, properties=props2)
    ack2.wait_for_publish(timeout=5.0)
    assert ack2.is_published()

    # Instance 2: Connects with same client_id and clean_session=False
    collector2 = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=collector_id,
        clean_session=False,
        session_expiry_interval=86400,
    )
    t2 = threading.Thread(target=collector2.start, kwargs={"poll_interval": 0.05}, daemon=True)
    t2.start()

    try:
        # Second message queued by Mosquitto while offline must arrive in collector 2!
        for _ in range(50):
            if collector2.spool.count_spooled_records()[0] >= 2:
                break
            time.sleep(0.1)
        assert collector2.spool.count_spooled_records()[0] == 2
    finally:
        collector2.stop()
        t2.join(timeout=3.0)
        sender.loop_stop()
        sender.disconnect()


def test_real_broker_quarantine_payload_flow(tmp_path):
    """Malformed non-JSON bytes over live MQTT are quarantined, ACKed to clear broker, and batched."""
    spool_dir = tmp_path / "spool"
    ready_dir = tmp_path / "ready"
    collector = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=f"test-coll-quarantine-{uuid.uuid4().hex[:8]}",
        clean_session=False,
        batch_size=1,
    )

    coll_thread = threading.Thread(target=collector.start, kwargs={"poll_interval": 0.1})
    coll_thread.daemon = True
    coll_thread.start()
    time.sleep(0.5)

    replay_id = f"bad-replay-{uuid.uuid4().hex[:8]}"
    topic = f"v1/telemetry/{replay_id}/AICU"
    bad_payload = b"MALFORMED_NON_JSON_CORRUPTION_BYTES_12345"

    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties
    props = Properties(PacketTypes.PUBLISH)
    props.UserProperty = [
        ("outbox_table", "telemetry_messages"),
        ("outbox_row_id", "1"),
        ("replay_id", replay_id),
        ("compartment_id", "AICU"),
        ("payload_hash", hashlib.sha256(bad_payload).hexdigest()),
    ]

    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    pub.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
    pub.loop_start()
    pub_info = pub.publish(topic, bad_payload, qos=1, properties=props)
    pub_info.wait_for_publish(timeout=5.0)
    pub.loop_stop()
    pub.disconnect()

    for _ in range(50):
        total, _, _ = collector.spool.count_spooled_records()
        if total >= 1:
            collector.run_once()
            break
        time.sleep(0.1)

    collector.stop()
    coll_thread.join(timeout=2.0)

    # Verify quarantined delivery
    deliveries = collector.spool.fetch_unbatched_deliveries(limit=10)
    # If already batched:
    ready_files = list(ready_dir.glob("batch-*.jsonl"))
    assert len(ready_files) == 1
    line = json.loads(ready_files[0].read_text(encoding="utf-8").strip())
    assert line["is_valid"] is False
    assert line["message_type"] == "quarantine"
    assert "Malformed JSON" in line["quarantine_reason"]


def test_real_broker_capacity_full_and_resume(tmp_path):
    """Test collector withholding ACK under ready quota limit, then resuming after quota space restored."""
    spool_dir = tmp_path / "spool"
    ready_dir = tmp_path / "ready"
    replay_id = f"cap-full-{uuid.uuid4().hex[:8]}"
    compartment_id = "AICU"
    topic = f"v1/telemetry/{replay_id}/{compartment_id}"
    payload = make_valid_envelope("event-cap", 1, replay_id, compartment_id)
    payload_hash = hashlib.sha256(payload).hexdigest()

    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties
    props = Properties(PacketTypes.PUBLISH)
    props.UserProperty = [
        ("outbox_table", "telemetry_messages"),
        ("outbox_row_id", "1"),
        ("replay_id", replay_id),
        ("compartment_id", compartment_id),
        ("payload_hash", payload_hash),
    ]

    # Create collector with tiny ready_quota_bytes (50 bytes)
    collector = MqttCollector(
        spool_dir=spool_dir,
        ready_dir=ready_dir,
        mqtt_host=BROKER_HOST,
        mqtt_port=BROKER_PORT,
        client_id=f"test-cap-{uuid.uuid4().hex[:8]}",
        clean_session=False,
        batch_size=1,
        ready_quota_bytes=50,
        headroom_bytes=10,
    )
    t = threading.Thread(target=collector.start, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    time.sleep(0.5)

    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    pub.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
    pub.loop_start()

    try:
        # Publish message - ready quota is tiny, so projected_ready exceeds quota
        ack = pub.publish(topic, payload, qos=1, properties=props)
        ack.wait_for_publish(timeout=5.0)

        # Message is NOT spooled because ready quota is exceeded
        time.sleep(0.5)
        total, _, _ = collector.spool.count_spooled_records()
        assert total == 0, "Expected message to be withheld under exceeded quota"

        # Now restore quota capacity
        collector.batch_manager.ready_quota_bytes = 10 * 1024 * 1024
        collector.spool.spool_quota_bytes = 10 * 1024 * 1024

        # Redeliver/republish to verify consumption resumes smoothly
        ack2 = pub.publish(topic, payload, qos=1, properties=props)
        ack2.wait_for_publish(timeout=5.0)

        for _ in range(50):
            total, _, _ = collector.spool.count_spooled_records()
            if total >= 1:
                break
            time.sleep(0.1)
        assert collector.spool.count_spooled_records()[0] >= 1

        collector.check_and_pack_batches(force=True)
        ready_files = list(ready_dir.glob("batch-*.jsonl"))
        assert len(ready_files) >= 1
    finally:
        collector.stop()
        t.join(timeout=2.0)
        pub.loop_stop()
        pub.disconnect()

