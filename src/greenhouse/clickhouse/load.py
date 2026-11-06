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


def load_snapshot(client, coordinator, raw, output, scope, run_id=None, *, kinds=None):
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
    counts=Counter()
    membership=[]
    for file in ingestion['files']:
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
                    disposition=record['disposition']
                    counts[disposition]+=1
                    if core_eligible(record):
                        revision_id,created=repo.append_record(scope,record)
                        membership.append(revision_id)
                        counts['inserted' if created else 'reused']+=1
                    if disposition!='accepted':
                        row={'scope_id':scope,'snapshot_id':record['snapshot_id'],
                            'source_record_id':record['source_record_id'],'source_file':record['source_file'],
                            'line_number':record['line_number'],'disposition':disposition,
                            'content_hash':digest(record),'payload':canonical_json(record)}
                        with writer_lock(coordinator,client):
                            repo._insert('source_disposition',[row],query_id='disposition-'+digest(row))
    canonical=repo.canonical_records(scope,revision_ids=membership)
    membership_hash=digest(sorted(r['revision_id'] for r in canonical))
    with writer_lock(coordinator,client):
        repo._insert('load_member',[{'scope_id':scope,'load_id':ingestion['run_id'],'revision_id':i} for i in membership],query_id='members-'+ingestion['run_id'])
        repo._insert('load_receipt',[{'scope_id':scope,'load_id':ingestion['run_id'],
            'schema_version':SCHEMA_VERSION,'input_hash':digest(manifest),
            'counts_json':canonical_json(dict(counts)),'membership_checksum':membership_hash,'membership_count':len(membership)}],query_id='load-'+ingestion['run_id'])
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
