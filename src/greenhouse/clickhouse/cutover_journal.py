"""Optional local rollback journal, durably recorded before a warehouse write.

The caller owns the existing coordinator lock. SQLite FULL/WAL transactions
keep intent and outcome records independent of the ClickHouse volume. Derived
mart inserts are rebuilt from captured source/configuration and are excluded.
"""
from datetime import datetime,timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from greenhouse.clickhouse.client import ClickHouseError

NAME='cutover-journal.sqlite'
DERIVED={'water_header','water_cycle','water_measure','harvest_header','harvest_cycle',
    'harvest_measure','harvest_state','threshold_header','threshold_cycle','threshold_measure',
    'energy_header','energy_cycle','energy_basis','energy_measure'}


class CutoverJournalError(ClickHouseError):
    pass


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def checksum(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def namespace(client):
    s=client.settings
    return dict(host=s.host,port=s.port,database=s.database,user=s.user)


@contextmanager
def connect(root):
    path=Path(root)/NAME
    if path.is_symlink():raise CutoverJournalError('Cutover journal cannot be a symlink')
    db=None
    try:
        db=sqlite3.connect(path,timeout=10)
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        path.chmod(0o600)
        with db:yield db
    except (sqlite3.Error,OSError) as exc:
        raise CutoverJournalError('Rollback journal unavailable; warehouse write acknowledgment withheld') from exc
    finally:
        if db is not None:db.close()


def enable(client,root,cutover_id,baseline):
    """Call while producers/config writers are stopped; baseline is checksummed."""
    from greenhouse.clickhouse.coordinator import writer_lock
    if not cutover_id or not isinstance(baseline,dict) or not baseline:
        raise ValueError('Explicit cutover identity and baseline required')
    root=Path(root)
    with writer_lock(root,client),connect(root) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS session
            (id INTEGER PRIMARY KEY CHECK(id=1),identity TEXT NOT NULL,namespace TEXT NOT NULL,
             baseline TEXT NOT NULL,baseline_hash TEXT NOT NULL,mode TEXT NOT NULL CHECK(mode IN ('active','paused')));
            CREATE TABLE IF NOT EXISTS intent (sequence INTEGER PRIMARY KEY,query_id TEXT UNIQUE NOT NULL,
             previous_hash TEXT NOT NULL,payload TEXT NOT NULL,content_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outcome (query_id TEXT PRIMARY KEY REFERENCES intent(query_id),
             status TEXT NOT NULL,evidence TEXT NOT NULL);''')
        expected=(cutover_id,encoded(namespace(client)),encoded(baseline),checksum(baseline))
        old=db.execute('SELECT identity,namespace,baseline,baseline_hash FROM session WHERE id=1').fetchone()
        if old is not None:
            if old!=expected:raise CutoverJournalError('Cutover identity or baseline changed')
        else:db.execute("INSERT INTO session VALUES (1,?,?,?,?, 'active')",expected)
    from greenhouse.clickhouse.journal import _sync_directory
    _sync_directory(root)


def before_write(client,root,query_id,sql):
    if not (Path(root)/NAME).exists():
        if os.environ.get('GREENHOUSE_CH_REQUIRE_CUTOVER_JOURNAL')=='1':
            raise CutoverJournalError('Required rollback journal is missing; warehouse writes refused')
        return None
    with connect(root) as db:
        row=db.execute('SELECT namespace,mode FROM session WHERE id=1').fetchone()
        if row is None or row[0]!=encoded(namespace(client)):
            raise CutoverJournalError('Cutover journal belongs to another warehouse namespace')
        if row[1]!='active':raise CutoverJournalError('Warehouse writes are paused for cutover/rollback')
        if db.execute('SELECT 1 FROM intent LEFT JOIN outcome USING(query_id) WHERE outcome.query_id IS NULL LIMIT 1').fetchone():
            raise CutoverJournalError('Unreconciled rollback intent; explicit recovery required before further writes')
        table=re.match(r'INSERT INTO ([a-z_]+)\b',sql)
        if table and table[1] in DERIVED:return None
        last=db.execute('SELECT sequence,content_hash FROM intent ORDER BY sequence DESC LIMIT 1').fetchone()
        sequence=1 if last is None else last[0]+1;previous='0'*64 if last is None else last[1]
        payload=encoded(dict(query_id=query_id,sql=sql,created_at=datetime.now(timezone.utc).isoformat()))
        hashed=checksum(dict(sequence=sequence,previous_hash=previous,payload=payload))
        db.execute('INSERT INTO intent VALUES (?,?,?,?,?)',(sequence,query_id,previous,payload,hashed))
    return query_id


def terminal(root,query_id,status,evidence):
    if status not in ('QueryFinish','ExceptionBeforeStart','ExceptionWhileProcessing'):
        raise CutoverJournalError('Terminal query evidence required for rollback journal')
    with connect(root) as db:
        if not db.execute('SELECT 1 FROM intent WHERE query_id=?',(query_id,)).fetchone():
            raise CutoverJournalError('Rollback outcome has no durable intent')
        old=db.execute('SELECT status FROM outcome WHERE query_id=?',(query_id,)).fetchone()
        if old and old[0]!=status:raise CutoverJournalError('Rollback journal outcome conflict')
        db.execute('INSERT OR IGNORE INTO outcome VALUES (?,?,?)',(query_id,status,evidence))


def set_mode(client,root,mode):
    from greenhouse.clickhouse.coordinator import writer_lock
    if mode not in ('active','paused'):raise ValueError('Unknown cutover mode')
    with writer_lock(root,client),connect(root) as db:
        row=db.execute('SELECT namespace FROM session WHERE id=1').fetchone()
        if row is None or row[0]!=encoded(namespace(client)):raise CutoverJournalError('Wrong cutover namespace')
        db.execute('UPDATE session SET mode=? WHERE id=1',(mode,))


def inspect(root,*,require_terminal=True):
    """Validate the chain; an intent without outcome is never called accepted."""
    with connect(root) as db:
        session=db.execute('SELECT identity,namespace,baseline,baseline_hash,mode FROM session WHERE id=1').fetchone()
        if session is None or checksum(json.loads(session[2]))!=session[3]:
            raise CutoverJournalError('Invalid cutover baseline')
        records=[];previous='0'*64
        for sequence,query_id,prior,payload,hashed in db.execute('SELECT * FROM intent ORDER BY sequence'):
            if sequence!=len(records)+1 or prior!=previous or checksum(dict(sequence=sequence,previous_hash=prior,payload=payload))!=hashed:
                raise CutoverJournalError('Cutover journal chain is incomplete or changed')
            value=json.loads(payload)
            if value['query_id']!=query_id:raise CutoverJournalError('Cutover query identity changed')
            outcome=db.execute('SELECT status,evidence FROM outcome WHERE query_id=?',(query_id,)).fetchone()
            if require_terminal and outcome is None:raise CutoverJournalError('Unreconciled rollback intent remains')
            records.append(dict(sequence=sequence,content_hash=hashed,**value,
                outcome=None if outcome is None else dict(status=outcome[0],evidence=outcome[1])))
            previous=hashed
        return dict(format='greenhouse-cutover-journal-v1',identity=session[0],namespace=json.loads(session[1]),
            baseline=json.loads(session[2]),baseline_hash=session[3],mode=session[4],head=previous,records=records)


def backup(root,destination):
    """SQLite backup includes committed WAL pages; copying only the base DB does not."""
    destination=Path(destination)
    if destination.exists():raise ValueError('Preserve previous rollback journal backup')
    from contextlib import closing
    with connect(root) as source,closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    destination.chmod(0o600)
    from greenhouse.clickhouse.journal import _sync_directory
    _sync_directory(destination.parent)
    return dict(status='backed_up',bytes=destination.stat().st_size,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
