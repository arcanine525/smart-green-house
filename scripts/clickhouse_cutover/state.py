"""Freeze portable native state and verify the restored PostgreSQL baseline.

All configuration kinds are archived verbatim, including recovery provenance.
Only derived marts are omitted: they are rebuilt from locked raw and revisions.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path[:0]=['/migration','/repro']
from common import COORDINATOR,connect,historical_id,write_json
from read_business_reference import project
from greenhouse.clickhouse import cutover_journal
from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.encoding import digest,canonical_json
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.registry import reference,publication_envelope
from greenhouse.clickhouse.cost import CostAdapter
from greenhouse.clickhouse.stream_store import receipts,verify_batch
from greenhouse.clickhouse.replay_publication import read_publication
from greenhouse.contracts import to_dict

ROOT=Path('/cutover')
RAW=Path('/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68')


def sealed_read(path):
    wrapper=json.loads(Path(path).read_text())
    if digest(wrapper['archive'])!=wrapper['sha256']:raise ValueError('Cutover archive changed')
    if wrapper['archive']['format']!='greenhouse-cutover-state-v1':raise ValueError('Wrong cutover archive format')
    return wrapper['archive'],wrapper['sha256']


def freeze(label,publication=None):
    import re
    if not re.fullmatch('[a-zA-Z0-9_-]+',label):raise ValueError('Simple archive label required')
    if (ROOT/(label+'.json')).exists():raise ValueError('Preserve previous archive; choose a new label')
    with closing(connect(action=True)) as client,writer_lock(COORDINATOR,client):
        pub=reference(client,publication or historical_id())
        registry=Repository(client,COORDINATOR).configurations()
        cost=CostAdapter(client,COORDINATOR)
        corrections=client.query("SELECT DISTINCT * FROM source_revision WHERE revision>1 ORDER BY scope_id,source_record_id,revision")
        for row in corrections:
            parent=json.loads(row['payload'])['correction_audit']['previous_revision']
            matches=client.query('SELECT DISTINCT * FROM source_revision WHERE scope_id={scope:String} AND revision_id={id:String}',
                {'scope':row['scope_id'],'id':parent})
            if len(matches)!=1 or matches[0]['source_record_id']!=row['source_record_id'] or int(matches[0]['revision'])+1!=int(row['revision']):
                raise ValueError('Correction parent is missing, foreign or ambiguous')
            row['parent']=matches[0]
        # Full original correction payloads include actor/evidence/previous row.
        batches=[]
        for receipt in receipts(client):
            batches.append(dict(receipt=receipt,records=verify_batch(client,receipt)))
        replays=[]
        pointers=client.query('SELECT DISTINCT replay_id,scope_id FROM replay_pointer ORDER BY scope_id,replay_id')
        for pointer in pointers:
            latest=client.query('SELECT DISTINCT publication_id,generation FROM replay_pointer WHERE replay_id={id:String} AND scope_id={scope:String} ORDER BY generation DESC',
                {'id':pointer['replay_id'],'scope':pointer['scope_id']})
            pins=client.query('SELECT DISTINCT publication_id FROM replay_seal WHERE replay_id={id:String} AND scope_id={scope:String} ORDER BY publication_id',
                {'id':pointer['replay_id'],'scope':pointer['scope_id']})
            for pin in pins:
                meta,events=read_publication(client,pin['publication_id'])
                replays.append(dict(**pointer,publication_id=pin['publication_id'],
                    current=pin['publication_id']==latest[0]['publication_id'],
                    metadata=json.loads(canonical_json(meta)),events=events))
        archive=dict(format='greenhouse-cutover-state-v1',backend='clickhouse',frozen_at=datetime.now(timezone.utc).isoformat(),
            publication=to_dict(pub),envelope=publication_envelope(client,pub.publication_id),
            configurations=registry,scenarios=[to_dict(s) for s in cost.list_scenarios(pub.scope_id)],
            corrections=corrections,batches=batches,replays=replays,
            business=project(client,pub.publication_id,RAW)['results'])
        if (COORDINATOR/cutover_journal.NAME).exists():archive['journal']=cutover_journal.inspect(COORDINATOR)
        write_json(ROOT/(label+'.json'),dict(archive=archive,sha256=digest(archive)))
        result=dict(status='passed',sha256=digest(archive),publication=pub.publication_id,
            configuration_versions=len(registry['rows']),scenario_versions=len(archive['scenarios']),
            corrected_revisions=len(corrections),batches=len(batches),replays=len(replays))
        write_json(ROOT/(label+'-receipt.json'),result);print(json.dumps(result),flush=True)
        return archive


def pg_connect():
    import psycopg
    from greenhouse.settings import Settings
    conn=psycopg.connect(**Settings.from_env().connection_kwargs(),autocommit=True)
    if conn.info.dbname!='greenhouse_rollback' or conn.info.host!='rollback-db':
        conn.close();raise ValueError('Writes require the separately restored rollback fixture')
    return conn


def baseline():
    archive,hashed=sealed_read(ROOT/'cutoff.json')
    from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
    with pg_connect() as conn:
        from rollback import archive_native,configurations
        # The older dump has the same raw/marts, but lacks the candidate's
        # newer scoped tariffs and user profiles. Transfer the final config
        # delta before comparing complete DTOs; never erase tariff identities.
        kinds=archive_native(conn,archive,hashed);configurations(conn,archive)
        source=archive['publication'];pub=PostgresEnergyQueryAdapter(conn).publication('3e4a051009084e11ab19efdd2343c9d3')
        if (pub.scope_id,pub.snapshot_id)!=(source['scope_id'],source['snapshot_id']):raise ValueError('PG rollback scope differs')
        result=project(conn,pub.publication_id,RAW)
        write_json(ROOT/'postgres-baseline-business.json',result)
        if result['results']!=archive['business']:raise ValueError('Restored PG baseline differs from cutover business contracts')
        write_json(ROOT/'baseline-map.json',dict(status='passed',source_archive_sha256=hashed,
            clickhouse=source,postgres=to_dict(pub),business_exact=True,configuration_kinds=kinds))
    print(json.dumps({'status':'passed','stage':'postgres_baseline_parity'}),flush=True)


def enable():
    archive,hashed=sealed_read(ROOT/'cutoff.json')
    mapping=json.loads((ROOT/'baseline-map.json').read_text())
    if mapping['status']!='passed' or mapping['source_archive_sha256']!=hashed:raise ValueError('Baseline must pass before enabling writes')
    with closing(connect()) as client:
        cutover_journal.enable(client,COORDINATOR,'clickhouse-cutover-20261107',dict(cutoff_sha256=hashed,
            publication=archive['publication'],postgres_baseline=mapping['postgres'],raw_snapshot_sha256=RAW.name))
        write_json(ROOT/'journal-enabled.json',cutover_journal.inspect(COORDINATOR))
    print(json.dumps({'status':'passed','stage':'journal_enabled'}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['cutoff','baseline','enable','freeze'])
    parser.add_argument('--label',default='post-cutover');parser.add_argument('--publication')
    args=parser.parse_args()
    if args.mode=='cutoff':freeze('cutoff')
    elif args.mode=='freeze':freeze(args.label,args.publication)
    elif args.mode=='baseline':baseline()
    else:enable()
