"""Verified filesystem ingestion → ClickHouse candidate revisions, no publish.

Reuses the source parser/ingestion manifest, including all conflicting members.
Business quality and sealed marts are evaluated by subsequent BR workflows.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

from greenhouse.clickhouse.client import Client, ClickHouseSettings
from greenhouse.clickhouse.encoding import digest
from greenhouse.clickhouse.repository import Repository, core_eligible
from greenhouse.clickhouse.schema import SCHEMA_VERSION, migrate
from greenhouse.ingestion.run import ingest_snapshot


def verified_snapshot(root):
    root=Path(root).resolve()
    manifest=json.loads((root/'manifest.json').read_text())
    seen=set()
    for entry in manifest['files']:
        relative=Path(entry['path'])
        target=root/relative
        if (relative.is_absolute() or '..' in relative.parts or '\\' in str(relative)
                or str(relative) in seen or not target.is_file()
                or any(p.is_symlink() for p in [target,*target.parents])):
            raise ValueError('Unsafe or repeated snapshot path')
        seen.add(str(relative))
        with target.open('rb') as stream:
            checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        if checksum!=entry['sha256']:
            raise ValueError('Snapshot file checksum mismatch')
    return manifest


def load_snapshot(client, coordinator, raw, output, scope, run_id=None, *, kinds=None, climate_window=None):
    from greenhouse.clickhouse.coordinator import writer_lock
    from greenhouse.clickhouse.encoding import canonical_json
    migrate(client,coordinator)
    manifest=verified_snapshot(raw)
    folder,ingestion=ingest_snapshot(raw,manifest,output,run_id=run_id)
    if ingestion['status'] not in ('completed','completed_with_issues'):
        raise ValueError('Failed/unfinished ingestion cannot complete a candidate load')
    repo=Repository(client,coordinator)
    repo.register_configuration('snapshot',scope,'source','snapshot:'+scope,
        {'snapshot_id':manifest['snapshot_id'],'archive':manifest['archive'],
         'files':manifest['files']})
    if climate_window is not None:
        from datetime import datetime
        if len(climate_window)!=2 or [datetime.fromisoformat(v).isoformat() for v in climate_window]!=list(climate_window) or datetime.fromisoformat(climate_window[0])>=datetime.fromisoformat(climate_window[1]) or any(datetime.fromisoformat(v).tzinfo is not None for v in climate_window):
            raise ValueError('Climate selection requires an explicit half-open source window')
    counts=Counter()
    pending=[]
    def flush():
        if pending:
            for rid,created in repo.append_initial_records(scope,pending):
                membership.append(rid); counts['inserted' if created else 'reused']+=1
            pending.clear()
    membership=[]
    audit_issues=[]
    audit_files=[]
    for file in ingestion['files']:
        selected=kinds is None or Path(file['source_file']).stem in kinds
        selection={'climate_window':list(climate_window)} if climate_window and Path(file['source_file']).stem=='GreenhouseClimate' else {}
        audit_files.append({'source_file':file['source_file'],'metadata':canonical_json(file|{'core_selected':selected}|selection)})
        if kinds is not None and Path(file['source_file']).stem not in kinds:
            counts['deferred_source_rows']+=file.get('read',0)
            continue
        if not file.get('outputs'):
            continue
        for category,relative in file['outputs'].items():
            target=folder/relative
            with gzip.open(target,'rt',encoding='utf-8') as stream:
                for line in stream:
                    record=json.loads(line)
                    if climate_window and Path(file['source_file']).stem=='GreenhouseClimate' and record.get('slot_start') is not None and not climate_window[0]<=record['slot_start']<climate_window[1]:
                        counts['deferred_source_window']+=1
                        continue
                    disposition=record['disposition']
                    counts[disposition]+=1
                    if core_eligible(record):
                        pending.append(record)
                        if len(pending)>=512:
                            flush()
                    if disposition!='accepted':
                        audit_issues.append({'source_record_id':record['source_record_id'],'source_file':record['source_file'],
                            'disposition':disposition,'payload':canonical_json(record)})
                        row={'scope_id':scope,'snapshot_id':record['snapshot_id'],
                            'source_record_id':record['source_record_id'],'source_file':record['source_file'],
                            'line_number':record['line_number'],'disposition':disposition,
                            'content_hash':digest(record),'payload':canonical_json(record)}
                        with writer_lock(coordinator,client):
                            repo._insert('source_disposition',[row],query_id='disposition-'+digest(row))
    flush()
    canonical=repo.canonical_records(scope,revision_ids=membership)
    membership_hash=digest(sorted(r['revision_id'] for r in canonical))
    audit_files.sort(key=lambda r:r['source_file'])
    audit_issues.sort(key=lambda r:(r['source_record_id'],r['disposition'],r['payload']))
    audit_checksum=digest({'files':audit_files,'issues':audit_issues})
    with writer_lock(coordinator,client):
        repo._insert('load_file',[{'scope_id':scope,'load_id':ingestion['run_id']}|r for r in audit_files])
        repo._insert('load_issue',[{'scope_id':scope,'load_id':ingestion['run_id']}|r for r in audit_issues])
        repo._insert('load_member',[{'scope_id':scope,'load_id':ingestion['run_id'],'revision_id':i} for i in membership],query_id='members-'+ingestion['run_id'])
        repo._insert('load_receipt',[{'scope_id':scope,'load_id':ingestion['run_id'],
            'schema_version':SCHEMA_VERSION,'input_hash':digest(manifest),
            'counts_json':canonical_json(dict(counts)),'membership_checksum':membership_hash,'membership_count':len(membership),'audit_checksum':audit_checksum}],query_id='load-'+ingestion['run_id'])
    return {'scope_id':scope,'load_id':ingestion['run_id'],'status':'candidate_loaded',
            'published':False,'counts':dict(counts),'membership_checksum':membership_hash,'ingestion_folder':str(folder)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--coordinator',required=True,type=Path)
    parser.add_argument('--scope',required=True)
    args=parser.parse_args()
    print(json.dumps(load_snapshot(Client(ClickHouseSettings.from_env()),args.coordinator,
        args.raw,args.output,args.scope),ensure_ascii=False))


if __name__=='__main__':
    main()
