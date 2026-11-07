"""Run a logged helper in a prepared clean-clone rehearsal, with no image pulls.

Usage: python scripts/clickhouse_repro/run.py --setup tmp/.../current.json
       --label parity -- python /migration/check_parity.py ...
The setup JSON records the clone, state paths, project and unique image tags.
Only the explicitly named disposable project is addressed.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', type=Path, required=True)
    parser.add_argument('--target', choices=('source', 'restored', 'rebuilt'), default='source')
    parser.add_argument('--label', required=True)
    parser.add_argument('--service', choices=('ch-job', 'ch-ui-test'), default='ch-job')
    parser.add_argument('--env', action='append', default=[])
    parser.add_argument('--min-free-gib', type=float,
        help='Raise the enforced free-space minimum (3 GiB reads; 6 GiB full backfill/rebuild)')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    setup = json.loads(args.setup.read_text())
    project = setup['project'] + ('' if args.target == 'source' else '-' + args.target)
    if not re.fullmatch(r'gh-ch-repro-[a-z0-9-]+', project):
        raise ValueError('Disposable rehearsal project required')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.label):
        raise ValueError('Simple log label required')
    root = Path(setup['root'])
    state = Path(setup['state']) if args.target == 'source' else root / args.target / 'state'
    override = root / 'runtime.yaml' if args.target == 'source' else root / args.target / 'runtime.yaml'
    data = Path(setup['checkout']) / 'data' if args.target == 'source' else root / args.target / 'data'
    env = dict(os.environ, GREENHOUSE_CH_STATE_PATH=str(state),
        GREENHOUSE_RAW_PATH=str(data / 'raw'),
        GREENHOUSE_IMPORT_PATH=str(data / 'import'),
        GREENHOUSE_CH_APP_PORT=str({'source': 18582, 'restored': 18583, 'rebuilt': 18584}[args.target]))
    helper = Path(__file__).resolve().parent
    command = ['docker', 'compose', '-f', 'compose.clickhouse.yaml', '-f', str(override),
        '-p', project, '--profile', 'tools', '--profile', 'test', 'run', '--rm', '--no-deps',
        '--pull', 'never', '-T', '--interactive=false', '--name', project + '-' + args.label,
        '-v', str(helper) + ':/repro:ro', '-v', str(state) + ':/state',
        '-v', str(state) + ':/candidate',
        '-v', str(data / 'raw') + ':/data/raw:ro',
        '-v', str(data / 'import') + ':/data/import:ro',
        '-v', str(root / ('references' if args.target == 'source' else args.target + '/references')) + ':/references:ro',
        '-v', str(Path(setup['checkout']) / 'scripts/clickhouse') + ':/migration:ro']
    for value in args.env:
        command.extend(('-e', value))
    tail = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not tail:
        raise ValueError('Explicit container command required')
    full_load = any(p in tail for p in ('/repro/initial_backfill.py', '/migration/backfill.py')) or (
        '/repro/native_state.py' in tail and 'rebuild' in tail)
    minimum = 6 if full_load else 3
    if args.min_free_gib is not None:
        if not args.min_free_gib >= minimum:
            raise ValueError('Cannot lower the enforced headroom minimum')
        minimum = args.min_free_gib
    command += [args.service, *tail]
    logfile = root / 'evidence' / (args.target + '-' + args.label + '.log')
    logfile.parent.mkdir(parents=True, exist_ok=True)
    if logfile.exists():
        raise ValueError('Choose a new label; preserve previous logs')
    print(json.dumps({'stage': args.label, 'project': project, 'log': str(logfile)}), flush=True)
    # A full raw load and a cold UI proof can each spill large verified inputs.
    # Keep all helper stages in this rehearsal serial across target projects.
    # Preserve a failed stage rather than risk a host-wide read-only filesystem.
    with (root / 'runtime-stage.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another rehearsal helper is running; wait for it before starting this stage') from None
        free = shutil.disk_usage(root).free
        if free < minimum * 1024**3:
            raise ValueError(f'Insufficient host headroom: {free / 1024**3:.2f} GiB; need {minimum} GiB before this stage')
        # A previous host runner may have died while its container survived.
        # The flock alone cannot account for that orphaned stage.
        for owner in (setup['project'], setup['project']+'-restored', setup['project']+'-rebuilt'):
            names = subprocess.check_output(['docker', 'ps', '--filter',
                'label=com.docker.compose.project='+owner, '--format', '{{.Names}}'], text=True).split()
            for name in names:
                labels = json.loads(subprocess.check_output(['docker','inspect','--format',
                    '{{json .Config.Labels}}',name],text=True))
                if str(labels.get('com.docker.compose.oneoff', '')).lower() == 'true':
                    raise ValueError('An owned helper container is still running: '+name)
        with logfile.open('w') as stream:
            stream.write(json.dumps(dict(stage=args.label, preflight_host_free_bytes=free,
                enforced_headroom_gib=minimum, helpers_serialized=True)) + '\n')
            stream.flush()
            result = subprocess.run(command, cwd=setup['checkout'], env=env,
                stdout=stream, stderr=subprocess.STDOUT)
    print(json.dumps({'stage': args.label, 'exit_code': result.returncode,
        'log': str(logfile)}), flush=True)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
