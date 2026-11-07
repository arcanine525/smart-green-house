"""Read-only catalog for isolated replay publications and source rules."""


def current_replays(connection):
    if connection.execute("SELECT to_regclass('sensor_stream.current_publication')").fetchone()[0] is None:
        return ()
    return tuple(connection.execute('''SELECT p.replay_id,p.publication_id,p.data_scope_id,
            p.cycle_id,p.compartment_id,p.observed_through,p.received_through,p.state,p.created_at
        FROM sensor_stream.current_publication c
        JOIN sensor_stream.publication p USING (replay_id,publication_id)
        JOIN sensor_stream.publication_seal s USING (replay_id,publication_id)
        ORDER BY p.created_at DESC,p.replay_id''').fetchall())


def source_rules(connection, snapshot_id, compartment_id, context, basis):
    """List explicitly compatible historical revisions; never choose one automatically."""
    if context not in ('climate', 'drain_process_computer'):
        raise ValueError('unknown replay rule context')
    metrics = ['temperature', 'co2'] if context == 'climate' else ['ec', 'ph']
    allowed_bases = ['requested', 'realized'] if context == 'climate' else ['measured']
    return tuple(connection.execute('''SELECT DISTINCT r.rule_version,r.label,r.is_demo,r.evidence_reference
        FROM config.rule_schedule_revision r
        JOIN meta.data_scope d ON d.data_scope_id=r.data_scope_id
        JOIN config.rule_schedule_segment s ON s.rule_version=r.rule_version
        WHERE d.snapshot_id=%s AND r.compartment_id=%s AND s.measurement_context=%s AND s.basis=%s
          AND s.metric=ANY(%s)
          AND NOT EXISTS (SELECT 1 FROM config.rule_schedule_segment other
              WHERE other.rule_version=r.rule_version AND NOT
                (other.measurement_context=%s AND other.metric=ANY(%s) AND other.basis=ANY(%s)))
        ORDER BY r.rule_version''', (snapshot_id, compartment_id, context, basis, metrics,
                                      context, metrics, allowed_bases)).fetchall())


def latest_quality(connection, metadata, event):
    from greenhouse.sensor.replay_slots import build_replay_rows
    replay, scope, cycle_id, compartment, policy, *_ = metadata
    cycle = connection.execute('SELECT end_source_time_exclusive FROM core.growing_cycle WHERE cycle_id=%s AND compartment_id=%s',
                               (cycle_id, compartment)).fetchone()
    if not cycle:
        raise ValueError('replay cycle not found')
    climate, _ = build_replay_rows(connection, [event], replay_id=replay, scope_id=scope,
        cycle_id=cycle_id, compartment_id=compartment, quality_policy_version=policy, cycle_end=cycle[0])
    return tuple({'measure': code, **verdict} for code, verdict in sorted(climate[0].measure_quality.items()))
