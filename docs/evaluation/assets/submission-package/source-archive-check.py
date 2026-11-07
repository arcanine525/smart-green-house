from pathlib import Path
import subprocess,json,hashlib
root=Path.cwd();bundle=root/'project-submit/29_application.zip';out=root/'tmp/submission-rehearsal/unpacked-final';out.mkdir(exist_ok=False)
subprocess.run(['unzip','-q',str(bundle),'-d',str(out)],check=True)
source=out/'smart-green-house'
assert not (source/'.git').exists() and not (source/'.env').exists() and not (source/'TODO.MD').exists()
assert (source/'scripts/check_submission.sh').stat().st_mode & 0o111
config=json.loads(subprocess.check_output(['docker','compose','--profile','tools','config','--format','json'],cwd=source,text=True))
assert 'db' not in config['services'] and config['services']['ch-app']['environment']['GREENHOUSE_BACKEND']=='clickhouse'
image='greenhouse-cutover-processing:20261107'
command=['docker','run','--rm','--network','none','--cpus','0.5','--memory','512m','-e','PYTHONPATH=/submission-source/src','-v',str(source)+':/submission-source:ro','--entrypoint','python',image,'-m','greenhouse','--help']
r=subprocess.run(command,capture_output=True,text=True);(out/'cli-help.log').write_text(r.stdout+r.stderr);assert r.returncode==0 and 'clickhouse' in r.stdout
image_id=subprocess.check_output(['docker','image','inspect',image,'--format','{{.Id}}'],text=True).strip()
result=dict(status='passed',zip_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),source_files=len(json.loads((source/'source-manifest.json').read_text())['files']),safe_extraction=True,executable_modes=True,no_personal_env_git_todo=True,default_compose_clickhouse_without_pg=True,actual_cli_from_extracted_source=True,prepared_image=image_id,container_network='none',no_full_docker_rebuild_claimed=True)
(root/'tmp/submission-rehearsal/source-package-final.json').write_text(json.dumps(result,indent=2)+'\n');print(result)
