"""Pause/drain journaled writes and produce a consistent SQLite backup."""
import argparse
from contextlib import closing
import json
import re
import sys

sys.path[:0]=['/cutover-tools','/repro']
from state import ROOT
from common import COORDINATOR,connect,write_json
from greenhouse.clickhouse import cutover_journal as journal
from greenhouse.clickhouse.repository import Repository

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('mode',choices=['pause','resume','inspect'])
parser.add_argument('--backup-label',default='journal-backup')
args=parser.parse_args()
if not re.fullmatch('[a-zA-Z0-9_-]+',args.backup_label):raise ValueError('Simple backup label required')
with closing(connect()) as client:
    if args.mode=='pause':
        journal.set_mode(client,COORDINATOR,'paused')
        state=journal.inspect(COORDINATOR)
        try:
            Repository(client,COORDINATOR).register_configuration('rollback_fence_probe','cutover','never-written','rollback-fence-probe',{'probe':True})
        except journal.CutoverJournalError:pass
        else:raise AssertionError('Paused writer unexpectedly acknowledged new configuration')
        target=ROOT/args.backup_label;target.mkdir(exist_ok=True)
        receipt=journal.backup(COORDINATOR,target/journal.NAME)
        copied=journal.inspect(target)
        if copied!=state:raise ValueError('SQLite backup differs from paused coordinator journal')
        write_json(ROOT/'journal-backup-receipt.json',dict(receipt,status='passed',head=state['head'],
            cursor=len(state['records']),baseline_hash=state['baseline_hash'],paused_write_refused=True,wal_backup_exact=True))
    elif args.mode=='resume':journal.set_mode(client,COORDINATOR,'active')
    state=journal.inspect(COORDINATOR)
    print(json.dumps({'status':'passed','mode':state['mode'],'cursor':len(state['records']),'head':state['head']}),flush=True)
