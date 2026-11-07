"""Fresh full six-BR/CSV/lineage/raw-threshold proof of the rollback target."""
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path('/cutover')
raw='/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68'
pg=json.loads((ROOT/'postgres-corrected-publication.json').read_text())['publication_id']
native=json.loads((ROOT/'post-cutover-receipt.json').read_text())['publication']
if json.loads((ROOT/'rollback-verification.json').read_text())['status']!='passed':
    raise ValueError('Verify rollback inputs/scenarios before the independent complete parity gate')
refs=ROOT/'references-current';refs.mkdir(exist_ok=False)
start=time.monotonic()
commands=[
 ['read_business_reference.py','--backend','postgres','--publication-id',pg,'--raw',raw,'--output',str(refs/'postgres-business-reference.json')],
 ['read_export_reference.py','--backend','postgres','--publication-id',pg,'--output',str(refs/'postgres-csv-reference.json')],
 ['read_lineage_reference.py','--backend','postgres','--publication-id',pg,'--raw',raw,'--output',str(refs/'postgres-lineage-reference.json')],
 ['read_parity_reference.py','--publication-id',pg,'--raw',raw,'--output',str(refs/'postgres-full-cycle-csv-reference.json')],
 ['check_parity.py','--publication-id',native,'--raw',raw,'--coordinator','/state/coordinator',
    '--references',str(refs),'--output',str(ROOT/'full-parity'),'--transient-rules']]
for command in commands:
    print(json.dumps({'stage':command[0],'status':'started'}),flush=True)
    subprocess.run([sys.executable,'/migration/'+command[0],*command[1:]],check=True)
proof=json.loads((ROOT/'full-parity/parity.json').read_text())
if proof['status']!='passed' or len(proof['threshold_groups'])!=18:
    raise ValueError('All six BR contracts and 18 threshold groups must pass')
proof['all_six_brs']=True;proof['elapsed_seconds']=time.monotonic()-start
proof['scope']='Fresh corrected publications on both engines; PG/raw oracle, 96 CSV and 30 lineage probes'
(ROOT/'final-six-br-parity.json').write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps({'stage':'final_six_br_parity','status':'passed','elapsed_seconds':proof['elapsed_seconds']}),flush=True)
