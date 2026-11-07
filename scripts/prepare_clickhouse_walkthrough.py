"""Prepare recording on an existing isolated, accepted ClickHouse rehearsal.

Reuses its disposable copy to avoid another full-volume allocation. The original
PostgreSQL and full migration candidate are never addressed. Only localhost
relay joins the frontend bridge; application/DB remain on the internal backend.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', type=Path, required=True)
    parser.add_argument('--target', choices=('rebuilt', 'restored'), default='rebuilt')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    setup = json.loads(args.setup.read_text())
    project = setup['project']+'-'+args.target
    if not re.fullmatch(r'gh-ch-repro-[a-z0-9-]+',project):
        raise ValueError('Explicit disposable rehearsal required')
    if args.output.exists():
        raise ValueError('Use a new recording preparation directory')
    root = Path(setup['root']); state = root/args.target/'state'; data = root/args.target/'data'
    if shutil.disk_usage(root).free < 3*1024**3:
        raise ValueError('Need at least3GiB host headroom before cold preparation')
    assert json.loads((state/('configuration-'+('rebuilt' if args.target=='rebuilt' else 'physical')+'-proof.json')).read_text())['status']=='passed'
    publication = json.loads((state/'historical.json').read_text())['publication_id']
    parity = json.loads((state/'parity/parity.json').read_text())
    if parity.get('status')!='passed' or not parity.get('all_six_brs') or parity.get('clickhouse_publication')!=publication:
        raise ValueError('Passing six-BR parity for this rehearsal publication required')
    args.output=args.output.resolve();args.output.mkdir(parents=True)
    (state/'walkthrough').mkdir(exist_ok=True,mode=0o777);(state/'walkthrough').chmod(0o777)
    tools=Path(__file__).resolve().parents[1]/'tools/walkthrough'
    extra=args.output/'runtime.yaml'
    extra.write_text(json.dumps({'services':{'ch-job':{'volumes':[str(tools)+':/walkthrough:ro']}},
        'networks':{'frontend':{'internal':False}}},indent=2)+'\n')
    env=dict(os.environ,GREENHOUSE_CH_STATE_PATH=str(state),GREENHOUSE_RAW_PATH=str(data/'raw'),
        GREENHOUSE_IMPORT_PATH=str(data/'import'),GREENHOUSE_CH_APP_PORT='18584')
    base=['docker','compose','-f','compose.clickhouse.yaml','-f',str(root/args.target/'runtime.yaml'),
        '-f',str(extra),'-p',project,'--profile','tools','--profile','app']
    with (root/'runtime-stage.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (args.output/'prepare.log').open('w') as log:
            subprocess.run(base+['run','--rm','--no-deps','--pull','never','-T','--interactive=false',
                'ch-job','python','/walkthrough/prepare_clickhouse_context.py','--publication-id',publication,
                '--output','/state/walkthrough/context.json'],cwd=setup['checkout'],env=env,
                stdout=log,stderr=subprocess.STDOUT,check=True)
            subprocess.run(base+['up','-d','--wait','--pull','never','ch-app','ch-web'],
                cwd=setup['checkout'],env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
    shutil.copy2(state/'walkthrough/context.json',args.output/'context.json')
    shutil.copy2(state/'parity/parity.json',args.output/'fixed-full-parity.json')
    app=project+'-ch-app-1'
    inspected=json.loads(subprocess.check_output(['docker','inspect',app],text=True))[0]
    app_env=dict(item.split('=',1) for item in inspected['Config']['Env'])
    assert app_env['GREENHOUSE_BACKEND']=='clickhouse'
    assert not any(app_env.get(k) for k in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'))
    source_root=Path(__file__).resolve().parents[1]
    expected={name:hashlib.sha256((source_root/'src/greenhouse/clickhouse'/name).read_bytes()).hexdigest()
              for name in ('client.py','threshold.py')}
    probe="import hashlib,inspect,json; from pathlib import Path; from greenhouse.clickhouse import client,threshold; print(json.dumps({Path(inspect.getfile(m)).name:hashlib.sha256(Path(inspect.getfile(m)).read_bytes()).hexdigest() for m in (client,threshold)}))"
    actual=json.loads(subprocess.check_output(['docker','exec',app,'python','-c',probe],text=True))
    if actual!=expected:
        raise ValueError('Rebuild rehearsal images from current source before recording')
    server_config=subprocess.check_output(['docker','exec',project+'-clickhouse-1','cat',
        '/etc/clickhouse-server/config.d/local.xml'])
    server_config_hash=hashlib.sha256(server_config).hexdigest()
    if server_config_hash!=hashlib.sha256((source_root/'docker/clickhouse/local.xml').read_bytes()).hexdigest():
        raise ValueError('Load current server XML before recording this rehearsal')
    result=dict(status='ready',backend='clickhouse',purpose='isolated_walkthrough',project=project,
        database=app_env['CH_DATABASE'],publication=publication,url='http://127.0.0.1:18584',
        ui_image=inspected['Image'],source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        source_files=actual,server_config_sha256=server_config_hash,
        app_postgres_credentials_present=False,app_networks=list(inspected['NetworkSettings']['Networks']),
        preparation_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        context_script_sha256=hashlib.sha256((tools/'prepare_clickhouse_context.py').read_bytes()).hexdigest(),
        source_originals_changed=False)
    (args.output/'runtime.json').write_text(json.dumps(result,indent=2)+'\n')
    context=json.loads((args.output/'context.json').read_text())
    context['runtime_verification']={key:result[key] for key in ('ui_image','source_commit','source_files','server_config_sha256','app_postgres_credentials_present','app_networks')}
    (args.output/'context.json').write_text(json.dumps(context,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
