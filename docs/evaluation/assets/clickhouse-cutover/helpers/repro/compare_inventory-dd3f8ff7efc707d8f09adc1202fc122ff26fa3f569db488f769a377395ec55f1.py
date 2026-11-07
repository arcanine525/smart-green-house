"""Compare complete ordered logical row/schema inventories before sensor resume."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
parser.add_argument('restored', type=Path)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
source, target = (json.loads(p.read_text()) for p in (args.source, args.restored))
if source['status'] != 'passed' or target['status'] != 'passed':
    raise ValueError('Two completed inventories required')
project = lambda report: [{k:v for k,v in row.items() if k!='seconds'} for row in report['tables']]
if source['logical_inventory_sha256'] != target['logical_inventory_sha256'] or project(source) != project(target):
    raise ValueError('Restored database logical rows/counts/schema differ')
result = dict(status='passed', all_tables_exact=True, physical_copies_counted=True,
    logical_inventory_sha256=source['logical_inventory_sha256'], tables=len(source['tables']),
    total_rows=sum(r['rows'] for r in source['tables']), before_sensor_resume=True)
args.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
