"""Pure contract/oracle tests: no database, Spark, adapters or source CSV needed."""
import ast
from dataclasses import replace
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re

import pytest

from greenhouse.contracts import (
    ASSUMPTION_LABEL, Applicability, BasisStatus, CycleWindow, EnergyComponent,
    IrrigationDay, IrrigationSummary, MeasureValue, PublicationRef, QualityStatus,
    QueryContext, ResultStatus, RuleStatus, ScenarioChange, ScenarioCommand,
    SinkBatch, SinkRecord, SinkValue, SourceIdentity, Versions, from_json, to_json,
)
from greenhouse.queries import summarize_irrigation, result_cache_key


@pytest.fixture
def publication():
    return PublicationRef('publication-1','scope-1','snapshot-1',
                          Versions('run-1','quality-1','parser-1','code-1','config-1'),
                          datetime(2026,11,4,12,30,tzinfo=timezone(timedelta(hours=7))), 'provisional')


def context(publication, start=date(2020,1,1), end=date(2020,1,3)):
    return QueryContext(publication.scope_id,publication.publication_id,publication.snapshot_id,
                        'cycle-1',('comp-1',),start,end)


def measure(value, code, *, basis=BasisStatus.VERIFIED, quality=None):
    parsed=None if value is None else Decimal(value)
    quality=quality or (QualityStatus.MISSING if parsed is None else QualityStatus.ACCEPTED)
    return MeasureValue(parsed,'L/m²',quality,
                        Applicability.UNKNOWN if parsed is None else Applicability.APPLICABLE,
                        reason_code='missing_source_value' if parsed is None else 'fixture_suspect' if quality==QualityStatus.SUSPECT else None,
                        basis_status=basis,basis_version_id='basis:'+code if basis!=BasisStatus.UNKNOWN else None,
                        area_m2=Decimal('100') if basis!=BasisStatus.UNKNOWN else None,
                        basis_kind='synthetic_water_area' if basis!=BasisStatus.UNKNOWN else None,
                        assumption_id='AGC_SHARED_BASIS_V1' if basis==BasisStatus.ASSUMED else None,
                        assumption_version=1 if basis==BasisStatus.ASSUMED else None,
                        basis_label=ASSUMPTION_LABEL if basis==BasisStatus.ASSUMED else 'verified' if basis==BasisStatus.VERIFIED else None)


def days(publication, values, **options):
    return tuple(IrrigationDay(publication,'cycle-1','comp-1',date(2020,1,1)+timedelta(days=i),
                               measure(irr,'Irr',**options),measure(drain,'Drain',**options))
                 for i,(irr,drain) in enumerate(values))


def summary(publication, values, **options):
    return summarize_irrigation(context(publication),publication,
                                CycleWindow('cycle-1','comp-1',date(2020,1,1),date(2020,1,5)),
                                days(publication,values,**options))


@pytest.mark.parametrize('values,observed,paired,ratio,coverage,status',[
    ([('10','2'),('30','12')],('40','14'),('40','14'),'35','100',ResultStatus.COMPLETE),
    ([('0','0'),('0','0')],('0','0'),('0','0'),None,'100',ResultStatus.COMPLETE),
    ([('10','2'),('30',None)],('40','2'),('10','2'),'20','50',ResultStatus.PARTIAL),
    ([(None,None),(None,None)],(None,None),(None,None),None,'0',ResultStatus.UNKNOWN),
])
def test_br02_pure_oracle(publication,values,observed,paired,ratio,coverage,status):
    s=summary(publication,values)
    dec=lambda v:None if v is None else Decimal(v)
    assert (s.irrigation_observed_l_m2,s.drain_observed_l_m2)==tuple(map(dec,observed))
    assert (s.paired_irrigation_l_m2,s.paired_drain_l_m2)==tuple(map(dec,paired))
    assert s.drain_fraction_pct==dec(ratio) and s.paired_coverage_pct==Decimal(coverage)
    assert s.expected_days==2 and s.status==status and s.rule_status==RuleStatus.NOT_CONFIGURED
    assert s.outside_rule_days is None and s.rule_version is None


def test_missing_calendar_day_and_suspect_are_not_zero(publication):
    one=days(publication,[('10','2')])[0]
    s=summarize_irrigation(context(publication),publication,CycleWindow('cycle-1','comp-1',date(2020,1,1),date(2020,1,5)),[one])
    assert s.expected_days==2 and s.unknown_days==1 and s.irrigation_coverage_pct==50
    assert s.irrigation_observed_l_m2==10 and s.paired_irrigation_l_m2==10
    suspect=replace(one,drain=measure('2','Drain',quality=QualityStatus.SUSPECT))
    s=summarize_irrigation(context(publication),publication,CycleWindow('cycle-1','comp-1',date(2020,1,1),date(2020,1,5)),[suspect])
    assert s.suspect_days==1 and s.unknown_days==2 and s.drain_observed_l_m2 is None
    assert s.paired_coverage_pct==0 and s.drain_fraction_pct is None


def test_assumed_unknown_different_area_and_excess_drain(publication):
    s=summary(publication,[('5','6'),('5','6')],basis=BasisStatus.ASSUMED)
    assert s.drain_fraction_pct==120 and s.drain_exceeds_irrigation_days==2
    assert s.basis_status==BasisStatus.ASSUMED and s.basis_label==ASSUMPTION_LABEL
    assert s.assumption_ids==('AGC_SHARED_BASIS_V1',) and s.assumption_versions==(1,)
    unknown=summary(publication,[('10','2'),('30','12')],basis=BasisStatus.UNKNOWN)
    assert unknown.irrigation_observed_l_m2==40 and unknown.drain_observed_l_m2==14
    assert unknown.drain_fraction_pct is None and not unknown.comparison_eligible
    assert 'incompatible_or_unknown_basis' in unknown.reason_codes
    row=days(publication,[('10','2')])[0]
    different=replace(row,drain=replace(row.drain,area_m2=Decimal('96')))
    s=summarize_irrigation(context(publication),publication,CycleWindow('cycle-1','comp-1',date(2020,1,1),date(2020,1,3)),[different])
    assert s.drain_fraction_pct is None and s.paired_days==0


@pytest.mark.parametrize('bad',['duplicate','compartment','cycle','publication','window','unit'])
def test_contract_rejects_grain_and_unit_regressions(publication,bad):
    row=days(publication,[('10','2')])[0]
    rows=[row]
    with pytest.raises(ValueError):
        if bad=='duplicate': rows.append(row)
        elif bad=='compartment': rows=[replace(row,compartment_id='another')]
        elif bad=='cycle': rows=[replace(row,cycle_id='another')]
        elif bad=='publication': rows=[replace(row,publication=replace(publication,publication_id='another'))]
        elif bad=='window': rows=[replace(row,resource_date=date(2020,1,3))]
        else: rows=[replace(row,irrigation=replace(row.irrigation,unit='m³'))]
        summarize_irrigation(context(publication),publication,CycleWindow('cycle-1','comp-1',date(2020,1,1),date(2020,1,5)),rows)


def test_clipped_window_exact_sum_and_cache_identity(publication):
    ctx=context(publication,date(2019,12,30),date(2020,1,7))
    cycle=CycleWindow('cycle-1','comp-1',date(2020,1,1),date(2020,1,3))
    s=summarize_irrigation(ctx,publication,cycle,days(publication,[('123456789012345678901234567890.0010','0'),('0.0001','0')]))
    assert str(s.irrigation_observed_l_m2)=='123456789012345678901234567890.0011'
    assert s.expected_days==2 and (s.window_start,s.window_end)==(date(2020,1,1),date(2020,1,3))
    assert from_json(IrrigationSummary,to_json(s))==s
    assert result_cache_key(ctx,publication,scenario_version='v1')!=result_cache_key(ctx,publication,scenario_version='v2')
    assert result_cache_key(ctx,publication)!=result_cache_key(ctx,replace(publication,versions=replace(publication.versions,config_bundle_hash='config-v2')))


def test_serialization_exact_decimals_null_zero_and_clock_domains(publication):
    s=summary(publication,[('0.0000','2.3000'),('30',None)])
    payload=json.loads(to_json(s));day=days(publication,[('1.2300',None)])[0]
    assert json.loads(to_json(day))['data']['irrigation']['value']=='1.2300'
    assert json.loads(to_json(day))['data']['drain']['value'] is None
    assert from_json(IrrigationDay,to_json(day))==day
    assert from_json(PublicationRef,to_json(publication)).committed_at.isoformat().endswith('+07:00')
    payload['data']['irrigation_observed_l_m2']=30.0
    with pytest.raises(ValueError,match='JSON string'):from_json(IrrigationSummary,json.dumps(payload))
    payload=json.loads(to_json(day));payload['data']['drain']['quality']='unversioned-new-status'
    with pytest.raises(ValueError):from_json(IrrigationDay,json.dumps(payload))
    for version in (2,True):
        payload=json.loads(to_json(day));payload['contract_version']=version
        with pytest.raises(ValueError):from_json(IrrigationDay,json.dumps(payload))
    with pytest.raises(ValueError):replace(publication,committed_at=datetime(2026,11,4))
    with pytest.raises(ValueError):replace(day.irrigation,value=1.23)


def test_scenario_and_sink_validation_roundtrip(publication):
    command=ScenarioCommand('request-1','baseline-v1','electricity scenario',publication,
                            (ScenarioChange(EnergyComponent.ELEC_HIGH,'EUR/kWh',change_percent=Decimal('20.00')),))
    assert from_json(ScenarioCommand,to_json(command))==command
    with pytest.raises(ValueError):replace(command,baseline_version_id='')
    with pytest.raises(ValueError):replace(command,request_id=1)
    with pytest.raises(ValueError):ScenarioChange(EnergyComponent.HEAT,'EUR/kWh',price=Decimal('.0083'))
    with pytest.raises(ValueError):ScenarioChange(EnergyComponent.HEAT,'EUR/MJ',price=Decimal('1'),change_percent=Decimal('20'))
    with pytest.raises(ValueError):ScenarioChange(EnergyComponent.ELEC_LOW,'EUR/kWh',change_percent=Decimal('-100.1'))
    source=SourceIdentity('snapshot-1','source-content-id','Synthetic/Resources.csv',2,2,'a'*64)
    record=SinkRecord(source,datetime(2020,1,1,0,0,0,808000),(SinkValue('Irr',Decimal('0.0000'),'L/m²/day'),),
                      'stable-event-id',datetime(2026,11,4,tzinfo=timezone.utc),Decimal('43831.000009000'))
    batch=SinkBatch('request-1','batch-content-id','run-1','scope-1','snapshot-1','parser-1',
                    datetime(2026,11,4,tzinfo=timezone.utc),(record,))
    encoded=to_json(batch)
    assert '2020-01-01T00:00:00.808000"' in encoded and '2026-11-04T00:00:00+00:00' in encoded
    assert '43831.000009000' in encoded and '0.0000' in encoded
    assert from_json(SinkBatch,encoded)==batch
    with pytest.raises(ValueError):replace(record,observed_at=record.published_at)
    with pytest.raises(ValueError):replace(record,event_id=123)
    with pytest.raises(ValueError):replace(batch,received_at=datetime(2026,11,4))
    with pytest.raises(ValueError):replace(batch,snapshot_id='other-snapshot')


@pytest.mark.parametrize('change',[{'end':date(2020,1,1)},{'start':datetime(2020,1,1)},
                                   {'compartment_ids':('comp-1','comp-2')},{'scope_id':''}])
def test_context_rejects_invalid_windows_and_selection(publication,change):
    with pytest.raises(ValueError):replace(context(publication),**change)


def test_architecture_guards_no_database_imports_ui_sql_or_sequence_identity():
    root=Path(__file__).parents[2]
    forbidden=('psycopg','pyspark','greenhouse.db')
    for package in ('contracts','queries'):
        for path in (root/'src/greenhouse'/package).rglob('*.py'):
            tree=ast.parse(path.read_text())
            for node in ast.walk(tree):
                imports=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
                assert not any(name==f or name.startswith(f+'.') for name in imports for f in forbidden),str(path)
                assert not (isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in ('__import__','eval','exec')),str(path)
    sql_pattern=re.compile(r'\b(?:SELECT\s+[\w*,.\s]+\s+FROM|INSERT\s+INTO|DELETE\s+FROM|CREATE\s+TABLE|UPDATE\s+\w+\s+SET)\b',re.I)
    for folder in (root/'src/greenhouse/app',root/'prototypes'):
        for path in folder.rglob('*.py'):
            tree=ast.parse(path.read_text())
            assert not any(isinstance(n,ast.Constant) and isinstance(n.value,str) and sql_pattern.search(n.value) for n in ast.walk(tree)),str(path)
        for path in folder.rglob('template.html'):
            assert not sql_pattern.search(path.read_text()),str(path)
    for path in (root/'src/greenhouse/db/migrations').glob('*.sql'):
        assert not re.search(r'\b(?:bigserial|smallserial|serial)\b|GENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY',path.read_text(),re.I),str(path)
