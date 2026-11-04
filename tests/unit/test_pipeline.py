"""Durable PostgreSQL workflow, explicit corrections and local Spark contracts."""
import csv
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql as pgsql
import pytest

from greenhouse.db.corrections import correct
from greenhouse.db.lineage import lineage
from greenhouse.db.migrate import migrate
from greenhouse.db.pipeline import (run_pipeline, publish, pin_publication, pinned_values,
                                   membership, verify_artifact, encode, save_artifact)
from greenhouse.db.spark_stage import stage_candidates
from greenhouse.ingestion.parser import HEADERS
from greenhouse.ingestion.run import ingest_snapshot
from greenhouse.settings import Settings


@pytest.fixture(scope='module')
def workflow_database():
    kwargs = Settings.from_env().connection_kwargs()
    name = 'test_pipeline_' + uuid4().hex[:12]
    keep = os.environ.get('GREENHOUSE_KEEP_TEST_DB') == '1'
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        conn.execute(pgsql.SQL('CREATE DATABASE {}').format(pgsql.Identifier(name)))
    print('Synthetic pipeline test database created:', name)
    target = kwargs | {'dbname': name}
    try:
        with psycopg.connect(**target) as conn:
            migrate(conn)
        yield target
    finally:
        if keep:
            print('Synthetic pipeline test database retained (GREENHOUSE_KEEP_TEST_DB=1):', name)
        else:
            assert name.startswith('test_pipeline_')
            with psycopg.connect(**kwargs, autocommit=True) as conn:
                conn.execute(pgsql.SQL('DROP DATABASE {} WITH (FORCE)').format(pgsql.Identifier(name)))
            print('Synthetic pipeline test database dropped:', name)


@pytest.fixture
def env(workflow_database, tmp_path):
    with psycopg.connect(**workflow_database, autocommit=True) as conn:
        tag = uuid4().hex
        scope, snapshot, cycle, comp = 'scope:'+tag, 'snapshot:'+tag, 'cycle:'+tag, 'comp:'+tag
        conn.execute("INSERT INTO core.site VALUES (%s,'Fixture','source-naive','fixture')",(tag,))
        conn.execute("INSERT INTO core.compartment VALUES (%s,%s,'Fixture','fixture')",(comp,tag))
        conn.execute("INSERT INTO core.team VALUES (%s,'Fixture',false,'fixture')",(tag,))
        conn.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,'tomato','2019-12-16','2020-05-30','fixture')",(cycle,comp,tag))
        conn.execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://fixture','fixture')",(snapshot,tag*2))
        conn.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')",(scope,snapshot))
        conn.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,'Synthetic',%s,%s,%s,'fixture')",(snapshot,comp,tag,cycle))
        conn.execute("INSERT INTO config.area_basis_version VALUES (%s,%s,%s,'Irr',1,100,'synthetic_area','verified','2019-12-16','2020-05-30',NULL,NULL,'fixture')",(tag,scope,comp))
        conn.execute("INSERT INTO config.basis_application VALUES (%s,%s,'Irr','shared_analysis',%s,true)",(scope,comp,tag))
        raw = tmp_path/'raw'; (raw/'Synthetic').mkdir(parents=True)
        path=raw/'Synthetic/Resources.csv'
        with path.open('w',newline='') as stream:
            writer=csv.writer(stream,lineterminator='\r\n');writer.writerow(HEADERS['Resources'])
            # One accepted, one duplicate, two conflicting, one missing-value row.
            for day,irr in [('43831','10'),('43831','10'),('43832','2'),('43832','3'),('43833','')]:
                writer.writerow([day]+[irr if h.strip()=='Irr' else '1' for h in HEADERS['Resources'][1:]])
        source={'snapshot_id':snapshot,'archive':{'sha256':tag*2},'files':[{'path':'Synthetic/Resources.csv','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'csv':{'data_rows':5}}]}
        (raw/'manifest.json').write_text(json.dumps(source))
        yield conn,scope,raw,tmp_path/'output'


def light_stage(conn, workflow, target, *, fault=None):
    """Coordinator fault tests isolate Spark; the Spark test below uses real JVM."""
    with conn.transaction():
        counts=membership(conn,workflow)
    target.mkdir();(target/'candidate.json').write_text(encode(counts))
    return save_artifact(target,counts|{'rows_in':counts['member_count'],'rows_out':counts['member_count']})


def run(env, **options):
    conn,scope,raw,output=env
    return run_pipeline(conn,raw,output,scope,'AGC_QUALITY_V1',stage_function=light_stage,**options)


def counts(conn,scope):
    return conn.execute('SELECT count(*) FROM core.record_revision WHERE data_scope_id=%s',(scope,)).fetchone()[0]


def correction(conn,scope):
    source,revision=conn.execute('SELECT source_record_id,revision_id FROM core.record_revision WHERE data_scope_id=%s AND revision_number=1 ORDER BY observed_at LIMIT 1',(scope,)).fetchone()
    old=dict(conn.execute('SELECT measure_code,value::text FROM core.measure_value WHERE revision_id=%s',(revision,)))
    new=old|{'Irr':'11'}
    return source,old,new


def test_same_snapshot_rerun_and_explicit_correction(env):
    conn,scope,raw,output=env
    a=run(env)
    assert counts(conn,scope)==2
    pub=a['publication_id'];total=conn.execute('SELECT count(*) FROM meta.publication_member WHERE publication_id=%s',(pub,)).fetchone()[0]
    b=run(env)
    assert b['publication_id']==pub and counts(conn,scope)==2
    assert conn.execute('SELECT count(*) FROM meta.publication_member WHERE publication_id=%s',(pub,)).fetchone()[0]==total
    dispositions=dict(conn.execute('SELECT disposition,count(*) FROM meta.quarantine_record q JOIN meta.pipeline_run p USING(run_id) WHERE p.data_scope_id=%s GROUP BY disposition',(scope,)))
    assert dispositions=={'duplicate':1,'conflict':2}
    source,old,new=correction(conn,scope)
    c=correct(conn,scope,source,'resource',old,new,'fixture actor','fixture evidence',correction_id=uuid4().hex)
    again=correct(conn,scope,source,'resource',old,new,'fixture actor','fixture evidence',correction_id=c['correction_id'])
    assert again['revision_id']==c['revision_id'] and again['reused'] and counts(conn,scope)==3
    d=run(env)
    assert d['publication_id']!=pub
    assert conn.execute('SELECT revision_id FROM meta.publication_member WHERE publication_id=%s AND revision_id=%s',(d['publication_id'],c['revision_id'])).fetchone()==(c['revision_id'],)
    assert counts(conn,scope)==3
    assert run(env)['publication_id']==d['publication_id']
    assert lineage(conn,c['revision_id'],raw)['raw_hash_verified']
    assert conn.execute('SELECT count(*) FROM meta.source_record s JOIN meta.source_file f USING(source_file_id) JOIN meta.data_scope d ON d.snapshot_id=f.snapshot_id WHERE d.data_scope_id=%s',(scope,)).fetchone()==(5,)
    with pytest.raises(ValueError,match='stale'):
        correct(conn,scope,source,'resource',old,new,'actor','evidence')


@pytest.mark.parametrize('fault',['before-ingest','after-ingest','before-load-core','after-load-core','before-spark','after-spark','publish-members','publish-hook','publish-pointer'])
def test_fault_resume_membership_matches_clean_and_pointer_retained(env,fault):
    conn,scope,raw,output=env
    first=run(env);oldpub=first['publication_id']
    source,old,new=correction(conn,scope)
    correct(conn,scope,source,'resource',old,new,'actor','evidence')
    workflow=uuid4().hex
    with pytest.raises(RuntimeError,match='Injected failure'):
        run(env,workflow=workflow,fault=fault)
    assert pin_publication(conn,scope)==oldpub
    steps=dict(conn.execute('SELECT step,attempt FROM meta.workflow_step WHERE workflow_id=%s AND status=\'completed\'',(workflow,)))
    result=run(env,workflow=workflow,resume=True)
    assert result['publication_id']!=oldpub
    for step,attempt in steps.items():
        assert conn.execute('SELECT attempt FROM meta.workflow_step WHERE workflow_id=%s AND step=%s',(workflow,step)).fetchone()==(attempt,)
    final=conn.execute('SELECT membership_checksum,member_count FROM meta.publication_seal WHERE publication_id=%s',(result['publication_id'],)).fetchone()
    actual=[r[0] for r in conn.execute('SELECT revision_id FROM meta.publication_member WHERE publication_id=%s ORDER BY revision_id',(result['publication_id'],))]
    assert final==(hashlib.sha256(''.join(r+'\n' for r in actual).encode()).hexdigest(),len(actual))
    assert len(actual)==2
    clean=run(env)
    assert conn.execute('SELECT membership_checksum,member_count FROM meta.publication_seal WHERE publication_id=%s',(clean['publication_id'],)).fetchone()==final
    assert counts(conn,scope)==3


def test_hook_atomic_and_pin_one_publication(env):
    conn,scope,raw,output=env
    conn.execute('CREATE TABLE IF NOT EXISTS public.synthetic_mart(publication_id text PRIMARY KEY,total numeric)')
    def hook(c,pub,wf):
        assert c is conn
        c.execute('INSERT INTO public.synthetic_mart SELECT %s,sum(v.value) FROM meta.workflow_candidate w JOIN core.measure_value v USING(revision_id) WHERE w.workflow_id=%s AND v.measure_code=\'Irr\'',(pub,wf))
    first=run(env,hook=hook);pinned=pin_publication(conn,scope)
    before=pinned_values(conn,pinned,'Irr')
    source,old,new=correction(conn,scope);correct(conn,scope,source,'resource',old,new,'actor','evidence')
    wf=uuid4().hex
    with pytest.raises(RuntimeError):run(env,workflow=wf,hook=hook,fault='publish-hook')
    assert conn.execute('SELECT count(*) FROM public.synthetic_mart').fetchone()==(1,)
    second=run(env,workflow=wf,resume=True,hook=hook)
    assert pin_publication(conn,scope)==second['publication_id']!=pinned
    assert pinned_values(conn,pinned,'Irr')==before
    assert pinned_values(conn,second['publication_id'],'Irr')!=before
    assert conn.execute('SELECT total FROM public.synthetic_mart WHERE publication_id=%s',(second['publication_id'],)).fetchone()==(11,)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute('DELETE FROM meta.publication_member WHERE publication_id=%s',(pinned,))
    assert publish(conn,wf)['publication_id']==second['publication_id']


def test_completed_artifact_tamper_and_raw_tamper_rejected(env):
    conn,scope,raw,output=env;wf=uuid4().hex
    with pytest.raises(RuntimeError):run(env,workflow=wf,fault='before-spark')
    artifact=conn.execute('SELECT artifact FROM meta.workflow_step WHERE workflow_id=%s AND step=\'load-core\'',(wf,)).fetchone()[0]
    p=Path(artifact['folder'])/'core.json';original=p.read_text();p.write_text('changed')
    with pytest.raises(ValueError,match='checksum'):run(env,workflow=wf,resume=True)
    p.write_text(original)
    revision=conn.execute('SELECT revision_id FROM core.record_revision WHERE data_scope_id=%s LIMIT 1',(scope,)).fetchone()[0]
    (raw/'Synthetic/Resources.csv').write_text('tampered')
    with pytest.raises(ValueError,match='checksum'):lineage(conn,revision,raw)
    with pytest.raises(ValueError,match='checksum'):run(env,workflow=wf,resume=True)


def test_spark_atomic_profile_and_resume(env):
    conn,scope,raw,output=env;wf=uuid4().hex
    with pytest.raises(RuntimeError,match='spark-before-rename'):
        run_pipeline(conn,raw,output,scope,'AGC_QUALITY_V1',workflow=wf,fault='spark-before-rename')
    root=output/'pipeline'/wf
    assert not list(root.glob('spark-*')) and list(root.glob('.pending-*'))
    result=run_pipeline(conn,raw,output,scope,'AGC_QUALITY_V1',workflow=wf,resume=True)
    artifact=conn.execute('SELECT artifact FROM meta.workflow_step WHERE workflow_id=%s AND step=\'spark\'',(wf,)).fetchone()[0]
    m=verify_artifact(artifact);assert m['member_count']==2
    profile=json.loads((Path(artifact['folder'])/'profile-summary.json').read_text())
    irr=next(r for r in profile['measures'] if r['measure_code']=='Irr')
    assert irr['observed_count']==2 and irr['null_count']==irr['missing_count']==1
    assert irr['min_observed']==irr['max_observed']=='10' and irr['days_observed']==2
    assert result['status']=='completed'


def test_stale_candidate_and_basis_drift_cannot_replace_pointer(env):
    conn,scope,raw,output=env
    first=run(env);stale=uuid4().hex
    with pytest.raises(RuntimeError):run(env,workflow=stale,fault='before-publish')
    source,old,new=correction(conn,scope);correct(conn,scope,source,'resource',old,new,'actor','evidence')
    current=run(env)
    with pytest.raises(ValueError,match='stale'):run(env,workflow=stale,resume=True)
    assert pin_publication(conn,scope)==current['publication_id']
    drift=uuid4().hex
    with pytest.raises(RuntimeError):run(env,workflow=drift,fault='before-publish')
    conn.execute('UPDATE config.basis_application SET enabled=false WHERE data_scope_id=%s',(scope,))
    with pytest.raises(ValueError,match='configuration differs'):run(env,workflow=drift,resume=True)
    assert pin_publication(conn,scope)==current['publication_id']
    nextpub=run(env)
    assert nextpub['publication_id']!=current['publication_id']
    assert conn.execute('SELECT count(*) FROM meta.publication_basis WHERE publication_id=%s',(nextpub['publication_id'],)).fetchone()==(0,)


def test_new_snapshot_requires_explicit_scope_and_no_basis_extension(env):
    conn,scope,raw,output=env
    run(env)
    manifest=json.loads((raw/'manifest.json').read_text())
    manifest['snapshot_id']='other-'+uuid4().hex
    manifest['archive']['sha256']='b'*64
    (raw/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):run(env)
    assert conn.execute('SELECT count(*) FROM config.basis_application WHERE data_scope_id=%s',(scope,)).fetchone()==(1,)


def test_post_commit_log_failure_keeps_completed_publication(env,monkeypatch):
    import greenhouse.db.pipeline as pipeline
    conn,scope,raw,output=env;wf=uuid4().hex
    original=pipeline.log_event
    def broken_log(folder,**event):
        if event.get('step')=='publish' and event.get('status')=='completed':
            raise OSError('synthetic output failure after commit')
        return original(folder,**event)
    monkeypatch.setattr(pipeline,'log_event',broken_log)
    with pytest.raises(OSError):run(env,workflow=wf)
    pub=pin_publication(conn,scope)
    assert conn.execute('SELECT status FROM meta.workflow_run WHERE workflow_id=%s',(wf,)).fetchone()==('completed',)
    assert conn.execute('SELECT status FROM meta.workflow_step WHERE workflow_id=%s AND step=\'publish\'',(wf,)).fetchone()==('completed',)
    monkeypatch.undo()
    assert run(env,workflow=wf,resume=True)['publication_id']==pub
    assert conn.execute('SELECT attempt FROM meta.workflow_step WHERE workflow_id=%s AND step=\'publish\'',(wf,)).fetchone()==(1,)
