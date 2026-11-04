"""Water mart parity, query-time profiles and atomic publication contracts."""
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
import json
from random import Random
from uuid import uuid4

import psycopg
import pytest

from greenhouse.contracts import IrrigationRuleProfile, IrrigationSummary, RuleStatus, from_json, to_json
from greenhouse.db.corrections import correct
from greenhouse.db.irrigation_rules import register_irrigation_rule
from greenhouse.db.pipeline import run_pipeline
from greenhouse.db.query_adapter import PostgresCoreQueryAdapter, PostgresQueryAdapter
from greenhouse.queries import IrrigationService, result_cache_key
from greenhouse.queries.irrigation import exact_sum, percent
from test_query_adapter import contracts_database, published_scope, fixture_stage


def core_service(e):
    return IrrigationService(PostgresCoreQueryAdapter(e.conn,raw_snapshots={e.snapshot:e.raw}))


def profile(e,*,version=1,maximum='40'):
    tag=uuid4().hex
    return IrrigationRuleProfile('rule-version:'+tag,'rule:'+tag,version,e.scope,e.comp,
                                 Decimal('20'),Decimal(maximum),Decimal('0'),Decimal('100'),
                                 'synthetic review thresholds; not agronomic guidance',True,'demo_rule')


@pytest.mark.parametrize('rows,basis',[
    ((('10','2'),('30','12')),'verified'),
    ((('0','0'),('0','0')),'verified'),
    ((('10','2'),('30',None)),'verified'),
    ((('5','6'),('0','1')),'assumed_unverified'),
    (((None,None),(None,None)),'verified'),
    ((('10','2'),('30','12')),'unknown'),
    ((('10','2'),),'verified'),
])
def test_stored_fact_and_summary_match_independent_core_oracle(published_scope,rows,basis):
    e=published_scope(rows,basis=basis);core=core_service(e)
    assert e.service.get_irrigation_days(e.context)==core.get_irrigation_days(e.context)
    assert e.service.get_irrigation_summary(e.context)==core.get_irrigation_summary(e.context)
    expected={d.resource_date:d for d in core.get_irrigation_days(e.context) if d.revision_id}
    stored=e.conn.execute('SELECT resource_date,irrigation_accepted,drain_accepted,paired,drain_fraction_pct,drain_exceeds_irrigation,reason_codes FROM mart.fact_irrigation_day WHERE publication_id=%s ORDER BY resource_date',(e.publication.publication_id,)).fetchall()
    assert len(stored)==len(expected)
    for when,irr,drain,paired,ratio,exceeds,reasons in stored:
        d=expected[when]
        assert (irr,drain,paired,ratio,exceeds,tuple(reasons))==(d.irrigation.accepted,d.drain.accepted,d.paired,d.drain_fraction_pct,d.drain_exceeds_irrigation,d.reason_codes)


def test_database_decimal_percent_matches_fifty_digit_half_even_oracle(published_scope):
    e=published_scope()
    cases=[('1','3'),('2','7'),('0','5'),('1e-75','3'),('1','7e-50'),
           ('1.234567890123456789012345678901234567890123456789045','1'),
           ('1.234567890123456789012345678901234567890123456789055','1'),
           ('999999999999999999999999999999999999999999999999999999999','1.0020')]
    random=Random(29)
    for _ in range(60):
        cases.append((str(random.randrange(1,10**65))+'e'+str(random.randrange(-80,10)),
                      str(random.randrange(1,10**60))+'e'+str(random.randrange(-80,10))))
    for n,d in cases:
        expected=percent(Decimal(n),Decimal(d))
        assert e.conn.execute('SELECT mart.water_percent(%s,%s)',(Decimal(n),Decimal(d))).fetchone()[0]==expected,(n,d)


def test_weekly_ratio_of_sums_iso_year_and_cycle_boundaries(published_scope):
    values=(('10','2'),('30','12'))*4+(('10','2'),)
    e=published_scope(values,start=date(2019,12,30),end=date(2020,1,8))
    context=replace(e.context,start=date(2019,12,29),end=date(2020,1,9))
    weeks=e.service.get_irrigation_weekly(context)
    assert [(s.iso_year,s.iso_week,s.expected_days) for s in weeks]==[(2020,1,7),(2020,2,2)]
    assert [(s.window_start,s.window_end) for s in weeks]==[(date(2019,12,30),date(2020,1,6)),(date(2020,1,6),date(2020,1,8))]
    cycle=e.service.get_irrigation_cycle(e.context)
    for name in ('irrigation_observed_l_m2','drain_observed_l_m2','paired_irrigation_l_m2','paired_drain_l_m2'):
        assert exact_sum(getattr(s,name) for s in weeks)==getattr(cycle,name)
    assert weeks[0].drain_fraction_pct==percent(Decimal(44),Decimal(130))
    daily=e.service.get_irrigation_days(context)[:7]
    assert weeks[0].drain_fraction_pct!=exact_sum(d.drain_fraction_pct for d in daily)/7
    short=e.service.get_irrigation_weekly(replace(context,start=date(2020,1,1),end=date(2020,1,7)))
    assert [s.expected_days for s in short]==[5,1]
    assert from_json(IrrigationSummary,to_json(weeks[0]))==weeks[0]


def test_ac06_explicit_version_boundary_unknown_and_ac07(published_scope):
    e=published_scope((('10','2'),('10','5')))
    assert e.service.get_irrigation_summary(e.context).rule_status==RuleStatus.NOT_CONFIGURED
    first=register_irrigation_rule(e.conn,profile(e))
    context=replace(e.context,rule_version=first.rule_version)
    days=e.service.get_irrigation_days(context)
    assert [d.rule_status for d in days]==[RuleStatus.WITHIN_RANGE,RuleStatus.OUTSIDE_RANGE]
    summary=e.service.get_irrigation_summary(context)
    assert summary.rule_status==RuleStatus.CONFIGURED and summary.outside_rule_days==1 and summary.rule_unknown_days==0
    assert summary.rule_version==first.rule_version and summary.rule_profile.label=='demo_rule'
    assert summary.rule_profile.evidence_reference==first.evidence_reference
    second=replace(first,rule_version='rule-version:'+uuid4().hex,version=2,max_drain_pct=Decimal('50'))
    register_irrigation_rule(e.conn,second)
    changed=replace(context,rule_version=second.rule_version)
    assert e.service.get_irrigation_summary(changed).outside_rule_days==0
    assert e.service.get_irrigation_summary(context).outside_rule_days==1
    assert result_cache_key(context,e.publication)!=result_cache_key(changed,e.publication)
    assert from_json(IrrigationSummary,to_json(summary))==summary
    with pytest.raises(psycopg.errors.CheckViolation):
        e.conn.execute('UPDATE config.irrigation_rule_profile SET max_drain_pct=50 WHERE rule_version=%s',(first.rule_version,))
    missing=published_scope((('10',None),('0','0')))
    unknown_rule=register_irrigation_rule(missing.conn,profile(missing))
    unknown=missing.service.get_irrigation_summary(replace(missing.context,rule_version=unknown_rule.rule_version))
    assert unknown.rule_unknown_days==2 and unknown.outside_rule_days==0
    with pytest.raises(ValueError,match='scope/compartment'):
        e.service.get_irrigation_summary(replace(e.context,rule_version=unknown_rule.rule_version))
    with pytest.raises(ValueError,match='Unknown irrigation rule'):
        e.service.get_irrigation_summary(replace(e.context,rule_version='does-not-exist'))


@pytest.mark.parametrize('changes',[{'min_drain_pct':Decimal('41')},{'max_drain_pct':Decimal('Infinity')},
                                    {'unit':'m³'},{'ratio_unit':'fraction'},{'label':'approved agronomy'},
                                    {'evidence_reference':''}])
def test_rule_contract_and_database_validation(published_scope,changes):
    e=published_scope();p=profile(e)
    with pytest.raises(ValueError):replace(p,**changes)
    register_irrigation_rule(e.conn,p)
    with pytest.raises(psycopg.errors.CheckViolation):
        e.conn.execute("INSERT INTO config.irrigation_rule_profile SELECT %s,rule_id,2,data_scope_id,compartment_id,'NaN'::numeric,max_drain_pct,min_irrigation_l_m2,max_irrigation_l_m2,unit,ratio_unit,evidence_reference,is_demo,label FROM config.irrigation_rule_profile WHERE rule_version=%s",('bad:'+uuid4().hex,p.rule_version))


def test_ac08_duplicate_conflict_and_rerun_do_not_multiply_mart(published_scope):
    e=published_scope((('10','2'),('10','2'),('30','12'),('31','12'),('20','4')),
                       times=('43831','43831','43832','43832','43833'))
    context=replace(e.context,end=date(2020,1,4))
    summary=e.service.get_irrigation_summary(context)
    assert (summary.irrigation_observed_l_m2,summary.drain_observed_l_m2,summary.paired_days)==(30,6,2)
    assert e.service.get_irrigation_days(context)[1].revision_id is None
    assert dict(e.conn.execute('SELECT disposition,count(*) FROM meta.quarantine_record q JOIN meta.pipeline_run r USING(run_id) WHERE r.data_scope_id=%s GROUP BY disposition',(e.scope,)))=={'duplicate':1,'conflict':2}
    before=e.conn.execute('SELECT count(*) FROM mart.fact_irrigation_day WHERE publication_id=%s',(e.publication.publication_id,)).fetchone()
    result=run_pipeline(e.conn,e.raw,e.output,e.scope,e.policy,stage_function=fixture_stage)
    assert result['publication_id']==e.publication.publication_id
    assert e.conn.execute('SELECT count(*) FROM mart.fact_irrigation_day WHERE publication_id=%s',(e.publication.publication_id,)).fetchone()==before==(2,)
    assert e.conn.execute('SELECT count(*) FROM meta.publication WHERE data_scope_id=%s',(e.scope,)).fetchone()==(1,)


@pytest.mark.parametrize('failure',['exception','wrong_revision'])
def test_hook_rollback_seal_and_old_publication(published_scope,failure):
    e=published_scope();old=e.service.get_irrigation_summary(e.context)
    revision=e.service.get_irrigation_days(e.context)[0].revision_id
    source=e.conn.execute('SELECT source_record_id FROM core.record_revision WHERE revision_id=%s',(revision,)).fetchone()[0]
    values=dict(e.conn.execute('SELECT measure_code,value::text FROM core.measure_value WHERE revision_id=%s',(revision,)))
    correct(e.conn,e.scope,source,'resource',values,values|{'Irr':'20'},'fixture actor','fixture evidence')
    seen=[]
    def broken(conn,pub,wf):
        assert conn is e.conn
        assert conn.execute('SELECT count(*) FROM mart.fact_irrigation_day WHERE publication_id=%s',(pub,)).fetchone()==(2,)
        seen.append(pub)
        if failure=='wrong_revision':
            conn.execute('UPDATE mart.fact_irrigation_day SET revision_id=%s WHERE publication_id=%s AND resource_date=%s',
                         (revision,pub,date(2020,1,1)))
        else:
            raise RuntimeError('synthetic mart hook failure')
    wf=uuid4().hex
    error=RuntimeError if failure=='exception' else psycopg.errors.ForeignKeyViolation
    with pytest.raises(error):
        run_pipeline(e.conn,e.raw,e.output,e.scope,e.policy,workflow=wf,hook=broken,stage_function=fixture_stage)
    assert e.service.pin_publication(e.scope)==e.publication
    for table in ('fact_irrigation_day','dim_irrigation_cycle','irrigation_publication'):
        assert e.conn.execute('SELECT count(*) FROM mart.'+table+' WHERE publication_id=%s',(seen[0],)).fetchone()==(0,)
    run_pipeline(e.conn,e.raw,e.output,e.scope,e.policy,workflow=wf,resume=True,stage_function=fixture_stage)
    assert e.service.get_irrigation_summary(e.context)==old
    new=e.service.pin_publication(e.scope)
    assert e.service.get_irrigation_summary(replace(e.context,publication_id=new.publication_id)).drain_fraction_pct==28
    for statement in ('UPDATE mart.fact_irrigation_day SET irrigation_l_m2=999 WHERE publication_id=%s',
                      'DELETE FROM mart.fact_irrigation_day WHERE publication_id=%s',
                      'UPDATE mart.dim_irrigation_cycle SET compartment_name=\'changed\' WHERE publication_id=%s',
                      'UPDATE mart.irrigation_publication SET day_count=0 WHERE publication_id=%s'):
        with pytest.raises(psycopg.errors.CheckViolation):e.conn.execute(statement,(e.publication.publication_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        e.conn.execute('INSERT INTO mart.fact_irrigation_day SELECT * FROM mart.fact_irrigation_day WHERE publication_id=%s LIMIT 1',(e.publication.publication_id,))


def test_missing_legacy_mart_fails_without_core_fallback(published_scope):
    e=published_scope();pub='legacy:'+uuid4().hex;wf=uuid4().hex
    e.conn.execute("INSERT INTO meta.workflow_run(workflow_id,data_scope_id,snapshot_id,input_spec,versions,status) VALUES (%s,%s,%s,'{}','{\"code\":\"pipeline-v1\"}','completed')",(wf,e.scope,e.snapshot))
    e.conn.execute("INSERT INTO meta.publication SELECT %s,data_scope_id,run_id,quality_policy_version,'legacy-config',observed_through,committed_at,finality_status FROM meta.publication WHERE publication_id=%s",(pub,e.publication.publication_id))
    e.conn.execute('INSERT INTO meta.publication_member SELECT %s,data_scope_id,record_type,logical_record_key,revision_id FROM meta.publication_member WHERE publication_id=%s',(pub,e.publication.publication_id))
    e.conn.execute('INSERT INTO meta.publication_basis SELECT %s,data_scope_id,compartment_id,measure_code,analysis_context,basis_version_id FROM meta.publication_basis WHERE publication_id=%s',(pub,e.publication.publication_id))
    e.conn.execute('INSERT INTO meta.publication_seal SELECT %s,%s,membership_checksum,member_count,artifact_checksum,sealed_at FROM meta.publication_seal WHERE publication_id=%s',(pub,wf,e.publication.publication_id))
    context=replace(e.context,publication_id=pub)
    with pytest.raises(ValueError,match='lacks the irrigation mart'):e.service.get_irrigation_summary(context)
    assert core_service(e).get_irrigation_summary(context).drain_fraction_pct==35
