"""Explicit ClickHouse commands composed from the same sealed adapters as UI.

Configurations are caller supplied or locked AGC defaults. A resume repeats
the configuration selection and never infers versions from a current pointer.
"""
import argparse
from contextlib import closing
import copy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.repository import Repository


def migrate_all(client,coordinator):
    from greenhouse.clickhouse import schema,water,harvest,threshold,energy,stream_store,replay_publication
    for module in (schema,water,harvest,threshold,energy,stream_store,replay_publication):
        module.migrate(client,coordinator)
    return {'status':'migrated','backend':'clickhouse'}


def _pipeline_options(parser):
    parser.add_argument('--workflow-id')
    parser.add_argument('--resume',metavar='WORKFLOW_ID')
    parser.add_argument('--base-load')
    choice=parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--agc-shared-basis',action='store_true')
    choice.add_argument('--config-json',type=Path,help='Object with water, harvest, threshold, energy configuration payloads')
    parser.add_argument('--archive',type=Path,default=Path(os.environ.get('GREENHOUSE_IMPORT_DIR','/data/import'))/'autonomous-greenhouse-challengeagc-2nd-2019.zip')
    parser.add_argument('--climate-start');parser.add_argument('--climate-end')
    for kind in ('water','harvest','threshold','energy'):
        parser.add_argument('--'+kind+'-version',default=kind+'-config-v1')


def parser_for(name):
    from greenhouse.ingestion.snapshot import AGC
    parser=argparse.ArgumentParser(prog='python -m greenhouse --backend clickhouse '+name)
    parser.add_argument('--coordinator',type=Path,default=Path(os.environ.get('GREENHOUSE_CH_COORDINATOR','/candidate/coordinator')))
    if name!='migrate':
        parser.add_argument('--snapshot',type=Path,default=Path(os.environ.get('GREENHOUSE_RAW_DIR','/data/raw'))/AGC.dataset/AGC.sha256)
    if name in ('setup','load-core','run-pipeline','transform','publish'):
        parser.add_argument('--output-root',type=Path,default=Path(os.environ.get('GREENHOUSE_OUTPUT_DIR','/data/output')))
    if name not in ('setup','migrate'):
        parser.add_argument('--scope-id',required=True)
    if name in ('run-pipeline','transform','publish'):_pipeline_options(parser)
    if name=='load-core':
        parser.add_argument('--run-id')
        parser.add_argument('--kinds',nargs='+',choices=['Resources','Production','GreenhouseClimate'],default=['Resources','Production','GreenhouseClimate'])
    if name=='explain-core':
        parser.add_argument('--compartment',required=True);parser.add_argument('--date',required=True,type=date.fromisoformat)
        parser.add_argument('--measure',required=True);parser.add_argument('--limit',type=int,default=10)
    if name=='correct-core':
        parser.add_argument('--source-record-id',required=True)
        parser.add_argument('--record-type',required=True,choices=['climate','drain','resource','harvest'])
        for flag in ('old-value','new-value'):parser.add_argument('--'+flag,type=Path,required=True)
        for flag in ('actor','evidence','correction-id'):parser.add_argument('--'+flag,required=True)
    if name=='lineage':
        parser.add_argument('--publication-id',required=True);parser.add_argument('--revision-id',required=True)
        parser.add_argument('--kind',required=True,choices=['water','harvest','climate','drain','energy'])
    if name=='register-agc-tariff':parser.add_argument('--publication-id',required=True)
    return parser


def _configs(options):
    from greenhouse.clickhouse.load import verified_snapshot
    from greenhouse.clickhouse.water_run import AGC_SNAPSHOT,AGC_SHA,water_configuration
    from greenhouse.clickhouse.harvest_run import harvest_configuration
    from greenhouse.clickhouse.threshold_run import threshold_configuration
    from greenhouse.clickhouse.energy_run import energy_configuration
    if options.config_json:
        configs=json.loads(options.config_json.read_text())
        if set(configs)!={'water','harvest','threshold','energy'}:raise ValueError('Config JSON requires exactly four bundle configurations')
    else:
        from greenhouse.ingestion.snapshot import verify_archive,archive_members,validate_snapshot
        verify_archive(options.archive)
        with zipfile.ZipFile(options.archive) as archive:
            source=validate_snapshot(options.snapshot,archive,archive_members(archive))
        if source['snapshot_id']!=AGC_SNAPSHOT or source['archive']['sha256']!=AGC_SHA:
            raise ValueError('AGC defaults require locked ZIP and raw snapshot')
        comps=['AICU','Automatoes','Digilog','IUACAAS','Reference','TheAutomators']
        configs=dict(water=water_configuration(AGC_SNAPSHOT,comps,assumed_basis=True),
            harvest=harvest_configuration(AGC_SNAPSHOT,comps,assumed_basis=True),
            threshold=threshold_configuration(AGC_SNAPSHOT,comps),
            energy=energy_configuration(options.scope_id,AGC_SNAPSHOT,comps,assumed_basis=True))
    if bool(options.climate_start)!=bool(options.climate_end):raise ValueError('Both climate window bounds required')
    if options.climate_start:configs['threshold']['climate_window']=[options.climate_start,options.climate_end]
    source=verified_snapshot(options.snapshot)
    if any(c['snapshot_id']!=source['snapshot_id'] for c in configs.values()):raise ValueError('Configuration snapshot differs from mounted source')
    return configs


def correct(client,options):
    """Append one audited typed revision; stale writes are refused by repository."""
    from greenhouse.clickhouse.encoding import decimal_text
    repo=Repository(client,options.coordinator)
    rows=[r for r in repo.canonical_records(options.scope_id) if r['source_record_id']==options.source_record_id]
    kind={'climate':'GreenhouseClimate','drain':'GreenhouseClimate','resource':'Resources','harvest':'Production'}[options.record_type]
    if not rows or any(r['record_type']!=kind for r in rows):raise ValueError('Source record absent from requested scope/type')
    old=json.loads(options.old_value.read_text());new=json.loads(options.new_value.read_text())
    if not isinstance(old,dict) or not isinstance(new,dict):raise ValueError('Correction requires full measure maps')
    def normalize(values):
        if any(v is not None and not isinstance(v,str) for v in values.values()):raise ValueError('Measures require exact decimal strings or null')
        return {k:None if v is None else decimal_text(v) for k,v in values.items()}
    old,new=normalize(old),normalize(new)
    previous=max(rows,key=lambda r:int(r['revision']))
    history=client.query('SELECT DISTINCT revision_id,revision,correction_id FROM source_revision WHERE scope_id={scope:String} AND source_record_id={source:String}',
        {'scope':options.scope_id,'source':options.source_record_id})
    reused=[r for r in history if r['correction_id']==options.correction_id]
    if reused:
        if len(reused)!=1:raise ValueError('Conflicting correction identity')
        parents=[r['revision_id'] for r in history if int(r['revision'])==int(reused[0]['revision'])-1]
        if len(parents)!=1:raise ValueError('Correction parent absent or ambiguous')
        previous=repo.canonical_records(options.scope_id,revision_ids=parents)[0]
    source=json.loads(previous['payload'])
    if normalize(source['values'])!=old:raise ValueError('Correction old values are stale')
    if set(new)!=set(old):raise ValueError('Correction cannot change measure set')
    corrected=copy.deepcopy(source);corrected['values']=new;corrected['run_id']='correction:'+options.correction_id
    corrected['null_reasons']={k:source['null_reasons'].get(k,'corrected_null') for k,v in new.items() if v is None}
    rid,created=repo.append_record(options.scope_id,corrected,revision=int(previous['revision'])+1,correction_id=options.correction_id,
        correction={'actor':options.actor,'evidence':options.evidence,'previous_revision':previous['revision_id']})
    return {'status':'corrected' if created else 'already_applied','revision_id':rid,'published':False}


def dispatch(client,name,options):
    if name=='migrate':return migrate_all(client,options.coordinator)
    if name=='setup':
        for path in (options.snapshot,):
            if not path.is_dir() or not os.access(path,os.R_OK|os.X_OK):raise ValueError('Raw snapshot mount is not readable')
        if not options.output_root.is_dir():raise ValueError('Output mount must exist')
        with tempfile.TemporaryFile(dir=options.output_root):pass
        options.coordinator.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryFile(dir=options.coordinator):pass
        client.ping();return {'status':'ready','backend':'clickhouse','migrated':False}
    if name=='load-core':
        from greenhouse.clickhouse.load import load_snapshot
        return load_snapshot(client,options.coordinator,options.snapshot,options.output_root,options.scope_id,options.run_id,kinds=set(options.kinds))
    if name in ('run-pipeline','transform','publish'):
        from greenhouse.clickhouse.energy_run import run
        if bool(options.workflow_id)==bool(options.resume):raise ValueError('Select exactly one workflow-id or resume')
        configs=_configs(options)
        return run(client,options.coordinator,options.snapshot,options.output_root,options.scope_id,options.workflow_id or options.resume,
            *(configs[k] for k in ('water','harvest','threshold','energy')),
            **{k+'_version':getattr(options,k+'_version') for k in configs},base_load=options.base_load)
    if name=='correct-core':return correct(client,options)
    if name=='explain-core':
        from greenhouse.clickhouse.quality import measure_verdict,AGC_QUALITY_DEFINITION
        if not 1<=options.limit<=1000:raise ValueError('Limit must be 1..1000')
        rows=[]
        for r in Repository(client,None).canonical_records(options.scope_id):
            if r['compartment_id']!=options.compartment or r['source_timestamp'][:10]!=options.date.isoformat():continue
            source=json.loads(r['payload'])
            if options.measure not in source['values']:continue
            value,status,app,reason=measure_verdict(AGC_QUALITY_DEFINITION,source,options.measure)
            rows.append(dict(revision_id=r['revision_id'],value=None if value is None else str(value),status=status,applicability=app,reason=reason))
        return {'backend':'clickhouse','rows':rows[:options.limit],'truncated':len(rows)>options.limit}
    if name in ('lineage','register-agc-tariff'):
        from greenhouse.clickhouse.registry import reference
        publication=reference(client,options.publication_id)
        if publication.scope_id!=options.scope_id:raise ValueError('Publication outside requested scope')
        if name=='register-agc-tariff':
            from greenhouse.clickhouse.water_run import AGC_SNAPSHOT
            from greenhouse.clickhouse.cost import CostAdapter
            if publication.snapshot_id!=AGC_SNAPSHOT:raise ValueError('AGC tariff registration requires explicit AGC publication')
            return {'tariff_version':CostAdapter(client,options.coordinator).register_historical_tariff(options.scope_id).tariff_version}
        from greenhouse.app import wiring
        factories={'water':wiring.irrigation_service,'harvest':wiring.harvest_service,'climate':wiring.climate_service,'drain':wiring.drain_service,'energy':wiring.energy_service}
        service=factories[options.kind](client,{publication.snapshot_id:options.snapshot})
        from greenhouse.contracts import to_dict
        return to_dict(service.get_lineage(publication,revision_id=options.revision_id))
    raise ValueError('Unsupported ClickHouse command')


def command(name,argv):
    if name=='test':return subprocess.call([sys.executable,'-m','unittest','discover','-s','tests/clickhouse',*(argv or ['-v'])])
    options=parser_for(name).parse_args(argv)
    try:
        # A command may write. It must freshly verify after mutations instead of
        # enabling the read-only UI action cache.
        with closing(Client(ClickHouseSettings.from_env())) as client:
            result=dispatch(client,name,options)
        print(json.dumps(result,ensure_ascii=False,default=str));return 0
    except Exception as error:
        # No raw error/DSN/SQL text or environment values in command output.
        print(f'{name} failed: {type(error).__name__}; check backend configuration, source/config pins and warehouse logs',file=sys.stderr)
        return 1
