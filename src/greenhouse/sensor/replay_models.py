"""Replay result identity shared by backend implementations."""
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class ReplayEvaluation:
    publication_id: str
    replay_id: str
    scope_id: str
    quality_policy_version: str
    observed_through: datetime
    window_end: datetime
    provisional: bool
    climate_rule_version: str | None
    drain_rule_version: str | None
    climate: object
    drain: object


