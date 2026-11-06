"""Durable MQTT collector, transactional spool, and atomic raw-ready batching.

Implements T33 acceptance criteria:
- Manual message acknowledgement (paho-mqtt 2.1 manual_ack) after durable spool fsync.
- Persistent session, QoS 1, versioned replay/compartment topics.
- Spool storage in SQLite with WAL mode and FULL synchronous transactions.
- Zero PostgreSQL dependency (collector is standalone broker/spool runtime).
- Quarantine retention for malformed JSON or schema invalid payloads with exact bytes and reason.
- Distinct delivery lineage for duplicates (same ID + same payload) and conflicts (same ID + divergent payload).
- Atomic raw-ready packing: .tmp in same ready volume, fsync file, replace, and fsync directory.
- Immutable ready files (never overwrite existing) and deterministic crash recovery.
- Bounded physical disk quota with headroom accounting for SQLite, WAL, batches, and manifest.
- Withholds MQTT ACK and emits backpressure credit when quota is constrained or persistence fails.
- Publishes collector receipts carrying immutable outbox table + row ID + payload hash.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Generator
import uuid

from greenhouse.sensor.schema import (
    CONTROL_SCHEMA_VERSION,
    SCHEMA_VERSION,
    ValidationError,
    validate_control_envelope,
    validate_telemetry_envelope,
)

logger = logging.getLogger(__name__)

DEFAULT_SPOOL_QUOTA_BYTES = 50 * 1024 * 1024  # 50 MiB
DEFAULT_READY_QUOTA_BYTES = 100 * 1024 * 1024  # 100 MiB
DEFAULT_HEADROOM_BYTES = 2 * 1024 * 1024  # 2 MiB safety headroom for WAL & transactions
DEFAULT_BATCH_SIZE = 50
DEFAULT_BATCH_TIMEOUT_SECONDS = 5.0


class CollectorError(RuntimeError):
    """Base error for collector operations."""


class QuotaExceededError(CollectorError):
    """Raised when spool or ready storage exceeds allocated quota plus headroom."""


class CorruptBatchError(CollectorError):
    """Raised when a raw-ready batch file is corrupted or fails checksum verification."""


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: int
    topic: str
    payload_bytes: bytes
    payload_hash: str
    received_at: str
    mqtt_mid: int | None
    mqtt_qos: int
    outbox_table: str | None
    outbox_row_id: int | None
    replay_id: str | None
    compartment_id: str | None
    event_id: str | None
    message_type: str  # "telemetry" | "control" | "quarantine"
    is_valid: bool
    quarantine_reason: str | None
    spool_batch_id: str | None
    spooled_at: str


@dataclass(frozen=True)
class BatchRecord:
    batch_id: str
    file_name: str
    record_count: int
    file_checksum: str
    cumulative_checksum: str
    first_delivery_id: int
    last_delivery_id: int
    status: str
    created_at: str
    sealed_at: str


@dataclass(frozen=True)
class StorageStats:
    spool_db_bytes: int
    spool_wal_bytes: int
    spool_shm_bytes: int
    total_spool_bytes: int
    ready_files_bytes: int
    total_bytes: int
    spool_quota_bytes: int
    ready_quota_bytes: int
    headroom_bytes: int
    is_spool_quota_exceeded: bool
    is_ready_quota_exceeded: bool


def fsync_directory(dir_path: Path) -> None:
    """Flush directory entry to disk to ensure atomic file rename durability."""
    dir_fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def parse_mqtt_user_properties(properties: Any) -> tuple[dict[str, str], list[str]]:
    """Extract string key-value pairs from MQTT v5 UserProperty tuples.

    Returns (props_dict, repeated_keys).
    """
    props_dict: dict[str, str] = {}
    repeated_keys: list[str] = []
    if properties is None:
        return props_dict, repeated_keys
    raw_user_props = getattr(properties, "UserProperty", None)
    if not raw_user_props:
        return props_dict, repeated_keys
    for item in raw_user_props:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            key, val = str(item[0]), str(item[1])
            if key in props_dict:
                repeated_keys.append(key)
            props_dict[key] = val
    return props_dict, repeated_keys


class DurableSpool:
    """ACID transactional spool store backed by SQLite WAL mode.

    Guarantees that messages are fsynced to disk before ACK. Operates completely
    offline without PostgreSQL.
    """

    def __init__(
        self,
        spool_dir: Path | str,
        spool_quota_bytes: int = DEFAULT_SPOOL_QUOTA_BYTES,
        headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
    ):
        self.spool_dir = Path(spool_dir)
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.spool_dir / "spool.db"
        self.spool_quota_bytes = spool_quota_bytes
        self.headroom_bytes = headroom_bytes

        self._init_db()

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

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS spool_messages (
                    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    payload_bytes BLOB NOT NULL,
                    payload_hash TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    mqtt_mid INTEGER,
                    mqtt_qos INTEGER NOT NULL DEFAULT 1,
                    outbox_table TEXT,
                    outbox_row_id INTEGER,
                    replay_id TEXT,
                    compartment_id TEXT,
                    event_id TEXT,
                    message_type TEXT NOT NULL,
                    is_valid INTEGER NOT NULL,
                    quarantine_reason TEXT,
                    spool_batch_id TEXT,
                    spooled_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_spool_unbatched
                    ON spool_messages(delivery_id) WHERE spool_batch_id IS NULL;
                CREATE INDEX IF NOT EXISTS idx_spool_event_id
                    ON spool_messages(event_id);
                CREATE INDEX IF NOT EXISTS idx_spool_outbox_ref
                    ON spool_messages(outbox_table, outbox_row_id);

                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    file_checksum TEXT NOT NULL,
                    cumulative_checksum TEXT NOT NULL,
                    first_delivery_id INTEGER NOT NULL,
                    last_delivery_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sealed_at TEXT NOT NULL
                );
            """)

    def get_physical_spool_bytes(self) -> tuple[int, int, int, int]:
        """Return (db_bytes, wal_bytes, shm_bytes, total_spool_bytes)."""
        def _get_size(p: Path) -> int:
            try:
                if p.is_file():
                    return p.stat().st_size
                return 0
            except FileNotFoundError:
                return 0

        db_b = _get_size(self.db_path)
        wal_b = _get_size(Path(f"{self.db_path}-wal"))
        shm_b = _get_size(Path(f"{self.db_path}-shm"))
        return db_b, wal_b, shm_b, db_b + wal_b + shm_b

    def is_quota_exceeded(self) -> bool:
        _, _, _, total_spool = self.get_physical_spool_bytes()
        return (total_spool + self.headroom_bytes) >= self.spool_quota_bytes

    def record_delivery(
        self,
        topic: str,
        payload_bytes: bytes,
        received_at: str,
        mqtt_mid: int | None = None,
        mqtt_qos: int = 1,
        outbox_table: str | None = None,
        outbox_row_id: int | None = None,
        replay_id: str | None = None,
        compartment_id: str | None = None,
        transport_quarantine_reason: str | None = None,
    ) -> DeliveryRecord:
        """Atomically persist an incoming MQTT delivery into durable SQLite spool.

        Withholds insertion if spool quota is exceeded by current or projected bytes.
        Validates payload JSON and schema, setting quarantine label on failure without
        losing original bytes.
        """
        db_b, wal_b, shm_b, total_spool = self.get_physical_spool_bytes()
        projected_size = total_spool + len(payload_bytes) + self.headroom_bytes
        if projected_size >= self.spool_quota_bytes:
            raise QuotaExceededError(
                f"Spool quota exceeded: projected physical usage {projected_size} bytes (current {total_spool} "
                f"+ incoming {len(payload_bytes)} + headroom {self.headroom_bytes}) "
                f">= limit {self.spool_quota_bytes} bytes. Withholding ACK."
            )

        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        spooled_at = datetime.now(timezone.utc).isoformat()

        # Parse and validate envelope
        event_id: str | None = None
        message_type: str = "quarantine"
        is_valid: bool = False
        quarantine_reason: str | None = None

        if transport_quarantine_reason:
            message_type = "quarantine"
            is_valid = False
            quarantine_reason = transport_quarantine_reason
            try:
                parsed_json = json.loads(payload_bytes.decode("utf-8"))
                if isinstance(parsed_json, dict):
                    event_id = parsed_json.get("event_id") or parsed_json.get("control_message_id")
            except Exception:
                pass
        else:
            try:
                parsed_json = json.loads(payload_bytes.decode("utf-8"))
                if not isinstance(parsed_json, dict):
                    quarantine_reason = "JSON root is not an object"
                else:
                    schema_ver = parsed_json.get("schema_version")
                    # Infer replay/compartment from payload if not in metadata
                    if not replay_id:
                        replay_id = parsed_json.get("replay_id")
                    if not compartment_id:
                        compartment_id = parsed_json.get("compartment_id")

                    if schema_ver == SCHEMA_VERSION:
                        validate_telemetry_envelope(parsed_json)
                        message_type = "telemetry"
                        is_valid = True
                        event_id = parsed_json.get("event_id")
                    elif schema_ver == CONTROL_SCHEMA_VERSION:
                        validate_control_envelope(parsed_json)
                        message_type = "control"
                        is_valid = True
                        event_id = parsed_json.get("control_message_id")
                    else:
                        quarantine_reason = f"Unrecognized schema_version '{schema_ver}'"
            except json.JSONDecodeError as err:
                quarantine_reason = f"Malformed JSON: {err}"
            except ValidationError as err:
                quarantine_reason = f"Schema validation error: {err}"
            except Exception as err:
                quarantine_reason = f"Validation exception: {type(err).__name__}: {err}"

        # Deduplication and conflict check against previously spooled deliveries
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.cursor()
                if event_id and is_valid:
                    cur.execute(
                        "SELECT delivery_id, payload_hash FROM spool_messages WHERE event_id = ? ORDER BY delivery_id ASC",
                        (event_id,),
                    )
                    existing = cur.fetchall()
                    if existing:
                        first_hash = existing[0][1]
                        if payload_hash == first_hash:
                            logger.info(
                                "Duplicate delivery recorded for event_id=%s (delivery count=%d)",
                                event_id,
                                len(existing) + 1,
                            )
                        else:
                            logger.warning(
                                "Conflict delivery recorded for event_id=%s: divergent hash %s vs original %s",
                                event_id,
                                payload_hash,
                                first_hash,
                            )

                cur.execute(
                    """
                    INSERT INTO spool_messages (
                        topic, payload_bytes, payload_hash, received_at, mqtt_mid, mqtt_qos,
                        outbox_table, outbox_row_id, replay_id, compartment_id, event_id,
                        message_type, is_valid, quarantine_reason, spool_batch_id, spooled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        topic,
                        payload_bytes,
                        payload_hash,
                        received_at,
                        mqtt_mid,
                        mqtt_qos,
                        outbox_table,
                        outbox_row_id,
                        replay_id,
                        compartment_id,
                        event_id,
                        message_type,
                        1 if is_valid else 0,
                        quarantine_reason,
                        None,
                        spooled_at,
                    ),
                )
                delivery_id = cur.lastrowid
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return DeliveryRecord(
            delivery_id=delivery_id,
            topic=topic,
            payload_bytes=payload_bytes,
            payload_hash=payload_hash,
            received_at=received_at,
            mqtt_mid=mqtt_mid,
            mqtt_qos=mqtt_qos,
            outbox_table=outbox_table,
            outbox_row_id=outbox_row_id,
            replay_id=replay_id,
            compartment_id=compartment_id,
            event_id=event_id,
            message_type=message_type,
            is_valid=is_valid,
            quarantine_reason=quarantine_reason,
            spool_batch_id=None,
            spooled_at=spooled_at,
        )

    def fetch_unbatched_deliveries(self, limit: int = DEFAULT_BATCH_SIZE) -> list[DeliveryRecord]:
        """Fetch oldest unbatched deliveries ordered strictly by delivery_id."""
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT delivery_id, topic, payload_bytes, payload_hash, received_at, mqtt_mid, mqtt_qos,
                       outbox_table, outbox_row_id, replay_id, compartment_id, event_id,
                       message_type, is_valid, quarantine_reason, spool_batch_id, spooled_at
                FROM spool_messages
                WHERE spool_batch_id IS NULL
                ORDER BY delivery_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [
                DeliveryRecord(
                    delivery_id=r[0],
                    topic=r[1],
                    payload_bytes=r[2],
                    payload_hash=r[3],
                    received_at=r[4],
                    mqtt_mid=r[5],
                    mqtt_qos=r[6],
                    outbox_table=r[7],
                    outbox_row_id=r[8],
                    replay_id=r[9],
                    compartment_id=r[10],
                    event_id=r[11],
                    message_type=r[12],
                    is_valid=bool(r[13]),
                    quarantine_reason=r[14],
                    spool_batch_id=r[15],
                    spooled_at=r[16],
                )
                for r in rows
            ]

    def get_deliveries_by_ids(self, delivery_ids: list[int]) -> dict[int, DeliveryRecord]:
        """Fetch spooled delivery records by their delivery IDs."""
        if not delivery_ids:
            return {}
        placeholders = ",".join("?" for _ in delivery_ids)
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT delivery_id, topic, payload_bytes, payload_hash, received_at, mqtt_mid, mqtt_qos,
                       outbox_table, outbox_row_id, replay_id, compartment_id, event_id,
                       message_type, is_valid, quarantine_reason, spool_batch_id, spooled_at
                FROM spool_messages
                WHERE delivery_id IN ({placeholders})
                """,
                delivery_ids,
            )
            rows = cur.fetchall()
            return {
                r[0]: DeliveryRecord(
                    delivery_id=r[0],
                    topic=r[1],
                    payload_bytes=r[2],
                    payload_hash=r[3],
                    received_at=r[4],
                    mqtt_mid=r[5],
                    mqtt_qos=r[6],
                    outbox_table=r[7],
                    outbox_row_id=r[8],
                    replay_id=r[9],
                    compartment_id=r[10],
                    event_id=r[11],
                    message_type=r[12],
                    is_valid=bool(r[13]),
                    quarantine_reason=r[14],
                    spool_batch_id=r[15],
                    spooled_at=r[16],
                )
                for r in rows
            }

    def mark_deliveries_batched(self, delivery_ids: list[int], batch_id: str) -> None:
        if not delivery_ids:
            return
        placeholders = ",".join("?" for _ in delivery_ids)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    f"UPDATE spool_messages SET spool_batch_id = ? WHERE delivery_id IN ({placeholders})",
                    [batch_id, *delivery_ids],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def record_batch_manifest(self, batch: BatchRecord, delivery_ids: list[int] | None = None) -> None:
        """Atomically record batch in batches table and mark deliveries batched in ONE transaction."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO batches (
                        batch_id, file_name, record_count, file_checksum, cumulative_checksum,
                        first_delivery_id, last_delivery_id, status, created_at, sealed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(batch_id) DO UPDATE SET
                        file_checksum = excluded.file_checksum,
                        cumulative_checksum = excluded.cumulative_checksum,
                        status = excluded.status,
                        sealed_at = excluded.sealed_at
                    """,
                    (
                        batch.batch_id,
                        batch.file_name,
                        batch.record_count,
                        batch.file_checksum,
                        batch.cumulative_checksum,
                        batch.first_delivery_id,
                        batch.last_delivery_id,
                        batch.status,
                        batch.created_at,
                        batch.sealed_at,
                    ),
                )
                if delivery_ids:
                    placeholders = ",".join("?" for _ in delivery_ids)
                    conn.execute(
                        f"UPDATE spool_messages SET spool_batch_id = ? WHERE delivery_id IN ({placeholders})",
                        [batch.batch_id, *delivery_ids],
                    )
                else:
                    conn.execute(
                        "UPDATE spool_messages SET spool_batch_id = ? "
                        "WHERE delivery_id BETWEEN ? AND ? AND spool_batch_id IS NULL",
                        (batch.batch_id, batch.first_delivery_id, batch.last_delivery_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_latest_cumulative_checksum(self) -> str:
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT cumulative_checksum FROM batches ORDER BY rowid DESC LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else ("0" * 64)

    def get_all_batches(self) -> list[BatchRecord]:
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT batch_id, file_name, record_count, file_checksum, cumulative_checksum,
                       first_delivery_id, last_delivery_id, status, created_at, sealed_at
                FROM batches ORDER BY rowid ASC
                """
            )
            return [
                BatchRecord(
                    batch_id=r[0],
                    file_name=r[1],
                    record_count=r[2],
                    file_checksum=r[3],
                    cumulative_checksum=r[4],
                    first_delivery_id=r[5],
                    last_delivery_id=r[6],
                    status=r[7],
                    created_at=r[8],
                    sealed_at=r[9],
                )
                for r in cur.fetchall()
            ]

    def count_spooled_records(self) -> tuple[int, int, int]:
        """Return (total_spooled, total_batched, total_unbatched)."""
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM spool_messages")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM spool_messages WHERE spool_batch_id IS NOT NULL")
            batched = cur.fetchone()[0]
            return total, batched, total - batched


class BatchManager:
    """Packs spooled deliveries into atomic, immutable JSONL files under raw-ready.

    Ensures Spark Structured Streaming only ever sees completed, sealed files.
    Enforces atomic replace on same filesystem and directory entry fsync.
    """

    def __init__(
        self,
        spool: DurableSpool,
        ready_dir: Path | str,
        ready_quota_bytes: int = DEFAULT_READY_QUOTA_BYTES,
        headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
    ):
        self.spool = spool
        self.ready_dir = Path(ready_dir)
        self.ready_dir.mkdir(parents=True, exist_ok=True)
        self.ready_quota_bytes = ready_quota_bytes
        self.headroom_bytes = headroom_bytes
        self.manifest_path = self.ready_dir / "manifest.json"

        # Deterministic crash recovery on startup
        self.recover_interrupted_batches()

    def get_ready_files_bytes(self) -> int:
        total = 0
        if self.ready_dir.is_dir():
            for p in self.ready_dir.iterdir():
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except FileNotFoundError:
                    pass
        return total

    def is_quota_exceeded(self, projected_bytes: int = 0) -> bool:
        return (self.get_ready_files_bytes() + projected_bytes + self.headroom_bytes) >= self.ready_quota_bytes

    def recover_interrupted_batches(self) -> None:
        """Deterministic cleanup of orphaned .tmp files and audit of ready files."""
        # 1. Clean incomplete temporary files in ready_dir
        if self.ready_dir.is_dir():
            for tmp_file in self.ready_dir.glob(".tmp_*"):
                try:
                    logger.warning("Purging incomplete temporary batch file %s", tmp_file)
                    tmp_file.unlink()
                except OSError as err:
                    logger.error("Failed to remove orphaned temp file %s: %s", tmp_file, err)

        # 2. Reconcile any unrecorded batch-*.jsonl files on disk not yet in batches table
        batches = self.spool.get_all_batches()
        known_batch_ids = {b.batch_id for b in batches}
        if self.ready_dir.is_dir():
            for batch_file in sorted(self.ready_dir.glob("batch-*.jsonl")):
                batch_id = batch_file.stem
                if batch_id not in known_batch_ids:
                    # Parse deliveries from the final file to reconcile state
                    lines = batch_file.read_bytes().split(b"\n")
                    delivery_ids: list[int] = []
                    parsed_records: list[dict[str, Any]] = []
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line.decode("utf-8"))
                        except Exception as err:
                            raise CorruptBatchError(f"Unrecorded batch file {batch_file} contains malformed JSON: {err}")
                        if not isinstance(rec, dict) or "delivery_id" not in rec or "payload_hash" not in rec:
                            raise CorruptBatchError(f"Unrecorded batch file {batch_file} contains invalid record structure")
                        delivery_ids.append(int(rec["delivery_id"]))
                        parsed_records.append(rec)

                    if not delivery_ids:
                        raise CorruptBatchError(f"Unrecorded batch file {batch_file} has no delivery records")

                    spool_deliveries = self.spool.get_deliveries_by_ids(delivery_ids)
                    if len(spool_deliveries) != len(delivery_ids):
                        raise CorruptBatchError(f"Unrecorded batch file {batch_file} references delivery IDs not in spool")

                    for rec in parsed_records:
                        did = int(rec["delivery_id"])
                        spool_rec = spool_deliveries.get(did)
                        if spool_rec is None or spool_rec.payload_hash != rec.get("payload_hash"):
                            raise CorruptBatchError(
                                f"Unrecorded batch file {batch_file} delivery {did} corrupted: payload_hash mismatch"
                            )

                    first_id = min(delivery_ids)
                    last_id = max(delivery_ids)
                    count = len(delivery_ids)
                    file_checksum = self._hash_file(batch_file)
                    prev_cumulative = self.spool.get_latest_cumulative_checksum()
                    cumulative_checksum = hashlib.sha256(f"{prev_cumulative}:{file_checksum}".encode("ascii")).hexdigest()
                    now_str = datetime.now(timezone.utc).isoformat()
                    reconciled_batch = BatchRecord(
                        batch_id=batch_id,
                        file_name=batch_file.name,
                        record_count=count,
                        file_checksum=file_checksum,
                        cumulative_checksum=cumulative_checksum,
                        first_delivery_id=first_id,
                        last_delivery_id=last_id,
                        status="ready",
                        created_at=now_str,
                        sealed_at=now_str,
                    )
                    self.spool.record_batch_manifest(reconciled_batch, delivery_ids=delivery_ids)
                    known_batch_ids.add(batch_id)
                    logger.info("Reconciled unrecorded ready batch %s into spool manifest", batch_id)

        # 3. Verify all sealed batches in DB against files on disk
        batches = self.spool.get_all_batches()
        for b in batches:
            final_path = self.ready_dir / b.file_name
            if not final_path.is_file():
                raise CorruptBatchError(
                    f"Batch {b.batch_id} recorded in spool manifest but file missing from ready_dir: {final_path}"
                )
            # Verify file checksum
            actual_checksum = self._hash_file(final_path)
            if actual_checksum != b.file_checksum:
                raise CorruptBatchError(
                    f"Batch {b.batch_id} corrupted on disk: expected {b.file_checksum}, got {actual_checksum}"
                )

        # 4. Synchronize manifest.json
        self._write_ready_manifest(batches)

    def _hash_file(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _write_ready_manifest(self, batches: list[BatchRecord]) -> None:
        """Atomically update manifest.json in ready_dir using same-filesystem rename and fsync."""
        manifest_data = {
            "version": "greenhouse-raw-ready-manifest-v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "batch_count": len(batches),
            "batches": [asdict(b) for b in batches],
        }
        manifest_bytes = json.dumps(manifest_data, indent=2, sort_keys=True).encode("utf-8")

        tmp_manifest = self.ready_dir / f".tmp_manifest_{uuid.uuid4().hex[:8]}.json"
        with tmp_manifest.open("wb") as handle:
            handle.write(manifest_bytes)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_manifest, self.manifest_path)
        fsync_directory(self.ready_dir)

    def pack_batch(self, max_records: int = DEFAULT_BATCH_SIZE) -> BatchRecord | None:
        """Packs unbatched spool records into one immutable JSONL file in ready_dir."""
        if self.is_quota_exceeded():
            raise QuotaExceededError(
                f"Ready directory quota exceeded: {self.get_ready_files_bytes()} bytes "
                f"+ {self.headroom_bytes} headroom >= {self.ready_quota_bytes} bytes."
            )

        deliveries = self.spool.fetch_unbatched_deliveries(limit=max_records)
        if not deliveries:
            return None

        projected_payload_bytes = sum(len(d.payload_bytes) for d in deliveries)
        current_ready_bytes = self.get_ready_files_bytes()
        if (current_ready_bytes + projected_payload_bytes + self.headroom_bytes) >= self.ready_quota_bytes:
            raise QuotaExceededError(
                f"Projected ready batch size ({projected_payload_bytes} bytes payload + {current_ready_bytes} current "
                f"+ {self.headroom_bytes} headroom) exceeds ready quota ({self.ready_quota_bytes} bytes)."
            )

        first_id = deliveries[0].delivery_id
        last_id = deliveries[-1].delivery_id
        count = len(deliveries)
        batch_id = f"batch-{first_id:08d}-{last_id:08d}-{count:04d}"
        file_name = f"{batch_id}.jsonl"
        final_path = self.ready_dir / file_name

        if final_path.exists():
            raise CollectorError(f"Refusing to overwrite immutable ready file: {final_path}")

        # Staging file on the SAME filesystem/volume
        tmp_path = self.ready_dir / f".tmp_{batch_id}_{uuid.uuid4().hex[:6]}.jsonl"
        created_at = datetime.now(timezone.utc).isoformat()

        try:
            with tmp_path.open("wb") as handle:
                for d in deliveries:
                    # Construct uniform JSONL envelope preserving raw bytes, collector receipts, and lineage
                    try:
                        parsed_payload = json.loads(d.payload_bytes.decode("utf-8"))
                    except Exception:
                        parsed_payload = None

                    line_dict = {
                        "delivery_id": d.delivery_id,
                        "topic": d.topic,
                        "received_at": d.received_at,
                        "payload_hash": d.payload_hash,
                        "message_type": d.message_type,
                        "is_valid": d.is_valid,
                        "quarantine_reason": d.quarantine_reason,
                        "outbox_table": d.outbox_table,
                        "outbox_row_id": d.outbox_row_id,
                        "replay_id": d.replay_id,
                        "compartment_id": d.compartment_id,
                        "event_id": d.event_id,
                        "raw_bytes_len": len(d.payload_bytes),
                        "payload": parsed_payload,
                    }
                    line_bytes = json.dumps(line_dict, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
                    handle.write(line_bytes)
                handle.flush()
                os.fsync(handle.fileno())

            actual_batch_bytes = tmp_path.stat().st_size
            if (current_ready_bytes + actual_batch_bytes + self.headroom_bytes) >= self.ready_quota_bytes:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise QuotaExceededError(
                    f"Ready batch file size ({actual_batch_bytes} bytes + {current_ready_bytes} current "
                    f"+ {self.headroom_bytes} headroom) exceeds ready quota ({self.ready_quota_bytes} bytes)."
                )

            file_checksum = self._hash_file(tmp_path)
            prev_cumulative = self.spool.get_latest_cumulative_checksum()
            cumulative_checksum = hashlib.sha256(f"{prev_cumulative}:{file_checksum}".encode("ascii")).hexdigest()
            sealed_at = datetime.now(timezone.utc).isoformat()

            batch_rec = BatchRecord(
                batch_id=batch_id,
                file_name=file_name,
                record_count=count,
                file_checksum=file_checksum,
                cumulative_checksum=cumulative_checksum,
                first_delivery_id=first_id,
                last_delivery_id=last_id,
                status="ready",
                created_at=created_at,
                sealed_at=sealed_at,
            )

            # Atomic replace on same filesystem
            os.replace(tmp_path, final_path)
            fsync_directory(self.ready_dir)

            # Record batch in spool DB and mark deliveries batched in ONE transaction
            self.spool.record_batch_manifest(batch_rec, delivery_ids=[d.delivery_id for d in deliveries])

            # Update manifest.json
            self._write_ready_manifest(self.spool.get_all_batches())
            logger.info("Sealed raw-ready batch %s with %d records (SHA=%s)", batch_id, count, file_checksum)
            return batch_rec
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise


class MqttCollector:
    """Durable MQTT collector client.

    Subscribes to versioned replay and control topics with QoS 1.
    Uses manual_ack to withhold broker ACK until durable spool commit.
    Publishes end-to-end collector receipts to upstream publisher.
    """

    def __init__(
        self,
        spool_dir: Path | str,
        ready_dir: Path | str,
        mqtt_host: str = "127.0.0.1",
        mqtt_port: int = 1883,
        client_id: str = "greenhouse-collector",
        clean_session: bool = False,
        session_expiry_interval: int = 86400,
        spool_quota_bytes: int = DEFAULT_SPOOL_QUOTA_BYTES,
        ready_quota_bytes: int = DEFAULT_READY_QUOTA_BYTES,
        headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_timeout: float = DEFAULT_BATCH_TIMEOUT_SECONDS,
        max_payload_bytes: int = 10 * 1024 * 1024,
        max_packet_bytes: int | None = None,
    ):
        self.spool = DurableSpool(spool_dir, spool_quota_bytes, headroom_bytes)
        self.batch_manager = BatchManager(self.spool, ready_dir, ready_quota_bytes, headroom_bytes)
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.client_id = client_id
        self.clean_session = clean_session
        self.session_expiry_interval = session_expiry_interval
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.max_payload_bytes = max_payload_bytes
        self.max_packet_bytes = max_packet_bytes or (max_payload_bytes + 65536)

        self._client: Any = None
        self._running = False
        self._last_batch_time = time.monotonic()
        self._completion_manifest_received = False
        self._received_count = 0
        self._acked_count = 0

    def get_storage_stats(self) -> StorageStats:
        db_b, wal_b, shm_b, total_spool = self.spool.get_physical_spool_bytes()
        ready_b = self.batch_manager.get_ready_files_bytes()
        return StorageStats(
            spool_db_bytes=db_b,
            spool_wal_bytes=wal_b,
            spool_shm_bytes=shm_b,
            total_spool_bytes=total_spool,
            ready_files_bytes=ready_b,
            total_bytes=total_spool + ready_b,
            spool_quota_bytes=self.spool.spool_quota_bytes,
            ready_quota_bytes=self.batch_manager.ready_quota_bytes,
            headroom_bytes=self.spool.headroom_bytes,
            is_spool_quota_exceeded=self.spool.is_quota_exceeded(),
            is_ready_quota_exceeded=self.batch_manager.is_quota_exceeded(),
        )

    def handle_message(self, message: Any) -> DeliveryRecord | None:
        """Process one incoming MQTT message: spool fsync -> emit receipt -> client.ack().

        If storage quota is exceeded, withholds client.ack() and receipt!
        If transport identity (topic/headers/declared hash/table) is malformed, retains in spool
        as quarantine, ACKs to broker to prevent redelivery poison loops, but emits status 'rejected'.
        If transport identity is valid but payload is invalid/corrupt, retains in spool as quarantine,
        ACKs to broker, and emits status 'persisted' with disposition 'quarantined', allowing publisher
        reconciliation to advance without infinite retries.
        """
        received_at = datetime.now(timezone.utc).isoformat()
        topic = message.topic
        payload_bytes = message.payload
        mid = getattr(message, "mid", None)
        qos = getattr(message, "qos", 1)

        # Extract MQTT v5 UserProperty metadata if attached by publisher
        props_dict, repeated_keys = parse_mqtt_user_properties(getattr(message, "properties", None))
        outbox_table = props_dict.get("outbox_table")
        outbox_row_id: int | None = None
        transport_quarantine_reasons: list[str] = []
        transport_identity_valid = True

        if repeated_keys:
            transport_identity_valid = False
            transport_quarantine_reasons.append(f"Duplicate UserProperty keys detected: {repeated_keys}")

        if "outbox_row_id" in props_dict:
            try:
                outbox_row_id = int(props_dict["outbox_row_id"])
                if outbox_row_id <= 0:
                    transport_identity_valid = False
                    transport_quarantine_reasons.append(f"Invalid non-positive outbox_row_id: {outbox_row_id}")
            except (ValueError, TypeError) as err:
                transport_identity_valid = False
                transport_quarantine_reasons.append(f"Malformed transport outbox_row_id '{props_dict['outbox_row_id']}': {err}")
                outbox_row_id = None
        else:
            transport_identity_valid = False
            transport_quarantine_reasons.append("Missing transport header outbox_row_id")

        replay_id = props_dict.get("replay_id")
        compartment_id = props_dict.get("compartment_id")

        if not outbox_table or outbox_table not in ("telemetry_messages", "control_messages"):
            transport_identity_valid = False
            transport_quarantine_reasons.append(f"Invalid or missing outbox_table '{outbox_table}'")

        # Table vs Topic type agreement
        if topic.startswith("v1/telemetry/") and outbox_table == "control_messages":
            transport_identity_valid = False
            transport_quarantine_reasons.append(f"Topic '{topic}' is telemetry but outbox_table is 'control_messages'")
        elif topic.startswith("v1/control/") and outbox_table == "telemetry_messages":
            transport_identity_valid = False
            transport_quarantine_reasons.append(f"Topic '{topic}' is control but outbox_table is 'telemetry_messages'")

        # Topic vs Header replay/compartment agreement
        topic_parts = topic.split("/")
        if len(topic_parts) >= 4 and topic_parts[0] == "v1":
            topic_replay = topic_parts[2]
            topic_compartment = topic_parts[3]
            if replay_id and topic_replay != replay_id:
                transport_identity_valid = False
                transport_quarantine_reasons.append(
                    f"Topic replay '{topic_replay}' does not match header replay_id '{replay_id}'"
                )
            if compartment_id and topic_compartment != compartment_id:
                transport_identity_valid = False
                transport_quarantine_reasons.append(
                    f"Topic compartment '{topic_compartment}' does not match header compartment_id '{compartment_id}'"
                )

        # Declared payload_hash vs actual payload sha256
        actual_payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        declared_hash = props_dict.get("payload_hash")
        if not declared_hash:
            transport_identity_valid = False
            transport_quarantine_reasons.append("Missing transport header payload_hash")
        elif declared_hash != actual_payload_hash:
            transport_identity_valid = False
            transport_quarantine_reasons.append(
                f"Declared payload_hash '{declared_hash}' does not match actual sha256 '{actual_payload_hash}'"
            )

        # Envelope JSON agreement if parseable
        try:
            parsed_env = json.loads(payload_bytes.decode("utf-8"))
            if isinstance(parsed_env, dict):
                env_replay = parsed_env.get("replay_id")
                env_compartment = parsed_env.get("compartment_id")
                if env_replay and replay_id and env_replay != replay_id:
                    transport_identity_valid = False
                    transport_quarantine_reasons.append(
                        f"Envelope replay_id '{env_replay}' does not match header replay_id '{replay_id}'"
                    )
                if env_replay and len(topic_parts) >= 4 and env_replay != topic_parts[2]:
                    transport_identity_valid = False
                    transport_quarantine_reasons.append(
                        f"Envelope replay_id '{env_replay}' does not match topic replay '{topic_parts[2]}'"
                    )
                if env_compartment and compartment_id and env_compartment != compartment_id:
                    transport_identity_valid = False
                    transport_quarantine_reasons.append(
                        f"Envelope compartment_id '{env_compartment}' does not match header compartment_id '{compartment_id}'"
                    )
                if env_compartment and len(topic_parts) >= 4 and env_compartment != topic_parts[3]:
                    transport_identity_valid = False
                    transport_quarantine_reasons.append(
                        f"Envelope compartment_id '{env_compartment}' does not match topic compartment '{topic_parts[3]}'"
                    )
        except Exception:
            pass

        if len(payload_bytes) > self.max_payload_bytes:
            transport_identity_valid = False
            transport_quarantine_reasons.append(
                f"Payload bytes ({len(payload_bytes)}) exceed max limit ({self.max_payload_bytes})"
            )

        transport_quarantine_reason = "; ".join(transport_quarantine_reasons) if transport_quarantine_reasons else None

        # Check ready storage quota with projected batch entry before intake
        projected_ready = len(payload_bytes) * 2 + 512
        if self.batch_manager.is_quota_exceeded(projected_ready):
            logger.warning("Ready directory quota exceeded: withholding ACK for mid=%s", mid)
            if self._client and outbox_table and outbox_row_id is not None:
                self._publish_backpressure_receipt(topic, outbox_table, outbox_row_id, replay_id, compartment_id)
            return None

        try:
            # 1. ACID Spool Commit (fsyncs SQLite WAL before returning)
            delivery = self.spool.record_delivery(
                topic=topic,
                payload_bytes=payload_bytes,
                received_at=received_at,
                mqtt_mid=mid,
                mqtt_qos=qos,
                outbox_table=outbox_table,
                outbox_row_id=outbox_row_id,
                replay_id=replay_id,
                compartment_id=compartment_id,
                transport_quarantine_reason=transport_quarantine_reason,
            )
            self._received_count += 1

            # Check if this delivery is a completion manifest
            if delivery.is_valid and delivery.message_type == "control":
                try:
                    c_obj = json.loads(payload_bytes.decode("utf-8"))
                    if c_obj.get("control_type") == "completion_manifest":
                        self._completion_manifest_received = True
                except Exception:
                    pass

            # 2. Publish Collector Receipt to Broker for End-to-End Backpressure
            # ONLY publish persisted receipt if transport identity was verified and matching!
            # If transport identity is valid, emit persisted receipt even if payload is quarantined,
            # allowing outbox cursor to advance without infinite retries of intentional bad payloads.
            if self._client and outbox_table and outbox_row_id is not None:
                if transport_identity_valid:
                    self._publish_receipt(delivery)
                else:
                    self._publish_rejected_receipt(delivery)

            # 3. Explicit Manual ACK to Mosquitto ONLY after durable disk persistence
            if self._client and hasattr(self._client, "ack") and mid is not None:
                self._client.ack(mid, qos)
                self._acked_count += 1

            return delivery

        except QuotaExceededError as err:
            logger.error("Spool quota exceeded: withholding ACK for mid=%s: %s", mid, err)
            if self._client and outbox_table and outbox_row_id is not None:
                self._publish_backpressure_receipt(topic, outbox_table, outbox_row_id, replay_id, compartment_id)
            return None
        except Exception as err:
            logger.error("Failed to persist delivery to spool; withholding ACK for mid=%s: %s", mid, err)
            return None

    def _publish_rejected_receipt(self, delivery: DeliveryRecord) -> None:
        """Publish a rejected / quarantined status receipt for an invalid delivery."""
        receipt_topic = f"v1/receipts/{delivery.replay_id or 'all'}/{delivery.compartment_id or 'all'}"
        receipt_payload = {
            "receipt_id": f"rcpt-rej-{delivery.delivery_id:08d}-{uuid.uuid4().hex[:6]}",
            "delivery_id": delivery.delivery_id,
            "outbox_table": delivery.outbox_table,
            "outbox_row_id": delivery.outbox_row_id,
            "replay_id": delivery.replay_id,
            "compartment_id": delivery.compartment_id,
            "payload_hash": delivery.payload_hash,
            "status": "rejected",
            "quarantine_reason": delivery.quarantine_reason,
            "received_at": delivery.received_at,
        }
        raw_receipt = json.dumps(receipt_payload, sort_keys=True).encode("utf-8")
        try:
            self._client.publish(receipt_topic, raw_receipt, qos=1, retain=False)
        except Exception as err:
            logger.warning("Failed to publish rejection receipt to %s: %s", receipt_topic, err)

    def _publish_receipt(self, delivery: DeliveryRecord) -> None:
        """Publish durable collector receipt to v1/receipts topic."""
        receipt_topic = f"v1/receipts/{delivery.replay_id or 'all'}/{delivery.compartment_id or 'all'}"
        _, _, _, total_spool = self.spool.get_physical_spool_bytes()
        free_bytes = max(0, self.spool.spool_quota_bytes - total_spool)

        receipt_payload = {
            "receipt_id": f"rcpt-{delivery.delivery_id:08d}-{uuid.uuid4().hex[:6]}",
            "delivery_id": delivery.delivery_id,
            "outbox_table": delivery.outbox_table,
            "outbox_row_id": delivery.outbox_row_id,
            "replay_id": delivery.replay_id,
            "compartment_id": delivery.compartment_id,
            "payload_hash": delivery.payload_hash,
            "event_id": delivery.event_id,
            "received_at": delivery.received_at,
            "spool_free_bytes": free_bytes,
            "credit": 50 if free_bytes > 5 * 1024 * 1024 else 5,
            "status": "persisted",
            "disposition": "valid" if delivery.is_valid else "quarantined",
            "quarantine_reason": delivery.quarantine_reason,
        }
        raw_receipt = json.dumps(receipt_payload, sort_keys=True).encode("utf-8")
        try:
            self._client.publish(receipt_topic, raw_receipt, qos=1, retain=False)
        except Exception as err:
            logger.warning("Failed to publish collector receipt to %s: %s", receipt_topic, err)

    def _publish_backpressure_receipt(
        self,
        topic: str,
        outbox_table: str,
        outbox_row_id: int,
        replay_id: str | None,
        compartment_id: str | None,
    ) -> None:
        receipt_topic = f"v1/receipts/{replay_id or 'all'}/{compartment_id or 'all'}"
        receipt_payload = {
            "receipt_id": f"bp-{uuid.uuid4().hex[:8]}",
            "outbox_table": outbox_table,
            "outbox_row_id": outbox_row_id,
            "replay_id": replay_id,
            "compartment_id": compartment_id,
            "spool_free_bytes": 0,
            "credit": 0,
            "status": "backpressure",
        }
        raw_receipt = json.dumps(receipt_payload, sort_keys=True).encode("utf-8")
        try:
            self._client.publish(receipt_topic, raw_receipt, qos=1, retain=False)
        except Exception as err:
            logger.warning("Failed to publish backpressure notice to %s: %s", receipt_topic, err)

    def check_and_pack_batches(self, force: bool = False) -> list[BatchRecord]:
        """Pack ready batches if batch size reached, timeout expired, or forced."""
        packed = []
        now = time.monotonic()
        _, _, unbatched = self.spool.count_spooled_records()

        should_pack = force or unbatched >= self.batch_size or (
            unbatched > 0 and (now - self._last_batch_time) >= self.batch_timeout
        )

        while should_pack and unbatched > 0:
            try:
                batch = self.batch_manager.pack_batch(max_records=self.batch_size)
                if not batch:
                    break
                packed.append(batch)
                self._last_batch_time = now
                _, _, unbatched = self.spool.count_spooled_records()
                should_pack = unbatched >= self.batch_size
            except QuotaExceededError as err:
                logger.warning("Ready directory quota exceeded during batch pack: %s. Holding in spool.", err)
                break

        return packed

    def run_once(self) -> list[BatchRecord]:
        return self.check_and_pack_batches()

    def start(self, exit_on_complete: bool = False, poll_interval: float = 0.5) -> None:
        """Start the collector MQTT loop and batch packing until stopped or completed."""
        import threading
        import paho.mqtt.client as mqtt
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            manual_ack=True,
            protocol=mqtt.MQTTv5,
        )
        if hasattr(client, "manual_ack_set"):
            client.manual_ack_set(True)
        if hasattr(client, "max_inflight_messages_set"):
            client.max_inflight_messages_set(50)
        if hasattr(client, "max_queued_messages_set"):
            client.max_queued_messages_set(100)

        subscribed_event = threading.Event()
        connection_error_event = threading.Event()
        telemetry_sub_mid: int | None = None
        control_sub_mid: int | None = None
        confirmed_subs: set[int] = set()
        sub_errors: list[str] = []

        def on_connect(c, userdata, flags, reason_code, properties):
            nonlocal telemetry_sub_mid, control_sub_mid
            confirmed_subs.clear()
            sub_errors.clear()
            subscribed_event.clear()
            if reason_code.is_failure:
                logger.error("Collector MQTT connection failed: %s", reason_code)
                connection_error_event.set()
                return
            logger.info("Collector connected to MQTT broker (%s:%d), subscribing...", self.mqtt_host, self.mqtt_port)
            res1, mid1 = c.subscribe("v1/telemetry/#", qos=1)
            res2, mid2 = c.subscribe("v1/control/#", qos=1)
            telemetry_sub_mid = mid1
            control_sub_mid = mid2

        def on_subscribe(c, userdata, mid, reason_codes, properties):
            for rc in reason_codes:
                if hasattr(rc, "is_failure") and rc.is_failure:
                    sub_errors.append(f"Subscription mid={mid} failed: {rc}")
                elif isinstance(rc, int) and rc >= 0x80:
                    sub_errors.append(f"Subscription mid={mid} failed code={rc}")
            confirmed_subs.add(mid)
            expected = {telemetry_sub_mid, control_sub_mid} - {None}
            if expected and expected.issubset(confirmed_subs):
                subscribed_event.set()

        def on_message(c, userdata, message):
            self.handle_message(message)

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        self._client = client

        logger.info("Connecting collector to broker at %s:%d...", self.mqtt_host, self.mqtt_port)
        connect_properties = Properties(PacketTypes.CONNECT)
        connect_properties.ReceiveMaximum = 50
        connect_properties.MaximumPacketSize = self.max_packet_bytes
        if not self.clean_session:
            connect_properties.SessionExpiryInterval = self.session_expiry_interval

        client.connect(
            self.mqtt_host,
            self.mqtt_port,
            keepalive=30,
            clean_start=self.clean_session,
            properties=connect_properties,
        )
        client.loop_start()
        self._running = True
        if not subscribed_event.wait(timeout=5.0):
            self.stop()
            raise CollectorError("Timed out waiting for broker SUBACK confirmation on subscriptions")

        if sub_errors:
            self.stop()
            raise CollectorError(f"Collector subscription failed: {', '.join(sub_errors)}")
        if connection_error_event.is_set():
            self.stop()
            raise CollectorError("Collector connection failed")

        try:
            while self._running:
                force_pack = bool(exit_on_complete and self._completion_manifest_received)
                self.check_and_pack_batches(force=force_pack)
                if exit_on_complete and self._completion_manifest_received:
                    _, _, unbatched = self.spool.count_spooled_records()
                    if unbatched == 0:
                        logger.info("Completion manifest processed and all batches sealed; exiting.")
                        break
                time.sleep(poll_interval)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop MQTT client and pack any remaining deliveries into batches."""
        self._running = False
        if self._client:
            try:
                self._client.loop_stop()
                if not self.clean_session:
                    from paho.mqtt.packettypes import PacketTypes
                    from paho.mqtt.properties import Properties
                    disc_props = Properties(PacketTypes.DISCONNECT)
                    disc_props.SessionExpiryInterval = self.session_expiry_interval
                    self._client.disconnect(properties=disc_props)
                else:
                    self._client.disconnect()
            except Exception:
                pass
            self._client = None
        # Final batch pack on shutdown
        try:
            self.batch_manager.pack_batch(max_records=self.batch_size)
        except Exception:
            pass
