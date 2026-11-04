SELECT rule_version,rule_id,version,data_scope_id AS scope_id,compartment_id,
 min_drain_pct,max_drain_pct,min_irrigation_l_m2,max_irrigation_l_m2,evidence_reference,is_demo,label,unit,ratio_unit
FROM config.irrigation_rule_profile WHERE rule_version=%s
