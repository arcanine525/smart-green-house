"""Import the frozen PostgreSQL archive through the actual six-BR parity gate."""
import argparse
import json
from pathlib import Path

from common import COORDINATOR, STATE, connect, write_json
from greenhouse.clickhouse.migration import import_scenarios

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--proof', type=Path, required=True)
args = parser.parse_args()
client = connect(action=True)
try:
    result = import_scenarios(client, COORDINATOR,
        json.loads((STATE / 'source-metadata.json').read_text()),
        json.loads(args.proof.read_text()))
    write_json(STATE / 'postgres-scenario-import.json', result)
    print(json.dumps(result), flush=True)
finally:
    client.close()
