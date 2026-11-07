from pathlib import Path
import subprocess,json,os,shutil,hashlib,time
root=Path.cwd();out=root/'tmp/clickhouse-cutover/t52-clone';out.mkdir(exist_ok=False)
project='gh-ch-docs-20261107';clone=out/'source';started=time.time()
def run(args,label,cwd=None):
 r=subprocess.run(args,cwd=cwd or root,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=360)
 (out/(label+'.log')).write_text(r.stdout)
 if r.returncode:raise RuntimeError(label+' failed: '+r.stdout[-2000:])
 return r.stdout
run(['git','clone','--no-hardlinks','--no-checkout',str(root),str(clone)],'clone')
run(['git','checkout','develop'],'checkout',clone)
base=run(['git','rev-parse','HEAD'],'source-head',clone).strip()
# Code is committed; overlay only candidate documentation under test.
for p in [root/'readme.md',*list((root/'docs/architecture').glob('*.md'))]:
 target=clone/p.relative_to(root);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
state=out/'state';raw=out/'data/raw';imp=out/'data/import'
for p in (state,state/'coordinator',state/'output',raw,imp):p.mkdir(parents=True,exist_ok=True);p.chmod(0o777)
example=(clone/'.env.example').read_text();(clone/'.env').write_text(example+'\n'+f'COMPOSE_PROJECT_NAME={project}\nGREENHOUSE_CH_APP_PORT=18586\nCH_DATABASE=docs_fresh\nCH_USER=docs_fresh\nGREENHOUSE_CH_STATE_PATH={state}\nGREENHOUSE_RAW_PATH={raw}\nGREENHOUSE_IMPORT_PATH={imp}\nGREENHOUSE_CH_UI_IMAGE=greenhouse-cutover-ui:20261107\nGREENHOUSE_CH_PROCESSING_IMAGE=greenhouse-cutover-processing:20261107\nGREENHOUSE_CH_CLIENT_IMAGE=greenhouse-cutover-client:20261107\n')
(clone/'.env').chmod(0o600)
prefix=['docker','compose','-p',project]
def compose(args,label):return run(prefix+args,label,clone)
config=json.loads(compose(['--profile','tools','config','--format','json'],'compose'))
assert 'db' not in config['services'] and config['services']['ch-app']['environment']['GREENHOUSE_BACKEND']=='clickhouse'
assert not any(k.startswith('PG') for k in config['services']['ch-app']['environment'])
assert not any(raw.iterdir()) and not any(imp.iterdir())
compose(['up','-d','--wait','--no-build','--pull','never','clickhouse'],'start-db')
compose(['--profile','tools','run','--rm','--no-deps','--pull','never','-T','ch-job','python','-m','greenhouse','migrate'],'migrate')
compose(['--profile','tools','run','--rm','--no-deps','--pull','never','-T','ch-job','python','-m','greenhouse','--help'],'cli-help')
probe='''import json,os,socket
from greenhouse.clickhouse.client import Client,ClickHouseSettings
c=Client(ClickHouseSettings.from_env());t=c.query("SELECT name FROM system.tables WHERE database=currentDatabase() ORDER BY name")
assert len(t)>20,t
assert not any(k.startswith("PG") for k in os.environ)
s=socket.socket();s.settimeout(2);egress=s.connect_ex(("1.1.1.1",443));s.close();assert egress!=0
print(json.dumps(dict(status="passed",version=c.query("SELECT version() AS version")[0]["version"],tables=len(t),external_connect_ex=egress,postgres_environment_absent=True)))'''
probe_result=compose(['--profile','tools','run','--rm','--no-deps','--pull','never','-T','ch-client','python','-c',probe],'database-probe')
compose(['up','-d','--wait','--no-build','--pull','never','ch-app','ch-web'],'start-app')
node='''const {chromium,expect}=require(process.argv[1]);const fs=require('fs');(async()=>{const b=await chromium.launch();try{const p=await b.newPage({viewport:{width:1440,height:1000}});await p.goto('http://127.0.0.1:18586');await p.getByTestId('stApp').waitFor();await p.waitForFunction(()=>document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state')==='notRunning',null,{timeout:120000});await expect(p.getByTestId('stException')).toHaveCount(0);const text=await p.locator('body').innerText();if(!/publication|công bố/i.test(text))throw Error('Missing empty warehouse explanation');fs.writeFileSync(process.argv[2]+'/empty-ui.txt',text);await p.screenshot({path:process.argv[2]+'/empty-ui.png'});console.log(JSON.stringify({status:'passed',no_exception:true,empty_data_explanation:true}));}finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1});'''
run(['node','-e',node,str(root/'tools/walkthrough/node_modules/playwright/test'),str(out)],'browser')
images={name:run(['docker','image','inspect',name,'--format','{{.Id}}'],'image-'+str(i)).strip() for i,name in enumerate(['greenhouse-cutover-ui:20261107','greenhouse-cutover-processing:20261107','greenhouse-cutover-client:20261107'])}
run(['docker','network','inspect',project+'_backend'],'network')
compose(['ps','--format','json'],'running')
# Retain only this project's new volumes for diagnosis; stop its small fixture.
compose(['stop'],'stop-fixture')
result=dict(status='passed',source_head=base,candidate_docs_only=True,project=project,images=images,prepared_images_reused=True,normal_docker_build_repeated=False,fresh_database=True,raw_import_empty=True,migrations=True,cli_help=True,native_default_without_postgres=True,browser_empty_state=True,internal_egress_blocked=True,fixture_stopped=True,user_volumes_untouched=True,elapsed_seconds=round(time.time()-started,3))
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
