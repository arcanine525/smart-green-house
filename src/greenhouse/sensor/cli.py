"""Command-line interface for the Greenhouse Climate sensor simulator."""

import argparse
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import sys
import uuid

from greenhouse.sensor.faults import FaultScheduler
from greenhouse.sensor.outbox import DurableOutbox
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
    return parser


def simulate_command(argv: list[str] | None = None) -> int:
    parser = build_simulate_parser()
    args = parser.parse_args(argv)

    outbox = DurableOutbox(args.outbox_dir)

    # Handle retry lookup check
    if args.retry_event:
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

    # Locate source file if not explicitly given
    source_path = args.source_file
    if source_path is None:
        standard_raw_dir = Path(os.environ.get("GREENHOUSE_RAW_DIR", "/data/raw"))
        canonical_target = standard_raw_dir / f"agc-2nd-2019/{STANDARD_SNAPSHOT_SHA}/{args.compartment}/GreenhouseClimate.csv"
        local_target = Path(f"data/raw/agc-2nd-2019/{STANDARD_SNAPSHOT_SHA}/{args.compartment}/GreenhouseClimate.csv")
        if canonical_target.is_file():
            source_path = canonical_target
        elif local_target.is_file():
            source_path = local_target

    if source_path is None or not source_path.is_file():
        print(f"Error: Source file not found: {source_path}. Specify --source-file.", file=sys.stderr)
        return 1

    replay_id = args.replay_id
    if not replay_id:
        if args.resume:
            # When resuming without replay-id, query outbox metadata
            with outbox._connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT value FROM meta WHERE key = 'replay_id'")
                row = cur.fetchone()
                if not row:
                    print("Error: Cannot --resume without saved replay_id in outbox.", file=sys.stderr)
                    return 1
                replay_id = row[0]
        else:
            replay_id = f"replay-{uuid.uuid4().hex[:8]}"

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
            compartment_id=args.compartment,
            data_scope_id=args.data_scope_id,
            speed=args.speed,
            seed=args.seed,
            cursor_interval=args.cursor_interval,
            pause_after=args.pause_after,
            limit=args.limit,
            fault_scheduler=fault_scheduler,
        )

        result = simulator.run(resume=args.resume)
        print(f"SIMULATION_{result.status.upper()}: replay_id={result.replay_id} scope={result.data_scope_id}")
        print(
            f"Events: expected={result.expected_unique_events} unique_emitted={result.emitted_unique_events} "
            f"total_messages={result.emitted_total_messages} last_line={result.last_source_line}"
        )
        print(f"Payload checksum: {result.payload_checksum}")
        print(f"Fault summary: {json.dumps(result.fault_summary['fault_counts'])}")
        return 0
    except Exception as err:
        print(f"Simulation failed: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
