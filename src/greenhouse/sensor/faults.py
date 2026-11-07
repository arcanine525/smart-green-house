"""Deterministic seeded fault scheduler for sensor replay.

Supports duplicate, conflict, delay/reorder, and drop faults.
Every fault is bounded, reproducible, and tracked in an explicit audit manifest.
Supports full state checkpointing and restoration for seamless pause/resume.
"""

from copy import deepcopy
from decimal import Decimal
import json
import random
from typing import Any, Callable


class FaultScheduler:
    """Deterministic fault injector driven by an explicit seed and probabilities."""

    def __init__(
        self,
        seed: int = 42,
        duplicate_prob: float = 0.0,
        conflict_prob: float = 0.0,
        delay_prob: float = 0.0,
        drop_prob: float = 0.0,
        max_delay_steps: int = 3,
        audit_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        for name, p in (("duplicate", duplicate_prob), ("conflict", conflict_prob),
                        ("delay", delay_prob), ("drop", drop_prob)):
            if not isinstance(p, (int, float)) or not (0.0 <= p <= 1.0):
                raise ValueError(f"Probability {name}_prob must be finite float between 0.0 and 1.0, got {p}")

        total_prob = duplicate_prob + conflict_prob + delay_prob + drop_prob
        if total_prob > 1.0 + 1e-9:
            raise ValueError(f"Sum of fault probabilities cannot exceed 1.0, got {total_prob}")

        self.seed = seed
        self.duplicate_prob = float(duplicate_prob)
        self.conflict_prob = float(conflict_prob)
        self.delay_prob = float(delay_prob)
        self.drop_prob = float(drop_prob)
        self.max_delay_steps = max(1, max_delay_steps)
        self._audit_callback = audit_callback

        self._rng = random.Random(seed)
        self._step = 0
        # List of (release_step, topic, raw_bytes, event_id, sequence, line_number)
        self._delay_buffer: list[tuple[int, str, bytes, str, int, int]] = []
        self._pending_audits: list[dict[str, Any]] = []
        self._counts = {
            "duplicates": 0,
            "conflicts": 0,
            "delays": 0,
            "drops": 0,
        }

    def set_audit_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._audit_callback = callback

    def pop_pending_audits(self) -> list[dict[str, Any]]:
        audits = list(self._pending_audits)
        self._pending_audits.clear()
        return audits

    def _record_audit(self, entry: dict[str, Any]) -> None:
        self._pending_audits.append(entry)
        if self._audit_callback is not None:
            self._audit_callback(entry)

    def process(
        self,
        envelope: dict[str, Any],
        topic: str,
    ) -> list[tuple[str, bytes, str, int, int]]:
        """Process one telemetry envelope through the fault scheduler.

        Returns a list of (topic, message_bytes, event_id, sequence, line_number) ready for outbox/transmission.
        """
        self._step += 1
        event_id = envelope["event_id"]
        orig_seq = envelope["sequence"]
        orig_line = envelope["source_provenance"]["line_number"]
        emitted: list[tuple[str, bytes, str, int, int]] = []

        # 1. Release any delayed messages scheduled for this step or earlier
        remaining_buffer: list[tuple[int, str, bytes, str, int, int]] = []
        for rel_step, d_top, d_bytes, d_id, d_seq, d_line in self._delay_buffer:
            if rel_step <= self._step:
                emitted.append((d_top, d_bytes, d_id, d_seq, d_line))
            else:
                remaining_buffer.append((rel_step, d_top, d_bytes, d_id, d_seq, d_line))
        self._delay_buffer = remaining_buffer

        # 2. Decide fault for current message
        roll = self._rng.random()
        base_bytes = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

        # Check drop: message is omitted from wire completely
        if roll < self.drop_prob:
            self._counts["drops"] += 1
            self._record_audit({
                "step": self._step,
                "event_id": event_id,
                "fault_type": "drop",
                "sequence": orig_seq,
                "source_line": orig_line,
            })
            return emitted

        roll -= self.drop_prob

        # Check delay: message is held in buffer to be emitted out-of-order later
        if roll < self.delay_prob:
            self._counts["delays"] += 1
            delay_steps = self._rng.randint(1, self.max_delay_steps)
            release_at = self._step + delay_steps
            self._delay_buffer.append((release_at, topic, base_bytes, event_id, orig_seq, orig_line))
            self._record_audit({
                "step": self._step,
                "event_id": event_id,
                "fault_type": "delay",
                "delay_steps": delay_steps,
                "release_at_step": release_at,
                "sequence": orig_seq,
                "source_line": orig_line,
            })
            return emitted

        roll -= self.delay_prob

        # Check conflict: emit ORIGINAL payload PLUS a divergent variant with the same event_id!
        if roll < self.conflict_prob:
            self._counts["conflicts"] += 1
            conflict_env = deepcopy(envelope)
            # Mutate temperature or another available measurement
            actuals = conflict_env["payload"]["actual_measurements"]
            target_measure = "Tair" if "Tair" in actuals and actuals["Tair"] is not None else next(
                (k for k, v in actuals.items() if v is not None), None
            )
            old_val = actuals.get(target_measure)
            if target_measure and old_val is not None:
                new_val = str(Decimal(old_val) + Decimal("10.0"))
                actuals[target_measure] = new_val
            else:
                target_measure = "Tair"
                actuals["Tair"] = "99.9"
                if "Tair" in conflict_env["payload"]["null_reasons"]:
                    del conflict_env["payload"]["null_reasons"]["Tair"]
                new_val = "99.9"

            conflict_env["fault_tags"].append("injected_conflict")
            conflict_bytes = json.dumps(conflict_env, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

            self._record_audit({
                "step": self._step,
                "event_id": event_id,
                "fault_type": "conflict",
                "mutated_column": target_measure,
                "original_value": old_val,
                "conflicted_value": new_val,
                "sequence": orig_seq,
                "source_line": orig_line,
            })

            # Emit original first, then conflicting payload with identical event_id and original provenance!
            emitted.append((topic, base_bytes, event_id, orig_seq, orig_line))
            emitted.append((topic, conflict_bytes, event_id, orig_seq, orig_line))
            return emitted

        roll -= self.conflict_prob

        # Check duplicate: emit identical message twice
        if roll < self.duplicate_prob:
            self._counts["duplicates"] += 1
            self._record_audit({
                "step": self._step,
                "event_id": event_id,
                "fault_type": "duplicate",
                "sequence": orig_seq,
                "source_line": orig_line,
            })
            emitted.append((topic, base_bytes, event_id, orig_seq, orig_line))
            emitted.append((topic, base_bytes, event_id, orig_seq, orig_line))
            return emitted

        # Clean emission
        emitted.append((topic, base_bytes, event_id, orig_seq, orig_line))
        return emitted

    def flush_delayed(self) -> list[tuple[str, bytes, str, int, int]]:
        """Flush any remaining delayed messages at end of simulation."""
        flushed: list[tuple[str, bytes, str, int, int]] = []
        for _, top, b, eid, seq, lno in self._delay_buffer:
            flushed.append((top, b, eid, seq, lno))
        self._delay_buffer.clear()
        return flushed

    def snapshot_state(self) -> dict[str, Any]:
        """Capture complete internal RNG and queue state for checkpointing."""
        # Convert bytes in delay buffer to hex/latin1 for JSON storage
        serialized_delays = [
            (rel_step, top, b.decode("latin1"), eid, seq, lno)
            for rel_step, top, b, eid, seq, lno in self._delay_buffer
        ]
        return {
            "step": self._step,
            "rng_state": self._rng.getstate(),
            "delay_buffer": serialized_delays,
            "counts": dict(self._counts),
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore internal RNG and queue state from a checkpoint."""
        self._step = state["step"]
        raw_rng = state["rng_state"]
        # In JSON, tuples become lists; Python random.setstate requires the internal state vector to be a tuple
        restored_rng = (raw_rng[0], tuple(raw_rng[1]), raw_rng[2])
        self._rng.setstate(restored_rng)
        self._counts = dict(state["counts"])
        self._delay_buffer = [
            (rel_step, top, s.encode("latin1"), eid, seq, lno)
            for rel_step, top, s, eid, seq, lno in state["delay_buffer"]
        ]

    def manifest(self) -> dict[str, Any]:
        """Return the fault summary tally."""
        return {
            "seed": self.seed,
            "fault_counts": dict(self._counts),
            "total_faults": sum(self._counts.values()),
        }
