"""Run the real live replay stage only on the accepted isolated recording copy."""
import argparse
import fcntl
import json
import os
import re
from pathlib import Path
import shutil
import subprocess

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--setup',type=Path,required=True)
parser.add_argument('--preparation',type=Path,required=True)
parser.add_argument('--run-name',default='live-replay')
parser.add_argument('mode',choices=('prepare','complete'))
args=parser.parse_args()
if not re.fullmatch(r'live-replay(?:-[a-z0-9]+)?',args.run_name):raise ValueError('Explicit replay run name required')
setup=json.loads(args.setup.read_text());root=Path(setup['root']);state=root/'rebuilt/state'
runtime=json.loads((args.preparation/'runtime.json').read_text())
assert runtime['purpose']=='isolated_walkthrough' and runtime['backend']=='clickhouse'
assert runtime['project']==setup['project']+'-rebuilt'
minimum=3 if args.mode=='prepare' else 1
if shutil.disk_usage(root).free<minimum*1024**3:raise ValueError('Insufficient headroom for replay stage')
env=dict(os.environ,GREENHOUSE_CH_STATE_PATH=str(state),GREENHOUSE_RAW_PATH=str(root/'rebuilt/data/raw'),
    GREENHOUSE_IMPORT_PATH=str(root/'rebuilt/data/import'),GREENHOUSE_CH_APP_PORT='18584')
command=['docker','compose','-f','compose.clickhouse.yaml','-f',str(root/'rebuilt/runtime.yaml'),
    '-f',str(args.preparation.resolve()/'runtime.yaml'),'-p',runtime['project'],'--profile','tools','--profile','sensor']
with (root/'runtime-stage.lock').open('a') as lock:
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    subprocess.run(command+['up','-d','--wait','--pull','never','ch-broker'],cwd=setup['checkout'],env=env,check=True)
    subprocess.run(command+['run','--rm','--no-deps','--pull','never','-T','--interactive=false',
        '-e','GREENHOUSE_WALKTHROUGH_REPLAY_RUN='+args.run_name,'ch-job','python','/walkthrough/live_replay.py',args.mode],cwd=setup['checkout'],env=env,check=True)
if args.mode=='prepare':
    replay_state=state/'walkthrough'/args.run_name
    partial=json.loads((replay_state/'partial-result.json').read_text())
    context=args.preparation/('context.json' if args.run_name=='live-replay' else 'context-'+args.run_name+'.json')
    if context.exists() and args.run_name!='live-replay':raise ValueError('Preserve prior context')
    value=json.loads((args.preparation/'context.json').read_text())
    value['live_replay']={**partial,'complete_result_host_path':str(replay_state/'complete-result.json'),
        'completion_command':['python3',str(Path(__file__).resolve()),'--setup',str(args.setup.resolve()),
            '--preparation',str(args.preparation.resolve()),'--run-name',args.run_name,'complete']}
    context.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
    (args.preparation/('live-partial.json' if args.run_name=='live-replay' else 'live-partial-'+args.run_name+'.json')).write_text(json.dumps(partial,indent=2)+'\n')
