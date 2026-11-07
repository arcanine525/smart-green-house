"""Operate only a named disposable rehearsal and prepare isolated target paths.

No destructive cleanup is exposed. Physical volumes must be backed up and
verified explicitly before any separately authorized removal.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--setup', type=Path, required=True)
parser.add_argument('--target', choices=('source', 'restored', 'rebuilt'), default='source')
parser.add_argument('mode', choices=('prepare', 'create', 'database', 'broker', 'app', 'stop', 'inspect'))
args = parser.parse_args()
setup = json.loads(args.setup.read_text())
base = setup['project']
if not re.fullmatch(r'gh-ch-repro-[a-z0-9-]+', base):
    raise ValueError('Disposable rehearsal project required')
root = Path(setup['root'])
project = base + ('' if args.target == 'source' else '-' + args.target)
folder = root if args.target == 'source' else root / args.target
state = Path(setup['state']) if args.target == 'source' else folder / 'state'
data = Path(setup['checkout']) / 'data' if args.target == 'source' else folder / 'data'
override = folder / 'runtime.yaml'
if args.mode == 'prepare':
    if args.target == 'source':
        raise ValueError('Prepare a target only')
    folder.mkdir(exist_ok=True)
    if args.target == 'rebuilt':
        if state.exists() or data.exists():
            raise ValueError('Fresh rebuild target already exists')
        restored = root / 'restored'
        if not (restored / 'bind-restore-proof.json').is_file():
            raise ValueError('Verified bind restore required')
        state.mkdir(mode=0o777)
        state.chmod(0o777)
        for name in ('output', 'coordinator', 'sensor'):
            (state / name).mkdir(mode=0o777)
            (state / name).chmod(0o777)
        for name in ('identity.json', 'source-metadata.json', 'native-state.json'):
            shutil.copy2(restored / 'state' / name, state / name)
        # Copy source evidence/ready envelopes, never the old SQLite databases
        # or checkpoints into the new warehouse generation.
        for name in ('metadata.json', 'restore-result.json'):
            shutil.copy2(restored / 'state/sensor' / name, state / 'sensor' / name)
        shutil.copytree(restored / 'state/sensor/ready', state / 'sensor/ready')
        shutil.copytree(restored / 'data', data)
        shutil.copytree(restored / 'references', folder / 'references')
    else:
        if not (folder / 'bind-restore-proof.json').is_file():
            raise ValueError('Extract and verify bind archive before preparing override')
    text = (root / 'runtime.yaml').read_text()
    text = text.replace(str(Path(setup['state'])), str(state)).replace(
        str(root / 'references'), str(folder / 'references')).replace('"'+base+'"', '"'+project+'"')
    override.write_text(text)
    print(json.dumps({'prepared': project, 'state': str(state), 'data': str(data)}))
    raise SystemExit(0)
if not override.is_file() or not state.is_dir():
    raise ValueError('Prepared override/state required')
env = dict(os.environ, GREENHOUSE_CH_STATE_PATH=str(state),
    GREENHOUSE_RAW_PATH=str(data / 'raw'), GREENHOUSE_IMPORT_PATH=str(data / 'import'),
    GREENHOUSE_CH_APP_PORT=str({'source':18582,'restored':18583,'rebuilt':18584}[args.target]))
command = ['docker', 'compose', '-f', 'compose.clickhouse.yaml', '-f', str(override),
    '-p', project, '--profile', 'sensor', '--profile', 'app', '--profile', 'tools']
if args.mode == 'create':
    existing = subprocess.check_output(['docker','volume','ls','--filter',
        'label=com.docker.compose.project=' + project,'--format','{{.Name}}'],text=True)
    if existing.strip():
        raise ValueError('Target already owns volumes; never reuse them as an empty restore')
    tail = ['create','--no-build','--pull','never','clickhouse','ch-broker']
elif args.mode == 'stop':
    tail = ['stop','--timeout','30']
elif args.mode == 'inspect':
    tail = ['ps','--all','--format','json']
else:
    service = {'database':['clickhouse'],'broker':['ch-broker'],'app':['ch-app','ch-web']}[args.mode]
    tail = ['up','-d','--wait','--pull','never',*service]
subprocess.run(command + tail, cwd=setup['checkout'], env=env, check=True)
