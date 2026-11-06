"""Durable MQTT transport publisher streaming outbox to Mosquitto.

Implements T33 publisher criteria:
- Reads from local SQLite outbox (telemetry_messages and control_messages).
- Attaches MQTT v5 UserProperty headers (outbox_table, outbox_row_id, replay_id, payload_hash)
  while keeping exact original payload bytes unadulterated.
- Implements end-to-end backpressure credit and sliding in-flight window.
- Listens for collector receipts on v1/receipts topic.
- Reconciles receipts against exact immutable (outbox_table, outbox_row_id, payload_hash).
- Discards stale/mismatched receipts.
- Contiguous cursor progression: out-of-order receipts cannot advance over unconfirmed holes.
- Retries unreceipted messages with bounded backoff.
- Tracks transport progress in outbox.db under publisher_checkpoints table.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Generator
import uuid

logger = logging.getLogger(__name__)

DEFAULT_INFLIGHT_LIMIT = 20
DEFAULT_RECEIPT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RETRIES = 5


class PublisherError(RuntimeError):
    """Base error for outbox publisher operations."""


class MismatchedReceiptError(PublisherError):
    """Raised or logged when a receipt's payload hash or metadata does not match the outbox."""


@dataclass(frozen=True)
class OutboxItem:
    outbox_table: str  # "telemetry_messages" | "control_messages"
    outbox_row_id: int
    topic: str
    payload_bytes: bytes
    payload_hash: str
    replay_id: str
    compartment_id: str
    sequence: int
    event_id: str | None


class OutboxPublisher:
    """Streams records from a DurableOutbox to MQTT broker with end-to-end receipt tracking."""

    def __init__(
        self,
        outbox_dir: Path | str,
        mqtt_host: str = "127.0.0.1",
        mqtt_port: int = 1883,
        client_id: str | None = None,
        max_inflight: int = DEFAULT_INFLIGHT_LIMIT,
        receipt_timeout: float = DEFAULT_RECEIPT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.outbox_dir = Path(outbox_dir)
        self.db_path = self.outbox_dir / "outbox.db"
        if not self.db_path.is_file():
            raise PublisherError(f"Outbox database not found at {self.db_path}")

        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.client_id = client_id or f"greenhouse-publisher-{uuid.uuid4().hex[:8]}"
        self.max_inflight = max_inflight
        self.receipt_timeout = receipt_timeout
        self.max_retries = max_retries

        self._lock = threading.Lock()
        self._inflight_timestamps: dict[tuple[str, int], float] = {}
        self._retry_counts: dict[tuple[str, int], int] = {}
        self._failed_keys: set[tuple[str, int]] = set()
        self._active_window_confirmed: set[tuple[str, int]] = set()
        self._backpressure_active = False
        self._receipt_event = threading.Event()

        self._client: Any = None
        self._running = False

        self._init_publisher_schema()

    def _get_raw_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._get_raw_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _init_publisher_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS publisher_checkpoints (
                    replay_id TEXT PRIMARY KEY,
                    last_telemetry_id INTEGER NOT NULL DEFAULT 0,
                    last_control_seq INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS published_receipts (
                    outbox_table TEXT NOT NULL,
                    outbox_row_id INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    delivery_id INTEGER,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (outbox_table, outbox_row_id)
                );
            """)

    def get_replay_metadata(self) -> dict[str, str]:
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM meta")
            return dict(cur.fetchall())

    def get_last_checkpoint(self, replay_id: str) -> tuple[int, int]:
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT last_telemetry_id, last_control_seq FROM publisher_checkpoints WHERE replay_id = ?",
                (replay_id,),
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else (0, 0)

    def update_checkpoint(self, replay_id: str, last_tel_id: int, last_ctrl_seq: int) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO publisher_checkpoints (replay_id, last_telemetry_id, last_control_seq, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(replay_id) DO UPDATE SET
                        last_telemetry_id = max(last_telemetry_id, excluded.last_telemetry_id),
                        last_control_seq = max(last_control_seq, excluded.last_control_seq),
                        updated_at = excluded.updated_at
                    """,
                    (replay_id, last_tel_id, last_ctrl_seq, now_str),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        # Prune confirmed keys strictly to the active window above checkpoint
        with self._lock:
            self._active_window_confirmed = {
                (tbl, row_id) for (tbl, row_id) in self._active_window_confirmed
                if (tbl == "telemetry_messages" and row_id > last_tel_id)
                or (tbl == "control_messages" and row_id > last_ctrl_seq)
            }

    def is_row_confirmed(self, table: str, row_id: int) -> bool:
        """Check if an outbox row is confirmed by receipt (active window or indexed SQLite)."""
        key = (table, row_id)
        with self._lock:
            if key in self._active_window_confirmed:
                return True
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM published_receipts WHERE outbox_table = ? AND outbox_row_id = ? AND status = 'persisted'",
                (table, row_id),
            )
            confirmed = cur.fetchone() is not None
            if confirmed:
                with self._lock:
                    self._active_window_confirmed.add(key)
                    if len(self._active_window_confirmed) > self.max_inflight * 2:
                        self._active_window_confirmed = set(list(self._active_window_confirmed)[-self.max_inflight:])
            return confirmed

    def record_receipt(
        self,
        receipt_dict: dict[str, Any],
    ) -> bool:
        """Validate and record a collector receipt against exact outbox row identity.

        Returns True if receipt was accepted, False if rejected as mismatched,
        stale, or damaged.
        """
        table = receipt_dict.get("outbox_table")
        row_id = receipt_dict.get("outbox_row_id")
        receipt_hash = receipt_dict.get("payload_hash")
        receipt_id = receipt_dict.get("receipt_id", f"rcpt-anon-{uuid.uuid4().hex[:6]}")
        received_at = receipt_dict.get("received_at", datetime.now(timezone.utc).isoformat())
        delivery_id = receipt_dict.get("delivery_id")
        status = receipt_dict.get("status")

        # 1. Require valid table and row ID
        if not table or row_id is None:
            logger.debug("Receipt missing table or row_id; rejecting: %s", receipt_dict)
            return False

        try:
            row_id = int(row_id)
        except (ValueError, TypeError):
            logger.warning("Receipt outbox_row_id is not an integer; rejecting: %s", row_id)
            return False

        # 2. Check replay_id and compartment_id against outbox metadata FIRST before acting on status/credit!
        # Do not allow foreign replay receipts to pause this publisher or trigger backpressure!
        meta = self.get_replay_metadata()
        expected_replay = meta.get("replay_id")
        expected_compartment = meta.get("compartment_id")

        if expected_replay and receipt_dict.get("replay_id") != expected_replay:
            logger.warning(
                "Receipt replay_id mismatch: expected '%s', got '%s'; rejecting",
                expected_replay,
                receipt_dict.get("replay_id"),
            )
            return False

        if expected_compartment and receipt_dict.get("compartment_id") != expected_compartment:
            logger.warning(
                "Receipt compartment_id mismatch: expected '%s', got '%s'; rejecting",
                expected_compartment,
                receipt_dict.get("compartment_id"),
            )
            return False

        # 3. Handle scoped backpressure or credit signals
        credit = receipt_dict.get("credit", 1)
        if status == "backpressure" or credit <= 0:
            with self._lock:
                self._backpressure_active = True
                self._receipt_event.set()
            logger.warning("Collector signaled backpressure for replay %s: pausing outbox publisher.", expected_replay)
            return False

        if status != "persisted":
            logger.warning("Receipt status is '%s' (not 'persisted'); rejecting", status)
            return False

        # 4. Require presence of payload_hash
        if not receipt_hash:
            logger.warning("Receipt missing payload_hash; rejecting")
            return False

        # 5. Verify receipt hash against actual outbox record
        with self._connection() as conn:
            cur = conn.cursor()
            if table == "telemetry_messages":
                cur.execute("SELECT payload_hash FROM telemetry_messages WHERE id = ?", (row_id,))
            elif table == "control_messages":
                cur.execute("SELECT payload_bytes FROM control_messages WHERE control_sequence = ?", (row_id,))
            else:
                logger.warning("Receipt references unknown table '%s'; rejecting", table)
                return False

            row = cur.fetchone()
            if not row:
                logger.warning("Receipt references non-existent row in %s ID %d; rejecting", table, row_id)
                return False

            if table == "telemetry_messages":
                expected_hash = row[0]
            else:
                expected_hash = hashlib.sha256(row[0]).hexdigest()

            if receipt_hash != expected_hash:
                logger.warning(
                    "Mismatched receipt payload hash for %s ID %d: expected %s, got %s; rejecting stale receipt",
                    table,
                    row_id,
                    expected_hash,
                    receipt_hash,
                )
                return False

            # Persist accepted receipt into published_receipts
            now_str = datetime.now(timezone.utc).isoformat()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO published_receipts (
                        outbox_table, outbox_row_id, payload_hash, receipt_id, delivery_id, received_at, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(outbox_table, outbox_row_id) DO UPDATE SET
                        status = excluded.status,
                        receipt_id = excluded.receipt_id,
                        delivery_id = excluded.delivery_id
                    """,
                    (table, row_id, expected_hash, receipt_id, delivery_id, received_at, status, now_str),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        with self._lock:
            key = (table, row_id)
            self._active_window_confirmed.add(key)
            if len(self._active_window_confirmed) > self.max_inflight * 2:
                self._active_window_confirmed = set(list(self._active_window_confirmed)[-self.max_inflight:])
            self._inflight_timestamps.pop(key, None)
            self._retry_counts.pop(key, None)
            if credit > 0 and self._backpressure_active:
                self._backpressure_active = False
            self._receipt_event.set()

        return True

    def calculate_contiguous_confirmed_cursor(self, replay_id: str) -> tuple[int, int]:
        """Compute maximum contiguous row IDs without skipping any unconfirmed holes.

        Out-of-order receipts cannot advance over holes. Uses windowed SQL queries.
        """
        last_tel, last_ctrl = self.get_last_checkpoint(replay_id)

        with self._connection() as conn:
            cur = conn.cursor()

            # 1. Telemetry messages: find contiguous confirmed id from last_tel upwards
            contig_tel = last_tel
            cur.execute(
                """
                SELECT t.id, r.outbox_row_id
                FROM telemetry_messages t
                LEFT JOIN published_receipts r
                  ON r.outbox_table = 'telemetry_messages'
                 AND r.outbox_row_id = t.id
                 AND r.status = 'persisted'
                WHERE t.id > ?
                ORDER BY t.id ASC
                LIMIT ?
                """,
                (contig_tel, max(100, self.max_inflight * 2)),
            )
            for tid, rid in cur.fetchall():
                if rid is not None:
                    contig_tel = tid
                else:
                    break  # Unconfirmed hole encountered!

            # 2. Control messages: find contiguous confirmed control_seq from last_ctrl upwards
            contig_ctrl = last_ctrl
            cur.execute(
                """
                SELECT c.control_sequence, r.outbox_row_id
                FROM control_messages c
                LEFT JOIN published_receipts r
                  ON r.outbox_table = 'control_messages'
                 AND r.outbox_row_id = c.control_sequence
                 AND r.status = 'persisted'
                WHERE c.control_sequence > ?
                ORDER BY c.control_sequence ASC
                LIMIT ?
                """,
                (contig_ctrl, max(50, self.max_inflight * 2)),
            )
            for cseq, rid in cur.fetchall():
                if rid is not None:
                    contig_ctrl = cseq
                else:
                    break

            return contig_tel, contig_ctrl

    def fetch_unconfirmed_telemetry(self, limit: int = 50) -> list[OutboxItem]:
        """Fetch pending unconfirmed telemetry rows using windowed SQL query."""
        meta = self.get_replay_metadata()
        replay_id = meta.get("replay_id", "default")
        compartment_id = meta.get("compartment_id", "AICU")
        last_tel, _ = self.get_last_checkpoint(replay_id)

        with self._lock:
            failed_ids = {row_id for (tbl, row_id) in self._failed_keys if tbl == "telemetry_messages"}

        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.id, t.topic, t.payload_bytes, t.payload_hash, t.sequence, t.event_id
                FROM telemetry_messages t
                LEFT JOIN published_receipts r
                  ON r.outbox_table = 'telemetry_messages'
                 AND r.outbox_row_id = t.id
                 AND r.status = 'persisted'
                WHERE t.id > ? AND r.outbox_row_id IS NULL
                ORDER BY t.id ASC
                LIMIT ?
                """,
                (last_tel, limit + len(failed_ids)),
            )
            return [
                OutboxItem(
                    outbox_table="telemetry_messages",
                    outbox_row_id=row[0],
                    topic=row[1],
                    payload_bytes=row[2],
                    payload_hash=row[3],
                    replay_id=replay_id,
                    compartment_id=compartment_id,
                    sequence=row[4],
                    event_id=row[5],
                )
                for row in cur.fetchall()
                if row[0] not in failed_ids
            ][:limit]

    def fetch_unconfirmed_controls(self, limit: int = 50) -> list[OutboxItem]:
        """Fetch pending unconfirmed control rows using windowed SQL query."""
        meta = self.get_replay_metadata()
        replay_id = meta.get("replay_id", "default")
        compartment_id = meta.get("compartment_id", "AICU")
        _, last_ctrl = self.get_last_checkpoint(replay_id)

        with self._lock:
            failed_seqs = {row_id for (tbl, row_id) in self._failed_keys if tbl == "control_messages"}

        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT c.control_sequence, c.control_id, c.control_type, c.topic, c.payload_bytes
                FROM control_messages c
                LEFT JOIN published_receipts r
                  ON r.outbox_table = 'control_messages'
                 AND r.outbox_row_id = c.control_sequence
                 AND r.status = 'persisted'
                WHERE c.control_sequence > ? AND r.outbox_row_id IS NULL
                ORDER BY c.control_sequence ASC
                LIMIT ?
                """,
                (last_ctrl, limit + len(failed_seqs)),
            )
            items = []
            for row in cur.fetchall():
                cseq = row[0]
                if cseq in failed_seqs:
                    continue
                pbytes = row[4]
                phash = hashlib.sha256(pbytes).hexdigest()
                items.append(
                    OutboxItem(
                        outbox_table="control_messages",
                        outbox_row_id=cseq,
                        topic=row[3],
                        payload_bytes=pbytes,
                        payload_hash=phash,
                        replay_id=replay_id,
                        compartment_id=compartment_id,
                        sequence=cseq,
                        event_id=row[1],
                    )
                )
                if len(items) >= limit:
                    break
            return items

    def publish_single_item(self, item: OutboxItem) -> bool:
        """Publish an outbox item via MQTT v5 with UserProperty headers."""
        if not self._client:
            return False

        key = (item.outbox_table, item.outbox_row_id)
        if self.is_row_confirmed(item.outbox_table, item.outbox_row_id):
            return True

        with self._lock:
            # Register in inflight BEFORE network publish to prevent early receipt race!
            self._inflight_timestamps[key] = time.monotonic()
            self._retry_counts[key] = self._retry_counts.get(key, 0) + 1

        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties

        props = Properties(PacketTypes.PUBLISH)
        props.UserProperty = [
            ("outbox_table", item.outbox_table),
            ("outbox_row_id", str(item.outbox_row_id)),
            ("replay_id", item.replay_id),
            ("payload_hash", item.payload_hash),
            ("compartment_id", item.compartment_id),
        ]

        # Publish QoS 1 to broker
        msg_info = self._client.publish(
            item.topic,
            item.payload_bytes,
            qos=1,
            properties=props,
            retain=False,
        )
        msg_info.wait_for_publish(timeout=5.0)
        if not msg_info.is_published():
            logger.warning("No broker PUBACK received for %s ID %d", item.outbox_table, item.outbox_row_id)
            with self._lock:
                self._inflight_timestamps.pop(key, None)
                if self._retry_counts.get(key, 0) >= self.max_retries:
                    self._failed_keys.add(key)
            return False

        # If receipt arrived during wait_for_publish, do not leave stuck in inflight
        if self.is_row_confirmed(item.outbox_table, item.outbox_row_id):
            with self._lock:
                self._inflight_timestamps.pop(key, None)

        return True

    def publish_all_pending(self, poll_interval: float = 0.1) -> int:
        """Publish all pending outbox telemetry and control messages respecting sliding window.

        Blocks until all outbox rows are confirmed by collector receipts or retry limits exceeded.
        """
        meta = self.get_replay_metadata()
        replay_id = meta.get("replay_id", "default")
        published_total = 0

        while self._running:
            # Check backpressure and failure status
            with self._lock:
                bp = self._backpressure_active
                inflight_count = len(self._inflight_timestamps)
                if self._failed_keys:
                    break

            if bp or inflight_count >= self.max_inflight:
                self._receipt_event.wait(timeout=0.5)
                self._receipt_event.clear()
                self._check_timeouts()
                continue

            # Fetch next pending items
            pending_tel = self.fetch_unconfirmed_telemetry(limit=self.max_inflight - inflight_count)
            pending_ctrl = self.fetch_unconfirmed_controls(limit=5)
            all_pending = pending_tel + pending_ctrl

            if not all_pending:
                with self._lock:
                    if not self._inflight_timestamps:
                        # All published rows have received collector receipts!
                        break
                    if self._failed_keys:
                        break
                # Still waiting for inflight receipts
                self._receipt_event.wait(timeout=0.5)
                self._receipt_event.clear()
                self._check_timeouts()
                continue

            for item in all_pending:
                key = (item.outbox_table, item.outbox_row_id)
                with self._lock:
                    if key in self._inflight_timestamps or key in self._active_window_confirmed:
                        continue  # Already inflight or confirmed
                if self.is_row_confirmed(item.outbox_table, item.outbox_row_id):
                    continue
                if self.publish_single_item(item):
                    published_total += 1

                # Update contiguous checkpoint
                contig_tel, contig_ctrl = self.calculate_contiguous_confirmed_cursor(replay_id)
                self.update_checkpoint(replay_id, contig_tel, contig_ctrl)

                with self._lock:
                    if len(self._inflight_timestamps) >= self.max_inflight:
                        break

            time.sleep(poll_interval)
            self._check_timeouts()

        # Final contiguous checkpoint update
        contig_tel, contig_ctrl = self.calculate_contiguous_confirmed_cursor(replay_id)
        self.update_checkpoint(replay_id, contig_tel, contig_ctrl)

        with self._lock:
            failed_count = len(self._failed_keys)
            inflight_left = len(self._inflight_timestamps)

        if failed_count > 0:
            raise PublisherError(
                f"Publishing failed for {failed_count} unreceipted outbox rows exceeding max retries ({self.max_retries}). "
                f"Last confirmed checkpoint: tel={contig_tel}, ctrl={contig_ctrl}."
            )

        unconfirmed_tel = self.fetch_unconfirmed_telemetry(1)
        unconfirmed_ctrl = self.fetch_unconfirmed_controls(1)
        if unconfirmed_tel or unconfirmed_ctrl or inflight_left > 0:
            raise PublisherError(
                f"Publishing interrupted or stopped before all outbox rows confirmed. "
                f"Remaining unconfirmed: tel={bool(unconfirmed_tel)}, ctrl={bool(unconfirmed_ctrl)}, inflight={inflight_left}. "
                f"Last confirmed checkpoint: tel={contig_tel}, ctrl={contig_ctrl}."
            )

        return published_total

    def _check_timeouts(self) -> None:
        """Check for unreceipted items exceeding timeout and trigger retry."""
        now = time.monotonic()
        to_retry = []
        with self._lock:
            for key, start_t in list(self._inflight_timestamps.items()):
                if key in self._active_window_confirmed:
                    self._inflight_timestamps.pop(key, None)
                    continue
                if now - start_t > self.receipt_timeout:
                    retries = self._retry_counts.get(key, 0)
                    if retries >= self.max_retries:
                        logger.error("Item %s reached max retries (%d); marking failed", key, self.max_retries)
                        self._failed_keys.add(key)
                        self._inflight_timestamps.pop(key, None)
                    else:
                        to_retry.append(key)

        for table, row_id in to_retry:
            if self.is_row_confirmed(table, row_id):
                with self._lock:
                    self._inflight_timestamps.pop((table, row_id), None)
                continue
            logger.info("Retrying unreceipted item %s ID %d", table, row_id)
            if table == "telemetry_messages":
                items = [i for i in self.fetch_unconfirmed_telemetry(100) if i.outbox_row_id == row_id]
            else:
                items = [i for i in self.fetch_unconfirmed_controls(100) if i.outbox_row_id == row_id]
            if items:
                self.publish_single_item(items[0])

    def start(self) -> None:
        """Connect to broker and subscribe to receipts."""
        import threading
        import paho.mqtt.client as mqtt
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            protocol=mqtt.MQTTv5,
        )
        if hasattr(client, "max_inflight_messages_set"):
            client.max_inflight_messages_set(self.max_inflight)
        if hasattr(client, "max_queued_messages_set"):
            client.max_queued_messages_set(self.max_inflight * 5)

        subscribed_event = threading.Event()

        def on_connect(c, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                logger.error("Publisher MQTT connection failed: %s", reason_code)
                return
            logger.info("Publisher connected to broker (%s:%d), subscribing to receipts...", self.mqtt_host, self.mqtt_port)
            c.subscribe("v1/receipts/#", qos=1)

        def on_subscribe(c, userdata, mid, reason_codes, properties):
            subscribed_event.set()

        def on_message(c, userdata, message):
            try:
                receipt_dict = json.loads(message.payload.decode("utf-8"))
                self.record_receipt(receipt_dict)
            except Exception as err:
                logger.warning("Failed to parse collector receipt on %s: %s", message.topic, err)

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        self._client = client

        logger.info("Connecting publisher to broker at %s:%d...", self.mqtt_host, self.mqtt_port)
        connect_properties = Properties(PacketTypes.CONNECT)
        connect_properties.ReceiveMaximum = max(20, self.max_inflight * 2)
        connect_properties.MaximumPacketSize = 10485760 + 65536

        client.connect(
            self.mqtt_host,
            self.mqtt_port,
            keepalive=30,
            properties=connect_properties,
        )
        client.loop_start()
        self._running = True
        if not subscribed_event.wait(timeout=5.0):
            self.stop()
            raise PublisherError("Timed out waiting for broker SUBACK confirmation on receipt subscription")

    def stop(self) -> None:
        self._running = False
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
