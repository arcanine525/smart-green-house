"""Sample only the named benchmark containers; no service mutations."""
import argparse,json,subprocess,time
from datetime import datetime,timezone
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--job',required=True);p.add_argument('--server',required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True)
with a.output.open('w') as out:
 while True:
  active=subprocess.run(['docker','ps','--filter','name=^/'+a.job+'$','--format','{{.Names}}'],capture_output=True,text=True,check=True).stdout.strip()
  if not active:break
  result=subprocess.run(['docker','stats','--no-stream','--format','{{json .}}',a.server,a.job],capture_output=True,text=True)
  row={'at_utc':datetime.now(timezone.utc).isoformat(),'sampled':[]}
  for line in result.stdout.splitlines():
   if line.startswith('{'):row['sampled'].append(json.loads(line))
  out.write(json.dumps(row)+'\n');out.flush();time.sleep(2)
print(json.dumps({'status':'sampling_complete','file':str(a.output)}))
