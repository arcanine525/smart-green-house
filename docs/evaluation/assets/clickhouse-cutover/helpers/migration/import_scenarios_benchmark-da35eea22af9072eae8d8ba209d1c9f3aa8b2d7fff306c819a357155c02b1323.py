"""Prepare native ClickHouse scenario fixture through the real six-BR parity gate."""
import argparse,json
from pathlib import Path
from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.migration import import_scenarios

p=argparse.ArgumentParser(description=__doc__)
for name in ('metadata','proof','coordinator','output'):p.add_argument('--'+name,type=Path,required=True)
a=p.parse_args();s=ClickHouseSettings.from_env()
if (s.host,s.database,s.user)!=('clickhouse','greenhouse_bench_ch','greenhouse_bench'):
    raise ValueError('Private benchmark endpoint/database/user required')
client=Client(s).begin_action()
try:
    result=import_scenarios(client,a.coordinator,json.loads(a.metadata.read_text()),json.loads(a.proof.read_text()))
    a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
finally:client.close()
