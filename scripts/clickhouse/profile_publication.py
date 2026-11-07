"""One cold verification diagnostic, separate from the repeated query benchmark."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import time

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.registry import publication_envelope


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();reference=json.loads(args.reference.read_text())
    if reference['publication_id']!=args.publication_id:raise ValueError('Different reference publication')
    started=datetime.now(timezone.utc).isoformat();begin=time.perf_counter()
    client=Client(ClickHouseSettings.from_env())
    try:
        result=publication_envelope(client,args.publication_id)
        elapsed=time.perf_counter()-begin
        if (result['load_id'],result['receipts'])!=(reference['load_id'],reference['receipts']):
            raise ValueError('Full publication receipts differ from correctness baseline')
        report={'kind':'cold_publication_verification_diagnostic','status':'passed',
            'started_at_utc':started,'finished_at_utc':datetime.now(timezone.utc).isoformat(),
            'seconds':elapsed,'publication_id':args.publication_id,'receipts_match_reference':True,
            'core_rows':result['receipts']['core']['rows'],'client_action_cache':False,
            'independent_samples':1,'is_repeated_six_br_benchmark':False}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)
    finally:client.close()


if __name__=='__main__':main()
