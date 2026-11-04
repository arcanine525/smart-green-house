"""Append an explicitly scoped, immutable rule version; no default thresholds."""
from greenhouse.contracts import IrrigationRuleProfile
from greenhouse.db.migrate import sql


def register_irrigation_rule(connection, profile: IrrigationRuleProfile):
    if not isinstance(profile, IrrigationRuleProfile):
        raise ValueError('Expected a validated irrigation rule profile')
    connection.execute(sql('insert_irrigation_rule'), (
        profile.rule_version, profile.rule_id, profile.version, profile.scope_id, profile.compartment_id,
        profile.min_drain_pct, profile.max_drain_pct, profile.min_irrigation_l_m2, profile.max_irrigation_l_m2,
        profile.unit, profile.ratio_unit, profile.evidence_reference, profile.is_demo, profile.label))
    return profile
