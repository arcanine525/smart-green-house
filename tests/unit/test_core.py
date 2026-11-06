"""Real PostgreSQL contracts with entirely synthetic source/staging data."""

from datetime import datetime
from decimal import Decimal
import csv
import gzip
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

import psycopg
from psycopg import sql as pgsql
import pytest

from greenhouse.db.loader import AGC_SCOPE, explain, load_core
from greenhouse.db.migrate import migrate
from greenhouse.ingestion.parser import HEADERS
from greenhouse.ingestion.run import ingest_snapshot
from greenhouse.settings import Settings


@pytest.fixture(scope="module")
def core_database():
    kwargs = Settings.from_env().connection_kwargs()
    name = "test_core_" + uuid4().hex
    with psycopg.connect(**kwargs, autocommit=True) as admin:
        admin.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
    print("Synthetic test database created:", name)
    target = kwargs | {"dbname": name}
    with psycopg.connect(**target) as connection:
        # The additive energy migration preserves checksums of 001..010.
        assert len(migrate(connection)["applied"]) == 16
    try:
        yield target
    finally:
        assert name.startswith("test_core_")
        with psycopg.connect(**kwargs, autocommit=True) as admin:
            admin.execute(pgsql.SQL("DROP DATABASE {} WITH (FORCE)").format(pgsql.Identifier(name)))
        print("Synthetic test database dropped:", name)


@pytest.fixture
def db(core_database):
    with psycopg.connect(**core_database) as connection:
        with connection.transaction(force_rollback=True):
            yield connection


def synthetic_scope(db):
    db.execute("INSERT INTO core.site VALUES ('synthetic','Synthetic','source-naive','synthetic fixture')")
    db.execute("INSERT INTO core.compartment VALUES ('Synthetic','synthetic','Synthetic','synthetic fixture')")
    db.execute("INSERT INTO core.team VALUES ('Synthetic','Synthetic',false,'synthetic fixture')")
    db.execute("INSERT INTO core.growing_cycle VALUES ('synthetic-cycle','Synthetic','Synthetic','tomato','2019-12-16','2020-05-30','synthetic analytical window')")
    db.execute("INSERT INTO meta.dataset_snapshot VALUES ('synthetic-core',%s,'synthetic://fixture','synthetic fixture')", ('a'*64,))
    db.execute("INSERT INTO meta.data_scope VALUES ('synthetic:scope','synthetic-core','historical',NULL,'synthetic fixture')")
    db.execute("INSERT INTO meta.source_folder_mapping VALUES ('synthetic-core','Synthetic','Synthetic','Synthetic','synthetic-cycle','synthetic fixture')")
    db.execute("INSERT INTO config.quality_policy_version SELECT 'SYNTHETIC_QUALITY_V1',version,valid_from,valid_to_exclusive,definition,definition_hash,true,'synthetic baseline oracle' FROM config.quality_policy_version WHERE policy_version_id='AGC_QUALITY_V1'")
    for measure in ('Irr','Drain','Heat_cons','ElecHigh','ElecLow','ProdA','ProdB'):
        area = 50 if measure.startswith('Prod') else 100
        db.execute("INSERT INTO config.area_basis_version VALUES (%s,'synthetic:scope','Synthetic',%s,1,%s,'synthetic_area','verified','2019-12-16','2020-05-30',NULL,NULL,'synthetic fixture area; not AGC metadata')", ('synthetic:'+measure,measure,area))
        db.execute("INSERT INTO config.basis_application VALUES ('synthetic:scope','Synthetic',%s,'shared_analysis',%s,true)", (measure,'synthetic:'+measure))


def stage(tmp_path, rows=None, run_id="synthetic-run"):
    root = tmp_path / "raw"
    (root/'Synthetic').mkdir(parents=True,exist_ok=True)
    files=[]
    rows = rows or {'Resources':[{'time':'43831','Irr':'10','Drain':'2'}]}
    for kind, examples in rows.items():
        path=root/'Synthetic'/(kind+'.csv')
        with path.open('w',encoding='utf-8',newline='') as handle:
            writer=csv.writer(handle,lineterminator='\r\n')
            writer.writerow(HEADERS[kind])
            for example in examples:
                writer.writerow([example.get('time','43831')]+[example.get(h.strip(),'1') for h in HEADERS[kind][1:]])
        files.append({'path':path.relative_to(root).as_posix(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'csv':{'data_rows':len(examples)}})
    manifest={'snapshot_id':'synthetic-core','archive':{'sha256':'a'*64},'files':files}
    (root/'manifest.json').write_text(json.dumps(manifest))
    folder,_=ingest_snapshot(root,manifest,tmp_path/'output',run_id)
    return folder,root


def load(db, folder, root):
    return load_core(db,folder,root,'synthetic:scope','SYNTHETIC_QUALITY_V1')


def test_migrations_skip_and_detect_changed_checksum(db,tmp_path):
    assert migrate(db)['applied']==[]
    root=Path(__file__).parents[2]/'src/greenhouse/db/migrations'
    if not root.exists(): root=Path('/usr/local/lib/python3.12/site-packages/greenhouse/db/migrations')
    shutil.copytree(root,tmp_path/'migrations')
    first=sorted((tmp_path/'migrations').glob('*.sql'))[0]
    first.write_text(first.read_text()+'\n-- synthetic checksum change\n')
    with pytest.raises(ValueError,match='checksum changed'):
        migrate(db,tmp_path/'migrations')


def test_agc_reference_basis_provenance_toggle_and_sensitivity(db):
    assert db.execute('SELECT count(*) FROM core.compartment').fetchone()==(6,)
    assert db.execute("SELECT count(*) FROM config.area_basis_version WHERE verification_status='verified'").fetchone()==(12,)
    assert db.execute("SELECT count(*) FROM config.selected_basis WHERE analysis_context='shared_analysis' AND applied").fetchone()==(42,)
    assert db.execute("SELECT count(*) FROM config.selected_basis WHERE analysis_context='sensitivity' AND applied").fetchone()==(0,)
    assert Decimal(96)/Decimal('62.5')==Decimal('1.536')
    db.execute("UPDATE config.assumption_application SET enabled=false WHERE data_scope_id=%s",(AGC_SCOPE,))
    assert db.execute("SELECT count(*) FROM config.selected_basis WHERE analysis_context='shared_analysis' AND applied").fetchone()==(0,)
    assert db.execute("SELECT count(*) FROM config.selected_basis WHERE analysis_context='source_verified' AND applied").fetchone()==(12,)
    with pytest.raises(psycopg.errors.CheckViolation),db.transaction():
        db.execute("UPDATE config.area_basis_version SET area_m2=96 WHERE verification_status='verified'")


def test_measure_quality_units_off_mode_and_no_fallback(db,tmp_path):
    synthetic_scope(db)
    folder,root=stage(tmp_path,{'GreenhouseClimate':[{'time':'43831','co2_sp':'0','co2_vip':'700','t_heat_sp':'','t_heat_vip':'20','EC_drain_PC':'-1','pH_drain_PC':'0'}],
                                'Resources':[{'time':'43831','Irr':'-1','Drain':'2'}],
                                'Production':[{'time':'43831','ProdA':'3','ProdB':''}]})
    result=load(db,folder,root)
    assert sum(r['records'] for r in result['record_counts'])==4
    off=explain(db,'synthetic:scope','Synthetic','2020-01-01','co2_sp')['values'][0]
    assert off['value']==0 and off['quality_status']=='accepted' and off['applicability_status']=='unknown'
    assert off['reason_code']=='off_mode_unverified' and off['usable_value'] is None and off['unit']=='ppm'
    requested=explain(db,'synthetic:scope','Synthetic','2020-01-01','t_heat_sp')['values'][0]
    assert requested['value'] is None and requested['quality_status']=='missing'
    for code,status,unit in [('pH_drain_PC','suspect','1'),('EC_drain_PC','invalid','dS/m'),('Irr','invalid','L/m²/day'),('ProdB','missing','kg/m²/harvest')]:
        row=explain(db,'synthetic:scope','Synthetic','2020-01-01',code)['values'][0]
        assert row['quality_status']==status and row['unit']==unit and row['usable_value'] is None
        assert row['raw_reference'].startswith('Synthetic/') and row['policy_version_id']=='SYNTHETIC_QUALITY_V1'
    assert db.execute('SELECT count(*) FROM core.published_measure').fetchone()==(0,)


def test_serial_precision_iso_year_and_closing_boundary(db,tmp_path):
    synthetic_scope(db)
    folder,root=stage(tmp_path,{'GreenhouseClimate':[{'time':'43829.003470000009'},{'time':'43980.999999'}]})
    load(db,folder,root)
    row=explain(db,'synthetic:scope','Synthetic','2019-12-30','Tair')['values'][0]
    assert (row['iso_year'],row['iso_week'])==(2020,1)
    assert row['source_time_serial']==Decimal('43829.003470000009') and row['raw_time_token']=='43829.003470000009'
    assert row['source_timestamp'].tzinfo is None and row['observed_at']==datetime(2019,12,30,0,5)
    assert row['source_timestamp']==datetime(2019,12,30,0,4,59,808000)  # Preserve T06 truncation; do not round to 808001.
    boundary=explain(db,'synthetic:scope','Synthetic','2020-05-30','Tair')['values'][0]
    assert boundary['is_close_boundary'] and not boundary['in_cycle'] and boundary['usable_value'] is None
    assert boundary['source_time_serial']==Decimal('43980.999999')


def test_basis_null_path_append_version_and_synthetic_area(db,tmp_path):
    synthetic_scope(db);folder,root=stage(tmp_path);load(db,folder,root)
    def value(): return explain(db,'synthetic:scope','Synthetic','2020-01-01','Irr')['values'][0]
    assert value()['basis_status']=='verified' and value()['area_m2']==100 and value()['basis_assumption_id'] is None
    db.execute("UPDATE config.basis_application SET enabled=false WHERE data_scope_id='synthetic:scope' AND measure_code='Irr'")
    assert value()['basis_version_id'] is None and value()['basis_reason']=='incompatible_or_unknown_basis'
    assert value()['value']==10
    db.execute("INSERT INTO config.area_basis_version VALUES ('synthetic:Irr:v2','synthetic:scope','Synthetic','Irr',2,120,'synthetic_area','verified','2019-12-16','2020-05-30',NULL,NULL,'new synthetic evidence')")
    db.execute("UPDATE config.basis_application SET enabled=true,basis_version_id='synthetic:Irr:v2' WHERE data_scope_id='synthetic:scope' AND measure_code='Irr'")
    assert value()['area_m2']==120
    assert db.execute("SELECT area_m2 FROM config.area_basis_version WHERE basis_version_id='synthetic:Irr'").fetchone()==(100,)


def test_quarantine_and_lab_defer_keep_completeness_evidence(db,tmp_path):
    synthetic_scope(db)
    folder,root=stage(tmp_path,{'Production':[{'time':'43000','ProdA':'1'}],'LabAnalysis':[{'time':'43831'}]})
    result=load(db,folder,root)
    assert result['quarantine_records']==1 and result['record_counts']==[]
    assert db.execute('SELECT source_rows_resolved FROM meta.harvest_ingestion_state').fetchone()==(False,)
    assert db.execute("SELECT core_status FROM meta.file_ingestion JOIN meta.source_file USING(source_file_id) WHERE relative_path LIKE '%%LabAnalysis.csv'").fetchone()==('deferred',)
    assert db.execute('SELECT source_time_serial FROM meta.source_record').fetchone()==(43000,)
    assert explain(db,'synthetic:scope','Synthetic','2020-01-01','ProdA')['cycle_quarantine'][0]['reason_code']=='out_of_cycle_unresolved'
    with pytest.raises(psycopg.errors.CheckViolation),db.transaction():
        db.execute("INSERT INTO meta.correction VALUES ('bad',(SELECT source_record_id FROM meta.source_record),1,'{}','{}','','',now())")
    db.execute("INSERT INTO meta.correction VALUES ('correction-v1',(SELECT source_record_id FROM meta.source_record),1,'{}','{}','synthetic evidence','fixture actor',now())")
    with pytest.raises(psycopg.errors.CheckViolation),db.transaction():
        db.execute("UPDATE meta.correction SET new_value='{}'")
    assert db.execute('SELECT source_time_serial FROM meta.source_record').fetchone()==(43000,)


@pytest.mark.parametrize('flag',['failed','outputs_consumable','run_failed'])
def test_manifest_failed_files_and_runs_never_open_outputs(db,tmp_path,flag):
    synthetic_scope(db);folder,root=stage(tmp_path)
    p=folder/'manifest.json';m=json.loads(p.read_text())
    if flag=='failed': m['files'][0]['status']='failed'
    elif flag=='outputs_consumable': m['files'][0]['outputs_consumable']=False
    else: m['status']='failed'
    m['files'][0]['outputs']={'accepted':'missing-do-not-open'}
    p.write_text(json.dumps(m))
    result=load(db,folder,root)
    assert not result['record_counts']
    assert db.execute('SELECT count(*) FROM meta.source_record').fetchone()==(0,)
    assert result['files'][0]['core_status'] in ('failed','blocked')


@pytest.mark.parametrize('field',['values','raw_hash','source_timestamp'])
def test_staging_tamper_rolls_back_metadata_and_core(db,tmp_path,field):
    synthetic_scope(db);folder,root=stage(tmp_path)
    path=folder/'accepted/Synthetic/Resources.csv.jsonl.gz'
    with gzip.open(path,'rt') as h: row=json.loads(h.readline())
    if field=='values': row['values']['Irr']='999'
    elif field=='raw_hash': row[field]='0'*64
    else: row[field]='2020-01-02T00:00:00'
    with gzip.open(path,'wt') as h: h.write(json.dumps(row)+'\n')
    with pytest.raises(ValueError,match='Staging/raw mismatch'):
        load(db,folder,root)
    assert db.execute('SELECT count(*) FROM meta.pipeline_run').fetchone()==(0,)
    assert db.execute('SELECT count(*) FROM meta.source_file').fetchone()==(0,)


def test_repeat_run_rejected_and_cross_run_constraints_prevent_duplicates(db,tmp_path):
    synthetic_scope(db);folder,root=stage(tmp_path);load(db,folder,root)
    with pytest.raises(ValueError,match='already loaded'): load(db,folder,root)
    second,_=stage(tmp_path,run_id='second-run')
    with pytest.raises(psycopg.errors.UniqueViolation): load(db,second,root)
    assert db.execute('SELECT count(*) FROM core.resource_day').fetchone()==(1,)
    assert db.execute('SELECT count(*) FROM meta.pipeline_run').fetchone()==(1,)


def test_grain_type_finite_quality_effective_and_scope_constraints(db,tmp_path):
    synthetic_scope(db);folder,root=stage(tmp_path,{'GreenhouseClimate':[{'time':'43831'}]});load(db,folder,root)
    revision=db.execute("SELECT revision_id FROM core.record_revision WHERE record_type='climate'").fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation),db.transaction():
        db.execute("INSERT INTO core.resource_day(revision_id,observed_at) VALUES (%s,'2020-01-01')",(revision,))
    with pytest.raises(psycopg.errors.CheckViolation),db.transaction():
        db.execute("UPDATE core.climate_observation SET tair='NaN' WHERE revision_id=%s",(revision,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation),db.transaction():
        db.execute("INSERT INTO core.measure_quality VALUES (%s,'climate','Irr','SYNTHETIC_QUALITY_V1','accepted','applicable',NULL)",(revision,))
    with pytest.raises(psycopg.errors.UniqueViolation),db.transaction():
        db.execute("INSERT INTO core.record_revision SELECT 'different-id',data_scope_id,snapshot_id,cycle_id,run_id,source_record_id,record_type,logical_record_key,revision_number,content_hash,observed_at,is_close_boundary,correction_id FROM core.record_revision WHERE revision_id=%s",(revision,))
    db.execute("INSERT INTO config.quality_policy_version SELECT 'expired',2,'2020-02-01','2020-03-01',definition,definition_hash,is_demo,'synthetic expired policy' FROM config.quality_policy_version WHERE policy_version_id='SYNTHETIC_QUALITY_V1'")
    with pytest.raises(psycopg.errors.CheckViolation),db.transaction():
        db.execute("INSERT INTO core.measure_quality VALUES (%s,'climate','Tair','expired','accepted','applicable',NULL)",(revision,))


def test_publication_membership_pointer_and_pinned_basis_schema_only(db,tmp_path):
    synthetic_scope(db);folder,root=stage(tmp_path);load(db,folder,root)
    revision,key=db.execute("SELECT revision_id,logical_record_key FROM core.record_revision").fetchone()
    db.execute("INSERT INTO meta.publication VALUES ('synthetic-pub','synthetic:scope','synthetic-run','SYNTHETIC_QUALITY_V1','synthetic-config',NULL,now(),'complete')")
    with pytest.raises(psycopg.errors.ForeignKeyViolation),db.transaction():
        db.execute("INSERT INTO meta.current_publication VALUES (%s,'synthetic-pub')",(AGC_SCOPE,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation),db.transaction():
        db.execute("INSERT INTO meta.publication_member VALUES ('synthetic-pub','synthetic:scope','resource','wrong-key',%s)",(revision,))
    db.execute("INSERT INTO meta.publication_member VALUES ('synthetic-pub','synthetic:scope','resource',%s,%s)",(key,revision))
    db.execute("INSERT INTO meta.publication_basis VALUES ('synthetic-pub','synthetic:scope','Synthetic','Irr','shared_analysis','synthetic:Irr')")
    db.execute("INSERT INTO meta.publication_seal(publication_id,membership_checksum,member_count,artifact_checksum) VALUES ('synthetic-pub','fixture',1,'fixture')")
    db.execute("INSERT INTO meta.current_publication VALUES ('synthetic:scope','synthetic-pub')")
    db.execute("UPDATE config.basis_application SET enabled=false WHERE data_scope_id='synthetic:scope'")
    assert db.execute("SELECT area_m2 FROM core.published_measure WHERE measure_code='Irr'").fetchone()==(100,)
    assert db.execute("SELECT area_m2 FROM core.explain_measure WHERE measure_code='Irr'").fetchone()==(None,)


def test_numeric_parse_quarantine_preserves_other_measures(db,tmp_path):
    synthetic_scope(db)
    folder,root=stage(tmp_path,{'GreenhouseClimate':[{'time':'43831','Tair':'21','EC_drain_PC':'3','pH_drain_PC':'not-a-number'}]})
    result=load(db,folder,root)
    assert result['quarantine_records']==1
    ec=explain(db,'synthetic:scope','Synthetic','2020-01-01','EC_drain_PC')['values'][0]
    ph=explain(db,'synthetic:scope','Synthetic','2020-01-01','pH_drain_PC')['values'][0]
    assert ec['quality_status']=='accepted' and ec['usable_value']==3
    assert ph['quality_status']=='invalid' and ph['reason_code']=='invalid_numeric' and ph['usable_value'] is None
    assert sum(r['records'] for r in result['record_counts'])==2


def test_run_manifest_cannot_silently_omit_source_files(db,tmp_path):
    synthetic_scope(db)
    folder,root=stage(tmp_path)
    path=folder/'manifest.json'
    manifest=json.loads(path.read_text())
    manifest['files']=[]
    manifest['counts']={key:0 for key in ('read','accepted','quarantine','duplicate','conflict')}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='file set differs'):
        load(db,folder,root)
    assert db.execute('SELECT count(*) FROM meta.pipeline_run').fetchone()==(0,)


@pytest.mark.parametrize('fail_last',[False,True])
def test_thousand_record_batches_keep_counts_and_atomic_rollback(db,tmp_path,fail_last):
    synthetic_scope(db)
    samples=[{'time':str((Decimal(43815)+Decimal(i)/288).quantize(Decimal('.00001')))} for i in range(1001)]
    folder,root=stage(tmp_path,{'GreenhouseClimate':samples})
    if fail_last:
        path=folder/'accepted/Synthetic/GreenhouseClimate.csv.jsonl.gz'
        with gzip.open(path,'rt') as handle: lines=handle.readlines()
        last=json.loads(lines[-1]);last['values']['Tair']='999'
        lines[-1]=json.dumps(last)+'\n'
        with gzip.open(path,'wt') as handle: handle.writelines(lines)
        with pytest.raises(ValueError,match='Staging/raw mismatch'):
            load(db,folder,root)
        assert db.execute('SELECT count(*) FROM core.record_revision').fetchone()==(0,)
        assert db.execute('SELECT count(*) FROM meta.source_record').fetchone()==(0,)
        assert db.execute('SELECT count(*) FROM meta.pipeline_run').fetchone()==(0,)
    else:
        result=load(db,folder,root)
        assert result['load_stats']=={'source_records':1001,'sql_batches':2,'max_batch_records':1000}
        assert db.execute('SELECT count(*) FROM core.record_revision').fetchone()==(2002,)
        assert db.execute('SELECT count(*) FROM core.measure_quality').fetchone()==(10010,)
