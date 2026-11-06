"""Whole-revision immutable envelopes: header and segments become visible together."""
import json

from greenhouse.clickhouse.repository import Repository
from greenhouse.contracts import RuleScheduleRevision, from_dict, to_dict
from greenhouse.queries.slot_rows import ScheduleRevisionOption


def read_rule_schedule(client,rule_version):
    rows=[r for r in Repository(client,None).configurations()['rows']
          if r['kind']=='rule_schedule' and r['version_id']==rule_version]
    if len(rows)>1:
        raise ValueError('Ambiguous rule schedule identity')
    if not rows:
        return None
    revision=from_dict(RuleScheduleRevision,json.loads(rows[0]['payload']))
    if revision.rule_version!=rows[0]['version_id'] or revision.scope_id!=rows[0]['scope_id']:
        raise ValueError('Rule schedule envelope identity mismatch')
    return revision


def register_rule_schedule(client,coordinator,revision):
    if not isinstance(revision,RuleScheduleRevision):
        raise ValueError('Expected a validated RuleScheduleRevision')
    # Global request identity also fences reuse of a rule version in another scope.
    created=Repository(client,coordinator).register_configuration('rule_schedule',revision.scope_id,
        revision.rule_version,'rule-schedule:'+revision.rule_version,to_dict(revision))
    return {'outcome':'registered' if created else 'reuse','rule_version':revision.rule_version}


def catalog_rules(client,scope_id,compartment_id,*,measurement_context,metrics,bases):
    bases={getattr(b,'value',b) for b in bases}; result=[]
    for row in Repository(client,None).configurations()['rows']:
        if row['kind']!='rule_schedule' or row['scope_id']!=scope_id:
            continue
        revision=read_rule_schedule(client,row['version_id'])
        if revision.compartment_id!=compartment_id or not all(s.measurement_context==measurement_context
                and s.metric in metrics and s.basis.value in bases for s in revision.segments):
            continue
        keys=tuple(sorted({(s.metric,s.measurement_context,s.basis) for s in revision.segments}))
        result.append(ScheduleRevisionOption(revision.rule_version,revision.rule_id,revision.version,
            revision.evidence_reference,revision.is_demo,revision.label,keys,len(revision.segments)))
    return tuple(sorted(result,key=lambda r:(r.rule_id,r.version,r.rule_version)))


def save_drain_profile(client,coordinator,publication,cycle,**kwargs):
    from greenhouse.clickhouse.threshold import DrainAdapter
    from greenhouse.contracts import QueryContext
    from greenhouse.queries.drain_profile import build_drain_profile
    adapter=DrainAdapter(client)
    if adapter.publication(publication.publication_id)!=publication:
        raise ValueError('Profile must use the pinned publication')
    context=QueryContext(publication.scope_id,publication.publication_id,publication.snapshot_id,
        cycle.cycle_id,(cycle.compartment_id,),cycle.start,cycle.end)
    pinned=adapter.cycle_window(context)
    if (pinned.start,pinned.end)!=(cycle.start,cycle.end):
        raise ValueError('Profile must use the pinned cycle window')
    revision=build_drain_profile(publication,cycle,**kwargs)
    register_rule_schedule(client,coordinator,revision)
    return revision
