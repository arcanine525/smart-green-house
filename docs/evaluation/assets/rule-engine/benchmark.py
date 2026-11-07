import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
import psycopg
from greenhouse.app import wiring
from greenhouse.contracts import RuleBasis, to_json
from greenhouse.settings import Settings

output = Path(os.environ['PROFILE_OUTPUT'])
with psycopg.connect(**Settings.from_env().connection_kwargs()) as connection:
    if connection.execute('show transaction_read_only').fetchone()[0] != 'on':
        raise RuntimeError('read-only required')
    cat = wiring.catalog_service(connection)
    row = next(x for x in cat.publications() if x.publication_id == 'df3e4bcbacab45a5b86fc37d2f53c00a')
    pub = wiring.resolve_publication(wiring.irrigation_service(connection), row.publication_id)
    cycle = next(x for x in cat.cycles(pub) if x.compartment_id == 'Automatoes')
    context = wiring.build_context(pub, cycle, cycle.start, cycle.end - timedelta(days=1))
    climate = wiring.climate_service(connection)
    drain = wiring.drain_service(connection)
    def run_climate():
        return climate.get_evaluation(context, basis=RuleBasis.REQUESTED,
            rule_version='climate-demo-b01:Automatoes:v1', use_cache=False).result
    for _ in range(5):
        reference = run_climate()
    timings = []
    for _ in range(30):
        start = perf_counter(); reference = run_climate(); timings.append((perf_counter()-start)*1000)
    drainage = drain.get_evaluation(context, rule_version='drain-demo-b03:Automatoes:v1', use_cache=False).result
    def digest(obj):
        return hashlib.sha256(to_json(obj).encode()).hexdigest()
    data={'utc':datetime.now(timezone.utc).isoformat(),'publication':pub.publication_id,
          'compartment':'Automatoes','warmups':5,'samples':timings,
          'p95_ms':sorted(timings)[28],'median_ms':median(timings),
          'climate_digest':digest(reference),'climate_episodes':len(reference.episodes),
          'drain_digest':digest(drainage),'drain_episodes':len(drainage.episodes)}
    output.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({k:v for k,v in data.items() if k != 'samples'},indent=2),flush=True)
