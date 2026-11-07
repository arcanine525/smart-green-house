"""Snapshot stopped-rehearsal bind state and locked raw/import/config inputs.

Physical ClickHouse/broker volumes are archived separately by volume_archive.py.
This archive includes SQLite WAL/SHM, Spark checkpoints, ready/spool/outbox,
coordinator state, frozen exports, outputs and the actual locked raw/ZIP files.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    result = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Bind snapshot must not contain symlinks')
        if path.is_file():
            result.append(dict(path=path.relative_to(root).as_posix(),
                bytes=path.stat().st_size, sha256=checksum(path)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('backup', 'restore'))
    parser.add_argument('--setup', type=Path, required=True)
    args = parser.parse_args()
    setup = json.loads(args.setup.read_text())
    project = setup['project']
    if not re.fullmatch(r'gh-ch-repro-[a-z0-9-]+', project):
        raise ValueError('Explicit disposable rehearsal identity required')
    target = project if args.mode == 'backup' else project + '-restored'
    running = subprocess.check_output(['docker', 'ps', '--filter',
        'label=com.docker.compose.project=' + target, '--format', '{{.Names}}'], text=True)
    if running.strip():
        raise ValueError('Stop the entire source/target rehearsal before snapshot/extraction')
    root = Path(setup['root'])
    folder = root / 'backup'
    folder.mkdir(exist_ok=True)
    archive = folder / 'bind-state-inputs.tar.gz'
    receipt = folder / 'bind-state-inputs.json'
    if args.mode == 'backup':
        if archive.exists() or receipt.exists():
            raise ValueError('Refuse to overwrite a snapshot')
        state = Path(setup['state'])
        identity = json.loads((state / 'identity.json').read_text())
        if identity['project'] != project:
            raise ValueError('Bind state identity differs from deployment')
        selected = {'state': state, 'data': Path(setup['checkout']) / 'data',
            'runtime.yaml': root / 'runtime.yaml', 'references': root / 'references',
            'clickhouse-local.xml': Path(setup['checkout']) / 'docker/clickhouse/local.xml',
            'mosquitto.conf': Path(setup['checkout']) / 'docker/mosquitto/mosquitto.conf'}
        files = {name: inventory(path) for name, path in selected.items() if path.is_dir()}
        with tarfile.open(archive, 'w:gz', compresslevel=3) as stream:
            for name, path in selected.items():
                stream.add(path, arcname=name)
        value = dict(source_project=project, archive_sha256=checksum(archive),
            archive_bytes=archive.stat().st_size, inventories=files)
        receipt.write_text(json.dumps(value, indent=2) + '\n')
    else:
        value = json.loads(receipt.read_text())
        if value['source_project'] != project or value['archive_sha256'] != checksum(archive):
            raise ValueError('Snapshot identity or archive hash differs')
        if value['archive_bytes'] != archive.stat().st_size:
            raise ValueError('Snapshot bytes differ')
        destination = root / 'restored'
        destination.mkdir(exist_ok=True)
        # An override file may already have been prepared. Never overwrite state.
        if any((destination / name).exists() for name in ('state', 'data')):
            raise ValueError('Restore state/data destination must be absent')
        with tarfile.open(archive, 'r:gz') as stream:
            for member in stream:
                if member.name == 'runtime.yaml' and (destination / 'runtime.yaml').exists():
                    continue
                stream.extract(member, destination, filter='data')
        for name, expected in value['inventories'].items():
            if inventory(destination / name) != expected:
                raise ValueError('Restored bind inventory differs: ' + name)
        (destination / 'bind-restore-proof.json').write_text(json.dumps(dict(
            status='passed', exact_file_bytes=True, archive_sha256=value['archive_sha256'],
            files=sum(len(v) for v in value['inventories'].values())), indent=2) + '\n')
    print(json.dumps(dict(status=args.mode + '_passed', archive=str(archive),
        bytes=value['archive_bytes'], sha256=value['archive_sha256'])), flush=True)


if __name__ == '__main__':
    main()
