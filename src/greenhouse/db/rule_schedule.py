"""Register/read shared immutable rule schedule revisions (BR-01/03, T14).

SQL lives here only. Semantics:
* A revision identity is exactly its `rule_version`; registration is
  idempotent when the stored content equals the candidate (same identity
  AND same segments) and refuses content drift for an existing identity.
* Registration is ATOMIC in exactly one repository-owned
  transaction/savepoint: the header and every segment are written together,
  so an injected append failure leaves no partial revision behind and a
  retry succeeds — on autocommit connections (automatic transaction) and on
  normal connections (including inside a caller's already-open outer
  transaction, where psycopg turns the block into a savepoint).
* Once committed, the revision is SEALED at the schema level (008): the
  header's registration_xid (stamped by trigger with pg_current_xact_id())
  no longer matches the current top-level xid, so NO later segment can join
  the same rule_version even when it would not overlap. The stamp is
  operational metadata and stays out of the logical revision DTO.
* Overlap enforcement is database-owned (008 exclusion constraint), so a
  repository bypass cannot create per-key overlap inside one revision.
* An evaluation selects at most ONE revision per requested metric key
  (checked in the pure engine); alternate explicitly selected revisions may
  cover the same historical interval at rest — but never the same metric
  key twice in one evaluation. The engine never chooses first/latest.
"""
from psycopg.rows import dict_row

from greenhouse.contracts import RuleBasis, RuleScheduleRevision, RuleScheduleSegment
from greenhouse.db.migrate import sql


def _revision_from_rows(rows):
    if not rows:
        return None
    first = rows[0]
    segments = tuple(sorted(
        (RuleScheduleSegment(
            metric=row['metric'], measurement_context=row['measurement_context'],
            basis=RuleBasis(row['basis']), start=row['source_start'],
            end=row['source_end'], delta=row['delta'], lower=row['lower'], upper=row['upper'])
         for row in rows if row['metric'] is not None),
        key=lambda segment: (segment.start, segment.metric, segment.measurement_context,
                             segment.basis.value, segment.end)))
    return RuleScheduleRevision(
        rule_version=first['rule_version'], rule_id=first['rule_id'],
        version=first['version'], scope_id=first['data_scope_id'],
        compartment_id=first['compartment_id'],
        evidence_reference=first['evidence_reference'], is_demo=first['is_demo'],
        label=first['label'], segments=segments)


def read_rule_schedule(connection, rule_version):
    """Stored revision as a validated contract, or None when unknown."""
    with connection.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(sql('read_rule_schedule'), (rule_version,)).fetchall()
    return _revision_from_rows(rows)


def register_rule_schedule(connection, revision):
    """Idempotent registration in ONE automatic transaction; drift on an
    existing identity is refused and a failed append leaves no partial
    revision behind, so a retry succeeds."""
    if not isinstance(revision, RuleScheduleRevision):
        raise ValueError('Expected a validated RuleScheduleRevision')
    stored = read_rule_schedule(connection, revision.rule_version)
    if stored is not None:
        if stored != revision:
            raise ValueError(
                f'Rule schedule drift for {revision.rule_version}: the stored revision differs '
                'from the candidate; a changed schedule needs a new explicit version')
        return {'outcome': 'reuse', 'rule_version': revision.rule_version}
    with connection.transaction():
        connection.execute(sql('insert_rule_schedule_revision'), (
            revision.rule_version, revision.rule_id, revision.version, revision.scope_id,
            revision.compartment_id, revision.evidence_reference, revision.is_demo, revision.label))
        for segment in revision.segments:
            connection.execute(sql('insert_rule_schedule_segment'), (
                revision.rule_version, segment.metric, segment.measurement_context,
                segment.basis.value, segment.start, segment.end,
                segment.delta, segment.lower, segment.upper))
    return {'outcome': 'registered', 'rule_version': revision.rule_version}
