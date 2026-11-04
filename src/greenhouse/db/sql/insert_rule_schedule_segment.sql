INSERT INTO config.rule_schedule_segment
 (rule_version, metric, measurement_context, basis,
  source_start, source_end, delta, lower, upper)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
