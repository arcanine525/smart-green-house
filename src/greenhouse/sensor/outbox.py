"""Transactional outbox and state persistence for sensor replay.

Uses a local SQLite transaction store (outbox.db) for ACID checkpointing,
atomic single-dispatch transactions, exact retry lookup, and persistent audit logging.
Maintains streaming JSONL exports with bounded memory.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Generator

INITIAL_CUMULATIVE_HASH = "0" * 64


class OutboxError(RuntimeError):
    """Base error for outbox operations."""


class CorruptStateError(OutboxError):
    """Raised when an existing state file or database is corrupt or invalid."""


class StateMismatchError(OutboxError):
    """Raised when resuming with incompatible replay ID, source file, or configuration."""


@dataclass(frozen=True)
class CheckpointData:
    checkpoint_id: int
    sequence: int
    line_number: int
    control_sequence: int
    cumulative_hash: str
    expected_unique_events: int
    emitted_unique_events: int
    emitted_total_messages: int
    fault_state: dict[str, Any]
    status: str  # "running" | "paused" | "completed"
    last_source_observed: str | None = None
    rejected_source_rows: int = 0


ReplayState = CheckpointData


class DurableOutbox:
    """Manages transactional outbox storage, event retry lookup, and crash recovery."""

    def __init__(self, outbox_dir: Path | str):
        self.dir = Path(outbox_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "outbox.db"
        self.events_jsonl_path = self.dir / "telemetry_outbox.jsonl"
        self.control_jsonl_path = self.dir / "control_outbox.jsonl"
        self.state_json_path = self.dir / "replay_state.json"
        self.manifest_json_path = self.dir / "fault_manifest.json"

        self._init_db()

    def _get_raw_connection(self) -> sqlite3.Connection:
        """Create connection outside transaction for PRAGMAs and DDL."""
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Safely yield a connection and guarantee it is explicitly closed."""
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

                CREATE TABLE IF NOT EXISTS telemetry_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    payload_bytes BLOB NOT NULL,
                    payload_hash TEXT NOT NULL,
                    cumulative_hash TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_telemetry_event_id ON telemetry_messages(event_id);
                CREATE INDEX IF NOT EXISTS idx_telemetry_sequence ON telemetry_messages(sequence);

                CREATE TABLE IF NOT EXISTS control_messages (
                    control_sequence INTEGER PRIMARY KEY,
                    control_id TEXT NOT NULL UNIQUE,
                    control_type TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    payload_bytes BLOB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fault_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    step INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    fault_type TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    source_line INTEGER NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sequence INTEGER NOT NULL,
                    line_number INTEGER NOT NULL,
                    control_sequence INTEGER NOT NULL,
                    cumulative_hash TEXT NOT NULL,
                    expected_unique_events INTEGER NOT NULL,
                    emitted_unique_events INTEGER NOT NULL,
                    emitted_total_messages INTEGER NOT NULL,
                    rejected_source_rows INTEGER NOT NULL DEFAULT 0,
                    fault_state_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_source_observed TEXT,
                    created_at TEXT NOT NULL
                );
            """)

            # Backwards compatibility: add rejected_source_rows column if missing in pre-fix schema
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(checkpoints)")
            ckpt_cols = {r[1] for r in cursor.fetchall()}
            if "rejected_source_rows" not in ckpt_cols:
                conn.execute("ALTER TABLE checkpoints ADD COLUMN rejected_source_rows INTEGER NOT NULL DEFAULT 0")

    def initialize_run(
        self,
        replay_id: str,
        data_scope_id: str,
        compartment_id: str,
        cycle_id: str,
        source_file: str,
        source_file_checksum: str,
        config_hash: str,
        resume: bool,
        initial_fault_state: dict[str, Any] | None = None,
    ) -> CheckpointData | None:
        """Validate or initialize replay metadata in outbox.

        If resume=False and outbox already contains ANY existing replay data, raise OutboxError.
        Never silently overwrite or delete existing outbox contents!
        If resume=True, verify config_hash, source checksum, and replay_id; return latest valid checkpoint.
        """
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM meta")
            existing_meta = dict(cursor.fetchall())

            if not resume:
                if existing_meta:
                    existing_id = existing_meta.get("replay_id", "unknown")
                    raise OutboxError(
                        f"Outbox directory '{self.dir}' already contains existing replay '{existing_id}'. "
                        "Specify a new --outbox-dir or pass --resume to continue."
                    )

                # Initialize fresh meta in transaction
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for k, v in [
                        ("replay_id", replay_id),
                        ("data_scope_id", data_scope_id),
                        ("compartment_id", compartment_id),
                        ("cycle_id", cycle_id),
                        ("source_file", source_file),
                        ("source_file_checksum", source_file_checksum),
                        ("config_hash", config_hash),
                        ("status", "running"),
                    ]:
                        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (k, v))

                    # Initial empty checkpoint at start of run
                    fault_json = json.dumps(initial_fault_state or {})
                    now_str = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "INSERT INTO checkpoints (sequence, line_number, control_sequence, cumulative_hash, "
                        "expected_unique_events, emitted_unique_events, emitted_total_messages, rejected_source_rows, fault_state_json, status, last_source_observed, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (0, 1, 0, INITIAL_CUMULATIVE_HASH, 0, 0, 0, 0, fault_json, "running", None, now_str),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                return None

            # Resuming
            if not existing_meta:
                raise OutboxError("Cannot resume: outbox contains no existing replay metadata")

            if existing_meta.get("replay_id") != replay_id:
                raise StateMismatchError(
                    f"Resume replay_id mismatch: existing={existing_meta.get('replay_id')} vs requested={replay_id}"
                )
            if existing_meta.get("data_scope_id") != data_scope_id:
                raise StateMismatchError(
                    f"Resume data_scope_id mismatch: existing={existing_meta.get('data_scope_id')} vs requested={data_scope_id}"
                )
            if existing_meta.get("compartment_id") != compartment_id:
                raise StateMismatchError(
                    f"Resume compartment_id mismatch: existing={existing_meta.get('compartment_id')} vs requested={compartment_id}"
                )
            if existing_meta.get("cycle_id") != cycle_id:
                raise StateMismatchError(
                    f"Resume cycle_id mismatch: existing={existing_meta.get('cycle_id')} vs requested={cycle_id}"
                )
            if existing_meta.get("source_file_checksum") != source_file_checksum:
                raise StateMismatchError(
                    f"Resume source file checksum mismatch: outbox has {existing_meta.get('source_file_checksum')}"
                )
            if existing_meta.get("config_hash") != config_hash:
                raise StateMismatchError(
                    "Resume configuration hash mismatch: replay configuration has drifted"
                )
            if existing_meta.get("status") == "completed":
                raise OutboxError("Replay is already completed; new replay requires new replay_id")

            cursor.execute(
                "SELECT checkpoint_id, sequence, line_number, control_sequence, cumulative_hash, "
                "expected_unique_events, emitted_unique_events, emitted_total_messages, fault_state_json, status, last_source_observed, rejected_source_rows "
                "FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                raise CorruptStateError("Cannot resume: state metadata exists but no valid checkpoint found")

            # Reconcile any uncommitted tail messages and audits past the checkpoint
            ckpt_seq = row[1]
            ckpt_line = row[2]
            ckpt_ctrl_seq = row[3]

            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM telemetry_messages WHERE sequence > ?", (ckpt_seq,))
                conn.execute("DELETE FROM control_messages WHERE control_sequence > ?", (ckpt_ctrl_seq,))
                conn.execute("DELETE FROM fault_audit WHERE source_line > ?", (ckpt_line,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            # Reconstruct or read rejected_source_rows:
            # If migrating from pre-fix candidate where checkpoints.rejected_source_rows was 0,
            # reconstruct from persisted fault_audit records up to ckpt_line.
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM fault_audit WHERE fault_type = 'rejected_source' AND source_line <= ?", (ckpt_line,))
            audit_rejected = cur.fetchone()[0]
            col_rejected = row[11] if len(row) > 11 and row[11] is not None else 0
            reconciled_rejected = max(col_rejected, audit_rejected)

            ckpt = CheckpointData(
                checkpoint_id=row[0],
                sequence=row[1],
                line_number=row[2],
                control_sequence=row[3],
                cumulative_hash=row[4],
                expected_unique_events=row[5],
                emitted_unique_events=row[6],
                emitted_total_messages=row[7],
                fault_state=json.loads(row[8]),
                status=row[9],
                last_source_observed=row[10] if len(row) > 10 else None,
                rejected_source_rows=reconciled_rejected,
            )

            return ckpt

    def commit_dispatch_transaction(
        self,
        telemetry_messages: list[tuple[str, bytes, str, int, int]],  # (topic, raw_bytes, event_id, sequence, line_no)
        control_messages: list[tuple[str, bytes, str, int, str]],    # (topic, raw_bytes, control_id, control_seq, control_type)
        fault_audits: list[dict[str, Any]],
        sequence: int,
        line_number: int,
        control_sequence: int,
        previous_cumulative_hash: str,
        expected_unique: int,
        emitted_unique: int,
        emitted_total: int,
        fault_state: dict[str, Any],
        status: str,
        last_source_observed: str | None = None,
        rejected_source_rows: int = 0,
    ) -> tuple[str, int, int]:
        """Atomically commit ONE logical dispatch step covering messages, controls, audits, and checkpoint."""
        cum_hash = previous_cumulative_hash
        now_str = datetime.now(timezone.utc).isoformat()
        fault_json = json.dumps(fault_state)

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 1. Telemetry messages
                for topic, raw_bytes, eid, seq, lno in telemetry_messages:
                    payload_hash = hashlib.sha256(raw_bytes).hexdigest()
                    cum_hash = hashlib.sha256(f"{cum_hash}:{payload_hash}".encode("ascii")).hexdigest()
                    conn.execute(
                        "INSERT INTO telemetry_messages (sequence, event_id, topic, line_number, payload_bytes, payload_hash, cumulative_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (seq, eid, topic, lno, raw_bytes, payload_hash, cum_hash),
                    )

                # Query DB for truthful unique events & total count
                cur = conn.cursor()
                cur.execute("SELECT COUNT(DISTINCT event_id), COUNT(*) FROM telemetry_messages")
                db_unique, db_total = cur.fetchone()

                # 2. Control messages
                for topic, raw_bytes, cid, cseq, ctype in control_messages:
                    conn.execute(
                        "INSERT INTO control_messages (control_sequence, control_id, control_type, topic, payload_bytes) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (cseq, cid, ctype, topic, raw_bytes),
                    )

                # 3. Fault audit
                for entry in fault_audits:
                    conn.execute(
                        "INSERT INTO fault_audit (step, event_id, fault_type, sequence, source_line, details_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (entry["step"], entry["event_id"], entry["fault_type"], entry["sequence"], entry["source_line"], json.dumps(entry)),
                    )

                # 4. Checkpoint
                conn.execute(
                    "INSERT INTO checkpoints (sequence, line_number, control_sequence, cumulative_hash, "
                    "expected_unique_events, emitted_unique_events, emitted_total_messages, rejected_source_rows, fault_state_json, status, last_source_observed, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sequence, line_number, control_sequence, cum_hash, expected_unique, db_unique, db_total, rejected_source_rows, fault_json, status, last_source_observed, now_str),
                )
                conn.execute("UPDATE meta SET value = ? WHERE key = 'status'", (status,))

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return cum_hash, db_unique, db_total

    def get_rejected_source_count(self) -> int:
        """Return count of rejected source records logged in fault_audit."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM fault_audit WHERE fault_type = 'rejected_source'")
            row = cursor.fetchone()
            return row[0] if row else 0

    def get_telemetry_counts(self) -> tuple[int, int]:
        """Return (distinct_event_count, total_message_count) from outbox."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(DISTINCT event_id), COUNT(*) FROM telemetry_messages")
            row = cursor.fetchone()
            return (row[0], row[1]) if row else (0, 0)

    def count_existing_event_ids(self, event_ids: list[str]) -> int:
        """Return count of distinct event_ids from a bounded candidate batch already in outbox."""
        if not event_ids:
            return 0
        unique_candidates = list(set(event_ids))
        placeholders = ",".join("?" for _ in unique_candidates)
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT COUNT(DISTINCT event_id) FROM telemetry_messages WHERE event_id IN ({placeholders})",
                unique_candidates,
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    def export_snapshot_jsonl(self) -> None:
        """Stream table contents into JSONL files using bounded cursor fetchmany."""
        with self._connection() as conn:
            # Stream telemetry_messages to temporary file then atomic rename
            tmp_telemetry = tempfile.NamedTemporaryFile(dir=self.dir, delete=False, mode="w", encoding="utf-8")
            try:
                cur = conn.cursor()
                cur.execute("SELECT topic, event_id, payload_bytes FROM telemetry_messages ORDER BY id ASC")
                while rows := cur.fetchmany(500):
                    for top, eid, pbytes in rows:
                        line_obj = {"topic": top, "event_id": eid, "payload": pbytes.decode("utf-8")}
                        tmp_telemetry.write(json.dumps(line_obj, ensure_ascii=False) + "\n")
                tmp_telemetry.flush()
                tmp_telemetry.close()
                os.replace(tmp_telemetry.name, self.events_jsonl_path)
            except Exception:
                if os.path.exists(tmp_telemetry.name):
                    os.remove(tmp_telemetry.name)
                raise

            # Stream control_messages to temporary file then atomic rename
            tmp_control = tempfile.NamedTemporaryFile(dir=self.dir, delete=False, mode="w", encoding="utf-8")
            try:
                cur = conn.cursor()
                cur.execute("SELECT topic, control_id, control_sequence, control_type, payload_bytes FROM control_messages ORDER BY control_sequence ASC")
                while crows := cur.fetchmany(500):
                    for top, cid, cseq, ctype, pbytes in crows:
                        line_obj = {
                            "topic": top,
                            "control_id": cid,
                            "control_sequence": cseq,
                            "control_type": ctype,
                            "payload": pbytes.decode("utf-8"),
                        }
                        tmp_control.write(json.dumps(line_obj, ensure_ascii=False) + "\n")
                tmp_control.flush()
                tmp_control.close()
                os.replace(tmp_control.name, self.control_jsonl_path)
            except Exception:
                if os.path.exists(tmp_control.name):
                    os.remove(tmp_control.name)
                raise

            # Export replay_state.json from latest checkpoint
            cur = conn.cursor()
            cur.execute(
                "SELECT sequence, line_number, control_sequence, cumulative_hash, "
                "expected_unique_events, emitted_unique_events, emitted_total_messages, status, rejected_source_rows "
                "FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                state_dict = {
                    "sequence": row[0],
                    "last_line_number": row[1],
                    "control_sequence": row[2],
                    "payload_checksum": row[3],
                    "expected_unique_events": row[4],
                    "emitted_unique_events": row[5],
                    "emitted_total_messages": row[6],
                    "status": row[7],
                    "rejected_source_rows": row[8] if len(row) > 8 and row[8] is not None else 0,
                }
                self.state_json_path.write_text(json.dumps(state_dict, indent=2, sort_keys=True), encoding="utf-8")

    def get_event_for_retry(self, event_id: str) -> tuple[str, bytes]:
        """Lookup original event in outbox for retry.

        Returns (topic, exact_payload_bytes). Raises KeyError if not found.
        """
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT topic, payload_bytes FROM telemetry_messages WHERE event_id = ? ORDER BY id ASC LIMIT 1",
                (event_id,),
            )
            row = cursor.fetchone()
            if row:
                return row[0], row[1]
        raise KeyError(f"Event {event_id} not found in outbox")
