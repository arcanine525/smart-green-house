"""Command-line interface for the Greenhouse Climate sensor simulator."""

import argparse
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from greenhouse.sensor.collector import MqttCollector
from greenhouse.sensor.faults import FaultScheduler
from greenhouse.sensor.outbox import DurableOutbox
from greenhouse.sensor.publisher import OutboxPublisher
from greenhouse.sensor.simulator import SensorSimulator

STANDARD_SNAPSHOT_ID = "agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"
STANDARD_SNAPSHOT_SHA = "c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"


def build_simulate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m greenhouse simulate",
        description="Deterministic Climate sensor replay simulator with versioned envelope, faults, and outbox.",
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        help="Path to GreenhouseClimate.csv source file.",
    )
    parser.add_argument(
        "--compartment",
        default="AICU",
        help="Target compartment identifier (default: AICU).",
    )
    parser.add_argument(
        "--replay-id",
        help="Explicit replay ID (default: generates a new unique replay ID).",
    )
    parser.add_argument(
        "--data-scope-id",
        help="Explicit data scope ID (default: {snapshot_id}:sim-{replay_id}).",
    )
    parser.add_argument(
        "--outbox-dir",
        type=Path,
        default=Path("/data/outbox") if Path("/data/outbox").is_dir() else Path("tmp/outbox"),
        help="Directory where durable telemetry, control, and state are stored.",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=0.0,
        help="Speed multiplier (0.0 = as-fast-as-possible / test mode, 1.0 = 1x real-time).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Pseudo-random generator seed for deterministic fault scheduling (default: 42).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of source events to process.",
    )
    parser.add_argument(
        "--pause-after",
        type=int,
        help="Pause simulation after processing N events.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume simulation from the saved outbox state.",
    )
    parser.add_argument(
        "--retry-event",
        metavar="EVENT_ID",
        help="Retrieve exact stored bytes and topic for an event ID to verify retry stability.",
    )
    parser.add_argument(
        "--cursor-interval",
        type=int,
        default=10,
        help="Emit cursor control message every N events (default: 10).",
    )
    parser.add_argument(
        "--duplicate-rate",
        type=float,
        default=0.0,
        help="Probability of injecting a duplicate event (default: 0.0).",
    )
    parser.add_argument(
        "--conflict-rate",
        type=float,
        default=0.0,
        help="Probability of injecting a conflicted payload (default: 0.0).",
    )
    parser.add_argument(
        "--delay-rate",
        type=float,
        default=0.0,
        help="Probability of delaying an event (default: 0.0).",
    )
    parser.add_argument(
        "--drop-rate",
        type=float,
        default=0.0,
        help="Probability of dropping an event (default: 0.0).",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish generated outbox messages to MQTT broker with end-to-end receipt tracking.",
    )
    parser.add_argument(
        "--publish-only",
        action="store_true",
        help="Publish existing unconfirmed outbox messages without running or re-running generation.",
    )
    parser.add_argument(
        "--mqtt-host",
        default=os.environ.get("MQTT_HOST", "127.0.0.1"),
        help="MQTT broker host (default: $MQTT_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=int(os.environ.get("MQTT_PORT", "1883")),
        help="MQTT broker port (default: $MQTT_PORT or 1883).",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=20,
        help="Sliding window size for unreceipted in-flight messages (default: 20).",
    )
    return parser


def simulate_command(argv: list[str] | None = None) -> int:
    parser = build_simulate_parser()
    args = parser.parse_args(argv)

    outbox_dir = Path(args.outbox_dir)
    outbox_db = outbox_dir / "outbox.db"

    # Handle direct publish-only mode on an existing initialized outbox directory
    if args.publish_only:
        if not outbox_db.is_file():
            print(f"Error: Outbox database not found at {outbox_db}. Cannot publish nonexistent or uninitialized outbox.", file=sys.stderr)
            return 1

        # Read-only validation that outbox is initialized without modifying disk
        try:
            with sqlite3.connect(f"file:{outbox_db}?mode=ro", uri=True) as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'")
                if not cur.fetchone():
                    print(f"Error: Outbox at {outbox_dir} has no meta table; uninitialized outbox.", file=sys.stderr)
                    return 1
                cur.execute("SELECT value FROM meta WHERE key = 'replay_id'")
                row = cur.fetchone()
                if not row or not row[0]:
                    print(f"Error: Outbox at {outbox_dir} is uninitialized; missing replay_id.", file=sys.stderr)
                    return 1
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry_messages'")
                if not cur.fetchone():
                    print(f"Error: Outbox at {outbox_dir} is uninitialized; missing telemetry_messages table.", file=sys.stderr)
                    return 1
        except Exception as err:
            print(f"Error: Failed to inspect outbox database at {outbox_db}: {err}", file=sys.stderr)
            return 1

        print(f"PUBLISHING_EXISTING_OUTBOX: dir={args.outbox_dir} broker={args.mqtt_host}:{args.mqtt_port} window={args.window_size}")
        publisher = OutboxPublisher(
            outbox_dir=args.outbox_dir,
            mqtt_host=args.mqtt_host,
            mqtt_port=args.mqtt_port,
            max_inflight=args.window_size,
        )
        publisher.start()
        try:
            published_count = publisher.publish_all_pending()
            print(f"PUBLISH_COMPLETED: published={published_count} messages confirmed by collector receipts")
            return 0
        except Exception as err:
            print(f"PUBLISH_FAILED: {type(err).__name__}: {err}", file=sys.stderr)
            return 1
        finally:
            publisher.stop()

    # Handle retry lookup check
    if args.retry_event:
        if not outbox_db.is_file():
            print(f"RETRY_ERROR: Outbox database not found at {outbox_db}", file=sys.stderr)
            return 1
        outbox = DurableOutbox(args.outbox_dir)
        try:
            topic, payload_bytes = outbox.get_event_for_retry(args.retry_event)
            print(f"RETRY_FOUND topic={topic} bytes={len(payload_bytes)}")
            print(payload_bytes.decode("utf-8"))
            return 0
        except KeyError as err:
            print(f"RETRY_ERROR: {err}", file=sys.stderr)
            return 1

    # Validate numerical parameters strictly
    for name, val in [
        ("duplicate-rate", args.duplicate_rate),
        ("conflict-rate", args.conflict_rate),
        ("delay-rate", args.delay_rate),
        ("drop-rate", args.drop_rate),
        ("speed", args.speed),
    ]:
        if math.isnan(val) or math.isinf(val) or val < 0.0:
            print(f"Error: Parameter --{name} must be a nonnegative finite number, got {val}", file=sys.stderr)
            return 2

    for name, val in [
        ("duplicate-rate", args.duplicate_rate),
        ("conflict-rate", args.conflict_rate),
        ("delay-rate", args.delay_rate),
        ("drop-rate", args.drop_rate),
    ]:
        if val > 1.0:
            print(f"Error: Fault rate --{name} cannot exceed 1.0, got {val}", file=sys.stderr)
            return 2

    sum_probs = args.duplicate_rate + args.conflict_rate + args.delay_rate + args.drop_rate
    if sum_probs > 1.0 + 1e-9:
        print(f"Error: Sum of fault rates cannot exceed 1.0 (mutually exclusive faults), got {sum_probs}", file=sys.stderr)
        return 2

    for name, val in [("limit", args.limit), ("pause-after", args.pause_after), ("cursor-interval", args.cursor_interval)]:
        if val is not None and val < 1:
            print(f"Error: Parameter --{name} must be >= 1, got {val}", file=sys.stderr)
            return 2

    raw_argv = argv if argv is not None else sys.argv[1:]
    compartment_specified = any(arg == "--compartment" or arg.startswith("--compartment=") for arg in raw_argv)

    # For resume, validate existing outbox and preserve saved original identity/config
    saved_meta: dict[str, str] = {}
    if args.resume:
        if not outbox_db.is_file():
            print(f"Error: Cannot --resume: outbox database not found at {outbox_db}. Specify an existing outbox directory.", file=sys.stderr)
            return 1
        try:
            with sqlite3.connect(f"file:{outbox_db}?mode=ro", uri=True) as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'")
                if not cur.fetchone():
                    print(f"Error: Cannot --resume: outbox at {outbox_dir} has no meta table.", file=sys.stderr)
                    return 1
                cur.execute("SELECT key, value FROM meta")
                saved_meta = dict(cur.fetchall())
                if "replay_id" not in saved_meta:
                    print(f"Error: Cannot --resume: outbox at {outbox_dir} contains no saved replay_id.", file=sys.stderr)
                    return 1
        except Exception as err:
            print(f"Error: Cannot --resume: failed to read outbox at {outbox_db}: {err}", file=sys.stderr)
            return 1

        saved_replay_id = saved_meta["replay_id"]
        saved_scope_id = saved_meta.get("data_scope_id")
        saved_compartment = saved_meta.get("compartment_id")

        if args.replay_id and args.replay_id != saved_replay_id:
            print(f"Error: Cannot --resume: requested replay_id '{args.replay_id}' does not match saved replay_id '{saved_replay_id}'.", file=sys.stderr)
            return 1

        if args.data_scope_id and args.data_scope_id != saved_scope_id:
            print(f"Error: Cannot --resume: requested data_scope_id '{args.data_scope_id}' does not match saved data_scope_id '{saved_scope_id}'.", file=sys.stderr)
            return 1

        replay_id = saved_replay_id
        data_scope_id = args.data_scope_id or saved_scope_id
        compartment_id = args.compartment if compartment_specified else (saved_compartment or args.compartment)
    else:
        replay_id = args.replay_id or f"replay-{uuid.uuid4().hex[:8]}"
        data_scope_id = args.data_scope_id
        compartment_id = args.compartment

    # Locate source file if not explicitly given
    source_path = args.source_file
    if source_path is None:
        standard_raw_dir = Path(os.environ.get("GREENHOUSE_RAW_DIR", "/data/raw"))
        canonical_target = standard_raw_dir / f"agc-2nd-2019/{STANDARD_SNAPSHOT_SHA}/{compartment_id}/GreenhouseClimate.csv"
        local_target = Path(f"data/raw/agc-2nd-2019/{STANDARD_SNAPSHOT_SHA}/{compartment_id}/GreenhouseClimate.csv")
        if canonical_target.is_file():
            source_path = canonical_target
        elif local_target.is_file():
            source_path = local_target
        elif args.resume and saved_meta.get("source_file"):
            cand = standard_raw_dir / f"agc-2nd-2019/{STANDARD_SNAPSHOT_SHA}/{saved_meta['source_file']}"
            if cand.is_file():
                source_path = cand

    if source_path is None or not source_path.is_file():
        print(f"Error: Source file not found: {source_path}. Specify --source-file.", file=sys.stderr)
        return 1

    try:
        fault_scheduler = FaultScheduler(
            seed=args.seed,
            duplicate_prob=args.duplicate_rate,
            conflict_prob=args.conflict_rate,
            delay_prob=args.delay_rate,
            drop_prob=args.drop_rate,
        )

        simulator = SensorSimulator(
            source_path=source_path,
            replay_id=replay_id,
            outbox_dir=args.outbox_dir,
            compartment_id=compartment_id,
            data_scope_id=data_scope_id,
            speed=args.speed,
            seed=args.seed,
            cursor_interval=args.cursor_interval,
            pause_after=args.pause_after,
            limit=args.limit,
            fault_scheduler=fault_scheduler,
        )

        # Handle completed replay resume
        if args.resume and saved_meta.get("status") == "completed":
            saved_config_hash = saved_meta.get("config_hash")
            if saved_config_hash and simulator.config_hash != saved_config_hash:
                print("Simulation failed: StateMismatchError: Resume configuration hash mismatch: replay configuration has drifted", file=sys.stderr)
                return 1

            if args.publish:
                print(f"SIMULATION_ALREADY_COMPLETED: replay_id={replay_id} status=completed; resuming transport publishing.")
                print(f"PUBLISHING_OUTBOX: broker={args.mqtt_host}:{args.mqtt_port} window={args.window_size}")
                publisher = OutboxPublisher(
                    outbox_dir=args.outbox_dir,
                    mqtt_host=args.mqtt_host,
                    mqtt_port=args.mqtt_port,
                    max_inflight=args.window_size,
                )
                publisher.start()
                try:
                    published_count = publisher.publish_all_pending()
                    print(f"PUBLISH_COMPLETED: published={published_count} messages confirmed by collector receipts")
                    return 0
                except Exception as err:
                    print(f"PUBLISH_FAILED: {type(err).__name__}: {err}", file=sys.stderr)
                    return 1
                finally:
                    publisher.stop()
            else:
                print("Error: Replay is already completed; new replay requires new replay_id", file=sys.stderr)
                return 1

        result = simulator.run(resume=args.resume)
        print(f"SIMULATION_{result.status.upper()}: replay_id={result.replay_id} scope={result.data_scope_id}")
        print(
            f"Events: expected={result.expected_unique_events} unique_emitted={result.emitted_unique_events} "
            f"total_messages={result.emitted_total_messages} last_line={result.last_source_line}"
        )
        print(f"Payload checksum: {result.payload_checksum}")
        print(f"Fault summary: {json.dumps(result.fault_summary['fault_counts'])}")

        # If publish requested, stream outbox to MQTT broker
        if args.publish:
            print(f"PUBLISHING_OUTBOX: broker={args.mqtt_host}:{args.mqtt_port} window={args.window_size}")
            publisher = OutboxPublisher(
                outbox_dir=args.outbox_dir,
                mqtt_host=args.mqtt_host,
                mqtt_port=args.mqtt_port,
                max_inflight=args.window_size,
            )
            publisher.start()
            try:
                published_count = publisher.publish_all_pending()
                print(f"PUBLISH_COMPLETED: published={published_count} messages confirmed by collector receipts")
            except Exception as err:
                print(f"PUBLISH_FAILED: {type(err).__name__}: {err}", file=sys.stderr)
                return 1
            finally:
                publisher.stop()

        return 0
    except Exception as err:
        print(f"Simulation failed: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


def build_collect_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m greenhouse collect",
        description="Durable MQTT collector with manual ACK, transactional spool, and atomic raw-ready batching.",
    )
    parser.add_argument(
        "--spool-dir",
        type=Path,
        default=Path("/data/spool") if Path("/data/spool").is_dir() else Path("tmp/spool"),
        help="Directory where transactional spool database and temporary buffers reside.",
    )
    parser.add_argument(
        "--ready-dir",
        type=Path,
        default=Path("/data/ready") if Path("/data/ready").is_dir() else Path("tmp/ready"),
        help="Directory where completed, immutable raw-ready JSONL batches are staged.",
    )
    parser.add_argument(
        "--mqtt-host",
        default=os.environ.get("MQTT_HOST", "127.0.0.1"),
        help="MQTT broker host (default: $MQTT_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=int(os.environ.get("MQTT_PORT", "1883")),
        help="MQTT broker port (default: $MQTT_PORT or 1883).",
    )
    parser.add_argument(
        "--client-id",
        default="greenhouse-collector",
        help="MQTT client identifier for persistent sessions (default: greenhouse-collector).",
    )
    parser.add_argument(
        "--clean-session",
        action="store_true",
        default=False,
        help="Whether to request a clean session (default: False for persistent sessions).",
    )
    parser.add_argument(
        "--session-expiry-interval",
        type=int,
        default=86400,
        help="MQTT v5 Session Expiry Interval in seconds (default: 86400).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Maximum records packed per raw-ready JSONL batch (default: 50).",
    )
    parser.add_argument(
        "--batch-timeout",
        type=float,
        default=5.0,
        help="Maximum seconds to hold unbatched records before sealing a batch (default: 5.0).",
    )
    parser.add_argument(
        "--spool-quota-mb",
        type=int,
        default=50,
        help="Spool directory quota in MiB before witholding ACK (default: 50).",
    )
    parser.add_argument(
        "--ready-quota-mb",
        type=int,
        default=100,
        help="Ready directory quota in MiB (default: 100).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds for batch packing loop (default: 0.5).",
    )
    parser.add_argument(
        "--exit-on-complete",
        action="store_true",
        help="Exit automatically once completion_manifest control message is received and sealed.",
    )
    return parser


def collect_command(argv: list[str] | None = None) -> int:
    parser = build_collect_parser()
    args = parser.parse_args(argv)

    try:
        collector = MqttCollector(
            spool_dir=args.spool_dir,
            ready_dir=args.ready_dir,
            mqtt_host=args.mqtt_host,
            mqtt_port=args.mqtt_port,
            client_id=args.client_id,
            clean_session=args.clean_session,
            session_expiry_interval=args.session_expiry_interval,
            batch_size=args.batch_size,
            batch_timeout=args.batch_timeout,
            spool_quota_bytes=args.spool_quota_mb * 1024 * 1024,
            ready_quota_bytes=args.ready_quota_mb * 1024 * 1024,
        )
        print(f"COLLECTOR_STARTING: broker={args.mqtt_host}:{args.mqtt_port} spool={args.spool_dir} ready={args.ready_dir}")
        collector.start(exit_on_complete=args.exit_on_complete, poll_interval=args.poll_interval)
        print("COLLECTOR_STOPPED: clean shutdown complete.")
        return 0
    except Exception as err:
        print(f"Collector failed: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
