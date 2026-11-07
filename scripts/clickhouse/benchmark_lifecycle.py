"""Fresh/rerun diagnostics on disposable benchmark DBs with real pipeline stages."""
import argparse
from collections import defaultdict
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
import zipfile

from greenhouse.ingestion.snapshot import verify_archive,validate_snapshot,archive_members


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=('postgres','clickhouse'),required=True)
    parser.add_argument('--mode',choices=('fresh','rerun'),default='fresh')
    for name in ('metadata','raw','archive','output','report'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--workflow-id',required=True)
    parser.add_argument('--coordinator',type=Path)
    args=parser.parse_args();metadata=json.loads(args.metadata.read_text())
    if metadata.get('backend')!='postgres' or metadata.get('read_only') is not True:
        raise ValueError('Frozen read-only source metadata required')
    scope=metadata['source_publication']['scope_id'];phases=defaultdict(list)
    report={'status':'in_progress','backend':args.backend,'mode':args.mode,
        'started_at_utc':datetime.now(timezone.utc).isoformat(),'workflow_id':args.workflow_id,
        'snapshot_id':metadata['source_publication']['snapshot_id'],'scope_id':scope,
        'independent_runs':1,'database_os_caches_flushed':False,'phases_seconds':phases,
        'stage_timings_overlap':True,'pipeline_result':None}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    def save():args.report.write_text(json.dumps(report,indent=2,default=str)+'\n')
    save();print(json.dumps({'stage':'lifecycle_started','backend':args.backend,'mode':args.mode}),flush=True)
    begin=time.perf_counter()
    try:
        if args.backend=='postgres':
            import psycopg
            from greenhouse.settings import Settings
            from greenhouse.db.migrate import migrate
            from greenhouse.db.pipeline import run_pipeline
            from greenhouse.db.rule_schedule import register_rule_schedule
            from greenhouse.db.cost_adapter import PostgresCostAdapter
            from greenhouse.contracts import RuleScheduleRevision,TariffProfile,from_dict
            settings=Settings.from_env()
            if settings.pg_database!='greenhouse_bench_pg':raise ValueError('Disposable benchmark database required')
            with psycopg.connect(**settings.connection_kwargs(),autocommit=True) as connection:
                if args.mode=='fresh':
                    count=connection.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')").fetchone()[0]
                    if count:raise ValueError('Fresh benchmark database is not empty')
                verify_archive(args.archive)
                with zipfile.ZipFile(args.archive) as archive:validate_snapshot(args.raw,archive,archive_members(archive))
                report['source_validation_seconds']=time.perf_counter()-begin;save()
                migrate(connection)
                result=run_pipeline(connection,snapshot=args.raw,output=args.output,scope=scope,policy='AGC_QUALITY_V1',
                    workflow=args.workflow_id,resume=args.mode=='rerun')
                report['pipeline_seconds']=time.perf_counter()-begin
                steps=connection.execute('''SELECT step,extract(epoch FROM finished_at-started_at)::text,rows_in,rows_out
                    FROM meta.workflow_step WHERE workflow_id=%s ORDER BY started_at''',(args.workflow_id,)).fetchall()
                report['workflow_steps']=[dict(zip(('step','seconds','rows_in','rows_out'),row,strict=True)) for row in steps]
                # Query-time metadata is separate from the timed historical pipeline.
                for value in metadata['rules']:register_rule_schedule(connection,from_dict(RuleScheduleRevision,value))
                adapter=PostgresCostAdapter(connection)
                for value in metadata['tariffs']:adapter.register_tariff(from_dict(TariffProfile,value))
        else:
            from greenhouse.clickhouse.client import Client,ClickHouseSettings
            from greenhouse.clickhouse import energy_run
            from greenhouse.clickhouse.publication import Publications
            from greenhouse.clickhouse.water import WaterMart
            from greenhouse.clickhouse.harvest import HarvestMart
            from greenhouse.clickhouse.threshold import SlotMart
            from greenhouse.clickhouse.energy import EnergyMart
            import backfill
            settings=ClickHouseSettings.from_env()
            if settings.database!='greenhouse_bench_ch':raise ValueError('Disposable benchmark database required')
            if args.coordinator is None:raise ValueError('Explicit coordinator required')
            client=Client(settings)
            if args.mode=='fresh' and int(client.query('SELECT count() AS n FROM system.tables WHERE database=currentDatabase()')[0]['n']):
                raise ValueError('Fresh benchmark database is not empty')
            args.coordinator.mkdir(parents=True,exist_ok=True)
            def timer(name,function):
                def measured(*positional,**keywords):
                    started=time.perf_counter()
                    try:return function(*positional,**keywords)
                    finally:
                        phases[name].append(time.perf_counter()-started);save()
                return measured
            bindings=[(energy_run,'load_snapshot','ingest_and_core_load'),
                (energy_run,'corrected_membership','freeze_membership'),
                (Publications,'publish','publication_total'),(Publications,'_verify','publication_verification'),
                (WaterMart,'build','water_transform_write'),(HarvestMart,'build','harvest_transform_write'),
                (SlotMart,'build','threshold_transform_write'),(EnergyMart,'build','energy_transform_write')]
            from contextlib import ExitStack
            arguments=['backfill.py','--metadata',str(args.metadata),'--raw',str(args.raw),'--archive',str(args.archive),
                '--output',str(args.output),'--coordinator',str(args.coordinator),'--workflow-id',args.workflow_id]
            with ExitStack() as stack:
                for owner,name,label in bindings:stack.enter_context(patch.object(owner,name,timer(label,getattr(owner,name))))
                stack.enter_context(patch.object(sys,'argv',arguments))
                result=backfill.main() # Main also returns the same printed result.
            if result is None:raise ValueError('Backfill helper must return its verified pipeline result')
            report['pipeline_seconds']=time.perf_counter()-begin
        report.update(status='measured',pipeline_result=result,finished_at_utc=datetime.now(timezone.utc).isoformat(),
            total_with_query_metadata_seconds=time.perf_counter()-begin)
        report['processing_cgroup']={}
        for name in ('cpu.max','memory.max','memory.peak'):
            path=Path('/sys/fs/cgroup')/name
            if path.is_file():report['processing_cgroup'][name]=path.read_text().strip()
        save();print(json.dumps({'status':'measured','backend':args.backend,'pipeline_seconds':report['pipeline_seconds'],
            'publication_id':result['publication_id']}),flush=True)
    except BaseException as error:
        report.update(status='failed',error_type=type(error).__name__,
            finished_at_utc=datetime.now(timezone.utc).isoformat(),elapsed_seconds_at_failure=time.perf_counter()-begin)
        save();raise



if __name__=='__main__':main()
