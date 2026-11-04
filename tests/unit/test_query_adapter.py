"""BR-02 service contracts against real PostgreSQL and synthetic published core.

No business mart, UI, AGC payload or alternative/fake query adapter is involved.
"""
import csv
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql as pgsql
from psycopg.types.json import Jsonb
import pytest

from greenhouse.contracts import (ASSUMPTION_LABEL, BasisStatus, IrrigationSummary,
                                  LineageResult, QueryContext, ResultStatus, RuleStatus, from_json, to_json)
from greenhouse.db.corrections import correct
from greenhouse.db.migrate import migrate
from greenhouse.db.pipeline import encode, membership, run_pipeline, save_artifact
from greenhouse.db.query_adapter import PostgresQueryAdapter, PostgresCoreQueryAdapter
from greenhouse.ingestion.parser import HEADERS
from greenhouse.queries import IrrigationService
from greenhouse.settings import Settings


@pytest.fixture(scope='module')
def contracts_database():
    kwargs=Settings.from_env().connection_kwargs()
    name='test_contracts_'+uuid4().hex[:12]
    keep=os.environ.get('GREENHOUSE_KEEP_TEST_DB')=='1'
    with psycopg.connect(**kwargs,autocommit=True) as admin:
        admin.execute(pgsql.SQL('CREATE DATABASE {}').format(pgsql.Identifier(name)))
    print('Synthetic contract test database created:',name)
    target=kwargs|{'dbname':name}
    try:
        with psycopg.connect(**target) as conn:
            migrate(conn)
        yield target
    finally:
        if keep:
            print('Synthetic contract test database retained (GREENHOUSE_KEEP_TEST_DB=1):',name)
        else:
            assert name.startswith('test_contracts_')
            with psycopg.connect(**kwargs,autocommit=True) as admin:
                admin.execute(pgsql.SQL('DROP DATABASE {} WITH (FORCE)').format(pgsql.Identifier(name)))
            print('Synthetic contract test database dropped:',name)


def fixture_stage(conn,workflow,target,*,fault=None):
    """Small prepared artifact for the production coordinator, not a query stub."""
    with conn.transaction():
        info=membership(conn,workflow)
    target.mkdir();(target/'fixture.json').write_text(encode(info))
    return save_artifact(target,info|{'rows_in':info['member_count'],'rows_out':info['member_count']})


@pytest.fixture
def published_scope(contracts_database,tmp_path):
    with psycopg.connect(**contracts_database,autocommit=True) as conn:
        def create(rows=(('10','2'),('30','12')),*,basis='verified',suspect_zero=False,
                   start=date(2020,1,1),end=date(2020,1,5),times=None):
            tag=uuid4().hex
            site,comp,cycle,snapshot,scope,policy=(name+':'+tag for name in ('site','comp','cycle','snapshot','scope','quality'))
            conn.execute("INSERT INTO core.site VALUES (%s,'Fixture','source-naive','fixture')",(site,))
            conn.execute("INSERT INTO core.compartment VALUES (%s,%s,'Fixture','fixture')",(comp,site))
            conn.execute("INSERT INTO core.team VALUES (%s,'Fixture',false,'fixture')",(site,))
            conn.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato',%s,%s,'fixture')",(cycle,comp,site,start,end))
            conn.execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://fixture','fixture')",(snapshot,tag*2))
            conn.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')",(scope,snapshot))
            conn.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,'Synthetic',%s,%s,%s,'fixture')",(snapshot,comp,site,cycle))
            definition={'nonnegative':['Irr','Drain'],'suspect_zero':['Drain'] if suspect_zero else [],'off_mode_zero':[]}
            conn.execute("INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,%s,%s,true,'synthetic quality fixture')",(policy,start,end,Jsonb(definition),hashlib.sha256(encode(definition).encode()).hexdigest()))
            if basis=='assumed_unverified':
                # Explicit test-only application; adapter never extends an assumption.
                conn.execute("INSERT INTO config.assumption_application VALUES (%s,'AGC_SHARED_BASIS_V1',1,true)",(scope,))
            if basis!='unknown':
                for code in ('Irr','Drain'):
                    assumption=('AGC_SHARED_BASIS_V1',1) if basis=='assumed_unverified' else (None,None)
                    conn.execute("INSERT INTO config.area_basis_version VALUES (%s,%s,%s,%s,1,100,'synthetic_water_area',%s,%s,%s,%s,%s,'explicit synthetic basis')",(tag+code,scope,comp,code,basis,start,end,*assumption))
                    conn.execute("INSERT INTO config.basis_application VALUES (%s,%s,%s,'shared_analysis',%s,true)",(scope,comp,code,tag+code))
            raw=tmp_path/tag/'raw';(raw/'Synthetic').mkdir(parents=True)
            path=raw/'Synthetic/Resources.csv'
            with path.open('w',newline='') as stream:
                writer=csv.writer(stream,lineterminator='\r\n');writer.writerow(HEADERS['Resources'])
                for i,(irr,drain) in enumerate(rows):
                    values={'Irr':irr,'Drain':drain}
                    serial=times[i] if times is not None else str((start-date(1899,12,30)).days+i)
                    writer.writerow([serial]+[values.get(h.strip(),'1') for h in HEADERS['Resources'][1:]])
            source={'snapshot_id':snapshot,'archive':{'sha256':tag*2},'files':[{'path':'Synthetic/Resources.csv','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'csv':{'data_rows':len(rows)}}]}
            (raw/'manifest.json').write_text(json.dumps(source))
            output=tmp_path/tag/'output'
            run_pipeline(conn,raw,output,scope,policy,stage_function=fixture_stage)
            adapter=PostgresQueryAdapter(conn,raw_snapshots={snapshot:raw})
            service=IrrigationService(adapter);pub=service.pin_publication(scope)
            context=QueryContext(scope,pub.publication_id,snapshot,cycle,(comp,),start,min(start+timedelta(days=2),end))
            return SimpleNamespace(conn=conn,scope=scope,comp=comp,cycle=cycle,snapshot=snapshot,
                                   policy=policy,raw=raw,output=output,adapter=adapter,service=service,
                                   publication=pub,context=context)
        yield create


@pytest.mark.parametrize('rows,observed,paired,ratio,cover,status',[
    ((('10','2'),('30','12')),('40','14'),('40','14'),'35','100',ResultStatus.COMPLETE), # AC-01
    ((('0','0'),('0','0')),('0','0'),('0','0'),None,'100',ResultStatus.COMPLETE), # AC-02
    ((('10','2'),('30',None)),('40','2'),('10','2'),'20','50',ResultStatus.PARTIAL), # AC-03
    (((None,None),(None,None)),(None,None),(None,None),None,'0',ResultStatus.UNKNOWN), # AC-05
])
def test_service_postgres_ac01_ac02_ac03_ac05_ac07(published_scope,rows,observed,paired,ratio,cover,status):
    e=published_scope(rows);s=e.service.get_irrigation_summary(e.context)
    dec=lambda v:None if v is None else Decimal(v)
    assert (s.irrigation_observed_l_m2,s.drain_observed_l_m2)==tuple(map(dec,observed))
    assert (s.paired_irrigation_l_m2,s.paired_drain_l_m2)==tuple(map(dec,paired))
    assert s.drain_fraction_pct==dec(ratio) and s.paired_coverage_pct==Decimal(cover)
    assert s.expected_days==2 and s.status==status and s.rule_status==RuleStatus.NOT_CONFIGURED
    assert s.rule_version is None and s.outside_rule_days is None
    assert s.publication.versions.quality_policy_version==e.policy
    assert from_json(IrrigationSummary,to_json(s))==s
    if ratio is None and observed==('0','0'):
        assert 'no_irrigation_quantity' in s.reason_codes


def test_service_postgres_ac04_and_published_assumption_label(published_scope):
    e=published_scope((('5','6'),('5','6')),basis='assumed_unverified')
    s=e.service.get_irrigation_summary(e.context)
    assert s.drain_fraction_pct==120 and s.drain_exceeds_irrigation_days==2
    assert s.basis_status==BasisStatus.ASSUMED and s.basis_label==ASSUMPTION_LABEL
    assert s.assumption_ids==('AGC_SHARED_BASIS_V1',) and s.assumption_versions==(1,)
    assert all(d.drain_fraction_pct==120 and d.drain_exceeds_irrigation for d in e.service.get_irrigation_days(e.context))


def test_service_postgres_ac09_and_pinned_basis_toggle(published_scope):
    e=published_scope(basis='unknown');s=e.service.get_irrigation_summary(e.context)
    assert s.irrigation_observed_l_m2==40 and s.drain_observed_l_m2==14
    assert s.drain_fraction_pct is None and not s.comparison_eligible
    assert 'incompatible_or_unknown_basis' in s.reason_codes
    known=published_scope(basis='assumed_unverified')
    before=known.service.get_irrigation_summary(known.context)
    known.conn.execute('UPDATE config.assumption_application SET enabled=false WHERE data_scope_id=%s',(known.scope,))
    assert known.service.get_irrigation_summary(known.context)==before
    run_pipeline(known.conn,known.raw,known.output,known.scope,known.policy,stage_function=fixture_stage)
    pub=known.service.pin_publication(known.scope)
    changed=replace(known.context,publication_id=pub.publication_id)
    after=known.service.get_irrigation_summary(changed)
    assert after.irrigation_observed_l_m2==40 and after.drain_fraction_pct is None
    assert 'incompatible_or_unknown_basis' in after.reason_codes


def test_publication_pin_mid_operation_and_verified_lineage(published_scope):
    e=published_scope();before=e.service.get_irrigation_days(e.context)
    revision=before[0].revision_id
    lineage=e.service.get_lineage(e.publication,revision_id=revision)
    assert lineage.raw_hash_verified and lineage.publication==e.publication
    assert lineage.source_time_serial==Decimal(43831) and lineage.source_timestamp.tzinfo is None
    assert from_json(LineageResult,to_json(lineage))==lineage
    assert e.service.get_lineage(e.publication,record_key=before[0].logical_record_key)==lineage
    source=e.conn.execute('SELECT source_record_id FROM core.record_revision WHERE revision_id=%s',(revision,)).fetchone()[0]
    old=dict(e.conn.execute('SELECT measure_code,value::text FROM core.measure_value WHERE revision_id=%s',(revision,)))
    c=correct(e.conn,e.scope,source,'resource',old,old|{'Irr':'20'},'fixture actor','fixture evidence')
    run_pipeline(e.conn,e.raw,e.output,e.scope,e.policy,stage_function=fixture_stage)
    new_pub=e.service.pin_publication(e.scope)
    assert new_pub.publication_id!=e.publication.publication_id
    # Same logical app task: chart, then pointer moves, then summary/export/lineage.
    assert e.service.get_irrigation_days(e.context)==before
    assert e.service.get_irrigation_summary(e.context).drain_fraction_pct==35
    new_context=replace(e.context,publication_id=new_pub.publication_id)
    assert e.service.get_irrigation_summary(new_context).drain_fraction_pct==28
    with pytest.raises(ValueError,match='absent'):e.service.get_lineage(e.publication,revision_id=c['revision_id'])
    corrected=e.service.get_lineage(new_pub,revision_id=c['revision_id'])
    assert corrected.correction_id==c['correction_id'] and corrected.correction_version==1
    assert corrected.source_record_id==lineage.source_record_id and corrected.raw_hash==lineage.raw_hash


def test_context_identity_filters_clipping_and_missing_calendar(published_scope):
    e=published_scope((('10','2'),))
    s=e.service.get_irrigation_summary(e.context)
    assert s.expected_days==2 and s.unknown_days==1 and s.paired_coverage_pct==50
    day=e.service.get_irrigation_days(e.context)[1]
    assert day.irrigation.value is None and day.drain.value is None and day.revision_id is None
    clipped=replace(e.context,start=date(2019,12,30),end=date(2020,1,7))
    s=e.service.get_irrigation_summary(clipped)
    assert s.expected_days==4 and s.window_start==date(2020,1,1) and s.window_end==date(2020,1,5)
    for ctx in (replace(e.context,scope_id='another'),replace(e.context,snapshot_id='another'),replace(e.context,compartment_ids=('another',)),replace(e.context,cycle_id='another')):
        with pytest.raises(ValueError):e.service.get_irrigation_summary(ctx)
    other=published_scope()
    with pytest.raises(ValueError):e.service.get_lineage(e.publication,revision_id=other.service.get_irrigation_days(other.context)[0].revision_id)


def test_adapter_rejects_unit_catalog_drift(published_scope):
    e=published_scope()
    # Deliberately corrupt only this disposable fixture inside a rollback. The
    # production immutable trigger normally prevents this catalog regression.
    with e.conn.transaction(force_rollback=True):
        e.conn.execute('ALTER TABLE config.measure_definition DISABLE TRIGGER immutable_measure')
        e.conn.execute("UPDATE config.measure_definition SET unit='m³' WHERE measure_code='Irr'")
        # The mart keeps its pinned source unit. The independent core diagnostic
        # still catches a corrupted live catalog, while historical mart reads stay stable.
        assert e.service.get_irrigation_summary(e.context).drain_fraction_pct==35
        core=IrrigationService(PostgresCoreQueryAdapter(e.conn))
        with pytest.raises(ValueError,match='unit'):core.get_irrigation_days(e.context)


def test_adapter_preserves_quality_and_rechecks_raw(published_scope):
    e=published_scope((('10','0'),('30','2')),suspect_zero=True)
    s=e.service.get_irrigation_summary(e.context)
    assert s.irrigation_observed_l_m2==40 and s.drain_observed_l_m2==2
    assert s.suspect_days==1 and s.paired_irrigation_l_m2==30 and s.paired_coverage_pct==50
    assert abs(s.drain_fraction_pct-Decimal('6.666666666666666666666666667'))<Decimal('1e-25')
    rev=e.service.get_irrigation_days(e.context)[0].revision_id
    (e.raw/'Synthetic/Resources.csv').write_text('tampered')
    with pytest.raises(ValueError,match='checksum'):e.service.get_lineage(e.publication,revision_id=rev)
