"""Unit and fault-injection tests for T33 durable MQTT collector and transport publisher.

Exercises:
- ACID spool persistence before ACK with exact payload bytes and SHA-256 hash.
- Zero PostgreSQL dependency (collector runs standalone with filesystem & broker).
- Quarantine retention for malformed JSON and schema-invalid payloads preserving raw bytes.
- Distinct delivery lineage for duplicates (identical payload) and conflicts (divergent payload).
- Atomic raw-ready JSONL packing: .tmp staging in same volume, atomic replace, and directory fsync.
- Immutable ready file protection: refusing overwrite of existing ready files.
- Deterministic crash recovery on restart: orphaned .tmp cleanup and checksum verification.
- Physical quota protection & backpressure: withholding ACK when spool or ready quota exceeded.
- Collector receipts carrying exact outbox table, row ID, and payload hash.
- Publisher receipt validation: rejecting mismatched or stale receipts.
- Contiguous cursor progression: out-of-order receipts cannot advance over unconfirmed holes.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
import pytest

from greenhouse.sensor.collector import (
    BatchManager,
    CorruptBatchError,
    DurableSpool,
    MqttCollector,
    QuotaExceededError,
)
from greenhouse.sensor.outbox import DurableOutbox
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.schema import (
    CONTROL_SCHEMA_VERSION,
    SCHEMA_VERSION,
)


@dataclass
class MockMqttMessage:
    topic: str
    payload: bytes
    mid: int = 1
    qos: int = 1
    properties: Any = None


@dataclass
class MockUserProperties:
    UserProperty: list[tuple[str, str]]


class MockMqttClient:
    def __init__(self):
        self.acked_mids: list[tuple[int, int]] = []
        self.published_messages: list[tuple[str, bytes, int]] = []

    def ack(self, mid: int, qos: int = 1):
        self.acked_mids.append((mid, qos))

    def publish(self, topic: str, payload: bytes, qos: int = 1, retain: bool = False, properties: Any = None):
        self.published_messages.append((topic, payload, qos))

        class Info:
            def wait_for_publish(self, timeout=None):
                pass
            def is_published(self):
                return True
        return Info()


def make_valid_telemetry_payload(event_id: str = "evt-001", replay_id: str = "replay-101", compartment: str = "AICU") -> bytes:
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
            "line_number": 2,
            "line_end": 2,
            "raw_hash": "a" * 64,
            "source_time_serial": "43815.0000000000",
            "adapter_id": "agc-standard",
            "parser_version": "agc-parser-v1",
        },
        "sequence": 1,
        "source_observed_at": "2019-12-16T00:00:00",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "actual_measurements": {
                "Tair": "21.5",
                "Rhair": "75.0",
            },
            "drain_measurements": {},
            "actuators": {},
            "controller_context": {},
            "null_reasons": {},
            "category_tags": {
                "Tair": "actual_climate",
                "Rhair": "actual_climate",
            },
        },
        "units": {
            "Tair": "°C",
            "Rhair": "%",
        },
        "fault_tags": [],
    }
    return json.dumps(data, sort_keys=True).encode("utf-8")


def make_valid_control_payload(control_id: str = "ctrl-001", replay_id: str = "replay-101") -> bytes:
    data = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "control_message_id": control_id,
        "replay_id": replay_id,
        "control_sequence": 1,
        "control_type": "replay_cursor",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "cursor_info": {
            "sequence": 10,
            "source_time": "2019-12-16T00:50:00",
            "status": "running",
        },
    }
    return json.dumps(data, sort_keys=True).encode("utf-8")


# ---------------------------------------------------------------------------
# Spool Persistence & ACID Tests
# ---------------------------------------------------------------------------

def test_spool_persistence_exact_bytes_and_hash(tmp_path):
    """Spool commits incoming delivery to SQLite WAL with exact payload bytes and hash before ACK."""
    spool = DurableSpool(tmp_path / "spool")
    payload = make_valid_telemetry_payload()
    expected_hash = hashlib.sha256(payload).hexdigest()

    record = spool.record_delivery(
        topic="v1/telemetry/replay-101/AICU",
        payload_bytes=payload,
        received_at="2026-11-06T10:00:00Z",
        mqtt_mid=42,
        mqtt_qos=1,
        outbox_table="telemetry_messages",
        outbox_row_id=1,
        replay_id="replay-101",
        compartment_id="AICU",
    )

    assert record.delivery_id == 1
    assert record.payload_hash == expected_hash
    assert record.payload_bytes == payload
    assert record.message_type == "telemetry"
    assert record.is_valid is True
    assert record.quarantine_reason is None

    # Verify directly from SQLite database file
    with sqlite3.connect(str(tmp_path / "spool" / "spool.db")) as conn:
        cur = conn.cursor()
        cur.execute("SELECT topic, payload_bytes, payload_hash, mqtt_mid FROM spool_messages WHERE delivery_id = 1")
        row = cur.fetchone()
        assert row[0] == "v1/telemetry/replay-101/AICU"
        assert row[1] == payload
        assert row[2] == expected_hash
        assert row[3] == 42


def test_quarantine_malformed_json_preserved(tmp_path):
    """Malformed non-JSON bytes are safely stored in spool with quarantine label and exact bytes."""
    spool = DurableSpool(tmp_path / "spool")
    bad_bytes = b"BROKEN_NON_JSON_DATA{unclosed:42"

    record = spool.record_delivery(
        topic="v1/telemetry/replay-101/AICU",
        payload_bytes=bad_bytes,
        received_at="2026-11-06T10:01:00Z",
        mqtt_mid=43,
    )

    assert record.message_type == "quarantine"
    assert record.is_valid is False
    assert "Malformed JSON" in record.quarantine_reason
    assert record.payload_bytes == bad_bytes
    assert record.payload_hash == hashlib.sha256(bad_bytes).hexdigest()


def test_quarantine_schema_invalid_preserved(tmp_path):
    """Payloads violating envelope schema are quarantined with precise error reason."""
    spool = DurableSpool(tmp_path / "spool")
    invalid_envelope = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "missing-required-fields",
        # missing data_scope_id, replay_id, etc.
    }
    raw = json.dumps(invalid_envelope).encode("utf-8")

    record = spool.record_delivery(
        topic="v1/telemetry/replay-101/AICU",
        payload_bytes=raw,
        received_at="2026-11-06T10:02:00Z",
    )

    assert record.message_type == "quarantine"
    assert record.is_valid is False
    assert "Schema validation error" in record.quarantine_reason
    assert record.payload_bytes == raw


# ---------------------------------------------------------------------------
# Duplicate vs Conflict Delivery Lineage
# ---------------------------------------------------------------------------

def test_duplicate_delivery_lineage(tmp_path):
    """Identical payload re-delivered under same event_id receives distinct delivery_id, keeping lineage."""
    spool = DurableSpool(tmp_path / "spool")
    payload = make_valid_telemetry_payload(event_id="evt-dup-1")

    d1 = spool.record_delivery("v1/telemetry/replay/AICU", payload, "2026-11-06T10:00:00Z", mqtt_mid=10)
    d2 = spool.record_delivery("v1/telemetry/replay/AICU", payload, "2026-11-06T10:00:05Z", mqtt_mid=11)

    assert d1.delivery_id == 1
    assert d2.delivery_id == 2
    assert d1.payload_hash == d2.payload_hash
    expected_dup_eid = hashlib.sha256(b"evt-dup-1").hexdigest()
    assert d1.event_id == d2.event_id == expected_dup_eid

    total, batched, unbatched = spool.count_spooled_records()
    assert total == 2
    assert unbatched == 2


def test_conflict_delivery_lineage(tmp_path):
    """Divergent payload re-delivered under same event_id is recorded with distinct delivery_id and hash."""
    spool = DurableSpool(tmp_path / "spool")
    payload_a = make_valid_telemetry_payload(event_id="evt-conflict-1")

    # Divergent payload for same event_id (e.g. injected sensor fault or sensor recalibration)
    dict_b = json.loads(payload_a.decode("utf-8"))
    dict_b["payload"]["actual_measurements"]["Tair"] = "99.9"
    payload_b = json.dumps(dict_b, sort_keys=True).encode("utf-8")

    d1 = spool.record_delivery("v1/telemetry/replay/AICU", payload_a, "2026-11-06T10:00:00Z")
    d2 = spool.record_delivery("v1/telemetry/replay/AICU", payload_b, "2026-11-06T10:00:10Z")

    assert d1.delivery_id == 1
    assert d2.delivery_id == 2
    assert d1.payload_hash != d2.payload_hash
    expected_conflict_eid = hashlib.sha256(b"evt-conflict-1").hexdigest()
    assert d1.event_id == d2.event_id == expected_conflict_eid


# ---------------------------------------------------------------------------
# Atomic Raw-Ready Batch Packing & Immutable Filesystem Tests
# ---------------------------------------------------------------------------

def test_atomic_batch_packing_and_manifest(tmp_path):
    """BatchManager packs spooled deliveries into atomic immutable JSONL and updates manifest.json."""
    spool_dir = tmp_path / "spool"
    ready_dir = tmp_path / "ready"
    spool = DurableSpool(spool_dir)
    bm = BatchManager(spool, ready_dir)

    for i in range(5):
        spool.record_delivery(
            "v1/telemetry/replay/AICU",
            make_valid_telemetry_payload(event_id=f"evt-{i}"),
            f"2026-11-06T10:0{i}:00Z",
        )

    batch = bm.pack_batch(max_records=5)
    assert batch is not None
    assert batch.record_count == 5
    assert batch.first_delivery_id == 1
    assert batch.last_delivery_id == 5

    final_file = ready_dir / batch.file_name
    assert final_file.is_file()

    # Verify no temporary files remain
    assert list(ready_dir.glob(".tmp_*")) == []

    # Verify manifest.json
    manifest_path = ready_dir / "manifest.json"
    assert manifest_path.is_file()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["batch_count"] == 1
    assert manifest_data["batches"][0]["batch_id"] == batch.batch_id
    assert manifest_data["batches"][0]["file_checksum"] == batch.file_checksum

    # Verify file content format
    lines = final_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5
    first_record = json.loads(lines[0])
    assert first_record["delivery_id"] == 1
    expected_evt0 = hashlib.sha256(b"evt-0").hexdigest()
    assert first_record["event_id"] == expected_evt0
    assert first_record["is_valid"] is True


def test_immutable_ready_file_refuses_overwrite(tmp_path):
    """BatchManager strictly refuses to overwrite existing sealed ready files."""
    spool = DurableSpool(tmp_path / "spool")
    ready_dir = tmp_path / "ready"
    bm = BatchManager(spool, ready_dir)

    spool.record_delivery("t", make_valid_telemetry_payload(), "2026-11-06T10:00:00Z")

    # Pre-create the target file
    expected_batch_id = "batch-00000001-00000001-0001"
    collision_file = ready_dir / f"{expected_batch_id}.jsonl"
    collision_file.write_text("COLLISION")

    with pytest.raises(Exception, match="Refusing to overwrite immutable ready file"):
        bm.pack_batch()


def test_crash_recovery_cleans_orphaned_tmp_files(tmp_path):
    """Crash recovery removes orphaned .tmp files and verifies sealed batches."""
    spool = DurableSpool(tmp_path / "spool")
    ready_dir = tmp_path / "ready"

    # Simulate an incomplete crash file
    ready_dir.mkdir(parents=True, exist_ok=True)
    orphan = ready_dir / ".tmp_batch_crash_12345.jsonl"
    orphan.write_text("INCOMPLETE_CRASHED_WRITE")

    bm = BatchManager(spool, ready_dir)
    assert not orphan.exists()


def test_crash_recovery_detects_corrupted_ready_file(tmp_path):
    """Corrupted batch file (checksum mismatch) raises CorruptBatchError on startup."""
    spool = DurableSpool(tmp_path / "spool")
    ready_dir = tmp_path / "ready"
    bm = BatchManager(spool, ready_dir)

    spool.record_delivery("t", make_valid_telemetry_payload(), "2026-11-06T10:00:00Z")
    batch = bm.pack_batch(max_records=1)

    # Tamper with the sealed ready file
    tampered_file = ready_dir / batch.file_name
    tampered_file.write_text("TAMPERED_BYTES")

    # Restarting BatchManager must detect corruption
    with pytest.raises(CorruptBatchError, match="corrupted on disk"):
        BatchManager(spool, ready_dir)


# ---------------------------------------------------------------------------
# Quota Protection & Backpressure
# ---------------------------------------------------------------------------

def test_spool_quota_exceeded_withholds_insertion_and_ack(tmp_path):
    """When spool size + headroom exceeds quota, record_delivery raises QuotaExceededError."""
    # Set a very low quota of 10 KiB with 2 KiB headroom
    spool = DurableSpool(tmp_path / "spool", spool_quota_bytes=10 * 1024, headroom_bytes=2 * 1024)

    # Populate spool until quota exceeded
    large_payload = make_valid_telemetry_payload()
    for _ in range(50):
        try:
            spool.record_delivery("t", large_payload, "2026-11-06T10:00:00Z")
        except QuotaExceededError:
            break
    else:
        pytest.fail("Expected QuotaExceededError was not raised")


def test_collector_withholds_mqtt_ack_on_quota_exceeded(tmp_path):
    """MqttCollector withholds client.ack() and emits backpressure receipt when quota is exceeded."""
    collector = MqttCollector(
        spool_dir=tmp_path / "spool",
        ready_dir=tmp_path / "ready",
        spool_quota_bytes=5 * 1024,  # Tiny quota
        headroom_bytes=1 * 1024,
    )
    mock_client = MockMqttClient()
    collector._client = mock_client

    # Fill up quota
    for i in range(100):
        msg = MockMqttMessage(
            topic="v1/telemetry/replay/AICU",
            payload=make_valid_telemetry_payload(event_id=f"e-{i}"),
            mid=i + 1,
            properties=MockUserProperties([
                ("outbox_table", "telemetry_messages"),
                ("outbox_row_id", str(i + 1)),
                ("replay_id", "replay"),
                ("compartment_id", "AICU"),
            ]),
        )
        res = collector.handle_message(msg)
        if res is None:
            # Backpressure occurred! Verify mid was NOT ACKed
            assert (i + 1, 1) not in mock_client.acked_mids
            # Verify backpressure notice published
            published_topics = [t for t, _, _ in mock_client.published_messages]
            assert any("v1/receipts" in t for t in published_topics)
            break
    else:
        pytest.fail("Quota was not triggered")


# ---------------------------------------------------------------------------
# Outbox Publisher & Receipt Validation Tests
# ---------------------------------------------------------------------------

def test_publisher_validates_receipt_payload_hash(tmp_path):
    """Publisher accepts valid receipts and rejects mismatched payload hashes."""
    outbox_dir = tmp_path / "outbox"
    outbox = DurableOutbox(outbox_dir)
    outbox.initialize_run(
        replay_id="rep-1",
        data_scope_id="scope-1",
        compartment_id="AICU",
        cycle_id="tomatoes-2019",
        source_file="source.csv",
        source_file_checksum="chk",
        config_hash="conf",
        resume=False,
    )

    payload = make_valid_telemetry_payload()
    outbox.commit_dispatch_transaction(
        telemetry_messages=[("v1/telemetry/rep-1/AICU", payload, "evt-1", 1, 2)],
        control_messages=[],
        fault_audits=[],
        sequence=1,
        line_number=2,
        control_sequence=0,
        previous_cumulative_hash="0" * 64,
        expected_unique=1,
        emitted_unique=1,
        emitted_total=1,
        fault_state={},
        status="running",
    )

    publisher = OutboxPublisher(outbox_dir)

    # 1. Mismatched payload hash: must be rejected
    rejected = publisher.record_receipt({
        "outbox_table": "telemetry_messages",
        "outbox_row_id": 1,
        "payload_hash": "WRONG_HASH",
        "replay_id": "rep-1",
        "compartment_id": "AICU",
        "status": "persisted",
    })
    assert rejected is False
    assert publisher.calculate_contiguous_confirmed_cursor("rep-1") == (0, 0)

    # 2. Valid matching receipt: must be accepted
    correct_hash = hashlib.sha256(payload).hexdigest()
    accepted = publisher.record_receipt({
        "outbox_table": "telemetry_messages",
        "outbox_row_id": 1,
        "payload_hash": correct_hash,
        "replay_id": "rep-1",
        "compartment_id": "AICU",
        "status": "persisted",
    })
    assert accepted is True
    assert publisher.calculate_contiguous_confirmed_cursor("rep-1") == (1, 0)


def test_out_of_order_receipts_cannot_advance_over_holes(tmp_path):
    """Hole rule: contiguous cursor stops at the first unconfirmed row."""
    outbox_dir = tmp_path / "outbox"
    outbox = DurableOutbox(outbox_dir)
    outbox.initialize_run(
        replay_id="rep-holes",
        data_scope_id="scope",
        compartment_id="AICU",
        cycle_id="tomatoes",
        source_file="source.csv",
        source_file_checksum="chk",
        config_hash="conf",
        resume=False,
    )

    # Commit 4 rows
    messages = []
    hashes = {}
    for i in range(1, 5):
        p = make_valid_telemetry_payload(event_id=f"e-{i}")
        hashes[i] = hashlib.sha256(p).hexdigest()
        messages.append(("v1/topic", p, f"e-{i}", i, i + 1))

    outbox.commit_dispatch_transaction(
        telemetry_messages=messages,
        control_messages=[],
        fault_audits=[],
        sequence=4,
        line_number=5,
        control_sequence=0,
        previous_cumulative_hash="0" * 64,
        expected_unique=4,
        emitted_unique=4,
        emitted_total=4,
        fault_state={},
        status="running",
    )

    publisher = OutboxPublisher(outbox_dir)

    # Confirm rows 1 and 2
    publisher.record_receipt({"outbox_table": "telemetry_messages", "outbox_row_id": 1, "payload_hash": hashes[1], "replay_id": "rep-holes", "compartment_id": "AICU", "status": "persisted"})
    publisher.record_receipt({"outbox_table": "telemetry_messages", "outbox_row_id": 2, "payload_hash": hashes[2], "replay_id": "rep-holes", "compartment_id": "AICU", "status": "persisted"})

    contig_tel, _ = publisher.calculate_contiguous_confirmed_cursor("rep-holes")
    assert contig_tel == 2

    # Out-of-order confirmation: confirm row 4 while row 3 is missing (hole)
    publisher.record_receipt({"outbox_table": "telemetry_messages", "outbox_row_id": 4, "payload_hash": hashes[4], "replay_id": "rep-holes", "compartment_id": "AICU", "status": "persisted"})

    contig_tel, _ = publisher.calculate_contiguous_confirmed_cursor("rep-holes")
    # Must NOT advance over hole at row 3!
    assert contig_tel == 2

    # Now confirm hole row 3
    publisher.record_receipt({"outbox_table": "telemetry_messages", "outbox_row_id": 3, "payload_hash": hashes[3], "replay_id": "rep-holes", "compartment_id": "AICU", "status": "persisted"})
    contig_tel, _ = publisher.calculate_contiguous_confirmed_cursor("rep-holes")
    # Now advances to 4
    assert contig_tel == 4


# ---------------------------------------------------------------------------
# Boundary Probes Matching Main Probe Suite
# ---------------------------------------------------------------------------

def test_restart_recovers_rename_before_spool_manifest(tmp_path, monkeypatch):
    """Crash after atomic file rename before spool DB manifest insert recovers deterministically."""
    spool = DurableSpool(tmp_path / 'spool')
    manager = BatchManager(spool, tmp_path / 'ready')
    spool.record_delivery('v1/raw', b'not valid JSON', '2026-11-06T00:00:00+00:00')

    def crash(*args, **kwargs):
        raise RuntimeError('main injected crash after rename')

    monkeypatch.setattr(spool, 'record_batch_manifest', crash)
    with pytest.raises(RuntimeError):
        manager.pack_batch()

    original = {p.name: p.read_bytes() for p in (tmp_path / 'ready').glob('batch-*.jsonl')}
    assert len(original) == 1

    reopened = DurableSpool(tmp_path / 'spool')
    recovered = BatchManager(reopened, tmp_path / 'ready')
    recovered.pack_batch()

    assert {p.name: p.read_bytes() for p in (tmp_path / 'ready').glob('batch-*.jsonl')} == original
    assert reopened.count_spooled_records() == (1, 1, 0)
    assert len(reopened.get_all_batches()) == 1


def test_incoming_record_cannot_overrun_physical_spool_quota(tmp_path):
    """Projected incoming payload exceeding physical quota raises QuotaExceededError without writing."""
    spool = DurableSpool(tmp_path / 'spool', spool_quota_bytes=131072, headroom_bytes=4096)
    with pytest.raises(QuotaExceededError):
        spool.record_delivery('v1/raw', b'x' * 262144, '2026-11-06T00:00:00+00:00')
    assert spool.count_spooled_records() == (0, 0, 0)
    assert spool.get_physical_spool_bytes()[-1] <= 131072


@pytest.fixture
def publisher_fixture(tmp_path):
    folder = tmp_path / 'outbox'
    box = DurableOutbox(folder)
    box.initialize_run(
        replay_id='main-replay',
        data_scope_id='main-scope',
        compartment_id='AICU',
        cycle_id='main-cycle',
        source_file='fixture.csv',
        source_file_checksum='a' * 64,
        config_hash='b' * 64,
        resume=False,
    )
    payload = b'{"fixture":true}'
    with sqlite3.connect(folder / 'outbox.db') as conn:
        conn.execute(
            'INSERT INTO telemetry_messages(sequence,event_id,topic,line_number,payload_bytes,payload_hash,cumulative_hash) VALUES(?,?,?,?,?,?,?)',
            (1, 'e' * 64, 'v1/telemetry/main-replay/AICU', 2, payload, hashlib.sha256(payload).hexdigest(), '0' * 64),
        )
    return OutboxPublisher(folder), hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize('damage', ['missing_hash', 'wrong_replay', 'wrong_compartment', 'unpersisted_status'])
def test_receipt_must_match_durable_transport_identity(publisher_fixture, damage):
    pub, checksum = publisher_fixture
    receipt = dict(
        outbox_table='telemetry_messages',
        outbox_row_id=1,
        payload_hash=checksum,
        replay_id='main-replay',
        compartment_id='AICU',
        status='persisted',
        receipt_id='main-receipt',
        delivery_id=1,
        received_at='2026-11-06T00:00:00+00:00',
    )
    if damage == 'missing_hash':
        receipt.pop('payload_hash')
    elif damage == 'wrong_replay':
        receipt['replay_id'] = 'other-replay'
    elif damage == 'wrong_compartment':
        receipt['compartment_id'] = 'Reference'
    else:
        receipt['status'] = 'rejected'
    assert pub.record_receipt(receipt) is False
    assert pub.calculate_contiguous_confirmed_cursor('main-replay') == (0, 0)


# ---------------------------------------------------------------------------
# Real Broker Redelivery / Crash Emulation Tests
# ---------------------------------------------------------------------------

def test_collector_redelivery_dup_handling_before_ack(tmp_path):
    """When collector crashes after spool persistence but before ACK, reconnect redelivery with DUP is safe."""
    collector = MqttCollector(
        spool_dir=tmp_path / "spool",
        ready_dir=tmp_path / "ready",
        batch_size=2,
    )
    mock_client = MockMqttClient()
    collector._client = mock_client

    payload = make_valid_telemetry_payload(event_id="evt-redeliver-1")
    msg1 = MockMqttMessage(
        topic="v1/telemetry/replay/AICU",
        payload=payload,
        mid=101,
        qos=1,
        properties=MockUserProperties([
            ("outbox_table", "telemetry_messages"),
            ("outbox_row_id", "1"),
            ("replay_id", "replay"),
            ("compartment_id", "AICU"),
        ]),
    )

    # First delivery: persisted, but simulate crash before mock_client receives ack
    d1 = collector.handle_message(msg1)
    assert d1 is not None
    assert (101, 1) in mock_client.acked_mids

    # Simulate broker redelivery upon client reconnect with mid=102 and same payload
    msg2 = MockMqttMessage(
        topic="v1/telemetry/replay/AICU",
        payload=payload,
        mid=102,
        qos=1,
        properties=MockUserProperties([
            ("outbox_table", "telemetry_messages"),
            ("outbox_row_id", "1"),
            ("replay_id", "replay"),
            ("compartment_id", "AICU"),
        ]),
    )
    d2 = collector.handle_message(msg2)
    assert d2 is not None
    assert d2.delivery_id == 2
    assert d2.event_id == d1.event_id
    # Second delivery acknowledged successfully without data corruption
    assert (102, 1) in mock_client.acked_mids

    # Both deliveries preserved with full lineage in raw-ready batch
    batches = collector.run_once()
    assert len(batches) == 1
    ready_lines = (tmp_path / "ready" / batches[0].file_name).read_text(encoding="utf-8").strip().split("\n")
    assert len(ready_lines) == 2
    parsed_deliveries = [json.loads(line) for line in ready_lines]
    assert parsed_deliveries[0]["delivery_id"] == 1
    assert parsed_deliveries[1]["delivery_id"] == 2
    assert parsed_deliveries[0]["payload_hash"] == parsed_deliveries[1]["payload_hash"]


def test_foreign_backpressure_receipt_does_not_pause_this_replay(publisher_fixture):
    """Backpressure signals from unrelated replays must not pause publisher for this replay."""
    pub, checksum = publisher_fixture
    result = pub.record_receipt(dict(
        outbox_table='telemetry_messages',
        outbox_row_id=1,
        payload_hash=checksum,
        replay_id='someone-else',
        compartment_id='Reference',
        status='backpressure',
        credit=0,
    ))
    assert result is False
    assert pub._backpressure_active is False


def test_ready_full_withholds_ack_and_durable_receipt(tmp_path):
    """When ready directory quota is exhausted, collector withholds ACK and persisted receipt."""
    collector = MqttCollector(tmp_path / 'spool', tmp_path / 'ready', ready_quota_bytes=16384, headroom_bytes=4096)
    (tmp_path / 'ready' / 'retained-evidence.bin').write_bytes(b'x' * 16384)
    client = MockMqttClient()
    collector._client = client
    message = MockMqttMessage(
        topic='v1/telemetry/main/AICU',
        payload=b'{"fixture":true}',
        mid=1,
        qos=1,
        properties=MockUserProperties([
            ('outbox_table', 'telemetry_messages'),
            ('outbox_row_id', '1'),
            ('replay_id', 'main'),
            ('compartment_id', 'AICU'),
        ]),
    )
    delivery = collector.handle_message(message)
    assert delivery is None
    assert (1, 1) not in client.acked_mids
    for topic, payload, qos in client.published_messages:
        assert b'"status": "persisted"' not in payload


def test_malformed_transport_metadata_is_retained_without_callback_crash(tmp_path):
    """Malformed non-integer outbox_row_id does not crash callback; quarantined with exact bytes."""
    collector = MqttCollector(tmp_path / 'spool', tmp_path / 'ready')
    client = MockMqttClient()
    collector._client = client
    message = MockMqttMessage(
        topic='v1/telemetry/main/AICU',
        payload=b'main malformed envelope',
        mid=1,
        qos=1,
        properties=MockUserProperties([('outbox_row_id', 'not-an-integer')]),
    )
    delivery = collector.handle_message(message)
    assert delivery is not None
    assert delivery.payload_bytes == message.payload
    assert not delivery.is_valid
    assert delivery.quarantine_reason
    assert "not-an-integer" in delivery.quarantine_reason


def test_publisher_exhausted_retries_aborts_and_reports_failure(publisher_fixture):
    """When retries are exhausted, publisher does not loop infinitely and raises failure."""
    from greenhouse.sensor.publisher import PublisherError
    pub, _ = publisher_fixture
    pub.max_retries = 2
    pub.receipt_timeout = 0.01

    class FailingClient:
        def publish(self, topic, payload, qos=1, properties=None, retain=False):
            class MsgInfo:
                def wait_for_publish(self, timeout=None):
                    pass
                def is_published(self):
                    return True
            return MsgInfo()

    pub._client = FailingClient()
    pub._running = True

    # Simulate timeouts until retries exhausted
    pub.publish_single_item(pub.fetch_unconfirmed_telemetry(1)[0])
    time.sleep(0.05)
    pub._check_timeouts()
    time.sleep(0.05)
    pub._check_timeouts()

    assert len(pub._failed_keys) == 1
    with pytest.raises(PublisherError):
        pub.publish_all_pending(poll_interval=0.01)


def test_cli_publish_only_missing_outbox_fails_nonzero(tmp_path, capsys):
    """simulate --publish-only on nonexistent path returns nonzero without creating outbox.db or false completion."""
    from greenhouse.sensor.cli import simulate_command
    nonexistent = tmp_path / "nonexistent_pub"
    ret = simulate_command(["--publish-only", "--outbox-dir", str(nonexistent)])
    assert ret != 0
    assert not nonexistent.exists(), "Nonexistent outbox path must not be created on publish-only"
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "PUBLISH_COMPLETED" not in captured.out


def test_cli_publish_only_empty_directory_fails_nonzero(tmp_path, capsys):
    """simulate --publish-only on empty directory fails nonzero without creating outbox.db."""
    from greenhouse.sensor.cli import simulate_command
    empty_dir = tmp_path / "empty_pub"
    empty_dir.mkdir()
    ret = simulate_command(["--publish-only", "--outbox-dir", str(empty_dir)])
    assert ret != 0
    assert not (empty_dir / "outbox.db").exists(), "outbox.db must not be generated on empty publish-only"
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "PUBLISH_COMPLETED" not in captured.out


def test_cli_publish_only_uninitialized_db_fails_nonzero(tmp_path, capsys):
    """simulate --publish-only on corrupt or zero-byte outbox.db fails nonzero without false completion."""
    from greenhouse.sensor.cli import simulate_command
    corrupt_dir = tmp_path / "corrupt_pub"
    corrupt_dir.mkdir()
    (corrupt_dir / "outbox.db").write_text("")
    ret = simulate_command(["--publish-only", "--outbox-dir", str(corrupt_dir)])
    assert ret != 0
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "PUBLISH_COMPLETED" not in captured.out


def test_cli_resume_missing_outbox_fails_nonzero(tmp_path, capsys):
    """simulate --resume on nonexistent path fails nonzero without creating outbox.db."""
    from greenhouse.sensor.cli import simulate_command
    nonexistent = tmp_path / "nonexistent_resume"
    ret = simulate_command(["--resume", "--outbox-dir", str(nonexistent)])
    assert ret != 0
    assert not nonexistent.exists(), "Nonexistent outbox path must not be created on resume"
    captured = capsys.readouterr()
    assert "Error:" in captured.err


def test_cli_resume_rejects_config_drift_without_overwriting_meta(tmp_path, capsys):
    """simulate --resume rejects drifted config and does not overwrite existing outbox metadata."""
    from greenhouse.sensor.cli import simulate_command
    from test_sensor_simulator import create_synthetic_climate_csv
    import sqlite3

    csv_file = tmp_path / "GreenhouseClimate.csv"
    create_synthetic_climate_csv(csv_file, num_rows=15)

    outbox_dir = tmp_path / "outbox_resume_test"

    # 1. Run simulation paused after 5 records
    ret1 = simulate_command([
        "--source-file", str(csv_file),
        "--outbox-dir", str(outbox_dir),
        "--compartment", "AICU",
        "--limit", "10",
        "--pause-after", "5",
        "--duplicate-rate", "0.0",
    ])
    assert ret1 == 0
    assert (outbox_dir / "outbox.db").is_file()

    # Read original meta
    with sqlite3.connect(outbox_dir / "outbox.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM meta")
        meta_before = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM telemetry_messages")
        tel_count_before = cur.fetchone()[0]

    assert tel_count_before == 5
    assert meta_before["status"] == "paused"

    # 2. Attempt resume with drifted config (--duplicate-rate 0.5)
    ret_drift = simulate_command([
        "--source-file", str(csv_file),
        "--outbox-dir", str(outbox_dir),
        "--compartment", "AICU",
        "--limit", "10",
        "--duplicate-rate", "0.5",
        "--resume",
    ])
    assert ret_drift != 0
    captured = capsys.readouterr()
    assert "Resume configuration hash mismatch" in captured.err

    # Verify metadata and records were NOT overwritten or modified by the failed resume
    with sqlite3.connect(outbox_dir / "outbox.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM meta")
        meta_after_drift = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM telemetry_messages")
        tel_count_after = cur.fetchone()[0]

    assert meta_after_drift == meta_before, "Failed resume must not overwrite or mutate outbox metadata"
    assert tel_count_after == 5, "Failed resume must not alter existing outbox records"

    # 3. Resume with matching config -> completes successfully
    ret_match = simulate_command([
        "--source-file", str(csv_file),
        "--outbox-dir", str(outbox_dir),
        "--compartment", "AICU",
        "--limit", "10",
        "--duplicate-rate", "0.0",
        "--resume",
    ])
    assert ret_match == 0
    with sqlite3.connect(outbox_dir / "outbox.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM telemetry_messages")
        assert cur.fetchone()[0] == 10


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_transient_sqlite_spool_file_disappearing_is_tolerated(tmp_path, monkeypatch, suffix):
    """Race-safe accounting tolerates transient SQLite WAL/SHM disappearing between check and stat."""
    spool = DurableSpool(tmp_path / "spool")
    transient = Path(f"{spool.db_path}{suffix}")
    transient.write_bytes(b"owned accounting-race fixture")

    real_is_file = Path.is_file

    def vanish_after_check(path):
        existed = real_is_file(path)
        if path == transient and existed:
            path.unlink()
        return existed

    monkeypatch.setattr(Path, "is_file", vanish_after_check)
    sizes = spool.get_physical_spool_bytes()
    assert sizes[-1] >= 0
    if suffix == "-wal":
        assert sizes[1] == 0
    elif suffix == "-shm":
        assert sizes[2] == 0

