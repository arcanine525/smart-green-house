"""Prepare a clean local clone and locked images before isolated CH rehearsal.

All input files are local exports/snapshots, never personal credentials or a live
source database. An existing destination is refused. No cleanup/prune is done.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'root', 'raw', 'archive', 'metadata', 'references'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.root.exists():
        raise ValueError('A new absent rehearsal destination is required')
    args.root.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(args.root.parent).free
    if free < 10 * 1024**3:
        raise ValueError('At least 10 GiB host headroom is required for images, snapshots and serial verification; inspect Docker storage before continuing')
    metadata = json.loads(args.metadata.read_text())
    if metadata.get('backend') != 'postgres' or metadata.get('read_only') is not True:
        raise ValueError('The initial backfill requires a frozen read-only PostgreSQL export')
    manifest = json.loads((args.raw / 'manifest.json').read_text())
    if metadata['source_publication']['snapshot_id'] != manifest['snapshot_id']:
        raise ValueError('Metadata and raw snapshot differ')
    if sha(args.archive) != manifest['archive']['sha256']:
        raise ValueError('Locked archive checksum differs')
    run = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    project = 'gh-ch-repro-' + run
    root = args.root.resolve()
    root.mkdir()
    checkout = root / 'checkout'
    subprocess.run(['git', 'clone', '--no-hardlinks', '--branch', 'develop', '--single-branch',
        str(args.source.resolve()), str(checkout)], check=True)
    head = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
    shutil.copy2(checkout / '.env.example', checkout / '.env')
    raw = checkout / 'data/raw/agc-2nd-2019' / manifest['archive']['sha256']
    shutil.copytree(args.raw, raw)
    imports = checkout / 'data/import'
    imports.mkdir(parents=True)
    shutil.copy2(args.archive, imports / 'autonomous-greenhouse-challengeagc-2nd-2019.zip')
    state = root / 'state'
    for path in (state, state / 'coordinator', state / 'sensor', state / 'output'):
        path.mkdir(mode=0o777)
        path.chmod(0o777)
    shutil.copy2(args.metadata, state / 'source-metadata.json')
    shutil.copytree(args.references, root / 'references')
    (root / 'evidence').mkdir()
    (root / 'backup').mkdir()
    identity = dict(project=project, source_head=head, database='greenhouse_repro')
    (state / 'identity.json').write_text(json.dumps(identity, indent=2) + '\n')
    images = {kind: 'greenhouse-ch-repro-' + kind + ':' + run for kind in ('processing', 'ui', 'client')}
    setup = dict(run=run, root=str(root), checkout=str(checkout), state=str(state), project=project,
        restore_project=project+'-restored', rebuild_project=project+'-rebuilt', source_head=head, images=images)
    (root / 'setup.json').write_text(json.dumps(setup, indent=2) + '\n')
    services = {'clickhouse': {'environment': dict(CLICKHOUSE_DB='greenhouse_repro',
        CLICKHOUSE_USER='greenhouse_repro', CLICKHOUSE_PASSWORD='rehearsal-local-only')}}
    helper = Path(__file__).resolve().parent
    for service, kind in (('ch-app','ui'),('ch-ui-test','ui'),('ch-client','client'),
                          ('ch-web','client'),('ch-job','processing'),('ch-stream','processing')):
        env = dict(CH_HOST='clickhouse', CH_PORT='8123', CH_DATABASE='greenhouse_repro',
            CH_USER='greenhouse_repro', CH_PASSWORD='rehearsal-local-only', GREENHOUSE_REHEARSAL_PROJECT=project)
        if kind == 'processing':
            env.update(MQTT_HOST='ch-broker', SPARK_LOCAL_IP='127.0.0.1',
                PYSPARK_SUBMIT_ARGS='--driver-memory 768m pyspark-shell')
        services[service] = dict(image=images[kind], environment=env)
        if kind == 'processing':
            services[service]['volumes'] = [str(helper)+':/repro:ro', str(state)+':/state',
                str(root/'references')+':/references:ro', str(checkout/'scripts/clickhouse')+':/migration:ro']
    override = dict(services=services, networks={'backend':{'internal':True},'frontend':{'internal':True}})
    # JSON is valid YAML and avoids an unpinned host YAML dependency.
    (root / 'runtime.yaml').write_text(json.dumps(override, indent=2) + '\n')
    with (root / 'evidence/build.log').open('w') as log:
        for kind, dockerfile in (('processing','docker/processing/Dockerfile'),
                                ('ui','docker/ui/Dockerfile'), ('client','docker/clickhouse-client/Dockerfile')):
            subprocess.run(['docker','build','-f',dockerfile,'-t',images[kind],'.'],
                cwd=checkout,stdout=log,stderr=subprocess.STDOUT,check=True)
    receipt = dict(source_head=head, preflight_host_free_bytes=free, required_headroom_bytes=10*1024**3,
        metadata_sha256=sha(state/'source-metadata.json'), archive_sha256=sha(args.archive),
        images={kind:subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',tag],text=True).strip()
                for kind,tag in images.items()}, personal_secrets_copied=False,
        runtime_networks_internal=True, source_database_connected=False)
    (root / 'evidence/preparation.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(dict(prepared=True, setup=str(root/'setup.json'), project=project)),flush=True)


if __name__ == '__main__':
    main()
