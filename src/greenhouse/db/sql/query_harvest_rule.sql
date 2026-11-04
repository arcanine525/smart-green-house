SELECT rule_version,rule_id,version,data_scope_id AS scope_id,compartment_id,
 max_class_b_share_pct,evidence_reference,is_demo,label,unit,ratio_unit
FROM config.harvest_rule_profile
WHERE rule_version=%s
