"""Engineer-authored fixed-bound profiles use the shared immutable repository.

No automatic version bump, overwrite or publication change: the caller names
the profile and revision explicitly, supplies provenance, and commits it.
"""
from greenhouse.contracts import QueryContext
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.db.rule_schedule import register_rule_schedule


def save_drain_profile(connection, publication, cycle, *, name, version,
                       ec_lower, ec_upper, ph_lower, ph_upper, provenance):
    """Register one unverified revision; transaction commit belongs to caller.

    Re-saving identical content is idempotent; changing existing content is
    rejected by register_rule_schedule. Both metric intervals cover the chosen
    cycle in source-naive time. Old revisions remain explicitly selectable.
    """
    adapter = PostgresDrainQueryAdapter(connection)
    pinned = adapter.publication(publication.publication_id)
    if pinned != publication:
        raise ValueError('Profile phải dùng đúng publication đã pin')
    context = QueryContext(publication.scope_id, publication.publication_id,
                           publication.snapshot_id, cycle.cycle_id,
                           (cycle.compartment_id,), cycle.start, cycle.end)
    pinned_cycle = adapter.cycle_window(context)
    if (pinned_cycle.start, pinned_cycle.end) != (cycle.start, cycle.end):
        raise ValueError('Profile phải dùng đúng cửa sổ vụ đã pin')
    from greenhouse.queries.drain_profile import build_drain_profile
    revision=build_drain_profile(publication,cycle,name=name,version=version,ec_lower=ec_lower,ec_upper=ec_upper,ph_lower=ph_lower,ph_upper=ph_upper,provenance=provenance)
    register_rule_schedule(connection, revision)
    return revision
