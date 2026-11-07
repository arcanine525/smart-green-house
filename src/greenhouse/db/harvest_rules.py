"""Append an explicitly scoped, immutable harvest rule version; no default."""
from greenhouse.contracts import HarvestRuleProfile
from greenhouse.db.migrate import sql


def register_harvest_rule(connection, profile: HarvestRuleProfile):
    if not isinstance(profile, HarvestRuleProfile):
        raise ValueError('Expected a validated harvest rule profile')
    connection.execute(sql('insert_harvest_rule'), (
        profile.rule_version, profile.rule_id, profile.version, profile.scope_id, profile.compartment_id,
        profile.max_class_b_share_pct, profile.unit, profile.ratio_unit, profile.evidence_reference,
        profile.is_demo, profile.label))
    return profile
